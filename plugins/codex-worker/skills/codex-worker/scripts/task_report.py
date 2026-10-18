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
            "last_verdict": None, "outcome": None, "result_status": None,
            "_reviews": {}, "_turns": [], "_latest_turn": 0}


def _account(name):
    return {"account": name, "turns": 0, "tokens": _empty_tokens(), "weekly_used_percent": 0}


def _review_verdict(rows):
    # A refused angle supplies no review result. Match the supervisor's merged
    # verdict for a mix of completed and refused angles, while preserving an
    # unknown verdict for incomplete or wholly refused review rounds.
    completed = [row for row in rows if row.get("outcome") != "refused"]
    if not completed or any(row.get("outcome") != "completed" or row.get("verdict") not in ("clean", "defects")
                            for row in completed):
        return None
    return "defects" if any(row["verdict"] == "defects" for row in completed) else "clean"


def _review_outcome(rows):
    outcomes = [row.get("outcome") for row in rows]
    if all(outcome in ("completed", "refused") for outcome in outcomes):
        return "completed" if "completed" in outcomes else "refused"
    return next((_identifier(outcome) for outcome in reversed(outcomes)
                 if outcome not in ("completed", "refused")), None)


def build_report(log_path, since="7d", repo=None, slug=None, now=None):
    """Return JSON-safe summary, account totals, and tasks ordered by cost.

    Percentages describe percentage points of a weekly window. Ratios are in
    the range 0..1 and are null when there are no measured samples.
    """
    now = time.time() if now is None else now
    since_at = _since_timestamp(since, now)
    rows = _read_rows(log_path, since_at, now, repo, slug)
    tasks = {}
    legacy_lifetimes = {}
    known_births = {}
    for row in rows:
        created = _number(row.get("created"))
        if created is not None:
            known_births.setdefault((row["repo"], row["slug"]), set()).add(created)
    known_births = {pair: sorted(births) for pair, births in known_births.items()}
    accounts = {}
    tokens = _empty_tokens()
    active_seconds = 0
    turns = 0
    checks = Counter()
    reviews = Counter()
    findings_samples = 0

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
        task = tasks.setdefault(identity, _task(row["repo"], row["slug"], created))
        task["_latest_turn"] = max(task["_latest_turn"], _round(row.get("turn")))
        if phase == "turn":
            # Final supervisor results carry no additional stage cost or usage.
            task["_turns"].append(row)
            task["rounds"] = max(task["rounds"], _round(row.get("round")))
            continue
        is_turn = phase in MODEL_PHASES
        seconds = _number(row.get("active_seconds")) or 0
        active_seconds += seconds
        task["active_minutes"] += seconds / 60
        task["rounds"] = max(task["rounds"], _round(row.get("round")))
        _add_tokens(tokens, row.get("tokens"))
        _add_tokens(task["tokens"], row.get("tokens"))
        if is_turn:
            turns += 1
            task["turns"] += 1
            # Normal wrap-up success/failure retains the parent budget/stuck
            # outcome. Cancellation or a policy refusal stops the whole task.
            if ((phase != "wrapup" or row.get("outcome") in ("stopped", "refused"))
                    and _identifier(row.get("outcome"))):
                task["outcome"] = row["outcome"]
        if phase == "scout":
            task["result_status"] = None
        elif phase in ("work", "fix") or (phase == "wrapup" and row.get("kind") == "work"):
            # A new working stage supersedes its predecessor even when it has
            # no structured result. Checks/reviews have no working result to
            # replace; review/scout wrap-ups must preserve that distinction.
            task["result_status"] = _identifier(row.get("result_status"))

        usage = row.get("usage")
        segments = [entry for entry in usage if isinstance(entry, dict)] if isinstance(usage, list) else []
        if not segments:
            segments = [row]
        touched = set()
        for segment in segments:
            delta = _weekly_delta(segment)
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

        if phase in CHECK_PHASES:
            outcome = row.get("check_outcome")
            if outcome in ("passed", "failed", "timeout", "stopped"):
                checks["total"] += 1
                checks["passed"] += outcome == "passed"
            # Ordinary check failures leave the model's task outcome intact;
            # cancellation stops the whole task, including during a base check.
            if outcome == "stopped" or row.get("outcome") == "stopped":
                task["outcome"] = "stopped"
        if phase == "review":
            group = (_round(row.get("turn")), _round(row.get("round")))
            review_rows = task["_reviews"].setdefault(group, [])
            review_rows.append(row)
            task["outcome"] = _review_outcome(review_rows)
            if row.get("outcome") == "completed" and row.get("verdict") in ("clean", "defects"):
                reviews["total"] += 1
                reviews["with_defects"] += row["verdict"] == "defects"
                findings = _number(row.get("findings"))
                if findings is not None:
                    reviews["findings"] += findings
                    findings_samples += 1

    fix_rounds = Counter()
    outcomes = Counter()
    result_status = Counter()
    for task in tasks.values():
        groups = list(task.pop("_reviews").items())
        verdicts = {group: _review_verdict(review_rows) for group, review_rows in groups}
        final_turns = task.pop("_turns")
        for row in final_turns:
            if row.get("kind") == "review":
                verdicts[(_round(row.get("turn")), _round(row.get("round")))] = (
                    _identifier(row.get("verdict")) if row.get("outcome") == "completed" else None)
        for (_, round_number), verdict in verdicts.items():
            if verdict == "clean":
                fix_rounds[str(round_number)] += 1
                break
        latest_turn = task.pop("_latest_turn")
        # A newer turn may have saved its stages but lost its final journal row.
        if final_turns and _round(final_turns[-1].get("turn")) == latest_turn:
            final = final_turns[-1]
            task["outcome"] = _identifier(final.get("outcome"))
            task["result_status"] = _identifier(final.get("result_status"))
            task["last_verdict"] = _identifier(final.get("verdict"))
        elif groups:
            task["last_verdict"] = _review_verdict(groups[-1][1])
        if task["outcome"] is not None:
            outcomes[task["outcome"]] += 1
        if task["result_status"] is not None:
            result_status[task["result_status"]] += 1
        task["tokens"] = _token_summary(task["tokens"])

    for account in accounts.values():
        account["tokens"] = _token_summary(account["tokens"])
    task_rows = sorted(tasks.values(), key=lambda task: (-task["weekly_used_percent"],
                                                        -task["tokens"]["total"],
                                                        task["repo"], task["slug"],
                                                        -(task["created"] or 0)))
    account_rows = sorted(accounts.values(), key=lambda account: account["account"])
    return {
        "since": _utc(since_at), "until": _utc(now), "filters": {"repo": repo, "slug": slug},
        "summary": {
            "tasks": len(tasks), "turns": turns, "active_hours": active_seconds / 3600,
            "tokens": _token_summary(tokens),
            "weekly_used_percent": sum(task["weekly_used_percent"] for task in task_rows),
            "checks": {"total": checks["total"], "passed": checks["passed"],
                       "pass_fraction": _fraction(checks["passed"], checks["total"])},
            "reviews": {"total": reviews["total"], "with_defects": reviews["with_defects"],
                        "defect_fraction": _fraction(reviews["with_defects"], reviews["total"]),
                        "findings": reviews["findings"],
                        "average_findings": _fraction(reviews["findings"], findings_samples)},
            "fix_rounds_to_clean": dict(sorted(fix_rounds.items(), key=lambda item: int(item[0]))),
            "outcomes": dict(sorted(outcomes.items())), "result_status": dict(sorted(result_status.items())),
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
    reviews = summary["reviews"]
    average = reviews["average_findings"]
    lines = [
        f"Task report: {report['since']} to {report['until']}",
        f"Tasks: {summary['tasks']}  Turns: {summary['turns']}  Active hours: {summary['active_hours']:.2f}",
        f"Tokens: {tokens['total']:,.0f} (input {tokens['input']:,.0f}, output {tokens['output']:,.0f}, "
        f"reasoning {tokens['reasoning']:,.0f}, cached input {tokens['cached_input']:,.0f}; "
        f"cache share {_percent(tokens['cached_input_fraction'])})",
        f"Checks passed: {checks['passed']}/{checks['total']} ({_percent(checks['pass_fraction'])})",
        f"Reviews with defects: {reviews['with_defects']}/{reviews['total']} "
        f"({_percent(reviews['defect_fraction'])}); average findings: "
        + ("n/a" if average is None else f"{average:.2f}"),
        "Fix rounds to clean: " + _distribution(summary["fix_rounds_to_clean"]),
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
    lines.extend(("", "Tasks by cost:", "slug  turns  rounds  minutes  tokens  weekly %  last verdict"))
    for task in report["tasks"]:
        lines.append(f"{task['slug']}  {task['turns']}  {task['rounds']}  {task['active_minutes']:.1f}  "
                     f"{task['tokens']['total']:,.0f}  {task['weekly_used_percent']:.2f}  "
                     f"{task['last_verdict'] or '-'}")
    if not report["tasks"]:
        lines.append("(no tasks)")
    return "\n".join(lines)
