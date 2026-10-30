"""Review angles, review turns and automatic fixes."""
import json
import re
import subprocess
from pathlib import Path

from . import budgets, checking, claude_review, clock, config, notifications, procs, prompts, task_state, telemetry_log, threads, turns


def selected_review_angles(st, cfg):
    selection = st.get("review_angles")
    if selection != "auto":
        return selection or []
    # Count committed and uncommitted changes separately, including staged edits.
    # Disable clean/process filters too: numstat still reads worktree contents.
    configured = subprocess.run(config.GIT + ["config", "--null", "--name-only", "--get-regexp",
                                       r"^filter\..*\.(clean|smudge|process)$"],
                                cwd=st["worktree"], text=True, capture_output=True)
    if configured.returncode not in (0, 1):
        configured.check_returncode()
    diff_git = list(config.GIT)
    drivers = {key[len("filter."):key.rfind(".")] for key in configured.stdout.split("\0") if key}
    for driver in sorted(drivers):
        for command in ("clean=", "smudge=", "process=", "required=false"):
            diff_git += ["-c", f"filter.{driver}.{command}"]
    lines = 0
    for revisions in ([st["base"] + "...HEAD"], ["HEAD"]):
        output = procs.sh(diff_git + ["diff", "--no-ext-diff", "--no-textconv", "--numstat"] + revisions,
                    cwd=st["worktree"])
        for record in output.splitlines():
            added, removed, _ = record.split("\t", 2)
            if added.isdigit() and removed.isdigit():
                lines += int(added) + int(removed)
    # New text files are not part of `git diff HEAD` until they are staged.
    untracked = procs.sh(config.GIT + ["ls-files", "--others", "--exclude-standard", "-z"], cwd=st["worktree"],
                   errors="surrogateescape")
    for name in untracked.split("\0"):
        path = Path(st["worktree"]) / name
        if not name or path.is_symlink() or not path.is_file():
            continue
        count, tail, binary = 0, b"", False
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                if b"\0" in chunk:
                    binary = True
                    break
                count += chunk.count(b"\n")
                tail = chunk[-1:]
        if not binary:
            lines += count + int(bool(tail) and tail != b"\n")
    return list(prompts.AUTO_REVIEW_ANGLES) if lines >= cfg["review_angles_min_lines"] else []


def merge_reviews(angles):
    findings, seen = [], {}
    for entry in angles:
        for finding in (entry.get("result") or {}).get("findings", []):
            key = (finding["file"], finding["line"], finding["title"].casefold())
            if key in seen:
                previous = seen[key]
                if entry["angle"] not in previous["angle"].split(","):
                    previous["angle"] += "," + entry["angle"]
                # Keep the strongest severity when two views identify the same defect.
                previous["severity"] = min(previous["severity"], finding["severity"])
                continue
            merged = dict(finding, id=f"F{len(findings) + 1}", angle=entry["angle"])
            findings.append(merged)
            seen[key] = merged
    verdict = "defects" if any((entry.get("result") or {}).get("verdict") == "defects"
                               for entry in angles) else "clean"
    summary = "\n".join(f"[{entry['angle']}] " +
                        ((entry.get("result") or {}).get("summary") or entry["outcome"])
                        for entry in angles)
    return {"verdict": verdict, "findings": findings, "summary": summary, "angles": angles}


def claude_review_turn(st, cfg, prompt, stopped):
    """Record one Claude attempt; only unavailability permits a Codex fallback."""
    st["review_account"] = "claude"
    with telemetry_log.record_phase(st, "review", stopped) as phase:
        phase.reviewer = "claude"
        entry = {"reviewer": "claude", "account": "claude", "thread": "",
                 "started": clock.CLOCK.time(), "finished": None, "result": None,
                 "round": len(st.get("rounds", [])),
                 "angle": st.get("review_angle") or "correctness", "outcome": "running"}
        st.setdefault("reviews", []).append(entry)
        task_state.save(st)
        last = config.TASKS / st["slug"] / "review.md"
        last.unlink(missing_ok=True)
        response = claude_review.run(st, cfg, prompt, stopped)
        if response["outcome"] == "completed":
            last.write_text(json.dumps(response["result"], ensure_ascii=False, indent=2), encoding="utf-8")
        st["outcome"] = response["outcome"]
        entry.update(result=response["result"], outcome=response["outcome"],
                     finished=clock.CLOCK.time(), thread=response["telemetry"].get("session_id") or "")
        phase.add_claude_usage(response["telemetry"])
        if response["outcome"] == "unavailable":
            entry["unavailable"] = response["reason"]
        elif response.get("reason"):
            st["error"] = response["reason"]
        task_state.save(st)
    return response["outcome"]


