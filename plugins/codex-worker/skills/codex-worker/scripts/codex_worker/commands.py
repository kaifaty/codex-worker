"""Task commands, descriptions and main repositories."""
import json
import os
import re
import runpy
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import accounts, budgets, clock, config, credit_resets, procs, prompts, result_schema, sandboxing, supervisor, task_state, threads


def main_repo(path):
    """The main checkout of the repository `path` belongs to (including worktrees)."""
    common = Path(procs.sh(config.GIT + ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path))
    return common.parent


def cmd_start(a):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", a.slug):
        procs.die("slug: 2-41 chars of a-z, 0-9 and '-'")
    cfg = config.accounts_config()
    reviewer = getattr(a, "reviewer", None) or cfg["reviewer"]
    if reviewer == "claude":
        config.require_claude()
    if (config.TASKS / a.slug).exists():
        procs.die(f"task {a.slug!r} exists; pick another slug or clean it")
    busy = task_state.running_tasks()
    if busy and not a.parallel:
        procs.die(f"already running: {', '.join(busy)} (wait for it, or pass --parallel if they cannot clash)")
    brief = sys.stdin.read() if a.brief == "-" else Path(a.brief).read_text()
    if not brief.strip():
        procs.die("empty brief")
    sandbox = a.sandbox if a.sandbox is not None else cfg["sandbox"]
    rules = sandboxing.rules_text(cfg)
    if sandbox == "workspace":
        rules += "\n\n" + sandboxing.sandbox_rules(cfg)
    repo = main_repo(a.repo)
    branch = a.branch if a.branch is not None else f"codex/{a.slug}"
    if subprocess.run(config.GIT + ["check-ref-format", "--branch", branch], cwd=repo,
                      capture_output=True).returncode:
        procs.die(f"invalid branch name: {branch!r}")
    wt = config.WORKTREES / f"claude-{a.slug}" / repo.name
    if wt.exists():
        procs.die(f"{wt} exists")
    procs.sh(config.GIT + ["fetch", "--quiet", "origin"], cwd=repo)
    a.base = a.base or procs.sh(config.GIT + ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo,
                          check=False) or "origin/main"
    wt.parent.mkdir(parents=True, exist_ok=True)
    procs.sh(config.GIT + ["worktree", "add", "--quiet", "-b", branch, str(wt), a.base], cwd=repo)
    d = config.TASKS / a.slug
    d.mkdir(parents=True)
    prompt = brief.rstrip() + "\n\n" + rules.replace("{branch}", branch).replace("{base}", a.base.removeprefix("origin/"))
    (d / "brief.md").write_text(prompt)
    st = {"slug": a.slug, "repo": str(repo), "worktree": str(wt), "branch": branch, "base": a.base,
          "effort": a.effort, "account": cfg["accounts"][0]["name"], "thread": "", "threads": [],
          "rotations": [], "reviews": [], "checks": [], "check": a.check, "check_timeout": a.check_timeout,
          "guards": a.guard, "base_check": a.base_check,
          "sandbox": sandbox, "writable_dirs": cfg["writable_dirs"],
          "review_angles": a.review_angles,
          "reviewer": reviewer,
          "max_turn_minutes": a.max_minutes if a.max_minutes is not None else cfg.get("max_turn_minutes"),
          "ci_enabled": a.ci or bool(a.ci_command) or cfg["ci"], "ci_command": a.ci_command,
          "ci_rounds": a.ci_rounds if a.ci_rounds is not None else 1,
          "ci_timeout_minutes": cfg["ci_timeout_minutes"],
          "max_task_weekly_percent": a.max_weekly if a.max_weekly is not None else cfg.get("max_task_weekly_percent"),
          "weekly_used_percent": 0,
          "created": clock.CLOCK.time(), "turns": 0}
    task_state.save(st)
    proc = supervisor.spawn(st, prompt, a.effort, review_after=a.review, fix_rounds=a.fix_rounds or 0)
    for _ in range(300):
        st = task_state.load(a.slug)
        if st["thread"] or st.get("outcome") == "sleeping" or st.get("pid") is None or proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"started {a.slug}: account {st['account']}, thread {st['thread'] or '?'}, pid {st.get('pid')}\n  worktree {wt}\n  branch {branch}")
    print(f"  wait: {config.SCRIPT} wait {a.slug}   (run it as a background command)")


