"""Rate-limited account snapshots from the plugin's read-only accounts command."""

import asyncio
import json
import time
from pathlib import Path

ACCOUNTS_TIMEOUT = 60


class Accounts:
    def __init__(self, script: Path, seconds: float):
        self.script = script
        self.seconds = seconds
        self.value = {"fetched_at": None, "error": None, "data": None}
        self._last_started = float("-inf")
        self._lock = asyncio.Lock()

    async def refresh(self):
        async with self._lock:
            if time.monotonic() - self._last_started < self.seconds:
                return None
            self._last_started = time.monotonic()
            proc = None
            data = self.value["data"]
            error = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-I", str(self.script), "accounts", "--json",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), ACCOUNTS_TIMEOUT)
                if proc.returncode:
                    raise ValueError(stderr.decode("utf-8", "replace").strip()
                                     or f"accounts exited with status {proc.returncode}")
                data = json.loads(stdout)
            except asyncio.TimeoutError:
                error = f"accounts timed out after {ACCOUNTS_TIMEOUT:g} seconds"
            except (OSError, ValueError) as exc:
                error = str(exc)
            finally:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.communicate()
            self.value = {"fetched_at": time.time(), "error": error, "data": data}
            return self.value
