"""Logical time shared by the supervisor and test processes."""
import fcntl
import json
import math
import signal
import time
from pathlib import Path

from . import config


class Clock:
    """Logical time, optionally shared by test processes through a locked JSON file.

    OS/process deadlines keep using the time module. File-clock sleeps advance
    both values under one lock, then briefly yield so other processes can run.
    A file with auto_advance=false leaves advancement to the test driver.
    The file is updated in place to preserve its flock identity.
    """

    def __init__(self, clock_file=None):
        self.path = Path(clock_file) if clock_file is not None else None

    def _read(self, name):
        with self.path.open(encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            return json.load(stream)[name]

    def time(self):
        return time.time() if self.path is None else self._read("wall")

    def monotonic(self):
        return time.monotonic() if self.path is None else self._read("monotonic")

    def sleep(self, seconds):
        if self.path is None:
            return time.sleep(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("sleep length must be finite and non-negative")
        # Supervisor signal handlers read CLOCK too. Deliver them after closing
        # the exclusive lock so a handler cannot wait on its interrupted writer.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            with self.path.open("r+", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                value = json.load(stream)
                auto_advance = value.get("auto_advance", True)
                if auto_advance:
                    value["wall"] += seconds
                    value["monotonic"] += seconds
                    stream.seek(0)
                    json.dump(value, stream)
                    stream.truncate()
                    stream.flush()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        time.sleep(0.001 if auto_advance else min(seconds, 0.05))


CLOCK = Clock(config.env("CODEX_TASK_CLOCK_FILE"))
