"""Thread events, rollout usage and continuation messages."""
import json
import math
import os
from pathlib import Path
import shlex

from . import accounts, clock, config, procs, result_schema, sandboxing


def effort_args(effort):
    return ["-c", f'model_reasoning_effort="{effort}"'] if effort else []


def events(st):
    p = config.TASKS / st["slug"] / "events.jsonl"
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def thread_messages(st):
    msgs, thread = [], ""
    for e in events(st):
        if e.get("type") == "thread.started":
            thread = e["thread_id"]
        if thread == st.get("thread") and e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            msgs.append(e["item"].get("text", ""))
    return msgs


def last_message(st):
    msgs = thread_messages(st)
    return msgs[-1] if msgs else ""


def followup_messages(st):
    messages = config.TASKS / st["slug"] / "messages.jsonl"
    return [json.loads(line) for line in messages.read_text().splitlines()] if messages.exists() else []


def followup_prompt(st, message, cfg, source="claude"):
    """Journal follow-ups before dispatch so rotation includes the current request."""
    with (config.TASKS / st["slug"] / "messages.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": clock.CLOCK.time(), "text": message, "source": source},
                                ensure_ascii=False) + "\n")
    if st.get("thread"):
        switch = st.get("base_switch")
        if switch:
            message += f"\n\nTask base is now {st['base']}; use it instead of {switch['from']}."
        if st.get("no_push"):
            message += ("\n\nКоммить локально. Не публикуй ветку и не открывай PR." if cfg.get("language", "en") == "ru" else
                        "\n\nCommit locally. Do not publish the branch or open a PR.")
        return message
    return handoff(st, current_message=True, cfg=cfg)


