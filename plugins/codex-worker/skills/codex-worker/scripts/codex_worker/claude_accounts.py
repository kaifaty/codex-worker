"""Role-neutral Claude Code accounts and credential-free cached limit snapshots."""
import fcntl
import json
import os
from contextlib import contextmanager

from . import clock, config, result_schema

TOKEN_RETRY_SECONDS = 3600


def accounts(cfg):
    return cfg["claude"]["accounts"]


def format_windows(info):
    windows, parts = info.get("unifiedWindows") or {}, []
    for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = windows.get(name) or {}
        used = window.get("utilization")
        percent = f"{used * 100:g}%" if type(used) in (int, float) else "unknown"
        parts.append(f"{label} {percent} (reset {config.local_time(window.get('resetsAt'))})")
    return "; ".join(parts)


def read_limits():
    try:
        limits = json.loads((config.TASKS / "claude-limits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(limits, dict):
        return {}
    if (isinstance(limits.get("status"), str) or result_schema.is_finite_number(limits.get("utilization"))
            or result_schema.is_finite_number(limits.get("resetsAt")) or "unifiedWindows" in limits):
        return {"default": limits}
    limits = {name: info for name, info in limits.items() if isinstance(info, dict)}
    for info in limits.values():
        if info.get("token_unavailable") and not result_schema.is_finite_number(info.get("token_retry_at")):
            # Earlier versions kept this mark until the next login; give it one hour.
            marked = info.get("limited_at")
            info["token_retry_at"] = marked + TOKEN_RETRY_SECONDS if result_schema.is_finite_number(marked) else 0
            if not result_schema.is_finite_number(info.get("limited_until")):
                info["limited_until"] = info["token_retry_at"]
    return limits


@contextmanager
def _locked_limits():
    config.TASKS.mkdir(parents=True, exist_ok=True)
    path = config.TASKS / "claude-limits.json"
    with (config.TASKS / "claude-limits.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        limits = read_limits()
        yield limits
        temporary = path.with_suffix(".%s.tmp" % os.getpid())
        temporary.write_text(json.dumps(limits), encoding="utf-8")
        temporary.replace(path)


def save_limits(name, info, event_time=None):
    """Store only documented numeric fields, never arbitrary CLI output."""
    selected = {}
    if info.get("status") in ("allowed", "allowed_warning", "rejected"):
        selected["status"] = info["status"]
    for key in ("utilization", "resetsAt"):
        if result_schema.is_finite_number(info.get(key)):
            selected[key] = info[key]
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        selected["unifiedWindows"] = {
            window_name: {key: window[key] for key in ("utilization", "resetsAt")
                          if result_schema.is_finite_number(window.get(key))}
            for window_name in ("five_hour", "seven_day")
            for window in [windows.get(window_name)] if isinstance(window, dict)}
    with _locked_limits() as limits:
        if selected:
            selected["updated_at"] = clock.CLOCK.time()
        previous = limits.get(name, {})
        active = limited(previous)
        marked_at = previous.get("limited_at")
        fresh = (result_schema.is_finite_number(event_time)
                 and result_schema.is_finite_number(marked_at) and event_time > marked_at)
        previous.update(selected)
        if (selected.get("status") in ("allowed", "allowed_warning") and (not active or fresh)
                and not token_marked(previous)):
            previous.pop("limited_until", None)
            previous.pop("limited_at", None)
        limits[name] = previous


def clear_limits(name):
    with _locked_limits() as limits:
        limits.pop(name, None)


def rename_limits(old, new):
    with _locked_limits() as limits:
        if old in limits:
            limits[new] = limits.pop(old)


def token_available(name):
    """Drop a missing-token mark after a successful read; keep a later limit."""
    if not read_limits().get(name, {}).get("token_unavailable"):
        return
    with _locked_limits() as limits:
        info = limits.get(name)
        if not info or not info.get("token_unavailable"):
            return
        until = info.pop("token_retry_at")
        info.pop("token_unavailable", None)
        reset = info.get("limited_until")
        if result_schema.is_finite_number(reset) and reset <= until:
            info.pop("limited_until", None)
            info.pop("limited_at", None)


def _window_used(window, now):
    reset = window.get("resetsAt")
    if result_schema.is_finite_number(reset) and reset <= now:
        return 0
    used = window.get("utilization")
    return used * 100 if result_schema.is_finite_number(used) else 0


def stale(cfg, info, now=None):
    now = clock.CLOCK.time() if now is None else now
    updated = info.get("updated_at")
    max_age = cfg["claude"].get("limits_max_age_minutes", 30)
    return (max_age == 0 or not result_schema.is_finite_number(updated)
            or now - updated > max_age * 60)


def probe_due(cfg, info, now=None):
    now = clock.CLOCK.time() if now is None else now
    attempted = info.get("probe_attempted_at")
    max_age = cfg["claude"].get("limits_max_age_minutes", 30)
    return stale(cfg, info, now) and (
        max_age == 0 or not result_schema.is_finite_number(attempted)
        or now - attempted >= max_age * 60)


def mark_probe_attempt(name):
    with _locked_limits() as limits:
        limits.setdefault(name, {})["probe_attempted_at"] = clock.CLOCK.time()


def token_marked(info, now=None):
    """Whether a missing-token mark from read_limits() is still active."""
    now = clock.CLOCK.time() if now is None else now
    return bool(info.get("token_unavailable")) and info.get("token_retry_at", 0) > now


def limited(info, now=None):
    now = clock.CLOCK.time() if now is None else now
    if "limited_until" in info:
        reset = info["limited_until"]
        return not result_schema.is_finite_number(reset) or reset > now
    if info.get("status") == "rejected":
        reset = info.get("resetsAt")
        return not result_schema.is_finite_number(reset) or reset > now
    return False


def mark_limited(name, token_unavailable=False):
    """Exclude an account until its known reset, or until fresh login/limit data.

    A missing token excludes it for TOKEN_RETRY_SECONDS; the next selection
    after that reads the token again.
    """
    now = clock.CLOCK.time()
    with _locked_limits() as limits:
        info = limits.setdefault(name, {})
        if token_unavailable:
            until = now + TOKEN_RETRY_SECONDS
            if limited(info, now):
                # An unknown reset would exclude the account for good; keep the hour.
                reset = info.get("limited_until", info.get("resetsAt"))
                if result_schema.is_finite_number(reset) and reset > until:
                    until = reset
            info.update(token_unavailable=True, token_retry_at=now + TOKEN_RETRY_SECONDS,
                        limited_until=until, limited_at=now)
            return until
        windows = info.get("unifiedWindows") or {}
        depleted = [window.get("resetsAt") for window in windows.values()
                    if isinstance(window, dict) and _window_used(window, now) >= 100]
        depleted = [reset for reset in depleted if result_schema.is_finite_number(reset) and reset > now]
        resets = [window.get("resetsAt") for window in windows.values() if isinstance(window, dict)]
        resets = [reset for reset in resets if result_schema.is_finite_number(reset) and reset > now]
        reset = info.get("resetsAt")
        if not result_schema.is_finite_number(reset) or reset <= now:
            reset = max(depleted) if depleted else min(resets, default=None)
        if token_marked(info, now) and (not result_schema.is_finite_number(reset)
                                        or reset < info["token_retry_at"]):
            reset = info["token_retry_at"]
        info["limited_until"] = reset
        info["limited_at"] = now
        return reset


def select(cfg, role, exclude=(), prefer_unused=()):
    """Choose the weighted weekly headroom that must be spent fastest."""
    limits, now = read_limits(), clock.CLOCK.time()
    choices = [account for account in accounts(cfg) if account["name"] not in exclude
               and not limited(limits.get(account["name"], {}), now)]
    thresholds = (cfg.get("soft_switch_at", cfg["switch_at"]), cfg["switch_at"])
    if role == "work":
        # Work keeps the top of each account's week in reserve for reviews.
        work = cfg["claude"].get("work_switch_at", 85)
        thresholds = (min(work, thresholds[0]), work)
    for threshold in thresholds:
        available = [account for account in choices if all(
            _window_used((limits.get(account["name"], {}).get("unifiedWindows") or {}).get(window) or {}, now)
            < threshold for window in ("five_hour", "seven_day"))]
        if available:
            available = [account for account in available if account["name"] not in prefer_unused] or available

            def priority(account):
                windows = limits.get(account["name"], {}).get("unifiedWindows") or {}
                weekly = windows.get("seven_day") or {}
                reset = weekly.get("resetsAt")
                hours = (reset - now) / 3600 if result_schema.is_finite_number(reset) and reset > now else 168
                score = (100 - _window_used(weekly, now)) * account.get("weight", 1) / max(hours, 1)
                return -score, _window_used(windows.get("five_hour") or {}, now)

            return min(available, key=priority)
    return None
