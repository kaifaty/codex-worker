"""Exercise the subprocess runner with small, fake-only unittest modules."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest


TESTS = Path(__file__).resolve().parent


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="codex-runner-meta-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.addCleanup(self._stop_leftover)

    def _stop_leftover(self):
        metadata = self.root / "detached.json"
        if metadata.exists():
            pid = json.loads(metadata.read_text())["pid"]
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run(self, source, timeout=1, jobs=2):
        script = self.root / "synthetic_tests.py"
        script.write_text(textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import signal
            import subprocess
            import sys
            import tempfile
            import time
            import unittest

            sys.path.insert(0, %r)
            import rotation_runner

            FIXTURE = Path(os.environ["RUNNER_META_FIXTURE"])
        """ % str(TESTS)) + "\n" + textwrap.dedent(source) + textwrap.dedent("""\

            if __name__ == "__main__":
                rotation_runner.run(sys.modules[__name__])
        """))
        temporary = self.root / "tmp"
        temporary.mkdir()
        env = dict(os.environ, CODEX_TEST_JOBS=str(jobs), CODEX_TEST_TIMEOUT=str(timeout),
                   RUNNER_META_FIXTURE=str(self.root), TMPDIR=str(temporary))
        process = subprocess.Popen(
            [sys.executable, "-I", str(script)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            # A regression may leave an unmarked child holding both pipes.
            # Release it before collecting diagnostics from the controller.
            self._stop_leftover()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
                self.fail("runner and its cleanup watchdog exceeded 30 seconds")
            self.fail("runner exceeded 20 seconds:\n" + stdout + stderr)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def _assert_summary(self, result, tests, failures=0, errors=0, skipped=0):
        self.assertEqual(result.returncode, int(bool(failures or errors)), result.stderr)
        self.assertRegex(result.stderr, r"\bRan %d tests in \d+\.\d+s\b" % tests)
        expected = "%s (failures=%d, errors=%d, skipped=%d)" % (
            "FAILED" if failures or errors else "OK", failures, errors, skipped)
        self.assertIn(expected, result.stderr)

    def _assert_crash_continues(self, action):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_crashes(self):
                    %s

                def test_b_waits_for_replacement(self):
                    # Replacement startup competes with the full stress load.
                    deadline = time.monotonic() + 4
                    while not (FIXTURE / "replacement").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)

                def test_c_runs_in_replacement(self):
                    (FIXTURE / "replacement").write_text(str(os.getpid()))
        """ % action, timeout=5)
        self._assert_summary(result, tests=3, errors=1)
        self.assertTrue((self.root / "replacement").exists(), result.stderr)
        self.assertIn("SyntheticTests.test_b_waits_for_replacement ... ok", result.stderr)
        self.assertIn("SyntheticTests.test_c_runs_in_replacement ... ok", result.stderr)
        self.assertRegex(result.stderr,
                         r"worker exited during SyntheticTests\.test_a_crashes.*"
                         r"after \d+\.\d+s")

    def test_runner_recovers_from_worker_eof(self):
        self._assert_crash_continues("os._exit(17)")

    def test_runner_recovers_from_worker_sigkill(self):
        self._assert_crash_continues("os.kill(os.getpid(), signal.SIGKILL)")

    def test_runner_crash_does_not_kill_worker_with_shared_directory_prefix(self):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                pass

            def healthy(self):
                if self._testMethodName == "test_10":
                    (FIXTURE / "worker-10-ready").touch()
                time.sleep(0.8)

            def crashes(self):
                deadline = time.monotonic() + 4
                while not (FIXTURE / "worker-10-ready").exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                os._exit(17)

            for index in range(11):
                setattr(SyntheticTests, "test_%02d" % index, healthy)
            SyntheticTests.test_01 = crashes
        """, timeout=5, jobs=11)
        self._assert_summary(result, tests=11, errors=1)
        self.assertIn("SyntheticTests.test_10 ... ok", result.stderr)

    def test_runner_crash_cleans_child_without_directory_hints(self):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_crashes_with_child(self):
                    read_fd, write_fd = os.pipe()
                    child = subprocess.Popen(
                        [sys.executable, "-I", "-c",
                         "import os, signal, sys, time; "
                         "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                         "os.write(int(sys.argv[1]), b'1'); "
                         "os.close(int(sys.argv[1])); time.sleep(60)", str(write_fd)],
                        env={}, pass_fds=(write_fd,))
                    (FIXTURE / "detached.json").write_text(json.dumps({"pid": child.pid}))
                    os.close(write_fd)
                    try:
                        self.assertEqual(os.read(read_fd, 1), b"1")
                    finally:
                        os.close(read_fd)
                    os._exit(17)

                def test_b_passes(self):
                    self.assertTrue(True)

                def test_c_passes(self):
                    self.assertTrue(True)
        """, timeout=5)
        self._assert_summary(result, tests=3, errors=1)
        self.assertIn("(exit 17)", result.stderr)
        self.assertIn("SyntheticTests.test_b_passes ... ok", result.stderr)
        self.assertIn("SyntheticTests.test_c_passes ... ok", result.stderr)
        metadata = json.loads((self.root / "detached.json").read_text())
        status = subprocess.run(
            ["ps", "-p", str(metadata["pid"]), "-o", "stat="],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith("Z"),
                        "unmarked child is still holding worker stdout: " + status.stdout)

    def _assert_neighbor_deadline_during_cleanup(self, delay, overdue):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_crashes_with_child(self):
                    read_fd, write_fd = os.pipe()
                    child = subprocess.Popen(
                        [sys.executable, "-I", "-c",
                         "import os, signal, sys, time; "
                         "from pathlib import Path; "
                         "signal.signal(signal.SIGTERM, "
                         "lambda *_: Path(sys.argv[2]).touch()); "
                         "os.write(int(sys.argv[1]), b'1'); "
                         "os.close(int(sys.argv[1])); time.sleep(60)",
                         str(write_fd), str(FIXTURE / "cleanup-started")],
                        env={}, pass_fds=(write_fd,))
                    (FIXTURE / "detached.json").write_text(json.dumps({"pid": child.pid}))
                    os.close(write_fd)
                    try:
                        self.assertEqual(os.read(read_fd, 1), b"1")
                    finally:
                        os.close(read_fd)
                    deadline = time.monotonic() + 4
                    while not (FIXTURE / "neighbor-started").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    os._exit(17)

                def test_b_completes_during_cleanup(self):
                    (FIXTURE / "neighbor-started").touch()
                    deadline = time.monotonic() + 4
                    while not (FIXTURE / "cleanup-started").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    time.sleep(%s)
                    (FIXTURE / "neighbor-finished").touch()

                def test_c_passes(self):
                    self.assertTrue(True)
        """ % delay)
        self._assert_summary(result, tests=3, errors=2 if overdue else 1)
        self.assertIn("(exit 17)", result.stderr)
        self.assertTrue((self.root / "neighbor-finished").exists(), result.stderr)
        self.assertIn("SyntheticTests.test_c_passes ... ok", result.stderr)
        if overdue:
            self.assertNotIn("SyntheticTests.test_b_completes_during_cleanup ... ok",
                             result.stderr)
            self.assertRegex(result.stderr,
                             r"SyntheticTests\.test_b_completes_during_cleanup.*"
                             r"timeout.*\d+\.\d+s")
        else:
            self.assertIn("SyntheticTests.test_b_completes_during_cleanup ... ok",
                          result.stderr)

    def test_runner_rejects_overdue_result_received_during_cleanup(self):
        self._assert_neighbor_deadline_during_cleanup(delay=1.5, overdue=True)

    def test_runner_keeps_on_time_result_received_during_cleanup(self):
        self._assert_neighbor_deadline_during_cleanup(delay=0.2, overdue=False)

    def test_runner_times_out_hanging_test_and_continues(self):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_hangs(self):
                    while True:
                        time.sleep(60)

                def test_b_passes(self):
                    self.assertTrue(True)

                def test_c_passes(self):
                    self.assertEqual(2 + 2, 4)
        """)
        self._assert_summary(result, tests=3, errors=1)
        self.assertIn("SyntheticTests.test_b_passes ... ok", result.stderr)
        self.assertIn("SyntheticTests.test_c_passes ... ok", result.stderr)
        self.assertRegex(result.stderr,
                         r"SyntheticTests\.test_a_hangs.*(?:timeout|timed out).*"
                         r"\d+\.\d+s")

    def test_runner_cleans_detached_process_before_removing_directory(self):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_leaves_detached_process(self):
                    ready = FIXTURE / "detached-ready"
                    child = subprocess.Popen(
                        [sys.executable, "-I", "-c",
                         "import os, signal, sys, time; "
                         "from pathlib import Path; "
                         "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                         "Path(sys.argv[1]).write_text(str(os.getpid())); "
                         "time.sleep(120)", str(ready)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                    (FIXTURE / "detached.json").write_text(json.dumps({
                        "pid": child.pid,
                        "directory": str(Path(tempfile.gettempdir()).parent)}))
                    deadline = time.monotonic() + 1.5
                    while not ready.exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)

                def test_b_passes(self):
                    self.assertTrue(True)
        """, timeout=2)
        self._assert_summary(result, tests=2)
        metadata = json.loads((self.root / "detached.json").read_text())
        status = subprocess.run(
            ["ps", "-p", str(metadata["pid"]), "-o", "stat="],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith("Z"),
                        "detached process is still running: " + status.stdout)
        self.assertFalse(Path(metadata["directory"]).exists(), result.stderr)

    def test_runner_reports_failure_error_and_skip_counts(self):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_passes(self):
                    self.assertEqual(1, 1)

                def test_b_fails(self):
                    self.fail("synthetic failure")

                def test_c_errors(self):
                    raise RuntimeError("synthetic error")

                @unittest.skip("synthetic skip")
                def test_d_skips(self):
                    pass
        """, timeout=2)
        self._assert_summary(result, tests=4, failures=1, errors=1, skipped=1)
        self.assertIn("synthetic failure", result.stderr)
        self.assertIn("synthetic error", result.stderr)
        self.assertIn("SyntheticTests.test_d_skips ... skipped", result.stderr)

    def test_runner_reports_success_and_skip_counts(self):
        result = self._run("""
            class SyntheticTests(unittest.TestCase):
                def test_a_passes(self):
                    self.assertEqual(1, 1)

                @unittest.skip("synthetic skip")
                def test_b_skips(self):
                    pass
        """, timeout=2)
        self._assert_summary(result, tests=2, skipped=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