def require_idle(st):
    if procs.alive(st.get("pid")):
        if st.get("outcome") == "sleeping":
            procs.die(f"waiting for limit reset until {config.local_time(st['sleeping']['until'])}; stop first to send a new message")
        procs.die(f"{st['slug']} is still running a turn; wait for it (or stop it) first")


def cmd_scout(a):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", a.slug):
        procs.die("slug: 2-41 chars of a-z, 0-9 and '-'")
    cfg = config.accounts_config()
    if (config.TASKS / a.slug).exists():
        procs.die(f"task {a.slug!r} exists; pick another slug or clean it")
    question = Path(a.file).read_text() if a.file else a.question
    if not question or not question.strip():
        procs.die("empty scout question")
    directory = Path(a.repo).expanduser().resolve()
    if not directory.is_dir():
        procs.die(f"scout directory does not exist: {directory}")
    d = config.TASKS / a.slug
    d.mkdir(parents=True)
    prompt = prompts.scout_prompt(question, cfg)
    (d / "brief.md").write_text(prompt)
    st = {"slug": a.slug, "kind": "scout", "repo": str(directory), "worktree": str(directory), "branch": "",
          "effort": a.effort, "account": cfg["accounts"][0]["name"], "thread": "", "threads": [],
          "rotations": [], "reviews": [], "checks": [], "created": clock.CLOCK.time(), "turns": 0}
    task_state.save(st)
    proc = supervisor.spawn(st, prompt, a.effort, kind="scout")
    for _ in range(300):
        st = task_state.load(a.slug)
        if st["thread"] or st.get("outcome") == "sleeping" or st.get("pid") is None or proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"started scout {a.slug}: account {st['account']}, thread {st['thread'] or '?'}, pid {st.get('pid')}\n  directory {directory}")
    print(f"  wait: {config.SCRIPT} wait {a.slug}   (run it as a background command)")


def cmd_say(a):
    st = task_state.load(a.slug)
    scouting = st.get("kind") == "scout"
    angles = getattr(a, "review_angles", None)
    reviewer = getattr(a, "reviewer", None)
    ci_command = getattr(a, "ci_command", None)
    no_ci_command = getattr(a, "no_ci_command", False)
    if scouting and (a.check is not None or a.review or a.check_timeout is not None or a.fix_rounds is not None
                     or a.no_base_check or angles is not None or reviewer is not None
                     or a.ci or a.no_ci or ci_command is not None or no_ci_command or a.ci_rounds is not None):
        procs.die("scout is read-only; say --check, --check-timeout, --review, --reviewer, --review-angles, --fix-rounds, --ci, --no-ci, --ci-command, --no-ci-command, --ci-rounds and --no-base-check are not supported")
    require_idle(st)
    msg = Path(a.file).read_text() if a.file else a.message
    if not msg or not msg.strip():
        procs.die("empty message")
    cfg = config.accounts_config()
    budgets.set_weekly_budget(st, getattr(a, "max_weekly", None), cfg)
    minutes = getattr(a, "max_minutes", None)
    st["max_turn_minutes"] = minutes if minutes is not None else cfg.get("max_turn_minutes")
    if no_ci_command:
        st["ci_command"] = None
    elif ci_command is not None:
        st["ci_command"] = ci_command
    if getattr(a, "no_ci", False):
        st["ci_enabled"] = False
    elif getattr(a, "ci", False) or ci_command is not None:
        st["ci_enabled"] = True
    if getattr(a, "ci_rounds", None) is not None:
        st["ci_rounds"] = a.ci_rounds
    st["ci_timeout_minutes"] = cfg.get("ci_timeout_minutes", 30)
    st["review_angles"] = angles
    if reviewer is not None:
        st["reviewer"] = reviewer
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
    prompt = threads.followup_prompt(st, msg, cfg)
    supervisor.spawn(st, prompt, a.effort or st.get("effort"), current_message=True,
          kind="scout" if scouting else "work", review_after=a.review, fix_rounds=a.fix_rounds or 0)
    print(f"sent to {a.slug} (turn {st['turns']}), pid {st['pid']}")


