"""Opt-in logical clocks without replacing the rotation tests' real processes."""
import builtins
import fcntl
import importlib.util
import json
import os
import subprocess
import threading
import time
import types
from pathlib import Path


class FrozenClock:
    """A parent-controlled clock shared by the CLI, supervisor and quota fake."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        return json.loads(self.path.read_text())

    def time(self):
        return self._read()["wall"]

    def monotonic(self):
        return self._read()["monotonic"]

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("the test clock cannot go backwards")
        # Telemetry retries may advance the clock in the supervisor while the
        # test advances an exec or a reset. Keep the snapshot atomic for readers.
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self._read()
            value["wall"] += seconds
            value["monotonic"] += seconds
            pending = self.path.with_name("%s.%s.%s" % (
                self.path.name, os.getpid(), threading.get_ident()))
            pending.write_text(json.dumps(value))
            os.replace(str(pending), str(self.path))

    def sleep(self, seconds):
        # Process readiness, shutdown and RPC polling continue to yield to real
        # processes. Only the explicit logical waits below advance the clock.
        time.sleep(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class RetryClock(FrozenClock):
    """The telemetry lock's bounded retry needs no real elapsed time."""

    def sleep(self, seconds):
        self.advance(seconds)


class SupervisorProcesses:
    """Redirect self-launches, leaving every other subprocess call unchanged."""

    def __init__(self, script, launcher):
        self.script, self.launcher = str(script), str(launcher)

    def Popen(self, args, *positional, **kwargs):
        if len(args) >= 4 and str(args[2]) == self.script and args[3] == "_run":
            args = list(args)
            args[2] = self.launcher
        return subprocess.Popen(args, *positional, **kwargs)

    def __getattr__(self, name):
        return getattr(subprocess, name)


def function_clock(function, clock):
    """Keep a function's exact body and change only its local time dependency."""
    namespace = dict(function.__globals__, time=clock)
    return types.FunctionType(function.__code__, namespace, function.__name__,
                              function.__defaults__, function.__closure__)


def run_worker(script, launcher, clock_path):
    spec = importlib.util.spec_from_file_location("rotation_clock_worker", script)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    # Preserve __file__: skill assets and report imports resolve beside the real
    # worker. The forwarding proxy changes only its supervisor executable path.
    worker.time = FrozenClock(clock_path)
    worker.subprocess = SupervisorProcesses(script, launcher)
    worker.terminate = function_clock(worker.terminate, time)
    worker.cmd_wait = function_clock(worker.cmd_wait, time)
    worker.write_telemetry = function_clock(worker.write_telemetry, RetryClock(clock_path))
    original_wait = worker.wait_for_account

    def wait_for_account(st, infos, cfg, reason, stopped, sleep=None):
        def ready_sleep(seconds):
            # sleeping is saved before telemetry captures its sleep clock. This
            # callback runs after that capture, so a jump excludes the entire
            # reset wait from active time even under process scheduling delays.
            marker = Path(clock_path).parent / ("clock-reset-%s-%s" % (
                st["slug"], len(st["rotations"])))
            marker.touch()
            (sleep or worker.time.sleep)(seconds)

        return original_wait(st, infos, cfg, reason, stopped, ready_sleep)

    worker.wait_for_account = wait_for_account
    worker.main()


