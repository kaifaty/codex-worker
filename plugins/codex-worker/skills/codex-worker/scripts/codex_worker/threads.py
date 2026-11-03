"""Thread events, rollout usage and continuation messages."""
import json
import os
from pathlib import Path
import re
import shlex

from . import accounts, clock, config, procs, result_schema, sandboxing

try:
    import tomllib
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False


def effort_args(effort):
    return ["-c", f'model_reasoning_effort="{effort}"'] if effort else []


def config_string(value):
    """Read a single TOML string setting without depending on Python 3.11."""
    match = re.fullmatch(r'''("(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#.*)?''', value.strip())
    if match is None:
        return None
    quoted = match[1]
    try:
        return quoted[1:-1] if quoted.startswith("'") else json.loads(quoted)
    except ValueError:
        return None


def configured_model_settings(home, args):
    """Best-effort fallback for only the selected model, effort and profile."""
    root, profiles, section = {}, {}, ""
    try:
        text = (Path(home) / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = ""
    if TOML_AVAILABLE:
        try:
            root = tomllib.loads(text)
            profiles = root.get("profiles", {})
            profiles = profiles if isinstance(profiles, dict) else {}
        except ValueError:
            pass
        lines = []
    else:
        # The Python 3.9 fallback accepts only simple string settings. Refuse
        # multiline strings rather than mistake embedded prompt text for keys.
        lines = [] if '"""' in text or "'''" in text else text.splitlines()
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            table = re.fullmatch(r'''\[\s*profiles\s*\.\s*([A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')\s*\]\s*(?:#.*)?''', line)
            section = (config_string(table[1]) if table[1].startswith(("'", '"')) else table[1]) if table else None
            continue
        key, separator, raw = line.partition("=")
        key = key.strip()
        if section is None or not separator or key not in ("model", "review_model", "model_reasoning_effort", "profile"):
            continue
        target = root if section == "" else profiles.setdefault(section, {})
        target[key] = config_string(raw)
    overrides, cli_model = {}, None
    for index, arg in enumerate(args[:-1]):
        if arg == "--model":
            cli_model = args[index + 1]
        elif arg in ("-c", "--config") and index + 1 < len(args) - 1:
            key, separator, raw = args[index + 1].partition("=")
            if separator and key in ("model", "review_model", "model_reasoning_effort", "profile"):
                overrides[key] = config_string(raw) if raw.strip().startswith(("'", '"')) else raw.strip()
    selected = dict(root)
    profile = overrides.get("profile", root.get("profile"))
    profile = profiles.get(profile, {}) if isinstance(profile, str) else {}
    if isinstance(profile, dict):
        selected.update(profile)
    selected.update(overrides)
    if cli_model is not None:
        selected["model"] = cli_model
    review_model = selected.get("review_model")
    review_model = review_model if isinstance(review_model, str) and review_model else None
    model = (review_model or selected.get("model")
             if args[1:3] == ["exec", "review"] else selected.get("model"))
    effort = selected.get("model_reasoning_effort")
    return {"model": model if isinstance(model, str) and model else None,
            "reasoning_effort": effort if isinstance(effort, str) and effort else None,
            "review_model": review_model}


def rollout_model_settings(path, offset=0):
    """Read authoritative settings only from contexts appended by this exec."""
    settings = {}
    if path is None:
        return settings
    try:
        with path.open("rb") as stream:
            for event in config.ROLLOUT["read_events"](stream, offset):
                if not isinstance(event, dict) or event.get("type") != "turn_context":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                mode = payload.get("collaboration_mode")
                nested = mode.get("settings") if isinstance(mode, dict) else None
                nested = nested if isinstance(nested, dict) else {}
                values = {"model": nested.get("model") or payload.get("model"),
                          "reasoning_effort": nested.get("reasoning_effort") or payload.get("effort")}
                settings.update({key: value for key, value in values.items()
                                 if isinstance(value, str) and value})
    except (OSError, UnicodeError):
        pass
    return settings


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
    if st.get("executor") == "claude":
        last = config.TASKS / st["slug"] / "last.md"
        return last.read_text(encoding="utf-8").strip() if last.exists() else ""
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
    if st.get("thread") or st.get("session_id"):
        switch = st.get("base_switch")
        if switch:
            message += (f"\n\nТеперь база задачи — {st['base']}; используй её вместо {switch['from']}."
                        if cfg.get("language", "en") == "ru" else
                        f"\n\nTask base is now {st['base']}; use it instead of {switch['from']}.")
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
                        if result_schema.is_finite_number(weekly) and 0 <= weekly <= 100:
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


def claude_messages(st):
    """Final messages of the task's recent Claude attempts, oldest first."""
    try:
        messages = json.loads((config.TASKS / st["slug"] / "claude-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [message for message in messages if isinstance(message, str)] if isinstance(messages, list) else []


def record_claude_message(st, message):
    path = config.TASKS / st["slug"] / "claude-messages.json"
    path.write_text(json.dumps((claude_messages(st) + [message])[-3:], ensure_ascii=False), encoding="utf-8")


def handoff(st, current_message=False, cfg=None):
    """Continuation brief for a new Codex thread or Claude session on another account."""
    cfg = config.accounts_config() if cfg is None else cfg
    ru = cfg.get("language", "en") == "ru"
    scouting = st.get("kind") == "scout"
    d = config.TASKS / st["slug"]
    prompt = (d / "brief.md").read_text().rstrip() + ("\n\n## Продолжение\n\n" if ru else "\n\n## Continuation\n\n")
    claude = st.get("executor") == "claude" and st.get("kind") not in ("review", "scout")
    has_threads = bool(st.get("threads") or st.get("thread") or claude and st.get("session_id"))
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
        for message in (claude_messages(st) if claude else thread_messages(st))[-3:]:
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
        prompt += (f"\nТеперь база задачи — {st['base']}; используй её вместо {switch['from']}.\n" if ru else
                   f"\nTask base is now {st['base']}; use it instead of {switch['from']}.\n")
    return prompt
