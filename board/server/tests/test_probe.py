"""Standalone probe compatibility, read-only execution and command-output samples."""

import ast
import json
import subprocess
from pathlib import Path

from codex_worker_board import probe


def test_memory_and_apple_samples():
    assert probe.parse_meminfo("MemTotal: 1000 kB\nMemFree: 40 kB\nMemAvailable: 300 kB\n") == {
        "total_bytes": 1024000, "available_bytes": 307200,
    }
    assert probe.parse_meminfo("MemTotal: 1000 kB\n") is None
    assert probe.parse_memory_pressure(
        "Pages free: 123\nSystem-wide memory free percentage: 77%\n", 10000
    ) == {"total_bytes": 10000, "available_bytes": 7700}
    assert probe.parse_memory_pressure("unavailable", 10000) is None
    assert probe.parse_ioreg(
        '+-o AGXAccelerator\n | "PerformanceStatistics" = {"Device Utilization %"=37}\n',
        "Apple Example\n",
    ) == [{"name": "Apple Example", "utilization_percent": 37,
           "memory_used_bytes": None, "memory_total_bytes": None, "processes": []}]


def test_nvidia_samples_uuid_mapping_na_and_top_processes():
    apps = "\n".join(
        f"GPU-B, /x/project/.venv/bin/python3, {size}" for size in range(100, 107)
    ) + "\nGPU-B, /x/build/bin/llama-server, 99\nGPU-A, /usr/bin/node, [N/A]\n"
    result = probe.parse_nvidia_smi(
        '7, GPU-A, "Example, A", [N/A], [N/A], 8192\n'
        '2, GPU-B, Example B, 75, 2048, 4096\n', apps,
    )
    assert result[0] == {
        "name": "Example, A", "utilization_percent": None,
        "memory_used_bytes": None, "memory_total_bytes": 8192 * probe.MIB,
        "processes": [],
    }
    assert result[1]["utilization_percent"] == 75
    assert result[1]["memory_used_bytes"] == 2048 * probe.MIB
    assert result[1]["processes"] == [
        {"name": "project", "memory_bytes": size * probe.MIB}
        for size in (106, 105, 104, 103, 102)
    ]
    assert probe.parse_nvidia_smi("0, GPU-X, Example, 0, 0, 1000", "GPU-X, /bin/a, 100")[0][
        "processes"
    ] == [{"name": "a", "memory_bytes": 100 * probe.MIB}]


def test_process_name_has_no_paths():
    for path, expected in [
        ("/x/decider/.venv/bin/python", "decider"),
        ("/x/decider/venv/bin/python3.9", "decider"),
        ("/x/build/bin/llama-server", "llama-server"),
        ("/x/app/.venv/bin/node", "app"),
        ("/x/service/venv/bin/java", "service"),
        ("/usr/bin/python3", "python3"),
    ]:
        assert probe.process_name(path) == expected


def test_commands_are_bounded_and_have_no_stdin(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"sample")

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.command(["example"]) == "sample"
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["timeout"] == 5

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(probe.subprocess, "run", missing)
    assert probe.gpus("linux") == []


def test_python39_source_and_real_isolated_local_probe(tmp_path):
    source = Path(probe.__file__).read_text()
    ast.parse(source, feature_version=(3, 9))
    result = subprocess.run(
        ["python3", "-I", "-", "/", str(tmp_path / "missing")],
        input=source, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)
    assert set(metrics) == {"os", "cpu", "memory", "gpus", "disks"}
    assert isinstance(metrics["os"], str)
    assert metrics["cpu"] is None or metrics["cpu"]["cores"] > 0
    assert metrics["memory"] is None or metrics["memory"]["total_bytes"] > 0
    assert isinstance(metrics["gpus"], list)
    assert len(metrics["disks"]) == 1
    assert metrics["disks"][0]["mount"] == "/"
    assert metrics["disks"][0]["total_bytes"] > 0
    assert metrics["disks"][0]["free_bytes"] >= 0


def test_host_probe_skips_gpu_and_disks(monkeypatch):
    def unexpected(*args):
        raise AssertionError("Host sampling must not query GPUs")

    monkeypatch.setattr(probe, "gpus", unexpected)
    metrics = probe.sample([], host=True)
    assert metrics["gpus"] == []
    assert metrics["disks"] == []
    assert set(metrics) == {"os", "cpu", "memory", "gpus", "disks"}
