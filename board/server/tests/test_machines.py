"""Read-only machine collection with temporary configuration and fake SSH only."""

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from codex_worker_board import machines as machines_module
from codex_worker_board import probe
from codex_worker_board.machines import Machines, machine_status, normalize_metrics


def metrics():
    return {
        "os": "linux",
        "cpu": {"cores": 4, "load1": 0, "load5": 0, "percent": None},
        "memory": {"total_bytes": 1000, "available_bytes": 1000},
        "gpus": [],
        "disks": [{"mount": "/", "total_bytes": 1000, "free_bytes": 1000}],
    }


def write_config(path, entries):
    path.write_text(json.dumps({"machines": entries}))


def fake_ssh(tmp_path, body):
    script = tmp_path / "fake-ssh"
    script.write_text(f"#!{sys.executable}\nimport sys, json, os\nfrom pathlib import Path\n" + body)
    script.chmod(0o755)
    return str(script)


@pytest.mark.parametrize("field,value,status,reasons", [
    ("cpu", 2.799, "free", []),
    ("cpu", 2.8, "busy", ["cpu"]),
    ("cpu", 5.999, "busy", ["cpu"]),
    ("cpu", 6, "overloaded", ["cpu"]),
    ("cpu_percent", 69.99, "free", []),
    ("cpu_percent", 70, "busy", ["cpu"]),
    ("cpu_percent", 89.99, "busy", ["cpu"]),
    ("cpu_percent", 90, "overloaded", ["cpu"]),
    ("memory", 250, "free", []),
    ("memory", 249, "busy", ["memory"]),
    ("memory", 100, "busy", ["memory"]),
    ("memory", 99, "overloaded", ["memory"]),
    ("gpu", 69.999, "free", []),
    ("gpu", 70, "busy", ["gpu"]),
    ("gpu_memory", 599, "free", []),
    ("gpu_memory", 600, "busy", ["gpu_memory"]),
    ("disk", 150, "free", []),
    ("disk", 149, "busy", ["disk:/"]),
    ("disk", 50, "busy", ["disk:/"]),
    ("disk", 49, "overloaded", ["disk:/"]),
])
def test_status_threshold_boundaries(field, value, status, reasons):
    sample = metrics()
    if field == "cpu":
        sample["cpu"]["load1"] = value
    elif field == "cpu_percent":
        sample["cpu"]["percent"] = value
    elif field == "memory":
        sample["memory"]["available_bytes"] = value
    elif field == "disk":
        sample["disks"][0]["free_bytes"] = value
    else:
        sample["gpus"] = [{"name": "Test GPU", "utilization_percent": 0,
                           "memory_used_bytes": 0, "memory_total_bytes": 1000, "processes": []}]
        key = "utilization_percent" if field == "gpu" else "memory_used_bytes"
        sample["gpus"][0][key] = value
    assert machine_status(sample, 0) == (status, reasons)


def test_status_failures_and_all_triggered_reasons():
    assert machine_status(None, 0) == ("unknown", [])
    assert machine_status(None, 1) == ("offline", [])
    sample = metrics()
    sample["cpu"]["load1"] = 6
    sample["memory"]["available_bytes"] = 99
    sample["disks"][0]["free_bytes"] = 49
    sample["gpus"] = [{"name": "Test GPU", "utilization_percent": 70,
                       "memory_used_bytes": 600, "memory_total_bytes": 1000, "processes": []}]
    status, reasons = machine_status(sample, 1)
    assert status == "overloaded"
    assert set(reasons) == {"cpu", "memory", "disk:/"}
    assert len(reasons) == 3
    sample["cpu"]["load1"] = 2.8
    sample["memory"]["available_bytes"] = 249
    sample["disks"][0]["free_bytes"] = 149
    status, reasons = machine_status(sample, 0)
    assert status == "busy"
    assert set(reasons) == {"cpu", "memory", "disk:/", "gpu", "gpu_memory"}
    assert len(reasons) == 5
    assert machine_status(sample, 2) == ("offline", [])


def test_known_cpu_percent_overrides_load_average():
    sample = metrics()
    sample["cpu"].update(cores=10, load1=124, percent=36)
    assert machine_status(sample, 0) == ("free", [])
    sample["cpu"]["percent"] = None
    assert machine_status(sample, 0) == ("overloaded", ["cpu"])


