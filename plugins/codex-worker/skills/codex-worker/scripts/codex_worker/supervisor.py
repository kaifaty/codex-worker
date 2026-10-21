"""Detached task supervision and spawning."""
import os
import signal
import subprocess
import sys
import threading
import time

from . import budgets, checking, ci_runs, clock, config, notifications, prompts, review_cycle, task_state, telemetry_log, turns


def spawn(st, prompt, effort=None, current_message=False, kind="work", review_after=False, fix_rounds=0):
    d = config.TASKS / st["slug"]
    for name in (("review.md",) if kind == "review" else ("last.md", "review.md")):
        last = d / name
        if last.exists():
            last.unlink()
    turn = {"pending": {"prompt": prompt, "effort": effort, "current_message": current_message,
                        "review_after": review_after}, "kind": kind, "fix_rounds": fix_rounds}
    st.update({key: field.spawn_reset(st, turn) for key, field in task_state.STATE_FIELDS.items()
               if field.spawn_reset is not None and field.spawn_reset is not task_state.STATE_DELETE})
    for key, field in task_state.STATE_FIELDS.items():
        if field.spawn_reset is task_state.STATE_DELETE:
            st.pop(key, None)
    if kind != "review":
        # Standalone review preserves the last work result and publication receipt.
        st.pop("publish", None)
        st["result"] = None
    with (d / "stderr.log").open("a") as errors:
        proc = subprocess.Popen([sys.executable, "-I", str(config.SCRIPT_RESOLVED), "_run", st["slug"]],
                                cwd=st["worktree"], stdin=subprocess.DEVNULL, stdout=errors, stderr=errors,
                                start_new_session=True)
    st["pid"] = proc.pid
    task_state.save(st)
    return proc


        # No automatic review after a CI repair. Only the saved check and CI gate it.


def cmd__run(a):
    stopped_flag = threading.Event()
    received_signal = None
    finalized = False
    st = None
    def stop(signum, frame):
        nonlocal received_signal
        at = clock.CLOCK.time()
        name = signal.Signals(signum).name
        if not finalized and received_signal is None:
            received_signal = {"signum": signum, "name": name, "at": at}
        # Only ordinary checkpoints may update state: the handler can interrupt
        # json.dumps while it is iterating this dictionary.
        line = (f"supervisor signal: signum={signum} name={name} "
                f"at={time.strftime('%Y-%m-%d %H:%M:%S%z', time.localtime(at))} "
                f"ppid={os.getppid()} pgid={os.getpgrp()}\n")
        # A separate unbuffered append avoids re-entering Python's stderr buffer
        # when a signal interrupts another log write. No sender inference.
        try:
            with (config.TASKS / a.slug / "stderr.log").open("ab", buffering=0) as log:
                log.write(line.encode("utf-8"))
        finally:
            if not finalized:
                stopped_flag.set()
    def stopped():
        notifications.reap_notifications()
        # Snapshot the flag before copying metadata: True must never be
        # returned for a signal that arrived after the metadata check.
        cancelled = stopped_flag.is_set()
        if received_signal is not None and st is not None:
            st.setdefault("signal", received_signal)
        return cancelled
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    # Preserve interruptible real waits; file-clock waits advance logical time.
    sleep = stopped_flag.wait if clock.CLOCK.path is None else clock.CLOCK.sleep
    # The launcher publishes pid and pending before the supervisor becomes the only writer.
    for _ in range(200):
        st = task_state.load(a.slug)
        if st.get("pid") == os.getpid():
            break
        time.sleep(0.01)
    else:
        return
    cfg = None
    check = None
    try:
        cfg = config.accounts_config()
        pending = st.pop("pending")
        effort = pending["effort"]
        working = st.get("kind") not in ("review", "scout")
        if st.get("kind") == "review":
            review_cycle.review_turn(st, cfg, pending["prompt"], effort, stopped, sleep, new_turn=True)
        else:
            turns.work_turn(st, cfg, pending["prompt"], effort, pending.get("current_message", False),
                      stopped, sleep)
            if working:
                task_state.save(st)
        work_result = st.get("result") if st.get("kind") not in ("review", "scout") else None
        if (work_result is not None and work_result["status"] != "done"
                and pending.get("review_after") and st.get("outcome") == "completed"):
            st["review_skipped"] = "result " + work_result["status"]
            task_state.save(st)
        if (st.get("kind") not in ("review", "scout")
                and st.get("outcome") in ("completed", "budget_exhausted", "stuck")
                and st.get("check") and not stopped()):
            check = checking.saved_check(st, cfg, stopped, pending.get("review_after"))
        if (st.get("kind") != "scout" and st.get("outcome") == "completed" and pending.get("review_after") and not stopped()
                and not st.get("review_skipped") and (check is None or check["outcome"] == "passed")
                and not budgets.stop_at_weekly_budget(st)):
            st["work_completed"] = True
            base_merge = review_cycle.check_base_merge(st)
            if not stopped() and not budgets.stop_at_weekly_budget(st):
                conflict = base_merge["outcome"] == "conflict"
                review = (None if conflict else
                          review_cycle.review_turn(st, cfg, prompts.review_prompt(st, cfg), effort, stopped, sleep))
                if st.get("fix_rounds") or conflict:
                    review_cycle.auto_fix(st, cfg, review, effort, stopped, sleep, base_merge=base_merge)
        if working:
            ci_runs.ci_cycle(st, cfg, effort, stopped, sleep)
        checks = st.get("checks", [])[st.get("check_start", 0):]
        check = checks[-1] if checks else None
    except (Exception, SystemExit) as exc:
        st.update(outcome="failed", error=str(exc))
        print(f"supervisor failed: {exc}", file=sys.stderr)
    finally:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        finalized = True
        try:
            guarded_changed = len(checking.guarded_changes(st)) if cfg is not None else 0
            st["outcome"] = task_state.stop_outcome(st, stopped) or st["outcome"]
            if st.get("fix_rounds") and (st["outcome"] != "completed" or not st.get("auto_fix_stop")):
                st["auto_fix_stop"] = ("budget" if budgets.budget_stop(st) == "weekly" else
                                       "review refused" if st["outcome"] == "refused" and st.get("kind") == "review"
                                       else st["outcome"] if st["outcome"] != "completed"
                                       else st.get("review_skipped", "review unknown"))
            st.pop("sleeping", None)
            st.pop("pending", None)
            st["turn_finished"] = clock.CLOCK.time()
            telemetry_log.record_turn_result(st, stopped)
            if cfg is not None:
                account = notifications.current_turn_account(st)
                message = f"turn {st['outcome']} on {account}"
                result = st.get("result")
                message += ", result " + (result["status"] if result is not None else "unknown")
                if check is not None:
                    message += ", check " + check["outcome"]
                notifications.notify(st, cfg, "turn_finished", message, outcome=st["outcome"], account=account, result=result,
                       guarded_changed=guarded_changed, ci=st.get("ci"))
                if st["outcome"] == "interrupted":
                    notifications.notify(st, cfg, "interrupted", task_state.interruption_message(st), outcome=st["outcome"],
                           signal=st.get("signal"))
                if (result is not None and result["status"] in ("blocked", "needs_decision")
                        and st.get("kind") not in ("review", "scout")):
                    question = next(iter(result["questions"]), "")
                    notifications.notify(st, cfg, "needs_input", f"result {result['status']}: {question}",
                           result=result, question=question)
            notifications.reap_notifications()
            st["pid"] = None
            task_state.save(st)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
