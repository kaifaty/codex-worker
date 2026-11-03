"""Work turns, wrap-up turns and turn execution."""
import json

from . import account_rotation, accounts, budgets, checking, claude_accounts, claude_review, claude_runner, clock, config, notifications, prompts, publishing, task_state, telemetry_log, threads, turn_monitor


def claude_work_turn(st, cfg, prompt, effort, stopped, account, wrapup=False):
    """Run one pinned Claude session, sharing the work result and phase gates."""
    directory = config.TASKS / st["slug"]
    last = directory / "last.md"
    last.unlink(missing_ok=True)
    settings = cfg["claude"]
    minutes = 10 if wrapup else st.get("max_turn_minutes", cfg.get("max_turn_minutes"))
    deadline = None if minutes is None else clock.CLOCK.monotonic() + minutes * config.MINUTE_SECONDS
    weekly_before = weekly_after = None
    weekly_spent = 0
    weekly_stopped = False

    def progress(telemetry):
        nonlocal weekly_before, weekly_after, weekly_spent, weekly_stopped
        session_id = telemetry.get("session_id")
        if session_id and session_id != st.get("session_id"):
            st["session_id"] = session_id
            task_state.save(st)
        weekly_before, weekly_after = telemetry.get("weekly_used_before"), telemetry.get("weekly_used_after")
        weekly_spent = (max(0, weekly_after - weekly_before)
                        if weekly_before is not None and weekly_after is not None else 0)
        budget = st.get("max_task_weekly_percent")
        weekly_stopped = (not wrapup and budget is not None
                          and st.get("weekly_used_percent", 0) + weekly_spent >= budget)

    def started(proc):
        st["claude_pid"] = proc.pid
        task_state.save(st)

    try:
        response = claude_runner.run_work_turn(
            account, st["worktree"], directory, prompt, lambda: stopped() or weekly_stopped,
            session_id=st.get("session_id") or None,
            model=st.get("task_model") or settings.get("model"),
            effort=effort or settings.get("effort"),
            deadline=deadline, started=started, progress=progress)
    finally:
        st.pop("claude_pid", None)
        task_state.save(st)
    telemetry = response["telemetry"]
    if telemetry.get("session_id"):
        st["session_id"] = telemetry["session_id"]
    st.update(model=telemetry.get("model"), reasoning_effort=telemetry.get("reasoning_effort"))
    st["weekly_used_percent"] = st.get("weekly_used_percent", 0) + weekly_spent
    phase = telemetry_log.telemetry_phase(st)
    if phase is not None:
        phase.add_claude_usage(telemetry, account=account["name"], weekly_before=weekly_before,
                              weekly_after=weekly_after, weekly_spent=weekly_spent)
    result = response["result"]
    outcome = task_state.stop_outcome(st, stopped) or response["outcome"]
    if weekly_stopped and not stopped():
        outcome = "completed" if result is not None else "budget_exhausted"
    if wrapup:
        st["wrapup"].update(outcome=outcome, result=result if outcome == "completed" else None)
    else:
        st.update(outcome=outcome, result=result)
    if outcome == "completed":
        last.write_text(response["raw_message"] or json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if outcome == "unavailable":
        reason = response.get("reason") or "account unavailable"
        st["error"] = f"No available Claude account: {account['name']} ({reason})"
        if reason in ("usage limit", "authentication failed", "token unavailable"):
            claude_accounts.mark_limited(account["name"], token_unavailable=reason == "token unavailable")
    elif response.get("reason"):
        st["error"] = response["reason"]
    task_state.save(st)
    return outcome


def run_claude_turn(st, cfg, prompt, effort, stopped):
    """Select once; subsequent work turns may use only the task's account."""
    pinned = st.get("claude_account")
    excluded = [account["name"] for account in claude_accounts.accounts(cfg)
                if pinned and account["name"] != pinned]
    account = claude_review.select_account(cfg, "work", exclude=excluded, stopped=stopped)
    if stopped():
        st["outcome"] = task_state.stop_outcome(st, stopped)
        return
    if account is None:
        st.update(outcome="unavailable", result=None,
                  error="No available Claude account" + (f": {pinned}" if pinned else " in the pool"))
        task_state.save(st)
        return
    st["claude_account"] = account["name"]
    st.pop("error", None)
    task_state.save(st)
    outcome = claude_work_turn(st, cfg, prompt, effort, stopped, account)
    if outcome == "budget_exhausted" and not stopped():
        phase = telemetry_log.telemetry_phase(st)
        if phase is not None:
            phase.finish(outcome=outcome)
        reason = ({"outcome": "budget_exhausted", "weekly": st["max_task_weekly_percent"],
                   "spent": st["weekly_used_percent"]} if budgets.weekly_budget_exhausted(st) else
                  {"outcome": "budget_exhausted", "minutes": st.get("max_turn_minutes", cfg.get("max_turn_minutes"))})
        wrapup_turn(st, cfg, account, effort, stopped, reason)
    elif budgets.weekly_budget_exhausted(st):
        if outcome == "completed":
            phase = telemetry_log.telemetry_phase(st)
            if phase is not None:
                phase.finish(outcome=outcome)
            publishing.publish_work(st, stopped)
            publishing.publish_pr(st, stopped)
        budgets.stop_at_weekly_budget(st)
        task_state.save(st)


def wrapup_turn(st, cfg, account, effort, stopped, reason, review_entry=None):
    with telemetry_log.record_phase(st, "wrapup", stopped) as phase:
        _wrapup_turn(st, cfg, account, effort, stopped, reason, review_entry)
        phase.finish(outcome=task_state.stop_outcome(st, stopped) or st["wrapup"]["outcome"])


def _wrapup_turn(st, cfg, account, effort, stopped, reason, review_entry=None):
    """Publish the typed stop and allow exactly one short resume on its thread."""
    st["turn_stop"] = dict(reason, at=clock.CLOCK.time())
    st.setdefault("turn_stops", []).append(dict(st["turn_stop"], turn=st.get("turns", 0)))
    st["outcome"] = reason["outcome"]
    st["review_skipped"] = reason["outcome"]
    st["wrapup"] = {"outcome": "not_run", "result": None}
    task_state.save(st)
    notifications.notify(st, cfg, reason["outcome"], turn_monitor.turn_stop_message(reason), account=account["name"], **reason)
    claude_work = st.get("executor") == "claude" and st.get("kind") not in ("review", "scout")
    thread = ((review_entry or {}).get("thread") if st.get("kind") == "review"
              else st.get("session_id") if claude_work
              else st.get("thread"))
    if not thread or stopped():
        return
    if st.get("kind") not in ("review", "scout"):
        st["result"] = None
    try:
        if claude_work:
            result = claude_work_turn(st, cfg, prompts.wrapup_prompt(st, reason, cfg), effort,
                                      stopped, account, wrapup=True)
        else:
            control = turn_monitor.TurnControl(10, cfg.get("repeat_limit", 3))
            result, _ = turn_monitor.monitor(st, cfg, account, prompts.wrapup_prompt(st, reason, cfg), effort, stopped,
                                lambda *args: None, control=control,
                                wrapup=True, wrapup_thread=thread)
        st["wrapup"]["outcome"] = result
        if not claude_work and result in ("stuck", "budget_exhausted"):
            st["turn_stops"].append(dict(control.reason, turn=st.get("turns", 0), phase="wrapup",
                                         active_seconds=round(control.elapsed, 3), at=clock.CLOCK.time()))
    except (Exception, SystemExit) as exc:
        st["wrapup"].update(outcome="failed", error=str(exc))
    if st["wrapup"]["outcome"] != "completed":
        st["wrapup"]["result"] = None
        last = config.TASKS / st["slug"] / ("review.md" if st.get("kind") == "review" else "last.md")
        last.unlink(missing_ok=True)
    if st.get("kind") not in ("review", "scout"):
        st["result"] = st["wrapup"]["result"]
    elif review_entry is not None and st["wrapup"]["outcome"] == "completed":
        review_entry.update(result=st["wrapup"]["result"], finished=clock.CLOCK.time())
    if review_entry is not None and st["wrapup"]["outcome"] == "refused":
        review_entry.update(outcome="refused", refusal=st["refusal"], result=None, finished=clock.CLOCK.time())
    st["outcome"] = (task_state.stop_outcome(st, stopped) or
                     ("refused" if st["wrapup"]["outcome"] == "refused" else reason["outcome"]))
    task_state.save(st)


def run_turn(st, cfg, prompt, effort, current_message, stopped, sleep, new_turn=True):
    kind = st.get("kind", "work")
    phase = "fix" if kind == "work" and st.get("rounds") else kind
    with telemetry_log.record_phase(st, phase, stopped):
        return _run_turn(st, cfg, prompt, effort, current_message, stopped, sleep, new_turn)


def _run_turn(st, cfg, prompt, effort, current_message, stopped, sleep, new_turn=True):
    """Run one work/scout/review phase, sharing quota, rotation and reset handling."""
    st.pop("refusal", None)
    if st.get("executor") == "claude" and st.get("kind") not in ("review", "scout"):
        return run_claude_turn(st, cfg, prompt, effort, stopped)
    reviewing = st.get("kind") == "review"
    minutes = (cfg.get("max_review_minutes", 30) if st.get("kind") in ("review", "scout")
               else st.get("max_turn_minutes", cfg.get("max_turn_minutes")))
    control = turn_monitor.TurnControl(minutes, cfg.get("repeat_limit", 3), st.get("max_task_weekly_percent"))
    visited = set()
    soft_visited = {}
    rotations = 0
    rotation = None
    soft_rotation = False
    floor_rotation = False
    capacity_retried = False
    resume_thread = None
    if new_turn:
        st.update(auth_excluded=[], auth_excluded_ids=[], auth_transitions=0, auth_failures={})
        st.pop("reset_reconciled", None)
    account = accounts.account_named(cfg, st["account"])
    quota_snapshot = None
    if cfg["enabled"]:
        infos = account_rotation.task_snapshots(st, cfg, stopped, new_turn=new_turn)
        budgets.warn_weekly_headroom(st, cfg, infos)
        current = next((i for i in infos if i["name"] == st["account"]), {})
        same_account = {i["name"] for i in infos if current.get("account_id")
                        and i.get("account_id") == current["account_id"]}
        resuming = bool(st.get("thread")) and not reviewing
        chosen = accounts.available(infos, cfg, same_account if resuming else ())
        if resuming and current.get("logged_in") and not accounts.exhausted(current, cfg):
            if (not accounts.exhausted(current, cfg, soft=True) or not chosen
                    or accounts.exhausted(chosen, cfg, soft=True)):
                chosen = current
            else:
                soft_rotation = True
        floor_reason = None
        if not chosen and cfg["credits"]:
            if current.get("logged_in") and accounts.has_credits(current, cfg):
                chosen = current
            else:
                floor_reason = accounts.credit_floor_reason(current, cfg) if current.get("logged_in") else None
                chosen = accounts.credit_account(infos, cfg)
        if stopped():
            return
        if not chosen:
            chosen = account_rotation.wait_for_account(
                st, infos, cfg, "all accounts unavailable" + (f"; {floor_reason}" if floor_reason else ""),
                stopped, sleep)
            if not chosen:
                return
        if resuming and chosen["name"] != st["account"]:
            prompt = threads.handoff(st, current_message=current_message, cfg=cfg)
            st["rotations"].append({"at": clock.CLOCK.time(), "from": st["account"], "to": chosen["name"],
                                    "reason": floor_reason or account_rotation.limit_reason(current, cfg, soft=soft_rotation),
                                    "thread": ""})
            floor_rotation = floor_reason is not None
            rotation = st["rotations"][-1]
            if not soft_rotation:
                rotations += 1
                visited.add(st["account"])
            else:
                soft_visited[st["account"]] = current["limits"]
            st["thread"] = ""
        account = accounts.account_named(cfg, chosen["name"])
        quota_snapshot = chosen.get("limits")
        if not reviewing:
            st["account"] = account["name"]
    if reviewing:
        st["review_account"] = account["name"]
    task_state.save(st)
    if rotation is not None:
        notifications.notify(st, cfg, "rotated", f"rotated from {rotation['from']} to {rotation['to']}" +
               (f": {rotation['reason']}" if soft_rotation or floor_rotation else ""),
               **{"from": rotation["from"], "to": rotation["to"], "account": account["name"]})

    def on_limit(local, reason, allow_credits, cancelled):
        infos = account_rotation.task_snapshots(st, cfg, cancelled)
        current = next((i for i in infos if i["name"] == account["name"]), {})
        if local and accounts.exhausted(local, cfg):
            # The RPC may lag the local rollout; keep its fresh credit balance.
            credits = current.get("limits", {}).get("credits", {})
            current["limits"] = dict(local["limits"], credits=credits)
        soft = allow_credits and local and not accounts.exhausted(local, cfg)
        if soft:
            for info in infos:
                previous = soft_visited.get(info["name"], {})
                windows = [(window, info.get("limits", {}).get(name, {}))
                           for name, window in previous.items() if name in ("primary", "secondary")
                           and window.get("used", 0) >= cfg.get("soft_switch_at", cfg["switch_at"])]
                if windows and all(before.get("reset") is not None and after.get("reset") is not None
                                   and after["reset"] > before["reset"] for before, after in windows):
                    del soft_visited[info["name"]]
        used = visited | set(soft_visited) if soft else visited
        used_ids = {i.get("account_id") for i in infos if i["name"] in used and i.get("account_id")}
        excluded = used | {i["name"] for i in infos if i.get("account_id") in used_ids}
        if soft:
            chosen = accounts.available(infos, cfg, excluded)
            if chosen and not accounts.exhausted(chosen, cfg, soft=True):
                # Keep the local windows until RPC confirms their reset, even if RPC usage lags.
                soft_visited[account["name"]] = local["limits"]
                return {"reason": reason, "infos": infos, "chosen": chosen, "soft": True}
            return None
        current["ordinary"] = False
        chosen = accounts.available(infos, cfg, excluded) if rotations < len(cfg["accounts"]) else None
        if allow_credits and not chosen and cfg["credits"] and current.get("logged_in"):
            # Each limit sample re-reads the balance, so a live exec on credits stops at the floor.
            if accounts.has_credits(current, cfg):
                return None
            if rotations < len(cfg["accounts"]):
                chosen = accounts.credit_account(infos, cfg, excluded)
            floor_reason = accounts.credit_floor_reason(current, cfg)
            if floor_reason:
                return {"reason": floor_reason, "infos": infos, "chosen": chosen, "floor": True}
        if not allow_credits and not chosen and cfg["credits"]:
            # Codex refused this exec; only another unvisited account may spend credits.
            if rotations < len(cfg["accounts"]):
                chosen = accounts.credit_account(infos, cfg, excluded)
            if not chosen:
                for info in infos:
                    if info["name"] in excluded or not info.get("logged_in") or info.get("duplicate"):
                        continue
                    floor_reason = accounts.credit_floor_reason(info, cfg)
                    if floor_reason:
                        reason += f"; {floor_reason}"
                        break
        return {"reason": reason, "infos": infos, "chosen": chosen}

    while not stopped():
        st.pop("reset_reconciled", None)
        visited.add(account["name"])
        review_start = len(st.get("reviews", []))
        attempt_prompt = prompts.capacity_resume_prompt(cfg) if resume_thread else prompt
        result, decision = turn_monitor.monitor(st, cfg, account, attempt_prompt, effort, stopped, on_limit, control=control,
                                   quota_snapshot=quota_snapshot, resume_thread=resume_thread)
        quota_snapshot = None
        resume_thread = None
        phase = telemetry_log.telemetry_phase(st)
        weekly_exhausted = (result in ("auth", "limit", "capacity", "failed", "completed")
                            and control.weekly_exhausted(st.get("weekly_used_percent", 0)))
        if weekly_exhausted and result != "completed":
            result = "budget_exhausted"
        if result in ("auth", "limit", "capacity") and control.expired(clock.CLOCK.monotonic()):
            result = "budget_exhausted"
        if result != "auth":
            st.setdefault("auth_failures", {})[account["name"]] = 0
        if stopped():
            st["outcome"] = task_state.stop_outcome(st, stopped)
            break
        if weekly_exhausted and result == "completed":
            # The model has finished: keep its result and account the phase once,
            # publish completed workspace work, then record the budget stop.
            st["outcome"] = result
            if phase is not None:
                phase.finish(outcome=result)
            publishing.publish_work(st, stopped)
            publishing.publish_pr(st, stopped)
            budgets.stop_at_weekly_budget(st)
            break
        if result in ("budget_exhausted", "stuck"):
            reason = dict(control.reason, active_seconds=round(control.elapsed, 3))
            reviews = st.get("reviews", [])[review_start:]
            phase = telemetry_log.telemetry_phase(st)
            if phase is not None:
                phase.finish(outcome=result)
            wrapup_turn(st, cfg, account, effort, stopped, reason,
                        review_entry=reviews[-1] if reviews else None)
            break
        if result == "capacity":
            if capacity_retried:
                st["outcome"] = "failed"
                break
            capacity_retried = True
            st["capacity_retries"] = st.get("capacity_retries", 0) + 1
            task_state.save(st)
            sleep_started = clock.CLOCK.monotonic()
            try:
                until = sleep_started + config.CAPACITY_RETRY_SECONDS
                while not stopped():
                    left = until - clock.CLOCK.monotonic()
                    if left <= 0:
                        break
                    sleep(min(config.SLEEP_STEP, left))
            finally:
                if phase is not None:
                    phase.sleep_seconds += max(0, clock.CLOCK.monotonic() - sleep_started)
            if stopped():
                st["outcome"] = task_state.stop_outcome(st, stopped)
                break
            reviews = st.get("reviews", [])[review_start:]
            thread = (reviews[-1]["thread"] if reviews else None) if reviewing else st.get("thread")
            if thread:
                resume_thread = thread
            continue
        if result in ("completed", "failed", "refused") or not cfg["enabled"]:
            st["outcome"] = result
            break
        if result == "auth":
            failures = st.setdefault("auth_failures", {})
            failures[account["name"]] = failures.get(account["name"], 0) + 1
            infos = account_rotation.task_snapshots(st, cfg, stopped)
            if stopped():
                break
            current = next((i for i in infos if i["name"] == account["name"]), {})
            capped = st["auth_transitions"] >= 2 * len(cfg["accounts"])
            if not capped and failures[account["name"]] == 1 and current.get("logged_in") and not accounts.exhausted(current, cfg):
                quota_snapshot = current.get("limits")
                if not reviewing and st.get("thread"):
                    prompt = prompts.auth_resume_prompt(cfg)
                task_state.save(st)
                continue
            if failures[account["name"]] >= 2 or current.get("logged_in") is False:
                account_rotation.exclude_auth_account(st, account, current)
                infos = account_rotation.refresh_login_state(st, infos)
            if not infos or capped:
                names = st.get("needs_login", []) or [a["name"] for a in cfg["accounts"]]
                st.update(outcome="failed", error="all accounts fail with auth errors; " +
                          "; ".join("run login " + name for name in names))
                task_state.save(st)
                break
            decision = {"reason": "authentication error", "infos": infos,
                        "chosen": accounts.available(infos, cfg, {account["name"]})}
        chosen = decision["chosen"]
        if not chosen:
            chosen = account_rotation.wait_for_account(st, decision["infos"], cfg, decision["reason"], stopped, sleep)
            if not chosen:
                break
            # Fresh quota snapshots start a new rotation cycle, including past accounts.
            visited.clear()
            soft_visited.clear()
            rotations = 0
        quota_snapshot = chosen.get("limits")
        if chosen["name"] == account["name"]:
            if not reviewing and st.get("thread"):
                prompt = prompts.reset_resume_prompt(cfg)
            task_state.save(st)
            continue
        if not reviewing:
            prompt = threads.handoff(st, current_message=current_message, cfg=cfg)
        st["rotations"].append({"at": clock.CLOCK.time(), "from": account["name"], "to": chosen["name"],
                                "reason": decision["reason"], "thread": ""})
        rotation = st["rotations"][-1]
        if result == "auth":
            st["auth_transitions"] += 1
        account = accounts.account_named(cfg, chosen["name"])
        if reviewing:
            st["review_account"] = account["name"]
        else:
            st.update(account=account["name"], thread="")
        soft_rotation = decision.get("soft", False)
        floor_rotation = decision.get("floor", False)
        if soft_rotation:
            # Its reserve remains usable after the other accounts reach the soft threshold.
            visited.discard(rotation["from"])
        else:
            rotations += 1
        task_state.save(st)
        notifications.notify(st, cfg, "rotated", f"rotated from {rotation['from']} to {rotation['to']}" +
               (f": {rotation['reason']}" if soft_rotation or floor_rotation else ""),
               **{"from": rotation["from"], "to": rotation["to"], "account": account["name"]})


def work_turn(st, cfg, prompt, effort, current_message, stopped, sleep, work_paths=None, **turn_options):
    if work_paths is None and st.get("kind") != "scout":
        work_paths = checking.check_paths(st)
    run_turn(st, cfg, prompt, effort, current_message, stopped, sleep, **turn_options)
    publishing.publish_work(st, stopped)
    publishing.publish_pr(st, stopped)
    if work_paths is not None:
        st["check_paths"] = sorted(set(work_paths) | set(checking.check_paths(st)))