def test_untrusted_metrics_are_projected_coerced_and_bounded():
    sample = metrics()
    sample["cpu"] = {"cores": "4", "load1": "2.5", "load5": "1.5", "private": "discard"}
    sample["os"] = "x" * 250
    sample["memory"] = {"total_bytes": "1000", "available_bytes": "500"}
    sample["gpus"] = [{"name": "g" * 250, "utilization_percent": "70",
                       "memory_used_bytes": None, "memory_total_bytes": None,
                       "processes": [{"name": name, "memory_bytes": "123456789"} for name in (
                           "/x/decider/.venv/bin/python", "/.venv/bin/python",
                           "/x/first/venv/nested/.venv/bin/node")],
                       "private": "discard"}]
    sample["private"] = "discard"
    clean = normalize_metrics(sample)
    assert set(clean) == {"os", "cpu", "memory", "gpus", "disks"}
    assert clean["cpu"] == {"cores": 4, "load1": 2.5, "load5": 1.5, "percent": None}
    assert normalize_metrics(dict(metrics(), cpu={"cores": 4, "load1": 1, "load5": 1, "percent": "36.5"}))[
        "cpu"]["percent"] == 36.5
    assert clean["memory"] == {"total_bytes": 1000, "available_bytes": 500}
    assert len(clean["os"]) == len(clean["gpus"][0]["name"]) == 200
    assert clean["gpus"][0]["processes"] == [
        {"name": name, "memory_bytes": 123456789} for name in ("decider", "python", "first")
    ]
    assert "discard" not in json.dumps(clean)
    for bad in ([], {"os": "linux"}, dict(metrics(), cpu={"cores": 0, "load1": 0, "load5": 0}),
                dict(metrics(), cpu={"cores": 4, "load1": "NaN", "load5": 0}),
                dict(metrics(), cpu={"cores": 1, "load1": 1e308, "load5": 0}),
                dict(metrics(), cpu={"cores": 4, "load1": 0, "load5": 0, "percent": 100.5}),
                dict(metrics(), cpu={"cores": 4, "load1": 0, "load5": 0, "percent": -1})):
        with pytest.raises(ValueError):
            normalize_metrics(bad)


def test_config_reload_and_last_valid_configuration(tmp_path, monkeypatch):
    path = tmp_path / "machines.json"
    machines = Machines(path)
    assert machines.value == {"config_error": None, "items": []}

    async def successful(config, ssh_command, host=False):
        return metrics(), None

    monkeypatch.setattr(machines_module, "run_probe", successful)

    async def check():
        entry = {"id": "laptop", "name": "Laptop", "local": True}
        write_config(path, [entry])
        await machines.refresh()
        assert machines.value["items"][0]["id"] == "laptop"
        assert machines.value["items"][0]["metrics"] == metrics()
        path.write_text('{"machines": [')
        await machines.refresh()
        assert machines.value["config_error"]
        assert len(machines.value["config_error"]) <= 200
        assert machines.value["items"][0]["id"] == "laptop"
        write_config(path, [{"id": "gpu-box", "name": "GPU box", "ssh": "gpu-box.example"}])
        await machines.refresh()
        assert machines.value["config_error"] is None
        assert [item["id"] for item in machines.value["items"]] == ["gpu-box"]
        path.unlink()
        await machines.refresh()
        assert machines.value == {"config_error": None, "items": []}

    asyncio.run(check())


@pytest.mark.parametrize("entries", [
    [{"id": "example", "name": "Example", "ssh": "-unsafe"}],
    [{"id": "example", "name": "Example", "ssh": "private-target", "local": True}],
    [{"id": "example", "name": "First", "local": True},
     {"id": "example", "name": "Second", "local": True}],
    [{"id": "Invalid ID", "name": "Example", "local": True}],
    [{"id": "example", "name": "Example", "local": True, "disks": ["relative"]}],
])
def test_invalid_config_rejects_whole_file_without_exposing_target(tmp_path, entries):
    path = tmp_path / "machines.json"
    write_config(path, entries)
    value = Machines(path).value
    assert value["items"] == []
    assert value["config_error"]
    assert "private-target" not in json.dumps(value)
    assert "-unsafe" not in json.dumps(value)