def run_fake(source, clock_path):
    """Keep the fake CLI's actual processes, signals and events; gate its delays."""
    namespace = {"__name__": "__main__", "__file__": str(source)}
    clock = FrozenClock(clock_path)
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        return clock if name == "time" else original_import(name, *args, **kwargs)

    def clock_delay(run, key, default, thread, stopped):
        seconds = run.get(key, default)
        if seconds <= 0:
            return
        gate = Path(clock_path).parent / ("clock-delay-%s-%s" % (os.getpid(), key))
        namespace["trace"]({"command": "clock-delay", "thread": thread, "seconds": seconds,
                            "key": key, "gate": str(gate)})
        while not gate.exists() and not stopped[0]:
            time.sleep(0.01)

    namespace["__builtins__"] = dict(vars(builtins), __import__=import_module)
    namespace["clock_delay"] = clock_delay
    source_text = Path(source).read_text()
    replacements = (
        ('time.sleep(run.get("limit_after", 0.15))',
         'clock_delay(run, "limit_after", 0.15, thread, stopped)'),
        ('    deadline = time.monotonic() + run.get("delay", 0)\n'
         '    while not stopped[0] and time.monotonic() < deadline:\n'
         '        time.sleep(0.02)\n',
         '    clock_delay(run, "delay", 0, thread, stopped)\n'))
    for before, after in replacements:
        if source_text.count(before) != 1:
            raise RuntimeError("the fake CLI delay changed; update its logical-clock replacement")
        source_text = source_text.replace(before, after, 1)
    exec(compile(source_text, str(source), "exec"), namespace)


class ClockFixture:
    @property
    def script(self):
        from rotation_support import SCRIPT
        return getattr(self, "_clock_script", SCRIPT)

    def enable_fake_time(self):
        from rotation_support import PYTHON, SCRIPT
        self.clock = FrozenClock(self.root / "clock.json")
        self.clock.path.write_text(json.dumps({"wall": time.time(), "monotonic": 0.0}))
        helper = str(Path(__file__).resolve())

        def loader(call):
            return ("#!" + PYTHON + "\nimport importlib.util\n"
                    "spec = importlib.util.spec_from_file_location('rotation_clock', %r)\n"
                    "helper = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(helper)\n" % helper) + call + "\n"

        self._clock_script = self.root / "worker-clock.py"
        self._clock_script.write_text(loader("helper.run_worker(%r, %r, %r)" % (
            str(SCRIPT), str(self._clock_script), str(self.clock.path))))
        source = self.root / "fake-codex-clock-source.py"
        source.write_text(self.fake.read_text())
        self.fake.write_text(loader("helper.run_fake(%r, %r)" % (str(source), str(self.clock.path))))

    def advance_clock(self, seconds):
        self.clock.advance(seconds)

    def wait_clock_exec(self, slug, number):
        def ready():
            executions = self.invocations()
            if len(executions) < number:
                return False
            execution = executions[number - 1]
            state = self.state(slug)
            if state.get("codex_pid") != execution["pid"]:
                return False
            # A resumed exec reuses the saved thread. Require its new event,
            # rather than mistaking the previous exec's thread for readiness.
            event_path = self.home / "claude-tasks" / slug / "events.jsonl"
            try:
                events = [json.loads(line) for line in event_path.read_text().splitlines()]
            except (OSError, ValueError):
                return False
            if not any(event.get("type") == "thread.started"
                       and event.get("thread_id") == execution["thread"]
                       for event in events[state["exec_event_start"]:]):
                return False
            if execution["review"]:
                return any(row.get("thread") == execution["thread"] for row in state.get("reviews", []))
            return state.get("thread") == execution["thread"]

        self.until(ready)
        return self.invocations()[number - 1]

    def finish_clock_delay(self, slug, number, key="delay"):
        execution = self.wait_clock_exec(slug, number)

        def delay():
            return next((event for event in self.trace()
                         if event.get("command") == "clock-delay" and event.get("key") == key
                         and event.get("pid") == execution["pid"]), None)

        self.until(lambda: delay() is not None)
        event = delay()
        self.advance_clock(event["seconds"])
        Path(event["gate"]).touch()

    def advance_clock_to_reset(self, slug):
        def ready():
            state = self.state(slug)
            marker = self.root / ("clock-reset-%s-%s" % (slug, len(state["rotations"])))
            return state.get("sleeping") and marker.exists()

        self.until(ready)
        until = self.state(slug)["sleeping"]["until"]
        self.advance_clock(max(0, until - self.clock.time()))
