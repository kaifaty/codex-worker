"""Account homes, app-server RPC and subscription limits."""
import json
import os
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, procs


def account_named(cfg, name):
    for a in cfg["accounts"]:
        if a["name"] == name:
            return a
    procs.die(f"account {name!r} is absent from the codex-worker account settings")


def account_env(account):
    return dict(os.environ, CODEX_HOME=account["home"])


def prepare_home(account):
    """Share settings, never credentials. In particular, never open auth.json."""
    home = Path(account["home"])
    if home == config.CODEX_HOME:
        return
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    source, target = config.CODEX_HOME / "config.toml", home / "config.toml"
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
            dest.symlink_to(config.CODEX_HOME / name)


class RPCError(RuntimeError):
    pass


@contextmanager
def account_rpc(account, cancelled=lambda: False):
    """One initialized app-server, ignoring unrelated replies; one 20s deadline."""
    proc = None
    try:
        proc = subprocess.Popen([config.codex_binary(), "app-server"], env=account_env(account), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + config.ACCOUNT_TIMEOUT
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
            procs.signal_group(proc, signal.SIGKILL)
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


def claude_reviewer_line():
    try:
        limits = json.loads((config.TASKS / "claude-limits.json").read_text(encoding="utf-8"))
        windows = limits.get("unifiedWindows") or {}
    except (OSError, ValueError, AttributeError):
        return "claude reviewer: unknown"
    parts = []
    for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = windows.get(name) or {}
        used = window.get("utilization")
        percent = f"{used * 100:g}%" if type(used) in (int, float) else "unknown"
        parts.append(f"{label} {percent} (reset {config.local_time(window.get('resetsAt'))})")
    return "claude reviewer: " + "; ".join(parts)
