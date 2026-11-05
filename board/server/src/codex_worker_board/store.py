"""Read task records without importing or mutating the worker plugin."""

import json
import math
import os
import stat
import threading
import time
from dataclasses import dataclass, field
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
    turn_finished provide known start times. For a missing start (including
    check, CI and fix), Store retains the time it first observes that phase in
    Record until the phase changes. A phase present on the server's initial
    poll keeps a null fallback: its start predates observation and is unknown.
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
    observed_phase: str | None = None
    phase_started_at: float | None = None


@dataclass
class SessionRecord:
    path: Path
    state: dict
    summary: dict = field(default_factory=dict)
    tail: EventTail | None = None
    transcript_search_at: float | None = None
    activity: dict | None = None


class Store:
    def __init__(self, tasks_path, stale_days=7, claude_desktop_dir=None, claude_home=None,
                 sessions_seconds=5, session_stale_hours=48):
        self.tasks_path = Path(tasks_path)
        self.stale_days = stale_days
        self.claude_desktop_dir = Path(claude_desktop_dir) if claude_desktop_dir is not None else None
        self.claude_home = Path(claude_home) if claude_home is not None else None
        self.sessions_seconds = sessions_seconds
        self.session_stale_hours = session_stale_hours
        self.session_records = {}
        self._session_signatures = {}
        self._session_paths = {}
        self._live_sessions = {}
        self._next_sessions_scan = 0.0
        self.records = {}
        self.lock = threading.RLock()
        self._initial_poll = True

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
                        archived.append((state.stat().st_mtime_ns, path.name, path))
                    except OSError:
                        pass
        paths.extend((path, True) for _, _, path in sorted(archived, reverse=True)[:50])
        return paths

    def _summary(self, slug, record):
        state = record.state
        now = time.time()
        is_alive = alive(state.get("pid"))
        result = state.get("result") or {}
        status = text(result.get("status"))
        outcome = text(state.get("outcome"))
        if record.archived or (not is_alive and (
                state.get("kind") == "scout"
                or record.updated_at < now - self.stale_days * 86400)):
            column = "closed"
        elif is_alive:
            column = "sleeping" if outcome == "sleeping" else "running"
        else:
            column = "review_me" if status == "done" and outcome not in FAILURES else "attention"
        current_phase, started_at = phase(state, is_alive)
        if record.observed_phase != current_phase:
            record.observed_phase = current_phase
            record.phase_started_at = None if self._initial_poll else now
        if started_at is None:
            started_at = record.phase_started_at
        executor = state.get("executor", "codex")
        summary = text(result.get("summary"))
        ci = state.get("ci") or {}
        return {
            "slug": slug,
            "kind": state.get("kind", "work"),
            "title": None,
            "link": None,
            "repo": Path(state.get("repo") or state.get("worktree") or "").name,
            "branch": text(state.get("branch")),
            "task_type": text(state.get("task_type")),
            "executor": executor,
            "account": text(state.get("claude_account") if executor == "claude" else state.get("account")),
            "reviewer": text(state.get("reviewer")),
            "model": text(state.get("task_model") or (state.get("model") if executor == "claude" else None)),
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
                        latest.append((activity["at"], activity))
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
        messages.extend(self._poll_sessions())
        self._initial_poll = False
        return messages

    @staticmethod
    def _safe_slug(value):
        return (isinstance(value, str) and bool(value) and not value.startswith(".")
                and Path(value).name == value and "\\" not in value and "\0" not in value)

    @staticmethod
    def _source_file(root, path):
        # Check every component inside the configured source, including its root.
        if not path.is_relative_to(root):
            return None
        current = path
        while True:
            if current.is_symlink():
                return None
            if current == root:
                break
            current = current.parent
        try:
            return path if path.is_file() else None
        except OSError:
            return None

    def _scan_sessions(self):
        root = self.claude_desktop_dir
        if root is None:
            return
        paths = set()
        if not root.is_symlink():
            for account in self._directories(root / "claude-code-sessions"):
                for directory in self._directories(account):
                    try:
                        candidates = directory.iterdir()
                        for path in candidates:
                            if not (path.name.startswith("local_") and path.suffix == ".json"):
                                continue
                            try:
                                metadata = path.lstat()
                                if not stat.S_ISREG(metadata.st_mode):
                                    continue
                                paths.add(path)
                                signature = (metadata.st_mtime_ns, metadata.st_size)
                                if signature == self._session_signatures.get(path):
                                    continue
                                self._session_signatures[path] = signature
                                raw = json.loads(path.read_text(encoding="utf-8"))
                                state = self._session_metadata(raw)
                            except (OSError, ValueError):
                                continue
                            if state is None:
                                continue
                            old_slug = self._session_paths.get(path)
                            slug = state["sessionId"]
                            if old_slug and (old_slug != slug or state.get("isArchived")):
                                self.session_records.pop(old_slug, None)
                                self._session_paths.pop(path, None)
                            if state.get("isArchived"):
                                continue
                            record = self.session_records.get(slug)
                            if record is None or record.path != path:
                                record = SessionRecord(path, state)
                                self.session_records[slug] = record
                            else:
                                if record.state.get("cliSessionId") != state.get("cliSessionId"):
                                    record.tail = None
                                    record.transcript_search_at = None
                                    record.activity = None
                                record.state = state
                            self._session_paths[path] = slug
                    except OSError:
                        continue
        for path in set(self._session_signatures) - paths:
            del self._session_signatures[path]
            slug = self._session_paths.pop(path, None)
            if slug:
                self.session_records.pop(slug, None)

    def _session_metadata(self, raw):
        if not isinstance(raw, dict):
            return None
        slug = raw.get("sessionId")
        if not self._safe_slug(slug) or not slug.startswith("local_"):
            return None
        fields = ("sessionId", "cliSessionId", "title", "cwd", "originCwd", "model", "effort",
                  "isArchived", "createdAt", "lastActivityAt", "completedTurns",
                  "interruptedAwaitingPerson")
        state = {key: raw[key] for key in fields if key in raw}
        if (number(state.get("createdAt")) is None
                or number(state.get("lastActivityAt")) is None
                or not isinstance(raw.get("prs", []), list)
                or type(state.get("isArchived", False)) is not bool
                or type(state.get("completedTurns", 0)) is not int):
            return None
        if any(state.get(key) is not None and not isinstance(state[key], str)
               for key in ("cliSessionId", "title", "cwd", "originCwd", "model", "effort")):
            return None
        if state.get("cliSessionId") is not None and not self._safe_slug(state["cliSessionId"]):
            return None
        if any("\0" in (state.get(key) or "") for key in ("cwd", "originCwd")):
            return None
        state["prs"] = [{key: pr[key] for key in ("url", "state", "branch", "own") if key in pr}
                        for pr in raw.get("prs", []) if isinstance(pr, dict)]
        return state

    def _read_live_sessions(self):
        root = self.claude_home
        if root is None or root.is_symlink():
            self._live_sessions.clear()
            return {}
        present = set()
        try:
            directory = root / "sessions"
            candidates = [] if directory.is_symlink() else directory.iterdir()
            for path in candidates:
                if not (path.suffix == ".json" and path.stem.isascii() and path.stem.isdecimal()):
                    continue
                if self._source_file(root, path) is None:
                    continue
                present.add(path)
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    cwd = raw.get("cwd") if isinstance(raw, dict) else None
                    if (isinstance(raw, dict) and type(raw.get("pid")) is int
                            and raw["pid"] == int(path.stem)
                            and self._safe_slug(raw.get("sessionId"))
                            and raw.get("status") in ("busy", "idle")
                            and number(raw.get("statusUpdatedAt")) is not None
                            and (cwd is None or (isinstance(cwd, str) and "\0" not in cwd))):
                        self._live_sessions[path] = {key: raw[key] for key in (
                            "pid", "sessionId", "cwd", "status", "statusUpdatedAt", "entrypoint")
                            if key in raw}
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        for path in set(self._live_sessions) - present:
            del self._live_sessions[path]
        sessions = {}
        for state in self._live_sessions.values():
            if not alive(state["pid"]):
                continue
            previous = sessions.get(state["sessionId"])
            rank = (state["status"] == "busy", state["statusUpdatedAt"])
            if previous is None or rank > (previous["status"] == "busy", previous["statusUpdatedAt"]):
                sessions[state["sessionId"]] = state
        return sessions

    def _session_activity(self, record, live):
        root = self.claude_home
        if root is None:
            return
        projects = root / "projects"
        if root.is_symlink() or projects.is_symlink():
            return
        if record.tail is not None and self._source_file(root, record.tail.path) is None:
            record.tail = None
        if record.tail is None:
            session_id = record.state.get("cliSessionId")
            if not self._safe_slug(session_id):
                return
            cwd = text(live.get("cwd")) or text(record.state.get("cwd")) or ""
            encoded = "".join(char if char.isascii() and char.isalnum() else "-" for char in cwd)
            path = self._source_file(root, projects / encoded / (session_id + ".jsonl"))
            now = time.monotonic()
            if path is None and (record.transcript_search_at is None
                                 or now - record.transcript_search_at >= 60):
                record.transcript_search_at = now
                for directory in self._directories(projects):
                    path = self._source_file(root, directory / (session_id + ".jsonl"))
                    if path is not None:
                        break
            if path is None:
                return
            record.tail = EventTail(path)
        if self._source_file(root, record.tail.path) is not None:
            activity = record.tail.read()
            if activity is not None:
                record.activity = activity

    def _session_summary(self, slug, record, live):
        state = record.state
        is_alive = live is not None and live.get("status") == "busy"
        updated_at = number(state.get("lastActivityAt")) / 1000
        prs = state.get("prs", [])
        own = [pr for pr in prs if pr.get("own") is True]
        pr = (own or prs or [{}])[-1]
        if is_alive:
            column = "running"
        elif state.get("interruptedAwaitingPerson") is not None:
            column = "attention"
        elif prs and all(pr.get("state") in ("MERGED", "CLOSED") for pr in prs):
            column = "closed"
        elif updated_at < time.time() - self.session_stale_hours * 3600:
            column = "closed"
        else:
            column = "review_me"
        cwd = text(state.get("originCwd")) or text(state.get("cwd")) or ""
        if cwd and Path(cwd).is_relative_to(self.claude_desktop_dir):
            repo = "(no folder)"
        else:
            repo = Path(cwd.split("/.claude/worktrees/", 1)[0]).name
        return {
            "slug": slug, "kind": "session", "title": text(state.get("title")),
            "link": "claude://claude.ai/epitaxy/" + slug,
            "repo": repo, "branch": text(own[-1].get("branch")) if own else None,
            "task_type": None, "executor": "claude", "account": None, "reviewer": None,
            "model": text(state.get("model")), "effort": text(state.get("effort")),
            "column": column, "phase": "turn" if is_alive else "idle",
            "phase_started_at": number(live.get("statusUpdatedAt")) / 1000 if is_alive else updated_at,
            "alive": is_alive, "turn": state.get("completedTurns", 0), "fix_round": 0,
            "outcome": None, "result_status": None, "summary": None,
            "pr": text(pr.get("url")), "ci": None, "sleeping_until": None,
            "created": number(state.get("createdAt")) / 1000,
            "updated_at": updated_at, "activity": record.activity if is_alive else None,
        }

    def _poll_sessions(self):
        if self.claude_desktop_dir is None:
            return []
        previous = {slug: record.summary for slug, record in self.session_records.items()}
        now = time.monotonic()
        if now >= self._next_sessions_scan:
            self._scan_sessions()
            self._next_sessions_scan = now + self.sessions_seconds
        live_sessions = self._read_live_sessions()
        messages = [{"type": "task_removed", "slug": slug}
                    for slug in previous if slug not in self.session_records]
        for slug, record in self.session_records.items():
            live = live_sessions.get(record.state.get("cliSessionId"))
            if live is not None and live["status"] == "busy":
                self._session_activity(record, live)
                if (record.activity is not None
                        and record.activity["at"] < number(live["statusUpdatedAt"]) / 1000):
                    record.activity = None
            else:
                record.activity = None
            summary = self._session_summary(slug, record, live)
            if summary != record.summary:
                old_activity = record.summary.get("activity")
                record.summary = summary
                messages.append({"type": "task", "task": summary})
                if summary["activity"] is not None and summary["activity"] != old_activity:
                    messages.append({"type": "activity", "slug": slug, "activity": summary["activity"]})
        return messages

    def summaries(self):
        with self.lock:
            rows = [record.summary.copy() for _, record in sorted(self.records.items())]
            rows.extend(record.summary.copy() for _, record in sorted(self.session_records.items()))
            closed = sorted((row for row in rows if row["column"] == "closed"),
                            key=lambda row: (row["updated_at"], row["slug"]), reverse=True)[:50]
            return [row for row in rows if row["column"] != "closed"] + closed

    def detail(self, slug):
        with self.lock:
            if not self._safe_slug(slug):
                return None
            session = self.session_records.get(slug)
            if session is not None:
                entries = [{"at": session.summary[key], "text": label}
                           for key, label in (("created", "Session created"),
                                              ("updated_at", "Last activity"))
                           if session.summary[key] is not None]
                return dict(session.summary, timeline=sorted(entries, key=lambda item: item["at"]),
                            files=[])
            record = self._record(slug)
            if record is None:
                return None
            state = dict(record.state, _updated_at=record.updated_at)
            return dict(record.summary, timeline=timeline(state),
                        files=[name for name, filename in FILES.items()
                               if self._file(record.path, filename) is not None])

    def file_path(self, slug, name):
        with self.lock:
            if (name not in FILES or self.tasks_path.is_symlink()
                    or not self._safe_slug(slug) or slug in self.session_records):
                return None
            record = self._record(slug)
            if record is None:
                return None
            if record.archived and record.path.parent.is_symlink():
                return None
            return self._file(record.path, FILES[name])
