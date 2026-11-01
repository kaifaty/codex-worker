"""Role-neutral Claude Code accounts and credential-free cached limit snapshots."""
import fcntl
import json
import math
import os
from contextlib import contextmanager

from . import clock, config

TOKEN_RETRY_SECONDS = 3600


def accounts(cfg):
    return cfg["claude"]["accounts"]


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_limits():
    try:
        limits = json.loads((config.TASKS / "claude-limits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(limits, dict):
        return {}
    if (isinstance(limits.get("status"), str) or _finite(limits.get("utilization"))
            or _finite(limits.get("resetsAt")) or "unifiedWindows" in limits):
        return {"default": limits}
    limits = {name: info for name, info in limits.items() if isinstance(info, dict)}
    for info in limits.values():
        if info.get("token_unavailable") and not _finite(info.get("token_retry_at")):
            # Earlier versions kept this mark until the next login; give it one hour.
            marked = info.get("limited_at")
            info["token_retry_at"] = marked + TOKEN_RETRY_SECONDS if _finite(marked) else 0
            if not _finite(info.get("limited_until")):
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
        if _finite(info.get(key)):
            selected[key] = info[key]
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        selected["unifiedWindows"] = {
            window_name: {key: window[key] for key in ("utilization", "resetsAt")
                          if _finite(window.get(key))}
            for window_name in ("five_hour", "seven_day")
            for window in [windows.get(window_name)] if isinstance(window, dict)}
    with _locked_limits() as limits:
        previous = limits.get(name, {})
        active = limited(previous)
        marked_at = previous.get("limited_at")
        fresh = _finite(event_time) and _finite(marked_at) and event_time > marked_at
        previous.update(selected)
        if (selected.get("status") in ("allowed", "allowed_warning") and (not active or fresh)
                and not previous.get("token_unavailable")):
            previous.pop("limited_until", None)
            previous.pop("limited_at", None)
        limits[name] = previous


def clear_limits(name):
    with _locked_limits() as limits:
        limits.pop(name, None)


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
        if _finite(reset) and reset <= until:
            info.pop("limited_until", None)
            info.pop("limited_at", None)


def _window_used(window, now):
    reset = window.get("resetsAt")
    if _finite(reset) and reset <= now:
        return 0
    used = window.get("utilization")
    return used * 100 if _finite(used) else 0


def token_marked(info, now=None):
    """Whether a missing-token mark from read_limits() is still active."""
    now = clock.CLOCK.time() if now is None else now
    return bool(info.get("token_unavailable")) and info.get("token_retry_at", 0) > now


def limited(info, now=None):
    now = clock.CLOCK.time() if now is None else now
    if "limited_until" in info:
        reset = info["limited_until"]
        return not _finite(reset) or reset > now
    if info.get("status") == "rejected":
        reset = info.get("resetsAt")
        return not _finite(reset) or reset > now
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
                reset = info.get("limited_until", info.get("resetsAt"))
                if not _finite(reset) or reset > until:
                    until = reset
            info.update(token_unavailable=True, token_retry_at=now + TOKEN_RETRY_SECONDS,
                        limited_until=until, limited_at=now)
            return until
        windows = info.get("unifiedWindows") or {}
        depleted = [window.get("resetsAt") for window in windows.values()
                    if isinstance(window, dict) and _window_used(window, now) >= 100]
        depleted = [reset for reset in depleted if _finite(reset) and reset > now]
        resets = [window.get("resetsAt") for window in windows.values() if isinstance(window, dict)]
        resets = [reset for reset in resets if _finite(reset) and reset > now]
        reset = info.get("resetsAt")
        if not _finite(reset) or reset <= now:
            reset = max(depleted) if depleted else min(resets, default=None)
        if token_marked(info, now) and _finite(reset) and reset < info["token_retry_at"]:
            reset = info["token_retry_at"]
        info["limited_until"] = reset
        info["limited_at"] = now
        return reset


def select(cfg, role, exclude=(), prefer_unused=()):
    """Choose by headroom and long reset; role labels the caller's journal only."""
    limits, now = read_limits(), clock.CLOCK.time()
    choices = [account for account in accounts(cfg) if account["name"] not in exclude
               and not limited(limits.get(account["name"], {}), now)]
    for threshold in (cfg.get("soft_switch_at", cfg["switch_at"]), cfg["switch_at"]):
        available = [account for account in choices if all(
            _window_used((limits.get(account["name"], {}).get("unifiedWindows") or {}).get(window) or {}, now)
            < threshold for window in ("five_hour", "seven_day"))]
        if available:
            available = [account for account in available if account["name"] not in prefer_unused] or available

            def long_reset(account):
                windows = limits.get(account["name"], {}).get("unifiedWindows") or {}
                reset = (windows.get("seven_day") or {}).get("resetsAt")
                return reset if _finite(reset) else float("inf")

            return min(available, key=long_reset)
    return None
