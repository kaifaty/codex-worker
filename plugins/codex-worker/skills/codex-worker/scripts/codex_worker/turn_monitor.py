"""Turn control, event streams and rollout monitoring."""
import fcntl
import hashlib
import json
import os
import selectors
import shlex
import subprocess
import sys
import termios
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, Optional

from . import account_rotation, accounts, budgets, clock, config, procs, result_schema, sandboxing, task_state, telemetry_log, threads


def _search_command(command):
    while True:
        if "\n" in command or "\r" in command:
            return False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&")
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = list(lexer)
        except ValueError:
            return False
        if (len(words) == 3 and Path(words[0]).name in ("sh", "bash", "zsh", "dash", "ksh", "fish")
                and words[1] in ("-c", "-lc")):
            command = words[2]
            continue
        if not words or any(word and all(char in "|;&" for char in word) and word != "|" for word in words):
            return False
        while "|" in words:
            words = words[words.index("|") + 1:]
        return bool(words) and (Path(words[0]).name in ("pgrep", "grep", "egrep", "fgrep", "rg")
                                or (Path(words[0]).name == "git" and words[1:2] == ["grep"]))


class TurnControl:
    """Limits belong to a phase, surviving exec retries but excluding account waits."""

    def __init__(self, minutes, repeat_limit, weekly=None):
        self.minutes = minutes
        self.weekly = weekly
        self.seconds = None if minutes is None else minutes * config.MINUTE_SECONDS
        self.elapsed = 0.0
        self.repeat_limit = repeat_limit
        self.failures = {}
        self.reason = None

    def expired(self, started):
        if self.seconds is None:
            return False
        usage = budgets.monitor_time_usage(self.elapsed, self.seconds, self.minutes, started, clock.CLOCK.monotonic())
        if usage.reason is not None:
            self.reason = usage.reason
            return True
        return False

    def weekly_exhausted(self, spent):
        if self.weekly is not None and spent >= self.weekly:
            self.reason = {"outcome": "budget_exhausted", "weekly": self.weekly, "spent": spent}
            return True
        return False

    def repeated(self, event):
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or item.get("type") != "command_execution":
            return False
        command, code = item.get("command"), item.get("exit_code")
        if not isinstance(command, str) or type(code) is not int:
            return False
        if code == 0:
            self.failures.pop(command, None)
            return False
        output = item.get("aggregated_output", "")
        if not isinstance(output, str):
            return False
        if code == 1 and not output.strip() and _search_command(command):
            self.failures.pop(command, None)
            return False
        digest = hashlib.sha256(output.encode("utf-8")[-2048:]).hexdigest()
        signature = (code, digest)
        previous, count = self.failures.get(command, (None, 0))
        count = count + 1 if signature == previous else 1
        self.failures[command] = (signature, count)
        if count >= self.repeat_limit:
            self.reason = {"outcome": "stuck", "command": command, "exit_code": code,
                           "output_sha256": digest, "count": count}
            return True
        return False


def turn_stop_message(reason):
    if reason["outcome"] == "budget_exhausted":
        if "weekly" in reason:
            return f"weekly budget {reason['weekly']:g}% exhausted (spent {reason['spent']:g}%)"
        return f"time budget {reason['minutes']:g} min exhausted"
    return f"repeated failure ×{reason['count']}: {reason['command']}"


class MonitorQuotaDecision(NamedTuple):
    action: Literal["continue", "limit", "failed"]
    info: Optional[dict] = None
    reason: Optional[str] = None
    allow_credits: bool = False


def monitor_quota_decision(info, cfg, reason=None) -> MonitorQuotaDecision:
    """Choose a quota action from a limit sample/settings, without RPCs or writes."""
    if reason:
        return MonitorQuotaDecision("limit" if cfg["enabled"] else "failed", info, reason)
    if cfg["enabled"] and accounts.exhausted(info, cfg):
        return MonitorQuotaDecision("limit", info, account_rotation.limit_reason(info, cfg), True)
    if cfg["enabled"] and accounts.exhausted(info, cfg, soft=True):
        return MonitorQuotaDecision("limit", info, account_rotation.limit_reason(info, cfg, soft=True), True)
    return MonitorQuotaDecision("continue")


