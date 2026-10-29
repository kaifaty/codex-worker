"""Task state fields, persistence and cancellation outcomes."""
import fcntl
import json
import os
from contextlib import contextmanager
from typing import Callable, Literal, NamedTuple, Optional

from . import clock, config, procs, threads


def interruption_message(st):
    received = st.get("signal") or {}
    return (f"interrupted externally by {received.get('name', 'unknown signal')} "
            f"at {config.local_time(received.get('at'))}; "
            "the worktree is kept — check git status and continue with say")


def state_path(slug):
    return config.TASKS / slug / "state.json"


STATE_DELETE = object()


class StateField(NamedTuple):
    """Root field lifetime and optional factories for load/spawn values.

    task lasts until clean; history accumulates; cursor tracks the current
    account/thread/phase; turn spans a user turn; attempt spans one exec;
    runtime holds process and stop bookkeeping. Lifetimes document ownership;
    only spawn_reset opts a field into the unconditional spawn reset.
    """
    lifetime: Literal["task", "history", "cursor", "turn", "attempt", "runtime"]
    load_default: Optional[Callable[[dict], object]] = None
    spawn_reset: object = None  # Factory (state, turn context), STATE_DELETE, or no reset.


# Keep the historical insertion order for both load defaults and spawn values:
# state.json remains a flat dict, including identical serialization bytes.
# Factories allocate fresh mutable defaults for each state/turn.
STATE_FIELDS = {
    "account": StateField("cursor", load_default=lambda st: "main"),
    "threads": StateField("history", load_default=lambda st: (
        [{"account": st["account"], "thread": st["thread"], "started": st.get("created", 0)}]
        if st.get("thread") else [])),
    "rotations": StateField("history", load_default=lambda st: []),
    "reviews": StateField("history", load_default=lambda st: []),
    "checks": StateField("history", load_default=lambda st: []),
    # Last work result survives standalone review; only work/scout spawn clears it.
    "result": StateField("turn", load_default=lambda st: None),
    "pending": StateField("turn", spawn_reset=lambda st, turn: turn["pending"]),
    # Phase cursor, also used as the persistent scout marker.
    "kind": StateField("cursor", spawn_reset=lambda st, turn: turn["kind"]),
    "work_completed": StateField("turn", spawn_reset=lambda st, turn: False),
    "review_start": StateField("turn", spawn_reset=lambda st, turn: len(st.get("reviews", []))),
    "turn_started": StateField("turn", spawn_reset=lambda st, turn: clock.CLOCK.time()),
    "check_start": StateField("turn", spawn_reset=lambda st, turn: len(st.get("checks", []))),
    "turns": StateField("history", spawn_reset=lambda st, turn: st.get("turns", 0) + 1),
    "rotation_start": StateField("turn", spawn_reset=lambda st, turn: len(st["rotations"])),
    "event_start": StateField("turn", spawn_reset=lambda st, turn: len(threads.events(st))),
    "exhausted": StateField("turn", spawn_reset=lambda st, turn: None),
    "outcome": StateField("turn", spawn_reset=lambda st, turn: "running"),
    # Waiting budget is also restarted at automatic phase boundaries.
    "reset_wait_seconds": StateField("turn", spawn_reset=lambda st, turn: 0),
    "capacity_retries": StateField("turn", spawn_reset=lambda st, turn: 0),
    "fix_rounds": StateField("turn", load_default=lambda st: 0,
                             spawn_reset=lambda st, turn: turn["fix_rounds"]),
    "rounds": StateField("turn", load_default=lambda st: [], spawn_reset=lambda st, turn: []),
    "guards": StateField("task", load_default=lambda st: []),
    "base_check": StateField("task", load_default=lambda st: False),
    "sandbox": StateField("task", load_default=lambda st: "off"),
    "writable_dirs": StateField("task", load_default=lambda st: []),
    "sleeping": StateField("turn", spawn_reset=STATE_DELETE),
    "turn_finished": StateField("turn", spawn_reset=STATE_DELETE),
    "error": StateField("turn", spawn_reset=STATE_DELETE),
    "review_skipped": StateField("turn", spawn_reset=STATE_DELETE),
    "auto_fix_stop": StateField("turn", spawn_reset=STATE_DELETE),
    "base_merges": StateField("turn", spawn_reset=STATE_DELETE),
    "auto_fix_escalations": StateField("turn", spawn_reset=STATE_DELETE),
    "turn_stop": StateField("turn", spawn_reset=STATE_DELETE),
    "wrapup": StateField("turn", spawn_reset=STATE_DELETE),
    "review_result": StateField("turn", spawn_reset=STATE_DELETE),
    # One review angle may span multiple exec attempts.
    "review_angle": StateField("turn", spawn_reset=STATE_DELETE),
    # Refusal belongs to the current phase, which may span multiple attempts.
    "refusal": StateField("turn", spawn_reset=STATE_DELETE),
    "stop_request": StateField("runtime", spawn_reset=STATE_DELETE),
    "signal": StateField("runtime", spawn_reset=STATE_DELETE),
    "ci": StateField("turn", spawn_reset=STATE_DELETE),
    "slug": StateField("task"),
    "repo": StateField("task"),
    "worktree": StateField("task"),
    "branch": StateField("task"),
    "base": StateField("task"),
    "effort": StateField("task"),
    "thread": StateField("cursor"),
    "check": StateField("task"),
    "check_timeout": StateField("task"),
    "ci_enabled": StateField("task"),
    "no_push": StateField("task"),
    "ci_command": StateField("task"),
    "ci_rounds": StateField("task"),
    "ci_timeout_minutes": StateField("task"),
    "max_task_weekly_percent": StateField("task"),
    "weekly_used_percent": StateField("history"),
    "created": StateField("task"),  # Immutable task birth metadata.
    "plugin_version": StateField("task"),
    "pid": StateField("runtime"),
    "codex_pid": StateField("runtime"),
    "check_pid": StateField("runtime"),
    "account_ids": StateField("task"),  # Task-wide identity cache, refreshed by probes.
    # Auth retry bookkeeping is reset by _run_turn(new_turn=True), not spawn.
    "auth_excluded": StateField("turn"),
    "auth_excluded_ids": StateField("turn"),
    "auth_failures": StateField("turn"),
    "auth_transitions": StateField("turn"),
    "needs_login": StateField("task"),  # Account health cache; cleared by a fresh successful probe.
    "reset_reconciled": StateField("attempt"),  # Short-lived reset/probe coordination flag.
    "weekly_marks": StateField("task"),  # High-water cache per account/weekly window.
    "weekly_budget_warning": StateField("task"),  # Derived quota probe cache, recomputed as needed.
    "check_paths": StateField("history"),  # Accumulating protected paths; explicit --no-check clears it.
    "max_turn_minutes": StateField("turn"),  # say replaces this launch parameter, not spawn.
    "review_angles": StateField("turn"),  # say/review replace this launch parameter, not spawn.
    "reviewer": StateField("task", load_default=lambda st: "codex"),
    "publish": StateField("turn"),  # Last work receipt survives standalone review, like result.
    # New user turns discard the exec cursor; automatic phases retain the last attempt.
    "exec_event_start": StateField("attempt", spawn_reset=STATE_DELETE),
    # review_turn marks the account unselected (None) before phase/angle early exits.
    "review_account": StateField("cursor"),
}