def review_turn(st, cfg, prompt, effort, stopped, sleep, new_turn=False, angles=None):
    st["review_account"] = None
    records = []
    result = None
    st.update(kind="review", outcome="running", reset_wait_seconds=0)
    st.pop("review_result", None)
    task_state.save(st)
    angles = selected_review_angles(st, cfg) if angles is None else angles
    try:
        for angle in angles or [None]:
            st["review_account"] = None
            if stopped():
                st["outcome"] = task_state.stop_outcome(st, stopped)
                break
            if budgets.stop_at_weekly_budget(st):
                break
            start = len(st.get("reviews", []))
            started = clock.CLOCK.time()
            st.update(review_angle=angle, outcome="running")
            focused = prompt + f"\n\nReview angle: {angle}. {prompts.REVIEW_ANGLES[angle]}" if angle else prompt
            reviewer = st.get("reviewer", "codex")
            if reviewer == "claude":
                outcome = claude_review_turn(st, cfg, focused, stopped)
                if outcome == "unavailable" and not stopped():
                    reviewer = "codex"
                    st["review_account"] = None
                    turns.run_turn(st, cfg, focused, effort, False, stopped, sleep, new_turn=new_turn)
            else:
                turns.run_turn(st, cfg, focused, effort, False, stopped, sleep, new_turn=new_turn)
            reviews = [entry for entry in st.get("reviews", [])[start:] if not entry.get("unavailable")]
            outcome = task_state.stop_outcome(st, stopped) or st["outcome"]
            result = reviews[-1].get("result") if reviews else None
            if reviews:
                if st.get("review_account") is None:
                    st["review_account"] = reviews[-1]["account"]
                reviews[-1]["outcome"] = outcome
            record = {"angle": angle or "correctness", "outcome": outcome, "result": result,
                      "account": notifications.current_turn_account(st),
                      "reviewer": reviewer,
                      "started": started, "finished": clock.CLOCK.time()}
            if outcome == "refused":
                record["refusal"] = st.get("refusal", "")
            records.append(record)
            if angles:
                result = merge_reviews(records)
            st["review_result"] = result
            task_state.save(st)
            if outcome not in ("completed", "refused"):
                break
        if (not stopped() and st["outcome"] != "budget_exhausted" and records
                and all(entry["outcome"] in ("completed", "refused") for entry in records)):
            st["outcome"] = "refused" if all(entry["outcome"] == "refused" for entry in records) else "completed"
    except (Exception, SystemExit):
        st["outcome"] = task_state.stop_outcome(st, stopped) or "failed"
        raise
    finally:
        st.pop("review_angle", None)
        if st.get("outcome") != "refused":
            st.pop("refusal", None)
        account = notifications.current_turn_account(st)
        st["outcome"] = outcome = task_state.stop_outcome(st, stopped) or st["outcome"]
        notifications.notify(st, cfg, "review_done", f"review {outcome} on {account}",
               outcome=outcome, account=account, angles=[entry["angle"] for entry in records],
               verdict=result["verdict"] if result else None,
               findings=len(result["findings"]) if result else 0, round=len(st.get("rounds", [])))
        task_state.save(st)
    return result