def cmd_review(a):
    st = task_state.load(a.slug)
    if st.get("kind") == "scout":
        procs.die("scout is read-only; review is not supported")
    require_idle(st)
    cfg = config.accounts_config()
    reviewer = getattr(a, "reviewer", None) or st["reviewer"]
    if reviewer == "claude":
        config.require_claude()
    st["reviewer"] = reviewer
    budgets.set_weekly_budget(st, getattr(a, "max_weekly", None), cfg)
    st["review_angles"] = a.review_angles
    supervisor.spawn(st, prompts.review_prompt(st, cfg), a.effort or st.get("effort"), kind="review")
    print(f"reviewing {a.slug}, pid {st['pid']}")


def describe(st, detail):
    supervisor_alive = procs.alive(st.get("pid"))
    if st.get("outcome") == "sleeping":
        state = (f"sleeping until {config.local_time(st['sleeping']['until'])}" if supervisor_alive
                 else "paused, supervisor gone; continue with say")
    else:
        state = "running" if supervisor_alive else "idle"
    reviewing = st.get("kind") == "review"
    scouting = st.get("kind") == "scout"
    account, thread = st["account"], st.get("thread", "")
    if reviewing:
        account, thread = st.get("review_account") or "?", ""
        reviews = st.get("reviews", [])[st.get("review_start", 0):]
        if reviews and ("review_account" not in st or st["review_account"] is not None):
            account, thread = reviews[-1]["account"], reviews[-1]["thread"]
    location = st["worktree"] if scouting else st["branch"]
    label = "  scout" if scouting else ("  review" if reviewing else "")
    line = (f"{st['slug']:24} {state:8} turns {st.get('turns', 0)}  {location}  "
            f"account {account}  thread {thread[:13]}" + label
            + "  sandbox " + ("read-only" if scouting else st.get("sandbox", "off")))
    if st.get("weekly_budget_warning"):
        line += "\n" + st["weekly_budget_warning"]
    if not detail:
        return line
    if scouting:
        return line + "\nlast message:\n" + (threads.last_message(st) or "(none)")
    pr = st.get("publish", {}).get("pr", "")
    commits = procs.sh(config.GIT + ["log", "--oneline", f"{st['base']}..HEAD"], cwd=st["worktree"], check=False)
    review = config.TASKS / st["slug"] / "review.md"
    message = review.read_text().strip() if reviewing and review.exists() else threads.last_message(st)
    return "\n".join([line, f"worktree {st['worktree']}", *result_schema.result_lines(st.get("result")),
                      f"PR: {pr or 'none'}", "commits:\n" + (commits or "(none)"),
                      "last message:\n" + (message or "(none)")])


def cmd_status(a):
    if a.slug:
        print(describe(task_state.load(a.slug), True))
        return
    for p in sorted(config.TASKS.glob("*/state.json")):
        print(describe(task_state.load(p.parent.name), False))


def cmd_stats(a):
    st = task_state.load(a.slug)
    cfg = config.accounts_config()
    for entry in st["threads"] + st["reviews"]:
        if entry.get("reviewer") == "claude":
            continue
        account = accounts.account_named(cfg, entry["account"])
        path = threads.rollout_path(account["home"], entry["thread"])
        print(f"== {entry['account']}: {entry['thread']}", flush=True)
        if path:
            subprocess.run([sys.executable, "-I", str(config.STATS), str(path)], check=True)
        else:
            print("(rollout not found)")


def cmd_report(a):
    report = runpy.run_path(str(config.REPORT))
    try:
        data = report["build_report"](config.CODEX_HOME / "codex-worker-log.jsonl", a.since, a.repo, a.slug)
    except ValueError as exc:
        procs.die(str(exc))
    print(json.dumps(data, ensure_ascii=False) if a.json else report["render_report"](data))


