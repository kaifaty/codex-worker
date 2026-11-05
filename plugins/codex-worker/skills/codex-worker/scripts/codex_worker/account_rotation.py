"""Account errors, reset waits and login refresh."""
import json
import re
import time

from . import accounts, claude_accounts, claude_review, clock, config, credit_resets, duty, notifications, result_schema, task_state, telemetry_log


def limit_reason(info, cfg, soft=False):
    limits = info.get("limits", {})
    threshold = cfg.get("soft_switch_at", cfg["switch_at"]) if soft else cfg["switch_at"]
    for name in ("primary", "secondary"):
        w = limits.get(name, {})
        if w.get("used", 0) >= threshold:
            label = "weekly" if w.get("minutes", 0) >= 10080 else name
            if soft:
                return f"{label} usage {w['used']:g}% reached soft_switch_at {threshold:g}%"
            return f"{label} limit {w['used']:g}%"
    return limits.get("reached") or "usage limit"


def limit_error(event):
    if event.get("type") not in ("turn.failed", "error"):
        return False
    text = json.dumps(event, ensure_ascii=False).lower()
    return "usage limit" in text or any(s in text for s in (
        "rate_limit_reached", "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached",
        "workspace_owner_credits_depleted", "workspace_member_credits_depleted"))


def auth_error(event):
    if event.get("type") not in ("turn.failed", "error"):
        return False
    text = json.dumps(event, ensure_ascii=False).lower()
    return bool(re.search(r"\b401\b", text)) or any(part in text for part in (
        "unauthorized", "access token could not be refreshed", "authentication session could not be refreshed",
        "oauth refresh token was rejected", "failed to refresh token"))


def capacity_error(event):
    return (event.get("type") in ("turn.failed", "error")
            and "at capacity" in json.dumps(event, ensure_ascii=False).lower())


def refusal_message(event):
    """Policy refusals are terminal model outcomes, not account failures."""
    if event.get("type") != "turn.failed":
        return None
    error = event.get("error")
    message = (error.get("message") if isinstance(error, dict) else error) or event.get("message")
    if isinstance(message, str) and "cybersecurity risk" in message.lower():
        return message
    return None


def earliest_reset(infos, cfg):
    resets = [(accounts.reset_time(i, cfg), i["name"]) for i in infos if i.get("logged_in") and not i.get("duplicate")]
    resets = [(stamp, name) for stamp, name in resets if stamp is not None]
    return min(resets) if resets else (None, None)


def record_exhausted(st, infos, cfg, reason):
    stamp, name = earliest_reset(infos, cfg)
    st.pop("sleeping", None)
    st["exhausted"] = {"at": clock.CLOCK.time(), "reset": stamp, "account": name, "reason": reason}
    st["outcome"] = "exhausted"
    task_state.save(st)
    notifications.notify(st, cfg, "paused", f"paused: {reason}", until=stamp)


def try_reset_credit(st, infos, cfg, stopped):
    """Return fresh quotas after a reset or concurrent quota recovery; never retry here."""
    stamp, _ = earliest_reset(infos, cfg)
    if (not cfg.get("use_reset_credits", False) or stamp is None
            or stamp - clock.CLOCK.time() <= cfg.get("reset_credit_min_hours", 12) * 3600
            or accounts.available(infos, cfg)):
        return None
    # Never spend a reset on a duty account that this task may not take.
    own = st.get("kind") != "review" and st.get("thread")
    reserved = duty.codex_reserved(infos, cfg, {st["account"]} if own else ())
    eligible = [i for i in infos if i.get("logged_in") and not i.get("duplicate") and i["name"] not in reserved]
    with credit_resets.reset_journal(stopped) as (entries, _):
        pending = [(entry.get("credit") or {"id": entry["creditId"]}, info)
                   for info in eligible for entry in credit_resets.pending_resets(entries, info)]
    choices = pending or [(credit, info) for info in eligible
               for credit in credit_resets.reset_credits(info)]
    if not choices or stopped():
        return None
    credit, info = min(choices, key=lambda pair: pair[0].get("expiresAt")
                       if pair[0].get("expiresAt") is not None else float("inf"))
    event = {"event": "reset_credit", "at": clock.CLOCK.time(), "account": info["name"],
             "credit_id": credit["id"], "expires_at": credit.get("expiresAt")}
    try:
        # A concurrent unknown attempt takes priority over the candidate selected above.
        action = credit_resets.reset_credit_action(info, credit["id"], st["slug"], stopped, confirmed=True,
                                     quota_cfg=cfg, credit_id_is_hint=True)
        if action["outcome"] == "available":
            return [action["info"] if i["name"] == info["name"] else i for i in infos]
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        event.update(outcome="error", error=str(exc))
    else:
        event.update(outcome=action["outcome"], credit_id=action["credit"]["id"],
                     expires_at=action["credit"].get("expiresAt"))
    st["rotations"].append(event)
    task_state.save(st)
    if event["outcome"] in ("success", "consumed (inferred)") and not stopped():
        notifications.notify(st, cfg, "reset_credit_used", f"used a free limit reset on {info['name']}",
               account=info["name"])
        return task_snapshots(st, cfg, stopped)
    return None


