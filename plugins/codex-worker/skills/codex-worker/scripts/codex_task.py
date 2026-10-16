#!/usr/bin/env python3
"""Delegate coding to Codex in a git worktree, with autonomous subscription rotation.

  codex_task.py start <slug> --brief FILE|- [--repo DIR] [--base origin/<branch>] [--effort E] [--parallel] [--review]
  codex_task.py say <slug> "message" | --file FILE [--review]  follow-up (task must be idle)
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
credits (false) and wait_for_reset (true). Legacy claude-accounts.json is a fallback.
Without an accounts setting, tasks use main (~/.codex) without quota monitoring or rotation.
State/logs: ~/.codex/claude-tasks/<slug>/; worktree: ~/.codex/worktrees/claude-<slug>/<repo>.
CODEX_TASK_HOME and CODEX_TASK_CODEX override the main home and CLI binary (also for tests).
The internal _run <slug> supervisor survives the caller and opens a new thread on rotation.
"""
import argparse
import fcntl
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
import threading
import time
import uuid
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
RESET_RETRY_SECONDS = 15 * 60
MAX_RESET_WAIT_SECONDS = 8 * 24 * 60 * 60


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


def sh(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
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
    if cfg["language"] not in ("en", "ru"):
        die('language must be "en" or "ru"')
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


def consume_reset_credit(account, credit, slug="", cancelled=lambda: False):
    """Pin the intent to a durable key before sending, including after a lost reply."""
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
        key = None
        last_line = ""
        for line in journal:
            last_line = line
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("creditId") == credit["id"] and entry.get("idempotencyKey"):
                key = entry["idempotencyKey"]
        if cancelled():
            raise RuntimeError("stopped")
        if last_line and not last_line.endswith("\n"):
            journal.write("\n")
        intent = {"slug": slug, "account": account["name"], "creditId": credit["id"],
                  "idempotencyKey": key or str(uuid.uuid4())}

        def record(**fields):
            journal.write(json.dumps(dict(intent, at=time.time(), **fields), ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())

        record(event="attempt")
        try:
            with account_rpc(account, cancelled) as call:
                response = call("account/rateLimitResetCredit/consume",
                                {"creditId": intent["creditId"], "idempotencyKey": intent["idempotencyKey"]})
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            # An RPC timeout can be returned as an error response too.
            unknown = not isinstance(exc, RPCError) or "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
            record(event="result", outcome="unknown" if unknown else "error", error=str(exc))
            raise
        record(event="result", outcome="success", response=response)
        return response


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
        if st["slug"] != exclude and alive(st.get("pid")):
            out.append(st["slug"])
    return out


def main_repo(path):
    """The main checkout of the repository `path` belongs to (including worktrees)."""
    common = Path(sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path))
    return common.parent


def spawn(st, prompt, effort=None, current_message=False, kind="work", review_after=False):
    d = TASKS / st["slug"]
    for name in (("last.md", "review.md") if kind == "work" else ("review.md",)):
        last = d / name
        if last.exists():
            last.unlink()
    st.update(pending={"prompt": prompt, "effort": effort, "current_message": current_message,
                       "review_after": review_after}, kind=kind, work_completed=False,
              review_start=len(st.get("reviews", [])), turn_started=time.time(),
              turns=st.get("turns", 0) + 1, rotation_start=len(st["rotations"]),
              event_start=len(events(st)), exhausted=None, outcome="running", reset_wait_seconds=0)
    st.pop("sleeping", None)
    st.pop("turn_finished", None)
    st.pop("error", None)
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
    d = TASKS / st["slug"]
    prompt = (d / "brief.md").read_text().rstrip() + ("\n\n## Продолжение\n\n" if ru else "\n\n## Continuation\n\n")
    has_threads = bool(st.get("threads") or st.get("thread"))
    if has_threads:
        prompt += (("Предыдущий исполнитель остановился посреди работы, потому что у его подписки кончился лимит.\n"
                    "Его коммиты и незакоммиченные правки в этом worktree — отправная точка. "
                    "Не начинай заново и не откатывай сделанную работу.\n\n") if ru else
                   ("The previous worker stopped partway through the task because its subscription reached its limit.\n"
                    "Its commits and uncommitted changes in this worktree are your starting point. "
                    "Continue the existing work without starting over or reverting it.\n\n"))
    prompt += "Сообщения от Claude по порядку:\n" if ru else "Messages from Claude, in order:\n"
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
    prompt += f"\n{'Коммиты' if ru else 'Commits'} (git log --oneline {st['base']}..HEAD):\n"
    prompt += sh(["git", "log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False) or ("(нет)" if ru else "(none)")
    prompt += ("\n\nСостояние worktree (git status --short):\n" if ru else "\n\nWorktree status (git status --short):\n")
    prompt += sh(["git", "status", "--short"], cwd=st["worktree"], check=False) or ("(чисто)" if ru else "(clean)")
    if has_threads:
        prompt += (("\n\nПоследние сообщения предыдущего исполнителя:\n" if ru else "\n\nLatest messages from the previous worker:\n")
                   + "\n\n".join(thread_messages(st)[-3:])[-4000:])
    prompt += (f"\n\nСначала посмотри git status и git diff {st['base']}, потом продолжай работу.\n" if ru else
               f"\n\nFirst inspect git status and git diff {st['base']}, then continue the task.\n")
    return prompt


def review_prompt(st, cfg=None):
    cfg = accounts_config() if cfg is None else cfg
    brief = TASKS / st["slug"] / "brief.md"
    if cfg.get("language", "en") == "ru":
        return (f"Проведи ревью изменений задачи: git diff {shlex.quote(st['base'])}.\n"
                f"Бриф задачи — в файле {brief}. Прочитай его и сверяйся с разделом «Готово, когда».\n"
                "Ищи только реальные дефекты: неверное поведение, невыполненные пункты брифа, "
                "сломанные или фиктивные проверки, опасные побочные эффекты, гонки.\n"
                "Для каждого дефекта укажи файл:строка, что не так и как это проверить. "
                "Без замечаний по стилю.\n"
                "Файлы не меняй, ничего не коммить. Если дефектов нет — так и напиши.")
    return (f"Review the task changes: git diff {shlex.quote(st['base'])}.\n"
            f"Read the task brief at {brief} and check its acceptance criteria.\n"
            "Look only for real defects: incorrect behavior, unmet brief requirements, "
            "broken or ineffective checks, dangerous side effects, and races.\n"
            "For each defect, give file:line, explain what is wrong and how to verify it. "
            "Do not comment on style.\n"
            "Do not change files or create commits. If there are no defects, say so.")


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


def try_reset_credit(st, infos, cfg, stopped):
    """Return a fresh snapshot only after a successful consume; never retry here."""
    stamp, _ = earliest_reset(infos, cfg)
    if (not cfg.get("use_reset_credits", False) or stamp is None
            or stamp - time.time() <= cfg.get("reset_credit_min_hours", 12) * 3600
            or available(infos, cfg)):
        return None
    choices = [(credit, info) for info in infos if info.get("logged_in") and not info.get("duplicate")
               for credit in reset_credits(info)]
    if not choices or stopped():
        return None
    credit, info = min(choices, key=lambda pair: pair[0].get("expiresAt")
                       if pair[0].get("expiresAt") is not None else float("inf"))
    event = {"event": "reset_credit", "at": time.time(), "account": info["name"],
             "credit_id": credit["id"], "expires_at": credit.get("expiresAt")}
    try:
        consume_reset_credit(info, credit, st["slug"], stopped)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        event.update(outcome="error", error=str(exc))
    else:
        event["outcome"] = "success"
    st["rotations"].append(event)
    save(st)
    if event["outcome"] == "success" and not stopped():
        return snapshots(cfg, stopped, prepare=True)
    return None


def refresh_login_state(st, infos):
    """Login flags describe fresh probes; they never blacklist an account."""
    names = list(st.get("needs_login", []))
    for info in infos:
        if info.get("logged_in") is True and info["name"] in names:
            names.remove(info["name"])
        elif info.get("logged_in") is False and info["name"] not in names:
            names.append(info["name"])
            st["rotations"].append({"event": "needs_login", "at": time.time(), "account": info["name"]})
    if names != st.get("needs_login", []):
        st["needs_login"] = names
        save(st)
    return infos


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
        refreshed = None if unknown else try_reset_credit(st, infos, cfg, cancelled)
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
        infos = refresh_login_state(st, snapshots(cfg, cancelled, prepare=True))
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


def monitor(st, cfg, account, prompt, effort, stopped, on_limit, review_fallback=False):
    """One exec attempt. The supervisor alone owns state; append CLI events verbatim."""
    d = TASKS / st["slug"]
    prepare_home(account)
    reviewing = st.get("kind") == "review"
    thread = "" if reviewing else st.get("thread", "")
    review_entry = None
    last = d / ("review.md" if reviewing else "last.md")
    if last.exists():
        last.unlink()
    args = [codex_binary(), "exec"]
    if reviewing:
        # Some CLI versions reject --base together with custom instructions.
        if not review_fallback:
            args += ["review", "--base", st["base"]]
        args += ["--json", "-o", str(last)]
    elif thread:
        args += ["resume", thread, "--json", "-o", str(last)]
    else:
        args += ["--json", "-C", st["worktree"], "-o", str(last)]
    args += effort_args(effort) + [prompt]
    path = rollout_path(account["home"], thread) if thread else None
    previous_size = path.stat().st_size if path else 0
    next_search = time.monotonic() + 5 if thread else 0.0
    next_poll = 0.0
    reason = None
    current_info = None
    failed = False
    authentication_failed = False
    st["exec_event_start"] = len(events(st))
    save(st)
    with (d / "events.jsonl").open("ab", buffering=0) as log, (d / "stderr.log").open("a") as errors:
        if stopped():
            return "stopped", None
        error_start = errors.tell()
        attempt_started = time.time()
        proc = subprocess.Popen(args, cwd=st["worktree"], env=account_env(account), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
        st["codex_pid"] = proc.pid
        save(st)
        buf = b""
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(proc.stdout, selectors.EVENT_READ)
                eof = False
                while not eof or proc.poll() is None:
                    if stopped():
                        terminate(proc)
                        return "stopped", None
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
                                    if reviewing:
                                        review_entry = {"account": account["name"], "thread": thread,
                                                        "started": attempt_started, "finished": None}
                                        st.setdefault("reviews", []).append(review_entry)
                                    else:
                                        st["thread"] = thread
                                    if not reviewing and not any(t["account"] == account["name"] and t["thread"] == thread for t in st["threads"]):
                                        st["threads"].append({"account": account["name"], "thread": thread, "started": time.time()})
                                    if (st["rotations"] and not st["rotations"][-1].get("event")
                                            and not st["rotations"][-1]["thread"]):
                                        st["rotations"][-1]["thread"] = thread
                                    save(st)
                                if limit_error(event):
                                    reason = "usage limit"
                                if auth_error(event):
                                    authentication_failed = True
                                # `error` events also carry retries ("Reconnecting... 2/5") of turns that complete.
                                if event.get("type") == "turn.failed":
                                    failed = True
                    if authentication_failed:
                        return ("auth" if cfg["enabled"] else "failed"), None
                    if reason:
                        return ("limit", on_limit(current_info, reason, False)) if cfg["enabled"] else ("failed", None)
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
                                    decision = on_limit(current_info, limit_reason(current_info, cfg), True)
                                    if decision is not None:
                                        return "limit", decision
                code = proc.wait()
        finally:
            try:
                terminate(proc)
            finally:
                proc.stdout.close()
                st.pop("codex_pid", None)
                if review_entry is not None:
                    review_entry["finished"] = time.time()
                save(st)
    if reviewing and not review_fallback and not thread and code == 2:
        with (d / "stderr.log").open("rb") as errors:
            errors.seek(error_start)
            error = errors.read().decode("utf-8", errors="replace")
        if all(part in error for part in ("cannot be used with", "--base", "PROMPT")):
            return monitor(st, cfg, account, prompt, effort, stopped, on_limit, review_fallback=True)
    return ("completed" if code == 0 and not failed else "failed"), None


def run_turn(st, cfg, prompt, effort, current_message, stopped, sleep):
    """Run one work/review phase, sharing quota, rotation and reset handling."""
    reviewing = st.get("kind") == "review"
    visited = set()
    rotations = 0
    auth_failures = 0
    account = account_named(cfg, st["account"])
    if cfg["enabled"]:
        infos = refresh_login_state(st, snapshots(cfg, stopped, prepare=True))
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
            rotations += 1
            visited.add(st["account"])
            st["thread"] = ""
        account = account_named(cfg, chosen["name"])
        if not reviewing:
            st["account"] = account["name"]
    if reviewing:
        st["review_account"] = account["name"]
    save(st)

    def on_limit(local, reason, allow_credits):
        infos = refresh_login_state(st, snapshots(cfg, stopped, prepare=True))
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
        visited.add(account["name"])
        result, decision = monitor(st, cfg, account, prompt, effort, stopped, on_limit)
        if stopped() or result == "stopped":
            st["outcome"] = "stopped"
            break
        if result in ("completed", "failed") or not cfg["enabled"]:
            st["outcome"] = result
            break
        if result == "auth":
            auth_failures += 1
            infos = refresh_login_state(st, snapshots(cfg, stopped, prepare=True))
            if stopped():
                break
            current = next((i for i in infos if i["name"] == account["name"]), {})
            if auth_failures == 1 and current.get("logged_in") and not exhausted(current, cfg):
                if not reviewing and st.get("thread"):
                    prompt = auth_resume_prompt(cfg)
                save(st)
                continue
            decision = {"reason": "authentication error", "infos": infos,
                        "chosen": available(infos, cfg, {account["name"]})}
        else:
            auth_failures = 0
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
        account = account_named(cfg, chosen["name"])
        auth_failures = 0
        if reviewing:
            st["review_account"] = account["name"]
        else:
            st.update(account=account["name"], thread="")
        rotations += 1
        save(st)


def cmd__run(a):
    stopped_flag = threading.Event()
    def stop(signum, frame):
        stopped_flag.set()
    def stopped():
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
    try:
        cfg = accounts_config()
        pending = st.pop("pending")
        effort = pending["effort"]
        run_turn(st, cfg, pending["prompt"], effort, pending.get("current_message", False),
                 stopped, stopped_flag.wait)
        if st.get("outcome") == "completed" and pending.get("review_after") and not stopped():
            st.update(kind="review", work_completed=True, outcome="running", reset_wait_seconds=0)
            save(st)
            run_turn(st, cfg, review_prompt(st, cfg), effort, False, stopped, stopped_flag.wait)
    except (Exception, SystemExit) as exc:
        st.update(outcome="failed", error=str(exc))
        print(f"supervisor failed: {exc}", file=sys.stderr)
    finally:
        if stopped():
            st["outcome"] = "stopped"
        st.pop("sleeping", None)
        st.update(pid=None, turn_finished=time.time())
        st.pop("pending", None)
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
          "rotations": [], "reviews": [], "created": time.time(), "turns": 0}
    save(st)
    proc = spawn(st, prompt, a.effort, review_after=a.review)
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


def cmd_say(a):
    st = load(a.slug)
    require_idle(st)
    msg = Path(a.file).read_text() if a.file else a.message
    if not msg or not msg.strip():
        die("empty message")
    cfg = accounts_config()
    with (TASKS / a.slug / "messages.jsonl").open("a") as f:
        f.write(json.dumps({"at": time.time(), "text": msg}, ensure_ascii=False) + "\n")
    # If no thread was created (all accounts exhausted on start), retain the brief too.
    prompt = msg if st.get("thread") else handoff(st, current_message=True, cfg=cfg)
    spawn(st, prompt, a.effort or st.get("effort"), current_message=True, review_after=a.review)
    print(f"sent to {a.slug} (turn {st['turns']}), pid {st['pid']}")


def cmd_review(a):
    st = load(a.slug)
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
            if r.get("outcome") == "success":
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
    d = TASKS / a.slug
    if st.get("kind") != "review" or st.get("work_completed"):
        last = d / "last.md"
        if (st.get("exhausted") or sleeping) and len(events(st)) == st.get("event_start", 0):
            # Paused before any exec: the thread's last message belongs to an earlier turn.
            print("(Codex did not run this turn)")
        else:
            print(last.read_text().strip() if last.exists() else last_message(st) or "(no final message)")
    if st.get("kind") == "review":
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        if reviews:
            entry = reviews[-1]
            minutes = ((entry.get("finished") or time.time()) - entry["started"]) / 60
            print(f"== review by {entry['account']} ({minutes:.1f} min):")
            review = d / "review.md"
            print(review.read_text().strip() if review.exists() else "(no final review)")
        else:
            print("(Codex did not run review)")
    failed = [e for e in events(st)[st.get("exec_event_start", st.get("event_start", 0)):] if e.get("type") in ("turn.failed", "error")]
    if failed:
        print("== errors:", json.dumps(failed[-1], ensure_ascii=False)[:500])
    if st.get("error"):
        print("== supervisor error:", st["error"])
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
    account, thread = st["account"], st.get("thread", "")
    if reviewing:
        account, thread = st.get("review_account", "?"), ""
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        if reviews:
            account, thread = reviews[-1]["account"], reviews[-1]["thread"]
    line = (f"{st['slug']:24} {state:8} turns {st.get('turns', 0)}  {st['branch']}  "
            f"account {account}  thread {thread[:13]}" + ("  review" if reviewing else ""))
    if not detail:
        return line
    pr = (sh(["gh", "pr", "list", "--head", st["branch"], "--state", "all", "--json", "number,state,url",
              "--jq", '.[] | "\\(.number) \\(.state) \\(.url)"'], cwd=st["worktree"], check=False)
          if shutil.which("gh") else "")
    commits = sh(["git", "log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False)
    review = TASKS / st["slug"] / "review.md"
    message = review.read_text().strip() if reviewing and review.exists() else last_message(st)
    return "\n".join([line, f"worktree {st['worktree']}", f"PR: {pr or 'none'}",
                      "commits:\n" + (commits or "(none)"), "last message:\n" + (message or "(none)")])


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
    info = account_read(account)
    if info.get("logged_in") is None:
        die(f"account {a.account} status unknown (rpc error: {info.get('error', 'unavailable')})")
    if not info.get("logged_in"):
        die(f"account {a.account} is not logged in: run login {a.account}")
    choices = reset_credits(info)
    credit = next((c for c in choices if c["id"] == a.credit), None) if a.credit else next(iter(choices), None)
    if credit is None:
        die(f"no available free limit reset for {a.account}" + (f" with ID {a.credit}" if a.credit else ""))
    print(f"Free limit reset on {a.account}: {credit['id']} ({credit.get('title') or 'Full reset'}), "
          f"expires {local_time(credit.get('expiresAt'))}")
    if not a.yes:
        print("Preview only; pass --yes to consume this reset.")
        return
    try:
        response = consume_reset_credit(account, credit)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        die(f"free limit reset failed: {exc}")
    print(json.dumps(response, ensure_ascii=False, indent=2))
    print_accounts([account_read(account)], cfg)


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
    s.add_argument("--review", action="store_true", help="review in a fresh thread after successful completion")
    s = sub.add_parser("say")
    s.add_argument("slug")
    s.add_argument("message", nargs="?")
    s.add_argument("--file")
    s.add_argument("--effort", default=None)
    s.add_argument("--review", action="store_true", help="review in a fresh thread after successful completion")
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
    a = p.parse_args()
    if a.cmd in ("start", "say", "review", "_run", "accounts", "reset-credit", "login", "clean"):
        global CODEX
        CODEX = resolve_codex()
    globals()["cmd_" + a.cmd.replace("-", "_")](a)


if __name__ == "__main__":
    main()
