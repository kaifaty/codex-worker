"""Duty account elections, the shared duty record and the new-work exclusion.

One account at a time is on duty. ``elect`` is pure; ``run_duty_tick`` builds
its candidates from cached limits and records the result. New work skips the
duty account while another account can take it.
"""
import fcntl
import json
import os
import sys
from contextlib import contextmanager

from . import accounts, claude_accounts, clock, config, result_schema


DUTY_FAMILIES = ("claude", "codex")
# A duty record older than this means that nothing is re-electing.
DUTY_STALE_MINUTES = 90
WEEK_MINUTES = 10080
# One warning per process for an unreadable duty.json.
DUTY_WARNINGS = set()


def duty_path():
    return config.CODEX_HOME / "duty.json"


def duty_log_path():
    return config.CODEX_HOME / "duty-log.jsonl"


def duty_label(record):
    """``family/account`` of a duty record or decision, or None without a duty account."""
    if not record or record.get("account") is None:
        return None
    return record["family"] + "/" + record["account"]


def valid_duty(record):
    if not isinstance(record, dict) or not isinstance(record.get("reason"), str):
        return False
    account, family, rooms = record.get("account"), record.get("family"), record.get("rooms")
    if account is None:
        if family is not None:
            return False
    elif not isinstance(account, str) or not account or family not in DUTY_FAMILIES:
        return False
    if not all(result_schema.is_finite_number(record.get(key)) for key in ("since", "checked_at")):
        return False
    return isinstance(rooms, dict) and all(
        name in DUTY_FAMILIES and isinstance(entries, dict)
        and all(isinstance(key, str) and result_schema.is_finite_number(room) for key, room in entries.items())
        for name, entries in rooms.items())


