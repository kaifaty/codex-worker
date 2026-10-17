#!/usr/bin/env python3
"""Delegate coding to Codex in a git worktree, with autonomous subscription rotation.

  codex_task.py start <slug> --brief FILE|- [--repo DIR] [--base origin/<branch>] [--effort E] [--parallel] [--max-minutes M] [--review [--fix-rounds N]] [--check CMD] [--check-timeout SEC] [--guard GLOB] [--base-check]
  codex_task.py scout <slug> "question" | --file FILE [--repo DIR] [--effort E]  read-only research
  codex_task.py say <slug> "message" | --file FILE [--max-minutes M] [--review [--fix-rounds N]] [--check CMD | --no-check] [--check-timeout SEC] [--no-base-check]
  codex_task.py review <slug> [--effort E]             fresh review (task must be idle)
  codex_task.py wait <slug> [--timeout SEC] [--through-sleep]  wait, show rotations and the result
  codex_task.py status [<slug>]                       state, account and current thread
  codex_task.py stats <slug>                          codex-stats.py for every task thread
  codex_task.py stop <slug>                           interrupt without rotating
  codex_task.py clean <slug> [--keep-branch]          remove worktree, archive all threads
  codex_task.py accounts                             read account limits and login status
  codex_task.py reset-credit <account> [--credit ID] [--yes]  preview/use a free limit reset
  codex_task.py login <name> [--browser]              add/sign in to an isolated account

~/.codex/codex-worker.json optionally configures language (en), accounts, switch_at (99),
credits (false), wait_for_reset (true), max_turn_minutes (no cap), max_review_minutes (30),
repeat_limit (3), notify and notify_events. Legacy claude-accounts.json is a fallback.
Without an accounts setting, tasks use main (~/.codex) without quota monitoring or rotation.
State/logs: ~/.codex/claude-tasks/<slug>/; worktree: ~/.codex/worktrees/claude-<slug>/<repo>.
CODEX_TASK_HOME and CODEX_TASK_CODEX override the main home and CLI binary (also for tests).
The internal _run <slug> supervisor survives the caller and opens a new thread on rotation.
"""
import argparse
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

