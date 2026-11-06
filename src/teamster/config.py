"""Worker settings, script paths and polling intervals."""
import datetime
import fcntl
import json
import os
import re
import runpy
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

import teamster

from . import executors, procs, result_schema


def env(legacy_name, default=None):
    """Read a Teamster override first, retaining the old variable as a fallback."""
    name = legacy_name.removeprefix("CODEX_TASK_").removeprefix("CODEX_WORKER_")
    return os.environ.get("TEAMSTER_" + name) or os.environ.get(legacy_name, default)


CHATGPT_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
CODEX = {"path": env("CODEX_TASK_CODEX")}
# Every file of ours, by data home layout. Codex's own files stay in CODEX_HOME.
LAYOUTS = {
    "legacy": {"tasks": "claude-tasks", "worktrees": "worktrees",
               "settings": "codex-worker.json", "settings_fallback": "claude-accounts.json",
               "settings_lock": "codex-worker.lock", "telemetry": "codex-worker-log.jsonl",
               "resets": "codex-worker-resets.jsonl", "rules": "codex-worker-rules.md",
               "duty": "duty.json", "duty_log": "duty-log.jsonl", "duty_lock": "duty.lock",
               "duty_launchd_log": "duty-launchd.log",
               "board_machines": "board-machines.json", "board_log": "board-server.log"},
    "new": {"tasks": "tasks", "worktrees": "worktrees",
            "settings": "config.json", "settings_fallback": None,
            "settings_lock": "config.lock", "telemetry": "log.jsonl",
            "resets": "resets.jsonl", "rules": "rules.md",
            "duty": "duty.json", "duty_log": "duty-log.jsonl", "duty_lock": "duty.lock",
            "duty_launchd_log": "duty-launchd.log",
            "board_machines": "board-machines.json", "board_log": "board-server.log"},
}
# A Codex home holding this file names the data home that replaced its legacy data.
HOME_MARKER = "teamster-home"
MIGRATION_RECORD = "migration.json"


# One process resolves its homes from the environment it started with.
USER_HOME = Path.home()
HOME_VARIABLES = {"TEAMSTER_HOME": os.environ.get("TEAMSTER_HOME"),
                  "CODEX_TASK_HOME": os.environ.get("CODEX_TASK_HOME"),
                  "TEAMSTER_CODEX_HOME": os.environ.get("TEAMSTER_CODEX_HOME")}


def default_codex_home():
    return USER_HOME / ".codex"


def default_data_home():
    return USER_HOME / ".teamster"


def migrating_home():
    """Where migrate-home assembles the new data home before its final rename."""
    return USER_HOME / ".teamster.migrating"


def resolve_homes(environ=None):
    """(Codex home, data home, layout) by the first matching rule; empty variables count as unset."""
    environ = HOME_VARIABLES if environ is None else environ

    def absolute(value):
        return Path(value).expanduser().resolve()

    codex = absolute(environ.get("CODEX_TASK_HOME") or default_codex_home())
    if environ.get("TEAMSTER_HOME"):
        return (absolute(environ.get("TEAMSTER_CODEX_HOME") or codex),
                absolute(environ["TEAMSTER_HOME"]), "new")
    if environ.get("CODEX_TASK_HOME"):
        marker = codex / HOME_MARKER
        target = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        return (codex, absolute(target), "new") if target else (codex, codex, "legacy")
    data = default_data_home()
    if data.exists():
        return codex, absolute(data), "new"
    legacy = LAYOUTS["legacy"]
    # An unfinished migration keeps resolving to the legacy home until its final rename.
    if (migrating_home().exists() or (codex / legacy["tasks"]).exists()
            or (codex / legacy["settings"]).exists()):
        return codex, codex, "legacy"
    return codex, absolute(data), "new"


CODEX_HOME, DATA_HOME, LAYOUT = resolve_homes()