def cmd_stop(a):
    with task_state.state_lock(a.slug):
        st = task_state.load(a.slug)
        pid = st.get("pid")
        if not procs.alive(pid):
            print("not running")
            return
        st["stop_request"] = {"at": clock.CLOCK.time(), "pid": pid}
        task_state.write_state(st)
    try:
        os.killpg(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(100):
        time.sleep(0.2)
        latest = task_state.load(a.slug)
        if latest.get("pid") != pid or not procs.alive(pid):
            break
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    print(f"stopped {a.slug}")


def cmd_clean(a):
    st = task_state.load(a.slug)
    if procs.alive(st.get("pid")):
        procs.die("still running; stop it first")
    if st.get("kind") != "scout":
        repo, wt = st["repo"], st["worktree"]
        if Path(wt).exists():
            procs.sh(config.GIT + ["worktree", "remove", "--force", wt], cwd=repo)
            shutil.rmtree(Path(wt).parent, ignore_errors=True)
        if not a.keep_branch:
            subprocess.run(config.GIT + ["branch", "-D", st["branch"]], cwd=repo, capture_output=True)
    cfg = config.accounts_config()
    for entry in st["threads"] + st["reviews"]:
        if entry.get("reviewer") == "claude":
            continue
        account = accounts.account_named(cfg, entry["account"])
        accounts.prepare_home(account)
        subprocess.run([config.codex_binary(), "archive", entry["thread"]], env=accounts.account_env(account), capture_output=True)
    record = config.TASKS / a.slug
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock.CLOCK.time()))
    archive = config.TASKS / ".archive" / f"{a.slug}-{stamp}"
    if archive.exists():
        procs.die(f"task record archive already exists: {archive}")
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        (record / "events.jsonl").unlink(missing_ok=True)
        (record / "state.lock").unlink(missing_ok=True)
        record.rename(archive)
    except OSError as exc:
        procs.die(f"could not archive task record to {archive}: {exc}; record kept at {record}")
    print(f"cleaned {a.slug}; record in {archive}")


def cmd_accounts(a):
    cfg = config.accounts_config()
    print_accounts(accounts.snapshots(cfg), cfg)
    print(accounts.claude_reviewer_line())


def print_accounts(infos, cfg):
    if cfg.get("soft_switch_at", cfg["switch_at"]) != cfg["switch_at"]:
        print(f"soft_switch_at: {cfg['soft_switch_at']:g}% (switch_at: {cfg['switch_at']:g}%)")
    print("NAME\tEMAIL\tPLAN\tWEEKLY %\tRESET (LOCAL)\tCREDITS\tRESETS\tSTATUS")
    for info in infos:
        w = accounts.longest_window(info)
        credits = info.get("limits", {}).get("credits", {})
        if info.get("duplicate"):
            status = "same account as " + info["duplicate"]
        elif info["logged_in"] is None:
            status = "unknown (rpc error: " + info.get("error", "unavailable") + ")"
        elif not info["logged_in"]:
            status = "not logged in"
        elif accounts.exhausted(info, cfg):
            status = "exhausted until " + config.local_time(accounts.reset_time(info, cfg))
        else:
            status = "available"
        account = info.get("account", {})
        resets = credit_resets.reset_credits(info)
        count = info.get("reset_credits", {}).get("availableCount", len(resets))
        nearest = resets[0].get("expiresAt") if resets else None
        reset_label = str(count) + (f" (exp {config.local_time(nearest).split()[0]})" if nearest is not None else "")
        print("\t".join((info["name"], account.get("email") or "—", account.get("planType") or "—",
                         f"{w['used']:g}" if w else "—", config.local_time(w.get("reset")),
                         "unlimited" if credits.get("unlimited") else str(credits.get("balance") or "0"),
                         reset_label, status)))


def cmd_reset_credit(a):
    cfg = config.accounts_config()
    account = accounts.account_named(cfg, a.account)
    try:
        action = credit_resets.reset_credit_action(account, a.credit, confirmed=a.yes)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        procs.die(f"free limit reset failed: {exc}")
    credit = action["credit"]
    print(f"Free limit reset on {a.account}: {credit['id']} ({credit.get('title') or 'Full reset'}), "
          f"expires {config.local_time(credit.get('expiresAt'))}")
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
        procs.die("invalid account name")
    cfg = config.accounts_config()
    account = next((i for i in cfg["accounts"] if i["name"] == a.name), None)
    if account is None:
        account = {"name": a.name, "home": str(config.CODEX_HOME.parent / ".codex-accounts" / a.name)}
        cfg["accounts"].append(account)
    cfg.pop("enabled", None)
    config.CODEX_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config.CODEX_HOME / "codex-worker.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
    accounts.prepare_home(account)
    binary = config.codex_binary()
    os.execvpe(binary, [binary, "login"] + ([] if a.browser else ["--device-auth"]), accounts.account_env(account))
