"""Telemetry journal rows and measured supervisor phases."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import budgets, clock, config, notifications, task_state, threads


TELEMETRY_PHASES = []


def telemetry_error(st, exc):
    try:
        with (config.TASKS / st["slug"] / "stderr.log").open("a", encoding="utf-8") as errors:
            errors.write(f"telemetry unavailable: {exc}\n")
    except OSError:
        pass


def telemetry_phase(st):
    return next((phase for phase in reversed(TELEMETRY_PHASES) if phase.st is st and not phase.finished), None)


def write_telemetry(st, row, stopped):
    try:
        payload = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        with (config.CODEX_HOME / "codex-worker-log.jsonl").open("a+b", buffering=0) as stream:
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if stopped() or remaining <= 0:
                        raise
                    time.sleep(min(0.05, remaining))
            if stream.seek(0, os.SEEK_END):
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    payload = b"\n" + payload
            remaining = memoryview(payload)
            while remaining:
                written = stream.write(remaining)
                if not written:
                    raise OSError("short telemetry write")
                remaining = remaining[written:]
    except Exception as exc:
        telemetry_error(st, exc)


def record_turn_result(st, stopped):
    """Persist the supervisor's final result separately from measured stages."""
    try:
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        review = st.get("review_result") or ((reviews[-1].get("result") or {}) if reviews else {})
        result = st.get("result") or {}
        row = {"v": 1, "at": clock.CLOCK.time(), "slug": st["slug"], "repo": Path(st["repo"]).name,
               "created": st.get("created"), "kind": st.get("kind", "work"), "phase": "turn",
               "turn": st.get("turns", 0), "round": len(st.get("rounds", [])),
               "account": notifications.current_turn_account(st), "started": st.get("turn_started"),
               "finished": st.get("turn_finished"), "outcome": st.get("outcome"),
               "result_status": result.get("status"), "verdict": review.get("verdict"),
               "auto_fix_stop": st.get("auto_fix_stop"),
               "ci": st.get("ci"),
               "capacity_retries": st.get("capacity_retries", 0),
               "max_task_weekly_percent": st.get("max_task_weekly_percent"),
               "budget_stop": budgets.budget_stop(st)}
        write_telemetry(st, row, stopped)
    except Exception as exc:
        telemetry_error(st, exc)