def data_path(purpose):
    """The path of one of our files or directories in the data home."""
    return DATA_HOME / LAYOUTS[LAYOUT][purpose]


def settings_path():
    """The settings file to read: the current name, else the legacy fallback when present."""
    path = data_path("settings")
    fallback = LAYOUTS[LAYOUT]["settings_fallback"]
    if not path.exists() and fallback and (DATA_HOME / fallback).exists():
        return DATA_HOME / fallback
    return path


def home_moved():
    """Why this process's data home is no longer current, or None."""
    if resolve_homes() != (CODEX_HOME, DATA_HOME, LAYOUT):
        return f"the data home moved after this command started; run {COMMAND} again"
    # After the final rename and before the marker, CODEX_TASK_HOME=~/.codex still resolves to legacy.
    if LAYOUT == "legacy" and DATA_HOME == default_codex_home().resolve() and (
            migrating_home().exists() or (default_data_home() / MIGRATION_RECORD).exists()):
        return (f"a data home migration is unfinished ({migrating_home()}); "
                f"run {COMMAND} migrate-home --apply to finish it")
    return None


@contextmanager
def home_lock(exclusive=False):
    """Hold the settings lock in the current data home.

    Commands that change task records hold it shared; settings writers and
    migrate-home hold it exclusively. After the lock is granted the data home
    is resolved again, so a command never writes into a home that moved.
    """
    DATA_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with data_path("settings_lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        reason = home_moved()
        if reason:
            procs.die(reason)
        yield


TASKS = data_path("tasks")
WORKTREES = data_path("worktrees")
PACKAGE = Path(__file__).absolute().parent
SCRIPT = PACKAGE / "__main__.py"
SCRIPT_RESOLVED = SCRIPT.resolve()
COMMAND = "teamster"
# Standalone helpers and standing rules live in the package directory.
STATS = PACKAGE.resolve() / "codex_stats.py"
ROLLOUT = runpy.run_path(str(PACKAGE / "rollout.py"))
REPORT = PACKAGE / "task_report.py"
# Tests can shorten polling without changing the default CLI intervals.
RATE_POLL_SECONDS = float(env("CODEX_TASK_RATE_POLL_SECONDS", "30"))
ROLLOUT_SEARCH_SECONDS = float(env("CODEX_TASK_ROLLOUT_SEARCH_SECONDS", "5"))
WAIT_POLL_SECONDS = float(env("CODEX_TASK_WAIT_POLL_SECONDS", "1"))
CI_POLL_SECONDS = float(env("CODEX_TASK_CI_POLL_SECONDS", "30"))
ACCOUNT_TIMEOUT = 20
CLAUDE_PROBE_TIMEOUT = 120
ACCOUNT_RETRY_SECONDS = float(env("CODEX_TASK_ACCOUNT_RETRY_SECONDS", "300"))
# When set, every capacity pause has this length instead of capacity_retry_minutes.
CAPACITY_RETRY_SECONDS = (float(env("CODEX_TASK_CAPACITY_RETRY_SECONDS"))
                          if env("CODEX_TASK_CAPACITY_RETRY_SECONDS") else None)
# Active minutes without a capacity error after a resume that restart the pause schedule.
CAPACITY_RESET_MINUTES = 10
MAX_ACCOUNT_RETRIES = 6
SLEEP_STEP = float(env("CODEX_TASK_SLEEP_STEP", "60"))
RESET_MARGIN = float(env("CODEX_TASK_RESET_MARGIN", "120"))
# Tests can shorten minutes without changing the supervisor's monotonic clock.
MINUTE_SECONDS = float(env("CODEX_TASK_MINUTE_SECONDS", "60"))
RESET_RETRY_SECONDS = 15 * 60
MAX_RESET_WAIT_SECONDS = 8 * 24 * 60 * 60
TOKEN_FIELDS = ROLLOUT["TOKEN_FIELDS"]
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def plugin_version():
    """The package version recorded in the existing task state field."""
    return teamster.__version__


def resolve_codex():
    override = env("CODEX_TASK_CODEX")
    if override:
        return str(Path(override).expanduser())
    if CHATGPT_CODEX.is_file():
        return str(CHATGPT_CODEX)
    binary = shutil.which("codex")
    if binary:
        return binary
    procs.die("Codex CLI was not found. Install it with npm install -g @openai/codex, "
        "run codex login, or set TEAMSTER_CODEX to its executable path.")


def codex_binary():
    return CODEX["path"] or resolve_codex()


def claude_binary():
    override = env("CODEX_TASK_CLAUDE")
    return shutil.which(str(Path(override).expanduser()) if override else "claude")


def require_claude():
    if not claude_binary():
        procs.die("Claude Code CLI was not found. Install Claude Code or set "
                  "TEAMSTER_CLAUDE to its executable path to use --executor claude or --reviewer claude.")


def accounts_config():
    p = settings_path()
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not isinstance(cfg, dict):
        procs.die("settings must be a JSON object")
    cfg["enabled"] = "accounts" in cfg
    cfg.setdefault("accounts", [{"name": "main", "home": str(CODEX_HOME)}])
    cfg.setdefault("language", "en")
    cfg.setdefault("switch_at", 99)
    cfg.setdefault("soft_switch_at", cfg["switch_at"])
    cfg.setdefault("credits", False)
    cfg.setdefault("credits_floor", 0)
    cfg.setdefault("credits_floor_until", None)
    cfg.setdefault("wait_for_reset", True)
    cfg.setdefault("use_reset_credits", False)
    cfg.setdefault("reset_credit_min_hours", 12)
    cfg.setdefault("max_turn_minutes", None)
    cfg.setdefault("max_task_weekly_percent", None)
    cfg.setdefault("max_review_minutes", 30)
    cfg.setdefault("executor", "codex")
    cfg.setdefault("reviewer", "codex")
    cfg.setdefault("claude", {})
    cfg.setdefault("ci", False)
    cfg.setdefault("ci_timeout_minutes", 30)
    cfg.setdefault("review_angles_min_lines", 400)
    cfg.setdefault("repeat_limit", 3)
    cfg.setdefault("capacity_retry_minutes", [1, 5, 15, 30])
    cfg.setdefault("sandbox", "off")
    cfg.setdefault("writable_dirs", [])
    try:
        executors.from_state(cfg)
    except (KeyError, TypeError):
        procs.die("executor must be " + " or ".join('"' + name + '"' for name in executors.names()))
    if cfg["reviewer"] not in ("codex", "claude"):
        procs.die('reviewer must be "codex" or "claude"')
    claude = cfg["claude"]
    if not isinstance(claude, dict):
        procs.die("claude must be an object")
    if "accounts" not in claude:
        claude["accounts"] = [{
            "name": "default",
            "config_dir": claude.get("config_dir", "~/.claude-accounts/reviewer"),
            "token_keychain": claude.get("token_keychain", "codex-worker-claude-reviewer")}]
    if not isinstance(claude["accounts"], list) or not claude["accounts"]:
        procs.die("claude.accounts must be a nonempty list")
    claude_names = set()
    for account in claude["accounts"]:
        if not isinstance(account, dict):
            procs.die("each Claude account must be an object with name, config_dir and token_keychain")
        name = account.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name) or name in claude_names:
            procs.die(f"invalid or duplicate Claude account name: {name!r}")
        claude_names.add(name)
        for key in ("config_dir", "token_keychain"):
            if not isinstance(account.get(key), str) or not account[key]:
                procs.die("claude.accounts." + key + " must be a nonempty string")
        weight = account.get("weight", 1)
        if not result_schema.is_finite_number(weight) or weight <= 0:
            procs.die("claude.accounts.weight must be a positive finite number")
        account["config_dir"] = str(Path(account["config_dir"]).expanduser().resolve())
    claude.setdefault("model", None)
    claude.setdefault("effort", None)
    claude.setdefault("max_turns", 40)
    claude.setdefault("limits_max_age_minutes", 30)
    claude.setdefault("work_switch_at", 85)
    if not result_schema.is_finite_number(claude["work_switch_at"]) or not 0 <= claude["work_switch_at"] <= 100:
        procs.die("claude.work_switch_at must be a finite number in [0, 100]")
    age = claude["limits_max_age_minutes"]
    if not result_schema.is_finite_number(age) or age < 0:
        procs.die("claude.limits_max_age_minutes must be a finite nonnegative number")
    for key in ("model", "effort"):
        if claude[key] is not None and (not isinstance(claude[key], str) or not claude[key]):
            procs.die("claude." + key + " must be a nonempty string or null")
    if type(claude["max_turns"]) is not int or claude["max_turns"] < 1:
        procs.die("claude.max_turns must be a positive integer")
    duty_settings = cfg.setdefault("duty", {})
    if not isinstance(duty_settings, dict):
        procs.die("duty must be an object")
    duty_settings.setdefault("relative_margin", 25)
    duty_settings.setdefault("absolute_margin", 100)
    duty_settings.setdefault("claude_only_task_types", ["visual", "layout"])
    for key in ("relative_margin", "absolute_margin"):
        if not result_schema.is_finite_number(duty_settings[key]) or duty_settings[key] < 0:
            procs.die("duty." + key + " must be a finite nonnegative number")
    task_types = duty_settings["claude_only_task_types"]
    if not isinstance(task_types, list) or not all(
            isinstance(label, str) and re.fullmatch(r"[a-z0-9-]{1,32}", label) for label in task_types):
        procs.die("duty.claude_only_task_types must be a list of task types (1-32 characters of a-z, 0-9 and '-')")
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
        if not result_schema.is_finite_number(value) or value <= 0:
            procs.die(key + " must be a positive finite number")
    if type(cfg["repeat_limit"]) is not int or cfg["repeat_limit"] < 1:
        procs.die("repeat_limit must be a positive integer")
    pauses = cfg["capacity_retry_minutes"]
    if not isinstance(pauses, list) or not all(
            result_schema.is_finite_number(value) and value > 0 for value in pauses):
        procs.die("capacity_retry_minutes must be a list of positive finite numbers")
    weekly = cfg["max_task_weekly_percent"]
    if weekly is not None and (not result_schema.is_finite_number(weekly) or not 0 < weekly <= 100):
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
    if (type(cfg["soft_switch_at"]) not in (int, float)
            or not 0 < cfg["soft_switch_at"] <= cfg["switch_at"]):
        procs.die("soft_switch_at must be a number in (0, switch_at]")
    if not isinstance(cfg["credits"], bool):
        procs.die("credits must be true or false")
    floor = cfg["credits_floor"]
    if not result_schema.is_finite_number(floor) or floor < 0:
        procs.die("credits_floor must be a finite nonnegative number")
    until = cfg["credits_floor_until"]
    if until is not None and not valid_date(until):
        procs.die("credits_floor_until must be a date YYYY-MM-DD or null")
    if not isinstance(cfg["ci"], bool):
        procs.die("ci must be true or false")
    if not isinstance(cfg["wait_for_reset"], bool):
        procs.die("wait_for_reset must be true or false")
    if not isinstance(cfg["use_reset_credits"], bool):
        procs.die("use_reset_credits must be true or false")
    hours = cfg["reset_credit_min_hours"]
    if not result_schema.is_finite_number(hours) or hours < 0:
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
        weight = a.get("weight", 1)
        if not result_schema.is_finite_number(weight) or weight <= 0:
            procs.die("accounts.weight must be a positive finite number")
        a["home"] = str(home)
        names.add(name)
        homes.add(str(home))
    return cfg


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def local_time(stamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp is not None else "unknown"
