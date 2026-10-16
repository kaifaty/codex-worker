#!/usr/bin/env python3
"""Block until the next Codex turn of one thread finishes, then print its record and exit.

Reads ~/.codex/turn-events.jsonl, written by turn-log.py from Codex's `notify`.
Usage: wait-turn.py <thread-id> [--follow]
  default   exit after the first new turn of that thread (for a background Bash command);
  --follow  print every new turn of that thread and never exit (for a Monitor).
"""
import json
import sys
import time
from pathlib import Path

LOG = Path.home() / ".codex" / "turn-events.jsonl"

if len(sys.argv) < 2:
    sys.exit(__doc__)
thread, follow = sys.argv[1], "--follow" in sys.argv[2:]

pos = LOG.stat().st_size if LOG.exists() else 0
while True:
    time.sleep(2)
    if not LOG.exists():
        continue
    size = LOG.stat().st_size
    if size < pos:
        pos = 0
    if size == pos:
        continue
    with LOG.open(encoding="utf-8") as f:
        f.seek(pos)
        chunk = f.read()
        pos = f.tell()
    for line in chunk.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("thread") != thread:
            continue
        print(f"[codex turn done {rec.get('ts')}] {rec.get('last', '')}", flush=True)
        if not follow:
            sys.exit(0)
