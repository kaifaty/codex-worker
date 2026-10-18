"""Run isolated unittest cases through a dynamic pool of subprocesses."""
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import unittest


def _owned_processes(root):
    # The environment also identifies detached supervisors before they publish a
    # pid, and tools whose command line contains no fixture path.
    snapshot = subprocess.run(
        ["ps", "axeww", "-o", "pid=,ppid=,pgid=,stat=,command="],
        capture_output=True, text=True, errors="replace", check=True)
    table, owned = {}, set()
    for line in snapshot.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5:
            continue
        pid, parent, group = map(int, fields[:3])
        table[pid] = (parent, group, fields[3])
        if root in fields[4] and pid != os.getpid():
            owned.add(pid)
    while True:
        children = {pid for pid, (parent, _, _) in table.items() if parent in owned}
        if children <= owned:
            break
        owned.update(children)
    return {pid: table[pid] for pid in owned
            if pid != os.getpid() and not table[pid][2].startswith("Z")}


def _cleanup(root):
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        deadline = time.monotonic() + grace
        while True:
            owned = _owned_processes(root)
            if not owned:
                return
            groups = {group for pid, (_, group, _) in owned.items() if pid == group}
            for group in groups - {0, 1, os.getpgrp()}:
                try:
                    os.killpg(group, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            for pid in owned.keys() - {0, 1, os.getpid()}:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise RuntimeError("test processes survived cleanup: %s" % sorted(_owned_processes(root)))


def _watch(root):
    # EOF survives an unhandled exception or SIGKILL in the controller. This
    # independent session can still stop workers and their detached children.
    sys.stdin.buffer.read()
    _cleanup(root)
    shutil.rmtree(root, ignore_errors=True)


def _worker(script, directory):
    os.environ["TMPDIR"] = directory
    tempfile.tempdir = directory
    module = types.SimpleNamespace(**runpy.run_path(script, run_name="rotation_tests"))
    for line in sys.stdin:
        name = json.loads(line)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            suite = unittest.defaultTestLoader.loadTestsFromName(name, module)
            result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        message = {"name": name, "tests": result.testsRun,
                   "failures": len(result.failures) + len(result.unexpectedSuccesses),
                   "errors": len(result.errors),
                   "skipped": len(result.skipped), "success": result.wasSuccessful(),
                   "output": output.getvalue()}
        print(json.dumps(message), flush=True)


def _read_results(worker, events):
    try:
        for line in worker.stdout:
            events.put((worker, json.loads(line)))
    except Exception:
        events.put((worker, {"runner_error": traceback.format_exc()}))
    finally:
        events.put((worker, None))


def _cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _cases(test)
        else:
            yield test


def run(module):
    """Keep named tests and the one-job mode on the standard unittest path."""
    if len(sys.argv) > 1:
        unittest.main(verbosity=2)
        return
    try:
        jobs = int(os.environ.get("CODEX_TEST_JOBS", str(os.cpu_count() or 1)))
        if jobs < 1:
            raise ValueError
    except ValueError:
        sys.exit("CODEX_TEST_JOBS must be a positive integer")
    if jobs == 1:
        unittest.main(verbosity=2)
        return
    tests = [test.__class__.__name__ + "." + test._testMethodName
             for test in _cases(unittest.defaultTestLoader.loadTestsFromModule(module))]
    pending = iter(tests)
    jobs = min(jobs, len(tests))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    started = time.monotonic()
    workers, active = [], {}
    events = queue.Queue()
    runner_errors = []
    interrupted = False
    helper = str(Path(__file__).resolve())
    directory = tempfile.mkdtemp(prefix="codex-rotation-run-")
    watcher = None
    try:
        watcher = subprocess.Popen([sys.executable, "-I", helper, "--watch", directory],
                                   stdin=subprocess.PIPE, start_new_session=True)
        for index in range(jobs):
            workdir = str(Path(directory) / str(index))
            os.mkdir(workdir)
            worker = subprocess.Popen(
                [sys.executable, "-I", helper, "--worker", module.__file__, workdir],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, start_new_session=True)
            workers.append(worker)
            threading.Thread(target=_read_results, args=(worker, events), daemon=True).start()
            name = next(pending)
            active[worker] = name
            worker.stdin.write(json.dumps(name) + "\n")
            worker.stdin.flush()
        print("Running %d tests with %d workers" % (len(tests), jobs), file=sys.stderr, flush=True)
        while active:
            if watcher.poll() is not None:
                raise RuntimeError("test cleanup watchdog exited unexpectedly")
            try:
                worker, result = events.get(timeout=0.1)
            except queue.Empty:
                # A descendant can inherit stdout and keep its pipe open after
                # a worker crashes, so EOF alone cannot detect worker death.
                for worker, name in active.items():
                    if worker.poll() is not None:
                        raise RuntimeError("worker exited during %s (exit %s)" %
                                           (name, worker.returncode))
                continue
            if result is None:
                if worker in active:
                    raise RuntimeError("worker exited during %s (exit %s)" %
                                       (active[worker], worker.poll()))
                continue
            if "runner_error" in result:
                raise RuntimeError(result["runner_error"])
            if result["name"] != active.pop(worker):
                raise RuntimeError("worker returned an unexpected test result")
            for key in counts:
                counts[key] += result[key]
            outcome = ("ok" if result["success"] else "FAILED")
            if result["skipped"]:
                outcome = "skipped"
            print("%s ... %s" % (result["name"], outcome), file=sys.stderr, flush=True)
            if not result["success"]:
                print(result["output"], file=sys.stderr, end="", flush=True)
            name = next(pending, None)
            if name is not None:
                active[worker] = name
                worker.stdin.write(json.dumps(name) + "\n")
                worker.stdin.flush()
            else:
                worker.stdin.close()
    except KeyboardInterrupt:
        interrupted = True
        runner_errors.append("Interrupted")
    except Exception:
        runner_errors.append(traceback.format_exc())
    finally:
        # The watchdog performs the same cleanup even if this finally block is
        # skipped because the controller is killed.
        if watcher is not None:
            watcher.stdin.close()
            try:
                if watcher.wait(timeout=10) != 0:
                    _cleanup(directory)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.wait()
                _cleanup(directory)
        else:
            _cleanup(directory)
        for worker in workers:
            worker.wait()
            worker.stdout.close()
            if not worker.stdin.closed:
                worker.stdin.close()
        shutil.rmtree(directory, ignore_errors=True)
    elapsed = time.monotonic() - started
    for error in runner_errors:
        print(error, file=sys.stderr)
    counts["errors"] += len(runner_errors)
    print("\nRan %d tests in %.3fs" % (counts["tests"], elapsed), file=sys.stderr)
    if counts["tests"] != len(tests):
        print("%d of %d tests did not finish" % (len(tests) - counts["tests"], len(tests)),
              file=sys.stderr)
    success = not (counts["failures"] or counts["errors"])
    print("%s (failures=%d, errors=%d, skipped=%d)" %
          ("OK" if success else "FAILED", counts["failures"], counts["errors"], counts["skipped"]),
          file=sys.stderr)
    sys.exit(130 if interrupted else (0 if success else 1))


if __name__ == "__main__":
    if sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "--watch":
        _watch(sys.argv[2])
