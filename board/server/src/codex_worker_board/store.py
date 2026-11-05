"""Read task records without importing or mutating the worker plugin."""

import json
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .activity import EventTail


FILES = {"brief": "brief.md", "last": "last.md", "review": "review.md", "check": "check.log"}
FAILURES = {"failed", "stopped", "interrupted", "budget_exhausted", "conflict", "stuck",
            "refused", "unavailable", "exhausted", "running", "sleeping"}


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def text(value):
    return value if isinstance(value, str) and value else None


def one_line(value):
    return " ".join(str(value).split())[:200]


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def phase(state, is_alive):
    """Derive the phase from the cursors the plugin persists, in priority order.

    A dead supervisor is idle. A live supervisor with outcome sleeping is
    sleeping; check_pid means the saved check is executing; ci.outcome pending
    means wait_ci is polling. kind review covers Codex and Claude review,
    including account selection between attempts. For work, nonempty rounds
    records an automatic review fix; a failed ci with rounds > 0 records a CI
    repair. These are fix while work is running (or an executor pid is set).
    Otherwise running work/scout is turn; completed work between phases is idle.

    turn_started, unfinished review.started, the latest sleep rotation.at and
    turn_finished provide known start times. The plugin persists no check, CI
    or fix start timestamp, so those phase_started_at values are null.
    """
    if not is_alive:
        return "idle", number(state.get("turn_finished"))
    if state.get("outcome") == "sleeping":
        sleeps = [item for item in state.get("rotations", []) if item.get("event") == "sleep"]
        return "sleeping", number(sleeps[-1].get("at")) if sleeps else None
    if state.get("check_pid"):
        return "check", None
    ci = state.get("ci") or {}
    if ci.get("outcome") == "pending":
        return "ci", None
    if state.get("kind") == "review":
        reviews = state.get("reviews", [])[state.get("review_start", 0):]
        latest = reviews[-1] if reviews else {}
        started = number(latest.get("started")) if latest.get("finished") is None else None
        return "review", started
    if state.get("outcome") == "running" or state.get("codex_pid") or state.get("claude_pid"):
        if state.get("rounds") or (ci.get("outcome") == "failed" and ci.get("rounds", 0) > 0):
            return "fix", None
        return "turn", number(state.get("turn_started"))
    return "idle", number(state.get("turn_finished"))


def timeline(state):
    entries = []

    def add(at, message):
        at = number(at)
        if at is not None:
            entries.append({"at": at, "text": one_line(message)})

    add(state.get("created"), "Task created")
    add(state.get("turn_started"), f"Turn {state.get('turns', 0)} started")
    add(state.get("turn_finished"), f"Turn {state.get('turns', 0)} {state.get('outcome', 'finished')}")
    for item in state.get("threads", []):
        add(item.get("started"), f"Turn on {item.get('account', '?')}")
    for item in state.get("checks", []):
        add(item.get("at"), f"Check {item.get('outcome', '?')}: {item.get('cmd', '')}")
    for item in state.get("reviews", []):
        label = f"Review {item.get('angle', 'correctness')} by {item.get('reviewer', 'codex')}/{item.get('account', '?')}"
        add(item.get("started"), label + " started")
        add(item.get("finished"), label + " " + str(item.get("outcome") or "finished"))
    for item in state.get("rotations", []):
        event = item.get("event")
        label = (f"{event}: {item.get('account') or ''} {item.get('reason') or ''}" if event else
                 f"Rotated {item.get('from', '?')} to {item.get('to', '?')}: {item.get('reason', '')}")
        add(item.get("at"), label)
    ci = state.get("ci") or {}
    if ci:
        # CI has no own wall-clock timestamp; state mtime is its durable observation.
        add(state.get("_updated_at"), f"CI {ci.get('outcome', '?')}: {ci.get('reason', '')}")
    return sorted(entries, key=lambda item: item["at"])[-200:]


@dataclass
class Record:
    path: Path
    archived: bool
    state: dict = field(default_factory=dict)
    signature: tuple | None = None
    updated_at: float = 0
    summary: dict = field(default_factory=dict)
    tails: dict = field(default_factory=dict)
    activity: dict | None = None


