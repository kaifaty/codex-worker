"""Read-only machine probes, validated snapshots and in-memory sample history."""

import asyncio
import json
import logging
import math
import os
import re
import shlex
import signal
import time
from pathlib import Path

PROBE_TIMEOUT = 15
OUTPUT_LIMIT = 1024 * 1024
HISTORY_LIMIT = 120
STRING_LIMIT = 200
CPU_BUSY = 0.7
CPU_OVERLOADED = 1.5
MEMORY_BUSY = 0.25
MEMORY_OVERLOADED = 0.10
DISK_BUSY = 0.15
DISK_OVERLOADED = 0.05
GPU_BUSY = 70
GPU_MEMORY_BUSY = 0.60
PROCESS_MIN_BYTES = 100 * 1024 * 1024
MACHINE_ID = re.compile(r"[a-z0-9-]+")
SSH_TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*")
LOGGER = logging.getLogger(__name__)


def machine_status(metrics, failures):
    if metrics is None:
        return ("offline" if failures else "unknown"), []
    if failures >= 2:
        return "offline", []
    busy, overloaded = [], []
    cpu = metrics["cpu"]
    if cpu and cpu["load1"] / cpu["cores"] >= CPU_BUSY:
        busy.append("cpu")
        if cpu["load1"] / cpu["cores"] >= CPU_OVERLOADED:
            overloaded.append("cpu")
    memory = metrics["memory"]
    if memory:
        available = memory["available_bytes"] / memory["total_bytes"]
        if available < MEMORY_BUSY:
            busy.append("memory")
            if available < MEMORY_OVERLOADED:
                overloaded.append("memory")
    if any(gpu["utilization_percent"] is not None and gpu["utilization_percent"] >= GPU_BUSY
           for gpu in metrics["gpus"]):
        busy.append("gpu")
    if any(gpu["memory_used_bytes"] is not None and gpu["memory_total_bytes"]
           and gpu["memory_used_bytes"] / gpu["memory_total_bytes"] >= GPU_MEMORY_BUSY
           for gpu in metrics["gpus"]):
        busy.append("gpu_memory")
    for disk in metrics["disks"]:
        if disk["total_bytes"] and disk["free_bytes"] / disk["total_bytes"] < DISK_BUSY:
            busy.append(("disk:" + disk["mount"])[:STRING_LIMIT])
            if disk["free_bytes"] / disk["total_bytes"] < DISK_OVERLOADED:
                overloaded.append(("disk:" + disk["mount"])[:STRING_LIMIT])
    return ("overloaded", overloaded) if overloaded else (("busy", busy) if busy else ("free", []))


def _string(value):
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value[:STRING_LIMIT]