def load(slug):
    p = state_path(slug)
    if not p.exists():
        procs.die(f"no task {slug!r}")
    st = json.loads(p.read_text())
    for key, field in STATE_FIELDS.items():
        if field.load_default is not None:
            st.setdefault(key, field.load_default(st))
    return st


@contextmanager
def state_lock(slug):
    # stop is the only concurrent state writer; serialize its read/modify/write
    # with supervisor saves so neither can lose the other's published fields.
    with state_path(slug).with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_state(st):
    p = state_path(st["slug"])
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(p)


def save(st):
    with state_lock(st["slug"]):
        p = state_path(st["slug"])
        if st.get("pid") and p.exists():
            latest = json.loads(p.read_text())
            if latest.get("pid") == st["pid"] and "stop_request" in latest:
                st["stop_request"] = latest["stop_request"]
        write_state(st)


def stop_outcome(st, stopped):
    """Classify cancellation only by a stop request addressed to this supervisor."""
    if not stopped():
        return None
    pid = st.get("pid")
    request = st.get("stop_request") or {}
    if pid and request.get("pid") != pid:
        # The signal can arrive before the supervisor's next save imports stop's
        # durable request. Atomic state replacement makes this read consistent.
        latest = load(st["slug"])
        if latest.get("pid") == pid:
            request = latest.get("stop_request") or {}
            if request.get("pid") == pid:
                st["stop_request"] = request
    return "stopped" if pid and request.get("pid") == pid else "interrupted"


def running_tasks(exclude=""):
    out = []
    for p in config.TASKS.glob("*/state.json"):
        st = json.loads(p.read_text())
        if st["slug"] != exclude and st.get("kind") != "scout" and procs.alive(st.get("pid")):
            out.append(st["slug"])
    return out
