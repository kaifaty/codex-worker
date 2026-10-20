"""One isolated Claude Code review, with no persisted CLI output or credentials."""
import json
import math
import os
import selectors
import subprocess
from pathlib import Path

from . import clock, config, procs, result_schema, task_state


def worktree_snapshot(st):
    return tuple(procs.sh(config.GIT + args, cwd=st["worktree"], errors="surrogateescape")
                 for args in (["rev-parse", "HEAD"], ["status", "--porcelain"]))


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def save_limits(info):
    """Keep only documented limit fields, never arbitrary stream text."""
    selected = {}
    if info.get("status") in ("allowed", "allowed_warning", "rejected"):
        selected["status"] = info["status"]
    for key in ("utilization", "resetsAt"):
        if numeric(info.get(key)):
            selected[key] = info[key]
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        selected["unifiedWindows"] = {
            name: {key: window[key] for key in ("utilization", "resetsAt")
                   if numeric(window.get(key))}
            for name in ("five_hour", "seven_day")
            for window in [windows.get(name)] if isinstance(window, dict)}
    path = config.TASKS / "claude-limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".%s.tmp" % os.getpid())
    temporary.write_text(json.dumps(selected), encoding="utf-8")
    temporary.replace(path)


def event_result(event, telemetry, token):
    """Decode selected stream fields; return (result, unavailability reason)."""
    if not isinstance(event, dict):
        return None, "invalid JSON output"
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        for key in ("session_id", "model"):
            value = event.get(key)
            if isinstance(value, str) and token not in value:
                telemetry[key] = value
    if ((kind == "system" and event.get("subtype") == "api_retry") or kind == "result"):
        if event.get("error_status", event.get("api_error_status")) in (401, 403):
            return None, "authentication failed"
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        if isinstance(info, dict):
            save_limits(info)
            if info.get("status") == "rejected":
                return None, "usage limit"
    if kind != "result":
        return None, None
    usage = event.get("usage")
    if isinstance(usage, dict):
        telemetry["usage"] = {key: usage[key] for key in
                              ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                               "cache_read_input_tokens") if numeric(usage.get(key))}
    if numeric(event.get("total_cost_usd")):
        telemetry["cost_usd"] = event["total_cost_usd"]
    if type(event.get("num_turns")) is int:
        telemetry["num_turns"] = event["num_turns"]
    if event.get("is_error"):
        return None, "review failed"
    result = event.get("structured_output")
    if not result_schema.matches_schema(result, result_schema.REVIEW_SCHEMA):
        return None, "invalid structured output"
    # A tool may echo its environment. Even a schema-valid answer must not
    # carry the credential into state, review files, or telemetry.
    serialized = json.dumps(result)
    if token in serialized:
        result = json.loads(serialized.replace(token, "[REDACTED]"))
    return result, None


def run(st, cfg, prompt, stopped):
    """Run one review angle; the caller owns review history and phase telemetry."""
    response = {"outcome": "unavailable", "result": None, "reason": None,
                "telemetry": {"usage": {}, "cost_usd": None, "session_id": None,
                              "model": None, "num_turns": None}}
    if stopped():
        response["outcome"] = task_state.stop_outcome(st, stopped)
        return response
    executable = config.claude_binary()
    if executable is None:
        response["reason"] = "Claude CLI was not found"
        return response
    settings = cfg["claude"]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CLAUDE", "ANTHROPIC"))}
    environment["CLAUDE_CONFIG_DIR"] = str(Path(settings["config_dir"]).expanduser())
    args = [executable, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(result_schema.REVIEW_SCHEMA),
            "--permission-mode", "dontAsk", "--allowedTools", "Read,Grep,Glob,Bash",
            "--disallowedTools", "Edit,Write,NotebookEdit",
            "--max-turns", str(settings["max_turns"])]
    for key in ("model", "effort"):
        if settings.get(key):
            args += ["--" + key, settings[key]]
    before = worktree_snapshot(st)
    # Read immediately before launch. Neither subprocess argv, stderr nor any
    # task artifact contains the token or the raw output of either process.
    try:
        credential = subprocess.run(
            ["security", "find-generic-password", "-s", settings["token_keychain"],
             "-a", os.environ.get("USER", ""), "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=config.ACCOUNT_TIMEOUT, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        response["reason"] = "reviewer token unavailable"
        return response
    token = credential.stdout.strip()
    if credential.returncode or not token:
        response["reason"] = "reviewer token unavailable"
        return response
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if stopped():
        response["outcome"] = task_state.stop_outcome(st, stopped)
        return response
    try:
        proc = subprocess.Popen(args, cwd=st["worktree"], env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        response["reason"] = "Claude CLI could not be started"
        return response
    started = clock.CLOCK.monotonic()
    minutes = cfg["max_review_minutes"]
    st["codex_pid"] = proc.pid
    buffer, eof = b"", False
    try:
        task_state.save(st)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while not eof or proc.poll() is None:
                if stopped():
                    response["outcome"] = task_state.stop_outcome(st, stopped)
                    break
                if clock.CLOCK.monotonic() - started >= minutes * config.MINUTE_SECONDS:
                    response["outcome"] = "budget_exhausted"
                    st["turn_stop"] = {"outcome": "budget_exhausted", "minutes": minutes}
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
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            response["reason"] = "invalid JSON output"
                            break
                        result, reason = event_result(event, response["telemetry"], token)
                        if result is not None:
                            response["result"] = result
                        if reason is not None:
                            response["reason"] = reason
                            break
                    if response["reason"]:
                        break
                if response["reason"]:
                    break
            else:
                if proc.wait() != 0:
                    response["reason"] = "review failed"
                elif response["result"] is None:
                    response["reason"] = "invalid structured output"
                else:
                    response["outcome"] = "completed"
    finally:
        procs.terminate(proc)
        proc.stdout.close()
        st.pop("codex_pid", None)
        task_state.save(st)
    if worktree_snapshot(st) != before:
        response.update(outcome="failed", result=None, reason="reviewer modified the worktree")
    return response
