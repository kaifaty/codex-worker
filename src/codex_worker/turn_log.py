#!/usr/bin/env python3
"""Codex `notify` hook: keeps Codex Computer Use's turn-ended call and logs each finished turn.

Codex appends the event JSON as the last argument. Every finished turn becomes one line in
~/.codex/turn-events.jsonl, which a Claude Code session can follow with `tail -F` under Monitor.
To undo: restore the previous `notify` line in ~/.codex/config.toml
(or remove the line if no previous hook was configured).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

COMPUTER_USE = (Path.home() / ".codex" / "computer-use" / "Codex Computer Use.app" /
                "Contents" / "SharedSupport" / "SkyComputerUseClient.app" / "Contents" /
                "MacOS" / "SkyComputerUseClient")
LOG = Path.home() / ".codex" / "turn-events.jsonl"

payload = sys.argv[-1] if len(sys.argv) > 1 else ""


def called_by_computer_use():
    """Computer Use rewrites `notify` to run itself first and chain here via --previous-notify."""
    try:
        parent = subprocess.run(["/bin/ps", "-p", str(os.getppid()), "-o", "command="],
                                capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return False
    return "SkyComputerUseClient" in parent


# When Codex calls this hook directly, keep Computer Use's turn-ended call it used to get.
try:
    if COMPUTER_USE.is_file() and not called_by_computer_use():
        subprocess.Popen([str(COMPUTER_USE), "turn-ended", payload], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
except Exception:
    pass

try:
    ev = json.loads(payload)
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": ev.get("type"),
        "client": ev.get("client"),
        "thread": ev.get("thread-id"),
        "turn": ev.get("turn-id"),
        "cwd": ev.get("cwd"),
        "last": (ev.get("last-assistant-message") or "")[:2000],
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
except Exception:
    pass
