"""Worker settings, script paths and polling intervals."""
import json
import math
import os
import re
import runpy
import shutil
import time
from pathlib import Path

from . import procs


CHATGPT_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
CODEX = {"path": os.environ.get("CODEX_TASK_CODEX")}
CODEX_HOME = Path(os.environ.get("CODEX_TASK_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
TASKS = CODEX_HOME / "claude-tasks"
WORKTREES = CODEX_HOME / "worktrees"
SCRIPT = Path(__file__).absolute().parent.parent / "codex_task.py"
SCRIPT_RESOLVED = SCRIPT.resolve()
SKILL = SCRIPT_RESOLVED.parent.parent
STATS = SCRIPT_RESOLVED.parent / "codex-stats.py"
ROLLOUT = runpy.run_path(str(SCRIPT.with_name("rollout.py")))
REPORT = SCRIPT.with_name("task_report.py")
# Tests can shorten polling without changing the default CLI intervals.
RATE_POLL_SECONDS = float(os.environ.get("CODEX_TASK_RATE_POLL_SECONDS", "30"))
ROLLOUT_SEARCH_SECONDS = float(os.environ.get("CODEX_TASK_ROLLOUT_SEARCH_SECONDS", "5"))
WAIT_POLL_SECONDS = float(os.environ.get("CODEX_TASK_WAIT_POLL_SECONDS", "1"))
CI_POLL_SECONDS = float(os.environ.get("CODEX_TASK_CI_POLL_SECONDS", "30"))
ACCOUNT_TIMEOUT = 20
ACCOUNT_RETRY_SECONDS = float(os.environ.get("CODEX_TASK_ACCOUNT_RETRY_SECONDS", "300"))
CAPACITY_RETRY_SECONDS = float(os.environ.get("CODEX_TASK_CAPACITY_RETRY_SECONDS", "60"))
MAX_ACCOUNT_RETRIES = 6
SLEEP_STEP = float(os.environ.get("CODEX_TASK_SLEEP_STEP", "60"))
RESET_MARGIN = float(os.environ.get("CODEX_TASK_RESET_MARGIN", "120"))
# Tests can shorten minutes without changing the supervisor's monotonic clock.
MINUTE_SECONDS = float(os.environ.get("CODEX_TASK_MINUTE_SECONDS", "60"))
RESET_RETRY_SECONDS = 15 * 60
MAX_RESET_WAIT_SECONDS = 8 * 24 * 60 * 60
TOKEN_FIELDS = ROLLOUT["TOKEN_FIELDS"]
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def resolve_codex():
    override = os.environ.get("CODEX_TASK_CODEX")
    if override:
        return str(Path(override).expanduser())
    if CHATGPT_CODEX.is_file():
        return str(CHATGPT_CODEX)
    binary = shutil.which("codex")
    if binary:
        return binary
    procs.die("Codex CLI was not found. Install it with npm install -g @openai/codex, "
        "run codex login, or set CODEX_TASK_CODEX to its executable path.")


def codex_binary():
    return CODEX["path"] or resolve_codex()


def claude_binary():
    override = os.environ.get("CODEX_TASK_CLAUDE")
    return shutil.which(str(Path(override).expanduser()) if override else "claude")


def require_claude():
    if not claude_binary():
        procs.die("Claude Code CLI was not found. Install Claude Code or set "
                  "CODEX_TASK_CLAUDE to its executable path to use --reviewer claude.")


def accounts_config():
    p = CODEX_HOME / "codex-worker.json"
    if not p.exists():
        p = CODEX_HOME / "claude-accounts.json"
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not isinstance(cfg, dict):
        procs.die("settings must be a JSON object")
    cfg["enabled"] = "accounts" in cfg
    cfg.setdefault("accounts", [{"name": "main", "home": str(CODEX_HOME)}])
    cfg.setdefault("language", "en")
    cfg.setdefault("switch_at", 99)
    cfg.setdefault("credits", False)
    cfg.setdefault("wait_for_reset", True)
    cfg.setdefault("use_reset_credits", False)
    cfg.setdefault("reset_credit_min_hours", 12)
    cfg.setdefault("max_turn_minutes", None)
    cfg.setdefault("max_task_weekly_percent", None)
    cfg.setdefault("max_review_minutes", 30)
    cfg.setdefault("reviewer", "codex")
    cfg.setdefault("claude", {})
    cfg.setdefault("ci", False)
    cfg.setdefault("ci_timeout_minutes", 30)
    cfg.setdefault("review_angles_min_lines", 400)
    cfg.setdefault("repeat_limit", 3)
    cfg.setdefault("sandbox", "off")
    cfg.setdefault("writable_dirs", [])
    if cfg["reviewer"] not in ("codex", "claude"):
        procs.die('reviewer must be "codex" or "claude"')
    claude = cfg["claude"]
    if not isinstance(claude, dict):
        procs.die("claude must be an object")
    claude.setdefault("config_dir", "~/.claude-accounts/reviewer")
    claude.setdefault("token_keychain", "codex-worker-claude-reviewer")
    claude.setdefault("model", None)
    claude.setdefault("effort", None)
    claude.setdefault("max_turns", 40)
    for key in ("config_dir", "token_keychain"):
        if not isinstance(claude[key], str) or not claude[key]:
            procs.die("claude." + key + " must be a nonempty string")
    claude["config_dir"] = str(Path(claude["config_dir"]).expanduser().resolve())
    for key in ("model", "effort"):
        if claude[key] is not None and (not isinstance(claude[key], str) or not claude[key]):
            procs.die("claude." + key + " must be a nonempty string or null")
    if type(claude["max_turns"]) is not int or claude["max_turns"] < 1:
        procs.die("claude.max_turns must be a positive integer")
    if cfg["sandbox"] not in ("off", "workspace"):
        procs.die('sandbox must be "off" or "workspace"')
    if not isinstance(cfg["writable_dirs"], list) or not all(
            isinstance(path, str) and path for path in cfg["writable_dirs"]):
        procs.die("writable_dirs must be a list of nonempty directory paths")
    cfg["writable_dirs"] = [str(Path(path).expanduser().resolve()) for path in cfg["writable_dirs"]]
    for key in ("max_turn_minutes", "max_review_minutes", "ci_timeout_minutes"):
        value = cfg[key]
        if key == "max_turn_minutes" and value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            procs.die(key + " must be a positive finite number")
    if type(cfg["repeat_limit"]) is not int or cfg["repeat_limit"] < 1:
        procs.die("repeat_limit must be a positive integer")
    weekly = cfg["max_task_weekly_percent"]
    if weekly is not None and (type(weekly) not in (int, float) or not 0 < weekly <= 100
                               or not math.isfinite(weekly)):
        procs.die("max_task_weekly_percent must be a finite number in (0, 100]")
    if type(cfg["review_angles_min_lines"]) is not int or cfg["review_angles_min_lines"] < 1:
        procs.die("review_angles_min_lines must be a positive integer")
    if cfg["language"] not in ("en", "ru"):
        procs.die('language must be "en" or "ru"')
    notification = cfg.get("notify")
    if notification is not None and notification != "macos" and not (
            isinstance(notification, list) and notification and
            all(isinstance(arg, str) for arg in notification) and notification[0]):
        procs.die('notify must be "macos" or a nonempty argv list')
    if "notify_events" in cfg and not (isinstance(cfg["notify_events"], list) and
                                       all(isinstance(event, str) for event in cfg["notify_events"])):
        procs.die("notify_events must be a list of event types")
    if not isinstance(cfg["switch_at"], (int, float)) or not 0 < cfg["switch_at"] <= 100:
        procs.die("switch_at must be in (0, 100]")
    if not isinstance(cfg["credits"], bool):
        procs.die("credits must be true or false")
    if not isinstance(cfg["ci"], bool):
        procs.die("ci must be true or false")
    if not isinstance(cfg["wait_for_reset"], bool):
        procs.die("wait_for_reset must be true or false")
    if not isinstance(cfg["use_reset_credits"], bool):
        procs.die("use_reset_credits must be true or false")
    hours = cfg["reset_credit_min_hours"]
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours < 0:
        procs.die("reset_credit_min_hours must be a finite nonnegative number")
    names = set()
    homes = set()
    if not isinstance(cfg["accounts"], list) or not cfg["accounts"]:
        procs.die("accounts must be a nonempty list")
    for a in cfg["accounts"]:
        if not isinstance(a, dict):
            procs.die("each account must be an object with name and home")
        name = a.get("name", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,40}", name) or name in names:
            procs.die(f"invalid or duplicate account name: {name!r}")
        if not isinstance(a.get("home"), str) or not a["home"]:
            procs.die(f"account {name!r} must have a home path")
        home = Path(a["home"]).expanduser().resolve()
        if str(home) in homes:
            procs.die(f"duplicate account home: {home}")
        a["home"] = str(home)
        names.add(name)
        homes.add(str(home))
    return cfg


def local_time(stamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp is not None else "unknown"