def refresh_login_state(st, infos, new_turn=False):
    """Only a fresh probe at the beginning of a turn can clear a login flag."""
    names = list(st.get("needs_login", []))
    identities = st.setdefault("account_ids", {})
    for info in infos:
        if info.get("account_id"):
            identities[info["name"]] = info["account_id"]
        if new_turn and info.get("logged_in") is True and info["name"] in names:
            names.remove(info["name"])
        elif info.get("logged_in") is False and info["name"] not in names:
            names.append(info["name"])
            st["rotations"].append({"event": "needs_login", "at": clock.CLOCK.time(), "account": info["name"]})
            notifications.notify(st, None, "needs_login", f"{info['name']} needs login", account=info["name"])
    if names != st.get("needs_login", []):
        st["needs_login"] = names
        task_state.save(st)
    return [i for i in infos if i["name"] not in st.get("auth_excluded", [])
            and i.get("duplicate") not in st.get("auth_excluded", [])
            and (i.get("account_id") or identities.get(i["name"])) not in st.get("auth_excluded_ids", [])]


def exclude_auth_account(st, account, info):
    name = account["name"]
    if name not in st["auth_excluded"]:
        st["auth_excluded"].append(name)
    ident = info.get("account_id") or st.get("account_ids", {}).get(name)
    if ident and ident not in st["auth_excluded_ids"]:
        st["auth_excluded_ids"].append(ident)
    names = st.setdefault("needs_login", [])
    if name not in names:
        names.append(name)
        st["rotations"].append({"event": "needs_login", "at": clock.CLOCK.time(), "account": name})
        notifications.notify(st, None, "needs_login", f"{name} needs login", account=name)
    task_state.save(st)


def task_snapshots(st, cfg, stopped, new_turn=False):
    infos = accounts.snapshots(cfg, stopped, prepare=True)
    if cfg.get("use_reset_credits", False):
        for info in infos:
            if (stopped() or not info.get("logged_in") or info.get("duplicate")
                    or info["name"] in st.get("auth_excluded", [])
                    or info.get("account_id") in st.get("auth_excluded_ids", [])):
                continue
            action = credit_resets.reset_credit_action(info, slug=st["slug"], cancelled=stopped,
                                         confirmed=True, pending_only=True, allow_consume=False)
            if action is None:
                continue
            info.update(action["info"])
            if action["outcome"] == "consumed (inferred)":
                st["reset_reconciled"] = True
                credit = action["credit"]
                st["rotations"].append({"event": "reset_credit", "at": clock.CLOCK.time(), "account": info["name"],
                                        "credit_id": credit["id"], "expires_at": credit.get("expiresAt"),
                                        "outcome": action["outcome"]})
                task_state.save(st)
                notifications.notify(st, cfg, "reset_credit_used", f"used a free limit reset on {info['name']}",
                       account=info["name"])
    return refresh_login_state(st, infos, new_turn)


def no_logged_in_accounts(st, infos):
    if any(i.get("logged_in") is not False for i in infos):
        return False
    names = st.get("needs_login", []) or [i["name"] for i in infos]
    st.update(outcome="failed", error="No usable logged-in accounts remain; " +
              "; ".join("run login " + name for name in names))
    st.pop("sleeping", None)
    task_state.save(st)
    return True