def rollout_path(home, thread):
    if not thread:
        return None
    paths = list((Path(home) / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def rollout_limits(path, on_limits=None, offset=0):
    """Replay this exec's quota samples; otherwise read only the latest tail."""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(offset if on_limits else max(0, size - 262144))
        lines = f.read().splitlines()
    limits = None
    for event in config.ROLLOUT["read_events"](lines if on_limits else reversed(lines)):
        try:
            sample = config.ROLLOUT["decode_token_count"](event)
            raw = sample.rate_limits if sample is not None and sample.event_type == "event_msg" else None
            parsed = accounts.normalize_limits(raw) if raw else None
        except (ValueError, TypeError, AttributeError):
            continue
        if parsed is not None:
            limits = parsed
            if on_limits:
                on_limits(limits)
            else:
                return limits
    return limits


def rollout_usage(path, offset=0):
    """First/last cumulative counters, using the same token_count data as stats."""
    first = last = None
    if path is not None:
        try:
            with path.open("rb") as stream:
                for event in config.ROLLOUT["read_events"](stream, offset):
                    try:
                        decoded = config.ROLLOUT["decode_token_count"](event)
                        if decoded is None:
                            continue
                        windows = [window for window in decoded.windows if isinstance(window, dict)]
                        window = max(windows, key=lambda w: w.get("window_minutes") or 0, default={})
                        used = window.get("used_percent")
                        # A short window cannot stand in for the weekly quota.
                        weekly = used if (window.get("window_minutes") or 0) >= 10080 else None
                        sample = {"tokens": decoded.tokens, "weekly": None}
                        if type(weekly) in (int, float) and math.isfinite(weekly) and 0 <= weekly <= 100:
                            sample["weekly"] = weekly
                        if first is None:
                            first = {"tokens": dict(sample["tokens"]), "weekly": sample["weekly"]}
                            last = {"tokens": dict(sample["tokens"]), "weekly": sample["weekly"]}
                        # Rate-limit-only notifications must not erase cumulative usage.
                        for key, value in sample["tokens"].items():
                            if value is not None:
                                if first["tokens"][key] is None:
                                    first["tokens"][key] = value
                                last["tokens"][key] = value
                        if sample["weekly"] is not None:
                            if first["weekly"] is None:
                                first["weekly"] = sample["weekly"]
                            last["weekly"] = sample["weekly"]
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        continue
        except OSError:
            pass
    return first, last


def handoff(st, current_message=False, cfg=None):
    cfg = config.accounts_config() if cfg is None else cfg
    ru = cfg.get("language", "en") == "ru"
    scouting = st.get("kind") == "scout"
    d = config.TASKS / st["slug"]
    prompt = (d / "brief.md").read_text().rstrip() + ("\n\n## Продолжение\n\n" if ru else "\n\n## Continuation\n\n")
    has_threads = bool(st.get("threads") or st.get("thread"))
    if has_threads and scouting:
        prompt += (("Предыдущий разведчик остановился из-за лимита подписки. "
                    "Продолжи изучение вопроса с учётом его сообщений ниже.\n\n") if ru else
                   ("The previous scout stopped because its subscription reached its limit. "
                    "Continue investigating the question using its messages below.\n\n"))
    elif has_threads:
        prompt += (("Предыдущий исполнитель остановился посреди работы, потому что у его подписки кончился лимит.\n"
                    "Его коммиты и незакоммиченные правки в этом worktree — отправная точка. "
                    "Не начинай заново и не откатывай сделанную работу.\n\n") if ru else
                   ("The previous worker stopped partway through the task because its subscription reached its limit.\n"
                    "Its commits and uncommitted changes in this worktree are your starting point. "
                    "Continue the existing work without starting over or reverting it.\n\n"))
    prompt += "Сообщения от Claude и ревью по порядку:\n" if ru else "Messages from Claude and review, in order:\n"
    messages = followup_messages(st)
    for n, message in enumerate(messages, 1):
        text = message["text"]
        if current_message and n == len(messages):
            heading = "Текущее сообщение — выполни его" if ru else "Current message — carry it out"
            prompt += f"\n### {heading}\n\n{text}\n"
        else:
            prompt += f"\n{n}. {text}\n"
    if not scouting:
        prompt += f"\n{'Коммиты' if ru else 'Commits'} (git log --oneline {st['base']}..HEAD):\n"
        prompt += procs.sh(config.GIT + ["log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False) or ("(нет)" if ru else "(none)")
        prompt += ("\n\nСостояние worktree (git status --short):\n" if ru else "\n\nWorktree status (git status --short):\n")
        driver = sandboxing.workspace_filter_driver(st)
        if driver is not None:
            prompt += (f"(пропущено: настроен фильтр {driver})" if ru else
                       f"(skipped: filter driver {driver} is configured)")
        else:
            prompt += procs.sh(config.GIT + ["status", "--short"], cwd=st["worktree"], check=False) or ("(чисто)" if ru else "(clean)")
    if has_threads:
        label = (("Последние сообщения предыдущего разведчика" if ru else "Latest messages from the previous scout")
                 if scouting else ("Последние сообщения предыдущего исполнителя" if ru else "Latest messages from the previous worker"))
        messages = []
        for message in thread_messages(st)[-3:]:
            result = None if scouting else result_schema.parse_result(message)
            if result is not None:
                message = result["summary"]
                if result["remaining"]:
                    message += ("\nОсталось:\n" if ru else "\nRemaining:\n") + "\n".join(result["remaining"])
            messages.append(message)
        prompt += f"\n\n{label}:\n" + "\n\n".join(messages)[-4000:]
    if not scouting:
        prompt += (f"\n\nСначала посмотри git status и git diff $(git merge-base {shlex.quote(st['base'])} HEAD), потом продолжай работу.\n" if ru else
                   f"\n\nFirst inspect git status and git diff $(git merge-base {shlex.quote(st['base'])} HEAD), then continue the task.\n")
    if st.get("no_push") and st.get("kind") not in ("review", "scout"):
        prompt += ("\nКоммить локально. Не публикуй ветку и не открывай PR.\n" if ru else
                   "\nCommit locally. Do not publish the branch or open a PR.\n")
    switch = st.get("base_switch")
    if switch:
        prompt += f"\nTask base is now {st['base']}; use it instead of {switch['from']}.\n"
    return prompt
