#!/usr/bin/env python3
"""Measure the rotation suite without changing its runner or test selection.

Run with ``CODEX_TEST_JOBS=10 python3 -I tests/benchmark_rotation.py`` (or 1).
The JSON on stdout includes fixture time and every git process started through
PATH. ``--output PATH`` also saves that JSON. Test runner output goes to stderr.
The shell git shim and per-process timing logs are removed after each run.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


_DIRECTORY = Path(__file__).resolve().parent


def install():
    """Instrument controller and worker imports, including setUp and tearDown."""
    log_directory = os.environ.get("CODEX_ROTATION_BENCH_TIMINGS")
    if not log_directory or getattr(unittest.TestCase.run, "_rotation_benchmark", False):
        return
    original_run = unittest.TestCase.run

    def timed_run(self, result=None):
        before = {
            key: len(getattr(result, key, ()))
            for key in ("failures", "errors", "skipped", "unexpectedSuccesses")
        }
        started = time.monotonic()
        completed = None
        try:
            completed = original_run(self, result)
            return completed
        finally:
            elapsed = time.monotonic() - started
            record = {
                "name": self.__class__.__name__ + "." + self._testMethodName,
                "seconds": elapsed,
            }
            observed = completed if completed is not None else result
            for key, count in before.items():
                record[key] = len(getattr(observed, key, ())) - count
            path = Path(log_directory) / (str(os.getpid()) + ".jsonl")
            with path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record) + "\n")

    timed_run._rotation_benchmark = True
    unittest.TestCase.run = timed_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="also save the JSON summary")
    args = parser.parse_args()
    real_git = shutil.which("git")
    if real_git is None:
        parser.error("git is not on PATH")
    root = _DIRECTORY.parent
    environment = os.environ.copy()
    command = [sys.executable, "-I", "tests/test_rotation.py"]
    with tempfile.TemporaryDirectory(prefix=".rotation-benchmark-", dir=str(root)) as temporary:
        directory = Path(temporary)
        timings = directory / "timings"
        timings.mkdir()
        git_log = directory / "git.log"
        wrapper = directory / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 1 >> \"$CODEX_ROTATION_BENCH_GIT_LOG\"\n"
            "exec " + shlex.quote(real_git) + " \"$@\"\n",
            encoding="utf-8")
        wrapper.chmod(0o700)
        environment["PATH"] = str(directory) + os.pathsep + environment.get("PATH", "")
        environment["CODEX_ROTATION_BENCH_GIT_LOG"] = str(git_log)
        environment["CODEX_ROTATION_BENCH_TIMINGS"] = str(timings)
        started = time.monotonic()
        completed = subprocess.run(command, cwd=str(root), env=environment, stdout=sys.stderr)
        elapsed = time.monotonic() - started
        records = []
        for log in timings.glob("*.jsonl"):
            records.extend(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
        durations = {record["name"]: record["seconds"] for record in records}
        counts = {
            key: sum(record[key] for record in records)
            for key in ("failures", "errors", "skipped", "unexpectedSuccesses")
        }
        summary = {
            "command": command,
            "python": sys.version,
            "jobs": int(environment.get("CODEX_TEST_JOBS", str(os.cpu_count() or 1))),
            "exit_code": completed.returncode,
            "elapsed_seconds": elapsed,
            "test_count": len(records),
            "unique_test_count": len(durations),
            "counts": counts,
            "git_processes": len(git_log.read_text(encoding="utf-8").splitlines()) if git_log.exists() else 0,
            "top20": [
                {"name": name, "seconds": seconds}
                for name, seconds in sorted(durations.items(), key=lambda item: (-item[1], item[0]))[:20]
            ],
            "test_seconds": dict(sorted(durations.items())),
        }
    encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