def test_fake_ssh_arguments_probe_stdin_host_and_retained_samples(tmp_path):
    path = tmp_path / "machines.json"
    log = tmp_path / "calls.jsonl"
    mode = tmp_path / "mode"
    target = "hidden-target.example"
    host_target = "hidden-host.example"
    write_config(path, [{"id": "gpu-box", "name": "GPU box", "role": "local models", "ssh": target,
                         "disks": ["/", "/data with spaces/$(literal)"],
                         "host": {"name": "Hypervisor", "ssh": host_target}}])
    sample = metrics()
    host_sample = metrics()
    host_sample["cpu"]["load1"] = 100
    ssh = fake_ssh(tmp_path,
        f"source = sys.stdin.read()\n"
        f"with Path({str(log)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps({'args': sys.argv[1:], 'source': source}) + '\\n')\n"
        f"if Path({str(mode)!r}).exists() and sys.argv[6] == {target!r}:\n"
        f"    print({target!r}, file=sys.stderr)\n    sys.exit(255)\n"
        f"print(json.dumps({host_sample!r} if sys.argv[6] == {host_target!r} else {sample!r}))\n")

    async def check():
        machines = Machines(path, ssh_command=ssh)
        assert machines.value["items"][0]["status"] == "unknown"
        assert machines.value["items"][0]["checked_at"] is None
        result = await machines.refresh()
        machine = result["items"][0]
        assert machine["status"] == "free"
        assert machine["role"] == "local models"
        assert machine["host"] == {"name": "Hypervisor", "cpu": host_sample["cpu"],
                                   "memory": host_sample["memory"], "error": None}
        assert machine["sampled_at"] == machine["history"][0]["at"]
        assert machine["history"][0] == {"at": machine["sampled_at"], "cpu_percent": 0,
                                          "memory_percent": 0, "gpu_percent": None}
        sampled_at = machine["sampled_at"]
        mode.touch()
        result = await machines.refresh()
        machine = result["items"][0]
        assert machine["status"] == "free"
        assert machine["error"] == "unreachable"
        assert machine["metrics"] == sample
        assert machine["sampled_at"] == sampled_at
        assert machine["checked_at"] >= sampled_at
        assert len(machine["history"]) == 1
        result = await machines.refresh()
        assert result["items"][0]["status"] == "offline"
        assert result["items"][0]["reasons"] == []
        assert await machines.refresh() is None
        assert target not in json.dumps(machines.value)
        assert host_target not in json.dumps(machines.value)

    asyncio.run(check())
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 8
    for call in calls:
        assert call["args"][:5] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--"]
        assert call["source"] == Path(probe.__file__).read_text()
        command = shlex.split(call["args"][6])
        if call["args"][5] == target:
            assert command == ["python3", "-I", "-", "/", "/data with spaces/$(literal)"]
        else:
            assert call["args"][5] == host_target
            assert command == ["python3", "-I", "-", "--host"]


@pytest.mark.parametrize("body,error", [
    ("print('not JSON')\n", "bad_output"),
    (f"sys.stdout.write(json.dumps({metrics()!r}) + ' ' * (2 * 1024 * 1024))\n", "bad_output"),
    ("sys.exit(8)\n", "probe_failed"),
])
def test_bad_probe_output_and_nonzero_status(tmp_path, body, error):
    path = tmp_path / "machines.json"
    write_config(path, [{"id": "example", "name": "Example", "ssh": "example"}])
    ssh = fake_ssh(tmp_path, "sys.stdin.read()\n" + body)

    async def check():
        value = await Machines(path, ssh_command=ssh).refresh()
        item = value["items"][0]
        assert item["error"] == error
        assert item["status"] == "offline"
        assert item["metrics"] is None

    asyncio.run(check())


def test_timeout_kills_and_reaps_without_blocking_fast_machine(tmp_path, monkeypatch):
    path = tmp_path / "machines.json"
    pid_path = tmp_path / "slow.pid"
    write_config(path, [{"id": "slow", "name": "Slow", "ssh": "slow.example"},
                        {"id": "fast", "name": "Fast", "ssh": "fast.example"}])
    ssh = fake_ssh(tmp_path,
        "sys.stdin.read()\n"
        "if sys.argv[6] == 'slow.example':\n"
        f"    Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "    import time\n    time.sleep(30)\n"
        f"print(json.dumps({metrics()!r}))\n")
    monkeypatch.setattr(machines_module, "PROBE_TIMEOUT", 2)

    async def check():
        machines = Machines(path, ssh_command=ssh)
        task = asyncio.create_task(machines.refresh())
        for _ in range(300):
            if machines.value["items"][1]["sampled_at"] is not None:
                break
            await asyncio.sleep(0.005)
        assert machines.value["items"][1]["status"] == "free"
        assert not task.done()
        value = await asyncio.wait_for(task, 5)
        assert value["items"][0]["error"] == "timeout"
        assert value["items"][1]["error"] is None
        assert pid_path.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)

    asyncio.run(check())


def test_history_keeps_120_latest_successes_and_cpu_percent(tmp_path, monkeypatch):
    path = tmp_path / "machines.json"
    write_config(path, [{"id": "local", "name": "Local", "local": True}])
    call = 0

    async def sample(config, ssh_command, host=False):
        nonlocal call
        call += 1
        value = metrics()
        value["cpu"]["load1"] = call
        # Even calls report a busy percentage; odd ones fall back to load, capped at 100.
        value["cpu"]["percent"] = 42 if call % 2 == 0 else None
        value["gpus"] = [{"name": "One", "utilization_percent": 20, "memory_used_bytes": None,
                          "memory_total_bytes": None, "processes": []},
                         {"name": "Two", "utilization_percent": 75, "memory_used_bytes": None,
                          "memory_total_bytes": None, "processes": []}]
        return value, None

    monkeypatch.setattr(machines_module, "run_probe", sample)

    async def check():
        machines = Machines(path)
        for _ in range(125):
            await machines.refresh()
        history = machines.value["items"][0]["history"]
        assert len(history) == 120
        assert history[0]["cpu_percent"] == 42
        assert history[-1]["cpu_percent"] == 100
        assert "load_percent" not in history[-1]
        assert history[-1]["memory_percent"] == 0
        assert history[-1]["gpu_percent"] == 75

    asyncio.run(check())
