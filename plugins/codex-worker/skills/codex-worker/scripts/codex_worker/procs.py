"""Process execution, signaling and bounded termination."""
import os
import signal
import subprocess
import sys
import time


def die(msg):
    sys.exit(f"codex_task: {msg}")


def sh(cmd, cwd=None, check=True, errors="strict"):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, errors=errors)
    if check and r.returncode != 0:
        die(f"{' '.join(cmd)} failed:\n{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.rstrip()


def signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS may reject killpg even while the leader is still alive.
        if proc.poll() is not None:
            return False
        if sig:
            try:
                os.kill(proc.pid, sig)
            except ProcessLookupError:
                return False
    return True


def terminate(proc):
    """Interrupt the entire exec group, then bound shutdown even if it ignores signals."""

    for sig, timeout in ((signal.SIGINT, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if not signal_group(proc, sig):
            proc.wait()
            break
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc.poll()  # Reap the leader, but also wait for its tool processes.
            if not signal_group(proc, 0):
                return
            time.sleep(0.05)


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