def read_duty(warn=True):
    """The duty record, or None when duty.json is absent or unreadable.

    An unreadable record counts as absent; the first one read by a process
    prints one warning, and the next tick replaces the file.
    """
    try:
        record = json.loads(duty_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        record = None
    if valid_duty(record):
        return record
    if warn and not DUTY_WARNINGS:
        DUTY_WARNINGS.add(str(duty_path()))
        print(f"codex-worker: {duty_path()} is unreadable; account selection ignores it until the next duty tick",
              file=sys.stderr, flush=True)
    return None


@contextmanager
def duty_lock():
    config.CODEX_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (config.CODEX_HOME / "duty.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_duty(record):
    path = duty_path()
    temporary = path.with_suffix(".%s.tmp" % os.getpid())
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def clear_duty():
    """Remove duty.json; return whether it existed. The change log stays."""
    with duty_lock():
        try:
            duty_path().unlink()
        except FileNotFoundError:
            return False
    return True


def duty_window_known(window, now):
    if not isinstance(window, dict):
        return False
    reset = window.get("resetsAt")
    return (result_schema.is_finite_number(window.get("utilization"))
            or result_schema.is_finite_number(reset) and reset <= now)


def claude_duty_candidate(cfg, account, info, now, refreshed_at):
    """Room and availability of a Claude account from its cached limits snapshot.

    A snapshot is fresh within claude.limits_max_age_minutes, or when it was
    saved after ``refreshed_at`` (this tick's refresh).
    """
    candidate = {"family": "claude", "name": account["name"], "room": 0, "unavailable": None}
    windows = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    weekly, short = windows.get("seven_day"), windows.get("five_hour")
    updated = info.get("updated_at")
    fresh = result_schema.is_finite_number(updated) and (
        now - updated <= cfg["claude"].get("limits_max_age_minutes", 30) * 60 or updated >= refreshed_at)
    if claude_accounts.token_marked(info, now):
        candidate["unavailable"] = "token unavailable"
    elif claude_accounts.limited(info, now):
        candidate["unavailable"] = ("limited until "
                                    + config.local_time(info.get("limited_until", info.get("resetsAt"))))
    elif not duty_window_known(weekly, now):
        candidate["unavailable"] = "limits unknown"
    elif not fresh:
        candidate["unavailable"] = "limits stale"
    else:
        threshold = cfg["claude"].get("work_switch_at", 85)
        candidate["room"] = round(max(0.0, (threshold - claude_accounts.window_used(weekly, now))
                                      * account.get("weight", 1)), 3)
        five_hour = claude_accounts.window_used(short or {}, now)
        if five_hour >= cfg["switch_at"]:
            candidate["unavailable"] = f"five-hour window at {five_hour:g}%"
        elif candidate["room"] <= 0:
            candidate["unavailable"] = f"seven-day window at {claude_accounts.window_used(weekly, now):g}%"
    return candidate


def codex_duty_candidate(cfg, info):
    """Room and availability of a Codex account from a fresh limits snapshot."""
    candidate = {"family": "codex", "name": info["name"], "room": 0, "unavailable": None}
    limits = info.get("limits") or {}
    windows = [limits[name] for name in ("primary", "secondary") if limits.get(name)]
    weekly = [window for window in windows if (window.get("minutes") or 0) >= WEEK_MINUTES]
    short = [window["used"] for window in windows if (window.get("minutes") or 0) < WEEK_MINUTES
             and result_schema.is_finite_number(window.get("used"))]
    if info.get("logged_in") is False:
        candidate["unavailable"] = "needs login"
    elif info.get("logged_in") is None:
        candidate["unavailable"] = "limits unknown"
    elif info.get("duplicate"):
        candidate["unavailable"] = "same account as " + info["duplicate"]
    elif info.get("ordinary") is False or limits.get("reached") is not None:
        candidate["unavailable"] = "limited"
    elif not weekly or not result_schema.is_finite_number(weekly[0].get("used")):
        candidate["unavailable"] = "limits unknown"
    else:
        threshold = cfg.get("soft_switch_at", cfg["switch_at"])
        candidate["room"] = round(max(0.0, (threshold - weekly[0]["used"]) * info.get("weight", 1)), 3)
        if max(short, default=0) >= cfg["switch_at"]:
            candidate["unavailable"] = f"five-hour window at {max(short):g}%"
        elif candidate["room"] <= 0:
            candidate["unavailable"] = f"weekly window at {weekly[0]['used']:g}%"
    return candidate


def elect(candidates, current, settings):
    """Choose the duty account without I/O.

    ``candidates`` have family, name, room and unavailable (None or a reason);
    ``current`` is the present duty's account and family, or None; ``settings``
    has relative_margin (percent) and absolute_margin (room units). Any
    available Claude account outranks Codex. Within a family the current duty
    yields only to the largest room, and only when it wins by both margins.
    Equal rooms go to the first name. Returns account, family and reason.
    """
    available = [candidate for candidate in candidates if candidate["unavailable"] is None]
    if not available:
        return {"account": None, "family": None, "reason": "no account available"}
    pool = [candidate for candidate in available if candidate["family"] == "claude"] or available
    best = min(pool, key=lambda candidate: (-candidate["room"], candidate["name"]))

    def decision(candidate, reason):
        return {"account": candidate["name"], "family": candidate["family"], "reason": reason}

    if not current or current.get("account") is None:
        return decision(best, f"largest room {best['room']:g}")
    label = duty_label(current)
    holder = next((candidate for candidate in candidates if candidate["family"] == current.get("family")
                   and candidate["name"] == current["account"]), None)
    if holder is None:
        return decision(best, f"{label} is no longer configured; largest room {best['room']:g}")
    if holder["unavailable"] is not None:
        return decision(best, f"{label} unavailable: {holder['unavailable']}; largest room {best['room']:g}")
    if holder["family"] != best["family"]:
        return decision(best, f"a Claude account is available again; largest room {best['room']:g}")
    if best["room"] == holder["room"]:
        return decision(holder, f"largest room {holder['room']:g}")
    relative = best["room"] >= holder["room"] * (1 + settings["relative_margin"] / 100)
    absolute = best["room"] - holder["room"] >= settings["absolute_margin"]
    comparison = f"{best['name']} room {best['room']:g} vs {holder['room']:g}"
    if relative and absolute:
        return decision(best, comparison + " passes both margins")
    missed = [name for name, passed in (("relative", relative), ("absolute", absolute)) if not passed]
    return decision(holder, f"kept: {comparison} misses the {' and '.join(missed)} margin"
                            + ("s" if len(missed) > 1 else ""))


def run_duty_tick(cfg, infos, refreshed_at):
    """Elect from cached Claude limits and Codex snapshots; record the result.

    The record is rewritten on every tick; the change log gets one line only
    when the duty account changes. Returns (record, changed).
    """
    now = clock.CLOCK.time()
    limits = claude_accounts.read_limits()
    candidates = ([claude_duty_candidate(cfg, account, limits.get(account["name"], {}), now, refreshed_at)
                   for account in claude_accounts.accounts(cfg)]
                  + [codex_duty_candidate(cfg, info) for info in infos])
    rooms = {family: {candidate["name"]: candidate["room"] for candidate in candidates
                      if candidate["family"] == family and candidate["unavailable"] is None}
             for family in DUTY_FAMILIES}
    with duty_lock():
        previous = read_duty(warn=False)
        current = {"account": previous["account"], "family": previous["family"]} if previous else None
        decision = elect(candidates, current, cfg["duty"])
        changed = duty_label(previous) != duty_label(decision)
        record = {"account": decision["account"], "family": decision["family"],
                  "since": previous["since"] if previous and not changed else now,
                  "checked_at": now, "reason": decision["reason"], "rooms": rooms}
        write_duty(record)
        if changed:
            with duty_log_path().open("a", encoding="utf-8") as log:
                log.write(json.dumps({"at": now, "from": duty_label(previous), "to": duty_label(decision),
                                      "reason": decision["reason"], "rooms": rooms}, ensure_ascii=False) + "\n")
    return record, changed


def other_family_available(record):
    """Whether the last tick found a candidate outside the duty account's family."""
    return any(entries for family, entries in record["rooms"].items() if family != record["family"])


def claude_reserved(cfg, role, exclude=(), task_type=None):
    """The Claude duty account as a set when new work must skip it, else an empty set.

    It takes new work only when no other Claude account can, and either the
    task type needs Claude (duty.claude_only_task_types) or the last tick found
    no Codex candidate.
    """
    record = read_duty()
    if record is None or record["family"] != "claude" or record["account"] in exclude:
        return set()
    name = record["account"]
    if claude_accounts.select(cfg, role, set(exclude) | {name}) is not None:
        return {name}
    if task_type in cfg["duty"]["claude_only_task_types"] or not other_family_available(record):
        return set()
    return {name}


def codex_reserved(infos, cfg, exclude=()):
    """The Codex duty account as a set when new work must skip it, else an empty set.

    ``exclude`` names the accounts that cannot take this work; the duty account
    takes it only when no other Codex account can and the last tick found no
    Claude candidate.
    """
    record = read_duty()
    if record is None or record["family"] != "codex" or record["account"] in exclude:
        return set()
    name = record["account"]
    if accounts.available(infos, cfg, set(exclude) | {name}) is not None or other_family_available(record):
        return {name}
    return set()
