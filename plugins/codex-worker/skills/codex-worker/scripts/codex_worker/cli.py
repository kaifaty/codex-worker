"""Delegate coding to Codex in a git worktree, with autonomous subscription rotation.

  codex_task.py start <slug> --brief FILE|- [--repo DIR] [--base origin/<branch>] [--branch NAME] [--no-push] [--executor codex|claude] [--effort E] [--model M] [--review-model M] [--task-type LABEL] [--parallel] [--sandbox off|workspace] [--max-minutes M] [--max-weekly P] [--review] [--reviewer codex|claude] [--review-angles LIST] [--fix-rounds N] [--ci] [--ci-command CMD] [--ci-rounds N] [--check CMD] [--check-timeout SEC] [--guard GLOB] [--base-check]
  codex_task.py scout <slug> "question" | --file FILE [--repo DIR] [--effort E]  read-only research
  codex_task.py say <slug> "message" | --file FILE [--effort E] [--model M] [--review-model M] [--task-type LABEL] [--max-minutes M] [--max-weekly P] [--review] [--reviewer codex|claude] [--review-angles LIST] [--fix-rounds N] [--ci | --no-ci] [--ci-command CMD | --no-ci-command] [--ci-rounds N] [--check CMD | --no-check] [--check-timeout SEC] [--no-base-check]
  codex_task.py review <slug> [--effort E] [--review-model M] [--max-weekly P] [--reviewer codex|claude] [--review-angles LIST]  fresh review (task must be idle)
  codex_task.py wait <slug> [--timeout SEC] [--through-sleep]  wait, show rotations and the result
  codex_task.py status [<slug>]                       state, account and current thread
  codex_task.py stats <slug>                          codex-stats.py for every task thread
  codex_task.py report [--since 7d|30d|YYYY-MM-DD] [--repo NAME] [--slug SLUG] [--by task-type|effort|model|executor|reviewer] [--json]
  codex_task.py verdict <slug> accepted|revised|redone|dropped [--note TEXT]  record the lead's verdict
  codex_task.py stop <slug>                           interrupt without rotating
  codex_task.py clean <slug> [--keep-branch]          remove worktree, archive all threads
  codex_task.py accounts [--json] [--refresh]        read account limits and login status
  codex_task.py reset-credit <account> [--credit ID] [--yes]  preview/use a free limit reset
  codex_task.py login <name> [--browser]              add/sign in to an isolated account
  codex_task.py claude-login <name> [--config-dir DIR] [--keychain SERVICE]  add/sign in to a Claude account
  codex_task.py claude-probe <name>                  check a Claude token and cache usage limits
  codex_task.py claude-rename <old> <new>            rename a Claude account without moving credentials
  codex_task.py claude-remove <name>                 remove a Claude account from the pool

All commands accept -h/--help.
~/.codex/codex-worker.json optionally configures language (en), accounts, switch_at (99),
soft_switch_at (same as switch_at),
credits (false), credits_floor (0), credits_floor_until (none),
wait_for_reset (true), max_turn_minutes (no cap), max_task_weekly_percent (no cap), max_review_minutes (30),
executor (codex), reviewer (codex), claude (isolated account pool settings), review_angles_min_lines (400), repeat_limit (3), sandbox (off), writable_dirs ([]), notify and notify_events.
start --ci (or ci: true) enables PR CI for the task; say inherits it and --ci-rounds (initially 1).
start/say --ci-command CMD enables CI via sh -c; say inherits it, and --no-ci-command removes it.
say --ci/--no-ci enables/disables CI; --ci-rounds N replaces the saved round limit. ci_timeout_minutes defaults to 30.
Legacy claude-accounts.json is a fallback.
Without an accounts setting, tasks use main (~/.codex) without rotation; weekly budgets still monitor rollout usage.
State/logs: ~/.codex/claude-tasks/<slug>/; worktree: ~/.codex/worktrees/claude-<slug>/<repo>.
CODEX_TASK_HOME and CODEX_TASK_CODEX override the main home and CLI binary (also for tests).
The internal _run <slug> supervisor survives the caller and opens a new thread on rotation.
"""
import argparse
import math
import re
import sys

from . import commands, config, prompts, supervisor, telemetry_log, wait_view


HELP_TEXT = __doc__


def task_type(value):
    if not re.fullmatch(r"[a-z0-9-]{1,32}", value):
        raise argparse.ArgumentTypeError("task type must be 1-32 characters of a-z, 0-9 and '-'")
    return value