def fix_round_changes(st, before, after, merge_round=False):
    """Record round commits and ordinary fix size, excluding guarded paths."""
    commits = procs.sh(config.GIT + ["rev-list"] + (["--first-parent"] if merge_round else [])
                      + [f"{before}..{after}"], cwd=st["worktree"]).splitlines()
    if merge_round:
        return {"commits": commits}
    stats = procs.sh(config.GIT + ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                      "--numstat", "-z", before, after, "--"], cwd=st["worktree"], errors="replace")
    paths = checking.check_paths(st)
    code = {"added": 0, "removed": 0}
    for item in filter(None, stats.split("\0")):
        added, removed, path = item.split("\t", 2)
        if not checking.guarded_path(path, st, paths):
            code["added"] += int(added) if added != "-" else 0
            code["removed"] += int(removed) if removed != "-" else 0
    return {"commits": commits, "code": code}


def fix_code_findings(st, review):
    """Find P1/P2 locations whose HEAD neighborhood came from a fix round."""
    origins = {sha: number for number, entry in enumerate(st.get("rounds", []), 1)
               for sha in entry.get("commits", [])}
    if not origins:
        return []
    escalations = []
    for finding in review["findings"]:
        path, line = finding.get("file"), finding.get("line")
        if finding.get("severity") not in ("P1", "P2") or not path or type(line) is not int or line < 1:
            continue
        blame = procs.sh(config.GIT + ["--literal-pathspecs", "blame", "--porcelain", "--no-textconv",
                          "-L", f"{max(1, line - 2)},{line + 2}", "HEAD", "--", path],
                   cwd=st["worktree"], check=False, errors="replace")
        headers = re.findall(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$", blame, re.M)
        # Git clamps the upper range to EOF. The finding itself must still exist.
        if not any(int(final_line) == line for _, final_line in headers):
            continue
        sha = next((sha for sha, _ in headers if sha in origins), None)
        if sha is not None:
            short = procs.sh(config.GIT + ["rev-parse", "--short", sha], cwd=st["worktree"])
            escalations.append(dict(finding, round=origins[sha], commit=short))
    return escalations


def check_base_merge(st, stopped):
    """Fetch the task base and probe its merge without changing HEAD or the index."""
    base = st["base"]
    entry = {"base": base, "outcome": "skipped"}
    stage = "fetch"
    try:
        for _ in range(2):
            remote, branch = base.split("/", 1)
            try:
                subprocess.run(config.GIT + ["fetch", "--quiet", remote,
                               f"+refs/heads/{branch}:refs/remotes/{base}"],
                               cwd=st["worktree"], text=True, capture_output=True, errors="replace",
                               timeout=60, check=True)
                break
            except subprocess.CalledProcessError as exc:
                if stopped():
                    return None
                missing = f"fatal: couldn't find remote ref refs/heads/{branch}"
                if st.get("base_switch") or missing not in (exc.stderr or "").splitlines():
                    raise
                default = subprocess.run(config.GIT + ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
                                         cwd=st["worktree"], text=True, capture_output=True, errors="replace",
                                         timeout=60)
                target = default.stdout.strip()
                if default.returncode or not target.startswith(remote + "/"):
                    raise ValueError(f"{missing}; cannot resolve default branch from refs/remotes/{remote}/HEAD") from exc
                if target == base:
                    raise
                st["base_switch"] = {"from": base, "to": target, "at": clock.CLOCK.time()}
                st["base"] = base = target
                entry["base"] = base
                task_state.save(st)
        stage = "merge-tree"
        merged = subprocess.run(config.GIT + ["merge-tree", "--write-tree", "--name-only", "-z", "HEAD", base],
                                cwd=st["worktree"], text=True, capture_output=True, errors="replace", timeout=60)
        if merged.returncode not in (0, 1):
            merged.check_returncode()
        entry["outcome"] = "conflict" if merged.returncode else "passed"
        if merged.returncode:
            # -z emits the tree ID, paths, an empty field, then conflict messages.
            entry["files"] = merged.stdout.split("\0\0", 1)[0].split("\0")[1:]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if stopped():
            return None
        detail = ((exc.stderr or exc.stdout or "").strip()
                  if isinstance(exc, subprocess.CalledProcessError) else str(exc))
        entry["reason"] = f"{stage}: {detail or str(exc)}"
    st.setdefault("base_merges", []).append(entry)
    task_state.save(st)
    return entry


def auto_fix(st, cfg, review, effort, stopped, sleep, base_merge=None):
    """Bounded work/check/review loop under the original supervisor and wait."""
    failed_check = None
    if budgets.stop_at_weekly_budget(st):
        return
    if st.get("outcome") == "refused":
        st["auto_fix_stop"] = "review refused"
        return
    while not stopped() and st.get("outcome") == "completed":
        if budgets.stop_at_weekly_budget(st):
            return
        conflict = base_merge is not None and base_merge["outcome"] == "conflict"
        if failed_check is None and not conflict:
            if review is None or any(entry["outcome"] == "completed" and entry.get("result") is None
                                     for entry in review.get("angles", [])):
                st["auto_fix_stop"] = "review unknown"
                return
            if review["verdict"] == "clean":
                st["auto_fix_stop"] = "clean"
                return
            if review["findings"] and all(finding["severity"] == "P3" for finding in review["findings"]):
                st["auto_fix_stop"] = "only P3"
                return
            escalations = fix_code_findings(st, review)
            if escalations:
                st["auto_fix_stop"] = "escalate"
                st["auto_fix_escalations"] = escalations
                return
        if conflict and len(st["rounds"]) >= st["fix_rounds"]:
            st["auto_fix_stop"] = st["review_skipped"] = "base conflict"
            return
        if len(st["rounds"]) >= st["fix_rounds"]:
            st["auto_fix_stop"] = "check " + failed_check["outcome"] if failed_check else "rounds_exhausted"
            return
        message = prompts.fix_prompt(st, review, failed_check, cfg, base_merge=base_merge if conflict else None)
        entry = {"review": None if failed_check or conflict else review, "decisions": [], "check": None}
        if conflict:
            entry["base_merge"] = base_merge
        base_merge = None
        st["rounds"].append(entry)
        st.update(kind="work", result=None, outcome="running", reset_wait_seconds=0)
        st.pop("review_skipped", None)
        st.pop("publish", None)
        prompt = threads.followup_prompt(st, message, cfg, source="review")
        task_state.save(st)
        work_paths = checking.check_paths(st)
        before = procs.sh(config.GIT + ["rev-parse", "HEAD"], cwd=st["worktree"])
        turns.work_turn(st, cfg, prompt, effort, True, stopped, sleep, new_turn=False, work_paths=work_paths)
        after = procs.sh(config.GIT + ["rev-parse", "HEAD"], cwd=st["worktree"])
        entry.update(fix_round_changes(st, before, after, merge_round="base_merge" in entry))
        result = st.get("result")
        entry["decisions"] = result["findings"] if result is not None else []
        task_state.save(st)
        if not stopped() and st.get("outcome") in ("budget_exhausted", "stuck"):
            entry["check"] = checking.saved_check(st, cfg, stopped, review_after=False)
            if budgets.budget_stop(st) == "weekly":
                st["auto_fix_stop"] = "budget"
            task_state.save(st)
            return
        if stopped() or st.get("outcome") != "completed":
            return
        if result is None or result["status"] != "done":
            st["auto_fix_stop"] = "result " + (result["status"] if result else "unknown")
            st["review_skipped"] = st["auto_fix_stop"]
            return
        check = checking.saved_check(st, cfg, stopped)
        entry["check"] = check
        task_state.save(st)
        if stopped():
            return
        if check is not None and check["outcome"] != "passed":
            failed_check = check
            continue
        failed_check = None
        st["work_completed"] = True
        if budgets.stop_at_weekly_budget(st):
            return
        base_merge = check_base_merge(st, stopped)
        if stopped():
            return
        if base_merge["outcome"] == "conflict":
            continue
        if budgets.stop_at_weekly_budget(st):
            return
        next_angles = None if review is None else []
        if review and len(review.get("angles", [])) > 1:
            next_angles = ["correctness"]
            next_angles += [entry["angle"] for entry in review["angles"]
                            if entry["angle"] != "correctness" and (entry.get("result") or {}).get("findings")]
        review = review_turn(st, cfg, prompts.review_prompt(st, cfg), effort, stopped, sleep, angles=next_angles)
        if st.get("outcome") == "refused":
            st["auto_fix_stop"] = "review refused"
            return