CHATGPT_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
CODEX = os.environ.get("CODEX_TASK_CODEX")
CODEX_HOME = Path(os.environ.get("CODEX_TASK_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
TASKS = CODEX_HOME / "claude-tasks"
WORKTREES = CODEX_HOME / "worktrees"
SKILL = Path(__file__).resolve().parent.parent
STATS = Path(__file__).resolve().parent / "codex-stats.py"
RATE_POLL_SECONDS = 30
ACCOUNT_TIMEOUT = 20
ACCOUNT_RETRY_SECONDS = float(os.environ.get("CODEX_TASK_ACCOUNT_RETRY_SECONDS", "300"))
MAX_ACCOUNT_RETRIES = 6
SLEEP_STEP = float(os.environ.get("CODEX_TASK_SLEEP_STEP", "60"))
RESET_MARGIN = float(os.environ.get("CODEX_TASK_RESET_MARGIN", "120"))
# Tests can shorten minutes without changing the supervisor's monotonic clock.
MINUTE_SECONDS = float(os.environ.get("CODEX_TASK_MINUTE_SECONDS", "60"))
RESET_RETRY_SECONDS = 15 * 60
MAX_RESET_WAIT_SECONDS = 8 * 24 * 60 * 60
NOTIFY_PROCESSES = []


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


RESULT_SCHEMA = object_schema({
    "status": {"type": "string", "enum": ["done", "partial", "blocked", "needs_decision"]},
    "summary": {"type": "string"},
    "checks": {"type": "array", "items": object_schema({
        "command": {"type": "string"},
        "outcome": {"type": "string", "enum": ["passed", "failed", "not_run"]},
        "details": {"type": "string", "description": "Include numerical evidence: counts, exits, attempts."},
    })},
    "pr": {"type": "string", "description": "PR URL, or an empty string."},
    "questions": {"type": "array", "items": {"type": "string"}},
    "remaining": {"type": "array", "items": {"type": "string"}},
    "findings": {"type": "array", "items": object_schema({
        "id": {"type": "string", "pattern": "^F[1-9][0-9]*$"},
        "decision": {"type": "string", "enum": ["fixed", "rejected"]},
        "note": {"type": "string"},
    })},
})
REVIEW_SCHEMA = object_schema({
    "verdict": {"type": "string", "enum": ["clean", "defects"]},
    "findings": {"type": "array", "items": object_schema({
        "id": {"type": "string", "pattern": "^F[1-9][0-9]*$"},
        "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
        "file": {"type": "string"},
        "line": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "verify": {"type": "string"},
    })},
    "summary": {"type": "string"},
})


def matches_schema(value, schema):
    """Validate the small JSON Schema subset used by our output contracts."""
    kind = schema["type"]
    if kind == "object":
        return (isinstance(value, dict) and set(value) == set(schema["required"])
                and all(matches_schema(value[key], field) for key, field in schema["properties"].items()))
    if kind == "array":
        return isinstance(value, list) and all(matches_schema(item, schema["items"]) for item in value)
    if kind == "integer":
        return type(value) is int and value >= schema.get("minimum", value)
    if kind == "string":
        return (isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
                and ("pattern" not in schema or re.fullmatch(schema["pattern"], value) is not None))
    return False


def parse_result(message, reviewing=False):
    try:
        value = json.loads(message)
    except (ValueError, TypeError):
        return None
    return value if matches_schema(value, REVIEW_SCHEMA if reviewing else RESULT_SCHEMA) else None


def result_lines(result, raw="(no final message)"):
    if result is None:
        return ["== result: unknown (unstructured final message)", raw]
    label = {"done": "done", "partial": "PARTIAL", "blocked": "BLOCKED", "needs_decision": "NEEDS DECISION"}
    lines = ["== result: " + label[result["status"]], result["summary"]]
    for check in result["checks"]:
        lines.append(f"- {check['command']}: {check['outcome']} — {check['details']}")
    if result["pr"]:
        lines.append("PR: " + result["pr"])
    if result["questions"]:
        lines.append("Questions:")
        lines.extend(f"{n}. {question}" for n, question in enumerate(result["questions"], 1))
    if result["remaining"]:
        lines.append("Remaining:")
        lines.extend("- " + item for item in result["remaining"])
    return lines


def die(msg):
    sys.exit(f"codex_task: {msg}")


def resolve_codex():
    override = os.environ.get("CODEX_TASK_CODEX")
    if override:
        return str(Path(override).expanduser())
    if CHATGPT_CODEX.is_file():
        return str(CHATGPT_CODEX)
    binary = shutil.which("codex")
    if binary:
        return binary
    die("Codex CLI was not found. Install it with npm install -g @openai/codex, "
        "run codex login, or set CODEX_TASK_CODEX to its executable path.")


def codex_binary():
    return CODEX or resolve_codex()


def sh(cmd, cwd=None, check=True, errors="strict"):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, errors=errors)
    if check and r.returncode != 0:
        die(f"{' '.join(cmd)} failed:\n{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.rstrip()


def accounts_config():
    p = CODEX_HOME / "codex-worker.json"
    if not p.exists():
        p = CODEX_HOME / "claude-accounts.json"
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not isinstance(cfg, dict):
        die("settings must be a JSON object")
    cfg["enabled"] = "accounts" in cfg
    cfg.setdefault("accounts", [{"name": "main", "home": str(CODEX_HOME)}])
    cfg.setdefault("language", "en")
    cfg.setdefault("switch_at", 99)
    cfg.setdefault("credits", False)
    cfg.setdefault("wait_for_reset", True)
    cfg.setdefault("use_reset_credits", False)
    cfg.setdefault("reset_credit_min_hours", 12)
    cfg.setdefault("max_turn_minutes", None)
    cfg.setdefault("max_review_minutes", 30)
    cfg.setdefault("repeat_limit", 3)
    for key in ("max_turn_minutes", "max_review_minutes"):
        value = cfg[key]
        if key == "max_turn_minutes" and value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            die(key + " must be a positive finite number")
    if type(cfg["repeat_limit"]) is not int or cfg["repeat_limit"] < 1:
        die("repeat_limit must be a positive integer")
    if cfg["language"] not in ("en", "ru"):
        die('language must be "en" or "ru"')
    notification = cfg.get("notify")
    if notification is not None and notification != "macos" and not (
            isinstance(notification, list) and notification and
            all(isinstance(arg, str) for arg in notification) and notification[0]):
        die('notify must be "macos" or a nonempty argv list')
    if "notify_events" in cfg and not (isinstance(cfg["notify_events"], list) and
                                       all(isinstance(event, str) for event in cfg["notify_events"])):
        die("notify_events must be a list of event types")
    if not isinstance(cfg["switch_at"], (int, float)) or not 0 < cfg["switch_at"] <= 100:
        die("switch_at must be in (0, 100]")
    if not isinstance(cfg["credits"], bool):
        die("credits must be true or false")
    if not isinstance(cfg["wait_for_reset"], bool):
        die("wait_for_reset must be true or false")
    if not isinstance(cfg["use_reset_credits"], bool):
        die("use_reset_credits must be true or false")
    hours = cfg["reset_credit_min_hours"]
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 0:
        die("reset_credit_min_hours must be a finite nonnegative number")
    names = set()
    homes = set()
    if not isinstance(cfg["accounts"], list) or not cfg["accounts"]:
        die("accounts must be a nonempty list")
    for a in cfg["accounts"]:
        if not isinstance(a, dict):
            die("each account must be an object with name and home")
        name = a.get("name", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,40}", name) or name in names:
            die(f"invalid or duplicate account name: {name!r}")
        if not isinstance(a.get("home"), str) or not a["home"]:
            die(f"account {name!r} must have a home path")
        home = Path(a["home"]).expanduser().resolve()
        if str(home) in homes:
            die(f"duplicate account home: {home}")
        a["home"] = str(home)
        names.add(name)
        homes.add(str(home))
    return cfg


def reap_notifications():
    """Poll only our notification children; never wait for a slow hook."""
    NOTIFY_PROCESSES[:] = [proc for proc in NOTIFY_PROCESSES if proc.poll() is None]


def notify(st, cfg, event, message, **fields):
    reap_notifications()
    cfg = accounts_config() if cfg is None else cfg
    setting = cfg.get("notify")
    if setting is None or ("notify_events" in cfg and event not in cfg["notify_events"]):
        return
    message = st["slug"] + ": " + " ".join(message.splitlines())
    payload = dict(event=event, slug=st["slug"], repo=st["repo"], branch=st["branch"],
                   kind=st.get("kind", "work"), account=st["account"], at=time.time(), message=message)
    payload.update(fields)
    if setting == "macos":
        escaped = message.replace("\\", "\\\\").replace('"', '\\"')
        args = ["osascript", "-e", f'display notification "{escaped}" with title "codex-worker"']
    else:
        args = list(setting) + [json.dumps(payload, ensure_ascii=False)]
    try:
        # The child keeps this descriptor even if clean unlinks the task directory.
        with (TASKS / st["slug"] / "notify.log").open("ab", buffering=0) as log:
            try:
                proc = subprocess.Popen(args, cwd="/", stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, start_new_session=True)
            except (OSError, ValueError) as exc:
                log.write(f"{event}: notification launch failed: {exc}\n".encode("utf-8", errors="replace"))
            else:
                NOTIFY_PROCESSES.append(proc)
    except OSError as exc:
        print(f"notification log failed: {exc}", file=sys.stderr)


def rules_path(cfg=None):
    override = CODEX_HOME / "codex-worker-rules.md"
    if override.is_file():
        return override
    cfg = accounts_config() if cfg is None else cfg
    filename = "rules.ru.md" if cfg.get("language", "en") == "ru" else "rules.md"
    return SKILL / "references" / filename


def rules_text(cfg=None):
    return rules_path(cfg).read_text(encoding="utf-8")


def account_named(cfg, name):
    for a in cfg["accounts"]:
        if a["name"] == name:
            return a
    die(f"account {name!r} is absent from the codex-worker account settings")


def account_env(account):
    return dict(os.environ, CODEX_HOME=account["home"])


def prepare_home(account):
    """Share settings, never credentials. In particular, never open auth.json."""
    home = Path(account["home"])
    if home == CODEX_HOME:
        return
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    source, target = CODEX_HOME / "config.toml", home / "config.toml"
    if source.exists():
        content = source.read_bytes()
        if not target.exists() or target.read_bytes() != content:
            # Replace a pre-existing symlink rather than writing through to main.
            tmp = home / f".config-{os.getpid()}.tmp"
            tmp.write_bytes(content)
            tmp.replace(target)
    for name in ("AGENTS.md", "skills", "rules"):
        dest = home / name
        if not os.path.lexists(str(dest)):
            dest.symlink_to(CODEX_HOME / name)


def signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS may reject killpg even while the leader is still alive.
        if proc.poll() is not None:
            return False
        if sig:
            try:
                os.kill(proc.pid, sig)
            except ProcessLookupError:
                return False
    return True


def terminate(proc):
    """Interrupt the entire exec group, then bound shutdown even if it ignores signals."""

    for sig, timeout in ((signal.SIGINT, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if not signal_group(proc, sig):
            proc.wait()
            break
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc.poll()  # Reap the leader, but also wait for its tool processes.
            if not signal_group(proc, 0):
                return
            time.sleep(0.05)


class RPCError(RuntimeError):
    pass


@contextmanager
def account_rpc(account, cancelled=lambda: False):
    """One initialized app-server, ignoring unrelated replies; one 20s deadline."""
    proc = None
    try:
        proc = subprocess.Popen([codex_binary(), "app-server"], env=account_env(account), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + ACCOUNT_TIMEOUT
        buf = b""
        replies = {}
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)

            def send(value):
                proc.stdin.write((json.dumps(value) + "\n").encode())
                proc.stdin.flush()

            def receive(wanted):
                nonlocal buf
                while wanted not in replies:
                    if cancelled():
                        raise RuntimeError("stopped")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("app-server timeout")
                    if not sel.select(min(0.2, remaining)):
                        continue
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("app-server closed stdout")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if "id" in msg:
                            replies[msg["id"]] = msg
                reply = replies.pop(wanted)
                if "error" in reply:
                    raise RPCError(str(reply["error"]))
                return reply["result"]

            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-task", "version": "1"}}})
            receive(1)
            send({"method": "initialized"})
            request_id = 1

            def call(method, params):
                nonlocal request_id
                if cancelled():
                    raise RuntimeError("stopped")
                request_id += 1
                send({"id": request_id, "method": method, "params": params})
                return receive(request_id)

            yield call
    finally:
        if proc is not None:
            # app-server must be killed at the deadline, not allowed a second grace period.
            signal_group(proc, signal.SIGKILL)
            proc.wait()
            proc.stdin.close()
            proc.stdout.close()


def account_read(account, cancelled=lambda: False):
    info = dict(account, logged_in=None)
    try:
        with account_rpc(account, cancelled) as call:
            result = call("account/read", {})
            if result["account"] is None:
                info["logged_in"] = False
                return info
            info.update(logged_in=True, account=result["account"],
                        account_id=(result.get("workspaceRouting") or {}).get("chatgptAccountId"))
            result = call("account/rateLimits/read", None)
            info.update(limits=normalize_limits(result.get("rateLimits") or {}),
                        reset_credits=result.get("rateLimitResetCredits") or {},
                        ordinary=result.get("ordinaryUsageAllowed"),
                        account_id=result.get("accountId") or info.get("account_id"))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        info.update(logged_in=None, error=str(exc))
    return info


def reset_credits(info):
    """Only spend available Codex resets that have not expired."""
    return sorted((c for c in info.get("reset_credits", {}).get("credits", [])
                   if c.get("id") and c.get("status") == "available"
                   and c.get("resetType") == "codexRateLimits"
                   and (c.get("expiresAt") is None or c["expiresAt"] > time.time())),
                  key=lambda c: c.get("expiresAt") if c.get("expiresAt") is not None else float("inf"))


@contextmanager
def reset_journal(cancelled=lambda: False):
    CODEX_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = CODEX_HOME / "codex-worker-resets.jsonl"
    # Parallel tasks must agree on a key, even if one dies during the RPC.
    with path.open("a+", encoding="utf-8") as journal:
        while True:
            if cancelled():
                raise RuntimeError("stopped")
            try:
                fcntl.flock(journal, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        journal.seek(0)
        entries = []
        last_line = ""
        for line in journal:
            last_line = line
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        if cancelled():
            raise RuntimeError("stopped")
        if last_line and not last_line.endswith("\n"):
            journal.write("\n")

        def record(entry, **fields):
            value = dict(entry, at=time.time(), **fields)
            journal.write(json.dumps(value, ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            entries.append(value)

        yield entries, record


def pending_resets(entries, account):
    latest = {}
    for entry in entries:
        if entry.get("account") == account["name"] and entry.get("creditId") and entry.get("idempotencyKey"):
            latest[entry["creditId"]] = entry
    return [entry for entry in latest.values()
            if entry.get("event") == "attempt" or entry.get("outcome") == "unknown"]


def consume_recorded_reset(account, credit, slug, cancelled, entries, record):
    key = next((entry["idempotencyKey"] for entry in reversed(entries)
                if entry.get("creditId") == credit["id"] and entry.get("idempotencyKey")), None)
    intent = {"slug": slug, "account": account["name"], "creditId": credit["id"],
              "idempotencyKey": key or str(uuid.uuid4()), "credit": credit}
    record(intent, event="attempt")
    try:
        with account_rpc(account, cancelled) as call:
            response = call("account/rateLimitResetCredit/consume",
                            {"creditId": intent["creditId"], "idempotencyKey": intent["idempotencyKey"]})
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        unknown = not isinstance(exc, RPCError) or "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
        record(intent, event="result", outcome="unknown" if unknown else "error", error=str(exc))
        raise
    record(intent, event="result", outcome="success", response=response)
    return response


def consume_reset_credit(account, credit, slug="", cancelled=lambda: False):
    """Pin the intent to a durable key before sending, including after a lost reply."""
    with reset_journal(cancelled) as (entries, record):
        return consume_recorded_reset(account, credit, slug, cancelled, entries, record)


def reset_credit_action(account, credit_id=None, slug="", cancelled=lambda: False,
                        confirmed=False, pending_only=False, allow_consume=True, quota_cfg=None):
    """Resolve a lost reply before selecting another credit, under the same lock."""
    with reset_journal(cancelled) as (entries, record):
        pending = pending_resets(entries, account)
        if pending_only and not pending:
            return None
        info = account_read(account, cancelled)
        if info.get("logged_in") is not True:
            if pending_only:
                return {"info": info, "outcome": "unavailable"}
            raise RuntimeError(f"account {account['name']} status " +
                               (f"unknown (rpc error: {info.get('error', 'unavailable')})"
                                if info.get("logged_in") is None else f"not logged in: run login {account['name']}"))
        if pending:
            entry = pending[0]
            credit = next((c for c in info.get("reset_credits", {}).get("credits", [])
                           if c.get("id") == entry["creditId"]), None)
            if credit is None or credit.get("status") != "available":
                if confirmed:
                    record(entry, event="result", outcome="consumed (inferred)")
                return {"credit": credit or entry.get("credit") or {"id": entry["creditId"]},
                        "info": info, "outcome": "consumed (inferred)"}
        if quota_cfg is not None and not exhausted(info, quota_cfg):
            return {"info": info, "outcome": "available"}
        if not pending:
            choices = reset_credits(info)
            credit = next((c for c in choices if c["id"] == credit_id), None) if credit_id else next(iter(choices), None)
            if credit is None:
                raise RuntimeError(f"no available free limit reset for {account['name']}" +
                                   (f" with ID {credit_id}" if credit_id else ""))
        action = {"credit": credit, "info": info, "outcome": "preview"}
        if confirmed and allow_consume:
            action["response"] = consume_recorded_reset(account, credit, slug, cancelled, entries, record)
            action.update(outcome="success", info=account_read(account, cancelled))
        return action


def normalize_limits(raw):
    out = {"reached": raw.get("rateLimitReachedType", raw.get("rate_limit_reached_type")),
           "credits": raw.get("credits") or {}}
    for name in ("primary", "secondary"):
        window = raw.get(name)
        if window:
            out[name] = {"used": window.get("usedPercent", window.get("used_percent", 0)),
                         "minutes": window.get("windowDurationMins", window.get("window_minutes", 0)),
                         "reset": window.get("resetsAt", window.get("resets_at"))}
    return out


def exhausted(info, cfg):
    limits = info.get("limits", {})
    return (info.get("ordinary") is False or limits.get("reached") is not None
            or any(limits.get(n, {}).get("used", 0) >= cfg["switch_at"] for n in ("primary", "secondary")))


def reset_time(info, cfg):
    limits = info.get("limits", {})
    windows = [limits[n] for n in ("primary", "secondary") if n in limits]
    depleted = [w for w in windows if w["used"] >= cfg["switch_at"]]
    # Every exhausted window must reset before this account can be used again.
    times = [w["reset"] for w in depleted if w.get("reset") is not None]
    return max(times) if times else None


def longest_window(info):
    windows = [info.get("limits", {}).get(n) for n in ("primary", "secondary")]
    windows = [w for w in windows if w]
    return max(windows, key=lambda w: w["minutes"]) if windows else {}


def has_credits(info):
    credits = info.get("limits", {}).get("credits", {})
    if credits.get("unlimited"):
        return True
    if not credits.get("hasCredits", credits.get("has_credits", False)):
        return False
    try:
        return float(credits.get("balance", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def snapshots(cfg, cancelled=lambda: False, prepare=False):
    result, seen = [], {}
    for a in cfg["accounts"]:
        if cancelled():
            break
        if prepare:
            prepare_home(a)
        info = account_read(a, cancelled)
        ident = info.get("account_id")
        if info["logged_in"] and ident and ident in seen:
            info["duplicate"] = seen[ident]
        elif info["logged_in"] and ident:
            seen[ident] = info["name"]
        result.append(info)
    return result


def available(infos, cfg, exclude=()):
    choices = [i for i in infos if i["logged_in"] and not i.get("duplicate")
               and i["name"] not in exclude and not exhausted(i, cfg)]
    return min(choices, key=lambda i: longest_window(i).get("reset") or float("inf")) if choices else None


def local_time(stamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp is not None else "unknown"


def state_path(slug):
    return TASKS / slug / "state.json"


def load(slug):
    p = state_path(slug)
    if not p.exists():
        die(f"no task {slug!r}")
    st = json.loads(p.read_text())
    st.setdefault("account", "main")
    st.setdefault("threads", ([{"account": st["account"], "thread": st["thread"], "started": st.get("created", 0)}]
                              if st.get("thread") else []))
    st.setdefault("rotations", [])
    st.setdefault("reviews", [])
    st.setdefault("checks", [])
    st.setdefault("result", None)
    st.setdefault("fix_rounds", 0)
    st.setdefault("rounds", [])
    st.setdefault("guards", [])
    st.setdefault("base_check", False)
    return st


def save(st):
    p = state_path(st["slug"])
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(p)


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_tasks(exclude=""):
    out = []
    for p in TASKS.glob("*/state.json"):
        st = json.loads(p.read_text())
        if st["slug"] != exclude and st.get("kind") != "scout" and alive(st.get("pid")):
            out.append(st["slug"])
    return out


def main_repo(path):
    """The main checkout of the repository `path` belongs to (including worktrees)."""
    common = Path(sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path))
    return common.parent


def spawn(st, prompt, effort=None, current_message=False, kind="work", review_after=False, fix_rounds=0):
    d = TASKS / st["slug"]
    for name in (("review.md",) if kind == "review" else ("last.md", "review.md")):
        last = d / name
        if last.exists():
            last.unlink()
    st.update(pending={"prompt": prompt, "effort": effort, "current_message": current_message,
                       "review_after": review_after}, kind=kind, work_completed=False,
              review_start=len(st.get("reviews", [])), turn_started=time.time(),
              check_start=len(st.get("checks", [])),
              turns=st.get("turns", 0) + 1, rotation_start=len(st["rotations"]),
              event_start=len(events(st)), exhausted=None, outcome="running", reset_wait_seconds=0,
              fix_rounds=fix_rounds, rounds=[])
    st.pop("sleeping", None)
    st.pop("turn_finished", None)
    st.pop("error", None)
    st.pop("review_skipped", None)
    st.pop("auto_fix_stop", None)
    st.pop("turn_stop", None)
    st.pop("wrapup", None)
    if kind != "review":
        st["result"] = None
    with (d / "stderr.log").open("a") as errors:
        proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "_run", st["slug"]],
                                cwd=st["worktree"], stdin=subprocess.DEVNULL, stdout=errors, stderr=errors,
                                start_new_session=True)
    st["pid"] = proc.pid
    save(st)
    return proc


def effort_args(effort):
    return ["-c", f'model_reasoning_effort="{effort}"'] if effort else []


def events(st):
    p = TASKS / st["slug"] / "events.jsonl"
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


def followup_prompt(st, message, cfg, source="claude"):
    """Journal follow-ups before dispatch so rotation includes the current request."""
    with (TASKS / st["slug"] / "messages.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": time.time(), "text": message, "source": source},
                                ensure_ascii=False) + "\n")
    return message if st.get("thread") else handoff(st, current_message=True, cfg=cfg)


def rollout_path(home, thread):
    if not thread:
        return None
    paths = list((Path(home) / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def rollout_limits(path):
    """Read the tail only; tolerate a partial last JSON line during append."""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - 262144))
        lines = f.read().splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("rate_limits"):
            return normalize_limits(payload["rate_limits"])
    return None


def handoff(st, current_message=False, cfg=None):
    cfg = accounts_config() if cfg is None else cfg
    ru = cfg.get("language", "en") == "ru"
    scouting = st.get("kind") == "scout"
    d = TASKS / st["slug"]
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
    messages = d / "messages.jsonl"
    if messages.exists():
        lines = messages.read_text().splitlines()
        for n, line in enumerate(lines, 1):
            text = json.loads(line)["text"]
            if current_message and n == len(lines):
                heading = "Текущее сообщение — выполни его" if ru else "Current message — carry it out"
                prompt += f"\n### {heading}\n\n{text}\n"
            else:
                prompt += f"\n{n}. {text}\n"
    if not scouting:
        prompt += f"\n{'Коммиты' if ru else 'Commits'} (git log --oneline {st['base']}..HEAD):\n"
        prompt += sh(["git", "log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False) or ("(нет)" if ru else "(none)")
        prompt += ("\n\nСостояние worktree (git status --short):\n" if ru else "\n\nWorktree status (git status --short):\n")
        prompt += sh(["git", "status", "--short"], cwd=st["worktree"], check=False) or ("(чисто)" if ru else "(clean)")
    if has_threads:
        label = (("Последние сообщения предыдущего разведчика" if ru else "Latest messages from the previous scout")
                 if scouting else ("Последние сообщения предыдущего исполнителя" if ru else "Latest messages from the previous worker"))
        messages = []
        for message in thread_messages(st)[-3:]:
            result = None if scouting else parse_result(message)
            if result is not None:
                message = result["summary"]
                if result["remaining"]:
                    message += ("\nОсталось:\n" if ru else "\nRemaining:\n") + "\n".join(result["remaining"])
            messages.append(message)
        prompt += f"\n\n{label}:\n" + "\n\n".join(messages)[-4000:]
    if not scouting:
        prompt += (f"\n\nСначала посмотри git status и git diff {st['base']}, потом продолжай работу.\n" if ru else
                   f"\n\nFirst inspect git status and git diff {st['base']}, then continue the task.\n")
    return prompt


def scout_prompt(question, cfg=None):
    cfg = accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        rules = ("Ты разведчик: изучи код и ответь на вопрос Claude.\n"
                 "Файлы не меняй, ничего не коммить и не пушь.\n"
                 "Ответ — примерно до 300 слов, если вопрос не просит иного; без вступления.\n"
                 "Каждый факт подкрепи ссылкой файл:строка. "
                 "Отдельно отметь, что проверено, а что предположение.\n"
                 "Назови тесты, которые покрывают эту область. Скажи, чего не смотрел.")
    else:
        rules = ("You are a scout: investigate the code and answer Claude's question.\n"
                 "Do not change files, commit, or push.\n"
                 "Keep the answer to about 300 words unless the question asks otherwise; no introduction.\n"
                 "Cite file:line for every fact. Separate verified findings from assumptions.\n"
                 "Name the tests covering this area. State what you did not inspect.")
    return question.rstrip() + "\n\n" + rules


def check_paths(st):
    """Read saved and existing relative paths from top-level check tokens."""
    paths = set(st.get("check_paths", []))
    root = Path(st["worktree"])
    try:
        lexer = shlex.shlex(st.get("check") or "", posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = []
    candidates = []
    for token in tokens:
        candidates.append(token)
        if "=" in token:
            name, value = token.split("=", 1)
            if token.startswith("--") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                candidates.append(value)
    for candidate in candidates:
        if not candidate or Path(candidate).is_absolute():
            continue
        path = Path(os.path.abspath(root / candidate))
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            exists = path.exists()
        except OSError:
            # Shell arguments can be code or long strings rather than filenames.
            continue
        if exists:
            paths.add(relative)
    return sorted(paths)


def guarded_path(path, st, paths):
    parts = path.split("/")
    return (any(part in ("tests", "test", "__tests__", "spec") for part in parts[:-1])
            or any(fnmatch.fnmatchcase(parts[-1], pattern)
                   for pattern in ("test_*", "*_test.*", "*.test.*", "*.spec.*"))
            or path == ".gitlab-ci.yml"
            or path.startswith((".github/workflows/", ".forgejo/workflows/"))
            or any(item == "." or path == item or path.startswith(item + "/") for item in paths)
            or any(fnmatch.fnmatchcase(path, pattern.removeprefix("./")) for pattern in st.get("guards", [])))


DISABLED_TEST = re.compile(r"\b(?:skip|skipif|xfail|only)\b|@unittest\.skip\w*|@Disabled\b|\bt\.Skip\(", re.I)


def guarded_changes(st):
    """Combine committed and dirty diffs, treating renames as deletion/addition."""
    if st.get("kind") == "scout":
        return []
    root = st["worktree"]
    paths = check_paths(st)
    changed = {}
    args = ["git", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-renames"]
    for revisions in ([st["base"], "HEAD"], ["--cached", "HEAD"], []):
        stats = sh(args + ["--numstat", "-z"] + revisions + ["--"], cwd=root, check=False)
        deleted = set(sh(args + ["--name-only", "-z", "--diff-filter=D"] + revisions + ["--"],
                         cwd=root, check=False).split("\0"))
        for entry in stats.split("\0"):
            if not entry:
                continue
            added, removed, path = entry.split("\t", 2)
            if not guarded_path(path, st, paths):
                continue
            item = changed.setdefault(path, {"path": path, "added": 0, "removed": 0,
                                             "deleted": False, "disabled": []})
            item["added"] += int(added) if added != "-" else 0
            item["removed"] += int(removed) if removed != "-" else 0
            item["deleted"] |= path in deleted
            # Patch contents can use any source-file encoding; ASCII hunk and
            # skip markers remain readable even when other bytes are replaced.
            patch = sh(args + ["--unified=0"] + revisions + ["--", path],
                       cwd=root, check=False, errors="replace")
            line_number = 0
            in_hunk = False
            for line in patch.splitlines():
                hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if hunk:
                    line_number = int(hunk[1])
                    in_hunk = True
                elif in_hunk and line.startswith("+"):
                    if DISABLED_TEST.search(line[1:]):
                        marker = {"line": line_number, "text": line[1:].strip()}
                        if marker not in item["disabled"]:
                            item["disabled"].append(marker)
                    line_number += 1
                elif in_hunk and line.startswith(" "):
                    line_number += 1
    untracked = sh(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root, check=False)
    for path in filter(None, untracked.split("\0")):
        if not guarded_path(path, st, paths):
            continue
        file = Path(root) / path
        try:
            content = os.readlink(file) if file.is_symlink() else file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = content.splitlines()
        item = changed.setdefault(path, {"path": path, "added": 0, "removed": 0,
                                         "deleted": False, "disabled": []})
        item["added"] += len(lines)
        item["disabled"].extend({"line": n, "text": line.strip()} for n, line in enumerate(lines, 1)
                                if DISABLED_TEST.search(line))
    return [changed[path] for path in sorted(changed)]


def guarded_lines(changes):
    if not changes:
        return []
    lines = ["== guarded files changed: " + ", ".join(
        f"{item['path']} (+{item['added']} −{item['removed']})" for item in changes)]
    deleted = [item["path"] for item in changes if item["deleted"]]
    if deleted:
        lines.append("== guarded files deleted: " + ", ".join(deleted))
    disabled = [f"{item['path']}:{marker['line']} {marker['text']}"
                for item in changes for marker in item["disabled"]]
    if disabled:
        lines.append("== guarded tests disabled: " + "; ".join(disabled))
    return lines


def review_prompt(st, cfg=None):
    cfg = accounts_config() if cfg is None else cfg
    brief = TASKS / st["slug"] / "brief.md"
    ru = cfg.get("language", "en") == "ru"
    context = ""
    if st.get("rounds"):
        context += (("Начиная со второго ревью ищи регрессии, внесённые исправлениями, и невыполненные "
                     "пункты «Готово, когда». Новые граничные случаи вне брифа отмечай только как P3.\n") if ru else
                    ("From the second review onward, look for regressions introduced by the fixes and unmet "
                     "acceptance criteria (Done when). Report new edge cases outside the brief only as P3.\n"))
    guarded = guarded_lines(guarded_changes(st))
    if guarded:
        context += "\n".join(guarded) + "\n"
        context += (("Прочитай эти охраняемые файлы целиком и проверь, не ослаблены ли проверки: "
                     "убранные утверждения, пропуски, расширенные допуски, моки вместо проверяемого кода.\n") if ru else
                    ("Read these guarded files in full and check for weakened checks: removed assertions, "
                     "skips, widened tolerances, or mocks replacing the code under test.\n"))
    checks = st.get("checks", [])
    if (checks and st.get("check") == checks[-1]["cmd"] and checks[-1]["outcome"] == "passed"):
        check = checks[-1]
        evidence = f"`{check['cmd']}`: passed (exit {check['exit']}, {check['seconds']:.0f} s).\n"
        context += (("Сохранённая проверка уже прошла: " + evidence +
                     "Не перезапускай весь набор проверок. Запускай только точечные проверки, "
                     "чтобы подтвердить конкретную находку.\n") if ru else
                    ("The saved check already passed: " + evidence +
                     "Do not rerun the full test suite. Run only targeted checks to confirm a specific finding.\n"))
        if check.get("base_check"):
            base = check["base_check"]
            if base["outcome"] == "skipped":
                context += f"Base tests: skipped ({base['reason']}).\n"
            else:
                context += (f"Base tests: {base['outcome']} (exit {base['exit']}, {base['seconds']:.0f} s).\n"
                            + base.get("tail", "") + "\n")
            context += (("Это сигнал для ревью; причиной может быть законная смена поведения.\n") if ru else
                        ("This is review evidence; a legitimate behavior change may explain a failure.\n"))
    rejected = []
    for entry in st.get("rounds", []):
        findings = {item["id"]: item for item in (entry.get("review") or {}).get("findings", [])}
        for decision in entry.get("decisions", []):
            finding = findings.get(decision["id"])
            if decision["decision"] == "rejected" and finding is not None:
                rejected.append(f"{finding['id']} [{finding['severity']}] {finding['file']}:{finding['line']} "
                                f"— {finding['title']}\n{finding['detail']}\nverify: {finding['verify']}\n"
                                + ("Причина отклонения: " if ru else "Rejection reason: ") + decision["note"])
    if rejected:
        context += (("Исполнитель отклонил следующие находки. Не поднимай их снова, если причина верна; "
                     "если поднимаешь — объясни, чем она неверна.\n") if ru else
                    ("The worker rejected these findings. Do not raise them again if the reason is valid; "
                     "if you raise one again, explain why its rejection reason is wrong.\n"))
        context += "\n\n".join(rejected) + "\n"
    if ru:
        return (f"Проведи ревью изменений задачи: git diff {shlex.quote(st['base'])}.\n"
                f"Бриф задачи — в файле {brief}. Прочитай его и сверяйся с разделом «Готово, когда».\n"
                "Ищи только реальные дефекты: неверное поведение, невыполненные пункты брифа, "
                "сломанные или фиктивные проверки, опасные побочные эффекты, гонки.\n"
                "Для каждого дефекта укажи файл:строка, что не так и как это проверить. "
                "Без замечаний по стилю.\n"
                + context +
                "Файлы не меняй, ничего не коммить. Ответь JSON по схеме ревью: verdict clean, если "
                "дефектов нет, иначе defects. Находки нумеруй F1, F2 и далее; severity P1/P2/P3, "
                "file, line, title, detail и verify. При clean findings пуст; summary — краткий итог.")
    return (f"Review the task changes: git diff {shlex.quote(st['base'])}.\n"
            f"Read the task brief at {brief} and check its acceptance criteria.\n"
            "Look only for real defects: incorrect behavior, unmet brief requirements, "
            "broken or ineffective checks, dangerous side effects, and races.\n"
            "For each defect, give file:line, explain what is wrong and how to verify it. "
            "Do not comment on style.\n"
            + context +
            "Do not change files or create commits. Return JSON matching the review schema: verdict clean "
            "when there are no defects, otherwise defects. Number findings F1, F2, etc.; include severity "
            "P1/P2/P3, file, line, title, detail and verify. For clean, findings is empty; summary is a short recap.")


def check_tail(st, filename="check.log"):
    path = TASKS / st["slug"] / filename
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", errors="replace") as output:
        return "".join(deque(output, maxlen=40)).rstrip()


def fix_prompt(st, review, check, cfg):
    ru = cfg.get("language", "en") == "ru"
    if check is not None:
        prompt = (("Исправь сбой сохранённой проверки после предыдущего исправления.\n" if ru else
                   "Fix the saved check failure after the previous fix.\n") +
                  f"`{check['cmd']}`: {check['outcome']}, exit {check['exit']}\ncheck.log (40):\n" +
                  check.get("tail", check_tail(st)))
    else:
        prompt = "Проверь находки ревью по коду:\n" if ru else "Check these review findings against the code:\n"
        for finding in review["findings"]:
            prompt += (f"\n{finding['id']} [{finding['severity']}] {finding['file']}:{finding['line']} "
                       f"— {finding['title']}\n{finding['detail']}\nverify: {finding['verify']}\n")
    prompt += (("\n\nПроверь каждую находку по коду. Настоящие дефекты исправь, где разумно — с тестом. "
                "Неверные отклони с причиной в одну строку. Если находка требует непропорциональной "
                "сложности ради случая вне «Готово, когда», отклони её с причиной out of scope "
                "и вынеси в remaining. Закоммить и запушь изменения. "
                "Верни итог по схеме работы; в findings укажи для каждой находки id, "
                "decision fixed или rejected и note с пояснением. Если находок нет, findings пуст.\n") if ru else
               ("\n\nVerify every finding against the code. Fix real defects, adding a test where reasonable. "
                "Reject incorrect findings with a one-line reason. If a finding requires disproportionate "
                "complexity for a case outside the acceptance criteria (Done when), reject it with the reason "
                "out of scope and list it in remaining. Commit and push the changes. "
                "Return the work result schema; in findings give each finding's id, decision fixed or rejected, "
                "and an explanatory note. When there are no findings, use an empty findings array.\n"))
    return prompt


def reset_resume_prompt(cfg=None):
    cfg = accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        return "Лимит подписки сбросился. Продолжай с того места, где остановился."
    return "The subscription limit has reset. Continue from where you stopped."


def auth_resume_prompt(cfg=None):
    cfg = accounts_config() if cfg is None else cfg
    if cfg.get("language", "en") == "ru":
        return "Ход прервался из-за ошибки входа. Продолжай с того места, где остановился."
    return "The turn was interrupted by an authentication error. Continue from where you stopped."


def limit_reason(info, cfg):
    limits = info.get("limits", {})
    for name in ("primary", "secondary"):
        w = limits.get(name, {})
        if w.get("used", 0) >= cfg["switch_at"]:
            label = "weekly" if w.get("minutes", 0) >= 10080 else name
            return f"{label} limit {w['used']:g}%"
    return limits.get("reached") or "usage limit"


def limit_error(event):
    if event.get("type") not in ("turn.failed", "error"):
        return False
    text = json.dumps(event, ensure_ascii=False).lower()
    return "usage limit" in text or any(s in text for s in (
        "rate_limit_reached", "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached",
        "workspace_owner_credits_depleted", "workspace_member_credits_depleted"))


def auth_error(event):
    if event.get("type") not in ("turn.failed", "error"):
        return False
    text = json.dumps(event, ensure_ascii=False).lower()
    return bool(re.search(r"\b401\b", text)) or any(part in text for part in (
        "unauthorized", "access token could not be refreshed", "authentication session could not be refreshed",
        "oauth refresh token was rejected", "failed to refresh token"))


def earliest_reset(infos, cfg):
    resets = [(reset_time(i, cfg), i["name"]) for i in infos if i.get("logged_in") and not i.get("duplicate")]
    resets = [(stamp, name) for stamp, name in resets if stamp is not None]
    return min(resets) if resets else (None, None)


def record_exhausted(st, infos, cfg, reason):
    stamp, name = earliest_reset(infos, cfg)
    st.pop("sleeping", None)
    st["exhausted"] = {"at": time.time(), "reset": stamp, "account": name, "reason": reason}
    st["outcome"] = "exhausted"
    save(st)
    notify(st, cfg, "paused", f"paused: {reason}", until=stamp)


def try_reset_credit(st, infos, cfg, stopped):
    """Return fresh quotas after a reset or concurrent quota recovery; never retry here."""
    stamp, _ = earliest_reset(infos, cfg)
    if (not cfg.get("use_reset_credits", False) or stamp is None
            or stamp - time.time() <= cfg.get("reset_credit_min_hours", 12) * 3600
            or available(infos, cfg)):
        return None
    eligible = [i for i in infos if i.get("logged_in") and not i.get("duplicate")]
    with reset_journal(stopped) as (entries, _):
        pending = [(entry.get("credit") or {"id": entry["creditId"]}, info)
                   for info in eligible for entry in pending_resets(entries, info)]
    choices = pending or [(credit, info) for info in eligible
               for credit in reset_credits(info)]
    if not choices or stopped():
        return None
    credit, info = min(choices, key=lambda pair: pair[0].get("expiresAt")
                       if pair[0].get("expiresAt") is not None else float("inf"))
    event = {"event": "reset_credit", "at": time.time(), "account": info["name"],
             "credit_id": credit["id"], "expires_at": credit.get("expiresAt")}
    try:
        action = reset_credit_action(info, credit["id"], st["slug"], stopped, confirmed=True, quota_cfg=cfg)
        if action["outcome"] == "available":
            return [action["info"] if i["name"] == info["name"] else i for i in infos]
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        event.update(outcome="error", error=str(exc))
    else:
        event.update(outcome=action["outcome"], credit_id=action["credit"]["id"],
                     expires_at=action["credit"].get("expiresAt"))
    st["rotations"].append(event)
    save(st)
    if event["outcome"] in ("success", "consumed (inferred)") and not stopped():
        notify(st, cfg, "reset_credit_used", f"used a free limit reset on {info['name']}",
               account=info["name"])
        return task_snapshots(st, cfg, stopped)
    return None


def refresh_login_state(st, infos, new_turn=False):
    """Only a fresh probe at the beginning of a turn can clear a login flag."""
    names = list(st.get("needs_login", []))
    identities = st.setdefault("account_ids", {})
    for info in infos:
        if info.get("account_id"):
            identities[info["name"]] = info["account_id"]
        if new_turn and info.get("logged_in") is True and info["name"] in names:
            names.remove(info["name"])
        elif info.get("logged_in") is False and info["name"] not in names:
            names.append(info["name"])
            st["rotations"].append({"event": "needs_login", "at": time.time(), "account": info["name"]})
            notify(st, None, "needs_login", f"{info['name']} needs login", account=info["name"])
    if names != st.get("needs_login", []):
        st["needs_login"] = names
        save(st)
    return [i for i in infos if i["name"] not in st.get("auth_excluded", [])
            and i.get("duplicate") not in st.get("auth_excluded", [])
            and (i.get("account_id") or identities.get(i["name"])) not in st.get("auth_excluded_ids", [])]


def exclude_auth_account(st, account, info):
    name = account["name"]
    if name not in st["auth_excluded"]:
        st["auth_excluded"].append(name)
    ident = info.get("account_id") or st.get("account_ids", {}).get(name)
    if ident and ident not in st["auth_excluded_ids"]:
        st["auth_excluded_ids"].append(ident)
    names = st.setdefault("needs_login", [])
    if name not in names:
        names.append(name)
        st["rotations"].append({"event": "needs_login", "at": time.time(), "account": name})
        notify(st, None, "needs_login", f"{name} needs login", account=name)
    save(st)


def task_snapshots(st, cfg, stopped, new_turn=False):
    infos = snapshots(cfg, stopped, prepare=True)
    if cfg.get("use_reset_credits", False):
        for info in infos:
            if (stopped() or not info.get("logged_in") or info.get("duplicate")
                    or info["name"] in st.get("auth_excluded", [])
                    or info.get("account_id") in st.get("auth_excluded_ids", [])):
                continue
            action = reset_credit_action(info, slug=st["slug"], cancelled=stopped,
                                         confirmed=True, pending_only=True, allow_consume=False)
            if action is None:
                continue
            info.update(action["info"])
            if action["outcome"] == "consumed (inferred)":
                st["reset_reconciled"] = True
                credit = action["credit"]
                st["rotations"].append({"event": "reset_credit", "at": time.time(), "account": info["name"],
                                        "credit_id": credit["id"], "expires_at": credit.get("expiresAt"),
                                        "outcome": action["outcome"]})
                save(st)
                notify(st, cfg, "reset_credit_used", f"used a free limit reset on {info['name']}",
                       account=info["name"])
    return refresh_login_state(st, infos, new_turn)


def no_logged_in_accounts(st, infos):
    if any(i.get("logged_in") is not False for i in infos):
        return False
    names = st.get("needs_login", []) or [i["name"] for i in infos]
    st.update(outcome="failed", error="No usable logged-in accounts remain; " +
              "; ".join("run login " + name for name in names))
    st.pop("sleeping", None)
    save(st)
    return True


def wait_for_account(st, infos, cfg, reason, stopped, sleep=None):
    """Wait without an exec, re-reading quotas after each wall-clock deadline."""
    infos = refresh_login_state(st, infos)
    if no_logged_in_accounts(st, infos):
        return None
    sleep = sleep or time.sleep
    probe_retries = 0
    wait_started = time.time()
    already_waited = st.get("reset_wait_seconds", 0)
    deadline = wait_started + MAX_RESET_WAIT_SECONDS - already_waited

    def cancelled():
        return stopped() or time.time() >= deadline

    def update_waited():
        st["reset_wait_seconds"] = already_waited + max(0, time.time() - wait_started)

    while not stopped():
        update_waited()
        remaining = deadline - time.time()
        if remaining <= 0:
            record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        unknown = [i for i in infos if i.get("logged_in") is None]
        probing = bool(unknown) and probe_retries < MAX_ACCOUNT_RETRIES
        if unknown and not probing and not any(i.get("logged_in") is True for i in infos):
            errors = "; ".join(f"{i['name']}: {i.get('error', 'RPC unavailable')}" for i in unknown)
            st.update(outcome="failed", error=f"Account status is unknown after {MAX_ACCOUNT_RETRIES} retries: {errors}")
            st.pop("sleeping", None)
            save(st)
            return None
        if not cfg["wait_for_reset"] and not probing:
            record_exhausted(st, infos, cfg, reason)
            return None
        # At most one consume attempt before this sleep, even if the result is unknown
        # or the refreshed quota still says exhausted.
        reconciled = st.pop("reset_reconciled", False)
        refreshed = None if unknown or reconciled else try_reset_credit(st, infos, cfg, cancelled)
        st.pop("reset_reconciled", None)
        if refreshed is not None:
            infos = refresh_login_state(st, refreshed)
        update_waited()
        if cancelled():
            if not stopped():
                record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        if refreshed is not None:
            current = next((i for i in infos if i["name"] == st["account"]), {})
            if st.get("kind") != "review" and st.get("thread") and current.get("logged_in") and not exhausted(current, cfg):
                return current
            chosen = available(infos, cfg)
            if chosen:
                return chosen
            unknown = [i for i in infos if i.get("logged_in") is None]
            probing = bool(unknown) and probe_retries < MAX_ACCOUNT_RETRIES
        if no_logged_in_accounts(st, infos):
            return None
        started = time.time()
        stamp, name = earliest_reset(infos, cfg)
        # A delayed reset may still have its old timestamp. Avoid a tight RPC loop.
        until = stamp + RESET_MARGIN if stamp is not None else started + RESET_RETRY_SECONDS
        if until <= started:
            until = started + SLEEP_STEP
        if probing:
            until = min(until, started + ACCOUNT_RETRY_SECONDS) if stamp is not None else started + ACCOUNT_RETRY_SECONDS
        until = min(until, deadline)
        sleeping = {"until": until, "account": name, "reason": reason}
        if probing:
            sleeping.update(probe=True, probe_attempt=probe_retries + 1, reason="account status unknown (RPC error)")
        st.update(outcome="sleeping", sleeping=sleeping, exhausted=None)
        st["rotations"].append(dict(sleeping, at=started, event="sleep"))
        save(st)
        notify(st, cfg, "paused", f"paused until {local_time(until)}: {sleeping['reason']}", until=until)
        try:
            while not stopped():
                left = until - time.time()
                if left <= 0:
                    break
                sleep(min(SLEEP_STEP, left))
        finally:
            update_waited()
        if stopped():
            return None
        if st["reset_wait_seconds"] >= MAX_RESET_WAIT_SECONDS:
            record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        st.pop("sleeping", None)
        st["outcome"] = "running"
        st["rotations"].append(dict(sleeping, at=time.time(), event="wake"))
        save(st)
        notify(st, cfg, "woke", "woke to recheck account limits")
        infos = task_snapshots(st, cfg, cancelled)
        if probing:
            probe_retries += 1
        if not any(i.get("logged_in") is None for i in infos):
            probe_retries = 0
        update_waited()
        if stopped():
            return None
        if st["reset_wait_seconds"] >= MAX_RESET_WAIT_SECONDS:
            record_exhausted(st, infos, cfg, reason + "; reset wait exceeded 8 days")
            return None
        if no_logged_in_accounts(st, infos):
            return None
        current = next((i for i in infos if i["name"] == st["account"]), {})
        if st.get("kind") != "review" and st.get("thread") and current.get("logged_in") and not exhausted(current, cfg):
            return current
        chosen = available(infos, cfg)
        if chosen:
            return chosen
    return None


class TurnControl:
    """Limits belong to a phase, surviving exec retries but excluding account waits."""

    def __init__(self, minutes, repeat_limit):
        self.minutes = minutes
        self.seconds = None if minutes is None else minutes * MINUTE_SECONDS
        self.elapsed = 0.0
        self.repeat_limit = repeat_limit
        self.failures = {}
        self.reason = None

    def expired(self, started):
        if self.seconds is not None and self.elapsed + time.monotonic() - started >= self.seconds:
            self.reason = {"outcome": "budget_exhausted", "minutes": self.minutes}
            return True
        return False

    def repeated(self, event):
        item = event.get("item") or {}
        if event.get("type") != "item.completed" or item.get("type") != "command_execution":
            return False
        command, code = item.get("command"), item.get("exit_code")
        if not isinstance(command, str) or type(code) is not int:
            return False
        if code == 0:
            self.failures.pop(command, None)
            return False
        output = item.get("aggregated_output", "")
        if not isinstance(output, str):
            return False
        digest = hashlib.sha256(output.encode("utf-8")[-2048:]).hexdigest()
        signature = (code, digest)
        previous, count = self.failures.get(command, (None, 0))
        count = count + 1 if signature == previous else 1
        self.failures[command] = (signature, count)
        if count >= self.repeat_limit:
            self.reason = {"outcome": "stuck", "command": command, "exit_code": code,
                           "output_sha256": digest, "count": count}
            return True
        return False


def turn_stop_message(reason):
    if reason["outcome"] == "budget_exhausted":
        return f"time budget {reason['minutes']:g} min exhausted"
    return f"repeated failure ×{reason['count']}: {reason['command']}"


def wrapup_prompt(st, reason, cfg):
    ru = cfg.get("language", "en") == "ru"
    if reason["outcome"] == "budget_exhausted":
        prompt = ("Время хода вышло. " if ru else "The turn's time budget has expired. ")
        if st.get("kind") not in ("review", "scout"):
            prompt += ("Закоммить то, что работает, и запушь. " if ru else
                       "Commit what works and push it. ")
        prompt += ("Коротко подведи итог со status partial и списком оставшегося в remaining. " if ru else
                   "Give a short final result with status partial and a list of remaining work in remaining. ")
    else:
        prompt = ((f"Команда {reason['command']!r} упала одинаково {reason['count']} раз "
                   f"(exit_code {reason['exit_code']}, SHA-256 хвоста вывода {reason['output_sha256']}). "
                   "Остановись. Короткий итог со status blocked: приложи числа и что пробовал. ") if ru else
                  (f"Command {reason['command']!r} failed identically {reason['count']} times "
                   f"(exit_code {reason['exit_code']}, output tail SHA-256 {reason['output_sha256']}). "
                   "Stop. Give a short final result with status blocked, numerical evidence and what you tried. "))
    prompt += ("На сворачивание не больше 10 минут; не продолжай реализацию и не запускай ревью. " if ru else
               "You have at most 10 minutes to wrap up; do not continue implementation or start a review. ")
    if st.get("kind") in ("review", "scout"):
        prompt += ("Режим только чтения: не меняй файлы, не коммить и не пушь. " if ru else
                   "Stay read-only: do not change files, commit or push. ")
    if st.get("kind") != "scout":
        prompt += "Ответь JSON по схеме работы." if ru else "Return JSON matching the work result schema."
    return prompt


def monitor(st, cfg, account, prompt, effort, stopped, on_limit, review_fallback=False,
            control=None, wrapup=False):
    """One exec attempt. The supervisor alone owns state; append CLI events verbatim."""
    d = TASKS / st["slug"]
    prepare_home(account)
    reviewing = st.get("kind") == "review"
    control = control or TurnControl(cfg.get("max_review_minutes", 30) if st.get("kind") in ("review", "scout")
                                     else st.get("max_turn_minutes", cfg.get("max_turn_minutes")),
                                     cfg.get("repeat_limit", 3))
    thread = ((st.get("reviews") or [{}])[-1].get("thread", "") if wrapup and reviewing
              else "" if reviewing else st.get("thread", ""))
    review_entry = None
    last = d / ("review.md" if reviewing else "last.md")
    if last.exists():
        last.unlink()
    args = [codex_binary(), "exec"]
    if reviewing and not wrapup:
        # Some CLI versions reject --base together with custom instructions.
        if not review_fallback:
            args += ["review", "--base", st["base"]]
        args += ["--json", "-o", str(last)]
    elif thread:
        args += ["resume", thread, "--json", "-o", str(last)]
    else:
        args += ["--json", "-C", st["worktree"], "-o", str(last)]
    if st.get("kind") == "scout" or (reviewing and wrapup):
        args += ["-c", 'sandbox_mode="read-only"']
    if st.get("kind") != "scout":
        schema = d / ("review-schema.json" if reviewing and not wrapup else "result-schema.json")
        schema.write_text(json.dumps(REVIEW_SCHEMA if reviewing and not wrapup else RESULT_SCHEMA, indent=2), encoding="utf-8")
        args += ["--output-schema", str(schema)]
    args += effort_args(effort) + [prompt]
    path = rollout_path(account["home"], thread) if thread else None
    previous_size = path.stat().st_size if path else 0
    next_search = time.monotonic() + 5 if thread else 0.0
    next_poll = 0.0
    reason = None
    current_info = None
    failed = False
    authentication_failed = False
    final_message = ""
    st["exec_event_start"] = len(events(st))
    save(st)
    with (d / "events.jsonl").open("ab", buffering=0) as log, (d / "stderr.log").open("a") as errors:
        if stopped():
            return "stopped", None
        error_start = errors.tell()
        attempt_started = time.time()
        active_started = time.monotonic()
        proc = subprocess.Popen(args, cwd=st["worktree"], env=account_env(account), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
        st["codex_pid"] = proc.pid
        save(st)
        buf = b""

        def quota_decision(local, why, allow_credits):
            # Quota RPCs and reset-journal locks run while exec is still alive.
            def cancelled():
                return stopped() or control.expired(active_started)

            try:
                decision = on_limit(local, why, allow_credits, cancelled)
            except RuntimeError:
                if not cancelled():
                    raise
                decision = None
            if stopped():
                return "stopped", None
            if control.expired(active_started):
                return "budget_exhausted", None
            return ("limit", decision) if decision is not None or not allow_credits else None

        try:
            with selectors.DefaultSelector() as sel:
                sel.register(proc.stdout, selectors.EVENT_READ)
                eof = False
                while not eof or proc.poll() is None:
                    if stopped():
                        terminate(proc)
                        return "stopped", None
                    if control.expired(active_started):
                        return "budget_exhausted", None
                    for key, _ in sel.select(0.2):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            sel.unregister(key.fileobj)
                            eof = True
                        else:
                            log.write(chunk)
                            buf += chunk
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue
                                if event.get("type") == "thread.started":
                                    thread = event["thread_id"]
                                    if reviewing and not wrapup:
                                        review_entry = {"account": account["name"], "thread": thread,
                                                        "started": attempt_started, "finished": None, "result": None,
                                                        "round": len(st.get("rounds", []))}
                                        st.setdefault("reviews", []).append(review_entry)
                                    elif not reviewing:
                                        st["thread"] = thread
                                    if not reviewing and not any(t["account"] == account["name"] and t["thread"] == thread for t in st["threads"]):
                                        st["threads"].append({"account": account["name"], "thread": thread, "started": time.time()})
                                    if (st["rotations"] and not st["rotations"][-1].get("event")
                                            and not st["rotations"][-1]["thread"]):
                                        st["rotations"][-1]["thread"] = thread
                                    save(st)
                                if (event.get("type") == "item.completed"
                                        and (event.get("item") or {}).get("type") == "agent_message"):
                                    final_message = event["item"].get("text", "")
                                if control.repeated(event):
                                    return "stuck", None
                                if limit_error(event):
                                    reason = "usage limit"
                                if auth_error(event):
                                    authentication_failed = True
                                # `error` events also carry retries ("Reconnecting... 2/5") of turns that complete.
                                if event.get("type") == "turn.failed":
                                    failed = True
                    if wrapup:
                        # A wrap-up gets one bounded attempt, without quota waits or retries.
                        if authentication_failed or reason:
                            return "failed", None
                        continue
                    if authentication_failed:
                        return ("auth" if cfg["enabled"] else "failed"), None
                    if reason:
                        return quota_decision(current_info, reason, False) if cfg["enabled"] else ("failed", None)
                    if cfg["enabled"] and thread and proc.poll() is None:
                        now = time.monotonic()
                        if path is None and now >= next_search:
                            path = rollout_path(account["home"], thread)
                            next_search = now + 5
                        if path is not None and now >= next_poll:
                            next_poll = now + RATE_POLL_SECONDS
                            size = path.stat().st_size
                            if size == previous_size:
                                continue
                            limits = rollout_limits(path)
                            previous_size = size
                            if limits is not None:
                                current_info = dict(account, logged_in=True, limits=limits)
                                if exhausted(current_info, cfg):
                                    decision = quota_decision(current_info, limit_reason(current_info, cfg), True)
                                    if decision is not None:
                                        return decision
                code = proc.wait()
        finally:
            try:
                terminate(proc)
            finally:
                control.elapsed += max(0, time.monotonic() - active_started)
                proc.stdout.close()
                st.pop("codex_pid", None)
                message = last.read_text(encoding="utf-8").strip() if last.exists() else final_message
                if wrapup:
                    st["wrapup"]["result"] = parse_result(message)
                elif review_entry is not None:
                    review_entry["finished"] = time.time()
                    review_entry["result"] = parse_result(message, reviewing=True)
                elif not reviewing and st.get("kind") != "scout":
                    st["result"] = parse_result(message)
                save(st)
    if reviewing and not review_fallback and not thread and code == 2:
        with (d / "stderr.log").open("rb") as errors:
            errors.seek(error_start)
            error = errors.read().decode("utf-8", errors="replace")
        if all(part in error for part in ("cannot be used with", "--base", "PROMPT")):
            return monitor(st, cfg, account, prompt, effort, stopped, on_limit, review_fallback=True,
                           control=control)
    return ("completed" if code == 0 and not failed else "failed"), None


def wrapup_turn(st, cfg, account, effort, stopped, reason):
    """Publish the typed stop and allow exactly one short resume on its thread."""
    st["turn_stop"] = dict(reason, at=time.time())
    st["outcome"] = reason["outcome"]
    st["review_skipped"] = reason["outcome"]
    st["wrapup"] = {"outcome": "not_run", "result": None}
    save(st)
    notify(st, cfg, reason["outcome"], turn_stop_message(reason), account=account["name"], **reason)
    thread = ((st.get("reviews") or [{}])[-1].get("thread") if st.get("kind") == "review"
              else st.get("thread"))
    if not thread or stopped():
        return
    if st.get("kind") not in ("review", "scout"):
        st["result"] = None
    try:
        result, _ = monitor(st, cfg, account, wrapup_prompt(st, reason, cfg), effort, stopped,
                            lambda *args: None, control=TurnControl(10, cfg.get("repeat_limit", 3)),
                            wrapup=True)
        st["wrapup"]["outcome"] = result
    except (Exception, SystemExit) as exc:
        st["wrapup"].update(outcome="failed", error=str(exc))
    if st["wrapup"]["outcome"] != "completed":
        st["wrapup"]["result"] = None
        last = TASKS / st["slug"] / ("review.md" if st.get("kind") == "review" else "last.md")
        last.unlink(missing_ok=True)
    if st.get("kind") not in ("review", "scout"):
        st["result"] = st["wrapup"]["result"]
    st["outcome"] = "stopped" if stopped() else reason["outcome"]
    save(st)


def run_turn(st, cfg, prompt, effort, current_message, stopped, sleep, new_turn=True):
    """Run one work/scout/review phase, sharing quota, rotation and reset handling."""
    reviewing = st.get("kind") == "review"
    minutes = (cfg.get("max_review_minutes", 30) if st.get("kind") in ("review", "scout")
               else st.get("max_turn_minutes", cfg.get("max_turn_minutes")))
    control = TurnControl(minutes, cfg.get("repeat_limit", 3))
    visited = set()
    rotations = 0
    rotation = None
    if new_turn:
        st.update(auth_excluded=[], auth_excluded_ids=[], auth_transitions=0, auth_failures={})
        st.pop("reset_reconciled", None)
    account = account_named(cfg, st["account"])
    if cfg["enabled"]:
        infos = task_snapshots(st, cfg, stopped, new_turn=new_turn)
        current = next((i for i in infos if i["name"] == st["account"]), {})
        same_account = {i["name"] for i in infos if current.get("account_id")
                        and i.get("account_id") == current["account_id"]}
        resuming = bool(st.get("thread")) and not reviewing
        chosen = (current if resuming and current.get("logged_in") and not exhausted(current, cfg)
                  else available(infos, cfg, same_account if resuming else ()))
        if not chosen and cfg["credits"]:
            chosen = current if current.get("logged_in") and has_credits(current) else None
        if stopped():
            return
        if not chosen:
            chosen = wait_for_account(st, infos, cfg, "all accounts unavailable", stopped, sleep)
            if not chosen:
                return
        if resuming and chosen["name"] != st["account"]:
            prompt = handoff(st, current_message=current_message, cfg=cfg)
            st["rotations"].append({"at": time.time(), "from": st["account"], "to": chosen["name"],
                                    "reason": limit_reason(current, cfg), "thread": ""})
            rotation = st["rotations"][-1]
            rotations += 1
            visited.add(st["account"])
            st["thread"] = ""
        account = account_named(cfg, chosen["name"])
        if not reviewing:
            st["account"] = account["name"]
    if reviewing:
        st["review_account"] = account["name"]
    save(st)
    if rotation is not None:
        notify(st, cfg, "rotated", f"rotated from {rotation['from']} to {rotation['to']}",
               **{"from": rotation["from"], "to": rotation["to"], "account": account["name"]})

    def on_limit(local, reason, allow_credits, cancelled):
        infos = task_snapshots(st, cfg, cancelled)
        current = next((i for i in infos if i["name"] == account["name"]), {})
        if local and exhausted(local, cfg):
            # The RPC may lag the local rollout; keep its fresh credit balance.
            credits = current.get("limits", {}).get("credits", {})
            current["limits"] = dict(local["limits"], credits=credits)
        current["ordinary"] = False
        used_ids = {i.get("account_id") for i in infos if i["name"] in visited and i.get("account_id")}
        excluded = visited | {i["name"] for i in infos if i.get("account_id") in used_ids}
        chosen = available(infos, cfg, excluded) if rotations < len(cfg["accounts"]) else None
        if allow_credits and not chosen and cfg["credits"] and current.get("logged_in") and has_credits(current):
            return None
        return {"reason": reason, "infos": infos, "chosen": chosen}

    while not stopped():
        st.pop("reset_reconciled", None)
        visited.add(account["name"])
        result, decision = monitor(st, cfg, account, prompt, effort, stopped, on_limit, control=control)
        if result in ("auth", "limit") and control.expired(time.monotonic()):
            result = "budget_exhausted"
        if result != "auth":
            st.setdefault("auth_failures", {})[account["name"]] = 0
        if stopped() or result == "stopped":
            st["outcome"] = "stopped"
            break
        if result in ("budget_exhausted", "stuck"):
            reason = dict(control.reason, active_seconds=round(control.elapsed, 3))
            wrapup_turn(st, cfg, account, effort, stopped, reason)
            break
        if result in ("completed", "failed") or not cfg["enabled"]:
            st["outcome"] = result
            break
        if result == "auth":
            failures = st.setdefault("auth_failures", {})
            failures[account["name"]] = failures.get(account["name"], 0) + 1
            infos = task_snapshots(st, cfg, stopped)
            if stopped():
                break
            current = next((i for i in infos if i["name"] == account["name"]), {})
            capped = st["auth_transitions"] >= 2 * len(cfg["accounts"])
            if not capped and failures[account["name"]] == 1 and current.get("logged_in") and not exhausted(current, cfg):
                if not reviewing and st.get("thread"):
                    prompt = auth_resume_prompt(cfg)
                save(st)
                continue
            if failures[account["name"]] >= 2 or current.get("logged_in") is False:
                exclude_auth_account(st, account, current)
                infos = refresh_login_state(st, infos)
            if not infos or capped:
                names = st.get("needs_login", []) or [a["name"] for a in cfg["accounts"]]
                st.update(outcome="failed", error="all accounts fail with auth errors; " +
                          "; ".join("run login " + name for name in names))
                save(st)
                break
            decision = {"reason": "authentication error", "infos": infos,
                        "chosen": available(infos, cfg, {account["name"]})}
        chosen = decision["chosen"]
        if not chosen:
            chosen = wait_for_account(st, decision["infos"], cfg, decision["reason"], stopped, sleep)
            if not chosen:
                break
            # Fresh quota snapshots start a new rotation cycle, including past accounts.
            visited.clear()
            rotations = 0
        if chosen["name"] == account["name"]:
            if not reviewing and st.get("thread"):
                prompt = reset_resume_prompt(cfg)
            save(st)
            continue
        if not reviewing:
            prompt = handoff(st, current_message=current_message, cfg=cfg)
        st["rotations"].append({"at": time.time(), "from": account["name"], "to": chosen["name"],
                                "reason": decision["reason"], "thread": ""})
        rotation = st["rotations"][-1]
        if result == "auth":
            st["auth_transitions"] += 1
        account = account_named(cfg, chosen["name"])
        if reviewing:
            st["review_account"] = account["name"]
        else:
            st.update(account=account["name"], thread="")
        rotations += 1
        save(st)
        notify(st, cfg, "rotated", f"rotated from {rotation['from']} to {rotation['to']}",
               **{"from": rotation["from"], "to": rotation["to"], "account": account["name"]})


def execute_check(st, stopped, cwd, filename):
    """Run the saved check after work, owning and reaping its process group."""
    if stopped():
        return None
    started = time.monotonic()
    proc = None
    outcome = "failed"
    code = None
    with (TASKS / st["slug"] / filename).open("wb") as log:
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", st["check"]], cwd=cwd,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            st["check_pid"] = proc.pid
            save(st)
            deadline = started + st.get("check_timeout", 1800)
            while proc.poll() is None:
                if stopped():
                    outcome = "stopped"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = "timeout"
                    break
                time.sleep(min(0.1, remaining))
            else:
                outcome = "passed" if proc.returncode == 0 else "failed"
        except OSError as exc:
            log.write(f"check launch failed: {exc}\n".encode("utf-8", errors="replace"))
        finally:
            if proc is not None:
                signal_group(proc, signal.SIGKILL)
                code = proc.wait()
            st.pop("check_pid", None)
    if stopped():
        outcome = "stopped"
        st["outcome"] = "stopped"
    result = {"cmd": st["check"], "exit": code, "seconds": round(time.monotonic() - started, 3),
              "at": time.time(), "outcome": outcome}
    if outcome in ("failed", "timeout"):
        result["tail"] = check_tail(st, filename)
    return result


def run_check(st, stopped):
    result = execute_check(st, stopped, st["worktree"], "check.log")
    if result is None:
        return None
    st.setdefault("checks", []).append(result)
    save(st)
    return result


def run_base_check(st, stopped):
    """Check task HEAD with the base's guarded files, leaving the task untouched."""
    if st["worktree"] in st["check"]:
        return {"outcome": "skipped", "reason": "check command uses the task worktree's absolute path"}
    started = time.monotonic()
    log = TASKS / st["slug"] / "base-check.log"
    log.write_text("", encoding="utf-8")
    result = None
    paths = check_paths(st)
    try:
        with tempfile.TemporaryDirectory(prefix="base-check-", dir=str(TASKS / st["slug"])) as directory:
            worktree = Path(directory) / "worktree"
            try:
                subprocess.run(["git", "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD"],
                               cwd=st["worktree"], check=True, capture_output=True)
                guarded = set()
                for revision in (st["base"], "HEAD"):
                    files = sh(["git", "ls-tree", "-r", "--name-only", "-z", revision],
                               cwd=worktree)
                    guarded.update(path for path in files.split("\0") if path and guarded_path(path, st, paths))
                if guarded:
                    subprocess.run(["git", "--literal-pathspecs", "restore", "--source=" + st["base"],
                                    "--staged", "--worktree", "--pathspec-from-file=-", "--pathspec-file-nul"],
                                   input="\0".join(sorted(guarded)) + "\0", text=True,
                                   cwd=worktree, check=True, capture_output=True)
                execution = execute_check(st, stopped, worktree, "base-check.log")
                if execution is not None:
                    result = {key: execution[key] for key in ("outcome", "exit", "seconds")}
                    if result["outcome"] == "stopped":
                        result["outcome"] = "failed"
            finally:
                if (worktree / ".git").exists():
                    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                                   cwd=st["worktree"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError, SystemExit) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        with log.open("a", encoding="utf-8") as output:
            output.write("\nbase check setup/cleanup failed: " + (detail or str(exc)) + "\n")
        result = None
    if result is None:
        result = {"outcome": "failed", "exit": None, "seconds": round(time.monotonic() - started, 3)}
    result["tail"] = check_tail(st, "base-check.log")
    return result


def saved_check(st, cfg, stopped, review_after=True):
    check = run_check(st, stopped) if st.get("check") and not stopped() else None
    if check and check["outcome"] == "passed" and st.get("base_check") and not stopped():
        check["base_check"] = run_base_check(st, stopped)
        save(st)
    if check and check["outcome"] in ("failed", "timeout"):
        if review_after and not st.get("review_skipped"):
            st["review_skipped"] = "check " + check["outcome"]
        save(st)
        notify(st, cfg, "check_failed", f"check {check['outcome']} on {st['account']}",
               outcome=check["outcome"])
    return check


def review_turn(st, cfg, prompt, effort, stopped, sleep, new_turn=False):
    start = len(st.get("reviews", []))
    st.update(kind="review", outcome="running", reset_wait_seconds=0)
    save(st)
    try:
        run_turn(st, cfg, prompt, effort, False, stopped, sleep, new_turn=new_turn)
    except (Exception, SystemExit):
        st["outcome"] = "stopped" if stopped() else "failed"
        raise
    finally:
        reviews = st.get("reviews", [])[start:]
        result = reviews[-1].get("result") if reviews else None
        account = st.get("review_account") or (reviews[-1]["account"] if reviews else st["account"])
        outcome = "stopped" if stopped() else st["outcome"]
        notify(st, cfg, "review_done", f"review {outcome} on {account}",
               outcome=outcome, account=account,
               verdict=result["verdict"] if result else None,
               findings=len(result["findings"]) if result else 0, round=len(st.get("rounds", [])))
    return result


def auto_fix(st, cfg, review, effort, stopped, sleep):
    """Bounded work/check/review loop under the original supervisor and wait."""
    failed_check = None
    while not stopped() and st.get("outcome") == "completed":
        if failed_check is None:
            if review is None:
                st["auto_fix_stop"] = "review unknown"
                return
            if review["verdict"] == "clean":
                st["auto_fix_stop"] = "clean"
                return
            if review["findings"] and all(finding["severity"] == "P3" for finding in review["findings"]):
                st["auto_fix_stop"] = "only P3"
                return
        if len(st["rounds"]) >= st["fix_rounds"]:
            st["auto_fix_stop"] = "check " + failed_check["outcome"] if failed_check else "rounds_exhausted"
            return
        message = fix_prompt(st, review, failed_check, cfg)
        entry = {"review": None if failed_check else review, "decisions": [], "check": None}
        st["rounds"].append(entry)
        st.update(kind="work", result=None, outcome="running", reset_wait_seconds=0)
        st.pop("review_skipped", None)
        prompt = followup_prompt(st, message, cfg, source="review")
        save(st)
        work_paths = check_paths(st)
        run_turn(st, cfg, prompt, effort, True, stopped, sleep, new_turn=False)
        st["check_paths"] = sorted(set(work_paths) | set(check_paths(st)))
        result = st.get("result")
        entry["decisions"] = result["findings"] if result is not None else []
        save(st)
        if not stopped() and st.get("outcome") in ("budget_exhausted", "stuck"):
            entry["check"] = saved_check(st, cfg, stopped, review_after=False)
            save(st)
            return
        if stopped() or st.get("outcome") != "completed":
            return
        if result is None or result["status"] != "done":
            st["auto_fix_stop"] = "result " + (result["status"] if result else "unknown")
            st["review_skipped"] = st["auto_fix_stop"]
            return
        check = saved_check(st, cfg, stopped)
        entry["check"] = check
        save(st)
        if stopped():
            return
        if check is not None and check["outcome"] != "passed":
            failed_check = check
            continue
        failed_check = None
        st["work_completed"] = True
        review = review_turn(st, cfg, review_prompt(st, cfg), effort, stopped, sleep)


def cmd__run(a):
    stopped_flag = threading.Event()
    def stop(signum, frame):
        stopped_flag.set()
    def stopped():
        reap_notifications()
        return stopped_flag.is_set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    # The launcher publishes pid and pending before the supervisor becomes the only writer.
    for _ in range(200):
        st = load(a.slug)
        if st.get("pid") == os.getpid():
            break
        time.sleep(0.01)
    else:
        return
    cfg = None
    check = None
    try:
        cfg = accounts_config()
        pending = st.pop("pending")
        effort = pending["effort"]
        if st.get("kind") == "review":
            review_turn(st, cfg, pending["prompt"], effort, stopped, stopped_flag.wait, new_turn=True)
        else:
            work_paths = check_paths(st) if st.get("kind") != "scout" else None
            run_turn(st, cfg, pending["prompt"], effort, pending.get("current_message", False),
                     stopped, stopped_flag.wait)
            if work_paths is not None:
                st["check_paths"] = sorted(set(work_paths) | set(check_paths(st)))
                save(st)
        work_result = st.get("result") if st.get("kind") not in ("review", "scout") else None
        if (work_result is not None and work_result["status"] != "done"
                and pending.get("review_after") and st.get("outcome") == "completed"):
            st["review_skipped"] = "result " + work_result["status"]
            save(st)
        if (st.get("kind") not in ("review", "scout")
                and st.get("outcome") in ("completed", "budget_exhausted", "stuck")
                and st.get("check") and not stopped()):
            check = saved_check(st, cfg, stopped, pending.get("review_after"))
        if (st.get("kind") != "scout" and st.get("outcome") == "completed" and pending.get("review_after") and not stopped()
                and not st.get("review_skipped") and (check is None or check["outcome"] == "passed")):
            st["work_completed"] = True
            review = review_turn(st, cfg, review_prompt(st, cfg), effort, stopped, stopped_flag.wait)
            if st.get("fix_rounds"):
                auto_fix(st, cfg, review, effort, stopped, stopped_flag.wait)
            checks = st.get("checks", [])[st.get("check_start", 0):]
            check = checks[-1] if checks else None
    except (Exception, SystemExit) as exc:
        st.update(outcome="failed", error=str(exc))
        print(f"supervisor failed: {exc}", file=sys.stderr)
    finally:
        if stopped():
            st["outcome"] = "stopped"
        if st.get("fix_rounds") and (st.get("outcome") != "completed" or not st.get("auto_fix_stop")):
            st["auto_fix_stop"] = (st.get("outcome") if st.get("outcome") != "completed"
                                   else st.get("review_skipped", "review unknown"))
        st.pop("sleeping", None)
        st["turn_finished"] = time.time()
        st.pop("pending", None)
        if cfg is not None:
            account = st["account"]
            if st.get("kind") == "review":
                reviews = st.get("reviews", [])[st.get("review_start", 0):]
                account = st.get("review_account") or (reviews[-1]["account"] if reviews else account)
            message = f"turn {st['outcome']} on {account}"
            result = st.get("result")
            message += ", result " + (result["status"] if result is not None else "unknown")
            if check is not None:
                message += ", check " + check["outcome"]
            notify(st, cfg, "turn_finished", message, outcome=st["outcome"], account=account, result=result,
                   guarded_changed=len(guarded_changes(st)))
            if (result is not None and result["status"] in ("blocked", "needs_decision")
                    and st.get("kind") not in ("review", "scout")):
                question = next(iter(result["questions"]), "")
                notify(st, cfg, "needs_input", f"result {result['status']}: {question}",
                       result=result, question=question)
        reap_notifications()
        # Publishing idle permits clean to remove both the task and its worktree.
        st["pid"] = None
        save(st)


def cmd_start(a):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", a.slug):
        die("slug: 2-41 chars of a-z, 0-9 and '-'")
    cfg = accounts_config()
    if (TASKS / a.slug).exists():
        die(f"task {a.slug!r} exists; pick another slug or clean it")
    busy = running_tasks()
    if busy and not a.parallel:
        die(f"already running: {', '.join(busy)} (wait for it, or pass --parallel if they cannot clash)")
    brief = sys.stdin.read() if a.brief == "-" else Path(a.brief).read_text()
    if not brief.strip():
        die("empty brief")
    rules = rules_text(cfg)
    repo = main_repo(a.repo)
    branch = f"codex/{a.slug}"
    wt = WORKTREES / f"claude-{a.slug}" / repo.name
    if wt.exists():
        die(f"{wt} exists")
    sh(["git", "fetch", "--quiet", "origin"], cwd=repo)
    a.base = a.base or sh(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo,
                          check=False) or "origin/main"
    wt.parent.mkdir(parents=True, exist_ok=True)
    sh(["git", "worktree", "add", "--quiet", "-b", branch, str(wt), a.base], cwd=repo)
    d = TASKS / a.slug
    d.mkdir(parents=True)
    prompt = brief.rstrip() + "\n\n" + rules.replace("{branch}", branch).replace("{base}", a.base.removeprefix("origin/"))
    (d / "brief.md").write_text(prompt)
    st = {"slug": a.slug, "repo": str(repo), "worktree": str(wt), "branch": branch, "base": a.base,
          "effort": a.effort, "account": cfg["accounts"][0]["name"], "thread": "", "threads": [],
          "rotations": [], "reviews": [], "checks": [], "check": a.check, "check_timeout": a.check_timeout,
          "guards": a.guard, "base_check": a.base_check,
          "max_turn_minutes": a.max_minutes if a.max_minutes is not None else cfg.get("max_turn_minutes"),
          "created": time.time(), "turns": 0}
    save(st)
    proc = spawn(st, prompt, a.effort, review_after=a.review, fix_rounds=a.fix_rounds or 0)
    for _ in range(300):
        st = load(a.slug)
        if st["thread"] or st.get("outcome") == "sleeping" or st.get("pid") is None or proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"started {a.slug}: account {st['account']}, thread {st['thread'] or '?'}, pid {st.get('pid')}\n  worktree {wt}\n  branch {branch}")
    print(f"  wait: {Path(__file__)} wait {a.slug}   (run it as a background command)")


def require_idle(st):
    if alive(st.get("pid")):
        if st.get("outcome") == "sleeping":
            die(f"waiting for limit reset until {local_time(st['sleeping']['until'])}; stop first to send a new message")
        die(f"{st['slug']} is still running a turn; wait for it (or stop it) first")


def cmd_scout(a):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", a.slug):
        die("slug: 2-41 chars of a-z, 0-9 and '-'")
    cfg = accounts_config()
    if (TASKS / a.slug).exists():
        die(f"task {a.slug!r} exists; pick another slug or clean it")
    question = Path(a.file).read_text() if a.file else a.question
    if not question or not question.strip():
        die("empty scout question")
    directory = Path(a.repo).expanduser().resolve()
    if not directory.is_dir():
        die(f"scout directory does not exist: {directory}")
    d = TASKS / a.slug
    d.mkdir(parents=True)
    prompt = scout_prompt(question, cfg)
    (d / "brief.md").write_text(prompt)
    st = {"slug": a.slug, "kind": "scout", "repo": str(directory), "worktree": str(directory), "branch": "",
          "effort": a.effort, "account": cfg["accounts"][0]["name"], "thread": "", "threads": [],
          "rotations": [], "reviews": [], "checks": [], "created": time.time(), "turns": 0}
    save(st)
    proc = spawn(st, prompt, a.effort, kind="scout")
    for _ in range(300):
        st = load(a.slug)
        if st["thread"] or st.get("outcome") == "sleeping" or st.get("pid") is None or proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"started scout {a.slug}: account {st['account']}, thread {st['thread'] or '?'}, pid {st.get('pid')}\n  directory {directory}")
    print(f"  wait: {Path(__file__)} wait {a.slug}   (run it as a background command)")


def cmd_say(a):
    st = load(a.slug)
    scouting = st.get("kind") == "scout"
    if scouting and (a.check is not None or a.review or a.check_timeout is not None or a.fix_rounds is not None
                     or a.no_base_check):
        die("scout is read-only; say --check, --check-timeout, --review, --fix-rounds and --no-base-check are not supported")
    require_idle(st)
    msg = Path(a.file).read_text() if a.file else a.message
    if not msg or not msg.strip():
        die("empty message")
    cfg = accounts_config()
    minutes = getattr(a, "max_minutes", None)
    st["max_turn_minutes"] = minutes if minutes is not None else cfg.get("max_turn_minutes")
    if a.no_check:
        st["check"] = None
        st["check_paths"] = []
    elif a.check is not None:
        st["check"] = a.check
    if a.no_base_check:
        st["base_check"] = False
    if a.check_timeout is not None:
        st["check_timeout"] = a.check_timeout
    # If no thread was created (all accounts exhausted on start), retain the brief too.
    prompt = followup_prompt(st, msg, cfg)
    spawn(st, prompt, a.effort or st.get("effort"), current_message=True,
          kind="scout" if scouting else "work", review_after=a.review, fix_rounds=a.fix_rounds or 0)
    print(f"sent to {a.slug} (turn {st['turns']}), pid {st['pid']}")


def cmd_review(a):
    st = load(a.slug)
    if st.get("kind") == "scout":
        die("scout is read-only; review is not supported")
    require_idle(st)
    cfg = accounts_config()
    spawn(st, review_prompt(st, cfg), a.effort or st.get("effort"), kind="review")
    print(f"reviewing {a.slug}, pid {st['pid']}")


def cmd_wait(a):
    st = load(a.slug)
    start = time.monotonic()
    while alive(st.get("pid")):
        if st.get("outcome") == "sleeping" and not getattr(a, "through_sleep", False):
            break
        if a.timeout and time.monotonic() - start > a.timeout:
            print(f"{a.slug}: still running after {a.timeout} s")
            return
        time.sleep(1)
        st = load(a.slug)
    if st.get("pid") and not alive(st["pid"]):
        # The supervisor may have published its final state since the last poll.
        st = load(a.slug)
        if st.get("pid") and not alive(st["pid"]):
            st["pid"] = None
            save(st)
    for r in st["rotations"][st.get("rotation_start", 0):]:
        if r.get("event") == "sleep":
            print(f"== sleeping until {local_time(r['until'])} ({r['account'] or 'unknown'}): {r['reason']}")
        elif r.get("event") == "wake":
            print(f"== woke at {local_time(r['at'])} to recheck account limits")
        elif r.get("event") == "reset_credit":
            if r.get("outcome") in ("success", "consumed (inferred)"):
                print(f"== used a free limit reset on {r['account']} ({r['credit_id']}), "
                      f"expires {local_time(r.get('expires_at'))}")
        elif r.get("event") == "needs_login":
            print(f"== account {r['account']} needs login: run login {r['account']}")
        else:
            print(f"== rotated {r['from']} → {r['to']}: {r['reason']}, new thread {r['thread'] or '?'}")
    sleeping = st.get("outcome") == "sleeping"
    if sleeping:
        if alive(st.get("pid")):
            s = st["sleeping"]
            label = "account status unknown; retry at" if s.get("probe") else "all accounts at limit; auto-continue at"
            print(f"== {label} {local_time(s['until'])} "
                  f"({s['account'] or 'unknown'}); run wait again to follow")
        else:
            print("== paused, supervisor gone; continue with say")
    if st.get("exhausted"):
        e = st["exhausted"]
        reset = f"{local_time(e['reset'])} ({e['account']})" if e["reset"] is not None else "unknown"
        print(f"== all accounts at limit; earliest reset {reset}; continue with say after that")
    state = "paused" if sleeping else "finished"
    print(f"== {a.slug}: turn {st.get('turns')} {state} after "
          f"{(st.get('turn_finished', time.time()) - st.get('turn_started', time.time())) / 60:.1f} min")
    if st.get("turn_stop"):
        print("== turn stopped: " + turn_stop_message(st["turn_stop"]))
    if st.get("kind") == "review" and st.get("wrapup"):
        print("\n".join(result_lines(st["wrapup"]["result"], "(no wrap-up result)")))
    d = TASKS / a.slug
    if st.get("kind") != "review" or st.get("work_completed"):
        last = d / "last.md"
        if (st.get("exhausted") or sleeping) and len(events(st)) == st.get("event_start", 0):
            # Paused before any exec: the thread's last message belongs to an earlier turn.
            print("(Codex did not run this turn)")
        else:
            raw = ("(no wrap-up result)" if st.get("wrapup", {}).get("outcome") not in (None, "completed")
                   else last.read_text().strip() if last.exists() else last_message(st) or "(no final message)")
            print(raw if st.get("kind") == "scout" else "\n".join(result_lines(st.get("result"), raw)))
    for line in guarded_lines(guarded_changes(st)):
        print(line)
    for number, entry in enumerate(st.get("rounds", []), 1):
        findings = (entry.get("review") or {}).get("findings", [])
        counts = ", ".join(f"{severity} {sum(f['severity'] == severity for f in findings)}"
                           for severity in ("P1", "P2", "P3") if any(f["severity"] == severity for f in findings))
        source = f"{len(findings)} findings" + (f" ({counts})" if counts else "")
        if entry.get("review") is None:
            source = "check failure"
        decisions = entry.get("decisions", [])
        fixed = sum(d["decision"] == "fixed" for d in decisions)
        rejected = sum(d["decision"] == "rejected" for d in decisions)
        print(f"== round {number}: {source} → fixed {fixed}, rejected {rejected}")
        for decision in decisions:
            if decision["decision"] == "rejected":
                print(f"- rejected {decision['id']}: {decision['note']}")
    checks = [] if st.get("kind") == "scout" else st.get("checks", [])[st.get("check_start", 0):]
    for check in checks:
        label = {"passed": "passed", "stopped": "stopped"}.get(check["outcome"], "FAILED")
        reason = "timeout, " if check["outcome"] == "timeout" else ""
        print(f"== check `{check['cmd']}`: {label} ({reason}exit {check['exit']}, {check['seconds']:.0f} s)")
        if check["outcome"] in ("failed", "timeout"):
            print(check.get("tail", check_tail(st)))
        if check.get("base_check"):
            base = check["base_check"]
            if base["outcome"] == "skipped":
                print(f"== base tests: skipped ({base['reason']})")
            else:
                label = "passed" if base["outcome"] == "passed" else "FAILED"
                reason = "timeout, " if base["outcome"] == "timeout" else ""
                print(f"== base tests: {label} ({reason}exit {base['exit']}, {base['seconds']:.0f} s)")
            if base.get("tail"):
                print(base["tail"])
    if st.get("review_skipped"):
        print("== review skipped: " + st["review_skipped"])
    if st.get("kind") == "review" or st.get("rounds"):
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        if reviews:
            entry = reviews[-1]
            minutes = ((entry.get("finished") or time.time()) - entry["started"]) / 60
            heading = f"== review by {entry['account']} ({minutes:.1f} min):"
            result = entry.get("result")
            if result is None:
                print(heading)
                if st.get("kind") == "review" and st.get("wrapup"):
                    print("(review interrupted; see wrap-up above)")
                else:
                    print("== result: unknown (unstructured final message)")
                    review = d / "review.md"
                    print(review.read_text().strip() if review.exists() else "(no final review)")
            else:
                count = len(result["findings"])
                print(heading + (f" {count} defect{'s' if count != 1 else ''}" if count else " no defects"))
                print(result["summary"])
                for finding in result["findings"]:
                    print(f"[{finding['severity']}] {finding['id']} {finding['file']}:{finding['line']} — {finding['title']}")
                    print(finding["detail"])
                    print("verify: " + finding["verify"])
        else:
            print("(Codex did not run review)")
    if st.get("fix_rounds"):
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        trajectory = " → ".join(str(len(entry["result"]["findings"])) for entry in reviews
                                if entry.get("result") is not None) or "unknown"
        reason = st.get("auto_fix_stop", st.get("outcome", "unknown"))
        print(f"== auto-fix: {len(st.get('rounds', []))} rounds, findings {trajectory}; stop: {reason}")
    failed = [e for e in events(st)[st.get("exec_event_start", st.get("event_start", 0)):] if e.get("type") in ("turn.failed", "error")]
    if failed:
        print("== errors:", json.dumps(failed[-1], ensure_ascii=False)[:500])
    if st.get("error"):
        print("== supervisor error:", st["error"])
    if st.get("kind") == "scout":
        return
    print(f"== commits on {st['branch']}:")
    print(sh(["git", "log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False) or "(none)")
    dirty = sh(["git", "status", "--short"], cwd=st["worktree"], check=False)
    if dirty:
        print("== uncommitted:\n" + dirty)


def describe(st, detail):
    supervisor_alive = alive(st.get("pid"))
    if st.get("outcome") == "sleeping":
        state = (f"sleeping until {local_time(st['sleeping']['until'])}" if supervisor_alive
                 else "paused, supervisor gone; continue with say")
    else:
        state = "running" if supervisor_alive else "idle"
    reviewing = st.get("kind") == "review"
    scouting = st.get("kind") == "scout"
    account, thread = st["account"], st.get("thread", "")
    if reviewing:
        account, thread = st.get("review_account", "?"), ""
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        if reviews:
            account, thread = reviews[-1]["account"], reviews[-1]["thread"]
    location = st["worktree"] if scouting else st["branch"]
    label = "  scout" if scouting else ("  review" if reviewing else "")
    line = (f"{st['slug']:24} {state:8} turns {st.get('turns', 0)}  {location}  "
            f"account {account}  thread {thread[:13]}" + label)
    if not detail:
        return line
    if scouting:
        return line + "\nlast message:\n" + (last_message(st) or "(none)")
    pr = (sh(["gh", "pr", "list", "--head", st["branch"], "--state", "all", "--json", "number,state,url",
              "--jq", '.[] | "\\(.number) \\(.state) \\(.url)"'], cwd=st["worktree"], check=False)
          if shutil.which("gh") else "")
    commits = sh(["git", "log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False)
    review = TASKS / st["slug"] / "review.md"
    message = review.read_text().strip() if reviewing and review.exists() else last_message(st)
    return "\n".join([line, f"worktree {st['worktree']}", *result_lines(st.get("result")),
                      f"PR: {pr or 'none'}", "commits:\n" + (commits or "(none)"),
                      "last message:\n" + (message or "(none)")])


def cmd_status(a):
    if a.slug:
        print(describe(load(a.slug), True))
        return
    for p in sorted(TASKS.glob("*/state.json")):
        print(describe(load(p.parent.name), False))


def cmd_stats(a):
    st = load(a.slug)
    cfg = accounts_config()
    for entry in st["threads"] + st["reviews"]:
        account = account_named(cfg, entry["account"])
        path = rollout_path(account["home"], entry["thread"])
        print(f"== {entry['account']}: {entry['thread']}", flush=True)
        if path:
            subprocess.run([sys.executable, "-I", str(STATS), str(path)], check=True)
        else:
            print("(rollout not found)")


def cmd_stop(a):
    st = load(a.slug)
    pid = st.get("pid")
    if not alive(pid):
        print("not running")
        return
    try:
        os.killpg(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(100):
        time.sleep(0.2)
        latest = load(a.slug)
        if latest.get("pid") != pid or not alive(pid):
            break
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    print(f"stopped {a.slug}")


def cmd_clean(a):
    st = load(a.slug)
    if alive(st.get("pid")):
        die("still running; stop it first")
    if st.get("kind") != "scout":
        repo, wt = st["repo"], st["worktree"]
        if Path(wt).exists():
            sh(["git", "worktree", "remove", "--force", wt], cwd=repo)
            shutil.rmtree(Path(wt).parent, ignore_errors=True)
        if not a.keep_branch:
            subprocess.run(["git", "branch", "-D", st["branch"]], cwd=repo, capture_output=True)
    cfg = accounts_config()
    for entry in st["threads"] + st["reviews"]:
        account = account_named(cfg, entry["account"])
        prepare_home(account)
        subprocess.run([codex_binary(), "archive", entry["thread"]], env=account_env(account), capture_output=True)
    shutil.rmtree(TASKS / a.slug)
    print(f"cleaned {a.slug}")


def cmd_accounts(a):
    cfg = accounts_config()
    print_accounts(snapshots(cfg), cfg)


def print_accounts(infos, cfg):
    print("NAME\tEMAIL\tPLAN\tWEEKLY %\tRESET (LOCAL)\tCREDITS\tRESETS\tSTATUS")
    for info in infos:
        w = longest_window(info)
        credits = info.get("limits", {}).get("credits", {})
        if info.get("duplicate"):
            status = "same account as " + info["duplicate"]
        elif info["logged_in"] is None:
            status = "unknown (rpc error: " + info.get("error", "unavailable") + ")"
        elif not info["logged_in"]:
            status = "not logged in"
        elif exhausted(info, cfg):
            status = "exhausted until " + local_time(reset_time(info, cfg))
        else:
            status = "available"
        account = info.get("account", {})
        resets = reset_credits(info)
        count = info.get("reset_credits", {}).get("availableCount", len(resets))
        nearest = resets[0].get("expiresAt") if resets else None
        reset_label = str(count) + (f" (exp {local_time(nearest).split()[0]})" if nearest is not None else "")
        print("\t".join((info["name"], account.get("email") or "—", account.get("planType") or "—",
                         f"{w['used']:g}" if w else "—", local_time(w.get("reset")),
                         "unlimited" if credits.get("unlimited") else str(credits.get("balance") or "0"),
                         reset_label, status)))


def cmd_reset_credit(a):
    cfg = accounts_config()
    account = account_named(cfg, a.account)
    try:
        action = reset_credit_action(account, a.credit, confirmed=a.yes)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        die(f"free limit reset failed: {exc}")
    credit = action["credit"]
    print(f"Free limit reset on {a.account}: {credit['id']} ({credit.get('title') or 'Full reset'}), "
          f"expires {local_time(credit.get('expiresAt'))}")
    if action["outcome"] == "consumed (inferred)":
        print("consumed (inferred)")
        print_accounts([action["info"]], cfg)
        return
    if not a.yes:
        print("Preview only; pass --yes to consume this reset.")
        return
    print(json.dumps(action["response"], ensure_ascii=False, indent=2))
    print_accounts([action["info"]], cfg)


def cmd_login(a):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,40}", a.name):
        die("invalid account name")
    cfg = accounts_config()
    account = next((i for i in cfg["accounts"] if i["name"] == a.name), None)
    if account is None:
        account = {"name": a.name, "home": str(CODEX_HOME.parent / ".codex-accounts" / a.name)}
        cfg["accounts"].append(account)
    cfg.pop("enabled", None)
    CODEX_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = CODEX_HOME / "codex-worker.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
    prepare_home(account)
    binary = codex_binary()
    os.execvpe(binary, [binary, "login"] + ([] if a.browser else ["--device-auth"]), account_env(account))


def check_timeout(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("check timeout must be a positive finite number")
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("check timeout must be a positive finite number")
    return seconds


def max_minutes(value):
    try:
        minutes = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max minutes must be a positive finite number")
    if not math.isfinite(minutes) or minutes <= 0:
        raise argparse.ArgumentTypeError("max minutes must be a positive finite number")
    return minutes


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("slug")
    s.add_argument("--brief", required=True)
    s.add_argument("--repo", default=".")
    s.add_argument("--base", default=None, help="default: the remote's default branch (origin/HEAD)")
    s.add_argument("--effort", default=None, help="default: model_reasoning_effort from ~/.codex/config.toml")
    s.add_argument("--parallel", action="store_true")
    s.add_argument("--max-minutes", type=max_minutes, help="active exec time budget (default: max_turn_minutes setting)")
    s.add_argument("--review", action="store_true", help="review in a fresh thread after successful completion")
    s.add_argument("--fix-rounds", type=int, choices=range(6), default=None,
                   help="automatic fix rounds after review, 0–5 (default: 0; requires --review)")
    s.add_argument("--check", help="shell command to run after completed work turns; saved for follow-ups")
    s.add_argument("--check-timeout", type=check_timeout, default=1800, help="check timeout in seconds (default: 1800)")
    s.add_argument("--guard", action="append", default=[], metavar="GLOB", help="guard matching repository paths (repeatable)")
    s.add_argument("--base-check", action="store_true", help="also check task HEAD with guarded files restored from base")
    s = sub.add_parser("scout", help="answer a code question in an existing directory, read-only")
    s.add_argument("slug")
    s.add_argument("question", nargs="?")
    s.add_argument("--file")
    s.add_argument("--repo", default=".")
    s.add_argument("--effort", default=None)
    scout_parser = s
    s = sub.add_parser("say")
    s.add_argument("slug")
    s.add_argument("message", nargs="?")
    s.add_argument("--file")
    s.add_argument("--effort", default=None)
    s.add_argument("--max-minutes", type=max_minutes, help="active exec time budget (default: max_turn_minutes setting)")
    s.add_argument("--review", action="store_true", help="review in a fresh thread after successful completion")
    s.add_argument("--fix-rounds", type=int, choices=range(6), default=None,
                   help="automatic fix rounds after review, 0–5 (default: 0; requires --review)")
    checks = s.add_mutually_exclusive_group()
    checks.add_argument("--check", help="replace the saved check command")
    checks.add_argument("--no-check", action="store_true", help="remove the saved check command")
    s.add_argument("--check-timeout", type=check_timeout, default=None, help="replace the saved check timeout")
    s.add_argument("--no-base-check", action="store_true", help="disable the saved base check")
    s = sub.add_parser("review")
    s.add_argument("slug")
    s.add_argument("--effort", default=None)
    s = sub.add_parser("wait")
    s.add_argument("slug")
    s.add_argument("--timeout", type=int, default=0)
    s.add_argument("--through-sleep", action="store_true", help="wait through limit resets until the turn ends")
    s = sub.add_parser("status")
    s.add_argument("slug", nargs="?")
    for name in ("stats", "stop", "_run"):
        sub.add_parser(name).add_argument("slug")
    s = sub.add_parser("clean")
    s.add_argument("slug")
    s.add_argument("--keep-branch", action="store_true")
    sub.add_parser("accounts", help="read each account's limits (no task is started)")
    s = sub.add_parser("reset-credit", help="preview or consume a free limit reset")
    s.add_argument("account")
    s.add_argument("--credit", help="choose an available reset by ID")
    s.add_argument("--yes", action="store_true", help="consume the reset (default: preview only)")
    s = sub.add_parser("login", help="sign in interactively to an isolated account home")
    s.add_argument("name")
    s.add_argument("--browser", action="store_true", help="use browser login instead of device auth")
    if sys.argv[1:2] == ["scout"]:
        # Intermixed parsing is supported on the scout parser, not on subparsers as a group.
        a = scout_parser.parse_intermixed_args(sys.argv[2:])
        a.cmd = "scout"
        if a.question is None and a.file is None:
            scout_parser.error("one of the arguments question --file is required")
        if a.question is not None and a.file is not None:
            scout_parser.error("argument --file: not allowed with argument question")
    else:
        a = p.parse_args()
    if a.cmd in ("start", "say") and a.fix_rounds is not None and not a.review:
        p.error("--fix-rounds requires --review")
    if a.cmd in ("start", "scout", "say", "review", "_run", "accounts", "reset-credit", "login", "clean"):
        global CODEX
        CODEX = resolve_codex()
    globals()["cmd_" + a.cmd.replace("-", "_")](a)


if __name__ == "__main__":
    main()