class Store:
    def __init__(self, tasks_path):
        self.tasks_path = Path(tasks_path)
        self.records = {}
        self.lock = threading.RLock()

    @staticmethod
    def _file(directory, name):
        path = directory / name
        if directory.is_symlink() or path.is_symlink():
            return None
        try:
            return path if path.is_file() else None
        except OSError:
            return None

    @staticmethod
    def _directories(root):
        if root.is_symlink():
            return []
        try:
            return [path for path in root.iterdir()
                    if not path.name.startswith(".") and not path.is_symlink() and path.is_dir()]
        except OSError:
            return []

    def _paths(self):
        paths = [(path, False) for path in self._directories(self.tasks_path)]
        archived = []
        if not self.tasks_path.is_symlink():
            for path in self._directories(self.tasks_path / ".archive"):
                state = self._file(path, "state.json")
                if state is not None:
                    try:
                        stamp = path.name.rsplit("-", 1)[-1]
                        try:
                            archived_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                        except ValueError:
                            archived_at = state.stat().st_mtime
                        archived.append((archived_at, path.name, path))
                    except OSError:
                        pass
        paths.extend((path, True) for _, _, path in sorted(archived, reverse=True)[:50])
        return paths

    @staticmethod
    def _summary(slug, record):
        state = record.state
        is_alive = alive(state.get("pid"))
        result = state.get("result") or {}
        status = text(result.get("status"))
        outcome = text(state.get("outcome"))
        if record.archived:
            column = "closed"
        elif is_alive:
            column = "sleeping" if outcome == "sleeping" else "running"
        else:
            column = "review_me" if status == "done" and outcome not in FAILURES else "attention"
        current_phase, started_at = phase(state, is_alive)
        executor = state.get("executor", "codex")
        summary = text(result.get("summary"))
        ci = state.get("ci") or {}
        return {
            "slug": slug,
            "kind": state.get("kind", "work"),
            "repo": Path(state.get("repo") or state.get("worktree") or "").name,
            "branch": text(state.get("branch")),
            "task_type": text(state.get("task_type")),
            "executor": executor,
            "account": text(state.get("claude_account") if executor == "claude" else state.get("account")),
            "reviewer": text(state.get("reviewer")),
            "model": text(state.get("model") or state.get("review_model" if current_phase == "review" else "task_model")),
            "effort": text(state.get("reasoning_effort") or state.get("effort")),
            "column": column,
            "phase": current_phase,
            "phase_started_at": started_at,
            "alive": is_alive,
            "turn": state.get("turns", 0),
            "fix_round": len(state.get("rounds", [])) + ci.get("rounds", 0),
            "outcome": outcome,
            "result_status": status,
            "summary": one_line(summary.splitlines()[0]) if summary else None,
            "pr": text((state.get("publish") or {}).get("pr") or result.get("pr")),
            "ci": text(ci.get("outcome")),
            "sleeping_until": number((state.get("sleeping") or {}).get("until")),
            "created": number(state.get("created")),
            "updated_at": record.updated_at,
            "activity": record.activity if is_alive else None,
        }

    def poll(self):
        with self.lock:
            return self._poll()

    def _read_state(self, record):
        state_path = self._file(record.path, "state.json")
        if state_path is not None:
            try:
                stat = state_path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature != record.signature:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(state, dict):
                        record.state = state
                        record.signature = signature
                        record.updated_at = stat.st_mtime
            except (OSError, ValueError):
                pass

    def _record(self, slug):
        record = self.records.get(slug)
        if record is not None:
            return record
        if not slug or slug.startswith(".") or Path(slug).name != slug:
            return None
        archive = self.tasks_path / ".archive"
        if self.tasks_path.is_symlink() or archive.is_symlink():
            return None
        # The 50-record limit applies to snapshots, not archive REST addresses.
        record = Record(archive / slug, archived=True)
        self._read_state(record)
        if record.signature is None:
            return None
        record.summary = self._summary(slug, record)
        return record

    def _poll(self):
        messages = []
        paths = self._paths()
        present = {path.name for path, _ in paths}
        for slug in list(self.records):
            if slug not in present:
                record = self.records.pop(slug)
                if not record.archived:
                    messages.append({"type": "task_removed", "slug": slug})
        for path, archived in paths:
            slug = path.name
            previous = self.records.get(slug)
            record = previous if previous and previous.path == path else Record(path, archived)
            self._read_state(record)
            if record.signature is None:
                continue
            record.archived = archived
            latest = []
            if not archived and alive(record.state.get("pid")):
                for filename in ("events.jsonl", "claude-events.jsonl"):
                    event_path = self._file(path, filename)
                    if event_path is None:
                        continue
                    if filename not in record.tails:
                        record.tails[filename] = EventTail(event_path)
                    tail = record.tails[filename]
                    activity = tail.read()
                    if activity is not None:
                        latest.append((activity.get("at") or tail.signature[2] / 1_000_000_000, activity))
            if latest:
                record.activity = max(latest, key=lambda item: item[0])[1]
            summary = self._summary(slug, record)
            if summary != record.summary:
                old_activity = record.summary.get("activity")
                record.summary = summary
                messages.append({"type": "task", "task": summary})
                if summary["activity"] is not None and summary["activity"] != old_activity:
                    messages.append({"type": "activity", "slug": slug, "activity": summary["activity"]})
            self.records[slug] = record
        return messages

    def summaries(self):
        with self.lock:
            return [record.summary.copy() for _, record in sorted(self.records.items())]

    def detail(self, slug):
        with self.lock:
            record = self._record(slug)
            if record is None:
                return None
            state = dict(record.state, _updated_at=record.updated_at)
            return dict(record.summary, timeline=timeline(state),
                        files=[name for name, filename in FILES.items()
                               if self._file(record.path, filename) is not None])

    def file_path(self, slug, name):
        with self.lock:
            if name not in FILES or self.tasks_path.is_symlink():
                return None
            record = self._record(slug)
            if record is None:
                return None
            if record.archived and record.path.parent.is_symlink():
                return None
            return self._file(record.path, FILES[name])
