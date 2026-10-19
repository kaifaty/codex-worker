"""Exercise real and shared file clocks without starting the Codex CLI."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_loader import WORKER_BOOTSTRAP, load_worker


SCRIPT = (Path(__file__).resolve().parents[1]
          / "plugins/codex-worker/skills/codex-worker/scripts/codex_task.py")
CHILD = WORKER_BOOTSTRAP + """
import json
import sys

worker = load_worker(name="child_clock_worker", script=sys.argv[1])
if len(sys.argv) > 2:
    print("ready", flush=True)
    if sys.stdin.readline() != "go\\n":
        sys.exit("missing start signal")
    for _ in range(int(sys.argv[2])):
        worker.CLOCK.sleep(float(sys.argv[3]))
print(json.dumps({"wall": worker.CLOCK.time(),
                  "monotonic": worker.CLOCK.monotonic()}))
"""


class ClockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="codex-clock-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.clock_file = self.home / "clock.json"
        self.initial = {"wall": 1_800_000_000.0, "monotonic": 1000.0}
        self.clock_file.write_text(json.dumps(self.initial), encoding="utf-8")
        environment = dict(os.environ, CODEX_TASK_HOME=str(self.home))
        environment.pop("CODEX_TASK_CLOCK_FILE", None)
        self.worker = load_worker(environment, name="clock_worker", clear=True)

    def test_real_clock_uses_module_time(self):
        clock = self.worker.Clock()
        for name in ("time", "monotonic"):
            with self.subTest(method=name):
                before = getattr(time, name)()
                value = getattr(clock, name)()
                after = getattr(time, name)()
                self.assertLessEqual(before, value)
                self.assertLessEqual(value, after)
        before = time.monotonic()
        clock.sleep(0.001)
        self.assertGreaterEqual(time.monotonic() - before, 0.001)

    def test_file_sleep_advances_both_clocks(self):
        clock = self.worker.Clock(self.clock_file)
        self.assertEqual(clock.time(), self.initial["wall"])
        self.assertEqual(clock.monotonic(), self.initial["monotonic"])
        clock.sleep(7.25)
        expected = {name: value + 7.25 for name, value in self.initial.items()}
        self.assertEqual(clock.time(), expected["wall"])
        self.assertEqual(clock.monotonic(), expected["monotonic"])
        self.assertEqual(json.loads(self.clock_file.read_text(encoding="utf-8")), expected)

    def child_environment(self):
        return dict(os.environ, CODEX_TASK_HOME=str(self.home),
                    CODEX_TASK_CLOCK_FILE=str(self.clock_file))

    def test_environment_clock_is_shared_by_two_processes(self):
        clock = self.worker.Clock(self.clock_file)
        for advance in (0, 3.5):
            with self.subTest(advance=advance):
                clock.sleep(advance)
                process = subprocess.run(
                    [sys.executable, "-I", "-c", CHILD, str(SCRIPT)],
                    env=self.child_environment(), capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(json.loads(process.stdout), {
                    "wall": clock.time(), "monotonic": clock.monotonic()})

    def stop_child(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def test_parallel_file_sleeps_preserve_every_advance(self):
        count, repeats, seconds = 4, 20, 0.25
        processes = []
        for _ in range(count):
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", CHILD, str(SCRIPT),
                 str(repeats), str(seconds)],
                env=self.child_environment(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.stop_child, process)
            processes.append(process)
        with selectors.DefaultSelector() as selector:
            for process in processes:
                selector.register(process.stdout, selectors.EVENT_READ, process)
            while selector.get_map():
                ready = selector.select(timeout=30)
                self.assertTrue(ready, "clock process did not become ready")
                for key, _ in ready:
                    self.assertEqual(key.fileobj.readline(), "ready\n")
                    selector.unregister(key.fileobj)
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            observed = json.loads(stdout)
            self.assertGreaterEqual(observed["wall"], self.initial["wall"] + repeats * seconds)
            self.assertGreaterEqual(observed["monotonic"],
                                    self.initial["monotonic"] + repeats * seconds)
        clock = self.worker.Clock(self.clock_file)
        total = count * repeats * seconds
        self.assertEqual(clock.time(), self.initial["wall"] + total)
        self.assertEqual(clock.monotonic(), self.initial["monotonic"] + total)


if __name__ == "__main__":
    unittest.main()