def model_name(value):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise argparse.ArgumentTypeError("model must be 1-64 characters of A-Z, a-z, 0-9, '.', '_' and '-'")
    return value


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


def ci_rounds(value):
    try:
        rounds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("CI rounds must be a nonnegative integer")
    if rounds < 0:
        raise argparse.ArgumentTypeError("CI rounds must be a nonnegative integer")
    return rounds


def max_weekly(value):
    try:
        weekly = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max weekly must be a finite number in (0, 100]")
    if not math.isfinite(weekly) or not 0 < weekly <= 100:
        raise argparse.ArgumentTypeError("max weekly must be a finite number in (0, 100]")
    return weekly


def review_angles(value):
    if value == "auto":
        return value
    angles = [angle.strip() for angle in value.split(",")]
    unknown = [angle for angle in angles if angle not in prompts.REVIEW_ANGLES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown review angle: {','.join(unknown)!r}; "
                                         "choose correctness,concurrency,security,tests or auto")
    return list(dict.fromkeys(angles))


# Rows follow the existing help/synopsis order for each command. Command entries
# override argparse keywords; "group" names a mutually exclusive option group.
# Synopsis tokens keep their own notation (including required/optional markers).
OPTION_REGISTRY = (
    {"names": ("--brief",), "argparse": {"required": True},
     "synopsis": "--brief FILE|-", "commands": {"start": {}}},
    {"names": ("--repo",), "argparse": {"default": "."},
     "synopsis": "[--repo DIR]", "commands": {"start": {}}},
    {"names": ("--base",),
     "argparse": {"default": None, "help": "default: the remote's default branch (origin/HEAD)"},
     "synopsis": "[--base origin/<branch>]", "commands": {"start": {}}},
    {"names": ("--branch",), "argparse": {"metavar": "NAME", "help": "task branch name (default: codex/<slug>)"},
     "synopsis": "[--branch NAME]", "commands": {"start": {}}},
    {"names": ("--no-push",),
     "argparse": {"action": "store_true", "help": "keep commits local; disable branch and PR publication and CI"},
     "synopsis": "[--no-push]", "commands": {"start": {}}},
    {"names": ("--executor",),
     "argparse": {"choices": ("codex", "claude"), "default": None,
                  "help": "work provider saved for the task (default: executor setting, codex)"},
     "synopsis": "[--executor codex|claude]", "commands": {"start": {}}},
    {"names": ("--file",), "argparse": {},
     "synopsis": "--file FILE", "commands": {"say": {}}},
    {"names": ("--effort",), "argparse": {"default": None},
     "synopsis": "[--effort E]",
     "commands": {"start": {"help": "default: executor configuration"},
                  "say": {}, "review": {}}},
    {"names": ("--model",),
     "argparse": {"type": model_name, "metavar": "M", "help": "work model; default clears the saved override"},
     "synopsis": "[--model M]", "commands": {"start": {}, "say": {}}},
    {"names": ("--review-model",),
     "argparse": {"type": model_name, "metavar": "M", "help": "Codex review model; default clears the saved override"},
     "synopsis": "[--review-model M]", "commands": {"start": {}, "say": {}, "review": {}}},
    {"names": ("--task-type",), "argparse": {"type": task_type, "metavar": "LABEL"},
     "synopsis": "[--task-type LABEL]",
     "commands": {"start": {"help": "optional task label for telemetry"},
                  "say": {"help": "replace the saved task label for telemetry"}}},
    {"names": ("--parallel",), "argparse": {"action": "store_true"},
     "synopsis": "[--parallel]", "commands": {"start": {}}},
    {"names": ("--sandbox",),
     "argparse": {"choices": ("off", "workspace"), "default": None,
                  "help": "write sandbox for this task (default: sandbox setting, off)"},
     "synopsis": "[--sandbox off|workspace]", "commands": {"start": {}}},
    {"names": ("--max-minutes",),
     "argparse": {"type": max_minutes, "help": "active exec time budget (default: max_turn_minutes setting)"},
     "synopsis": "[--max-minutes M]", "commands": {"start": {}, "say": {}}},
    {"names": ("--max-weekly",),
     "argparse": {"type": max_weekly, "metavar": "P", "help": "replace the task weekly percentage point budget"},
     "synopsis": "[--max-weekly P]",
     "commands": {"start": {"help": "task weekly percentage point budget (default: max_task_weekly_percent setting)"},
                  "say": {}, "review": {}}},
    {"names": ("--review",),
     "argparse": {"action": "store_true", "help": "review in a fresh thread after successful completion"},
     "synopsis": "[--review]", "commands": {"start": {}, "say": {}}},
    {"names": ("--reviewer",),
     "argparse": {"choices": ("codex", "claude"), "default": None,
                  "help": "review provider (default: saved reviewer; Claude work uses Codex reviews)"},
     "synopsis": "[--reviewer codex|claude]", "commands": {"start": {}, "say": {}, "review": {}}},
    {"names": ("--review-angles",),
     "argparse": {"type": review_angles, "metavar": "LIST",
                  "help": "review with comma-separated presets or auto (implies --review)"},
     "synopsis": "[--review-angles LIST]",
     "commands": {"start": {}, "say": {},
                  "review": {"help": "comma-separated presets or auto (default: one review)"}}},
    {"names": ("--fix-rounds",),
     "argparse": {"type": int, "choices": range(6), "default": None,
                  "help": "automatic fix rounds after review, 0–5 (default: 0; requires --review)"},
     "synopsis": "[--fix-rounds N]", "commands": {"start": {}, "say": {}}},
    {"names": ("--ci",), "argparse": {"action": "store_true"}, "synopsis": "[--ci]",
     "commands": {"start": {"help": "wait for PR CI and repair failures (default: ci setting)"},
                  "say": {"group": "ci", "help": "enable PR CI waiting and repair for this task"}}},
    {"names": ("--no-ci",), "argparse": {"action": "store_true"}, "synopsis": "[--no-ci]",
     "commands": {"say": {"group": "ci", "help": "disable PR CI waiting and repair for this task"}}},
    {"names": ("--ci-command",), "argparse": {"metavar": "CMD"}, "synopsis": "[--ci-command CMD]",
     "commands": {"start": {"help": "enable CI waiting via a shell command instead of GitHub checks"},
                  "say": {"group": "ci_command", "help": "replace the saved CI command and enable CI"}}},
    {"names": ("--no-ci-command",), "argparse": {"action": "store_true"}, "synopsis": "[--no-ci-command]",
     "commands": {"say": {"group": "ci_command", "help": "remove the saved CI command"}}},
    {"names": ("--ci-rounds",), "argparse": {"type": ci_rounds, "metavar": "N"},
     "synopsis": "[--ci-rounds N]",
     "commands": {"start": {"help": "maximum CI repair rounds (default: 1)"},
                  "say": {"help": "replace the saved maximum CI repair rounds"}}},
    {"names": ("--check",), "argparse": {}, "synopsis": "[--check CMD]",
     "commands": {"start": {"help": "shell command to run after completed work turns; saved for follow-ups"},
                  "say": {"group": "checks", "help": "replace the saved check command"}}},
    {"names": ("--no-check",), "argparse": {"action": "store_true"}, "synopsis": "[--no-check]",
     "commands": {"say": {"group": "checks", "help": "remove the saved check command"}}},
    {"names": ("--check-timeout",), "argparse": {"type": check_timeout},
     "synopsis": "[--check-timeout SEC]",
     "commands": {"start": {"default": 1800, "help": "check timeout in seconds (default: 1800)"},
                  "say": {"default": None, "help": "replace the saved check timeout"}}},
    {"names": ("--guard",),
     "argparse": {"action": "append", "default": [], "metavar": "GLOB",
                  "help": "guard matching repository paths (repeatable)"},
     "synopsis": "[--guard GLOB]", "commands": {"start": {}}},
    {"names": ("--base-check",),
     "argparse": {"action": "store_true", "help": "also check task HEAD with guarded files restored from base"},
     "synopsis": "[--base-check]", "commands": {"start": {}}},
    {"names": ("--no-base-check",),
     "argparse": {"action": "store_true", "help": "disable the saved base check"},
     "synopsis": "[--no-base-check]", "commands": {"say": {}}},
)


