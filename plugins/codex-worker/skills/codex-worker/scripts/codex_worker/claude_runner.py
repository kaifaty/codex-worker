"""Isolated Claude Code transport shared by reviews and writable work turns."""
import json
import os
import selectors
import subprocess
import threading
from pathlib import Path

from . import claude_accounts, clock, config, procs, result_schema, sandboxing


def account_environment(account, timeout):
    """Read an isolated account's token without exposing credential output."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CLAUDE", "ANTHROPIC"))}
    environment["CLAUDE_CONFIG_DIR"] = str(Path(account["config_dir"]).expanduser())
    environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    try:
        credential = subprocess.run(
            ["security", "find-generic-password", "-s", account["token_keychain"],
             "-a", os.environ.get("USER", ""), "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=timeout, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return environment, ""
    token = credential.stdout.strip() if credential.returncode == 0 else ""
    if token:
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return environment, token


def stream_unavailable(event, account):
    """Persist safe limit fields and recognize account availability errors."""
    kind = event.get("type")
    if (kind == "result" or kind == "system" and event.get("subtype") == "api_retry"):
        if event.get("error_status", event.get("api_error_status")) in (401, 403):
            return "authentication failed"
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        if isinstance(info, dict):
            claude_accounts.save_limits(account, info, event.get("timestamp"))
            if info.get("status") == "rejected":
                return "usage limit"
    return None


def event_result(event, telemetry, token, account, reviewing=True):
    """Decode selected stream fields; return (result, unavailability reason)."""
    if not isinstance(event, dict):
        return None, "invalid JSON output"
    kind = event.get("type")
    if (kind == "system" and event.get("subtype") == "init"
            or not reviewing and kind == "result"):
        for key in ("session_id", "model"):
            value = event.get(key)
            if isinstance(value, str) and token not in value:
                telemetry[key] = value
    if kind != "result":
        if not reviewing and kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            windows = info.get("unifiedWindows") if isinstance(info, dict) else None
            window = windows.get("seven_day") if isinstance(windows, dict) else None
            if isinstance(window, dict):
                used, reset = window.get("utilization"), window.get("resetsAt")
                if (result_schema.is_finite_number(used) and 0 <= used <= 1
                        and result_schema.is_finite_number(reset) and reset > clock.CLOCK.time()):
                    telemetry.setdefault("weekly_used_before", used * 100)
                    telemetry.setdefault("weekly_reset_before", reset)
                    telemetry["weekly_used_after"] = used * 100
                    telemetry["weekly_reset_after"] = reset
        return None, stream_unavailable(event, account)
    usage = event.get("usage")
    if isinstance(usage, dict):
        telemetry["usage"] = {key: usage[key] for key in
                              ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                               "cache_read_input_tokens") if result_schema.is_finite_number(usage.get(key))}
    if result_schema.is_finite_number(event.get("total_cost_usd")):
        telemetry["cost_usd"] = event["total_cost_usd"]
    if type(event.get("num_turns")) is int:
        telemetry["num_turns"] = event["num_turns"]
    reason = stream_unavailable(event, account)
    if reason:
        return None, reason
    if event.get("is_error"):
        return None, "review failed" if reviewing else "work turn failed"
    result = event.get("structured_output")
    schema = result_schema.REVIEW_SCHEMA if reviewing else result_schema.RESULT_SCHEMA
    if not result_schema.matches_schema(result, schema):
        return None, "invalid structured output"
    # A tool may echo its environment. Even a schema-valid answer must not
    # carry the credential into state, review files, or telemetry.
    serialized = json.dumps(result)
    if token in serialized:
        result = json.loads(serialized.replace(token, "[REDACTED]"))
    return result, None


def validate_work_effort(effort):
    if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("Invalid Claude effort: expected low, medium, high, xhigh, or max")


def run_work_turn(account, worktree, task_dir, prompt, stopped, session_id=None,
                  model=None, effort=None, max_turns=None, deadline=None, started=None, progress=None):
    """Run a writable turn without supervisor task state.

    The caller supplies an existing task directory and a monotonic deadline.
    An unparsed result returns outcome ``unknown``, result ``None``, and the
    last answer in ``raw_message``. Session and usage fields are in telemetry.
    """
    validate_work_effort(effort)
    system_prompt = sandboxing.claude_system_prompt(worktree, task_dir)
    try:
        return run_claude(account, worktree, prompt, stopped, deadline, model, effort, max_turns,
                          session_id=session_id, system_prompt=system_prompt, started=started, progress=progress,
                          task_dir=task_dir)
    finally:
        system_prompt.unlink(missing_ok=True)


def run_claude(account, worktree, prompt, stopped, deadline=None, model=None, effort=None,
               max_turns=None, *, session_id=None, system_prompt=None, reviewing=False, started=None, progress=None,
               task_dir=None):
    """Execute and drain one stream; callers own role-specific lifecycle state."""
    response = {"outcome": "unavailable", "result": None, "reason": None, "deadline": deadline,
                "telemetry": {"usage": {}, "cost_usd": None, "session_id": None,
                              "model": None, "reasoning_effort": None, "num_turns": None}}
    if not reviewing:
        response["raw_message"] = ""
    if stopped():
        response["outcome"] = "stopped"
        return response
    if deadline is not None and clock.CLOCK.monotonic() >= deadline:
        response["outcome"] = "budget_exhausted"
        return response
    executable = config.claude_binary()
    if executable is None:
        response["reason"] = "Claude CLI was not found"
        return response
    schema = result_schema.REVIEW_SCHEMA if reviewing else result_schema.RESULT_SCHEMA
    args = [executable, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(schema)]
    if reviewing:
        args += ["--permission-mode", "dontAsk", "--allowedTools", "Read,Grep,Glob,Bash",
                 "--disallowedTools", "Edit,Write,NotebookEdit"]
    else:
        args += ["--permission-mode", "bypassPermissions", "--strict-mcp-config",
                 "--append-system-prompt-file", str(system_prompt)]
    if max_turns is not None:
        args += ["--max-turns", str(max_turns)]
    for key, value in (("model", model), ("effort", effort)):
        if value:
            args += ["--" + key, value]
    if session_id is not None:
        args += ["--resume", session_id]
    # Read immediately before launch. Credential output never reaches artifacts.
    remaining = config.ACCOUNT_TIMEOUT if deadline is None else deadline - clock.CLOCK.monotonic()
    if remaining <= 0:
        response["outcome"] = "budget_exhausted"
        return response
    environment, token = account_environment(account, min(config.ACCOUNT_TIMEOUT, remaining))
    if stopped():
        response["outcome"] = "stopped"
        return response
    if deadline is not None and clock.CLOCK.monotonic() >= deadline:
        response["outcome"] = "budget_exhausted"
        return response
    if not token:
        response["reason"] = "token unavailable"
        return response
    claude_accounts.token_available(account["name"])
    if stopped():
        response["outcome"] = "stopped"
        return response
    try:
        proc = subprocess.Popen(args, cwd=worktree, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        response["reason"] = "Claude CLI could not be started"
        return response
    response["telemetry"].update(model=model, reasoning_effort=effort)
    buffer, eof, termination = b"", False, None
    events_failed = False
    failure = "review failed" if reviewing else "work turn failed"
    try:
        if started is not None:
            started(proc)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                if stopped():
                    response["outcome"] = "stopped"
                    break
                if deadline is not None and clock.CLOCK.monotonic() >= deadline:
                    response["outcome"] = "budget_exhausted"
                    break
                if eof and proc.poll() is not None:
                    if (not reviewing and proc.wait() != 0 and response["reason"] in
                            (None, "invalid structured output", "invalid JSON output", failure)):
                        response["reason"] = failure
                    if response["reason"] is None:
                        if proc.wait() != 0:
                            response["reason"] = failure
                        elif response["result"] is None:
                            response["reason"] = "invalid structured output"
                        else:
                            response["outcome"] = "completed"
                    if not reviewing and response["reason"] in (
                            "invalid structured output", "invalid JSON output", failure):
                        response.update(outcome="unknown", result=None)
                    break
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        eof = True
                    buffer += chunk
                    while b"\n" in buffer or (eof and buffer):
                        line, separator, remaining = buffer.partition(b"\n")
                        buffer = remaining if separator else b""
                        if separator and task_dir is not None and not events_failed:
                            try:
                                with (Path(task_dir) / "claude-events.jsonl").open("ab") as events:
                                    events.write(line + separator)
                                    events.flush()
                            except OSError:
                                events_failed = True
                                try:
                                    with (Path(task_dir) / "stderr.log").open("a", encoding="utf-8") as errors:
                                        errors.write("Could not append to claude-events.jsonl.\n")
                                except OSError:
                                    pass
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            if not reviewing:
                                with (Path(system_prompt).parent / "stderr.log").open(
                                        "a", encoding="utf-8") as errors:
                                    errors.write(line.decode("utf-8", errors="replace").replace(
                                        token, "[REDACTED]") + "\n")
                                continue
                            response["reason"] = response["reason"] or "invalid JSON output"
                            response["result"] = None
                            continue
                        if not reviewing and isinstance(event, dict):
                            if event.get("type") == "assistant":
                                message = event.get("message", {})
                                content = message.get("content") if isinstance(message, dict) else None
                                if isinstance(content, list):
                                    texts = [item["text"] for item in content
                                             if isinstance(item, dict) and item.get("type") == "text"
                                             and isinstance(item.get("text"), str)]
                                    if texts:
                                        response["raw_message"] = "\n".join(texts).replace(token, "[REDACTED]")
                            elif event.get("type") == "result" and isinstance(event.get("result"), str):
                                if event["result"]:
                                    response["raw_message"] = event["result"].replace(token, "[REDACTED]")
                        result, reason = event_result(event, response["telemetry"], token,
                                                      account["name"], reviewing)
                        if reason is not None:
                            response["reason"] = response["reason"] or reason
                            response["result"] = None
                        elif result is not None and response["reason"] is None:
                            response["result"] = result
                        if not reviewing and progress is not None:
                            progress(response["telemetry"])
                if (response["reason"] and termination is None and
                        (reviewing or response["reason"] not in
                         ("invalid structured output", "invalid JSON output", failure))):
                    # Drain buffered and shutdown usage while terminating, even
                    # when the final result is larger than the stdout pipe.
                    termination = threading.Thread(target=procs.terminate, args=(proc,))
                    termination.start()
    finally:
        if termination is None:
            procs.terminate(proc)
        else:
            termination.join()
        proc.stdout.close()
    return response