@dataclass
class MonitorEvents:
    """State derived from exec stdout, including an unfinished JSON line."""
    thread: str
    review_entry: Optional[dict] = None
    buffer: bytes = b""
    eof: bool = False
    failed: bool = False
    completed: bool = False
    reason: Optional[str] = None
    authentication_failed: bool = False
    capacity_failed: bool = False
    refusal: Optional[str] = None
    final_message: str = ""
    exit_code: Optional[int] = None


@dataclass
class MonitorRollout:
    """Rollout polling cursor and the stdout prefix queued at a weekly stop."""
    path: Optional[Path]
    previous_size: int
    usage_offset: int
    next_search: float
    next_poll: float = 0.0
    pending_weekly_spent: Optional[float] = None
    pending_weekly_bytes: int = 0
    current_info: Optional[dict] = None


def monitor_command(st, cfg, prompt, effort, reviewing, review_fallback, wrapup, thread, last):
    """Prepare exec arguments/schema and return the effective prompt for fallback."""
    if last.exists():
        last.unlink()
    args = [config.codex_binary(), "exec"]
    if reviewing and not wrapup and not thread:
        # Some CLI versions reject --base together with custom instructions.
        if not review_fallback:
            args += ["review", "--base", st["base"]]
        args += ["--json", "-o", str(last)]
    elif thread:
        args += ["resume", thread, "--json", "-o", str(last)]
    else:
        args += ["--json", "-C", st["worktree"], "-o", str(last)]
    if st.get("kind") == "scout":
        args += ["-c", 'sandbox_mode="read-only"']
    else:
        args += sandboxing.sandbox_args(st)
        if st.get("sandbox") == "workspace":
            rules = sandboxing.sandbox_rules(cfg, reviewing=reviewing, no_push=st.get("no_push", False))
            if not prompt.endswith(rules):
                prompt += "\n\n" + rules
    if st.get("kind") != "scout":
        schema = last.parent / ("review-schema.json" if reviewing else "result-schema.json")
        schema.write_text(json.dumps(result_schema.REVIEW_SCHEMA if reviewing else result_schema.RESULT_SCHEMA, indent=2), encoding="utf-8")
        args += ["--output-schema", str(schema)]
    return args + threads.effort_args(effort) + [prompt], prompt


