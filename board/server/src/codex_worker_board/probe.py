"""Read-only, standalone Python 3.9 machine probe, sent to Python over stdin."""

import csv
import io
import math
import os
import re
import subprocess
import sys
import json

COMMAND_TIMEOUT = 5
MIB = 1024 * 1024
MIN_PROCESS_MEMORY = 100 * MIB


def command(args):
    """Read command output without allowing a command to consume probe input."""
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.decode("utf-8", "replace")


def parse_meminfo(text):
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable):\s*([0-9]+)\s+kB\s*", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    return {"total_bytes": values["MemTotal"], "available_bytes": values["MemAvailable"]}


def parse_memory_pressure(text, total_bytes):
    lines = text.strip().splitlines()
    match = re.fullmatch(
        r"System-wide memory free percentage:\s*([0-9]+)%\s*", lines[-1]
    ) if lines else None
    if not match or not 0 <= int(match.group(1)) <= 100:
        return None
    return {"total_bytes": total_bytes,
            "available_bytes": total_bytes * int(match.group(1)) // 100}


def process_name(path):
    name = path.rsplit("/", 1)[-1]
    if name.startswith("python") or name in ("node", "java"):
        prefix = re.split(r"/(?:\.venv|venv)/", path, maxsplit=1)
        if len(prefix) == 2 and prefix[0]:
            return prefix[0].rsplit("/", 1)[-1]
    return name


def number(text):
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def parse_nvidia_smi(gpu_text, process_text):
    """Parse index,uuid,name,utilization,memory-used,memory-total CSV and apps CSV."""
    gpus = []
    by_uuid = {}
    for row in csv.reader(io.StringIO(gpu_text), skipinitialspace=True):
        if len(row) != 6:
            continue
        _, uuid, name, utilization, used, total = (cell.strip() for cell in row)
        used, total = number(used), number(total)
        gpu = {"name": name, "utilization_percent": number(utilization),
               "memory_used_bytes": int(used * MIB) if used is not None else None,
               "memory_total_bytes": int(total * MIB) if total is not None else None,
               "processes": []}
        gpus.append(gpu)
        by_uuid[uuid] = gpu
    for row in csv.reader(io.StringIO(process_text), skipinitialspace=True):
        if len(row) != 3:
            continue
        uuid, path, used = (cell.strip() for cell in row)
        used = number(used)
        if uuid not in by_uuid or used is None or used * MIB < MIN_PROCESS_MEMORY:
            continue
        by_uuid[uuid]["processes"].append(
            {"name": process_name(path), "memory_bytes": int(used * MIB)}
        )
    for gpu in gpus:
        gpu["processes"] = sorted(
            gpu["processes"], key=lambda process: process["memory_bytes"], reverse=True
        )[:5]
    return gpus


def parse_ioreg(text, name):
    values = re.findall(r'"Device Utilization %"\s*=\s*([0-9]+)', text)
    return [{"name": name.strip(), "utilization_percent": int(value),
             "memory_used_bytes": None, "memory_total_bytes": None, "processes": []}
            for value in values]


def memory(os_name):
    if os_name == "linux":
        try:
            with open("/proc/meminfo", encoding="utf-8") as source:
                return parse_meminfo(source.read())
        except OSError:
            return None
    if os_name == "darwin":
        total = command(["sysctl", "-n", "hw.memsize"])
        pressure = command(["memory_pressure"])
        try:
            total_bytes = int(total) if total is not None else 0
        except ValueError:
            return None
        if total_bytes > 0 and pressure is not None:
            return parse_memory_pressure(pressure, total_bytes)
    return None


def gpus(os_name):
    if os_name == "darwin":
        info = command(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])
        name = command(["sysctl", "-n", "machdep.cpu.brand_string"])
        return parse_ioreg(info, name) if info is not None and name is not None else []
    if os_name == "linux":
        info = command([
            "nvidia-smi",
            "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ])
        if info is None:
            return []
        processes = command([
            "nvidia-smi", "--query-compute-apps=gpu_uuid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ])
        return parse_nvidia_smi(info, processes) if processes is not None else []
    return []


def sample(disks, host=False):
    os_name = sys.platform
    cpu = None
    try:
        cores = os.cpu_count()
        load1, load5, _ = os.getloadavg()
        if cores:
            cpu = {"cores": cores, "load1": load1, "load5": load5}
    except (AttributeError, OSError):
        pass
    mounts = []
    for mount in disks:
        try:
            stats = os.statvfs(mount)
        except OSError:
            continue
        mounts.append({"mount": mount, "total_bytes": stats.f_blocks * stats.f_frsize,
                       "free_bytes": stats.f_bavail * stats.f_frsize})
    return {"os": os_name, "cpu": cpu, "memory": memory(os_name),
            "gpus": [] if host else gpus(os_name), "disks": mounts}


if __name__ == "__main__":
    host = sys.argv[1:] == ["--host"]
    print(json.dumps(sample([] if host else sys.argv[1:], host=host)))
