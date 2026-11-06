"""Read-only, standalone Python 3.9 machine probe, sent to Python over stdin."""

import csv
import io
import math
import os
import re
import subprocess
import sys
import json
import time

COMMAND_TIMEOUT = 5
CPU_SAMPLE_SECONDS = 1
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


def parse_proc_stat(text):
    """Return (busy, total) jiffies from the aggregate `cpu` line of /proc/stat; idle is idle + iowait."""
    lines = text.splitlines()
    parts = lines[0].split() if lines else []
    if len(parts) < 6 or parts[0] != "cpu":
        return None
    try:
        values = [int(value) for value in parts[1:9]]
    except ValueError:
        return None
    if any(value < 0 for value in values):
        return None
    total = sum(values)
    return total - values[3] - values[4], total


def cpu_percent_between(first, second):
    """Busy share of all cores between two /proc/stat samples, or None."""
    if first is None or second is None:
        return None
    total = second[1] - first[1]
    busy = second[0] - first[0]
    if total <= 0 or busy < 0:
        return None
    return round(min(100.0, busy / total * 100), 2)


def parse_top_cpu(text):
    """`100 - idle` from the last `CPU usage:` line of `top -l 2 -n 0 -s 1`; the first sample is since boot."""
    matches = re.findall(
        r"CPU usage:\s*[0-9.]+% user,\s*[0-9.]+% sys,\s*([0-9.]+)% idle", text
    )
    if not matches:
        return None
    idle = number(matches[-1])
    if idle is None or idle > 100:
        return None
    return round(100 - idle, 2)


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


def read_proc_stat():
    try:
        with open("/proc/stat", encoding="utf-8") as source:
            return parse_proc_stat(source.read())
    except OSError:
        return None


def cpu_percent(os_name):
    """Busy share of all cores over about one second, or None."""
    if os_name == "linux":
        first = read_proc_stat()
        if first is None:
            return None
        time.sleep(CPU_SAMPLE_SECONDS)
        return cpu_percent_between(first, read_proc_stat())
    if os_name == "darwin":
        text = command(["top", "-l", "2", "-n", "0", "-s", str(CPU_SAMPLE_SECONDS)])
        return parse_top_cpu(text) if text is not None else None
    return None


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
            cpu = {"cores": cores, "load1": load1, "load5": load5, "percent": cpu_percent(os_name)}
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
