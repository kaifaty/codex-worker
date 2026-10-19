"""Weekly quota accounting and active time budgets."""
import math
from typing import NamedTuple, Optional

from . import accounts, clock, procs


def budget_stop(st):
    reason = st.get("turn_stop") or {}
    if reason.get("outcome") == "budget_exhausted":
        return "weekly" if "weekly" in reason else "minutes"
    return None


def weekly_used(limits):
    window = accounts.longest_window({"limits": limits})
    value = window.get("used")
    if ((window.get("minutes") or 0) >= 10080 and type(value) in (int, float)
            and math.isfinite(value) and 0 <= value <= 100):
        return value
    return None


def record_weekly_usage(st, account, limits, seed=False):
    """Advance an account's weekly high-water mark; exec seeds are free."""
    used = weekly_used(limits)
    if used is None:
        return 0
    reset = accounts.longest_window({"limits": limits}).get("reset")
    if type(reset) not in (int, float) or not math.isfinite(reset):
        reset = None
    marks = st.setdefault("weekly_marks", {})
    mark = marks.get(account)
    if mark is not None and reset is not None and mark["reset"] is not None:
        # Re-reading a rollout must not replay samples from an older window.
        if reset < mark["reset"] - 3600:
            return 0
        if reset > mark["reset"] + 3600:
            mark = None
    if mark is None:
        marks[account] = {"reset": reset, "used": used}
        return 0
    if mark["reset"] is None:
        mark["reset"] = reset
    delta = 0 if seed else max(0, used - mark["used"])
    mark["used"] = max(used, mark["used"])
    st["weekly_used_percent"] = st.get("weekly_used_percent", 0) + delta
    return delta


def weekly_budget_exhausted(st):
    budget = st.get("max_task_weekly_percent")
    return budget is not None and st.get("weekly_used_percent", 0) >= budget


def stop_at_weekly_budget(st):
    """Do not admit another automatic phase once the completed phases used the budget."""
    if not weekly_budget_exhausted(st):
        return False
    st.update(outcome="budget_exhausted", review_skipped="budget")
    if budget_stop(st) != "weekly":
        st["turn_stop"] = {"outcome": "budget_exhausted", "weekly": st["max_task_weekly_percent"],
                           "spent": st["weekly_used_percent"], "at": clock.CLOCK.time()}
    if st.get("fix_rounds"):
        st["auto_fix_stop"] = "budget"
    return True


def set_weekly_budget(st, value, cfg):
    budget = (value if value is not None else
              st.get("max_task_weekly_percent", cfg.get("max_task_weekly_percent")))
    spent = st.get("weekly_used_percent", 0)
    if budget is not None and spent >= budget:
        procs.die(f"weekly budget {budget:g}% exhausted (spent {spent:g}%); "
            f"pass --max-weekly with a number greater than {spent:g}")
    st["max_task_weekly_percent"] = budget
    st.pop("weekly_budget_warning", None)


def warn_weekly_headroom(st, cfg, infos):
    """Use the quota snapshots already read for account selection, never another RPC."""
    st.pop("weekly_budget_warning", None)
    budget = st.get("max_task_weekly_percent")
    if budget is None:
        return
    reserve = sum(cfg["switch_at"] - used for info in infos
                  if info.get("logged_in") and not info.get("duplicate")
                  for used in [weekly_used(info.get("limits", {}))] if used is not None)
    if budget - st.get("weekly_used_percent", 0) > reserve:
        if cfg.get("language", "en") == "ru":
            warning = (f"бюджет {budget:g}%, запас до switch_at по аккаунтам {reserve:g}%: "
                       "задача может уснуть до сброса")
        else:
            warning = (f"budget {budget:g}%, account headroom to switch_at {reserve:g}%: "
                       "task may sleep until reset")
        st["weekly_budget_warning"] = warning


class MonitorTimeUsage(NamedTuple):
    elapsed: float
    reason: Optional[dict]


def monitor_time_usage(elapsed, seconds, minutes, started, now) -> MonitorTimeUsage:
    """Calculate active time and a deadline reason from one clock sample."""
    reason = ({"outcome": "budget_exhausted", "minutes": minutes}
              if seconds is not None and elapsed + now - started >= seconds else None)
    return MonitorTimeUsage(elapsed + max(0, now - started), reason)


class MonitorWeeklyUsage(NamedTuple):
    updates: dict
    seeded: bool
    delta: float


def monitor_weekly_usage(st, account, limits, seeded) -> MonitorWeeklyUsage:
    """Return weekly state changes without mutating the supplied task state."""
    sample = {key: st[key] for key in ("weekly_marks", "weekly_used_percent") if key in st}
    if "weekly_marks" in sample:
        sample["weekly_marks"] = {name: dict(mark) for name, mark in sample["weekly_marks"].items()}
    delta = record_weekly_usage(sample, account, limits, seed=not seeded)
    seeded = seeded or weekly_used(limits) is not None
    updates = {key: value for key, value in sample.items() if key not in st or value != st[key]}
    return MonitorWeeklyUsage(updates, seeded, delta)