def command_options(command):
    for option in OPTION_REGISTRY:
        if command in option["commands"]:
            kwargs = dict(option["argparse"], **option["commands"][command])
            group = kwargs.pop("group", None)
            yield option, kwargs, group


def add_command_options(parser, command):
    groups = {}
    for option, kwargs, group in command_options(command):
        target = parser
        if group is not None:
            if group not in groups:
                groups[group] = parser.add_mutually_exclusive_group()
            target = groups[group]
        target.add_argument(*option["names"], **kwargs)


def command_synopsis(command):
    positional = '<slug> "message" |' if command == "say" else "<slug>"
    parts = ["codex_task.py", command, positional]
    options = list(command_options(command))
    seen_groups = set()
    for option, _, group in options:
        if group is None:
            parts.append(option["synopsis"])
        elif group not in seen_groups:
            tokens = [item["synopsis"].strip("[]") for item, _, name in options if name == group]
            parts.append("[" + " | ".join(tokens) + "]")
            seen_groups.add(group)
    synopsis = " ".join(parts)
    if command == "review":
        synopsis += "  fresh review (task must be idle)"
    return synopsis


COMMAND_HANDLERS = {
    "start": commands.cmd_start,
    "scout": commands.cmd_scout,
    "say": commands.cmd_say,
    "review": commands.cmd_review,
    "wait": wait_view.cmd_wait,
    "status": commands.cmd_status,
    "stats": commands.cmd_stats,
    "report": commands.cmd_report,
    "verdict": commands.cmd_verdict,
    "stop": commands.cmd_stop,
    "clean": commands.cmd_clean,
    "accounts": commands.cmd_accounts,
    "reset-credit": commands.cmd_reset_credit,
    "login": commands.cmd_login,
    "claude-login": commands.cmd_claude_login,
    "claude-probe": commands.cmd_claude_probe,
    "claude-rename": commands.cmd_claude_rename,
    "claude-remove": commands.cmd_claude_remove,
    "_run": supervisor.cmd__run,
}