def wait_for_account(st, infos, cfg, reason, stopped, sleep=None):
    """Wait without an exec, re-reading quotas after each wall-clock deadline."""
    infos = refresh_login_state(st, infos)
    if no_logged_in_accounts(st, infos):
        return None
    sleep = sleep or clock.CLOCK.sleep
    probe_retries = 0
    wait_started = clock.CLOCK.time()
    already_waited = st.get("reset_wait_seconds", 0)
    deadline = wait_started + config.MAX_RESET_WAIT_SECONDS - already_waited

    def cancelled():
        return stopped() or clock.CLOCK.time() >= deadline

    def update_waited():
        st["reset_wait_seconds"] = already_waited + max(0, clock.CLOCK.time() - wait_started)

    def choose(infos):
        current = next((i for i in infos if i["name"] == st["account"]), {})
        own = st.get("kind") != "review" and st.get("thread")
        if own and current.get("logged_in") and not accounts.exhausted(current, cfg):
            return current
        # New work skips the duty account; the task's own thread may spend its credits.
        reserved = duty.codex_reserved(infos, cfg)
        chosen = accounts.available(infos, cfg, reserved)
        if not chosen and cfg.get("credits"):
            chosen = (current if current.get("logged_in") and accounts.has_credits(current, cfg)
                      and (own or current.get("name") not in reserved)
                      else accounts.credit_account(infos, cfg, reserved))
        return chosen

    while not stopped():
        update_waited()
        remaining = deadline - clock.CLOCK.time()
        if remaining <= 0:
            record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        unknown = [i for i in infos if i.get("logged_in") is None]
        probing = bool(unknown) and probe_retries < config.MAX_ACCOUNT_RETRIES
        if unknown and not probing and not any(i.get("logged_in") is True for i in infos):
            errors = "; ".join(f"{i['name']}: {i.get('error', 'RPC unavailable')}" for i in unknown)
            st.update(outcome="failed", error=f"Account status is unknown after {config.MAX_ACCOUNT_RETRIES} retries: {errors}")
            st.pop("sleeping", None)
            task_state.save(st)
            return None
        if not cfg["wait_for_reset"] and not probing:
            record_exhausted(st, infos, cfg, reason)
            return None
        # At most one consume attempt before this sleep, even if the result is unknown
        # or the refreshed quota still says exhausted.
        reconciled = st.pop("reset_reconciled", False)
        refreshed = None if unknown or reconciled else try_reset_credit(st, infos, cfg, cancelled)
        st.pop("reset_reconciled", None)
        if refreshed is not None:
            infos = refresh_login_state(st, refreshed)
        update_waited()
        if cancelled():
            if not stopped():
                record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        if refreshed is not None:
            chosen = choose(infos)
            if chosen:
                return chosen
            unknown = [i for i in infos if i.get("logged_in") is None]
            probing = bool(unknown) and probe_retries < config.MAX_ACCOUNT_RETRIES
        if no_logged_in_accounts(st, infos):
            return None
        started = clock.CLOCK.time()
        # Do not wake for a duty account that this task may not take.
        own = st.get("kind") != "review" and st.get("thread")
        skipped = duty.codex_reserved(infos, cfg, {st["account"]} if own else ())
        stamp, name = earliest_reset([i for i in infos if i["name"] not in skipped], cfg)
        # A delayed reset may still have its old timestamp. Avoid a tight RPC loop.
        until = stamp + config.RESET_MARGIN if stamp is not None else started + config.RESET_RETRY_SECONDS
        if until <= started:
            until = started + config.SLEEP_STEP
        if cfg.get("credits") and cfg.get("credits_floor_until"):
            floor_account = next((i for i in infos if i.get("logged_in") and not i.get("duplicate")
                                  and accounts.has_credits(i) and not accounts.has_credits(i, cfg)), None)
            if floor_account:
                end = time.strptime(cfg["credits_floor_until"], "%Y-%m-%d")
                floor_end = time.mktime((end.tm_year, end.tm_mon, end.tm_mday + 1, 0, 0, 0, 0, 0, -1))
                if floor_end < until:
                    until, name = floor_end, floor_account["name"]
        if probing:
            until = min(until, started + config.ACCOUNT_RETRY_SECONDS) if stamp is not None else started + config.ACCOUNT_RETRY_SECONDS
        until = min(until, deadline)
        sleeping = {"until": until, "account": name, "reason": reason}
        if probing:
            sleeping.update(probe=True, probe_attempt=probe_retries + 1, reason="account status unknown (RPC error)")
        st.update(outcome="sleeping", sleeping=sleeping, exhausted=None)
        st["rotations"].append(dict(sleeping, at=started, event="sleep"))
        task_state.save(st)
        notifications.notify(st, cfg, "paused", f"paused until {config.local_time(until)}: {sleeping['reason']}", until=until)
        phase = telemetry_log.telemetry_phase(st)
        sleep_started = clock.CLOCK.monotonic() if phase is not None else None
        try:
            while not stopped():
                left = until - clock.CLOCK.time()
                if left <= 0:
                    break
                sleep(min(config.SLEEP_STEP, left))
        finally:
            if phase is not None:
                phase.sleep_seconds += max(0, clock.CLOCK.monotonic() - sleep_started)
            update_waited()
        if stopped():
            return None
        if st["reset_wait_seconds"] >= config.MAX_RESET_WAIT_SECONDS:
            record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        st.pop("sleeping", None)
        st["outcome"] = "running"
        st["rotations"].append(dict(sleeping, at=clock.CLOCK.time(), event="wake"))
        task_state.save(st)
        notifications.notify(st, cfg, "woke", "woke to recheck account limits")
        infos = task_snapshots(st, cfg, cancelled)
        if probing:
            probe_retries += 1
        if not any(i.get("logged_in") is None for i in infos):
            probe_retries = 0
        update_waited()
        if stopped():
            return None
        if st["reset_wait_seconds"] >= config.MAX_RESET_WAIT_SECONDS:
            record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        if no_logged_in_accounts(st, infos):
            return None
        chosen = choose(infos)
        if chosen:
            return chosen
    return None


