"""Wait output rendering and the wait command."""
import json
import time

from . import account_rotation, checking, ci_runs, clock, config, procs, result_schema, sandboxing, task_state, threads, turn_monitor


def wait_rotation_lines(st):
    if st.get("weekly_budget_warning"):
        yield st["weekly_budget_warning"]
    for r in st["rotations"][st.get("rotation_start", 0):]:
        if r.get("event") == "sleep":
            yield f"== sleeping until {config.local_time(r['until'])} ({r['account'] or 'unknown'}): {r['reason']}"
        elif r.get("event") == "wake":
            yield f"== woke at {config.local_time(r['at'])} to recheck account limits"
        elif r.get("event") == "reset_credit":
            if r.get("outcome") in ("success", "consumed (inferred)"):
                yield f"== used a free limit reset on {r['account']} ({r['credit_id']}), expires {config.local_time(r.get('expires_at'))}"
        elif r.get("event") == "needs_login":
            yield f"== account {r['account']} needs login: run login {r['account']}"
        else:
            yield f"== rotated {r['from']} → {r['to']}: {r['reason']}, new thread {r['thread'] or '?'}"
    if st.get("outcome") == "sleeping":
        if procs.alive(st.get("pid")):
            s = st["sleeping"]
            label = "account status unknown; retry at" if s.get("probe") else "all accounts at limit; auto-continue at"
            yield f"== {label} {config.local_time(s['until'])} ({s['account'] or 'unknown'}); run wait again to follow"
        else:
            yield "== paused, supervisor gone; continue with say"
    if st.get("exhausted"):
        e = st["exhausted"]
        reset = f"{config.local_time(e['reset'])} ({e['account']})" if e["reset"] is not None else "unknown"
        yield f"== all accounts at limit; earliest reset {reset}; continue with say after that"


def wait_publication_lines(st):
    if st.get("sandbox") == "workspace":
        yield "== sandbox: workspace"
    publication = st.get("publish")
    if publication and (st.get("kind") != "review" or st.get("work_completed")):
        if publication["sha"]:
            subject = procs.sh(config.GIT + ["log", "-1", "--format=%s", publication["sha"]], cwd=st["worktree"], check=False)
            yield (f"== committed {publication['sha']} " + json.dumps(subject, ensure_ascii=False)
                   + ("; pushed" if publication["pushed"] else "")
                   + ("; PR " + publication["pr"] if publication["pr"] else ""))
        error = publication["error"]
        if error.startswith(("commit: ", "push: ")):
            yield "== publish error: " + error
        action = publication.get("action")
        if action in ("created", "updated", "skipped"):
            yield "== PR " + ("skipped: " + error if action == "skipped" else action + " " + publication["pr"])
    if st.get("ci"):
        yield "\n".join(ci_runs.ci_lines(st["ci"]))


def wait_turn_lines(st, slug):
    sleeping = st.get("outcome") == "sleeping"
    publication = st.get("publish")
    yield (f"== {slug}: turn {st.get('turns')} {'paused' if sleeping else 'finished'} after "
          f"{(st.get('turn_finished', clock.CLOCK.time()) - st.get('turn_started', clock.CLOCK.time())) / 60:.1f} min")
    if st.get("capacity_retries", 0):
        yield "== retried: model at capacity"
    if st.get("turn_stop"):
        yield "== turn stopped: " + turn_monitor.turn_stop_message(st["turn_stop"])
    if st.get("outcome") == "interrupted":
        yield "== " + task_state.interruption_message(st)
    if st.get("outcome") == "refused" and st.get("kind") != "review":
        yield "== refused by the model: " + st.get("refusal", "")[:200]
    if (st.get("kind") != "review" or st.get("work_completed")) and not (
            st.get("outcome") == "refused" and st.get("kind") != "review"):
        last = config.TASKS / slug / "last.md"
        if (st.get("exhausted") or sleeping) and len(threads.events(st)) == st.get("event_start", 0):
            # Paused before any exec: the thread's last message belongs to an earlier turn.
            yield "(Codex did not run this turn)"
        else:
            raw = ("(no wrap-up result)" if st.get("wrapup", {}).get("outcome") not in (None, "completed")
                   else last.read_text().strip() if last.exists() else threads.last_message(st) or "(no final message)")
            yield raw if st.get("kind") == "scout" else "\n".join(result_schema.result_lines(st.get("result"), raw))
            if publication and publication["pr"]:
                yield "PR: " + publication["pr"]
    yield from checking.guarded_lines(checking.guarded_changes(st))


def wait_round_lines(st):
    for number, entry in enumerate(st.get("rounds", []), 1):
        findings = (entry.get("review") or {}).get("findings", [])
        counts = ", ".join(f"{severity} {sum(f['severity'] == severity for f in findings)}"
                           for severity in ("P1", "P2", "P3") if any(f["severity"] == severity for f in findings))
        source = (f"{len(findings)} findings" + (f" ({counts})" if counts else "")
                  if entry.get("review") is not None else "check failure")
        decisions = entry.get("decisions", [])
        fixed, rejected = (sum(d["decision"] == action for d in decisions) for action in ("fixed", "rejected"))
        code = entry.get("code")
        size = f"; code +{code['added']} −{code['removed']}" if code is not None else ""
        yield f"== round {number}: {source} → fixed {fixed}, rejected {rejected}{size}"
        yield from (f"- rejected {d['id']}: {d['note']}" for d in decisions if d["decision"] == "rejected")


