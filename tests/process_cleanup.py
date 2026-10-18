"""Find and stop processes owned by an isolated test directory."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def _mentions_root(command, root_paths):
    # ps flattens argv and the environment into one string. Match complete
    # directory components so worker /1 cannot claim worker /10 or /1-other.
    # Keep literal spaces inside a root: neither argv nor ps escapes them.
    before_path = " \t\r\n=:'\""
    after_path = "/ \t\r\n:'\""
    for path in root_paths:
        start = command.find(path)
        while start != -1:
            end = start + len(path)
            if ((start == 0 or command[start - 1] in before_path)
                    and (end == len(command) or command[end] in after_path)):
                return True
            start = command.find(path, start + 1)
    return False


def _fixture_processes(root, owned, groups):
    # Published pids still identify children after a supervisor exits or clears
    # its final state. Traces also retain process groups of orphaned tools.
    for path in root.rglob("state.json"):
        try:
            state = json.loads(path.read_text())
            owned.update(int(state[key]) for key in ("pid", "codex_pid", "check_pid")
                         if state.get(key))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    for path in root.rglob("*trace*.jsonl"):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
                owned.update(int(event[key]) for key in ("pid", "ppid", "child_pid")
                             if event.get(key))
                if event.get("pgid"):
                    groups.add(int(event["pgid"]))
            except (ValueError, TypeError, AttributeError):
                pass  # A killed writer can leave a partial final record.


def _owned_processes(root, owned, groups):
    _fixture_processes(root, owned, groups)
    # The environment identifies detached children before they publish a pid,
    # including tools whose command line contains no fixture path. macOS may
    # truncate a command in the middle of a UTF-8 character.
    snapshot = subprocess.run(
        ["ps", "axeww", "-o", "pid=,ppid=,pgid=,stat=,command="],
        capture_output=True, text=True, errors="replace", check=True)
    table = {}
    protected = {0, 1, os.getpid()}
    root_paths = {str(root), str(root.resolve())}
    for line in snapshot.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5:
            continue
        try:
            pid, parent, group = map(int, fields[:3])
        except ValueError:
            continue
        table[pid] = (parent, group, fields[3])
        if pid not in protected and _mentions_root(fields[4], root_paths):
            owned.add(pid)
    owned.difference_update(protected)
    groups.difference_update((0, 1, os.getpgrp()))
    while True:
        descendants = {pid for pid, (parent, group, _) in table.items()
                       if pid not in protected and (parent in owned or group in groups)}
        if descendants <= owned:
            break
        owned.update(descendants)
    groups.update(group for pid, (_, group, _) in table.items()
                  if pid in owned and group == pid)
    groups.difference_update((0, 1, os.getpgrp()))
    live = set()
    for pid in owned:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            pass
        if pid in table and not table[pid][2].startswith("Z"):
            live.add(pid)
    return live


def cleanup_processes(root, extra_pids=(), process_groups=()):
    """Escalate SIGTERM to SIGKILL and return any surviving live process ids.

    The caller decides whether survivors are a test assertion or runner error.
    Ownership is retained between snapshots so reparenting cannot hide a child.
    Known process groups also identify children after their group leader exits,
    even when the children remove the test directory from their environment.
    """
    # Keep macOS's /var spelling as well as /private/var: either can occur in
    # subprocess argv or inherited environment variables.
    root = Path(root).absolute()
    owned, groups = set(extra_pids), set(process_groups)
    live = set()
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        signalled, signalled_groups = set(), set()
        deadline = time.monotonic() + grace
        while True:
            live = _owned_processes(root, owned, groups)
            if not live:
                return live
            for group in groups - signalled_groups:
                try:
                    os.killpg(group, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                signalled_groups.add(group)
            for pid in live - signalled:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                signalled.add(pid)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    return live