def claude_reset(cfg, skip=()):
    """Earliest future limit end or window reset in the Claude pool, without the accounts in skip."""
    limits, now, resets = claude_accounts.read_limits(), clock.CLOCK.time(), []
    for account in claude_accounts.accounts(cfg):
        if account["name"] in skip:
            continue
        info = limits.get(account["name"], {})
        windows = info.get("unifiedWindows") or {}
        # A limited account returns at its known limit end, whatever its windows say.
        stamps = ([info.get("limited_until", info.get("resetsAt"))]
                  if claude_accounts.limited(info, now) else [])
        if not any(result_schema.is_finite_number(stamp) for stamp in stamps):
            stamps = [window.get("resetsAt") for window in windows.values() if isinstance(window, dict)]
        resets += [(stamp, account["name"]) for stamp in stamps
                   if result_schema.is_finite_number(stamp) and stamp > now]
    return min(resets) if resets else (None, None)


def claude_choice(st, cfg, stopped, exclude=()):
    """Prefer the task's account and session, then any other pool account."""
    current = st.get("claude_account")
    others = [account["name"] for account in claude_accounts.accounts(cfg) if account["name"] != current]
    if current and current not in exclude:
        # The task's own account is not new work, even on duty.
        claude_review.refresh_limits(cfg, stopped)
        chosen = claude_accounts.select(cfg, "work", exclude=others)
        if chosen is not None or stopped():
            return chosen
    return claude_review.select_account(cfg, "work", exclude=set(exclude) | {current}, stopped=stopped,
                                        task_type=st.get("task_type"))


def wait_for_claude_account(st, cfg, reason, stopped, sleep=None):
    """Sleep until the Claude pool's earliest reset, without credits or reset credits."""
    sleep = sleep or clock.CLOCK.sleep
    wait_started = clock.CLOCK.time()
    already_waited = st.get("reset_wait_seconds", 0)
    deadline = wait_started + config.MAX_RESET_WAIT_SECONDS - already_waited

    def update_waited():
        st["reset_wait_seconds"] = already_waited + max(0, clock.CLOCK.time() - wait_started)

    def reserved():
        # Do not wake for a duty account that this task may not take.
        current = st.get("claude_account")
        return duty.claude_reserved(cfg, "work", {current} if current else (), st.get("task_type"), stopped)

    def exhausted(why):
        stamp, name = claude_reset(cfg, reserved())
        st.pop("sleeping", None)
        st.update(outcome="exhausted", exhausted={"at": clock.CLOCK.time(), "reset": stamp,
                                                  "account": name, "reason": why})
        task_state.save(st)
        notifications.notify(st, cfg, "paused", f"paused: {why}", until=stamp)

    while not stopped():
        update_waited()
        started = clock.CLOCK.time()
        if started >= deadline:
            exhausted(reason + "; reset wait exceeded 8 days")
            return None
        if not cfg["wait_for_reset"]:
            exhausted(reason)
            return None
        stamp, name = claude_reset(cfg, reserved())
        # A delayed reset may still have its old timestamp. Avoid a tight probe loop.
        until = stamp + config.RESET_MARGIN if stamp is not None else started + config.RESET_RETRY_SECONDS
        if until <= started:
            until = started + config.SLEEP_STEP
        until = min(until, deadline)
        sleeping = {"until": until, "account": name, "reason": reason}
        st.update(outcome="sleeping", sleeping=sleeping, exhausted=None)
        st["rotations"].append(dict(sleeping, at=started, event="sleep"))
        task_state.save(st)
        notifications.notify(st, cfg, "paused", f"paused until {config.local_time(until)}: {reason}", until=until)
        phase = telemetry_log.telemetry_phase(st)
        sleep_started = clock.CLOCK.monotonic()
        try:
            while not stopped():
                left = until - clock.CLOCK.time()
                if left <= 0:
                    break
                sleep(min(config.SLEEP_STEP, left))
        finally:
            if phase is not None:
                phase.sleep_seconds += max(0, clock.CLOCK.monotonic() - sleep_started)
            update_waited()
        if stopped():
            return None
        if st["reset_wait_seconds"] >= config.MAX_RESET_WAIT_SECONDS:
            exhausted(reason + "; reset wait exceeded 8 days")
            return None
        st.pop("sleeping", None)
        st["outcome"] = "running"
        st["rotations"].append(dict(sleeping, at=clock.CLOCK.time(), event="wake"))
        task_state.save(st)
        notifications.notify(st, cfg, "woke", "woke to recheck account limits")
        chosen = claude_choice(st, cfg, stopped)
        if chosen is not None and not stopped():
            return chosen
    return None