@contextmanager
def monitor_process(st, account, args, errors):
    """Own one exec process group; terminate it before the caller's final accounting."""
    proc = subprocess.Popen(args, cwd=st["worktree"], env=accounts.account_env(account), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
    try:
        yield proc
    finally:
        procs.terminate(proc)


def monitor_thread_started(st, account, stream, reviewing, wrapup, attempt_started):
    """Apply thread.started to task/event state before saving the new thread."""
    thread = stream.thread
    if reviewing and not wrapup:
        stream.review_entry = {"account": account["name"], "thread": thread,
                               "started": attempt_started, "finished": None, "result": None,
                               "round": len(st.get("rounds", [])),
                               "angle": st.get("review_angle") or "correctness"}
        st.setdefault("reviews", []).append(stream.review_entry)
    elif not reviewing:
        st["thread"] = thread
    if not reviewing and not any(t["account"] == account["name"] and t["thread"] == thread for t in st["threads"]):
        st["threads"].append({"account": account["name"], "thread": thread, "started": clock.CLOCK.time()})
    if (st["rotations"] and not st["rotations"][-1].get("event")
            and not st["rotations"][-1]["thread"]):
        st["rotations"][-1]["thread"] = thread
    task_state.save(st)


def monitor_event(st, account, event, stream, control, reviewing, wrapup, attempt_started):
    """Apply one decoded event in wire order; return an immediate stop, if any."""
    if event.get("type") == "thread.started":
        stream.thread = event["thread_id"]
        monitor_thread_started(st, account, stream, reviewing, wrapup, attempt_started)
    if (event.get("type") == "item.completed"
            and (event.get("item") or {}).get("type") == "agent_message"):
        stream.final_message = event["item"].get("text", "")
    stream.refusal = account_rotation.refusal_message(event)
    if stream.refusal is not None:
        st["refusal"] = stream.refusal
        return "refused", None
    if control.repeated(event):
        return "stuck", None
    if account_rotation.limit_error(event):
        stream.reason = "usage limit"
    if account_rotation.auth_error(event):
        stream.authentication_failed = True
    if not stream.reason and not stream.authentication_failed and account_rotation.capacity_error(event):
        stream.capacity_failed = True
    # `error` events also carry retries ("Reconnecting... 2/5") of turns that complete.
    if event.get("type") == "turn.failed":
        stream.failed = True
    if event.get("type") == "turn.completed":
        stream.completed = True
    return None


def read_monitor_events(st, account, ready, sel, log, stream, usage, control,
                        reviewing, wrapup, attempt_started):
    """Append ready bytes verbatim, then decode complete lines without reordering."""
    for key, _ in ready:
        size = min(65536, usage.pending_weekly_bytes) if usage.pending_weekly_spent is not None else 65536
        chunk = os.read(key.fileobj.fileno(), size)
        if usage.pending_weekly_spent is not None:
            usage.pending_weekly_bytes -= len(chunk)
        if not chunk:
            sel.unregister(key.fileobj)
            stream.eof = True
        else:
            log.write(chunk)
            stream.buffer += chunk
            while b"\n" in stream.buffer:
                line, stream.buffer = stream.buffer.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                result = monitor_event(st, account, event, stream, control, reviewing, wrapup, attempt_started)
                if result is not None:
                    return result
    return None


def resolve_monitor_quota(st, request, stopped, control, active_started, on_limit):
    """Run quota effects while exec is alive, preserving stop/deadline priority."""
    if request.action == "failed":
        return "failed", None
    # Quota RPCs and reset-journal locks run while exec is still alive.
    def cancelled():
        return stopped() or control.expired(active_started)

    try:
        decision = on_limit(request.info, request.reason, request.allow_credits, cancelled)
    except RuntimeError:
        if not cancelled():
            raise
        decision = None
    if stopped():
        return task_state.stop_outcome(st, stopped), None
    if control.expired(active_started):
        return "budget_exhausted", None
    return ("limit", decision) if decision is not None or not request.allow_credits else None


def poll_monitor_rollout(st, cfg, account, proc, stream, usage, control, record_limits):
    """Read a changed rollout sample; return its quota action or queue a weekly stop."""
    now = time.monotonic()
    if usage.path is None and now >= usage.next_search:
        usage.path = threads.rollout_path(account["home"], stream.thread)
        usage.next_search = now + config.ROLLOUT_SEARCH_SECONDS
    if usage.path is None or now < usage.next_poll:
        return MonitorQuotaDecision("continue")
    usage.next_poll = now + config.RATE_POLL_SECONDS
    size = usage.path.stat().st_size
    if size == usage.previous_size:
        return MonitorQuotaDecision("continue")
    limits = threads.rollout_limits(usage.path, record_limits, usage.usage_offset)
    usage.previous_size = size
    if limits is None:
        return MonitorQuotaDecision("continue")
    # A completed model turn needs no wrap-up while the CLI exits.
    # The supervisor checks its final usage after monitor returns.
    if control.weekly is not None and not stream.completed:
        spent = st.get("weekly_used_percent", 0)
        if spent >= control.weekly:
            usage.pending_weekly_spent = spent
            queued = fcntl.ioctl(proc.stdout.fileno(), termios.FIONREAD, bytes(4))
            usage.pending_weekly_bytes = int.from_bytes(queued, sys.byteorder)
            return MonitorQuotaDecision("continue")
    usage.current_info = dict(account, logged_in=True, limits=limits)
    return monitor_quota_decision(usage.current_info, cfg)


def monitor_event_stream(st, cfg, account, proc, log, stream, usage, control, stopped,
                         on_limit, record_limits, reviewing, wrapup, attempt_started, active_started):
    """Supervise exec/stdout and return the original (outcome, quota payload) pair."""
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        while not stream.eof or proc.poll() is None:
            if stopped():
                procs.terminate(proc)
                return task_state.stop_outcome(st, stopped), None
            if control.expired(active_started):
                return "budget_exhausted", None
            ready = ([] if usage.pending_weekly_spent is not None and usage.pending_weekly_bytes == 0
                     else sel.select(0 if usage.pending_weekly_spent is not None else 0.2))
            result = read_monitor_events(st, account, ready, sel, log, stream, usage, control,
                                         reviewing, wrapup, attempt_started)
            if result is not None:
                return result
            if wrapup:
                # A wrap-up gets one bounded attempt, without quota waits or retries.
                if stream.authentication_failed or stream.reason or stream.capacity_failed:
                    return "failed", None
                continue
            if usage.pending_weekly_spent is not None:
                # Rollout and stdout are separate streams. A completion
                # sent before the quota sample can still be queued here.
                # Drain that queued prefix (including partial JSON),
                # without waiting for or following subsequent output.
                if not stream.completed:
                    if ready and usage.pending_weekly_bytes > 0 and not stream.eof:
                        continue
                    if control.weekly_exhausted(usage.pending_weekly_spent):
                        return "budget_exhausted", None
                usage.pending_weekly_spent = None
            if stream.authentication_failed:
                return ("auth" if cfg["enabled"] else "failed"), None
            if stream.reason:
                request = monitor_quota_decision(usage.current_info, cfg, stream.reason)
                return resolve_monitor_quota(st, request, stopped, control, active_started, on_limit)
            if stream.capacity_failed:
                return "capacity", None
            if (cfg["enabled"] or control.weekly is not None) and stream.thread and proc.poll() is None:
                request = poll_monitor_rollout(st, cfg, account, proc, stream, usage, control, record_limits)
                if request.action != "continue":
                    result = resolve_monitor_quota(st, request, stopped, control, active_started, on_limit)
                    if result is not None:
                        return result
        stream.exit_code = proc.wait()
    return ("completed" if stream.exit_code == 0 and not stream.failed else "failed"), None


def finish_monitor(st, account, proc, stream, usage, last, reviewing, wrapup,
                   record_limits, telemetry, usage_before, weekly_started):
    """Close stdout, finalize results, account final rollout/telemetry, then save."""
    proc.stdout.close()
    st.pop("codex_pid", None)
    message = last.read_text(encoding="utf-8").strip() if last.exists() else stream.final_message
    if wrapup:
        st["wrapup"]["result"] = None if stream.refusal else result_schema.parse_result(message, reviewing=reviewing)
        if stream.refusal:
            st["wrapup"]["refusal"] = stream.refusal
    elif stream.review_entry is not None:
        stream.review_entry["finished"] = clock.CLOCK.time()
        stream.review_entry["result"] = None if stream.refusal else result_schema.parse_result(message, reviewing=True)
        if stream.refusal:
            stream.review_entry.update(outcome="refused", refusal=stream.refusal)
    elif not reviewing and st.get("kind") != "scout":
        st["result"] = None if stream.refusal else result_schema.parse_result(message)
    try:
        final_path = usage.path or threads.rollout_path(account["home"], stream.thread)
        if final_path is not None:
            threads.rollout_limits(final_path, record_limits, usage.usage_offset)
        if telemetry is not None:
            telemetry.add_usage(account["name"], stream.thread, usage_before,
                                final_path, usage.usage_offset,
                                st.get("weekly_used_percent", 0) - weekly_started)
    except Exception as exc:
        telemetry_log.telemetry_error(st, exc)
    task_state.save(st)


def monitor(st, cfg, account, prompt, effort, stopped, on_limit, review_fallback=False,
            control=None, wrapup=False, wrapup_thread=None, quota_snapshot=None, resume_thread=None):
    """One exec attempt. The supervisor alone owns state; append CLI events verbatim."""
    d = config.TASKS / st["slug"]
    accounts.prepare_home(account)
    reviewing = st.get("kind") == "review"
    control = control or TurnControl(cfg.get("max_review_minutes", 30) if st.get("kind") in ("review", "scout")
                                     else st.get("max_turn_minutes", cfg.get("max_turn_minutes")),
                                     cfg.get("repeat_limit", 3), st.get("max_task_weekly_percent"))
    thread = (wrapup_thread if wrapup else resume_thread if resume_thread is not None
              else "" if reviewing else st.get("thread", ""))
    stream = MonitorEvents(thread)
    last = d / ("review.md" if reviewing else "last.md")
    args, prompt = monitor_command(st, cfg, prompt, effort, reviewing, review_fallback, wrapup, thread, last)
    path = threads.rollout_path(account["home"], thread) if thread else None
    previous_size = path.stat().st_size if path else 0
    telemetry = telemetry_log.telemetry_phase(st)
    usage_before = threads.rollout_usage(path)[1] if telemetry is not None else None
    weekly_started = st.get("weekly_used_percent", 0)
    weekly_seeded = budgets.weekly_used(quota_snapshot or {}) is not None
    if weekly_seeded:
        st.update(budgets.monitor_weekly_usage(st, account["name"], quota_snapshot, False).updates)

    def record_limits(limits):
        nonlocal weekly_seeded
        sample = budgets.monitor_weekly_usage(st, account["name"], limits, weekly_seeded)
        st.update(sample.updates)
        weekly_seeded = sample.seeded
        return sample.delta

    next_search = time.monotonic() + config.ROLLOUT_SEARCH_SECONDS if thread else 0.0
    usage = MonitorRollout(path, previous_size, previous_size, next_search)
    st["exec_event_start"] = len(threads.events(st))
    task_state.save(st)
    with (d / "events.jsonl").open("ab", buffering=0) as log, (d / "stderr.log").open("a") as errors:
        if stopped():
            return task_state.stop_outcome(st, stopped), None
        error_start = errors.tell()
        attempt_started = clock.CLOCK.time()
        active_started = clock.CLOCK.monotonic()
        proc = None
        try:
            with monitor_process(st, account, args, errors) as proc:
                st["codex_pid"] = proc.pid
                task_state.save(st)
                result = monitor_event_stream(st, cfg, account, proc, log, stream, usage, control, stopped,
                                              on_limit, record_limits, reviewing, wrapup, attempt_started, active_started)
        finally:
            if proc is not None:
                control.elapsed = budgets.monitor_time_usage(control.elapsed, control.seconds, control.minutes,
                                                     active_started, clock.CLOCK.monotonic()).elapsed
                finish_monitor(st, account, proc, stream, usage, last, reviewing, wrapup,
                               record_limits, telemetry, usage_before, weekly_started)
    if reviewing and not review_fallback and not stream.thread and stream.exit_code == 2:
        with (d / "stderr.log").open("rb") as errors:
            errors.seek(error_start)
            error = errors.read().decode("utf-8", errors="replace")
        if all(part in error for part in ("cannot be used with", "--base", "PROMPT")):
            return monitor(st, cfg, account, prompt, effort, stopped, on_limit, review_fallback=True,
                           control=control, quota_snapshot=quota_snapshot)
    return result