def main():
    p = argparse.ArgumentParser(description=HELP_TEXT, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("slug")
    add_command_options(s, "start")
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
    add_command_options(s, "say")
    s = sub.add_parser("review")
    s.add_argument("slug")
    add_command_options(s, "review")
    s = sub.add_parser("wait")
    s.add_argument("slug")
    s.add_argument("--timeout", type=int, default=0)
    s.add_argument("--through-sleep", action="store_true", help="wait through limit resets until the turn ends")
    s = sub.add_parser("status")
    s.add_argument("slug", nargs="?")
    for name in ("stats", "stop", "_run"):
        sub.add_parser(name).add_argument("slug")
    s = sub.add_parser("report", help="summarize persistent task costs and review/check outcomes")
    s.add_argument("--since", default="7d", metavar="7d|30d|YYYY-MM-DD")
    s.add_argument("--repo", help="repository directory name")
    s.add_argument("--slug")
    s.add_argument("--by", help="group by task-type, effort, model, executor, reviewer, "
                   "or comma-separated dimensions")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("verdict", help="record the lead's verdict on a task for report")
    s.add_argument("slug")
    s.add_argument("verdict", choices=telemetry_log.LEAD_VERDICTS)
    s.add_argument("--note", help="short free-text note stored with the verdict")
    s = sub.add_parser("clean")
    s.add_argument("slug")
    s.add_argument("--keep-branch", action="store_true")
    s = sub.add_parser("accounts", help="read each account's limits (no task is started)")
    s.add_argument("--json", action="store_true", help="print account limits as JSON")
    s.add_argument("--refresh", action="store_true", help="refresh stale Claude account limits before printing")
    s = sub.add_parser("reset-credit", help="preview or consume a free limit reset")
    s.add_argument("account")
    s.add_argument("--credit", help="choose an available reset by ID")
    s.add_argument("--yes", action="store_true", help="consume the reset (default: preview only)")
    s = sub.add_parser("login", help="sign in interactively to an isolated account home")
    s.add_argument("name")
    s.add_argument("--browser", action="store_true", help="use browser login instead of device auth")
    s = sub.add_parser("claude-login", help="sign in interactively to an isolated Claude account")
    s.add_argument("name")
    s.add_argument("--config-dir", metavar="DIR", help="isolated Claude config directory")
    s.add_argument("--keychain", metavar="SERVICE", help="Keychain service for the account token")
    s = sub.add_parser("claude-probe", help="check a Claude token and refresh cached account limits")
    s.add_argument("name")
    s = sub.add_parser("claude-rename", help="rename a Claude account without moving credentials")
    s.add_argument("old")
    s.add_argument("new")
    s = sub.add_parser("claude-remove", help="remove a Claude account without deleting credentials")
    s.add_argument("name")
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
    if a.cmd in ("start", "say") and a.review_angles is not None:
        a.review = True
    if a.cmd in ("start", "say") and a.fix_rounds is not None and not a.review:
        p.error("--fix-rounds requires --review")
    if a.cmd in ("start", "scout", "say", "review", "_run", "accounts", "reset-credit", "login", "clean"):
        config.CODEX["path"] = config.resolve_codex()
    COMMAND_HANDLERS[a.cmd](a)