def wait_check_lines(st):
    checks = [] if st.get("kind") == "scout" else st.get("checks", [])[st.get("check_start", 0):]
    for check in checks:
        for name, item in ((f"check `{check['cmd']}`", check), ("base tests", check.get("base_check"))):
            if not item:
                continue
            if name == "base tests" and item["outcome"] == "skipped":
                yield f"== base tests: skipped ({item['reason']})"
            else:
                label = item["outcome"] if item["outcome"] in ("passed", "stopped", "interrupted") else "FAILED"
                reason = "timeout, " if item["outcome"] == "timeout" else ""
                yield f"== {name}: {label} ({reason}exit {item['exit']}, {item['seconds']:.0f} s)"
            if name != "base tests" and item["outcome"] in ("failed", "timeout"):
                yield item.get("tail", checking.check_tail(st))
            elif name == "base tests" and item.get("tail"):
                yield item["tail"]


def wait_review_lines(st, slug):
    if st.get("review_skipped"):
        yield "== review skipped: " + st["review_skipped"]
    if st.get("auto_fix_stop") == "escalate":
        for finding in st.get("auto_fix_escalations", []):
            yield (f"== auto-fix: stop: escalate — {finding['id']} {finding['file']}:{finding['line']} "
                   f"targets code from fix round {finding['round']} ({finding['commit']})")
    reviews = st.get("reviews", [])[st.get("review_start", 0):]
    if not (st.get("kind") == "review" or st.get("rounds") or reviews):
        return
    aggregate = st.get("review_result")
    angles = aggregate and aggregate.get("angles")
    if not angles and st.get("kind") == "review" and st.get("outcome") == "refused":
        yield "== refused by the model: " + st.get("refusal", "")[:200]
        return
    entries = angles or reviews[-1:]
    for entry in entries:
        minutes = ((entry["finished"] if angles else entry.get("finished") or clock.CLOCK.time()) - entry["started"]) / 60
        heading = "== review " + (f"angle {entry['angle']} " if angles else "") + f"by {entry['account']} ({minutes:.1f} min):"
        result = entry.get("result")
        if angles and entry["outcome"] == "refused":
            yield heading + " refused"
            yield "== refused by the model: " + entry.get("refusal", "")[:200]
        elif result is None:
            yield heading + (" " + entry["outcome"] + " (result unknown)" if angles else "")
            if not angles:
                if st.get("kind") == "review" and st.get("wrapup"):
                    yield "(review interrupted; no wrap-up result)"
                else:
                    yield "== result: unknown (unstructured final message)"
                    review = config.TASKS / slug / "review.md"
                    yield review.read_text().strip() if review.exists() else "(no final review)"
        else:
            count = len(result["findings"])
            yield heading + (f" {count} defect{'s' if count != 1 else ''}" if count else " no defects")
    result = aggregate if angles else entries[-1].get("result") if entries else None
    if result is not None:
        yield result["summary"]
        for finding in result["findings"]:
            angle = f"[{finding['angle']}] " if angles else ""
            yield f"[{finding['severity']}] {angle}{finding['id']} {finding['file']}:{finding['line']} — {finding['title']}"
            yield from (finding["detail"], "verify: " + finding["verify"])
    elif not entries:
        yield "(Codex did not run review)"


def wait_stop_lines(st):
    if st.get("fix_rounds"):
        if (st.get("review_result") or {}).get("angles"):
            results = [entry.get("review") for entry in st.get("rounds", [])] + [st["review_result"]]
        else:
            results = [entry.get("result") for entry in st.get("reviews", [])[st.get("review_start", 0):]]
        trajectory = " → ".join(str(len(result["findings"])) for result in results if result is not None) or "unknown"
        reason = st.get("auto_fix_stop", st.get("outcome", "unknown"))
        yield f"== auto-fix: {len(st.get('rounds', []))} rounds, findings {trajectory}; stop: {reason}"
    failed = [e for e in threads.events(st)[st.get("exec_event_start", st.get("event_start", 0)):]
              if e.get("type") in ("turn.failed", "error") and account_rotation.refusal_message(e) is None]
    if failed:
        yield "== errors: " + json.dumps(failed[-1], ensure_ascii=False)[:500]
    if st.get("error"):
        yield "== supervisor error: " + str(st["error"])
    if st.get("kind") == "scout":
        return
    yield f"== commits on {st['branch']}:"
    yield procs.sh(config.GIT + ["log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False) or "(none)"
    dirty = (procs.sh(config.GIT + ["status", "--short"], cwd=st["worktree"], check=False)
             if sandboxing.workspace_filter_driver(st) is None else "")
    if dirty:
        yield "== uncommitted:\n" + dirty


def cmd_wait(a):
    st = task_state.load(a.slug)
    start = time.monotonic()
    while procs.alive(st.get("pid")):
        if st.get("outcome") == "sleeping" and not getattr(a, "through_sleep", False):
            break
        if a.timeout and time.monotonic() - start > a.timeout:
            print(f"{a.slug}: still running after {a.timeout} s")
            return
        time.sleep(config.WAIT_POLL_SECONDS)
        st = task_state.load(a.slug)
    if st.get("pid") and not procs.alive(st["pid"]):
        # The supervisor may have published its final state since the last poll.
        st = task_state.load(a.slug)
        if st.get("pid") and not procs.alive(st["pid"]):
            st["pid"] = None
            task_state.save(st)
    for section in (wait_rotation_lines(st), wait_publication_lines(st), wait_turn_lines(st, a.slug),
                    wait_round_lines(st), wait_check_lines(st), wait_review_lines(st, a.slug),
                    wait_stop_lines(st)):
        for line in section:
            print(line)
