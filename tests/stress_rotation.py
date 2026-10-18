#!/usr/bin/env python3
"""Repeat reset synchronization tests with concurrent suite or CPU load.

Run with /usr/bin/python3 -I tests/stress_rotation.py --repetitions 20 --jobs 4.
The selected methods are discovered from calls to reset_after_ready, rather
than maintained as a separate list. Both runners use the current interpreter.
Use --cpu-load --load-jobs 10 for ten CPU-bound processes instead of full suites.
"""
import argparse
import ast
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest


SCRIPT = Path(__file__).resolve()
TEST_SCRIPT = SCRIPT.with_name("test_rotation.py")
RUNNER_SCRIPT = SCRIPT.with_name("rotation_runner.py")
_suite_module = None
_suite_names = []
_suite_repetitions = 0


def reset_tests():
    """Find every test method that uses the reset-deadline synchronization."""
    names = []
    source = ast.parse(TEST_SCRIPT.read_text())
    for cls in source.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef) or not method.name.startswith("test_"):
                continue
            if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "reset_after_ready" for node in ast.walk(method)):
                names.append(cls.name + "." + method.name)
    return names


def load_tests(loader, tests, pattern):
    # The one-worker path also uses this hook through standard unittest.main.
    return unittest.TestSuite(loader.loadTestsFromName(name, _suite_module)
                              for _ in range(_suite_repetitions) for name in _suite_names)


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def run_stress(args, names):
    global _suite_module, _suite_names, _suite_repetitions
    _suite_module = types.SimpleNamespace(**runpy.run_path(str(TEST_SCRIPT), run_name="rotation_tests"))
    _suite_module.load_tests = load_tests
    _suite_names, _suite_repetitions = names, args.repetitions
    os.environ["CODEX_TEST_JOBS"] = str(args.jobs)
    sys.argv = [str(SCRIPT)]
    runpy.run_path(str(RUNNER_SCRIPT))["run"](_suite_module)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=positive, default=20)
    parser.add_argument("--jobs", type=positive, default=os.cpu_count() or 1)
    parser.add_argument("--load-jobs", type=positive, default=20,
                        help="full-suite workers, or CPU processes with --cpu-load")
    parser.add_argument("--cpu-load", action="store_true",
                        help="use CPU-bound processes instead of background full suites")
    parser.add_argument("--run-stress", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    names = reset_tests()
    if not names:
        parser.error("no tests call reset_after_ready")
    if args.run_stress:
        run_stress(args, names)
        return

    print("Stress selection: %d tests x %d repetitions = %d runs; %d workers" %
          (len(names), args.repetitions, len(names) * args.repetitions, args.jobs), flush=True)
    for name in names:
        print("  " + name, flush=True)
    helper = runpy.run_path(str(RUNNER_SCRIPT))
    root = tempfile.mkdtemp(prefix="codex-rotation-stress-")
    watcher = stress = load = None
    burners = []
    load_log = None
    completed, failed = 0, 0
    started = time.monotonic()
    exitcode = 1

    def finish_load():
        nonlocal completed, failed, load_log
        completed += 1
        load_log.close()
        print("Full-suite load #%d: exit=%d" % (completed, load.returncode), flush=True)
        if load.returncode:
            failed += 1
            print(Path(load_log.name).read_text(errors="replace"), file=sys.stderr, end="", flush=True)
        load_log = None

    def start_load():
        nonlocal load, load_log
        directory = Path(root) / ("load-%d" % completed)
        directory.mkdir()
        load_log = (directory / "output.log").open("w")
        env = dict(os.environ, TMPDIR=str(directory), CODEX_TEST_JOBS=str(args.load_jobs))
        load = subprocess.Popen([sys.executable, "-I", str(TEST_SCRIPT)], env=env,
                                stdout=load_log, stderr=subprocess.STDOUT, start_new_session=True)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    def check_burners():
        for index, process in enumerate(burners):
            if process.poll() is not None:
                raise RuntimeError("CPU load process %d exited early (exit %s)" %
                                   (index, process.returncode))

    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        # Closing this pipe also cleans up detached descendants after SIGKILL.
        watcher = subprocess.Popen([sys.executable, "-I", str(RUNNER_SCRIPT), "--watch", root],
                                   stdin=subprocess.PIPE, start_new_session=True)
        if args.cpu_load:
            for index in range(args.load_jobs):
                directory = Path(root) / ("cpu-%d" % index)
                directory.mkdir()
                burners.append(subprocess.Popen(
                    [sys.executable, "-I", "-c", "while True:\n    pass\n"],
                    env=dict(os.environ, TMPDIR=str(directory)),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True))
            check_burners()
            print("CPU load: %d processes" % len(burners), flush=True)
        else:
            start_load()
        directory = Path(root) / "stress"
        directory.mkdir()
        stress = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "--run-stress", "--repetitions",
             str(args.repetitions), "--jobs", str(args.jobs)],
            env=dict(os.environ, TMPDIR=str(directory)), start_new_session=True)
        while stress.poll() is None:
            if watcher.poll() is not None:
                raise RuntimeError("stress cleanup watchdog exited unexpectedly")
            if args.cpu_load:
                check_burners()
            elif load.poll() is not None:
                finish_load()
                start_load()
            time.sleep(0.05)
        if args.cpu_load:
            check_burners()
            load_summary = "CPU load: %d processes, 0 early exits" % len(burners)
        else:
            # Finish the last load suite so an already-failing background test is
            # never hidden by cancelling that suite as soon as stress completes.
            while load.poll() is None:
                if watcher.poll() is not None:
                    raise RuntimeError("stress cleanup watchdog exited unexpectedly")
                time.sleep(0.05)
            finish_load()
            load_summary = "full-suite load: %d completed, %d failed" % (completed, failed)
        exitcode = 0 if stress.returncode == 0 and failed == 0 else 1
        print("Stress: %d scheduled runs, exit=%d; %s; %.3fs" %
              (len(names) * args.repetitions, stress.returncode, load_summary,
               time.monotonic() - started), flush=True)
    except KeyboardInterrupt:
        print("Stress run interrupted", file=sys.stderr, flush=True)
        exitcode = 130
    finally:
        # The watchdog also handles interruption and failed controller startup.
        if watcher is not None:
            watcher.stdin.close()
            try:
                if watcher.wait(timeout=10) != 0:
                    helper["_cleanup"](root)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.wait()
                helper["_cleanup"](root)
        else:
            helper["_cleanup"](root)
        for process in [stress, load] + burners:
            if process is not None:
                process.wait()
        if load_log is not None:
            load_log.close()
        shutil.rmtree(root, ignore_errors=True)
        signal.signal(signal.SIGTERM, previous_term)
    sys.exit(exitcode)


if __name__ == "__main__":
    main()