def _number(value, minimum=0, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise ValueError("Number out of range")
    if integer:
        if not number.is_integer():
            raise ValueError("Expected an integer")
        return int(number)
    return number


def _optional_number(value, **kwargs):
    return None if value is None else _number(value, **kwargs)


def _cpu(value):
    if value is None:
        return None
    return {"cores": _number(value["cores"], minimum=1, integer=True),
            "load1": _number(value["load1"]), "load5": _number(value["load5"])}


def _memory(value):
    if value is None:
        return None
    total = _number(value["total_bytes"], minimum=1, integer=True)
    return {"total_bytes": total,
            "available_bytes": _number(value["available_bytes"], maximum=total, integer=True)}


def _process_name(value):
    if not isinstance(value, str):
        raise ValueError("Expected a process name")
    parts = value.split("/")
    base = parts[-1]
    if base.startswith("python") or base in ("node", "java"):
        for marker in (".venv", "venv"):
            if marker in parts[1:-1]:
                return parts[parts.index(marker) - 1][:STRING_LIMIT]
    return base[:STRING_LIMIT]


def normalize_metrics(raw):
    """Project untrusted JSON onto the public schema; reject malformed sections."""
    try:
        if not isinstance(raw, dict) or not isinstance(raw["gpus"], list) or not isinstance(raw["disks"], list):
            raise ValueError("Expected metrics object")
        result = {"os": _string(raw["os"]), "cpu": _cpu(raw["cpu"]),
                  "memory": _memory(raw["memory"]), "gpus": [], "disks": []}
        if result["cpu"] and not math.isfinite(result["cpu"]["load1"] / result["cpu"]["cores"] * 100):
            raise ValueError("Load percentage out of range")
        for gpu in raw["gpus"]:
            if not isinstance(gpu["processes"], list):
                raise ValueError("Expected processes list")
            total = _optional_number(gpu["memory_total_bytes"], minimum=1, integer=True)
            processes = [{"name": _process_name(item["name"]),
                          "memory_bytes": _number(item["memory_bytes"], integer=True)}
                         for item in gpu["processes"]]
            result["gpus"].append({
                "name": _string(gpu["name"]),
                "utilization_percent": _optional_number(gpu["utilization_percent"], maximum=100),
                "memory_used_bytes": _optional_number(gpu["memory_used_bytes"], maximum=total, integer=True),
                "memory_total_bytes": total,
                "processes": sorted((p for p in processes if p["memory_bytes"] >= PROCESS_MIN_BYTES),
                                    key=lambda p: p["memory_bytes"], reverse=True)[:5],
            })
        for disk in raw["disks"]:
            total = _number(disk["total_bytes"], integer=True)
            mount = _string(disk["mount"])
            if not mount.startswith("/"):
                raise ValueError("Expected absolute mount")
            result["disks"].append({"mount": mount, "total_bytes": total,
                                    "free_bytes": _number(disk["free_bytes"], maximum=total, integer=True)})
        return result
    except (KeyError, TypeError, OverflowError, ValueError) as exc:
        raise ValueError("Invalid metrics") from exc


def _target(config):
    local = config.get("local") is True
    ssh = config.get("ssh")
    if local == ("ssh" in config) or ("local" in config and not local):
        raise ValueError("Choose exactly one of local: true or ssh")
    if not local and (not isinstance(ssh, str) or not SSH_TARGET.fullmatch(ssh)):
        raise ValueError("Invalid ssh target")


def _parse_config(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("machines"), list):
        raise ValueError("Expected a machines list")
    result, seen = [], set()
    for item in raw["machines"]:
        if not isinstance(item, dict):
            raise ValueError("Expected a machine object")
        machine_id = item.get("id")
        if not isinstance(machine_id, str) or not MACHINE_ID.fullmatch(machine_id):
            raise ValueError("Invalid machine id")
        if machine_id in seen:
            raise ValueError("Duplicate machine id")
        seen.add(machine_id)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("Machine name is required")
        _target(item)
        if item.get("role") is not None and not isinstance(item["role"], str):
            raise ValueError("Invalid machine role")
        disks = item.get("disks", ["/"])
        if not isinstance(disks, list) or any(not isinstance(p, str) or not p.startswith("/")
                                              or "\x00" in p for p in disks):
            raise ValueError("Disks must be absolute paths")
        host = item.get("host")
        if host is not None:
            if not isinstance(host, dict) or not isinstance(host.get("name"), str) or not host["name"].strip():
                raise ValueError("Host name is required")
            _target(host)
            if "ssh" not in host:
                raise ValueError("Host ssh target is required")
        result.append({"id": machine_id, "name": item["name"][:STRING_LIMIT],
                       "role": item.get("role"), "local": item.get("local", False),
                       "ssh": item.get("ssh"), "disks": disks,
                       "host": {"name": host["name"][:STRING_LIMIT], "ssh": host["ssh"]} if host else None})
    return result


class _ProbeOutput(asyncio.SubprocessProtocol):
    """Bound retained bytes without paused StreamReader pipes blocking process cleanup."""

    def __init__(self):
        self.done = asyncio.get_running_loop().create_future()
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.too_large = False
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def stop(self):
        # The isolated process group includes local probe commands and their inherited pipes.
        try:
            os.killpg(self.transport.get_pid(), signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.transport.close()

    def pipe_data_received(self, fd, data):
        output = self.stdout if fd == 1 else self.stderr
        remaining = OUTPUT_LIMIT - len(output)
        output.extend(data[:remaining])
        if fd == 1 and len(data) > remaining:
            self.too_large = True
            self.stop()

    def connection_lost(self, exc):
        self.done.set_result(None)


async def run_probe(config, ssh_command, host=False):
    """Send the standalone probe over stdin; bound output, lifetime and cleanup."""
    transport = None
    output = _ProbeOutput()
    try:
        source = Path(__file__).with_name("probe.py").read_bytes()
        arguments = ["--host"] if host else config["disks"]
        if config.get("local"):
            command = ["python3", "-I", "-", *arguments]
        else:
            remote = "python3 -I -" + "".join(" " + shlex.quote(p) for p in arguments)
            command = [ssh_command, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--",
                       config["ssh"], remote]

        async def execute():
            nonlocal transport
            transport, _ = await asyncio.get_running_loop().subprocess_exec(
                lambda: output, *command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdin = transport.get_pipe_transport(0)
            stdin.write(source)
            stdin.close()
            await asyncio.shield(output.done)

        await asyncio.wait_for(execute(), PROBE_TIMEOUT)
        if output.too_large:
            return None, "bad_output"
        code = transport.get_returncode()
        if code:
            return None, "unreachable" if not config.get("local") and code == 255 else "probe_failed"
        return normalize_metrics(json.loads(output.stdout)), None
    except asyncio.TimeoutError:
        return None, "timeout"
    except (ValueError, UnicodeError, RecursionError):
        return None, "bad_output"
    except OSError:
        return None, "probe_failed"
    finally:
        if transport is not None:
            if not output.done.done():
                output.stop()
            transport.close()
            # connection_lost runs only once the process has been reaped and pipes closed.
            await output.done
        if output.stderr:
            LOGGER.warning("Machine probe stderr: %s", output.stderr.decode("utf-8", "replace").strip())


def _history(metrics, at):
    cpu, memory = metrics["cpu"], metrics["memory"]
    gpu_loads = [gpu["utilization_percent"] for gpu in metrics["gpus"]
                 if gpu["utilization_percent"] is not None]
    return {"at": at, "load_percent": cpu["load1"] / cpu["cores"] * 100 if cpu else None,
            "memory_percent": (1 - memory["available_bytes"] / memory["total_bytes"]) * 100 if memory else None,
            "gpu_percent": max(gpu_loads) if gpu_loads else None}


class Machines:
    def __init__(self, path, ssh_command="ssh"):
        self.path = path
        self.ssh_command = ssh_command
        self.value = {"config_error": None, "items": []}
        self._signature = None
        self._configs = []
        self._failures = {}
        self._lock = asyncio.Lock()
        self._load_config()

    def _load_config(self):
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            signature = (None, None)
        except OSError:
            self.value = {**self.value, "config_error": "Cannot read machine configuration"}
            return
        if signature == self._signature:
            return
        self._signature = signature
        try:
            configs = [] if signature == (None, None) else _parse_config(json.loads(self.path.read_text()))
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            reason = str(exc) if type(exc) is ValueError else "Invalid machine configuration"
            self.value = {**self.value, "config_error": reason[:STRING_LIMIT]}
            return
        previous = {c["id"]: c for c in self._configs}
        old_items = {item["id"]: item for item in self.value["items"]}
        items = []
        for config in configs:
            machine_id = config["id"]
            if previous.get(machine_id) == config:
                items.append(old_items[machine_id])
                continue
            self._failures[machine_id] = 0
            items.append({"id": machine_id, "name": config["name"],
                          "role": config["role"][:STRING_LIMIT] if config["role"] is not None else None,
                          "status": "unknown", "reasons": [], "checked_at": None, "sampled_at": None,
                          "error": None, "metrics": None, "history": [],
                          "host": {"name": config["host"]["name"], "cpu": None, "memory": None,
                                   "error": None} if config["host"] else None})
        self._failures = {c["id"]: self._failures[c["id"]] for c in configs}
        self._configs = configs
        self.value = {"config_error": None, "items": items}

    def _replace(self, machine_id, **updates):
        self.value = {**self.value, "items": [dict(item, **updates) if item["id"] == machine_id else item
                                             for item in self.value["items"]]}

    async def _sample(self, config, host=False):
        metrics, error = await run_probe(config["host"] if host else config, self.ssh_command, host=host)
        item = next(item for item in self.value["items"] if item["id"] == config["id"])
        if host:
            self._replace(config["id"], host={"name": config["host"]["name"],
                          "cpu": metrics["cpu"] if metrics else item["host"]["cpu"],
                          "memory": metrics["memory"] if metrics else item["host"]["memory"], "error": error})
            return
        now = time.time()
        self._failures[config["id"]] = self._failures[config["id"]] + 1 if error else 0
        metrics = metrics if not error else item["metrics"]
        status, reasons = machine_status(metrics, self._failures[config["id"]])
        self._replace(config["id"], checked_at=now, sampled_at=item["sampled_at"] if error else now,
                      error=error, metrics=metrics, status=status, reasons=reasons,
                      history=item["history"] if error else (item["history"] + [_history(metrics, now)])[-HISTORY_LIMIT:])

    async def refresh(self):
        async with self._lock:
            before = self.value
            self._load_config()
            await asyncio.gather(*(self._sample(config, host=host) for config in self._configs
                                   for host in ([False, True] if config["host"] else [False])))
            def comparable(value):
                return {**value, "items": [{k: v for k, v in item.items() if k != "checked_at"}
                                          for item in value["items"]]}
            return self.value if comparable(before) != comparable(self.value) else None
