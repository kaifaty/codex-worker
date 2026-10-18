"""Run isolated unittest cases through a dynamic pool of subprocesses."""
import contextlib
from collections import deque
import io
import json
import math
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


# Workers and watchdogs are also invoked directly with isolated Python.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from process_cleanup import cleanup_processes


def _cleanup(root):
    live = cleanup_processes(root)
    if live:
        raise RuntimeError("test processes survived cleanup: %s" % sorted(live))


def _wait_for_exit(process):
    """Never let an uncooperative worker or watchdog hold shutdown open."""
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait(timeout=1)


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
    try:
        timeout = float(os.environ.get("CODEX_TEST_TIMEOUT", "300"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except ValueError:
        sys.exit("CODEX_TEST_TIMEOUT must be a positive number of seconds")
    if jobs == 1:
        unittest.main(verbosity=2)
        return
    tests = [test.__class__.__name__ + "." + test._testMethodName
             for test in _cases(unittest.defaultTestLoader.loadTestsFromModule(module))]
    pending = deque(tests)
    jobs = min(jobs, len(tests))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    started = time.monotonic()
    workers, active, directories = [], {}, {}
    events = queue.Queue()
    runner_errors = []
    interrupted = False
    helper = str(Path(__file__).resolve())
    directory = tempfile.mkdtemp(prefix="codex-rotation-run-")
    watcher = None

    def dispatch(worker):
        name = pending.popleft()
        active[worker] = (name, time.monotonic())
        try:
            worker.stdin.write(json.dumps(name) + "\n")
            worker.stdin.flush()
        except (BrokenPipeError, OSError):
            events.put((worker, None))

    def start_worker():
        workdir = str(Path(directory) / str(len(workers)))
        os.mkdir(workdir)
        worker = subprocess.Popen(
            [sys.executable, "-I", helper, "--worker", module.__file__, workdir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, start_new_session=True)
        workers.append(worker)
        directories[worker] = workdir
        threading.Thread(target=_read_results, args=(worker, events), daemon=True).start()
        dispatch(worker)

    def retire(worker, reason):
        name, began = active.pop(worker)
        counts["tests"] += 1
        counts["errors"] += 1
        print("%s ... ERROR (%s after %.3fs)" %
              (name, reason, time.monotonic() - began), file=sys.stderr, flush=True)
        # Use this worker's directory so concurrent tests keep running. Its
        # process group and detached descendants share the same ownership scan.
        _cleanup(directories[worker])
        _wait_for_exit(worker)
        with contextlib.suppress(BrokenPipeError, OSError):
            worker.stdin.close()
        if pending:
            start_worker()

    try:
        watcher = subprocess.Popen([sys.executable, "-I", helper, "--watch", directory],
                                   stdin=subprocess.PIPE, start_new_session=True)
        for index in range(jobs):
            start_worker()
        print("Running %d tests with %d workers" % (len(tests), jobs), file=sys.stderr, flush=True)
        while active:
            if watcher.poll() is not None:
                raise RuntimeError("test cleanup watchdog exited unexpectedly")
            try:
                worker, result = events.get(timeout=0.1)
            except queue.Empty:
                # A descendant can inherit stdout and keep its pipe open after
                # a worker crashes, so EOF alone cannot detect worker death.
                for worker in list(active):
                    if worker.poll() is not None:
                        retire(worker, "worker exited during %s (exit %s)" %
                               (active[worker][0], worker.returncode))
                        break
            else:
                # Late EOF/results from a retired worker cannot affect its replacement.
                if worker in active:
                    if result is None:
                        retire(worker, "worker exited during %s (exit %s)" %
                               (active[worker][0], worker.poll()))
                    elif "runner_error" in result:
                        retire(worker, result["runner_error"])
                    else:
                        if result["name"] != active[worker][0]:
                            raise RuntimeError("worker returned an unexpected test result")
                        active.pop(worker)
                        for key in counts:
                            counts[key] += result[key]
                        outcome = ("ok" if result["success"] else "FAILED")
                        if result["skipped"]:
                            outcome = "skipped"
                        print("%s ... %s" % (result["name"], outcome), file=sys.stderr, flush=True)
                        if not result["success"]:
                            print(result["output"], file=sys.stderr, end="", flush=True)
                        if pending:
                            dispatch(worker)
                        else:
                            worker.stdin.close()
            # Cleanup can take the TERM grace period. Consume results already
            # queued by healthy workers before checking their deadlines.
            if not events.empty():
                continue
            now = time.monotonic()
            for worker, (name, began) in list(active.items()):
                if now - began >= timeout:
                    retire(worker, "timeout %.3fs exceeded" % timeout)
                    break
    except KeyboardInterrupt:
        interrupted = True
        runner_errors.append("Interrupted")
    except Exception:
        runner_errors.append(traceback.format_exc())
    finally:
        # The watchdog performs the same cleanup even if this finally block is
        # skipped because the controller is killed.
        try:
            if watcher is not None:
                watcher.stdin.close()
                if _wait_for_exit(watcher) != 0:
                    _cleanup(directory)
            else:
                _cleanup(directory)
        except Exception:
            runner_errors.append(traceback.format_exc())
        for worker in workers:
            try:
                _wait_for_exit(worker)
            except Exception:
                runner_errors.append(traceback.format_exc())
            finally:
                worker.stdout.close()
                with contextlib.suppress(BrokenPipeError, OSError):
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
