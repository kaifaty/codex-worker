"""Read the persistent task journal and summarize measured stage costs.

Only journal rows are needed; task state and account homes are never consulted.
"""

import datetime
import json
import math
import re
import statistics
import time
from bisect import bisect_right
from collections import Counter
from pathlib import Path


TOKEN_KEYS = ("input", "cached_input", "output", "reasoning")
MODEL_PHASES = frozenset(("work", "fix", "wrapup", "review", "scout"))
CHECK_PHASES = frozenset(("check", "base_check"))
CI_OUTCOMES = frozenset(("passed", "failed", "timeout", "none"))
CANCELLATION_OUTCOMES = ("stopped", "interrupted")
LEAD_VERDICTS = ("accepted", "revised", "redone", "dropped")
DIMENSIONS = ("task-type", "effort", "model", "executor", "reviewer")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except (OverflowError, ValueError):
        return None


def _identifier(value):
    return value if isinstance(value, str) and value else None


def _round(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _since_timestamp(since, now):
    if since in ("7d", "30d"):
        return now - int(since[:-1]) * 86400
    if isinstance(since, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        try:
            date = datetime.datetime.strptime(since, "%Y-%m-%d")
            return date.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            pass
    raise ValueError("--since must be 7d, 30d, or a valid YYYY-MM-DD date")


def _utc(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_tokens():
    return {key: 0 for key in TOKEN_KEYS}


def _add_tokens(target, source):
    if not isinstance(source, dict):
        return
    for key in TOKEN_KEYS:
        value = _number(source.get(key))
        if value is not None:
            target[key] += value


def _token_summary(tokens):
    result = dict(tokens)
    result["total"] = tokens["input"] + tokens["output"]
    result["cached_input_fraction"] = _fraction(tokens["cached_input"], tokens["input"])
    return result


def _fraction(numerator, denominator):
    return numerator / denominator if denominator else None


def _weekly_delta(row):
    before = _number(row.get("weekly_used_before"))
    after = _number(row.get("weekly_used_after"))
    # A decrease crosses a reset. Neither endpoint tells us how much was used
    # on each side, so it must not be counted as new consumption.
    if before is None or after is None or before > after or after > 100:
        return 0
    return after - before


def _read_rows(log_path, until, repo, slug):
    """Return stage rows and lead verdict rows up to the end of the window."""
    try:
        handle = Path(log_path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    rows = []
    verdicts = []
    try:
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict) or type(row.get("v")) is not int or row["v"] != 1:
                    continue
                at = _number(row.get("at"))
                if at is None or at > until:
                    continue
                if not _identifier(row.get("repo")) or not _identifier(row.get("slug")):
                    continue
                if repo is not None and row["repo"] != repo or slug is not None and row["slug"] != slug:
                    continue
                if row.get("kind") == "verdict":
                    if row.get("verdict") in LEAD_VERDICTS:
                        verdicts.append(row)
                    continue
                if not isinstance(row.get("phase"), str) or row["phase"] not in MODEL_PHASES | CHECK_PHASES | {"turn"}:
                    continue
                rows.append(row)
    except OSError:
        # Keep complete rows already read if a journal becomes unreadable.
        pass
    return sorted(rows, key=lambda row: row["at"]), sorted(verdicts, key=lambda row: row["at"])


def _task(repo, slug, created):
    return {"repo": repo, "slug": slug, "created": created, "turns": 0, "rounds": 0,
            "task_type": None, "models": [], "reasoning_efforts": [],
            "active_minutes": 0, "tokens": _empty_tokens(), "weekly_used_percent": 0,
            "capacity_retries": 0,
            "last_verdict": None, "outcome": None, "result_status": None,
            "auto_fix_stop": None, "ci": None,
            "max_task_weekly_percent": None, "budget_stop": None,
            "turn_stops": [], "reviews": [],
            "executors": [], "reviewers": [], "first_review_findings": None, "follow_ups": None,
            "lead_verdict": None, "lead_note": None,
            "_reviews": {}, "_turns": [], "_latest_turn": 0, "_model_settings": [], "_work_settings": [],
            "_work_turns": set(), "_first_done_turn": None, "_first_review": None, "_first_review_rows": [], "_started": None, "_checks": Counter(), "_ci": Counter()}


def _row_settings(row):
    usage = row.get("usage")
    settings = [entry for entry in usage if isinstance(entry, dict)
                and ("model" in entry or "reasoning_effort" in entry)] if isinstance(usage, list) else []
    return [(_identifier(entry.get("model")), _identifier(entry.get("reasoning_effort")))
            for entry in settings or [row]]


def _accumulate_model_settings(row, task):
    for model, effort in _row_settings(row):
        if (model, effort) not in task["_model_settings"]:
            task["_model_settings"].append((model, effort))
        for key, value in (("models", model), ("reasoning_efforts", effort)):
            if value not in task[key]:
                task[key].append(value)


def _is_work_phase(row):
    """Return whether a stage row is the executor's work: first turn, say, fix, CI repair, or wrap-up."""
    return row["phase"] in ("work", "fix") or (row["phase"] == "wrapup" and row.get("kind") == "work")


def _attribute(row, task):
    """Record what places a task in groups: executor, work model settings, reviewer, first review, follow-ups."""
    if row["phase"] == "review":
        reviewer = "claude" if row.get("reviewer") == "claude" else "codex"
        if reviewer not in task["reviewers"]:
            task["reviewers"].append(reviewer)
        group = (_round(row.get("turn")), _round(row.get("round")))
        if task["_first_review"] in (None, group):
            task["_first_review"] = group
            task["_first_review_rows"].append(row)
        return
    if not (_is_work_phase(row) or (row["phase"] == "turn" and row.get("kind", "work") == "work")):
        return
    executor = "claude" if row.get("provider") == "claude" else "codex"
    if executor not in task["executors"]:
        task["executors"].append(executor)
    if row["phase"] != "turn":
        for setting in _row_settings(row):
            if setting not in task["_work_settings"]:
                task["_work_settings"].append(setting)
    _accumulate_work(row, task)


def _accumulate_work(row, task):
    """Record the turn of the first done result and every work turn."""
    turn = _round(row.get("turn"))
    task["_work_turns"].add(turn)
    if row.get("result_status") == "done" and task["_first_done_turn"] is None:
        task["_first_done_turn"] = turn


def _account(provider, name):
    return {"provider": provider, "account": name, "turns": 0,
            "tokens": _empty_tokens(), "weekly_used_percent": 0}


def _review_verdict(rows):
    # A refused angle supplies no review result. Match the supervisor's merged
    # verdict for a mix of completed and refused angles, while preserving an
    # unknown verdict for incomplete or wholly refused review rounds.
    completed = [row for row in rows if row.get("outcome") not in ("refused", "unavailable")]
    if not completed or any(row.get("outcome") != "completed" or row.get("verdict") not in ("clean", "defects")
                            for row in completed):
        return None
    return "defects" if any(row["verdict"] == "defects" for row in completed) else "clean"


def _review_outcome(rows):
    outcomes = [row.get("outcome") for row in rows if row.get("outcome") != "unavailable"]
    if not outcomes:
        return "unavailable"
    if all(outcome in ("completed", "refused") for outcome in outcomes):
        return "completed" if "completed" in outcomes else "refused"
    return next((_identifier(outcome) for outcome in reversed(outcomes)
                 if outcome not in ("completed", "refused")), None)


def _task_identities(rows):
    """Return one (repo, slug, birth or legacy generation) key per row."""
    identities = []
    legacy_lifetimes = {}
    known_births = {}
    for row in rows:
        created = _number(row.get("created"))
        if created is not None:
            known_births.setdefault((row["repo"], row["slug"]), set()).add(created)
    known_births = {pair: sorted(births) for pair, births in known_births.items()}
    for row in rows:
        phase = row["phase"]
        pair = (row["repo"], row["slug"])
        created = _number(row.get("created"))
        if created is None:
            # A running task can gain birth timestamps after an upgrade. Join
            # its earlier stages to a later tagged stage from the same birth,
            # but keep stages before that birth in their older task lifetime.
            started = _number(row.get("started"))
            births = known_births.get(pair, [])
            index = bisect_right(births, row["at"] if started is None else started)
            if index:
                created = births[index - 1]
        if created is None:
            # Earlier journals had no birth timestamp. A fresh initial work or
            # scout turn can identify slug reuse when its turn counter restarts.
            # Missing counters and the repeated turns of reviews/fixes cannot.
            lifetime = legacy_lifetimes.setdefault(pair, {"generation": 0, "turn": None})
            turn = _round(row.get("turn"))
            if (phase in ("work", "scout") and turn > 0 and lifetime["turn"] is not None
                    and turn <= lifetime["turn"]):
                lifetime["generation"] += 1
            if turn > 0:
                lifetime["turn"] = turn
            identity = pair + (("legacy", lifetime["generation"]),)
        else:
            identity = pair + (created,)
        identities.append(identity)
    return identities


def _accumulate_usage(row, task, accounts, is_turn):
    """Add a stage's weekly cost and attribute usage to accounts."""
    usage = row.get("usage")
    segments = [entry for entry in usage if isinstance(entry, dict)] if isinstance(usage, list) else []
    if not segments:
        segments = [row]
    # New phases record the task's increase directly; segment costs only
    # attribute that increase to accounts. Endpoint deltas remain legacy.
    weekly_spent = _number(row.get("weekly_spent"))
    if weekly_spent is not None:
        task["weekly_used_percent"] += weekly_spent
    touched = set()
    for segment in segments:
        delta = _number(segment.get("weekly_spent")) if weekly_spent is not None else None
        if delta is None:
            delta = _weekly_delta(segment)
        if weekly_spent is None:
            task["weekly_used_percent"] += delta
        name = _identifier(segment.get("account")) or _identifier(row.get("account"))
        if name is None:
            continue
        provider = ("claude" if (segment.get("provider") or row.get("provider")
                                 or row.get("reviewer")) == "claude" else "codex")
        key = (provider, name)
        account = accounts.setdefault(key, _account(provider, name))
        account["weekly_used_percent"] += delta
        _add_tokens(account["tokens"], segment.get("tokens"))
        if is_turn:
            touched.add(key)
    for key in touched:
        accounts[key]["turns"] += 1


def _accumulate_checks_and_reviews(row, task, totals):
    """Collect check/review samples and their task outcome overrides."""
    phase = row["phase"]
    if phase in CHECK_PHASES:
        outcome = row.get("check_outcome")
        if outcome in ("passed", "failed", "timeout") or outcome in CANCELLATION_OUTCOMES:
            totals["checks"]["total"] += 1
            totals["checks"]["passed"] += outcome == "passed"
            if phase == "check":
                task["_checks"]["total"] += 1
                task["_checks"]["passed"] += outcome == "passed"
        # Ordinary check failures leave the model's task outcome intact;
        # cancellation stops the whole task, including during a base check.
        if row.get("outcome") in CANCELLATION_OUTCOMES:
            task["outcome"] = row["outcome"]
        elif outcome in CANCELLATION_OUTCOMES:
            task["outcome"] = outcome
    if phase == "review":
        reviewer = "claude" if row.get("reviewer") == "claude" else "codex"
        task["reviews"].append({"reviewer": reviewer, "account": _identifier(row.get("account")),
                                "turn": _round(row.get("turn")), "round": _round(row.get("round")),
                                "angle": _identifier(row.get("angle")),
                                "outcome": _identifier(row.get("outcome"))})
        totals["reviewers"][reviewer]["reviews"] += 1
        totals["reviewers"][reviewer]["cost_usd"] += _number(row.get("cost_usd")) or 0
        group = (_round(row.get("turn")), _round(row.get("round")))
        review_rows = task["_reviews"].setdefault(group, [])
        review_rows.append(row)
        task["outcome"] = _review_outcome(review_rows)
        if row.get("outcome") == "completed" and row.get("verdict") in ("clean", "defects"):
            totals["reviews"]["total"] += 1
            totals["reviews"]["with_defects"] += row["verdict"] == "defects"
            findings = _number(row.get("findings"))
            if findings is not None:
                totals["reviews"]["findings"] += findings
                totals["findings_samples"] += 1


def _accumulate_rows(rows, identities, since=0):
    """Return tasks, account rows, and totals for rows and their task keys.

    Rows before ``since`` only place tasks that have rows in the window into groups.
    """
    window = {identity for row, identity in zip(rows, identities) if row["at"] >= since}
    tasks = {}
    accounts = {}
    totals = {"tokens": _empty_tokens(), "active_seconds": 0, "turns": 0, "capacity_retries": 0,
              "checks": Counter(), "reviews": Counter(), "findings_samples": 0,
              "reviewers": {name: {"reviews": 0, "cost_usd": 0} for name in ("codex", "claude")}}
    for row, identity in zip(rows, identities):
        if identity not in window:
            continue
        phase = row["phase"]
        task = tasks.setdefault(identity, _task(row["repo"], row["slug"], _number(identity[2])))
        task["_started"] = task["_started"] or row["at"]
        _attribute(row, task)
        if row["at"] < since:
            continue
        if _identifier(row.get("task_type")) is not None:
            task["task_type"] = row["task_type"]
        task["_latest_turn"] = max(task["_latest_turn"], _round(row.get("turn")))
        if phase == "turn":
            # Final supervisor results carry no additional stage cost or usage.
            task["_turns"].append(row)
            task["turn_stops"] = row.get("turn_stops", [])
            task["rounds"] = max(task["rounds"], _round(row.get("round")))
            continue
        is_turn = phase in MODEL_PHASES
        seconds = _number(row.get("active_seconds")) or 0
        totals["active_seconds"] += seconds
        task["active_minutes"] += seconds / 60
        retries = _round(row.get("capacity_retries"))
        totals["capacity_retries"] += retries
        task["capacity_retries"] += retries
        task["rounds"] = max(task["rounds"], _round(row.get("round")))
        _add_tokens(totals["tokens"], row.get("tokens"))
        _add_tokens(task["tokens"], row.get("tokens"))
        if is_turn:
            _accumulate_model_settings(row, task)
            totals["turns"] += 1
            task["turns"] += 1
            # Normal wrap-up success/failure retains the parent budget/stuck
            # outcome. Cancellation or a policy refusal stops the whole task.
            if ((phase != "wrapup" or row.get("outcome") in CANCELLATION_OUTCOMES + ("refused",))
                    and _identifier(row.get("outcome"))):
                task["outcome"] = row["outcome"]
        if phase == "scout":
            task["result_status"] = None
        elif phase in ("work", "fix") or (phase == "wrapup" and row.get("kind") == "work"):
            # A new working stage supersedes its predecessor even when it has
            # no structured result. Checks/reviews have no working result to
            # replace; review/scout wrap-ups must preserve that distinction.
            task["result_status"] = _identifier(row.get("result_status"))

        _accumulate_usage(row, task, accounts, is_turn)
        _accumulate_checks_and_reviews(row, task, totals)

    for account in accounts.values():
        account["tokens"] = _token_summary(account["tokens"])
    return tasks, sorted(accounts.values(), key=lambda account: (account["account"], account["provider"])), totals


def _reconcile_tasks(tasks):
    """Finalize accumulated tasks and return cost-ordered rows and distributions."""
    ci = Counter()
    fix_rounds = Counter()
    outcomes = Counter()
    result_status = Counter()
    auto_fix_stops = Counter()
    for task in tasks.values():
        groups = list(task.pop("_reviews").items())
        verdicts = {group: _review_verdict(review_rows) for group, review_rows in groups}
        final_turns = task.pop("_turns")
        if not task["_model_settings"]:
            for row in final_turns or [{}]:
                _accumulate_model_settings(row, task)
        if not task["_work_settings"]:
            # Turn rows can carry a reviewer's model, so they only stand in for missing work stages.
            for row in [row for row in final_turns if row.get("kind", "work") == "work"] or [{}]:
                for setting in _row_settings(row):
                    if setting not in task["_work_settings"]:
                        task["_work_settings"].append(setting)
        task.pop("_first_review")
        first_review = task.pop("_first_review_rows")
        if first_review:
            first = [row for row in first_review if row.get("outcome") == "completed"
                     and _number(row.get("severe_findings")) is not None]
            if first:
                task["first_review_findings"] = sum(row["severe_findings"] for row in first)
        work_turns = task.pop("_work_turns")
        done_turn = task.pop("_first_done_turn")
        if done_turn is not None:
            task["follow_ups"] = sum(turn > done_turn for turn in work_turns)
        for row in final_turns:
            turn_ci = row.get("ci")
            if isinstance(turn_ci, dict) and _identifier(turn_ci.get("outcome")) in CI_OUTCOMES:
                ci["total"] += 1
                ci["passed"] += turn_ci["outcome"] == "passed"
                task["_ci"]["total"] += 1
                task["_ci"]["passed"] += turn_ci["outcome"] == "passed"
            if row.get("kind") == "review":
                verdicts[(_round(row.get("turn")), _round(row.get("round")))] = (
                    _identifier(row.get("verdict")) if row.get("outcome") == "completed" else None)
        escalated_turns = {_round(row.get("turn")) for row in final_turns
                           if row.get("auto_fix_stop") == "escalate"}
        for (turn, round_number), verdict in verdicts.items():
            if verdict == "clean" and turn not in escalated_turns:
                fix_rounds[str(round_number)] += 1
                break
        latest_turn = task.pop("_latest_turn")
        # A newer turn may have saved its stages but lost its final journal row.
        if final_turns and _round(final_turns[-1].get("turn")) == latest_turn:
            final = final_turns[-1]
            task["outcome"] = _identifier(final.get("outcome"))
            task["result_status"] = _identifier(final.get("result_status"))
            task["last_verdict"] = _identifier(final.get("verdict"))
            task["max_task_weekly_percent"] = _number(final.get("max_task_weekly_percent"))
            task["budget_stop"] = final.get("budget_stop") if final.get("budget_stop") in ("minutes", "weekly") else None
            task["auto_fix_stop"] = _identifier(final.get("auto_fix_stop"))
            task["ci"] = final.get("ci") if isinstance(final.get("ci"), dict) else None
        elif groups:
            task["last_verdict"] = _review_verdict(groups[-1][1])
        if task["outcome"] is not None:
            outcomes[task["outcome"]] += 1
        if task["result_status"] is not None:
            result_status[task["result_status"]] += 1
        if task["auto_fix_stop"] is not None:
            auto_fix_stops[task["auto_fix_stop"]] += 1
        task["tokens"] = _token_summary(task["tokens"])

    task_rows = sorted(tasks.values(), key=lambda task: (-task["weekly_used_percent"],
                                                        -task["tokens"]["total"],
                                                        task["repo"], task["slug"],
                                                        -(task["created"] or 0)))
    return task_rows, {"ci": ci, "fix_rounds": fix_rounds, "outcomes": outcomes,
                       "result_status": result_status, "auto_fix_stops": auto_fix_stops}


def _verdict_task(tasks, repo, slug, created, at):
    """Return the task a verdict belongs to: the one born at ``created``, else the newest born by ``at``."""
    candidates = []
    for key, task in tasks.items():
        if key[:2] == (repo, slug):
            if created is not None and task["created"] == created:
                return task
            # Legacy tasks without a birth keep their generation, so the newest one sorts last.
            generation = key[2][1] if isinstance(key[2], tuple) else 0
            candidates.append(((task["created"] or 0, generation), task))
    # A task without a birth started at its first journal row.
    earlier = [item for item in candidates
               if (item[1]["created"] if item[1]["created"] is not None else item[1]["_started"]) <= at]
    return max(earlier, key=lambda item: item[0], default=(None, None))[1]


def _attach_lead_verdicts(tasks, verdicts):
    """Give each task its latest lead verdict, matched by repository, slug, and birth."""
    for row in verdicts:
        task = _verdict_task(tasks, row["repo"], row["slug"], _number(row.get("created")), row["at"])
        if task is not None:
            task["lead_verdict"] = row["verdict"]
            task["lead_note"] = row.get("note") if isinstance(row.get("note"), str) else None


def _median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def _mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _pooled(members, key):
    return _fraction(sum(task[key]["passed"] for task in members), sum(task[key]["total"] for task in members))


def _group_tasks(tasks, by):
    groups = {}
    for task in tasks:
        memberships = set()
        for model, effort in task["_work_settings"]:
            for executor in task["executors"] or ["codex"]:
                for reviewer in task["reviewers"] or ["none"]:
                    values = {"task-type": task["task_type"], "effort": effort, "model": model,
                              "executor": executor, "reviewer": reviewer}
                    memberships.add(tuple(values[key] or "unknown" for key in by))
        for membership in memberships:
            groups.setdefault(membership, []).append(task)
    rows = []
    for values, members in sorted(groups.items()):
        row = {key.replace("-", "_"): value for key, value in zip(by, values)}
        judged = [task for task in members if task["lead_verdict"] is not None]
        row.update(tasks=len(members),
                   done_fraction=_fraction(sum(task["result_status"] == "done" for task in members), len(members)),
                   median_rounds=statistics.median(task["rounds"] for task in members),
                   median_active_minutes=statistics.median(task["active_minutes"] for task in members),
                   median_weekly_used_percent=statistics.median(task["weekly_used_percent"] for task in members),
                   accepted_fraction=_fraction(sum(task["lead_verdict"] == "accepted" for task in judged),
                                               len(judged)),
                   average_first_review_findings=_mean(task["first_review_findings"] for task in members),
                   check_pass_fraction=_pooled(members, "_checks"),
                   ci_pass_fraction=_pooled(members, "_ci"),
                   median_follow_ups=_median(task["follow_ups"] for task in members))
        rows.append(row)
    return rows


def lead_verdict(log_path, repo, slug, created, now=None):
    """Return the lead verdict a report gives the task born at ``created``.

    A missing journal has no verdicts; other read errors raise OSError.
    """
    now = time.time() if now is None else now
    try:
        Path(log_path).open("rb").close()
    except FileNotFoundError:
        return None
    rows, verdicts = _read_rows(log_path, now, repo, slug)
    tasks = {}
    for row, identity in zip(rows, _task_identities(rows)):
        task = tasks.setdefault(identity, _task(row["repo"], row["slug"], _number(identity[2])))
        task["_started"] = task["_started"] or row["at"]
    _attach_lead_verdicts(tasks, verdicts)
    task = _verdict_task(tasks, repo, slug, _number(created), now)
    return None if task is None else task["lead_verdict"]


def build_report(log_path, since="7d", repo=None, slug=None, now=None, by=None):
    """Return JSON-safe summary, account totals, and tasks ordered by cost.

    Percentages describe percentage points of a weekly window. Ratios are in
    the range 0..1 and are null when there are no measured samples.
    """
    now = time.time() if now is None else now
    dimensions = list(dict.fromkeys(key.strip() for key in by.split(","))) if by is not None else []
    if any(key not in DIMENSIONS for key in dimensions):
        raise ValueError("--by must contain task-type, effort, model, executor, or reviewer separated by commas")
    since_at = _since_timestamp(since, now)
    rows, verdicts = _read_rows(log_path, now, repo, slug)
    tasks, account_rows, totals = _accumulate_rows(rows, _task_identities(rows), since_at)
    _attach_lead_verdicts(tasks, verdicts)
    task_rows, reconciled = _reconcile_tasks(tasks)
    groups = _group_tasks(task_rows, dimensions) if dimensions else None
    for task in task_rows:
        for key in ("_model_settings", "_work_settings", "_checks", "_ci", "_started"):
            task.pop(key)
    checks = totals["checks"]
    reviews = totals["reviews"]
    ci = reconciled["ci"]
    report = {
        "since": _utc(since_at), "until": _utc(now), "filters": {"repo": repo, "slug": slug},
        "summary": {
            "tasks": len(task_rows), "turns": totals["turns"], "active_hours": totals["active_seconds"] / 3600,
            "capacity_retries": totals["capacity_retries"],
            "tokens": _token_summary(totals["tokens"]),
            "weekly_used_percent": sum(task["weekly_used_percent"] for task in task_rows),
            "checks": {"total": checks["total"], "passed": checks["passed"],
                       "pass_fraction": _fraction(checks["passed"], checks["total"])},
            "ci": {"total": ci["total"], "passed": ci["passed"],
                   "pass_fraction": _fraction(ci["passed"], ci["total"])},
            "reviews": {"total": reviews["total"], "with_defects": reviews["with_defects"],
                        "defect_fraction": _fraction(reviews["with_defects"], reviews["total"]),
                        "findings": reviews["findings"],
                        "average_findings": _fraction(reviews["findings"], totals["findings_samples"])},
            "reviewers": totals["reviewers"],
            "fix_rounds_to_clean": dict(sorted(reconciled["fix_rounds"].items(), key=lambda item: int(item[0]))),
            "auto_fix_stops": dict(sorted(reconciled["auto_fix_stops"].items())),
            "outcomes": dict(sorted(reconciled["outcomes"].items())),
            "result_status": dict(sorted(reconciled["result_status"].items())),
        },
        "accounts": account_rows, "tasks": task_rows,
    }
    if dimensions:
        report.update(by=dimensions, groups=groups)
    return report


def _percent(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _distribution(values):
    return ", ".join(f"{key}: {value}" for key, value in values.items()) or "none"


def render_report(report):
    """Render the same metrics as the JSON report in a compact CLI table."""
    if report.get("by"):
        dimensions = report["by"]
        lines = [f"Task report: {report['since']} to {report['until']}",
                 "  ".join(dimensions + ["tasks", "done", "median rounds", "median minutes", "median weekly %",
                                         "accepted %", "findings", "checks %", "ci %", "follow-ups"])]
        for row in report["groups"]:
            labels = [row[key.replace("-", "_")] for key in dimensions]
            findings = row["average_first_review_findings"]
            follow_ups = row["median_follow_ups"]
            lines.append("  ".join(labels + [str(row["tasks"]), _percent(row["done_fraction"]),
                                            f"{row['median_rounds']:g}", f"{row['median_active_minutes']:.1f}",
                                            f"{row['median_weekly_used_percent']:.2f}",
                                            _percent(row["accepted_fraction"]),
                                            "n/a" if findings is None else f"{findings:.2f}",
                                            _percent(row["check_pass_fraction"]), _percent(row["ci_pass_fraction"]),
                                            "n/a" if follow_ups is None else f"{follow_ups:g}"]))
        if not report["groups"]:
            lines.append("(no tasks)")
        return "\n".join(lines)
    summary = report["summary"]
    tokens = summary["tokens"]
    checks = summary["checks"]
    ci = summary["ci"]
    reviews = summary["reviews"]
    average = reviews["average_findings"]
    lines = [
        f"Task report: {report['since']} to {report['until']}",
        f"Tasks: {summary['tasks']}  Turns: {summary['turns']}  Active hours: {summary['active_hours']:.2f}",
        f"Capacity retries: {summary['capacity_retries']}",
        f"Tokens: {tokens['total']:,.0f} (input {tokens['input']:,.0f}, output {tokens['output']:,.0f}, "
        f"reasoning {tokens['reasoning']:,.0f}, cached input {tokens['cached_input']:,.0f}; "
        f"cache share {_percent(tokens['cached_input_fraction'])})",
        f"Checks passed: {checks['passed']}/{checks['total']} ({_percent(checks['pass_fraction'])})",
        f"CI passed: {ci['passed']}/{ci['total']} ({_percent(ci['pass_fraction'])})",
        f"Reviews with defects: {reviews['with_defects']}/{reviews['total']} "
        f"({_percent(reviews['defect_fraction'])}); average findings: "
        + ("n/a" if average is None else f"{average:.2f}"),
        "Reviews by reviewer: " + "; ".join(
            f"{name} {values['reviews']} (${values['cost_usd']:.2f})"
            for name, values in summary["reviewers"].items()),
        "Fix rounds to clean: " + _distribution(summary["fix_rounds_to_clean"]),
        "Auto-fix stops: " + _distribution(summary["auto_fix_stops"]),
        "Outcomes: " + _distribution(summary["outcomes"]),
        "Result status: " + _distribution(summary["result_status"]),
        "", "Account weekly window usage:",
    ]
    if report["accounts"]:
        for account in report["accounts"]:
            label = "claude/" + account["account"] if account.get("provider") == "claude" else account["account"]
            lines.append(f"  {label}: {account['weekly_used_percent']:.2f}% "
                         f"({account['tokens']['total']:,.0f} tokens, {account['turns']} turns)")
    else:
        lines.append("  none")
    lines.extend(("", "Tasks by cost:", "slug  repo  started  turns  rounds  minutes  tokens  weekly %  last verdict  lead verdict  budget %  budget stop  auto-fix stop  capacity retries"))
    for task in report["tasks"]:
        budget = task["max_task_weekly_percent"]
        created = task["created"]
        started = "-" if created is None else time.strftime("%m-%d %H:%M", time.localtime(created))
        lines.append(f"{task['slug']}  {task['repo']}  {started}  "
                     f"{task['turns']}  {task['rounds']}  {task['active_minutes']:.1f}  "
                     f"{task['tokens']['total']:,.0f}  {task['weekly_used_percent']:.2f}  "
                     f"{task['last_verdict'] or '-'}  {task['lead_verdict'] or '-'}  "
                     + ("-" if budget is None else f"{budget:g}") + f"  {task['budget_stop'] or '-'}"
                     + f"  {task['auto_fix_stop'] or '-'}  {task['capacity_retries']}")
    if not report["tasks"]:
        lines.append("(no tasks)")
    return "\n".join(lines)
