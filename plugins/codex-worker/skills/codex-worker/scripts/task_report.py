"""Read the persistent task journal and summarize measured stage costs.

Only journal rows are needed; task state and account homes are never consulted.
"""

import datetime
import json
import math
import re
import time
from bisect import bisect_right
from collections import Counter
from pathlib import Path


TOKEN_KEYS = ("input", "cached_input", "output", "reasoning")
MODEL_PHASES = frozenset(("work", "fix", "wrapup", "review", "scout"))
CHECK_PHASES = frozenset(("check", "base_check"))
CI_OUTCOMES = frozenset(("passed", "failed", "timeout", "none"))
CANCELLATION_OUTCOMES = ("stopped", "interrupted")


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


def _read_rows(log_path, since, until, repo, slug):
    try:
        handle = Path(log_path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
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
                if at is None or at < since or at > until:
                    continue
                if not _identifier(row.get("repo")) or not _identifier(row.get("slug")):
                    continue
                if not isinstance(row.get("phase"), str) or row["phase"] not in MODEL_PHASES | CHECK_PHASES | {"turn"}:
                    continue
                if repo is not None and row["repo"] != repo:
                    continue
                if slug is not None and row["slug"] != slug:
                    continue
                rows.append(row)
    except OSError:
        # Keep complete rows already read if a journal becomes unreadable.
        pass
    return sorted(rows, key=lambda row: row["at"])


def _task(repo, slug, created):
    return {"repo": repo, "slug": slug, "created": created, "turns": 0, "rounds": 0,
            "active_minutes": 0, "tokens": _empty_tokens(), "weekly_used_percent": 0,
            "capacity_retries": 0,
            "last_verdict": None, "outcome": None, "result_status": None,
            "auto_fix_stop": None, "ci": None,
            "max_task_weekly_percent": None, "budget_stop": None,
            "turn_stops": [],
            "_reviews": {}, "_turns": [], "_latest_turn": 0}


def _account(name):
    return {"account": name, "turns": 0, "tokens": _empty_tokens(), "weekly_used_percent": 0}


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
        account = accounts.setdefault(name, _account(name))
        account["weekly_used_percent"] += delta
        _add_tokens(account["tokens"], segment.get("tokens"))
        if is_turn:
            touched.add(name)
    for name in touched:
        accounts[name]["turns"] += 1


def _accumulate_checks_and_reviews(row, task, totals):
    """Collect check/review samples and their task outcome overrides."""
    phase = row["phase"]
    if phase in CHECK_PHASES:
        outcome = row.get("check_outcome")
        if outcome in ("passed", "failed", "timeout") or outcome in CANCELLATION_OUTCOMES:
            totals["checks"]["total"] += 1
            totals["checks"]["passed"] += outcome == "passed"
        # Ordinary check failures leave the model's task outcome intact;
        # cancellation stops the whole task, including during a base check.
        if row.get("outcome") in CANCELLATION_OUTCOMES:
            task["outcome"] = row["outcome"]
        elif outcome in CANCELLATION_OUTCOMES:
            task["outcome"] = outcome
    if phase == "review":
        reviewer = "claude" if row.get("reviewer") == "claude" else "codex"
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


def _accumulate_rows(rows, identities):
    """Return tasks, account rows, and totals for rows and their task keys."""
    tasks = {}
    accounts = {}
    totals = {"tokens": _empty_tokens(), "active_seconds": 0, "turns": 0, "capacity_retries": 0,
              "checks": Counter(), "reviews": Counter(), "findings_samples": 0,
              "reviewers": {name: {"reviews": 0, "cost_usd": 0} for name in ("codex", "claude")}}
    for row, identity in zip(rows, identities):
        phase = row["phase"]
        task = tasks.setdefault(identity, _task(row["repo"], row["slug"], _number(identity[2])))
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
    return tasks, sorted(accounts.values(), key=lambda account: account["account"]), totals


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
        for row in final_turns:
            turn_ci = row.get("ci")
            if isinstance(turn_ci, dict) and _identifier(turn_ci.get("outcome")) in CI_OUTCOMES:
                ci["total"] += 1
                ci["passed"] += turn_ci["outcome"] == "passed"
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


def build_report(log_path, since="7d", repo=None, slug=None, now=None):
    """Return JSON-safe summary, account totals, and tasks ordered by cost.

    Percentages describe percentage points of a weekly window. Ratios are in
    the range 0..1 and are null when there are no measured samples.
    """
    now = time.time() if now is None else now
    since_at = _since_timestamp(since, now)
    rows = _read_rows(log_path, since_at, now, repo, slug)
    tasks, account_rows, totals = _accumulate_rows(rows, _task_identities(rows))
    task_rows, reconciled = _reconcile_tasks(tasks)
    checks = totals["checks"]
    reviews = totals["reviews"]
    ci = reconciled["ci"]
    return {
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


def _percent(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _distribution(values):
    return ", ".join(f"{key}: {value}" for key, value in values.items()) or "none"


def render_report(report):
    """Render the same metrics as the JSON report in a compact CLI table."""
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
            lines.append(f"  {account['account']}: {account['weekly_used_percent']:.2f}% "
                         f"({account['tokens']['total']:,.0f} tokens, {account['turns']} turns)")
    else:
        lines.append("  none")
    lines.extend(("", "Tasks by cost:", "slug  repo  started  turns  rounds  minutes  tokens  weekly %  last verdict  budget %  budget stop  auto-fix stop  capacity retries"))
    for task in report["tasks"]:
        budget = task["max_task_weekly_percent"]
        created = task["created"]
        started = "-" if created is None else time.strftime("%m-%d %H:%M", time.localtime(created))
        lines.append(f"{task['slug']}  {task['repo']}  {started}  "
                     f"{task['turns']}  {task['rounds']}  {task['active_minutes']:.1f}  "
                     f"{task['tokens']['total']:,.0f}  {task['weekly_used_percent']:.2f}  "
                     f"{task['last_verdict'] or '-'}  "
                     + ("-" if budget is None else f"{budget:g}") + f"  {task['budget_stop'] or '-'}"
                     + f"  {task['auto_fix_stop'] or '-'}  {task['capacity_retries']}")
    if not report["tasks"]:
        lines.append("(no tasks)")
    return "\n".join(lines)