class PhaseTelemetry:
    """Supervisor-only, best-effort accounting. Never persist prompts or output."""

    def __init__(self, st, phase, stopped=lambda: False):
        self.st, self.phase = st, phase
        self.stopped = stopped
        self.started, self.clock = clock.CLOCK.time(), clock.CLOCK.monotonic()
        self.sleep_seconds = 0.0
        self.weekly_started = st.get("weekly_used_percent", 0)
        self.finished = False
        self.usage = []
        self.kind = st.get("kind", "work")
        self.round = len(st.get("rounds", []))
        self.angle = (st.get("review_angle") or "correctness") if self.kind == "review" else None
        self.review_start = len(st.get("reviews", []))
        self.rotation_start = len(st.get("rotations", []))
        self.capacity_start = st.get("capacity_retries", 0)
        self.check = None
        self.reviewer = "codex"
        self.cost_usd = None

    def add_usage(self, account, thread, before, path, offset, weekly_spent=0):
        if not thread:
            return
        first, last = threads.rollout_usage(path, offset)
        weekly_before = (first or before or {}).get("weekly")
        last = last or before
        tokens = {}
        for key in config.TOKEN_FIELDS:
            start = (before or {}).get("tokens", {}).get(key)
            if start is None:
                start = (first or {}).get("tokens", {}).get(key)
            end = (last or {}).get("tokens", {}).get(key)
            if end is None:
                end = (before or {}).get("tokens", {}).get(key)
            tokens[key] = (end - start if type(start) in (int, float) and type(end) in (int, float)
                           and end >= start else None)
        self.usage.append({"account": account, "thread": thread, "tokens": tokens,
                           "weekly_used_before": weekly_before,
                           "weekly_used_after": ((last or {}).get("weekly")
                                                 if (last or {}).get("weekly") is not None else weekly_before),
                           "weekly_spent": weekly_spent})

    def add_claude_usage(self, telemetry):
        self.reviewer = "claude"
        self.cost_usd = telemetry.get("cost_usd")
        usage = telemetry.get("usage") or {}
        # Anthropic reports cached inputs separately; Codex input includes them.
        inputs = [usage.get(key) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        tokens = {"input": sum(value for value in inputs if value is not None)
                  if any(value is not None for value in inputs) else None,
                  "cached_input": usage.get("cache_read_input_tokens"),
                  "output": usage.get("output_tokens"), "reasoning": None}
        self.usage.append({"account": None, "thread": None, "tokens": tokens,
                           "weekly_used_before": None, "weekly_used_after": None, "weekly_spent": 0})

    def finish(self, outcome=None, result=None):
        if self.finished:
            return
        self.finished = True
        try:
            st = self.st
            reviewing = self.kind == "review"
            reviews = st.get("reviews", [])[self.review_start:]
            if result is None:
                result = (reviews[-1].get("result") if reviews else None) if reviewing else st.get("result")
            if self.phase == "wrapup":
                result = st.get("wrapup", {}).get("result")
            elif self.phase in ("check", "base_check"):
                result = None
            result = result or {}
            decisions = result.get("findings", []) if not reviewing else []
            tokens = {key: (sum(entry["tokens"][key] for entry in self.usage if entry["tokens"][key] is not None)
                            if any(entry["tokens"][key] is not None for entry in self.usage)
                            else None) for key in config.TOKEN_FIELDS}
            last = self.usage[-1] if self.usage else {}
            finished = clock.CLOCK.time()
            check = self.check or {}
            row = {"v": 1, "at": finished, "slug": st["slug"], "repo": Path(st["repo"]).name,
                   "created": st.get("created"),
                   "kind": self.kind, "phase": self.phase, "turn": st.get("turns", 0),
                   "round": self.round, "angle": self.angle,
                   "account": last.get("account", st.get("review_account") if reviewing else st.get("account")),
                   "thread": last.get("thread", (reviews[-1].get("thread") if reviews else None)
                                      if reviewing else st.get("thread")),
                   "started": self.started, "finished": finished,
                   "active_seconds": round(max(0, clock.CLOCK.monotonic() - self.clock - self.sleep_seconds), 3),
                   "sleep_seconds": round(self.sleep_seconds, 3),
                   "outcome": outcome or st.get("outcome"), "result_status": result.get("status"),
                   "verdict": result.get("verdict"),
                   "findings": len(result["findings"]) if "findings" in result else None,
                   "decisions": {key: sum(item.get("decision") == key for item in decisions)
                                 for key in ("fixed", "rejected")},
                   "check_outcome": check.get("outcome"), "check_seconds": check.get("seconds"),
                   "rotations": sum("from" in entry and "to" in entry
                                    for entry in st.get("rotations", [])[self.rotation_start:]),
                   "capacity_retries": st.get("capacity_retries", 0) - self.capacity_start,
                   "tokens": tokens, "weekly_used_before": last.get("weekly_used_before"),
                   "weekly_used_after": last.get("weekly_used_after"), "usage": self.usage,
                   "weekly_spent": st.get("weekly_used_percent", 0) - self.weekly_started}
            if self.phase == "review":
                row["reviewer"] = self.reviewer
                if self.reviewer == "claude":
                    row.update(account=None, thread=None, weekly_spent=0, cost_usd=self.cost_usd)
            write_telemetry(st, row, self.stopped)
        except Exception as exc:
            telemetry_error(self.st, exc)


@contextmanager
def record_phase(st, phase, stopped):
    entry = PhaseTelemetry(st, phase, stopped)
    TELEMETRY_PHASES.append(entry)
    try:
        yield entry
    except (Exception, SystemExit):
        entry.finish(outcome=task_state.stop_outcome(st, stopped) or "failed")
        raise
    finally:
        entry.finish(outcome=task_state.stop_outcome(st, stopped))
        TELEMETRY_PHASES.pop()
