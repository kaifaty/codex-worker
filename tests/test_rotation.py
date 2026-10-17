#!/usr/bin/env python3
"""Exercise account rotation through the public CLI, without a real Codex login.

Run with python3 -I tests/test_rotation.py. The mid-turn case deliberately
waits for the supervisor's real 30-second rollout poll.
"""
import datetime
import fcntl
import importlib.util
import io
import json
import os
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "codex-worker" / "skills" / "codex-worker"
SCRIPT = SKILL / "scripts" / "codex_task.py"
PYTHON = sys.executable


# The fake has the same process boundary as Codex: the supervisor must discover
# the quota in a rollout, interrupt the process group, and create a fresh thread.
FAKE_CODEX = r'''import datetime
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
fixture_path = home / "fixture.json"
trace_path = Path(os.environ["CODEX_FAKE_TRACE"])

def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")

def fixture():
    return json.loads(fixture_path.read_text())

def trace(value):
    value["home"] = str(home)
    value.setdefault("pid", os.getpid())
    value.setdefault("ppid", os.getppid())
    value.setdefault("pgid", os.getpgrp())
    value.setdefault("monotonic", time.monotonic())
    append(trace_path, value)

def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

def journal_records():
    journal = Path(os.environ["CODEX_TASK_HOME"]) / "codex-worker-resets.jsonl"
    entries = []
    for line in journal.read_text().splitlines() if journal.exists() else []:
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass  # A killed writer may leave a partial final record.
    return entries

def limits(value):
    result = {"primary": dict(value["primary"]),
              "secondary": dict(value["secondary"]) if value.get("secondary") else None,
              "credits": value.get("credits", {"hasCredits": False, "balance": "0", "unlimited": False}),
              "spendControlReached": False, "planType": "pro",
              "rateLimitReachedType": value.get("rateLimitReachedType")}
    if value.get("auto_reset"):
        for name in ("primary", "secondary"):
            window = result[name]
            if not window or window.get("resetsAt") is None or time.time() < window["resetsAt"]:
                continue
            release = value.get("release_at", window["resetsAt"])
            if time.time() < release:
                # A quota provider can postpone its reset after the first deadline.
                window["resetsAt"] = release
            else:
                window["usedPercent"] = 0
    return result

def app_server():
    trace({"command": "app-server"})
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        value = fixture()
        method = request["method"]
        if value.get("rpc_error") and method == "account/read":
            emit({"id": request["id"], "error": {"code": -32000, "message": "fixture account unavailable"}})
            continue
        if method == "initialize":
            result = {"userAgent": "fake-codex", "platformFamily": "unix"}
        elif method == "account/read":
            result = {"account": value.get("account"),
                      "workspaceRouting": {"chatgptAccountId": value["accountId"]}}
        elif method == "account/rateLimits/read":
            gate = value.get("rate_limits_gate_after_exec")
            if gate and (home / "exec-count").exists():
                trace({"command": "quota-waiting", "gate": gate})
                while not Path(gate).exists():
                    time.sleep(0.02)
            if value.get("clear_quota_when_reset_locked"):
                journal = Path(os.environ["CODEX_TASK_HOME"]) / "codex-worker-resets.jsonl"
                if journal.exists():
                    with journal.open("a") as stream:
                        try:
                            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            value["primary"]["usedPercent"] = 0
                            fixture_path.write_text(json.dumps(value))
            current_limits = limits(value)
            trace({"command": "quota-snapshot", "primary": current_limits["primary"]})
            result = {"accountId": value["accountId"],
                      "ordinaryUsageAllowed": value.get("ordinaryUsageAllowed", True),
                      "rateLimits": current_limits,
                      "rateLimitResetCredits": value.get("rateLimitResetCredits", {
                          "availableCount": 0, "credits": []})}
        elif method == "account/rateLimitResetCredit/consume":
            params = request.get("params") or {}
            trace({"command": "consume", "params": params, "journal_before": journal_records()})
            credit_id = params.get("creditId")
            key = params.get("idempotencyKey")
            if not isinstance(credit_id, str) or not credit_id or not isinstance(key, str) or not key:
                emit({"id": request["id"], "error": {"message": "creditId and idempotencyKey must not be empty"}})
                continue
            if value.get("consume_error") or value.get("consume_timeout"):
                message = ("rate limit reset consume timed out" if value.get("consume_timeout")
                           else "fixture reset credit unavailable")
                emit({"id": request["id"], "error": {"code": -32000, "message": message}})
                continue
            reset_credits = value.get("rateLimitResetCredits", {})
            credit = next((credit for credit in reset_credits.get("credits", [])
                           if credit.get("id") == credit_id and credit.get("status") == "available"), None)
            if not credit:
                emit({"id": request["id"], "error": {"message": "fixture reset credit not available"}})
                continue
            credit["status"] = "consumed"
            reset_credits["availableCount"] = sum(credit.get("status") == "available"
                                                    for credit in reset_credits["credits"])
            if not value.get("consume_no_reset"):
                for name in ("primary", "secondary"):
                    if value.get(name):
                        value[name]["usedPercent"] = 0
                value["ordinaryUsageAllowed"] = True
            fixture_path.write_text(json.dumps(value))
            if value.get("consume_timeout_applied"):
                emit({"id": request["id"], "error": {"code": -32000,
                      "message": "rate limit reset consume timed out"}})
                continue
            # Deliberately opaque fields: callers must not assume a response schema.
            result = {"fixtureReceipt": "receipt-for-" + credit_id, "fixtureApplied": True}
        else:
            emit({"id": request["id"], "error": {"message": "unknown method"}})
            continue
        # Both unrelated replies and notifications may precede the reply we need.
        emit({"method": "account/rateLimits/updated", "params": {"ignored": True}})
        emit({"id": 99999, "result": {"ignored": True}})
        emit({"id": request["id"], "result": result})

def execute():
    argv = sys.argv[2:]
    resumed = bool(argv and argv[0] == "resume")
    review = bool(argv and argv[0] == "review")
    value = fixture()
    prompt = argv[-1]
    output = Path(argv[argv.index("-o") + 1]) if "-o" in argv else None
    schema = Path(argv[argv.index("--output-schema") + 1]) if "--output-schema" in argv else None
    invocation = {"command": "exec", "resume": resumed, "review": review,
                  "prompt": prompt, "pid": os.getpid(), "ppid": os.getppid(),
                  "argv": sys.argv[1:], "cwd": os.getcwd(),
                  "output": str(output) if output else None,
                  "schema_path": str(schema) if schema else None,
                  "output_schema": json.loads(schema.read_text()) if schema else None}
    if review and value.get("review_prompt_conflict"):
        trace(dict(invocation, thread="", mode="review_prompt_conflict"))
        print("error: the argument '--base <BRANCH>' cannot be used with '[PROMPT]'", file=sys.stderr,
              flush=True)
        return 2
    thread = argv[1] if resumed else str(uuid.uuid4())
    counter = home / "exec-count"
    number = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(number + 1))
    runs = value.get("runs", [{"mode": "complete", "reply": "done"}])
    run = runs[min(number, len(runs) - 1)]
    day = datetime.datetime.now()
    sessions = home / "sessions" / day.strftime("%Y/%m/%d")
    sessions.mkdir(parents=True, exist_ok=True)
    found = list(sessions.glob("rollout-*-" + thread + ".jsonl"))
    rollout = found[0] if found else sessions / ("rollout-" + day.strftime("%Y-%m-%dT%H-%M-%S") + "-" + thread + ".jsonl")
    stopped = [False]
    child = None

    def interrupted(signum, frame):
        trace({"command": "signal", "signal": signum, "thread": thread, "pid": os.getpid()})
        stopped[0] = True

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    trace(dict(invocation, thread=thread, mode=run["mode"], journal_before=journal_records()))

    def rollout_limits():
        current = fixture()
        raw = limits(current)
        if "rollout_used" in run:
            raw["primary"] = dict(raw["primary"], usedPercent=run["rollout_used"])
        windows = {}
        for name in ("primary", "secondary"):
            window = raw[name]
            windows[name] = ({"used_percent": window["usedPercent"],
                              "window_minutes": window["windowDurationMins"],
                              "resets_at": window["resetsAt"]} if window else None)
        windows["credits"] = raw["credits"]
        windows["rate_limit_reached_type"] = raw["rateLimitReachedType"]
        append(rollout, {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         "type": "event_msg", "payload": {"type": "token_count",
                         "info": {"total_token_usage": {"total_tokens": 12}},
                         "rate_limits": windows}})

    def message(text):
        emit({"type": "item.completed", "item": {"id": str(uuid.uuid4()),
              "type": "agent_message", "text": text}})
        append(rollout, {"type": "event_msg", "payload": {"type": "agent_message", "message": text}})

    def auth_failure():
        value = fixture()
        if "account_after" in run:
            value["account"] = run["account_after"]
        if "used_after" in run:
            value["primary"]["usedPercent"] = run["used_after"]
        if "account_after" in run or "used_after" in run:
            fixture_path.write_text(json.dumps(value))
        for other_home in run.get("logout_other_homes", []):
            path = Path(other_home) / "fixture.json"
            other = json.loads(path.read_text())
            other["account"] = None
            path.write_text(json.dumps(other))
        error = {"message": run.get("error", "Your access token could not be refreshed because you have since "
                                   "logged out or signed in to another account. Please sign in again.")}
        if run.get("event") == "error":
            emit(dict(error, type="error"))
        else:
            emit({"type": "turn.failed", "error": error})
        return 1

    if run["mode"] == "auth_error" and run.get("before_thread"):
        return auth_failure()
    if run.get("rollout_gate"):
        trace({"command": "rollout-waiting", "thread": thread})
        while not Path(run["rollout_gate"]).exists() and not stopped[0]:
            time.sleep(0.02)
        if stopped[0]:
            return 130
    rollout_limits()
    # Publish the ready fixture before the supervisor starts looking for its rollout.
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    if run.get("spawn_child"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        trace({"command": "exec-child", "child_pid": child.pid, "thread": thread})
    for command in run.get("commands", []):
        item = {"id": str(uuid.uuid4()), "type": command.get("item_type", "command_execution"),
                "command": command["command"], "exit_code": command["exit_code"],
                "aggregated_output": command.get("aggregated_output", "fixture command failed\n")}
        emit({"type": command.get("event_type", "item.completed"), "item": item})
    for text in run.get("messages", []):
        message(text)
    if run.get("modify"):
        Path("worker.txt").write_text("checkpoint by account A\n")
        subprocess.run(["git", "add", "worker.txt"], check=True)
        subprocess.run(["git", "commit", "-qm", "checkpoint from account A"], check=True)
        Path("tracked.txt").write_text("unfinished changes from account A\n")
        Path("pending.txt").write_text("unfinished untracked file\n")
    for name in run.get("create_files", []):
        Path(name).write_text("created by fake Codex\n")
    for name, contents in run.get("write_files", {}).items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding=run.get("write_encodings", {}).get(name))
    for name in run.get("remove_files", []):
        Path(name).unlink(missing_ok=True)
    if run.get("commit_files"):
        subprocess.run(["git", "add", "--"] + run["commit_files"], check=True)
        subprocess.run(["git", "commit", "-qm", "Apply fake worker changes"], check=True)
    if run["mode"] in ("limit", "usage_error"):
        time.sleep(run.get("limit_after", 0.15))
        value = fixture()
        value["primary"]["usedPercent"] = 99
        value["ordinaryUsageAllowed"] = run.get("ordinaryUsageAllowed", True)
        fixture_path.write_text(json.dumps(value))
        rollout_limits()
        trace({"command": "quota-written", "thread": thread})
    if run["mode"] == "usage_error":
        emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}})
        return 1
    if run["mode"] == "failed":
        emit({"type": "turn.failed", "error": {"message": "fixture task failed"}})
        return 1
    if run["mode"] == "auth_error":
        return auth_failure()
    deadline = time.monotonic() + run.get("delay", 0)
    while not stopped[0] and time.monotonic() < deadline:
        time.sleep(0.02)
    if run["mode"] in ("limit", "hold"):
        deadline = time.monotonic() + 100
        while not stopped[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        if stopped[0]:
            if child:
                child.wait(timeout=2)
            return 130
        emit({"type": "turn.failed", "error": {"message": "fake turn was never interrupted"}})
        return 2
    if stopped[0]:
        if child:
            child.wait(timeout=2)
        return 130
    time.sleep(0.05)
    for n in range(run.get("reconnects", 0)):
        emit({"type": "error", "message": f"Reconnecting... {n + 1}/5 (stream disconnected before completion)"})
    reply = run.get("reply", "done")
    if not isinstance(reply, str):
        reply = json.dumps(reply, ensure_ascii=False)
    message(reply)
    if output:
        output.write_text(reply + "\n")
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    return 0

command = sys.argv[1]
if command == "app-server":
    app_server()
elif command == "exec":
    sys.exit(execute())
elif command == "sandbox":
    trace({"command": "sandbox", "argv": sys.argv[1:], "cwd": os.getcwd()})
    args = sys.argv[sys.argv.index("--") + 1:]
    os.execv(args[0], args)
elif command == "archive":
    trace({"command": "archive", "thread": sys.argv[2]})
else:
    sys.exit("unsupported fake codex command: " + command)
'''


FAKE_GH = r'''import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["CODEX_FAKE_GH_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
event = {"argv": args, "cwd": os.getcwd()}
if "--body-file" in args:
    event["body"] = Path(args[args.index("--body-file") + 1]).read_text()
with Path(os.environ["CODEX_FAKE_GH_TRACE"]).open("a") as stream:
    stream.write(json.dumps(event) + "\n")
branch = args[args.index("--head") + 1] if "--head" in args else ""
if args[:2] == ["pr", "list"]:
    url = state.get(branch, "")
    if "--jq" in args:
        expression = args[args.index("--jq") + 1]
        print(("42 OPEN " + url if "number" in expression else url) if url else "")
    else:
        print(json.dumps([{"number": 42, "state": "OPEN", "url": url}] if url else []))
elif args[:2] == ["pr", "create"]:
    if os.environ.get("CODEX_FAKE_GH_CREATE_ERROR"):
        print("fixture PR creation failed", file=sys.stderr)
        sys.exit(1)
    url = "https://example.test/pull/" + str(len(state) + 1)
    state[branch] = url
    state_path.write_text(json.dumps(state))
    print(url)
else:
    sys.exit("unsupported fake gh invocation")
'''


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-rotation-")
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        (self.home / "config.toml").write_text('model = "fake-model"\n')
        (self.home / "AGENTS.md").write_text("Shared instructions.\n")
        (self.home / "skills").mkdir()
        (self.home / "rules").mkdir()
        self.other = self.root / "second-home"
        self.other.mkdir()
        self.trace_path = self.root / "fake-trace.jsonl"
        self.extra_pids = set()
        self.fake = self.root / "codex"
        self.fake.write_text("#!" + PYTHON + "\n" + FAKE_CODEX)
        self.fake.chmod(0o700)
        self.gh_trace_path = self.root / "fake-gh-trace.jsonl"
        fake_gh = self.root / "gh"
        fake_gh.write_text("#!" + PYTHON + "\n" + FAKE_GH)
        fake_gh.chmod(0o700)
        self.env = dict(os.environ, CODEX_TASK_HOME=str(self.home),
                        CODEX_TASK_CODEX=str(self.fake), CODEX_FAKE_TRACE=str(self.trace_path),
                        CODEX_FAKE_GH_TRACE=str(self.gh_trace_path),
                        CODEX_FAKE_GH_STATE=str(self.root / "fake-gh-state.json"),
                        PATH=str(self.root) + os.pathsep + os.environ.get("PATH", ""),
                        CODEX_TASK_SLEEP_STEP="0.05", CODEX_TASK_RESET_MARGIN="0.1",
                        GIT_AUTHOR_NAME="Rotation Test", GIT_AUTHOR_EMAIL="rotation@example.test",
                        GIT_COMMITTER_NAME="Rotation Test", GIT_COMMITTER_EMAIL="rotation@example.test")
        # The external process gets no opportunity to ask for a credential or pager.
        self.env.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        (self.repo / "tracked.txt").write_text("initial\n")
        self.git("add", "tracked.txt")
        self.git("commit", "--quiet", "-m", "initial")
        origin = self.root / "origin.git"
        self.git("init", "--quiet", "--bare", "--initial-branch=main", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "--quiet", "-u", "origin", "main")
        self.git("remote", "set-head", "origin", "main")
        self.brief = self.root / "brief.md"
        self.brief.write_text("Реализовать переносимую задачу.\nСохранить весь исходный бриф.\n"
                              "Готово, когда:\n- весь исходный бриф сохранён.\n")
        # A reset well into the future also makes local-time assertions stable.
        self.reset = int(time.time()) + 7200
        self.account(self.home, "A", self.reset)
        self.account(self.other, "B", self.reset + 3600)

    def tearDown(self):
        # Supervisors publish final state after SIGTERM. Wait for every owned
        # writer and its tools before removing their directories.
        owned, groups = set(self.extra_pids), set()

        def processes():
            for path in (self.home / "claude-tasks").glob("*/state.json"):
                try:
                    state = json.loads(path.read_text())
                    owned.update(int(state[key]) for key in ("pid", "codex_pid", "check_pid") if state.get(key))
                except (OSError, ValueError):
                    pass
            for event in self.trace():
                if event.get("command") in ("exec", "app-server"):
                    owned.update(int(event[key]) for key in ("pid", "ppid") if event.get(key))
                    if event.get("pgid"):
                        groups.add(int(event["pgid"]))
            owned.difference_update((0, 1, os.getpid()))
            groups.difference_update((0, 1, os.getpgrp()))
            result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat=,command="],
                                    text=True, capture_output=True, check=True)
            table = {}
            for line in result.stdout.splitlines():
                fields = line.split(None, 4)
                if len(fields) == 5:
                    table[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3])
                    # Hooks start in their own session and may outlive the
                    # supervisor's final pid=None save. Their fixture paths
                    # still identify them after they have been reparented.
                    if str(self.root) in fields[4]:
                        owned.add(int(fields[0]))
            while True:
                descendants = {pid for pid, (parent, group, _) in table.items()
                               if parent in owned or group in groups}
                if descendants <= owned:
                    break
                owned.update(descendants)
            groups.update(group for pid, (_, group, _) in table.items() if pid in owned and group == pid)
            live = set()
            for pid in owned:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError):
                    pass
                if pid in table and not table[pid][2].startswith("Z"):
                    live.add(pid)
            return live

        live = set()
        for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
            signalled, signalled_groups = set(), set()
            deadline = time.monotonic() + grace
            while True:
                live = processes()
                if not live:
                    break
                for group in groups - signalled_groups:
                    try:
                        os.killpg(group, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                    signalled_groups.add(group)
                for pid in live - signalled:
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                    signalled.add(pid)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            if not live:
                break
        self.assertFalse(live, "task processes still running before temporary-directory cleanup: %s" % sorted(live))
        self.tmp.cleanup()

    def git(self, *args):
        result = subprocess.run(["git"] + list(args), cwd=self.repo, env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit_fixture(self, files):
        for name, contents in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self.git("add", "--", *files)
        self.git("commit", "--quiet", "-m", "Add guarded file fixtures")
        self.git("push", "--quiet", "origin", "main")

    def cli(self, *args, **kwargs):
        result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args), cwd=self.repo,
                                env=self.env, text=True, capture_output=True,
                                timeout=kwargs.get("timeout", 15))
        self.assertEqual(result.returncode, 0,
                         "command: %s\nstdout:\n%s\nstderr:\n%s" % (args, result.stdout, result.stderr))
        # Keep supervisors after their final save clears state['pid']; wait
        # returns as soon as that idle state is visible, before process exit.
        if args and args[0] in ("start", "scout", "say", "review"):
            for line in result.stdout.splitlines():
                pid = line.rpartition(", pid ")[2]
                if pid.isdigit():
                    self.extra_pids.add(int(pid))
        return result.stdout

    def account(self, home, account_id, reset, used=10, runs=None, auto_reset=False, release_at=None):
        value = {"account": {"type": "chatgpt", "email": account_id.lower() + "@example.test", "planType": "pro"},
                 "accountId": account_id, "ordinaryUsageAllowed": True,
                 "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset},
                 "secondary": {"usedPercent": 2, "windowDurationMins": 300, "resetsAt": reset - 1800},
                 "credits": {"hasCredits": True, "unlimited": False, "balance": "123.45"},
                 "runs": runs or [{"mode": "complete", "reply": "Результат " + account_id}]}
        if auto_reset:
            value["auto_reset"] = True
        if release_at is not None:
            value["release_at"] = release_at
        (home / "fixture.json").write_text(json.dumps(value))
        return value

    def configure(self, credits=False, wait_for_reset=None, language="ru",
                  use_reset_credits=None, reset_credit_min_hours=None, **settings):
        config = {
            "switch_at": 99, "credits": credits, "language": language,
            "accounts": [{"name": "main", "home": str(self.home)},
                         {"name": "second", "home": str(self.other)}]}
        if wait_for_reset is not None:
            config["wait_for_reset"] = wait_for_reset
        if use_reset_credits is not None:
            config["use_reset_credits"] = use_reset_credits
        if reset_credit_min_hours is not None:
            config["reset_credit_min_hours"] = reset_credit_min_hours
        config.update(settings)
        (self.home / "claude-accounts.json").write_text(json.dumps(config))

    def reset_credits(self, home, *credits, **changes):
        path = home / "fixture.json"
        value = json.loads(path.read_text())
        value["rateLimitResetCredits"] = {
            "availableCount": sum(credit.get("status") == "available" for credit in credits),
            "credits": list(credits)}
        value.update(changes)
        path.write_text(json.dumps(value))

    def reset_credit(self, credit_id, expires, **changes):
        credit = {"id": credit_id, "resetType": "codexRateLimits", "status": "available",
                  "grantedAt": int(time.time()) - 86400, "expiresAt": expires,
                  "title": "Full reset", "description": "A free full reset from the fixture."}
        credit.update(changes)
        return credit

    def consume_calls(self):
        return [event for event in self.trace() if event.get("command") == "consume"]

    def reset_journal(self):
        path = self.home / "codex-worker-resets.jsonl"
        entries = []
        for line in path.read_text().splitlines() if path.exists() else []:
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
        return entries

    def worker(self):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "dont_write_bytecode", True):
            spec = importlib.util.spec_from_file_location("rotation_worker", str(SCRIPT))
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
        return worker

    def start(self, slug, *args):
        return self.cli("start", slug, "--brief", str(self.brief), "--repo", str(self.repo), *args)

    def state(self, slug):
        return json.loads((self.home / "claude-tasks" / slug / "state.json").read_text())

    def trace(self):
        if not self.trace_path.exists():
            return []
        events = []
        for line in self.trace_path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        return events

    def invocations(self):
        return [event for event in self.trace() if event.get("command") == "exec"]

    def review_invocations(self):
        return [event for event in self.invocations() if event.get("review")]

    def sandbox_invocations(self):
        return [event for event in self.trace() if event.get("command") == "sandbox"]

    def gh_invocations(self):
        if not self.gh_trace_path.exists():
            return []
        return [json.loads(line) for line in self.gh_trace_path.read_text().splitlines()]

    def record_git_calls(self):
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        path = self.root / "git-trace.jsonl"
        wrapper = self.root / "git"
        wrapper.write_text(
            "#!" + PYTHON + "\n"
            "import json, os, sys\n"
            "with open(" + repr(str(path)) + ", 'a') as stream:\n"
            "    stream.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
            "os.execv(" + repr(real_git) + ", [" + repr(real_git) + "] + sys.argv[1:])\n")
        wrapper.chmod(0o700)
        return path

    def configure_notify(self, events=None, command=None):
        path = self.home / "codex-worker.json"
        legacy = self.home / "claude-accounts.json"
        source = path if path.exists() else legacy
        config = json.loads(source.read_text()) if source.exists() else {}
        self.notify_path = self.root / "notifications.jsonl"
        if command is None:
            receiver = self.root / "receive-notification.py"
            receiver.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "event = json.loads(sys.argv[-1])\n"
                "with Path(sys.argv[1]).open('a') as stream:\n"
                "    stream.write(json.dumps(event) + '\\n')\n")
            command = [PYTHON, str(receiver), str(self.notify_path)]
        config["notify"] = command
        if events is not None:
            config["notify_events"] = events
        path.write_text(json.dumps(config))

    def notifications(self, slug=None):
        result = []
        if self.notify_path.exists():
            for line in self.notify_path.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # The asynchronous writer may still be appending.
                if slug is None or event.get("slug") == slug:
                    result.append(event)
        return result

    def notification(self, slug, name):
        self.until(lambda: any(event.get("event") == name for event in self.notifications(slug)))
        return next(event for event in self.notifications(slug) if event.get("event") == name)

    def process_is_live(self, pid):
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                                text=True, capture_output=True)
        return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")

    def check_child(self, slug):
        path = Path(self.state(slug)["worktree"]) / "check-child.pid"
        self.until(lambda: path.exists() and path.read_text().strip().isdigit())
        pid = int(path.read_text())
        self.extra_pids.add(pid)
        return pid

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for fake Codex fixture")

    def wait(self, slug, timeout=90, through_sleep=False):
        args = ["wait", slug, "--timeout", str(timeout)]
        if through_sleep:
            args.append("--through-sleep")
        pid = self.state(slug).get("pid")
        if pid:
            self.extra_pids.add(pid)
        output = self.cli(*args, timeout=timeout + 15)
        state = self.state(slug)
        stderr_path = self.home / "claude-tasks" / slug / "stderr.log"
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
        diagnostics = "state:\n%s\nstderr.log:\n%s\nwait:\n%s" % (
            json.dumps(state, ensure_ascii=False, indent=2), stderr, output)
        self.assertNotIn("still running", output, diagnostics)
        self.assertFalse(state.get("error"), diagnostics)
        self.assertNotEqual(state.get("outcome"), "failed", diagnostics)
        return output

    def assert_rotation(self, slug, output):
        state = self.state(slug)
        self.assertEqual(state["account"], "second")
        rotations = [entry for entry in state["rotations"] if "from" in entry]
        self.assertEqual(len(rotations), 1)
        rotation = rotations[0]
        self.assertEqual((rotation["from"], rotation["to"]), ("main", "second"))
        self.assertTrue(rotation["reason"])
        self.assertIn("at", rotation)
        self.assertEqual(rotation["thread"], state["thread"])
        self.assertEqual([thread["account"] for thread in state["threads"]], ["main", "second"])
        self.assertTrue(all(thread.get("started") for thread in state["threads"]))
        self.assertIn("== rotated main → second:", output)
        self.assertIn("new thread", output)
        self.assertIn("Результат B", output)
        executions = self.invocations()
        self.assertNotEqual(executions[0]["thread"], executions[-1]["thread"])
        self.assertFalse(executions[-1]["resume"], "threads cannot resume across accounts")
        self.assertEqual(executions[-1]["home"], str(self.other))
        return executions[-1]["prompt"]

    def assert_sleeping(self, slug, output, reset, account="main"):
        state = self.state(slug)
        self.assertEqual(state.get("outcome"), "sleeping")
        self.assertTrue(state.get("pid"))
        os.kill(state["pid"], 0)
        sleeping = state["sleeping"]
        self.assertAlmostEqual(sleeping["until"], reset + 0.1, places=3)
        self.assertEqual(sleeping["account"], account)
        self.assertTrue(sleeping["reason"])
        expected = datetime.datetime.fromtimestamp(sleeping["until"]).strftime("%Y-%m-%d %H:%M")
        self.assertIn("== all accounts at limit; auto-continue at " + expected +
                      " (" + account + "); run wait again to follow", output)
        self.assertIn("== commits on ", output)
        return state

    def assert_scout_read_only(self, invocation):
        argv = invocation["argv"]
        self.assertIn('sandbox_mode="read-only"', argv)
        index = argv.index('sandbox_mode="read-only"')
        self.assertGreater(index, 0)
        self.assertEqual(argv[index - 1], "-c")
        self.assertNotIn("--output-schema", argv)
        self.assertIsNone(invocation["schema_path"])
        self.assertIsNone(invocation["output_schema"])

    def work_result(self, status="done", **changes):
        result = {"status": status, "summary": "Implemented the typed final result.",
                  "checks": [{"command": "python3 -I tests/test_rotation.py", "outcome": "passed",
                              "details": "3 tests passed, 0 failed"}],
                  "pr": "https://example.test/pull/42", "questions": [], "remaining": [],
                  "findings": [], "commit_message": ""}
        result.update(changes)
        return result

    def review_result(self, defects=True):
        findings = [
            {"id": "F1", "severity": "P2", "file": "src/parser.py", "line": 12,
             "title": "Empty input raises an exception", "detail": "An empty line reaches the index lookup.",
             "verify": "Pass an empty line and assert that parsing succeeds."},
            {"id": "F2", "severity": "P3", "file": "src/format.py", "line": 7,
             "title": "Summary loses the final word", "detail": "The slice excludes the last token.",
             "verify": "Format three words and check that all three remain."}]
        return {"verdict": "defects" if defects else "clean", "findings": findings if defects else [],
                "summary": "Found two defects." if defects else "No defects found."}

    def assert_output_schema(self, invocation, slug, review=False):
        filename = "review-schema.json" if review else "result-schema.json"
        expected = self.home / "claude-tasks" / slug / filename
        self.assertIn("--output-schema", invocation["argv"])
        self.assertEqual(invocation["schema_path"], str(expected))
        schema = invocation["output_schema"]
        self.assertEqual(schema, json.loads(expected.read_text()))

        def strict(value):
            if isinstance(value, dict):
                if value.get("type") == "object" or "properties" in value:
                    self.assertIs(value.get("additionalProperties"), False, value)
                    self.assertCountEqual(value.get("required", []), value.get("properties", {}), value)
                for child in value.values():
                    strict(child)
            elif isinstance(value, list):
                for child in value:
                    strict(child)

        strict(schema)
        self.assertEqual(schema["type"], "object")
        properties = schema["properties"]
        expected_fields = (set(self.review_result()) if review else set(self.work_result()))
        self.assertEqual(set(properties), expected_fields)
        field = "verdict" if review else "status"
        choices = (["clean", "defects"] if review else
                   ["done", "partial", "blocked", "needs_decision"])
        self.assertCountEqual(properties[field]["enum"], choices)
        if not review:
            self.assertEqual(properties["commit_message"]["type"], "string")
            decision = properties["findings"]["items"]["properties"]["decision"]
            self.assertCountEqual(decision["enum"], ["fixed", "rejected"])

    def assert_workspace_sandbox(self, invocation, writable_dirs=()):
        argv = invocation["argv"]
        settings = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-c"]
        sandbox = [value for value in settings if value.startswith("sandbox_")]
        self.assertEqual(len(sandbox), 3, argv)
        self.assertIn('sandbox_mode="workspace-write"', sandbox)
        self.assertIn("sandbox_workspace_write.network_access=true", sandbox)
        roots = next(value.split("=", 1)[1] for value in sandbox
                     if value.startswith("sandbox_workspace_write.writable_roots="))
        self.assertEqual(json.loads(roots), list(writable_dirs))
        self.assertNotIn("\x7f", roots, "TOML requires DEL to be escaped")
        self.assertNotIn("-s", argv)
        self.assertNotIn("--add-dir", argv)

    def assert_no_workspace_sandbox(self, invocation):
        self.assertFalse(any(value.startswith("sandbox_") for value in invocation["argv"]),
                         invocation["argv"])

    def test_workspace_settings_are_normalized_saved_and_inherited_by_say_and_review(self):
        user_home = self.root / "user-home"
        user_home.mkdir()
        self.env["HOME"] = str(user_home)
        cache = '.cache/quote"-slash\\-line\n-tab\t-del\x7f-🙂'
        roots = [str(user_home / cache), str(self.repo / "build-cache")]
        self.configure(language="en", sandbox="workspace", writable_dirs=["~/" + cache, "build-cache"])
        settings = self.home / "codex-worker.json"
        settings.write_text((self.home / "claude-accounts.json").read_text())
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)},
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("sandbox-inherit", "--review")
        output = self.wait("sandbox-inherit")
        state = self.state("sandbox-inherit")
        self.assertEqual((state["sandbox"], state["writable_dirs"]), ("workspace", roots))
        self.assertIn("== sandbox: workspace", output)
        for args in (("status",), ("status", "sandbox-inherit")):
            self.assertIn("sandbox workspace", self.cli(*args))
        config = json.loads(settings.read_text())
        config.update(sandbox="off", writable_dirs=[])
        settings.write_text(json.dumps(config))
        self.cli("say", "sandbox-inherit", "Continue with the task's saved sandbox")
        self.wait("sandbox-inherit")
        self.cli("review", "sandbox-inherit")
        self.wait("sandbox-inherit")
        executions = self.invocations()
        self.assertEqual([event["resume"] for event in executions], [False, False, True, False])
        self.assertEqual([event["review"] for event in executions], [False, True, False, True])
        self.assertEqual(self.state("sandbox-inherit")["writable_dirs"], roots)
        for event in executions:
            self.assert_workspace_sandbox(event, roots)
            self.assertIn("## Workspace sandbox", event["prompt"])
            if event["review"]:
                self.assertNotIn("commit_message", event["prompt"])
                self.assertNotIn("commit_message", event["output_schema"]["properties"])
        self.start("sandbox-override", "--sandbox", "workspace")
        self.wait("sandbox-override")
        self.assertEqual(self.state("sandbox-override")["sandbox"], "workspace")
        self.assert_workspace_sandbox(self.invocations()[-1])

    def test_workspace_settings_validate_values_and_default_to_off(self):
        worker = self.worker()
        config = worker.accounts_config()
        self.assertEqual(config["sandbox"], "off")
        self.assertEqual(config["writable_dirs"], [])
        invalid = {"sandbox": (None, False, "workspace-write"),
                   "writable_dirs": (None, "cache", [None], [""])}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    (self.home / "codex-worker.json").write_text(json.dumps({key: value}))
                    with self.assertRaises(SystemExit) as raised:
                        worker.accounts_config()
                    self.assertIn(key, str(raised.exception))

    def test_workspace_rules_are_localized_and_off_has_no_sandbox_flags_or_section(self):
        for language, heading in (("en", "## Workspace sandbox"), ("ru", "## Песочница worktree")):
            with self.subTest(language=language):
                self.configure(language=language, sandbox="workspace")
                self.start("sandbox-rules-" + language)
                self.wait("sandbox-rules-" + language)
                prompt = self.invocations()[-1]["prompt"]
                self.assertIn(heading, prompt)
                for text in ("git commit", "git push", "gh pr create", "commit_message", "worktree"):
                    self.assertIn(text, prompt[prompt.index(heading):])
                self.start("sandbox-off-" + language, "--sandbox", "off")
                output = self.wait("sandbox-off-" + language)
                self.assert_no_workspace_sandbox(self.invocations()[-1])
                self.assertNotIn(heading, self.invocations()[-1]["prompt"])
                self.assertNotIn("== sandbox:", output)
                self.assertEqual(self.state("sandbox-off-" + language)["sandbox"], "off")
        self.configure(language="en")
        self.start("sandbox-default-off")
        self.wait("sandbox-default-off")
        self.assert_no_workspace_sandbox(self.invocations()[-1])
        self.assertIn("sandbox off", self.cli("status", "sandbox-default-off"))

    def test_workspace_rotation_and_review_fallback_keep_the_sandbox_and_scout_stays_read_only(self):
        self.configure(language="en", sandbox="workspace", writable_dirs=[str(self.root / "cache")])
        roots = [str(self.root / "cache")]
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("sandbox-rotation")
        self.wait("sandbox-rotation")
        self.assertEqual(self.state("sandbox-rotation")["account"], "second")
        self.assertEqual(len(self.invocations()), 2)
        self.assertNotEqual(self.invocations()[0]["thread"], self.invocations()[1]["thread"])
        fixture = json.loads((self.other / "fixture.json").read_text())
        fixture["review_prompt_conflict"] = True
        (self.other / "fixture.json").write_text(json.dumps(fixture))
        self.cli("review", "sandbox-rotation")
        self.wait("sandbox-rotation")
        rejected, fallback = self.invocations()[-2:]
        self.assertEqual(rejected["mode"], "review_prompt_conflict")
        self.assertTrue(rejected["review"])
        self.assertFalse(fallback["review"])
        self.assertEqual(fallback["prompt"], rejected["prompt"])
        for event in self.invocations():
            self.assert_workspace_sandbox(event, roots)
            self.assertIn("## Workspace sandbox", event["prompt"])
        for event in (rejected, fallback):
            self.assertNotIn("commit_message", event["prompt"])
            self.assertNotIn("commit_message", event["output_schema"]["properties"])
        self.cli("scout", "sandbox-scout", "Read the task's implementation")
        self.wait("sandbox-scout")
        scout = self.invocations()[-1]
        self.assert_scout_read_only(scout)
        self.assertFalse(any(value.startswith("sandbox_workspace_write.") for value in scout["argv"]))
        self.assertNotIn("## Workspace sandbox", scout["prompt"])
        self.assertIn("sandbox read-only", self.cli("status", "sandbox-scout"))

    def test_workspace_supervisor_commits_pushes_without_hooks_and_creates_only_one_pr(self):
        self.configure(language="en", sandbox="workspace")
        hooks = self.root / "hooks"
        hooks.mkdir()
        markers = []
        for name in ("pre-commit", "pre-push"):
            marker = self.root / (name + ".ran")
            markers.append(marker)
            hook = hooks / name
            hook.write_text("#!/bin/sh\n: > " + shlex.quote(str(marker)) + "\nexit 17\n")
            hook.chmod(0o700)
        self.git("config", "core.hooksPath", str(hooks))
        message = 'Publish "workspace" changes\n\nKeep supervisor hooks disabled.\nRecord the worktree changes.'
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "first sandbox change\n",
                                                     "new.txt": "new file\n"},
             "reply": self.work_result(commit_message=message)},
            {"mode": "complete", "write_files": {"tracked.txt": "second sandbox change\n"},
             "remove_files": ["new.txt"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result()}])
        self.start("sandbox-publish")
        output = self.wait("sandbox-publish")
        state = self.state("sandbox-publish")
        publish = state["publish"]
        self.assertEqual(set(publish), {"at", "sha", "pushed", "pr", "error"})
        self.assertGreater(publish["at"], 0)
        self.assertEqual(publish["sha"], self.git("rev-parse", state["branch"]))
        self.assertTrue(publish["pushed"])
        self.assertEqual(publish["error"], "")
        self.assertEqual(publish["pr"], "https://example.test/pull/1")
        self.assertEqual(self.git("log", "-1", "--format=%B", state["branch"]), message)
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse", state["branch"]),
                         publish["sha"])
        subject = message.splitlines()[0]
        self.assertIn("== committed " + publish["sha"] + " " + json.dumps(subject) +
                      "; pushed; PR " + publish["pr"], output)
        creates = [event for event in self.gh_invocations() if event["argv"][:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 1)
        args = creates[0]["argv"]
        self.assertEqual(args[args.index("--base") + 1], "main")
        self.assertEqual(args[args.index("--head") + 1], state["branch"])
        self.assertEqual(args[args.index("--title") + 1], subject)
        self.assertEqual(creates[0]["body"], message.partition("\n")[2].strip())
        first_sha = publish["sha"]
        self.cli("say", "sandbox-publish", "Make another change")
        self.wait("sandbox-publish")
        state = self.state("sandbox-publish")
        self.assertNotEqual(state["publish"]["sha"], first_sha)
        self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]), "sandbox-publish: turn 2")
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse", state["branch"]),
                         state["publish"]["sha"])
        self.assertFalse((Path(state["worktree"]) / "new.txt").exists())
        for marker in markers:
            self.assertFalse(marker.exists(), "supervisor must disable " + marker.stem)
        second_sha = state["publish"]["sha"]
        self.cli("say", "sandbox-publish", "Confirm that no more changes are needed")
        self.wait("sandbox-publish")
        self.assertEqual(self.git("rev-parse", state["branch"]), second_sha)
        self.assertFalse(self.state("sandbox-publish").get("publish"))
        self.assertEqual(len([event for event in self.gh_invocations()
                              if event["argv"][:2] == ["pr", "create"]]), 1)

    def test_workspace_commits_completed_turns_with_any_typed_status(self):
        self.configure(language="en", sandbox="workspace")
        for status in ("done", "partial", "blocked", "needs_decision"):
            with self.subTest(status=status):
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": status + "\n"},
                    "reply": self.work_result(status, commit_message="Save " + status + " checkpoint")}])
                slug = "sandbox-status-" + status.replace("_", "-")
                self.start(slug)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual(state["result"]["status"], status)
                self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]),
                                 "Save " + status + " checkpoint")
                self.assertTrue(state["publish"]["pushed"])

    def test_workspace_filter_guard_blocks_worker_clean_script_before_status_and_wait(self):
        self.configure(language="en", sandbox="workspace")
        # Commit the attributes while no driver is configured, then install a
        # harmless initial clean command before the worker replaces its script.
        self.commit_fixture({".gitattributes": "tracked.txt filter=project\n",
                             "clean.sh": "#!/bin/sh\ncat\n"})
        self.git("config", "filter.project.clean", "/bin/sh clean.sh")
        initial = self.git("rev-parse", "HEAD")
        marker = self.root / "outside-worktree.marker"
        marker.write_text("untouched\n")
        script = "#!/bin/sh\nprintf 'filter executed\\n' > " + shlex.quote(str(marker)) + "\ncat\n"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "worker change\n", "clean.sh": script},
            "reply": self.work_result(commit_message="Do not run a worker-written filter")}])
        trace_path = self.record_git_calls()
        self.start("sandbox-filter-guard")
        output = self.wait("sandbox-filter-guard")
        state = self.state("sandbox-filter-guard")
        error = ("commit: filter driver project is configured; the supervisor does not run filters "
                 "on worker-written files — commit this turn manually")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["publish"]["error"], error)
        self.assertEqual(state["publish"]["sha"], "")
        self.assertFalse(state["publish"]["pushed"])
        self.assertEqual(state["publish"]["pr"], "")
        self.assertIn("== publish error: " + error, output)
        self.assertEqual(marker.read_text(), "untouched\n")
        self.assertEqual(self.git("rev-parse", state["branch"]), initial)
        self.assertEqual((Path(state["worktree"]) / "clean.sh").read_text(), script)
        self.assertEqual((Path(state["worktree"]) / "tracked.txt").read_text(), "worker change\n")
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        worktree_events = [event for event in events if event["cwd"] == state["worktree"]]
        self.assertTrue(worktree_events)
        self.assertIn("config", worktree_events[0]["argv"])
        self.assertTrue(any("--get-regexp" in event["argv"] for event in worktree_events))
        self.assertFalse(any("status" in event["argv"] or "add" in event["argv"] or
                             "commit" in event["argv"] for event in worktree_events), worktree_events)
        self.assertFalse(self.gh_invocations())

    def test_workspace_filter_guard_allows_git_lfs_command_prefixes_without_running_lfs(self):
        self.configure(language="en", sandbox="workspace")
        for index, command in enumerate(("git-lfs clean -- %f", "git lfs filter-process")):
            with self.subTest(command=command):
                # No attributes select this driver: real Git LFS is never run.
                self.git("config", "filter.lfs.clean", command)
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {"tracked.txt": command + "\n"},
                    "reply": self.work_result(commit_message="Allow an inactive Git LFS driver")}])
                slug = "sandbox-lfs-filter-" + str(index)
                self.start(slug)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual(state["publish"]["error"], "")
                self.assertTrue(state["publish"]["pushed"])
                self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]),
                                 "Allow an inactive Git LFS driver")

    def test_workspace_filter_guard_reads_all_config_scopes_and_command_forms(self):
        self.env.update(GIT_CONFIG_SYSTEM=str(self.root / "git-system.conf"),
                        GIT_CONFIG_GLOBAL=str(self.root / "git-global.conf"))
        worker = self.worker()
        cases = (("clean", "/bin/sh clean.sh", "project.nested"),
                 ("smudge", "/bin/sh\nclean.sh", "project.nested"),
                 ("process", "git-lfs-custom process", "project.nested"),
                 ("clean", "", "project.nested"),
                 ("smudge", " git lfs smudge", "project.nested"),
                 ("process", "git-lfs filter-process", None),
                 ("clean", "git lfs clean -- %f", None),
                 ("smudge", "git-lfs smudge\n--skip", None))
        with mock.patch.dict(os.environ, self.env):
            self.assertIsNone(worker.unsafe_filter_driver(self.repo))
            for scope in ("--system", "--global", "--local"):
                for operation, command, expected in cases:
                    with self.subTest(scope=scope, operation=operation, command=command):
                        key = "filter.project.nested." + operation
                        self.git("config", scope, key, command)
                        try:
                            self.assertEqual(worker.unsafe_filter_driver(self.repo), expected)
                        finally:
                            self.git("config", scope, "--unset-all", key)

    def test_workspace_supervisor_git_commands_disable_fsmonitor_and_hooks(self):
        self.configure(language="en", sandbox="workspace")
        trace_path = self.record_git_calls()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "supervisor publication\n"},
            "reply": self.work_result(commit_message="Keep supervisor Git commands isolated")}])
        self.start("sandbox-git-safety")
        self.wait("sandbox-git-safety")
        self.cli("status", "sandbox-git-safety")
        self.assertTrue(self.state("sandbox-git-safety")["publish"]["pushed"])
        events = [json.loads(line) for line in trace_path.read_text().splitlines()]
        self.assertTrue(events)
        for event in events:
            argv = event["argv"]
            self.assertIn("core.hooksPath=/dev/null", argv, event)
            self.assertIn("core.fsmonitor=false", argv, event)
            for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=false"):
                self.assertEqual(argv[argv.index(setting) - 1], "-c", event)
        for command in ("config", "status", "add", "commit", "push"):
            self.assertTrue(any(command in event["argv"] for event in events), (command, events))

    def test_workspace_stopped_and_failed_turns_leave_changes_uncommitted(self):
        self.configure(language="en", sandbox="workspace")
        initial = self.git("rev-parse", "HEAD")
        for mode in ("failed", "hold"):
            with self.subTest(mode=mode):
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": mode, "write_files": {"tracked.txt": mode + " changes\n"},
                    "reply": self.work_result(commit_message="Do not commit interrupted work")}])
                slug = "sandbox-" + mode
                self.start(slug)
                worktree = Path(self.state(slug)["worktree"])
                self.until(lambda: (worktree / "tracked.txt").read_text() == mode + " changes\n")
                if mode == "hold":
                    self.cli("stop", slug, timeout=20)
                self.cli("wait", slug, "--timeout", "10")
                state = self.state(slug)
                self.assertEqual(state["outcome"], "stopped" if mode == "hold" else "failed")
                self.assertEqual(self.git("rev-parse", state["branch"]), initial)
                self.assertFalse(state.get("publish"))
                self.assertFalse(self.gh_invocations())

    def test_workspace_check_uses_codex_sandbox_and_keeps_the_saved_command_and_log(self):
        roots = [str(self.root / "cache")]
        self.configure(language="en", sandbox="workspace", writable_dirs=roots)
        command = "printf 'sandbox check marker\\n'; test -f tracked.txt"
        self.start("sandbox-check", "--check", command)
        output = self.wait("sandbox-check")
        state = self.state("sandbox-check")
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual(state["checks"][-1]["cmd"], command)
        events = self.sandbox_invocations()
        self.assertEqual(len(events), 1)
        self.assert_workspace_sandbox(events[0], roots)
        self.assertEqual(events[0]["argv"][-4:], ["--", "/bin/sh", "-c", command])
        self.assertEqual(events[0]["cwd"], state["worktree"])
        self.assertEqual(events[0]["home"], str(self.home))
        self.assertIn("sandbox check marker", (self.home / "claude-tasks" / "sandbox-check" / "check.log").read_text())
        self.assertIn("== check `" + command + "`: passed", output)

    def test_workspace_check_timeout_and_stop_kill_the_sandbox_process_group(self):
        self.configure(language="en", sandbox="workspace")
        command = 'sleep 60 & child=$!; echo "$child" > check-child.pid; wait "$child"'
        for outcome in ("timeout", "stopped"):
            with self.subTest(outcome=outcome):
                slug = "sandbox-check-" + outcome
                self.start(slug, "--check", command, "--check-timeout", "0.5" if outcome == "timeout" else "60")
                child = self.check_child(slug)
                if outcome == "stopped":
                    self.cli("stop", slug, timeout=20)
                self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["checks"][-1]["outcome"], outcome)
                if outcome == "timeout":
                    self.assertGreaterEqual(state["checks"][-1]["seconds"], 0.5)
                else:
                    self.assertEqual(state["outcome"], "stopped")
                self.assertFalse(state.get("check_pid"))
                event = self.sandbox_invocations()[-1]
                self.assert_workspace_sandbox(event)
                self.assertEqual(event["pid"], event["pgid"])
                self.until(lambda: not self.process_is_live(child))
                self.until(lambda: not self.process_is_live(event["pid"]))

    def test_workspace_push_failure_is_reported_without_failing_the_turn(self):
        self.configure(language="en", sandbox="workspace")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "write_files": {"tracked.txt": "unpublished change\n"},
             "reply": self.work_result(commit_message="Save work without an origin")}])
        self.start("sandbox-no-origin")
        self.wait("sandbox-no-origin")
        self.assertFalse(self.state("sandbox-no-origin").get("publish"))
        self.git("remote", "remove", "origin")
        self.cli("say", "sandbox-no-origin", "Save another change")
        output = self.wait("sandbox-no-origin")
        state = self.state("sandbox-no-origin")
        publish = state["publish"]
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(publish["sha"], self.git("rev-parse", state["branch"]))
        self.assertFalse(publish["pushed"])
        self.assertEqual(publish["pr"], "")
        self.assertIn("origin", publish["error"])
        self.assertIn("== publish error: " + publish["error"], output)
        self.assertFalse(self.gh_invocations())

    def test_workspace_pr_failure_is_reported_without_failing_the_turn(self):
        self.configure(language="en", sandbox="workspace")
        self.env["CODEX_FAKE_GH_CREATE_ERROR"] = "1"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "published change without PR\n"},
            "reply": self.work_result(commit_message="Publish before a PR service failure")}])
        self.start("sandbox-pr-error")
        output = self.wait("sandbox-pr-error")
        state = self.state("sandbox-pr-error")
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(state["publish"]["pushed"])
        self.assertEqual(state["publish"]["pr"], "")
        self.assertIn("fixture PR creation failed", state["publish"]["error"])
        self.assertIn("== publish error: " + state["publish"]["error"], output)

    def test_workspace_fix_round_commits_before_each_sandboxed_check(self):
        self.configure(language="en", sandbox="workspace")
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Fixed the reported defect."}
                     for finding in self.review_result()["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "initial implementation\n"},
             "reply": self.work_result(commit_message="Implement sandbox work")},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "write_files": {"tracked.txt": "corrected implementation\n"},
             "reply": self.work_result(commit_message="Fix sandbox review findings", findings=decisions)},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = 'test -z "$(git status --porcelain)" && git log -1 --format=%s'
        self.start("sandbox-fix", "--review", "--fix-rounds", "1", "--check", command)
        self.wait("sandbox-fix")
        state = self.state("sandbox-fix")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "passed")
        self.assertEqual(self.git("log", "-2", "--format=%s", state["branch"]).splitlines(),
                         ["Fix sandbox review findings", "Implement sandbox work"])
        self.assertEqual(state["publish"]["sha"], self.git("rev-parse", state["branch"]))
        self.assertTrue(state["publish"]["pushed"])
        self.assertEqual(len(self.sandbox_invocations()), 2)
        for event in self.sandbox_invocations() + self.invocations():
            self.assert_workspace_sandbox(event)
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False, True])
        self.assertTrue(self.invocations()[2]["resume"])
        self.assertEqual(len([event for event in self.gh_invocations()
                              if event["argv"][:2] == ["pr", "create"]]), 1)

    def test_time_budget_kills_exec_group_wraps_same_thread_checks_and_skips_review(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure(language="en")
        self.configure_notify()
        result = self.work_result("partial", summary="Saved the working checkpoint.",
                                  remaining=["Complete the remaining integration."])
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "spawn_child": True},
            {"mode": "complete", "reply": result, "write_files": {"wrapped.txt": "saved\n"}}])
        self.start("time-cap", "--max-minutes", "90", "--review", "--fix-rounds", "2",
                   "--check", "test -f wrapped.txt && printf 'checked\\n' > cap-check")
        self.until(lambda: any(event.get("command") == "exec-child" for event in self.trace()))
        child = next(event["child_pid"] for event in self.trace() if event.get("command") == "exec-child")
        self.extra_pids.add(child)
        output = self.wait("time-cap", timeout=15)
        state = self.state("time-cap")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], result)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual((Path(state["worktree"]) / "cap-check").read_text(), "checked\n")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assert_output_schema(executions[1], "time-cap")
        prompt = executions[1]["prompt"].lower()
        for word in ("time", "commit", "push", "partial", "remaining"):
            self.assertIn(word, prompt)
        self.assertNotRegex(executions[1]["prompt"], "[А-Яа-яЁё]")
        self.assertFalse(self.review_invocations())
        self.assertIn("== turn stopped: time budget 90 min exhausted", output)
        self.assertIn(result["summary"], output)
        self.assertIn(result["remaining"][0], output)
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(self.process_is_live(executions[0]["pid"]))
        self.assertTrue(any(event.get("command") == "signal" and
                            event.get("pid") == executions[0]["pid"] for event in self.trace()))
        self.assertEqual(self.notification("time-cap", "turn_finished")["outcome"], "budget_exhausted")
        self.assertEqual(self.notification("time-cap", "budget_exhausted")["outcome"], "budget_exhausted")

    def test_time_budget_interrupts_quota_refresh_with_a_locked_reset_journal(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure(language="en", use_reset_credits=True)
        gate = self.root / "publish-rollout"
        result = self.work_result("partial", summary="Wrapped up while the reset journal remained locked.")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "spawn_child": True, "rollout_used": 99, "rollout_gate": str(gate)},
            {"mode": "complete", "reply": result}])
        starter = subprocess.Popen(
            [PYTHON, "-I", str(SCRIPT), "start", "cap-locked-journal", "--brief", str(self.brief),
             "--repo", str(self.repo), "--max-minutes", "2"],
            cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.extra_pids.add(starter.pid)
        try:
            self.until(lambda: any(event.get("command") == "rollout-waiting" for event in self.trace()))
            # Preflight is complete. Contend only the journal opened by on_limit
            # while the first exec remains alive, then keep it locked through wrap-up.
            with (self.home / "codex-worker-resets.jsonl").open("a+") as journal:
                fcntl.flock(journal, fcntl.LOCK_EX)
                before = len(self.trace())
                gate.touch()
                stdout, stderr = starter.communicate(timeout=5)
                self.assertEqual(starter.returncode, 0, stdout + stderr)
                self.until(lambda: sum(event.get("command") == "quota-snapshot"
                                       for event in self.trace()[before:]) >= 2)
                child = next(event["child_pid"] for event in self.trace()
                             if event.get("command") == "exec-child")
                self.extra_pids.add(child)
                self.assertTrue(self.process_is_live(self.invocations()[0]["pid"]))
                output = self.wait("cap-locked-journal", timeout=10)
                state = self.state("cap-locked-journal")
                self.assertEqual(state["outcome"], "budget_exhausted")
                self.assertEqual(state["wrapup"]["outcome"], "completed")
                self.assertEqual(state["result"], result)
                executions = self.invocations()
                self.assertEqual(len(executions), 2)
                self.assertTrue(executions[1]["resume"])
                self.assertEqual(executions[1]["thread"], executions[0]["thread"])
                self.assertIn("time budget has expired", executions[1]["prompt"])
                self.assertIn("== turn stopped: time budget 2 min exhausted", output)
                self.assertIn(result["summary"], output)
                self.assertFalse(self.process_is_live(executions[0]["pid"]))
                self.until(lambda: not self.process_is_live(child))
                self.assertFalse(self.consume_calls())
        finally:
            if starter.poll() is None:
                starter.terminate()
            starter.communicate(timeout=5)

    def test_time_budget_interrupts_a_slow_account_rpc_after_a_usage_error(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure(language="en")
        gate = self.root / "release-quota-rpc"
        result = self.work_result("partial", summary="Wrapped up without waiting for the quota RPC.")
        fixture = self.account(self.home, "A", self.reset, runs=[
            {"mode": "usage_error", "limit_after": 0.05}, {"mode": "complete", "reply": result}])
        fixture["rate_limits_gate_after_exec"] = str(gate)
        (self.home / "fixture.json").write_text(json.dumps(fixture))
        self.start("cap-slow-quota-rpc", "--max-minutes", "2")
        self.until(lambda: any(event.get("command") == "quota-waiting" for event in self.trace()))
        blocked_rpc = next(event["pid"] for event in self.trace() if event.get("command") == "quota-waiting")
        self.assertTrue(self.process_is_live(blocked_rpc))
        output = self.wait("cap-slow-quota-rpc", timeout=10)
        state = self.state("cap-slow-quota-rpc")
        self.assertFalse(gate.exists(), "the deadline must cancel the RPC before its gate opens")
        self.assertFalse(self.process_is_live(blocked_rpc))
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], result)
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)
        self.assertIn(result["summary"], output)

    def test_time_budget_does_not_count_sleep_until_subscription_reset(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "usage_error", "limit_after": 0.1},
            {"mode": "complete", "delay": 0.6, "reply": "Finished after a long quota sleep"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("cap-reset-sleep", "--max-minutes", "2.5")
        output = self.wait("cap-reset-sleep", timeout=12, through_sleep=True)
        state = self.state("cap-reset-sleep")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual([entry["event"] for entry in state["rotations"]], ["sleep", "wake"])
        self.assertIn("Finished after a long quota sleep", output)
        self.assertNotIn("time budget", output)

    def test_time_budget_accumulates_exec_time_across_account_rotation(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error", "limit_after": 0.9}])
        self.account(self.other, "B", self.reset, runs=[
            {"mode": "complete", "delay": 1.1, "reply": "This would finish if the budget restarted"},
            {"mode": "complete", "reply": self.work_result("partial")}])
        self.start("cap-rotation", "--max-minutes", "1.7")
        self.wait("cap-rotation", timeout=12)
        executions = self.invocations()
        self.assertEqual(self.state("cap-rotation")["outcome"], "budget_exhausted")
        self.assertEqual(len(executions), 3)
        self.assertEqual(executions[1]["home"], str(self.other))
        self.assertFalse(executions[1]["resume"])
        self.assertTrue(executions[2]["resume"])
        self.assertEqual(executions[2]["thread"], executions[1]["thread"])

    def test_wrap_up_has_its_own_ten_minute_cap_without_recursing(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "hold", "spawn_child": True}])
        self.start("cap-wrap-timeout", "--max-minutes", "10", "--check", "printf 'checked\\n' > checked")
        output = self.wait("cap-wrap-timeout", timeout=12)
        state = self.state("cap-wrap-timeout")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(len(self.invocations()), 2, "a timed-out wrap-up must not start another wrap-up")
        self.assertIsNone(state["result"])
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== turn stopped: time budget 10 min exhausted", output)
        child = next(event["child_pid"] for event in self.trace() if event.get("command") == "exec-child")
        self.extra_pids.add(child)
        self.until(lambda: not self.process_is_live(child))
        self.assertTrue(all(not self.process_is_live(event["pid"]) for event in self.invocations()))

    def test_repeated_failure_during_wrap_up_stops_without_another_wrap_up(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure()
        command = {"command": "check-wrap", "exit_code": 2, "aggregated_output": "same wrap failure"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "hold", "commands": [command] * 3}])
        self.start("cap-wrap-stuck", "--max-minutes", "10")
        output = self.wait("cap-wrap-stuck", timeout=12)
        state = self.state("cap-wrap-stuck")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["wrapup"]["outcome"], "stuck")
        self.assertIsNone(state["result"])
        self.assertEqual(len(self.invocations()), 2)
        self.assertIn("== turn stopped: time budget 10 min exhausted", output)

    def test_repeated_command_failure_uses_output_tail_and_survives_other_commands(self):
        self.configure(language="en")
        self.configure_notify()
        command = "python3 check_widget.py --empty-input"
        tail = "same error tail\n" + "x" * 2048
        failures = [{"command": command, "exit_code": 7, "aggregated_output": prefix + tail}
                    for prefix in ("first attempt\n", "second attempt\n", "third attempt\n")]
        events = [failures[0], {"command": "git status", "exit_code": 0}, failures[1],
                  {"command": "another-command", "exit_code": 9}, failures[2]]
        result = self.work_result("blocked", summary="Stopped after three identical failures.",
                                  remaining=["Resolve the fixture command failure."])
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": events}, {"mode": "complete", "reply": result}])
        self.start("repeat-cap", "--review", "--check", "printf 'checked\\n' > checked")
        output = self.wait("repeat-cap", timeout=12)
        state = self.state("repeat-cap")
        executions = self.invocations()
        self.assertEqual(state["outcome"], "stuck")
        self.assertEqual(state["result"], result)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        prompt = executions[1]["prompt"]
        self.assertIn(command, prompt)
        for word in ("blocked", "3", "7"):
            self.assertIn(word, prompt)
        self.assertNotRegex(prompt, "[А-Яа-яЁё]")
        self.assertIn("== turn stopped: repeated failure ×3: " + command, output)
        self.assertIn(result["summary"], output)
        self.assertFalse(self.review_invocations())
        self.assertEqual(self.notification("repeat-cap", "stuck")["outcome"], "stuck")
        self.assertEqual(self.notification("repeat-cap", "turn_finished")["outcome"], "stuck")

    def test_different_failures_success_and_other_event_types_do_not_trigger_stuck(self):
        self.configure()
        failure = {"command": "check-widget", "exit_code": 1, "aggregated_output": "error A"}
        cases = {
            "commands": [dict(failure, command="check-%s" % number) for number in range(3)],
            "output": [dict(failure, aggregated_output="error %s" % number) for number in (1, 2, 1, 2, 1)],
            "exit": [dict(failure, exit_code=number) for number in (1, 2, 1, 2, 1)],
            "success": [failure, failure, dict(failure, exit_code=0), failure, failure],
            "changed-between": [failure, dict(failure, aggregated_output="error B"), failure],
            "unfinished": [dict(failure, event_type="item.started") for _ in range(3)],
            "other-item": [dict(failure, item_type="other_execution") for _ in range(3)]}
        for name, commands in cases.items():
            with self.subTest(case=name):
                (self.home / "exec-count").unlink(missing_ok=True)
                before = len(self.invocations())
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "commands": commands, "reply": "No loop detected"}])
                slug = "repeat-distinct-" + name
                self.start(slug)
                output = self.wait(slug)
                self.assertEqual(self.state(slug)["outcome"], "completed")
                self.assertEqual(len(self.invocations()) - before, 1)
                self.assertIn("No loop detected", output)

    def test_repeat_limit_and_russian_wrap_prompt_follow_settings(self):
        self.configure(repeat_limit=2)
        command = {"command": "pytest tests/widget.py", "exit_code": 2,
                   "aggregated_output": "missing fixture"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": [command, command]},
            {"mode": "complete", "reply": self.work_result("blocked")}])
        self.start("repeat-setting")
        output = self.wait("repeat-setting", timeout=12)
        self.assertEqual(self.state("repeat-setting")["outcome"], "stuck")
        self.assertIn("== turn stopped: repeated failure ×2: " + command["command"], output)
        prompt = self.invocations()[-1]["prompt"]
        self.assertRegex(prompt, "[А-Яа-яЁё]")
        self.assertIn(command["command"], prompt)
        self.assertIn("blocked", prompt)

    def test_max_minutes_uses_config_default_and_say_override_for_one_turn(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure(language="en", max_turn_minutes=18)
        legacy = self.home / "claude-accounts.json"
        (self.home / "codex-worker.json").write_text(legacy.read_text())
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "complete", "reply": self.work_result("partial")},
            {"mode": "complete", "delay": 1.2, "reply": "Say override completed"},
            {"mode": "hold"}, {"mode": "complete", "reply": self.work_result("partial")}])
        self.start("cap-config")
        output = self.wait("cap-config", timeout=12)
        self.assertEqual(self.state("cap-config")["outcome"], "budget_exhausted")
        self.assertIn("== turn stopped: time budget 18 min exhausted", output)
        self.cli("say", "cap-config", "Use a larger budget", "--max-minutes", "48")
        self.assertIn("Say override completed", self.wait("cap-config", timeout=12))
        self.assertEqual(self.state("cap-config")["outcome"], "completed")
        self.cli("say", "cap-config", "Use the configured default again")
        self.wait("cap-config", timeout=12)
        self.assertEqual(self.state("cap-config")["outcome"], "budget_exhausted")
        self.assertEqual(len(self.invocations()), 5)

    def test_start_max_minutes_does_not_limit_later_say_without_a_default(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "complete", "reply": self.work_result("partial")},
            {"mode": "complete", "delay": 1.2, "reply": "The later turn has no budget"}])
        self.start("cap-no-default", "--max-minutes", "12")
        self.wait("cap-no-default", timeout=12)
        self.cli("say", "cap-no-default", "Continue without a time limit")
        output = self.wait("cap-no-default", timeout=12)
        self.assertEqual(self.state("cap-no-default")["outcome"], "completed")
        self.assertIn("The later turn has no budget", output)
        self.assertEqual(len(self.invocations()), 3)

    def test_max_minutes_rejects_nonpositive_and_nonfinite_values_before_start_or_say(self):
        self.configure()
        for value in ("0", "-1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(command="start", value=value):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "start", "cap-invalid",
                                         "--brief", str(self.brief), "--repo", str(self.repo),
                                         "--max-minutes=" + value], cwd=self.repo, env=self.env,
                                        text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-minutes", result.stderr)
                self.assertFalse((self.home / "claude-tasks" / "cap-invalid").exists())
        self.assertFalse(self.invocations())
        self.start("cap-valid-say")
        self.wait("cap-valid-say")
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(command="say", value=value):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "say", "cap-valid-say", "Continue",
                                         "--max-minutes=" + value], cwd=self.repo, env=self.env,
                                        text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-minutes", result.stderr)
        self.assertEqual(len(self.invocations()), 1)

    def test_review_and_scout_default_to_their_own_thirty_minute_budget(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure(language="en", max_turn_minutes=1000)
        work = self.work_result()
        checkpoint = self.review_result()
        checkpoint["summary"] = "Review incomplete: found two defects before the time budget expired."
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": work},
            {"mode": "hold"}, {"mode": "complete", "reply": checkpoint},
            {"mode": "hold"}, {"mode": "complete", "reply": "Scouting checkpoint"}])
        self.start("cap-review-default", "--review")
        review_output = self.wait("cap-review-default", timeout=12)
        state = self.state("cap-review-default")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], work, "review wrap-up must preserve the work result")
        self.assertEqual(state["wrapup"]["result"], checkpoint)
        self.assertEqual(state["reviews"][-1]["result"], checkpoint)
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        self.assertTrue(executions[1]["review"])
        self.assertTrue(executions[2]["resume"])
        self.assertEqual(executions[2]["thread"], executions[1]["thread"])
        self.assert_output_schema(executions[2], "cap-review-default", review=True)
        self.assertIn(checkpoint["summary"], review_output)
        self.assertIn("== turn stopped: time budget 30 min exhausted", review_output)
        self.cli("scout", "cap-scout-default", "Inspect the current configuration")
        scout_output = self.wait("cap-scout-default", timeout=12)
        self.assertEqual(self.state("cap-scout-default")["outcome"], "budget_exhausted")
        executions = self.invocations()
        self.assertEqual(len(executions), 5)
        self.assertEqual(executions[4]["thread"], executions[3]["thread"])
        self.assert_scout_read_only(executions[4])
        self.assertIn("Scouting checkpoint", scout_output)
        self.assertIn("== turn stopped: time budget 30 min exhausted", scout_output)

    def test_review_budget_before_rotated_thread_started_does_not_resume_previous_review(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure(language="en", max_review_minutes=2)
        work = self.work_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": work},
            {"mode": "usage_error", "limit_after": 0.05}])
        gate = self.root / "release-rotated-review-before-thread"
        self.account(self.other, "B", self.reset, runs=[
            {"mode": "hold", "rollout_gate": str(gate)},
            {"mode": "complete", "reply": self.review_result()}])
        self.start("cap-review-before-thread", "--review")
        self.until(lambda: any(event.get("command") == "rollout-waiting"
                               and event["home"] == str(self.other) for event in self.trace()))
        output = self.wait("cap-review-before-thread", timeout=12)
        state = self.state("cap-review-before-thread")
        executions = self.invocations()
        self.assertFalse(gate.exists(), "the replacement review must never publish thread.started")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["wrapup"]["outcome"], "not_run")
        self.assertIsNone(state["wrapup"]["result"])
        self.assertEqual(state["result"], work)
        self.assertEqual(state["review_account"], "second")
        self.assertEqual(len(executions), 3, "no wrap-up may resume a previous account's review")
        self.assertEqual([event["review"] for event in executions], [False, True, True])
        self.assertFalse(any(event["resume"] for event in executions))
        self.assertEqual(executions[-1]["home"], str(self.other))
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(state["reviews"][0]["thread"], executions[1]["thread"])
        self.assertNotEqual(state["reviews"][0]["thread"], executions[2]["thread"])
        self.assertTrue(all(not self.process_is_live(event["pid"]) for event in executions))
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)

    def test_wrap_up_prompts_use_the_schema_for_work_or_review_in_both_languages(self):
        worker = self.worker()
        reasons = [
            {"outcome": "budget_exhausted", "minutes": 30},
            {"outcome": "stuck", "command": "fixture-check", "count": 3,
             "exit_code": 7, "output_sha256": "a" * 64}]
        for language in ("en", "ru"):
            for reason in reasons:
                with self.subTest(language=language, reason=reason["outcome"]):
                    cfg = {"language": language}
                    work = worker.wrapup_prompt({"kind": "work"}, reason, cfg)
                    review = worker.wrapup_prompt({"kind": "review"}, reason, cfg)
                    self.assertIn("work result schema" if language == "en" else "схеме работы", work)
                    self.assertIn("partial" if reason["outcome"] == "budget_exhausted" else "blocked", work)
                    if reason["outcome"] == "budget_exhausted":
                        self.assertIn("Commit what works and push" if language == "en" else
                                      "Закоммить то, что работает, и запушь", work)
                    self.assertIn("review schema" if language == "en" else "схеме ревью", review)
                    for field in ("JSON", "findings", "verdict", "defects", "summary"):
                        self.assertIn(field, review)
                    self.assertIn("incomplete" if language == "en" else "не завершено", review)
                    self.assertNotIn("work result schema" if language == "en" else "схеме работы", review)
                    self.assertNotIn("status partial", review)
                    self.assertNotIn("status blocked", review)

    def test_configured_review_budget_is_independent_of_work_and_applies_to_scout(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure(language="en", max_turn_minutes=0.2, max_review_minutes=3)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "delay": 0.8, "reply": self.review_result(False)},
            {"mode": "complete", "delay": 0.8, "reply": "Longer scouting completed"}])
        self.start("cap-review-independent", "--max-minutes", "0.6", "--review")
        self.wait("cap-review-independent", timeout=12)
        self.assertEqual(self.state("cap-review-independent")["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        self.cli("scout", "cap-scout-independent", "Inspect configuration independently")
        output = self.wait("cap-scout-independent", timeout=12)
        self.assertEqual(self.state("cap-scout-independent")["outcome"], "completed")
        self.assertIn("Longer scouting completed", output)
        self.assertEqual(len(self.invocations()), 3)

    def test_cap_notifications_obey_notify_events_filter(self):
        self.configure()
        self.configure_notify(events=["turn_finished"])
        command = {"command": "check-loop", "exit_code": 1, "aggregated_output": "same failure"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": [command] * 3},
            {"mode": "complete", "reply": self.work_result("blocked")}])
        self.start("cap-notify-filter")
        self.wait("cap-notify-filter", timeout=12)
        self.assertEqual(self.notification("cap-notify-filter", "turn_finished")["outcome"], "stuck")
        self.assertEqual([event["event"] for event in self.notifications("cap-notify-filter")], ["turn_finished"])

    def test_cap_settings_validate_values_and_keep_safe_defaults(self):
        worker = self.worker()
        config = worker.accounts_config()
        self.assertIsNone(config["max_turn_minutes"])
        self.assertEqual(config["max_review_minutes"], 30)
        self.assertEqual(config["repeat_limit"], 3)
        path = self.home / "codex-worker.json"
        invalid = {"max_turn_minutes": (0, -1, True, "10", float("inf"), float("nan")),
                   "max_review_minutes": (None, 0, -1, False, "30", float("inf")),
                   "repeat_limit": (0, -1, True, 1.5, "3")}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    path.write_text(json.dumps({key: value}))
                    with self.assertRaises(SystemExit) as raised:
                        worker.accounts_config()
                    self.assertIn(key, str(raised.exception))

    def test_budget_during_automatic_fix_runs_check_then_stops_the_cycle(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.05"
        self.configure(language="en")
        result = self.work_result("partial", summary="Saved the bounded correction.")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "hold"},
            {"mode": "complete", "reply": result}])
        self.start("cap-during-fix", "--max-minutes", "10", "--review", "--fix-rounds", "2",
                   "--check", "printf 'checked\\n' >> check-runs")
        output = self.wait("cap-during-fix", timeout=12)
        state = self.state("cap-during-fix")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], result)
        self.assertEqual(len(state["checks"]), 2)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual(len(self.invocations()), 4)
        self.assertIn(result["summary"], output)

    def test_typed_work_result_prints_summary_checks_pr_and_persists_for_status(self):
        self.configure()
        self.configure_notify()
        result = self.work_result()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": result}])
        self.start("typed-done")
        output = self.wait("typed-done")
        self.assert_output_schema(self.invocations()[0], "typed-done")
        self.assertEqual(self.state("typed-done")["result"], result)
        self.assertRegex(output, r"min\n== result: done\n")
        for text in (result["summary"], result["checks"][0]["command"], "passed",
                     result["checks"][0]["details"], result["pr"]):
            self.assertIn(text, output)
        self.assertNotIn(json.dumps(result, ensure_ascii=False), output)
        status = self.cli("status", "typed-done")
        self.assertIn("done", status)
        self.assertIn(result["summary"], status)
        event = self.notification("typed-done", "turn_finished")
        self.assertEqual(event["result"], result)
        self.assertIn("done", event["message"])

    def test_incomplete_results_skip_automatic_review_but_run_checks_and_request_input(self):
        self.configure()
        self.configure_notify()
        for status, label in (("needs_decision", "NEEDS DECISION"), ("blocked", "BLOCKED"),
                              ("partial", "PARTIAL")):
            with self.subTest(status=status):
                (self.home / "exec-count").unlink(missing_ok=True)
                questions = ["Which existing API should receive the result?", "Keep the legacy spelling?"]
                result = self.work_result(status, questions=questions, remaining=["Connect the selected API."],
                                          checks=[{"command": "fixture check", "outcome": "not_run",
                                                   "details": "0 runs while awaiting a decision"}])
                self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": result}])
                slug = "typed-" + status.replace("_", "-")
                before = len(self.invocations())
                self.start(slug, "--review", "--check", "printf 'check complete\\n' > check-ran")
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(len(self.invocations()) - before, 1)
                self.assert_output_schema(self.invocations()[-1], slug)
                self.assertEqual(state["result"], result)
                self.assertEqual(state["review_skipped"], "result " + status)
                self.assertEqual(len(state["checks"]), 1)
                self.assertEqual((Path(state["worktree"]) / "check-ran").read_text(), "check complete\n")
                self.assertIn("== result: " + label, output)
                self.assertIn("== review skipped: result " + status, output)
                for number, question in enumerate(questions, 1):
                    self.assertIn("%s. %s" % (number, question), output)
                self.assertIn(result["remaining"][0], output)
                self.assertEqual(self.notification(slug, "turn_finished")["result"], result)
                if status in ("blocked", "needs_decision"):
                    event = self.notification(slug, "needs_input")
                    self.assertEqual(event["result"], result)
                    self.assertEqual(event["question"], questions[0])
                else:
                    self.assertFalse(any(event["event"] == "needs_input" for event in self.notifications(slug)))

    def test_unstructured_and_invalid_work_results_keep_legacy_automatic_review(self):
        self.configure()
        self.configure_notify()
        invalid_results = ["Legacy final message without JSON", '{"status": "done"',
                           self.work_result(status="finished"), self.work_result(extra="unexpected"),
                           self.work_result(commit_message=None), self.work_result(commit_message=["subject"]),
                           {key: value for key, value in self.work_result().items() if key != "commit_message"},
                           self.work_result(checks=[{"command": "check", "outcome": "passed"}]),
                           self.work_result(checks=[{"command": "check", "outcome": "passed",
                                                    "details": "1 passed", "extra": "unexpected"}]),
                           self.work_result(findings=[{"id": "F1", "decision": "ignored", "note": "No change"}]),
                           self.work_result(findings=[{"id": "F1", "decision": "fixed"}]),
                           self.work_result(findings=[{"id": "F1", "decision": "fixed", "note": "Done",
                                                       "extra": "unexpected"}]),
                           self.work_result(questions="Choose the API")]
        for number, result in enumerate(invalid_results):
            with self.subTest(result=result):
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": result},
                    {"mode": "complete", "reply": "Legacy review remains readable"}])
                slug = "typed-invalid-%s" % number
                self.start(slug, "--review")
                output = self.wait(slug)
                state = self.state(slug)
                self.assertIsNone(state["result"])
                self.assertEqual(len(state["reviews"]), 1)
                self.assertIsNone(state["reviews"][0]["result"])
                self.assertNotIn("review_skipped", state)
                self.assertIn("== result: unknown (unstructured final message)", output)
                raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                self.assertIn(raw, output)
                self.assertIn("Legacy review remains readable", output)
                self.assertIsNone(self.notification(slug, "turn_finished")["result"])

    def test_typed_reviews_print_findings_and_clean_verdict_and_notify_counts(self):
        self.configure()
        self.configure_notify()
        for defects in (True, False):
            with self.subTest(defects=defects):
                (self.home / "exec-count").unlink(missing_ok=True)
                result, review = self.work_result(), self.review_result(defects)
                self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": result},
                    {"mode": "complete", "reply": review}])
                slug = "typed-review-" + ("defects" if defects else "clean")
                self.start(slug, "--review")
                output = self.wait(slug)
                self.assert_output_schema(self.review_invocations()[-1], slug, review=True)
                state = self.state(slug)
                self.assertEqual(state["result"], result)
                self.assertEqual(state["reviews"][-1]["result"], review)
                expected = "2 defects" if defects else "no defects"
                self.assertRegex(output, r"== review by main \(\d+(?:\.\d+)? min\): " + expected)
                self.assertIn(review["summary"], output)
                for finding in review["findings"]:
                    self.assertIn("[{severity}] {id} {file}:{line} — {title}".format(**finding), output)
                    self.assertIn(finding["detail"], output)
                    self.assertIn(finding["verify"], output)
                event = self.notification(slug, "review_done")
                self.assertEqual(event["verdict"], review["verdict"])
                self.assertEqual(event["findings"], len(review["findings"]))

    def test_invalid_standalone_review_keeps_raw_text_and_the_working_result(self):
        self.configure()
        self.configure_notify()
        result, review = self.work_result(), self.review_result()
        review["findings"][0]["line"] = True  # A JSON boolean is not a schema integer.
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": result},
            {"mode": "complete", "reply": review}])
        self.start("typed-invalid-review")
        self.wait("typed-invalid-review")
        self.cli("review", "typed-invalid-review")
        output = self.wait("typed-invalid-review")
        state = self.state("typed-invalid-review")
        self.assertEqual(state["result"], result)
        self.assertIsNone(state["reviews"][-1]["result"])
        self.assert_output_schema(self.review_invocations()[-1], "typed-invalid-review", review=True)
        self.assertIn(json.dumps(review, ensure_ascii=False), output)
        event = self.notification("typed-invalid-review", "review_done")
        self.assertIsNone(event["verdict"])
        self.assertEqual(event["findings"], 0)

    def test_typed_say_resumes_with_schema_and_handoff_uses_summary_and_remaining(self):
        self.configure()
        first = self.work_result("partial", summary="Implemented the parser.",
                                 remaining=["Add the empty-input case."])
        second = self.work_result("partial", summary="Covered the empty-input case.",
                                  remaining=["Document the output contract."])
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": first},
            {"mode": "complete", "reply": second}])
        self.start("typed-handoff")
        self.wait("typed-handoff")
        self.cli("say", "typed-handoff", "Continue with the input case.")
        self.wait("typed-handoff")
        executions = self.invocations()
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        for execution in executions:
            self.assert_output_schema(execution, "typed-handoff")
        self.account(self.home, "A", self.reset, used=99)
        self.cli("say", "typed-handoff", "Finish the documentation.")
        self.wait("typed-handoff")
        rotated = self.invocations()[-1]
        self.assertEqual(rotated["home"], str(self.other))
        self.assertFalse(rotated["resume"])
        self.assert_output_schema(rotated, "typed-handoff")
        self.assertIn(second["summary"], rotated["prompt"])
        self.assertIn(second["remaining"][0], rotated["prompt"])
        self.assertNotIn(json.dumps(second, ensure_ascii=False), rotated["prompt"])
        self.assertNotIn('"checks":', rotated["prompt"])

    def assert_scout_output(self, output):
        for section in ("commits on", "== uncommitted", "== check `", "== review"):
            self.assertNotIn(section, output)

    def test_scout_reads_current_checkout_with_english_rules_and_prints_brief(self):
        (self.home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
        (self.home / "codex-worker-rules.md").write_text("TASK-RULES-MUST-NOT-REACH-SCOUT\n")
        question = "Where does tracked.txt enter the application?"
        reply = "Verified: tracked.txt:1 is fixture input. Tests: tests/test_rotation.py:1."
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": reply}])
        (self.repo / "tracked.txt").write_text("Current uncommitted checkout contents\n")
        (self.repo / "pending.txt").write_text("Keep this untracked file\n")
        branches = self.git("branch", "--format=%(refname)")
        worktrees = self.git("worktree", "list", "--porcelain")

        self.cli("scout", "scout-current", question, "--effort", "high")
        output = self.wait("scout-current")
        state = self.state("scout-current")
        invocation = self.invocations()[0]
        self.assertEqual(state["kind"], "scout")
        self.assertEqual(state["worktree"], str(self.repo))
        self.assertEqual(state["effort"], "high")
        self.assertEqual(invocation["cwd"], str(self.repo))
        self.assert_scout_read_only(invocation)
        self.assertIn('model_reasoning_effort="high"', invocation["argv"])
        prompt = invocation["prompt"]
        self.assertIn(question, prompt)
        self.assertNotIn("TASK-RULES-MUST-NOT-REACH-SCOUT", prompt)
        self.assertNotRegex(prompt, "[А-Яа-яЁё]")
        lower = prompt.lower()
        self.assertRegex(lower, r"(?:do not|never|don't)[^\n]*(?:change|edit|modify)")
        for rule in ("commit", "push", "300", "file:line", "test", "assum"):
            self.assertIn(rule, lower)
        self.assertRegex(lower, r"verified|checked|confirmed")
        self.assertRegex(lower, r"introduction|preamble")
        self.assertRegex(lower, r"not (?:inspect|read|look)|haven't (?:inspect|read|look)|did not (?:inspect|read|look)")
        self.assertIn(reply, output)
        self.assert_scout_output(output)
        self.assertEqual(self.git("branch", "--format=%(refname)"), branches)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.git("branch", "--list", "codex/scout-current"), "")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "Current uncommitted checkout contents\n")
        self.assertTrue((self.repo / "pending.txt").is_file())
        for status in (self.cli("status"), self.cli("status", "scout-current")):
            self.assertIn("scout", status)
            self.assertIn(str(self.repo), status)
            self.assertNotIn("codex/scout-current", status)

    def test_scout_accepts_question_before_or_after_options(self):
        question = "Where is the configuration loaded?"
        repo_args = ("--repo", str(self.repo))
        cases = ((question,) + repo_args,
                 repo_args + (question,),
                 ("--effort", "low", question) + repo_args)
        for index, args in enumerate(cases):
            with self.subTest(args=args):
                slug = "scout-args-%s" % index
                self.cli("scout", slug, *args)
                self.wait(slug)
                invocation = self.invocations()[-1]
                self.assertIn(question, invocation["prompt"])
                self.assertEqual(invocation["cwd"], str(self.repo))
                self.assertEqual(self.state(slug)["worktree"], str(self.repo))
                self.assert_scout_read_only(invocation)
                if "--effort" in args:
                    self.assertIn('model_reasoning_effort="low"', invocation["argv"])

    def test_scout_requires_exactly_one_question_source_before_creating_a_task(self):
        question_file = self.root / "scout-question.md"
        question_file.write_text("Where is the configuration loaded?\n")
        conflict = "argument --file: not allowed with argument question"
        cases = ((("--repo", str(self.repo)), "one of the arguments question --file is required"),
                 (("Where is the configuration loaded?", "--file", str(question_file)), conflict),
                 (("--file", str(question_file), "Where is the configuration loaded?"), conflict))
        for index, (args, expected_error) in enumerate(cases):
            with self.subTest(args=args):
                slug = "scout-invalid-args-%s" % index
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "scout", slug] + list(args),
                                        cwd=self.repo, env=self.env, text=True, capture_output=True,
                                        timeout=15)
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(self.invocations(), [])
                self.assertFalse((self.home / "claude-tasks" / slug).exists())

    def test_say_accepts_positional_and_file_messages_on_the_existing_thread(self):
        self.start("say-question-sources")
        self.wait("say-question-sources")
        thread = self.state("say-question-sources")["thread"]
        message = self.root / "followup.md"
        message.write_text("Continue with the file-supplied followup.\n")
        cases = (("Continue with the positional followup.",), ("--file", str(message)))
        for args in cases:
            with self.subTest(args=args):
                self.cli("say", "say-question-sources", *args)
                self.wait("say-question-sources")
                invocation = self.invocations()[-1]
                self.assertTrue(invocation["resume"])
                self.assertEqual(invocation["thread"], thread)
                expected = message.read_text().strip() if args[0] == "--file" else args[0]
                self.assertIn(expected, invocation["prompt"])
                self.assertEqual(self.state("say-question-sources")["kind"], "work")
        self.assertEqual(len(self.invocations()), 3)

    def test_scout_file_keeps_another_tasks_worktree_and_uses_russian_rules(self):
        self.configure(language="ru")
        self.start("scout-owner")
        self.wait("scout-owner")
        target = Path(self.state("scout-owner")["worktree"])
        (target / "tracked.txt").write_text("Uncommitted work in the other task\n")
        question = self.root / "question.md"
        question.write_text("Как читается tracked.txt в текущем каталоге?\n")
        (self.home / "codex-worker-rules.md").write_text("SCOUT-MUST-SKIP-CUSTOM-TASK-RULES\n")
        branches = self.git("branch", "--format=%(refname)")
        worktrees = self.git("worktree", "list", "--porcelain")

        self.cli("scout", "scout-file", "--file", str(question), "--repo", str(target))
        self.assert_scout_output(self.wait("scout-file"))
        state = self.state("scout-file")
        invocation = self.invocations()[-1]
        self.assertEqual(state["kind"], "scout")
        self.assertEqual(state["worktree"], str(target))
        self.assertEqual(invocation["cwd"], str(target))
        self.assert_scout_read_only(invocation)
        prompt = invocation["prompt"]
        self.assertIn(question.read_text().strip(), prompt)
        self.assertNotIn("SCOUT-MUST-SKIP-CUSTOM-TASK-RULES", prompt)
        for rule in ("300", "файл:строка", "провер", "предполож", "тест"):
            self.assertIn(rule, prompt.lower())
        self.assertEqual(self.git("branch", "--format=%(refname)"), branches)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.git("branch", "--list", "codex/scout-file"), "")
        self.cli("clean", "scout-file")
        self.assertTrue((self.home / "claude-tasks" / "scout-owner").is_dir())
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "tracked.txt").read_text(), "Uncommitted work in the other task\n")
        self.assertEqual(self.git("branch", "--format=%(refname)"), branches)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertFalse((self.home / "claude-tasks" / "scout-file").exists())
        self.assertIn(state["thread"], [event.get("thread") for event in self.trace()
                                       if event.get("command") == "archive"])

    def test_scout_say_resumes_the_same_read_only_thread(self):
        self.configure()
        self.cli("scout", "scout-say", "Find the input reader")
        self.wait("scout-say")
        thread = self.state("scout-say")["thread"]
        self.cli("say", "scout-say", "Which tests cover its empty-input case?", "--effort", "low")
        self.assert_scout_output(self.wait("scout-say"))
        first, followup = self.invocations()
        self.assertFalse(first["resume"])
        self.assertTrue(followup["resume"])
        self.assertEqual(followup["thread"], thread)
        self.assertIn("Which tests cover its empty-input case?", followup["prompt"])
        self.assertIn('model_reasoning_effort="low"', followup["argv"])
        self.assertEqual(self.state("scout-say")["kind"], "scout")
        for invocation in (first, followup):
            self.assert_scout_read_only(invocation)

    def test_scout_and_working_task_can_start_in_either_order_without_parallel(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("work-before-scout")
        self.cli("scout", "scout-after-work", "Read the active task's context")
        self.assertTrue(self.process_is_live(self.state("work-before-scout")["pid"]))
        self.assertTrue(self.process_is_live(self.state("scout-after-work")["pid"]))
        self.cli("stop", "work-before-scout", timeout=20)
        self.wait("work-before-scout")
        self.start("work-after-scout")
        self.assertTrue(self.process_is_live(self.state("scout-after-work")["pid"]))
        self.assertTrue(self.process_is_live(self.state("work-after-scout")["pid"]))
        self.cli("stop", "work-after-scout", timeout=20)
        self.wait("work-after-scout")
        self.cli("stop", "scout-after-work", timeout=20)
        self.assert_scout_output(self.wait("scout-after-work"))

    def test_scout_rejects_checks_and_reviews_without_starting_a_turn(self):
        self.configure()
        self.cli("scout", "scout-no-writes", "Find the parser")
        self.wait("scout-no-writes")
        state = self.state("scout-no-writes")
        count = len(self.invocations())
        for args in (("say", "scout-no-writes", "Run tests", "--check", "touch forbidden-check"),
                     ("say", "scout-no-writes", "Set timeout", "--check-timeout", "5"),
                     ("say", "scout-no-writes", "Review it", "--review"),
                     ("say", "scout-no-writes", "Disable fixes", "--fix-rounds", "0"),
                     ("say", "scout-no-writes", "Fix review", "--review", "--fix-rounds", "1"),
                     ("review", "scout-no-writes")):
            with self.subTest(args=args):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args), cwd=self.repo,
                                        env=self.env, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("scout", (result.stdout + result.stderr).lower())
                self.assertEqual(len(self.invocations()), count)
                self.assertEqual(self.state("scout-no-writes"), state)
        self.assertFalse((self.home / "claude-tasks" / "scout-no-writes" / "messages.jsonl").exists())
        self.assertFalse((self.repo / "forbidden-check").exists())

    def test_scout_rotation_hands_off_only_question_and_conversation_and_clean_archives(self):
        self.configure()
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "OLD-SCOUT-OUTPUT-OUTSIDE-LAST-THREE"},
            {"mode": "complete", "reply": "Earlier followup result"},
            {"mode": "usage_error", "messages": ["Scout finding one", "Scout finding two", "Scout finding three"]}])
        question = "Which function validates the input before dispatch?"
        first_followup = "Trace the empty input path"
        second_followup = "Confirm whether all callers use that validation"
        branches = self.git("branch", "--format=%(refname)")
        worktrees = self.git("worktree", "list", "--porcelain")
        self.cli("scout", "scout-rotate", question)
        self.wait("scout-rotate")
        self.cli("say", "scout-rotate", first_followup)
        self.wait("scout-rotate")
        self.cli("say", "scout-rotate", second_followup)
        output = self.wait("scout-rotate")
        prompt = self.assert_rotation("scout-rotate", output)
        self.assert_scout_output(output)
        self.assertIn(question, prompt)
        self.assertIn(first_followup, prompt)
        self.assertIn(second_followup, prompt)
        self.assertLess(prompt.index(first_followup), prompt.index(second_followup))
        for message in ("Scout finding one", "Scout finding two", "Scout finding three"):
            self.assertIn(message, prompt)
        self.assertNotIn("OLD-SCOUT-OUTPUT-OUTSIDE-LAST-THREE", prompt)
        for excluded in ("git status", "git diff", "git log", "worktree", "commits (", "коммиты ("):
            self.assertNotIn(excluded, prompt.lower())
        for invocation in self.invocations():
            self.assert_scout_read_only(invocation)
            self.assertEqual(invocation["cwd"], str(self.repo))
        for event in (self.notification("scout-rotate", "turn_finished"),
                      self.notification("scout-rotate", "rotated")):
            self.assertEqual(event["kind"], "scout")
        state = self.state("scout-rotate")
        self.assertEqual(state["kind"], "scout")
        self.cli("clean", "scout-rotate")
        archived = [(event["home"], event["thread"]) for event in self.trace()
                    if event.get("command") == "archive"]
        homes = {"main": self.home, "second": self.other}
        self.assertCountEqual(archived, [(str(homes[entry["account"]]), entry["thread"])
                                        for entry in state["threads"]])
        self.assertFalse((self.home / "claude-tasks" / "scout-rotate").exists())
        self.assertTrue(self.repo.is_dir())
        self.assertEqual((self.repo / "tracked.txt").read_text(), "initial\n")
        self.assertEqual(self.git("branch", "--format=%(refname)"), branches)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)

    def test_scout_sleep_resumes_read_only_and_reports_its_kind(self):
        self.configure()
        self.configure_notify()
        self.cli("scout", "scout-sleep", "Which tests cover the configuration reader?")
        self.wait("scout-sleep")
        thread = self.state("scout-sleep")["thread"]
        reset = int(time.time()) + 3
        self.account(self.home, "A", reset, used=99, auto_reset=True)
        self.account(self.other, "B", reset + 3600, used=99)
        followup = "Read the missing-file handling too"
        self.cli("say", "scout-sleep", followup)
        paused = self.wait("scout-sleep")
        self.assertTrue(any(entry.get("event") == "sleep"
                            for entry in self.state("scout-sleep")["rotations"]))
        self.assert_scout_output(paused)
        self.assert_scout_output(self.wait("scout-sleep", timeout=10, through_sleep=True))
        invocation = self.invocations()[-1]
        self.assertTrue(invocation["resume"])
        self.assertEqual(invocation["thread"], thread)
        self.assertIn(followup, invocation["prompt"])
        self.assert_scout_read_only(invocation)
        self.assertEqual(self.state("scout-sleep")["kind"], "scout")
        for name in ("paused", "woke", "turn_finished"):
            self.assertEqual(self.notification("scout-sleep", name)["kind"], "scout")

    def test_binary_resolution_env_bundle_path_and_install_error(self):
        worker = self.worker()
        bundle = self.root / "bundled-codex"
        bundle.write_text("fixture")
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(worker, "CHATGPT_CODEX", bundle), \
                mock.patch.object(worker.shutil, "which", return_value="/opt/bin/codex") as which:
            self.assertEqual(worker.resolve_codex(), str(self.fake))
            which.assert_not_called()
            os.environ.pop("CODEX_TASK_CODEX")
            self.assertEqual(worker.resolve_codex(), str(bundle))
            which.assert_not_called()
            bundle.unlink()
            self.assertEqual(worker.resolve_codex(), "/opt/bin/codex")
            which.assert_called_once_with("codex")
            which.return_value = None
            with self.assertRaises(SystemExit) as error:
                worker.resolve_codex()
            self.assertIn("Codex CLI", str(error.exception))
            self.assertIn("CODEX_TASK_CODEX", str(error.exception))

    def test_new_settings_override_legacy_and_legacy_remains_readable(self):
        self.configure()
        worker = self.worker()
        self.assertTrue(worker.accounts_config()["enabled"])
        self.assertEqual(worker.accounts_config()["language"], "ru")
        path = self.home / "codex-worker.json"
        path.write_text(json.dumps({"language": "en", "switch_at": 95}))
        cfg = worker.accounts_config()
        self.assertEqual(cfg["language"], "en")
        self.assertEqual(cfg["switch_at"], 95)
        self.assertFalse(cfg["enabled"], "a settings file alone must not enable rotation")
        self.assertEqual(cfg["accounts"], [{"name": "main", "home": str(self.home)}])
        # A malformed legacy file must not affect the preferred settings.
        (self.home / "claude-accounts.json").write_text("not json")
        self.assertEqual(worker.accounts_config(), cfg)

    def test_language_defaults_rules_and_generated_prompts(self):
        worker = self.worker()
        task = worker.TASKS / "language"
        task.mkdir(parents=True)
        (task / "brief.md").write_text("Implement the requested change.\n")
        st = {"slug": "language", "base": "HEAD", "worktree": str(self.repo), "threads": []}
        for language in ("en", "ru"):
            with self.subTest(language=language):
                if language == "ru":
                    (self.home / "codex-worker.json").write_text('{"language":"ru"}')
                cfg = worker.accounts_config()
                self.assertEqual(cfg["language"], language)
                expected = SKILL / "references" / ("rules.ru.md" if language == "ru" else "rules.md")
                self.assertEqual(worker.rules_path(), expected)
                self.assertEqual(worker.rules_text(), expected.read_text())
                prompts = [worker.handoff(st), worker.review_prompt(st), worker.reset_resume_prompt()]
                for prompt in prompts:
                    if language == "ru":
                        self.assertRegex(prompt, "[А-Яа-яЁё]")
                    else:
                        self.assertNotRegex(prompt, "[А-Яа-яЁё]")
                self.assertIn("git diff", prompts[0])
                self.assertIn("git diff", prompts[1])

    def test_rules_override_replaces_both_languages(self):
        worker = self.worker()
        override = self.home / "codex-worker-rules.md"
        override.write_text("Custom instructions for {branch}, targeting {base}.\n")
        for language in ("en", "ru"):
            with self.subTest(language=language):
                (self.home / "codex-worker.json").write_text(json.dumps({"language": language}))
                self.assertEqual(worker.rules_path(), override)
                self.assertEqual(worker.rules_text(), override.read_text())
        self.start("custom-rules")
        self.wait("custom-rules")
        prompt = self.invocations()[0]["prompt"]
        self.assertIn("Custom instructions for codex/custom-rules, targeting main.", prompt)
        self.assertNotIn("## Как работать", prompt)
        self.assertNotIn("{branch}", prompt)

    def test_settings_without_accounts_skip_monitoring_and_use_english(self):
        (self.home / "codex-worker.json").write_text('{"credits":true,"wait_for_reset":true}')
        self.configure_notify()
        self.account(self.home, "A", self.reset, used=99)
        self.brief.write_text("Implement a portable task.\nDone when the brief is preserved.\n")
        self.start("single-account", "--review")
        self.wait("single-account")
        self.assertEqual(len(self.invocations()), 2)
        self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        self.assertFalse(self.state("single-account")["rotations"])
        for invocation in self.invocations():
            self.assertNotRegex(invocation["prompt"], "[А-Яа-яЁё]")
        self.assertIn((SKILL / "references" / "rules.md").read_text().splitlines()[0],
                      self.invocations()[0]["prompt"])
        self.notification("single-account", "turn_finished")

    def test_login_migrates_legacy_settings_to_new_file(self):
        self.configure()
        legacy = self.home / "claude-accounts.json"
        old = legacy.read_bytes()
        cfg = json.loads(old)
        cfg["switch_at"] = 97
        legacy.write_text(json.dumps(cfg))
        old = legacy.read_bytes()
        worker = self.worker()
        with mock.patch.object(worker, "prepare_home"), \
                mock.patch.object(worker.os, "execvpe") as execute, \
                mock.patch("builtins.print"):
            worker.cmd_login(SimpleNamespace(name="second", browser=False))
        self.assertEqual(legacy.read_bytes(), old)
        saved = json.loads((self.home / "codex-worker.json").read_text())
        self.assertEqual(saved["switch_at"], 97)
        self.assertEqual(saved["language"], "ru")
        self.assertEqual(saved["accounts"], cfg["accounts"])
        self.assertEqual(execute.call_args.args[2]["CODEX_HOME"], str(self.other))

    def test_notify_logs_without_computer_use_and_chains_only_when_present(self):
        hook = SCRIPT.with_name("turn-log.py")
        hook_home = self.root / "hook-home"
        client = (hook_home / ".codex" / "computer-use" / "Codex Computer Use.app" / "Contents" /
                  "SharedSupport" / "SkyComputerUseClient.app" / "Contents" / "MacOS" / "SkyComputerUseClient")
        (hook_home / ".codex").mkdir(parents=True)
        payload = json.dumps({"type": "agent-turn-complete", "thread-id": "desktop-thread",
                              "turn-id": "turn-1", "last-assistant-message": "Готово"})
        for present, parent_is_client in ((False, False), (True, False), (True, True)):
            with self.subTest(present=present, parent_is_client=parent_is_client):
                if present and not client.exists():
                    client.parent.mkdir(parents=True)
                    client.write_text("fixture")
                with mock.patch.object(Path, "home", return_value=hook_home), \
                        mock.patch.object(sys, "argv", [str(hook), payload]), \
                        mock.patch.object(subprocess, "run", return_value=SimpleNamespace(
                            stdout="SkyComputerUseClient" if parent_is_client else "python")) as run, \
                        mock.patch.object(subprocess, "Popen") as popen:
                    runpy.run_path(str(hook), run_name="__main__")
                if present and not parent_is_client:
                    self.assertEqual(popen.call_args.args[0], [str(client), "turn-ended", payload])
                else:
                    popen.assert_not_called()
                if not present:
                    run.assert_not_called()
        records = [json.loads(line) for line in (hook_home / ".codex" / "turn-events.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(rec["thread"] == "desktop-thread" and rec["last"] == "Готово" for rec in records))

    def test_desktop_wait_uses_the_next_matching_turn(self):
        hook_home = self.root / "wait-home"
        log = hook_home / ".codex" / "turn-events.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"thread": "desktop-thread", "last": "old"}) + "\n")

        def finish_turn(seconds):
            self.assertEqual(seconds, 2)
            with log.open("a") as stream:
                for thread, last in (("other-thread", "ignore"), ("desktop-thread", "new turn")):
                    stream.write(json.dumps({"thread": thread, "last": last}) + "\n")

        with mock.patch.object(Path, "home", return_value=hook_home), \
                mock.patch.object(sys, "argv", ["wait-turn.py", "desktop-thread"]), \
                mock.patch.object(time, "sleep", side_effect=finish_turn), \
                mock.patch("builtins.print") as printer:
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(SCRIPT.with_name("wait-turn.py")), run_name="__main__")
        self.assertEqual(error.exception.code, 0)
        printer.assert_called_once()
        self.assertIn("new turn", printer.call_args.args[0])

    def test_stats_helper_reads_a_rollout_with_the_current_python(self):
        rollout = self.root / "stats-rollout.jsonl"
        events = [
            {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "function_call"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2, "total_tokens": 12}}}},
            {"timestamp": "2026-01-01T00:01:00Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        rollout.write_text("\n".join(json.dumps(event) for event in events))
        result = subprocess.run([PYTHON, "-I", str(SCRIPT.with_name("codex-stats.py")), str(rollout), "--json"],
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        totals = json.loads(result.stdout)["totals"]
        self.assertEqual(totals["turns"], 1)
        self.assertEqual(totals["tool_calls"], 1)
        self.assertEqual(totals["wall_min"], 1)
        self.assertEqual(totals["uncached_input_tokens"], 7)

    def test_stats_selects_weekly_window_or_longest_and_labels_it(self):
        cases = [
            ("secondary-weekly", 300, 10, 10080, 80, 80, 10080, "weekly %"),
            ("primary-weekly", 10080, 60, 300, 90, 60, 10080, "weekly %"),
            ("longest-primary", 300, 10, 60, 80, 10, 300, "5h %"),
            ("longest-secondary", 60, 10, 300, 80, 80, 300, "5h %"),
        ]
        for name, primary_minutes, primary_used, secondary_minutes, secondary_used, expected, minutes, label in cases:
            with self.subTest(name=name):
                rollout = self.root / (name + ".jsonl")
                limits = {"primary": {"window_minutes": primary_minutes, "used_percent": primary_used},
                          "secondary": {"window_minutes": secondary_minutes, "used_percent": secondary_used}}
                before = {key: dict(window, used_percent=window["used_percent"] - 5)
                          for key, window in limits.items()}
                events = [
                    {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": before}},
                    {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
                     "payload": {"type": "task_started"}},
                    {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}},
                    # Events without rate limits must preserve the last observed snapshot.
                    {"type": "event_msg", "payload": {"type": "token_count"}},
                    {"timestamp": "2026-01-01T00:01:00Z", "type": "event_msg",
                     "payload": {"type": "task_complete"}},
                ]
                rollout.write_text("\n".join(json.dumps(event) for event in events))
                command = [PYTHON, "-I", str(SCRIPT.with_name("codex-stats.py")), str(rollout)]
                result = subprocess.run(command + ["--json"], text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                turn = json.loads(result.stdout)["turns"][0]
                self.assertEqual(turn["limit_start"], expected - 5)
                self.assertEqual(turn["limit_end"], expected)
                self.assertEqual(turn["limit_window_minutes"], minutes)
                result = subprocess.run(command, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"{label} {expected - 5}→{expected}", result.stdout)

    def test_check_options_reject_invalid_timeouts_and_conflicting_say_flags(self):
        for timeout in ("0", "-1", "nan", "inf"):
            with self.subTest(timeout=timeout):
                result = subprocess.run(
                    [PYTHON, "-I", str(SCRIPT), "start", "invalid-check", "--brief", str(self.brief),
                     "--repo", str(self.repo), "--check", "true", "--check-timeout", timeout],
                    cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("positive finite", result.stderr)
        result = subprocess.run(
            [PYTHON, "-I", str(SCRIPT), "say", "invalid-check", "Continue", "--check", "true", "--no-check"],
            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)
        self.assertFalse(self.invocations(), "invalid options must not launch Codex")

    def test_check_passes_after_completed_work_before_automatic_review(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "create_files": ["done.txt"]},
            {"mode": "complete", "reply": "Review after objective check"}])
        command = "test -f done.txt"
        self.start("check-passed", "--review", "--check", command)
        output = self.wait("check-passed")
        state = self.state("check-passed")
        self.assertEqual(state["check"], command)
        self.assertEqual(state["check_timeout"], 1800)
        self.assertEqual(len(state["checks"]), 1, "review must not rerun the working check")
        check = state["checks"][0]
        self.assertEqual((check["cmd"], check["exit"], check["outcome"]), (command, 0, "passed"))
        self.assertGreaterEqual(check["seconds"], 0)
        self.assertIsInstance(check["at"], (int, float))
        self.assertRegex(output, r"== check `test -f done\.txt`: passed \(exit 0, \d+(?:\.\d+)? s\)")
        self.assertTrue((self.home / "claude-tasks" / "check-passed" / "check.log").is_file())
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual(self.sandbox_invocations(), [])
        for event in self.invocations():
            self.assert_no_workspace_sandbox(event)
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn(command, prompt)
        self.assertIn("passed", prompt.lower())
        self.assertRegex(prompt.lower(), r"(?:do not|don't).*rerun.*(?:full|entire)")
        self.assertRegex(prompt.lower(), r"targeted|focused|specific")
        self.assertLess(output.index("== check `"), output.index("== review by "))

    def test_guarded_changes_decode_latin1_patches_for_every_diff_layer(self):
        original = ("# coding: latin-1\n"
                    "import unittest\n"
                    "class Latin1Tests(unittest.TestCase):\n"
                    "    def test_cafe(self):\n"
                    "        self.assertEqual('café', 'café')\n")
        changed = original.replace("    def test_cafe(self):\n",
                                   "    @unittest.skip('café')\n    def test_cafe(self):\n")
        changed = changed.replace("self.assertEqual('café', 'café')", "self.assertIn('café', 'café')")
        path = self.repo / "tests/test_latin1.py"
        path.parent.mkdir()
        path.write_bytes(original.encode("latin-1"))
        self.git("add", "--", "tests/test_latin1.py")
        self.git("commit", "--quiet", "-m", "Add Latin-1 test fixture")
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        path.write_bytes(changed.encode("latin-1"))
        for layer in ("working", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                self.assertEqual(len(changes), 1)
                self.assertEqual((changes[0]["path"], changes[0]["added"], changes[0]["removed"]),
                                 ("tests/test_latin1.py", 2, 1))
                self.assertEqual(len(changes[0]["disabled"]), 1)
                self.assertEqual(changes[0]["disabled"][0]["line"], 4)
                self.assertIn("@unittest.skip", changes[0]["disabled"][0]["text"])
            if layer == "working":
                self.git("add", "--", "tests/test_latin1.py")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Change Latin-1 test fixture")

    def test_latin1_guarded_patch_finishes_check_review_and_notification(self):
        self.configure(language="en")
        self.configure_notify()
        original = ("# coding: latin-1\n"
                    "import unittest\n"
                    "class Latin1Tests(unittest.TestCase):\n"
                    "    def test_cafe(self):\n"
                    "        self.assertEqual('café', 'café')\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n")
        changed = original.replace("    def test_cafe(self):\n",
                                   "    @unittest.skip('café')\n    def test_cafe(self):\n")
        changed = changed.replace("self.assertEqual('café', 'café')", "self.assertIn('café', 'café')")
        path = self.repo / "tests/test_latin1.py"
        path.parent.mkdir()
        path.write_bytes(original.encode("latin-1"))
        self.git("add", "--", "tests/test_latin1.py")
        self.git("commit", "--quiet", "-m", "Add Latin-1 test fixture")
        self.git("push", "--quiet", "origin", "main")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tests/test_latin1.py": changed},
             "write_encodings": {"tests/test_latin1.py": "latin-1"},
             "commit_files": ["tests/test_latin1.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-latin1", "--review", "--check", PYTHON + " -I tests/test_latin1.py")
        output = self.wait("guarded-latin1")
        state = self.state("guarded-latin1")
        self.assertIsNone(state["pid"])
        self.assertFalse(state.get("error"))
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== guarded files changed: tests/test_latin1.py (+2 −1)", output)
        self.assertIn("== guarded tests disabled: tests/test_latin1.py:4 @unittest.skip", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertIn("tests/test_latin1.py", self.review_invocations()[0]["prompt"])
        self.assertEqual(self.notification("guarded-latin1", "turn_finished")["guarded_changed"], 1)
        self.assertEqual((Path(state["worktree"]) / "tests/test_latin1.py").read_bytes(),
                         changed.encode("latin-1"))

    def test_guarded_changes_print_counts_and_reach_review_and_notification(self):
        self.configure(language="en")
        self.configure_notify()
        self.commit_fixture({"tests/test_a.py": "def test_a():\n    assert True\n",
                             "src/a.py": "value = 1\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {
                "tests/test_a.py": "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n",
                "src/a.py": "value = 2\n"}, "commit_files": ["tests/test_a.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-counts", "--review")
        output = self.wait("guarded-counts")
        guarded = [line for line in output.splitlines() if line.startswith("== guarded files changed:")]
        self.assertEqual(guarded, ["== guarded files changed: tests/test_a.py (+2 −1)"])
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn("tests/test_a.py", prompt)
        self.assertRegex(prompt.lower(), r"assertion|assert")
        self.assertRegex(prompt.lower(), r"skip|disabled")
        self.assertRegex(prompt.lower(), r"tolerance|tolerances")
        self.assertIn("mock", prompt.lower())
        self.assertEqual(self.notification("guarded-counts", "turn_finished")["guarded_changed"], 1)

    def test_disabled_test_markers_ignore_prose_and_match_test_annotations(self):
        pattern = self.worker().DISABLED_TEST
        for line in ("only P3 remains", "Contend only for the reset journal lock",
                     "skip the step", "skipped = True", "xfail = False"):
            with self.subTest(line=line):
                self.assertIsNone(pattern.search(line))
        for line in ("@unittest.skip('reason')", "@unittest.skipIf(True, 'reason')",
                     "@unittest.skipUnless(False, 'reason')", "@unittest.expectedFailure",
                     "self.skipTest('reason')", "@pytest.mark.skip", "@pytest.mark.skipif(True)",
                     "@pytest.mark.xfail", "pytestmark = pytest.mark.skip(reason='fixture')",
                     "pytest.param(1, marks=pytest.mark.xfail)", "pytest.skip('reason')", "it.skip('case', fn)",
                     "it.only('case', fn)", "xit('case', fn)", "xdescribe('suite', fn)",
                     "@Disabled", "@Ignore", "t.Skip('reason')"):
            with self.subTest(line=line):
                self.assertIsNotNone(pattern.search(line))

    def test_guarded_deleted_files_and_added_skips_have_separate_warnings(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_a.py": "def test_a():\n    assert True\n",
                             "tests/test_removed.py": "def test_removed():\n    assert True\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {
                "tests/test_a.py": "@unittest.skip('temporarily disabled')\ndef test_a():\n    assert True\n"},
            "remove_files": ["tests/test_removed.py"]}])
        self.start("guarded-disabled")
        output = self.wait("guarded-disabled")
        deleted = [line for line in output.splitlines() if line.startswith("== guarded files deleted:")]
        disabled = [line for line in output.splitlines() if line.startswith("== guarded tests disabled:")]
        self.assertEqual(len(deleted), 1)
        self.assertIn("tests/test_removed.py", deleted[0])
        self.assertEqual(len(disabled), 1)
        self.assertIn("tests/test_a.py", disabled[0])
        self.assertNotIn("tests/test_removed.py", disabled[0])

    def test_check_paths_and_repeatable_guard_globs_protect_custom_files(self):
        self.configure(language="en")
        self.commit_fixture({"scripts/verify.sh": "printf 'original check\\n'\n",
                             "fixtures/nested/sample.json": "{}\n",
                             "config/check.toml": "enabled = true\n",
                             "src/a.py": "value = 1\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {
                "scripts/verify.sh": "printf 'replacement check\\n'\n",
                "fixtures/nested/sample.json": '{"changed": true}\n',
                "config/check.toml": "enabled = false\n",
                "src/a.py": "value = 2\n"}}])
        self.start("guarded-custom", "--check", "sh scripts/verify.sh", "--guard", "fixtures/**",
                   "--guard", "config/*.toml")
        output = self.wait("guarded-custom")
        state = self.state("guarded-custom")
        self.assertEqual(state["guards"], ["fixtures/**", "config/*.toml"])
        guarded = next(line for line in output.splitlines() if line.startswith("== guarded files changed:"))
        for name in ("scripts/verify.sh", "fixtures/nested/sample.json", "config/check.toml"):
            self.assertIn(name + " (+1 −1)", guarded)
        self.assertNotIn("src/a.py", guarded)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")

    def test_check_paths_find_assignments_options_and_literal_equals_paths(self):
        files = {"scripts/verify.sh": "exit 0\n", "scripts/check=literal.sh": "exit 0\n",
                 "verify=literal.sh": "exit 0\n", "config/check.toml": "enabled = true\n"}
        self.commit_fixture(files)
        command = ('CHECK_SCRIPT=scripts/verify.sh EMPTY= sh "$CHECK_SCRIPT" '
                   '--config=config/check.toml scripts/check=literal.sh verify=literal.sh')
        paths = self.worker().check_paths({"worktree": str(self.repo), "check": command})
        self.assertEqual(paths, sorted(files))
        self.assertNotIn(".", paths, "an empty assignment must not guard every repository file")

    def test_check_paths_only_inspect_relative_top_level_tokens(self):
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        worker = self.worker()
        for command in ("sh -c 'sh scripts/verify.sh'",
                        "sh " + shlex.quote(str(self.repo / "scripts/verify.sh")),
                        "sh scripts/verify.sh 'unterminated"):
            with self.subTest(command=command):
                state = {"worktree": str(self.repo), "check": command}
                self.assertEqual(worker.check_paths(state), [])
                self.assertNotIn("check_paths", state)

    def test_check_paths_separate_shell_punctuation_from_paths(self):
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        worker = self.worker()
        for command in ("sh scripts/verify.sh;", "sh scripts/verify.sh&&true",
                        "sh scripts/verify.sh|cat", "cat>scripts/verify.sh"):
            with self.subTest(command=command):
                state = {"worktree": str(self.repo), "check": command}
                self.assertEqual(worker.check_paths(state), ["scripts/verify.sh"])
                self.assertNotIn("check_paths", state)

    def test_check_paths_return_union_without_mutating_state(self):
        self.commit_fixture({"scripts/first.sh": "exit 0\n", "scripts/second.sh": "exit 0\n"})
        worker = self.worker()
        state = {"worktree": str(self.repo), "check": "sh scripts/first.sh"}
        self.assertEqual(worker.check_paths(state), ["scripts/first.sh"])
        self.assertNotIn("check_paths", state)
        state["check_paths"] = ["scripts/first.sh"]
        state["check"] = "sh scripts/second.sh"
        snapshot = json.loads(json.dumps(state))
        self.assertEqual(worker.check_paths(state), ["scripts/first.sh", "scripts/second.sh"])
        self.assertEqual(state, snapshot)
        (self.repo / "scripts/first.sh").unlink()
        (self.repo / "scripts/second.sh").unlink()
        state["check"] = "sh 'unterminated"
        snapshot = json.loads(json.dumps(state))
        self.assertEqual(worker.check_paths(state), ["scripts/first.sh"])
        self.assertEqual(state, snapshot)

    def test_guarded_changes_review_prompt_and_status_do_not_write_state(self):
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        (self.repo / "scripts/verify.sh").write_text("exit 1\n")
        worker = self.worker()
        state = {"slug": "guarded-read-only", "worktree": str(self.repo),
                 "branch": "test-branch", "base": self.git("rev-parse", "HEAD"),
                 "account": "main", "turns": 1, "pid": None,
                 "check": "sh scripts/verify.sh", "check_paths": []}
        snapshot = json.loads(json.dumps(state))
        with mock.patch.object(worker, "save") as save:
            self.assertEqual([change["path"] for change in worker.guarded_changes(state)],
                             ["scripts/verify.sh"])
            self.assertIn("scripts/verify.sh", worker.review_prompt(state, {"language": "en"}))
            with mock.patch.object(worker, "load", return_value=state), \
                    mock.patch.object(worker, "sh", return_value=""), \
                    mock.patch.object(worker, "last_message", return_value=""), \
                    mock.patch("builtins.print"):
                worker.cmd_status(SimpleNamespace(slug=state["slug"]))
        save.assert_not_called()
        self.assertEqual(state, snapshot)

    def test_wait_does_not_overwrite_a_concurrent_say_when_discovering_check_paths(self):
        worker = self.worker()
        slug = "guarded-wait-say-race"
        (worker.TASKS / slug).mkdir(parents=True)
        state = {"slug": slug, "worktree": str(self.repo), "branch": "test-branch",
                 "base": self.git("rev-parse", "HEAD"), "account": "main", "thread": "existing-thread",
                 "rotations": [], "turns": 1, "pid": None, "outcome": "completed",
                 "check": "sh scripts/verify.sh", "check_paths": []}
        worker.save(state)
        script = self.repo / "scripts/verify.sh"
        script.parent.mkdir()
        script.write_text("exit 0\n")
        new_pid = 2 ** 31 - 1
        original_popen = worker.subprocess.Popen
        original_guarded_changes = worker.guarded_changes
        launches = []

        def popen(command, *args, **kwargs):
            if len(command) > 3 and command[3] == "_run":
                launches.append(command)
                return SimpleNamespace(pid=new_pid)
            return original_popen(command, *args, **kwargs)

        def describe_old_state(old_state):
            worker.cmd_say(SimpleNamespace(
                slug=slug, file=None, message="Continue working", effort=None, no_check=False,
                check=None, check_timeout=None, review=False, fix_rounds=None, no_base_check=False))
            return original_guarded_changes(old_state)

        with mock.patch.object(worker, "accounts_config", return_value={"language": "en"}), \
                mock.patch.object(worker, "alive", side_effect=lambda pid: pid == new_pid), \
                mock.patch.object(worker.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(worker, "guarded_changes", side_effect=describe_old_state), \
                mock.patch.object(worker, "save", wraps=worker.save) as save, \
                mock.patch("builtins.print") as printer:
            worker.cmd_wait(SimpleNamespace(slug=slug, timeout=0, through_sleep=False))
        self.assertEqual(len(launches), 1)
        save.assert_called_once()
        current = worker.load(slug)
        self.assertEqual((current["turns"], current["pid"], current["outcome"]),
                         (2, new_pid, "running"))
        self.assertEqual(current["check_paths"], [], "only the supervisor may save newly found paths")
        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("== guarded files changed: scripts/verify.sh (+1 −0)", output)

    def test_check_assignment_script_is_guarded_restored_and_reviewed(self):
        self.configure(language="en")
        self.configure_notify()
        self.commit_fixture({"scripts/verify.sh": "exit 1\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"scripts/verify.sh": "exit 0\n"},
             "commit_files": ["scripts/verify.sh"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = "CHECK_SCRIPT=scripts/verify.sh sh -c 'sh \"$CHECK_SCRIPT\"'"
        self.start("guarded-assignment", "--review", "--check", command, "--base-check")
        output = self.wait("guarded-assignment")
        state = self.state("guarded-assignment")
        self.assertEqual(state["check"], command, "the saved command must remain unchanged")
        self.assertEqual(state["check_paths"], ["scripts/verify.sh"])
        check = state["checks"][-1]
        self.assertEqual((check["outcome"], check["exit"]), ("passed", 0))
        self.assertEqual((check["base_check"]["outcome"], check["base_check"]["exit"]), ("failed", 1))
        self.assertIn("== guarded files changed: scripts/verify.sh (+1 −1)", output)
        self.assertIn("== base tests: FAILED", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertIn("scripts/verify.sh", self.review_invocations()[0]["prompt"])
        self.assertIn("Base tests: failed", self.review_invocations()[0]["prompt"])
        self.assertLess(output.index("== base tests: FAILED"), output.index("== review by "))
        self.assertEqual(self.notification("guarded-assignment", "turn_finished")["guarded_changed"], 1)
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)
        self.assertEqual((Path(state["worktree"]) / "scripts/verify.sh").read_text(), "exit 0\n")

    def test_builtin_guards_cover_nested_tests_names_and_ci_and_untracked_files(self):
        self.configure(language="en")
        guarded_names = ("pkg/tests/a.py", "pkg/test/a.py", "pkg/__tests__/a.js", "pkg/spec/a.rb",
                         "pkg/test_widget.py", "pkg/widget_test.go", "pkg/widget.test.js",
                         "pkg/widget.spec.ts", ".github/workflows/check.yml", ".gitlab-ci.yml",
                         ".forgejo/workflows/check.yml")
        self.commit_fixture({name: "old\n" for name in guarded_names})
        changes = {name: "new\n" for name in guarded_names}
        changes.update({"tests/test_new.py": "new test\n", "src/a.py": "new source\n",
                        "contest/a.py": "unrelated\n"})
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "write_files": changes}])
        self.start("guarded-patterns")
        output = self.wait("guarded-patterns")
        guarded = next(line for line in output.splitlines() if line.startswith("== guarded files changed:"))
        for name in guarded_names:
            self.assertIn(name + " (+1 −1)", guarded)
        self.assertIn("tests/test_new.py (+1 −0)", guarded)
        self.assertNotIn("src/a.py", guarded)
        self.assertNotIn("contest/a.py", guarded)

    def test_base_check_restores_base_tests_reports_failure_and_still_reviews(self):
        self.configure(language="en")
        base_test = ("from pathlib import Path\n"
                     "assert Path('tests/test_removed.py').is_file(), 'deleted test was not restored'\n"
                     "assert not Path('tests/test_new.py').exists(), 'new test was not removed'\n"
                     "print('base guarded files restored', flush=True)\n"
                     "assert Path('src/a.txt').read_text() == 'expected\\n', 'original assertion failed'\n")
        self.commit_fixture({"tests/test_a.py": base_test, "tests/test_removed.py": "# original test\n",
                             "src/a.txt": "expected\n"})
        changes = {"tests/test_a.py": "print('weakened check passed')\n",
                   "tests/test_new.py": "# replacement test\n", "src/a.txt": "broken\n"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": changes, "remove_files": ["tests/test_removed.py"],
             "commit_files": list(changes) + ["tests/test_removed.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = PYTHON + " -I tests/test_a.py"
        self.start("guarded-base", "--review", "--check", command, "--base-check")
        output = self.wait("guarded-base")
        state = self.state("guarded-base")
        self.assertTrue(state["base_check"])
        check = state["checks"][-1]
        self.assertEqual((check["outcome"], check["exit"]), ("passed", 0))
        self.assertEqual(check["base_check"]["outcome"], "failed")
        self.assertNotEqual(check["base_check"]["exit"], 0)
        self.assertGreaterEqual(check["base_check"]["seconds"], 0)
        self.assertIn("== base tests: FAILED", output)
        self.assertIn("base guarded files restored", output)
        self.assertIn("original assertion failed", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertLess(output.index("== base tests: FAILED"), output.index("== review by "))
        self.assertNotIn("== review skipped:", output)
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)
        self.assertEqual((Path(state["worktree"]) / "tests/test_a.py").read_text(), changes["tests/test_a.py"])
        self.assertFalse((Path(state["worktree"]) / "tests/test_removed.py").exists())
        self.assertTrue((Path(state["worktree"]) / "tests/test_new.py").is_file())

    def test_base_check_skips_absolute_task_paths_and_still_reviews(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_a.py": "assert False, 'base assertion'\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tests/test_a.py": "print('weakened test passed')\n"},
             "commit_files": ["tests/test_a.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        slug = "guarded-absolute"
        worktree = self.home / "worktrees" / ("claude-" + slug) / self.repo.name
        command = shlex.quote(PYTHON) + " -I " + shlex.quote(str(worktree / "tests/test_a.py"))
        reason = "check command uses the task worktree's absolute path"
        self.start(slug, "--review", "--check", command, "--base-check")
        output = self.wait(slug)
        state = self.state(slug)
        check = state["checks"][-1]
        self.assertEqual(state["check"], command, "the saved command must remain unchanged")
        self.assertEqual((check["outcome"], check["exit"]), ("passed", 0))
        self.assertEqual(check["base_check"], {"outcome": "skipped", "reason": reason})
        self.assertIn("== base tests: skipped (" + reason + ")", output)
        self.assertEqual(len(self.review_invocations()), 1)
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn("Base tests: skipped", prompt)
        self.assertIn(reason, prompt)
        self.assertNotIn("== review skipped:", output)
        self.assertFalse((self.home / "claude-tasks" / slug / "base-check.log").exists())
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)
        self.assertIn("weakened test", (worktree / "tests/test_a.py").read_text())

    def test_say_disables_base_check_and_keeps_it_disabled(self):
        self.configure(language="en")
        self.start("guarded-disable-base", "--check", "true", "--base-check")
        output = self.wait("guarded-disable-base")
        self.assertIn("== base tests: passed", output)
        self.assertEqual(self.state("guarded-disable-base")["checks"][0]["base_check"]["outcome"], "passed")
        self.cli("say", "guarded-disable-base", "Disable the baseline comparison", "--no-base-check")
        self.assertNotIn("== base tests:", self.wait("guarded-disable-base"))
        self.cli("say", "guarded-disable-base", "Continue with the saved settings")
        self.assertNotIn("== base tests:", self.wait("guarded-disable-base"))
        state = self.state("guarded-disable-base")
        self.assertFalse(state.get("base_check"))
        self.assertEqual(len(state["checks"]), 3)
        self.assertTrue(all(not check.get("base_check") for check in state["checks"][1:]))

    def test_no_guarded_change_has_no_warning_and_notifies_zero(self):
        self.configure(language="en")
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"src/a.py": "value = 2\n"}}])
        self.start("guarded-none")
        output = self.wait("guarded-none")
        self.assertNotIn("== guarded files changed:", output)
        self.assertNotIn("== guarded files deleted:", output)
        self.assertNotIn("== guarded tests disabled:", output)
        self.assertEqual(self.notification("guarded-none", "turn_finished")["guarded_changed"], 0)

    def test_guarded_index_changes_survive_restoring_working_contents(self):
        self.configure(language="en")
        self.configure_notify()
        original = "def test_a():\n    assert True\n"
        self.commit_fixture({"tests/test_a.py": original})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete"}, {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-index")
        self.wait("guarded-index")
        self.notification("guarded-index", "turn_finished")
        worktree = Path(self.state("guarded-index")["worktree"])
        path = worktree / "tests/test_a.py"
        path.write_text("@unittest.skip('staged weakening')\n" + original)
        staged = subprocess.run(["git", "add", "--", "tests/test_a.py"], cwd=worktree,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        path.write_text(original)
        combined = subprocess.run(["git", "diff", "HEAD", "--", "tests/test_a.py"], cwd=worktree,
                                  env=self.env, text=True, capture_output=True)
        self.assertEqual(combined.returncode, 0, combined.stderr)
        self.assertEqual(combined.stdout, "", "fixture must conceal the staged edit from git diff HEAD")
        output = self.wait("guarded-index")
        guarded = next(line for line in output.splitlines() if line.startswith("== guarded files changed:"))
        disabled = next(line for line in output.splitlines() if line.startswith("== guarded tests disabled:"))
        self.assertIn("tests/test_a.py", guarded)
        self.assertIn("tests/test_a.py", disabled)
        self.cli("review", "guarded-index")
        self.wait("guarded-index")
        self.assertIn("tests/test_a.py", self.review_invocations()[0]["prompt"])
        self.until(lambda: len([event for event in self.notifications("guarded-index")
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications("guarded-index") if event["event"] == "turn_finished"]
        self.assertEqual(finished[-1]["guarded_changed"], 1)

    def test_check_root_directory_path_guards_non_test_files(self):
        self.configure(language="en")
        self.commit_fixture({"src/a.py": "value = 1\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"src/a.py": "value = 2\n"}}])
        command = "test -d . && : " + "x" * 300
        self.start("guarded-root-path", "--check", command)
        output = self.wait("guarded-root-path")
        self.assertIn("== guarded files changed: src/a.py (+1 −1)", output)
        self.assertEqual(self.state("guarded-root-path")["checks"][-1]["outcome"], "passed")

    def test_check_path_stays_guarded_after_worker_deletes_it(self):
        self.configure(language="en")
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "remove_files": ["scripts/verify.sh"]}])
        self.start("guarded-deleted-check", "--check", "test ! -e scripts/verify.sh")
        output = self.wait("guarded-deleted-check")
        self.assertIn("== guarded files changed: scripts/verify.sh (+0 −1)", output)
        self.assertIn("== guarded files deleted: scripts/verify.sh", output)
        self.assertEqual(self.state("guarded-deleted-check")["checks"][-1]["outcome"], "passed")

    def test_new_check_path_stays_guarded_after_later_turn_deletes_it(self):
        self.configure(language="en")
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"scripts/verify.sh": "exit 0\n"},
             "commit_files": ["scripts/verify.sh"]},
            {"mode": "complete", "remove_files": ["scripts/verify.sh"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        slug = "guarded-later-deleted-check"
        state_path = self.home / "claude-tasks" / slug / "state.json"
        saved_path_check = ("import json; from pathlib import Path; "
                            "assert json.loads(Path(%r).read_text())['check_paths'] == ['scripts/verify.sh']"
                            % str(state_path))
        command = (PYTHON + " -I -c " + shlex.quote(saved_path_check) +
                   " && { test ! -e scripts/verify.sh || sh scripts/verify.sh; }")
        self.start(slug, "--check", command)
        self.wait(slug)
        self.assertEqual(self.state(slug)["check_paths"], ["scripts/verify.sh"])
        self.notification(slug, "turn_finished")
        self.cli("say", slug, "Remove the generated check script", "--review")
        output = self.wait(slug)
        state = self.state(slug)
        self.assertEqual(state["check_paths"], ["scripts/verify.sh"])
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertIn("== guarded files changed: scripts/verify.sh (+1 −1)", output)
        self.assertIn("== guarded files deleted: scripts/verify.sh", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertIn("scripts/verify.sh", self.review_invocations()[0]["prompt"])
        self.until(lambda: len([event for event in self.notifications(slug)
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications(slug) if event["event"] == "turn_finished"]
        self.assertEqual(finished[-1]["guarded_changed"], 1)

    def test_base_check_timeout_prints_tail_reviews_and_removes_worktree(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_a.py": "import time\nprint('base timeout marker', flush=True)\ntime.sleep(60)\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tests/test_a.py": "print('fast modified test')\n"},
             "commit_files": ["tests/test_a.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-base-timeout", "--review", "--check", PYTHON + " -I tests/test_a.py",
                   "--base-check", "--check-timeout", "0.5")
        output = self.wait("guarded-base-timeout")
        state = self.state("guarded-base-timeout")
        check = state["checks"][-1]
        self.assertEqual(check["outcome"], "passed")
        self.assertEqual(check["base_check"]["outcome"], "timeout")
        self.assertGreaterEqual(check["base_check"]["seconds"], 0.5)
        self.assertIn("== base tests: FAILED", output)
        self.assertIn("base timeout marker", output)
        self.assertEqual(len(self.review_invocations()), 1)
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)

    def test_check_failure_prints_last_forty_lines_and_skips_review(self):
        self.configure()
        command = ("i=1; while [ \"$i\" -le 45 ]; do printf 'check-line-%02d\\n' \"$i\"; "
                   "i=$((i + 1)); done; printf 'failure-stderr\\n' >&2; test -f done.txt")
        self.start("check-failed", "--review", "--check", command)
        output = self.wait("check-failed")
        check = self.state("check-failed")["checks"][-1]
        self.assertEqual((check["cmd"], check["exit"], check["outcome"]), (command, 1, "failed"))
        self.assertIn("== check `" + command + "`: FAILED (exit 1, ", output)
        self.assertIn("check-line-07", output)
        self.assertIn("check-line-45", output)
        self.assertIn("failure-stderr", output)
        self.assertNotIn("check-line-06", output)
        self.assertIn("== review skipped: check failed", output)
        self.assertFalse(self.review_invocations())
        self.assertNotIn("== review by", output)

    def test_check_timeout_kills_the_shell_process_group_and_child(self):
        self.configure()
        command = 'sleep 60 & child=$!; echo "$child" > check-child.pid; wait "$child"'
        self.start("check-timeout", "--review", "--check", command, "--check-timeout", "0.5")
        child = self.check_child("check-timeout")
        output = self.wait("check-timeout")
        state = self.state("check-timeout")
        self.assertEqual(state["checks"][-1]["outcome"], "timeout")
        self.assertGreaterEqual(state["checks"][-1]["seconds"], 0.5)
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(state.get("check_pid"))
        self.assertIn("FAILED", output)
        self.assertIn("timeout", output)
        self.assertIn("== review skipped:", output)
        self.assertFalse(self.review_invocations())

    def test_say_inherits_replaces_and_disables_check_and_timeout(self):
        self.configure()
        first = "printf 'first\\n' >> check-runs"
        second = "printf 'second\\n' >> check-runs"
        self.start("check-say", "--check", first, "--check-timeout", "7")
        self.wait("check-say")
        self.cli("say", "check-say", "Run another working turn")
        inherited = self.wait("check-say")
        self.assertIn("== check `" + first + "`: passed", inherited)
        self.assertEqual(len(self.state("check-say")["checks"]), 2)
        self.assertEqual(self.state("check-say")["check_timeout"], 7)
        self.cli("say", "check-say", "Use the replacement check", "--check", second,
                 "--check-timeout", "9")
        self.wait("check-say")
        self.assertEqual(self.state("check-say")["check"], second)
        self.assertEqual(self.state("check-say")["check_timeout"], 9)
        self.cli("say", "check-say", "Continue without checking", "--no-check")
        disabled = self.wait("check-say")
        self.assertFalse(self.state("check-say").get("check"))
        self.assertNotIn("== check `", disabled)
        self.cli("say", "check-say", "The disabled check stays disabled")
        self.assertNotIn("== check `", self.wait("check-say"))
        state = self.state("check-say")
        self.assertEqual(len(state["checks"]), 3)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(),
                         ["first", "first", "second"])

    def test_stop_during_check_kills_its_group_and_records_stopped(self):
        self.configure()
        command = 'sleep 60 & child=$!; echo "$child" > check-child.pid; wait "$child"'
        self.start("check-stopped", "--review", "--check", command)
        child = self.check_child("check-stopped")
        self.until(lambda: bool(self.state("check-stopped").get("check_pid")))
        self.cli("stop", "check-stopped", timeout=20)
        self.wait("check-stopped")
        state = self.state("check-stopped")
        self.assertEqual(state["outcome"], "stopped")
        self.assertEqual(state["checks"][-1]["outcome"], "stopped")
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(state.get("check_pid"))
        self.assertFalse(self.review_invocations())

    def test_explicit_review_does_not_run_a_persisted_check(self):
        self.configure(language="en")
        self.start("check-explicit-review", "--check", "printf 'checked\\n' >> check-runs")
        self.wait("check-explicit-review")
        self.cli("review", "check-explicit-review")
        output = self.wait("check-explicit-review")
        state = self.state("check-explicit-review")
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text(), "checked\n")
        self.assertNotIn("== check `", output)
        self.assertEqual(len(self.review_invocations()), 1)
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn("printf 'checked\\n' >> check-runs", prompt)
        self.assertIn("passed", prompt.lower())
        self.assertRegex(prompt.lower(), r"(?:do not|don't).*rerun.*(?:full|entire)")

    def test_review_without_saved_check_has_no_passed_suite_instruction(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("review-no-check", "--review")
        self.wait("review-no-check")
        prompt = self.review_invocations()[0]["prompt"].lower()
        self.assertNotIn("passed", prompt)
        self.assertNotRegex(prompt, r"(?:do not|don't).*rerun.*(?:full|entire)")

    def test_notify_reports_completed_turn_and_failed_check_with_task_fields(self):
        self.configure()
        self.configure_notify()
        self.start("notify-check", "--check", "test -f done.txt")
        self.wait("notify-check")
        finished = self.notification("notify-check", "turn_finished")
        failed = self.notification("notify-check", "check_failed")
        self.assertEqual(finished["outcome"], "completed")
        state = self.state("notify-check")
        for event in (finished, failed):
            self.assertEqual(event["repo"], str(self.repo))
            self.assertEqual(event["branch"], state["branch"])
            self.assertEqual(event["account"], "main")
            self.assertIsInstance(event["at"], (int, float))
            self.assertTrue(event["message"].startswith("notify-check: "))
            self.assertNotIn("\n", event["message"])
        self.assertIn("check", finished["message"].lower())

    def test_notify_reports_rotation_pause_wake_and_review(self):
        self.configure()
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.start("notify-rotation")
        self.wait("notify-rotation")
        rotated = self.notification("notify-rotation", "rotated")
        self.assertEqual((rotated["from"], rotated["to"]), ("main", "second"))

        reset = int(time.time()) + 3
        self.account(self.home, "A", reset, used=99, auto_reset=True)
        self.account(self.other, "B", reset + 3600, used=99)
        self.start("notify-sleep", "--review")
        self.wait("notify-sleep")
        paused = self.notification("notify-sleep", "paused")
        self.assertAlmostEqual(paused["until"], reset + 0.1, places=3)
        self.wait("notify-sleep", timeout=10, through_sleep=True)
        self.notification("notify-sleep", "woke")
        self.notification("notify-sleep", "review_done")
        self.notification("notify-sleep", "turn_finished")

    def test_notify_events_filters_unrequested_event_types(self):
        self.configure()
        self.configure_notify(events=["turn_finished"])
        self.start("notify-filter", "--check", "test -f done.txt")
        self.wait("notify-filter")
        self.notification("notify-filter", "turn_finished")
        self.assertEqual([event["event"] for event in self.notifications("notify-filter")],
                         ["turn_finished"])

    def test_final_notifications_survive_clean_at_idle_state_publication(self):
        self.configure()
        receiver = self.root / "receive-after-clean.py"
        receiver.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "gate, output, task, worktree = map(Path, sys.argv[1:5])\n"
            "deadline = time.monotonic() + 10\n"
            "while not gate.exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise RuntimeError('cleanup never released notification receiver')\n"
            "    time.sleep(0.01)\n"
            "event = json.loads(sys.argv[-1])\n"
            "event['receiver_cwd'] = os.getcwd()\n"
            "event['task_removed'] = not task.exists()\n"
            "event['worktree_removed'] = not worktree.exists()\n"
            "os.write(1, ('stdout: ' + event['event'] + '\\n').encode())\n"
            "os.write(2, ('stderr: ' + event['event'] + '\\n').encode())\n"
            "with output.open('a') as stream:\n"
            "    stream.write(json.dumps(event) + '\\n')\n")
        cases = (("work", None, {"turn_finished"}),
                 ("review", None, {"review_done", "turn_finished"}),
                 ("failed-check", "exit 1", {"check_failed", "turn_finished"}))
        for mode, check, expected in cases:
            with self.subTest(mode=mode):
                slug = "notify-clean-" + mode
                branch = "codex/" + slug
                task = self.home / "claude-tasks" / slug
                task.mkdir(parents=True)
                worktree = self.home / "worktrees" / ("claude-" + slug) / self.repo.name
                worktree.parent.mkdir(parents=True)
                self.git("worktree", "add", "--quiet", "-b", branch, str(worktree), "main")
                gate = self.root / (slug + ".released")
                output = self.root / (slug + ".jsonl")
                self.configure_notify(command=[PYTHON, str(receiver), str(gate), str(output),
                                               str(task), str(worktree)])
                worker = self.worker()
                state = {"slug": slug, "repo": str(self.repo), "worktree": str(worktree),
                         "branch": branch, "base": "main", "account": "main", "thread": "",
                         "threads": [], "reviews": [], "rotations": [], "checks": [],
                         "pid": os.getpid(), "kind": "review" if mode == "review" else "work",
                         "outcome": "running", "check": check, "check_timeout": 1,
                         "pending": {"prompt": "fixture turn", "effort": None,
                                     "review_after": mode == "failed-check"}}
                worker.save(state)
                real_save = worker.save
                publications = []

                def completed_turn(current, *args, **kwargs):
                    current["outcome"] = "completed"
                    if current["kind"] == "review":
                        # Exercise fallback to this turn's review metadata.
                        current["reviews"].append({"account": "second", "thread": "fixture-review"})

                def save_and_clean(current):
                    real_save(current)
                    if current.get("pid") is not None:
                        return
                    publications.append({"state": json.loads(json.dumps(current)),
                                         "launched": {json.loads(proc.args[-1])["event"]
                                                      for proc in worker.NOTIFY_PROCESSES}})
                    try:
                        worker.cmd_clean(SimpleNamespace(slug=slug, keep_branch=False))
                    finally:
                        gate.touch()

                # Keep the same descriptors open after clean unlinks their paths.
                with (task / "notify.log").open("a+b", buffering=0) as notify_log, \
                        (task / "stderr.log").open("w+") as errors, \
                        mock.patch.object(worker.sys, "stderr", errors), \
                        mock.patch.object(worker.sys, "stdout", io.StringIO()), \
                        mock.patch.dict(os.environ, self.env), \
                        mock.patch.object(worker.signal, "signal"), \
                        mock.patch.object(worker, "run_turn", side_effect=completed_turn), \
                        mock.patch.object(worker, "save", side_effect=save_and_clean):
                    try:
                        worker.cmd__run(SimpleNamespace(slug=slug))
                    finally:
                        gate.touch()
                        self.extra_pids.update(proc.pid for proc in worker.NOTIFY_PROCESSES)
                        for proc in worker.NOTIFY_PROCESSES:
                            self.assertEqual(proc.wait(timeout=5), 0)
                    errors.flush()
                    errors.seek(0)
                    self.assertNotIn("notification log failed", errors.read())
                    notify_log.seek(0)
                    log = notify_log.read().decode()
                self.assertEqual(len(publications), 1)
                self.assertEqual(publications[0]["launched"], expected,
                                 "every final hook must start before pid=None becomes visible")
                self.assertFalse(task.exists())
                self.assertFalse(worktree.exists())
                events = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual({event["event"] for event in events}, expected)
                for event in events:
                    self.assertEqual(event["receiver_cwd"], "/")
                    self.assertTrue(event["task_removed"])
                    self.assertTrue(event["worktree_removed"])
                    self.assertIn("stdout: " + event["event"], log)
                    self.assertIn("stderr: " + event["event"], log)
                    if mode == "review":
                        self.assertEqual(event["account"], "second")
                        self.assertIn("on second", event["message"])
                self.assertIsNone(publications[0]["state"]["pid"])
                self.assertEqual(publications[0]["state"]["outcome"], "completed")

    def test_standalone_review_turn_finished_uses_its_executor_account(self):
        self.configure()
        self.configure_notify()
        slug = "notify-review-account"
        self.start(slug)
        self.wait(slug)
        self.notification(slug, "turn_finished")
        working = self.state(slug)
        self.assertEqual(working["account"], "main")
        self.account(self.home, "A", self.reset, used=99)
        self.cli("review", slug)
        self.wait(slug)
        self.until(lambda: len([event for event in self.notifications(slug)
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications(slug) if event["event"] == "turn_finished"][-1]
        self.assertEqual(finished["account"], "second")
        self.assertIn("on second", finished["message"])
        self.assertEqual(self.state(slug)["account"], "main")
        self.assertEqual(self.state(slug)["thread"], working["thread"])
        self.assertEqual(self.review_invocations()[-1]["home"], str(self.other))

    def test_missing_notify_executable_is_logged_without_failing_work(self):
        self.configure()
        missing = str(self.root / "missing-notification-executable")
        self.configure_notify(command=[missing])
        self.start("notify-missing")
        self.wait("notify-missing")
        self.assertEqual(self.state("notify-missing")["outcome"], "completed")
        log = self.home / "claude-tasks" / "notify-missing" / "notify.log"
        self.assertTrue(log.is_file())
        self.assertIn(missing, log.read_text())

    def test_slow_notify_does_not_block_supervisor_completion(self):
        self.configure()
        received = self.root / "slow-notification.json"
        code = ("import json, os, sys, time; from pathlib import Path; "
                "event = json.loads(sys.argv[-1]); event['receiver_pid'] = os.getpid(); "
                "Path(sys.argv[1]).write_text(json.dumps(event)); time.sleep(60)")
        self.configure_notify(command=[PYTHON, "-c", code, str(received)])
        self.start("notify-slow")
        self.wait("notify-slow", timeout=10)
        self.until(lambda: received.exists() and received.read_text().endswith("}"))
        event = json.loads(received.read_text())
        child = event["receiver_pid"]
        self.extra_pids.add(child)
        self.assertEqual(event["event"], "turn_finished")
        self.assertTrue(self.process_is_live(child), "the supervisor must finish before the slow hook")

    def test_notify_reaps_finished_children_without_waiting_for_live_ones(self):
        worker = self.worker()
        finished, running = mock.Mock(), mock.Mock()
        finished.poll.return_value = 0
        running.poll.return_value = None
        worker.NOTIFY_PROCESSES[:] = [finished, running]
        worker.reap_notifications()
        self.assertEqual(worker.NOTIFY_PROCESSES, [running])
        finished.poll.assert_called_once_with()
        running.poll.assert_called_once_with()
        finished.wait.assert_not_called()
        running.wait.assert_not_called()

    def test_macos_notify_escapes_quotes_backslashes_and_line_breaks(self):
        worker = self.worker()
        state = {"slug": "notify-macos", "repo": str(self.repo),
                 "branch": "codex/notify-macos", "account": "main", "worktree": str(self.repo)}
        (self.home / "claude-tasks" / state["slug"]).mkdir(parents=True)
        with mock.patch.object(worker.subprocess, "Popen") as popen:
            worker.notify(state, {"notify": "macos"}, "turn_finished",
                          'completed "quoted" C:\\tmp\nwith newline')
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [
            "osascript", "-e",
            'display notification "notify-macos: completed \\"quoted\\" C:\\\\tmp with newline" '
            'with title "codex-worker"'])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_review_selects_available_account_and_preserves_working_thread(self):
        self.configure()
        self.start("review-alone")
        self.wait("review-alone")
        working = self.state("review-alone")
        self.account(self.home, "A", self.reset + 7200)
        self.account(self.other, "B", self.reset,
                     runs=[{"mode": "complete", "reply": "Дефект: tracked.txt:1 — проверь содержимое."}])

        self.cli("review", "review-alone", "--effort", "xhigh")
        output = self.wait("review-alone")
        execution = self.review_invocations()[0]
        state = self.state("review-alone")
        task_dir = self.home / "claude-tasks" / "review-alone"
        self.assertEqual(execution["argv"][:2], ["exec", "review"])
        self.assertEqual(execution["argv"][execution["argv"].index("--base") + 1], working["base"])
        self.assertIn("--json", execution["argv"])
        self.assertIn('model_reasoning_effort="xhigh"', execution["argv"])
        self.assertNotIn("-C", execution["argv"])
        self.assertEqual(execution["cwd"], working["worktree"])
        self.assertEqual(execution["home"], str(self.other))
        self.assertEqual(execution["output"], str(task_dir / "review.md"))
        self.assertIn(str(task_dir / "brief.md"), execution["prompt"])
        self.assertIn("Готово, когда", execution["prompt"])
        self.assertFalse(execution["resume"])
        self.assertNotEqual(execution["thread"], working["thread"])
        self.assertEqual((state["thread"], state["account"], state["threads"]),
                         (working["thread"], working["account"], working["threads"]))
        self.assertEqual(len(state["reviews"]), 1)
        review = state["reviews"][0]
        self.assertEqual((review["account"], review["thread"]), ("second", execution["thread"]))
        self.assertLessEqual(review["started"], review["finished"])
        self.assertRegex(output, r"== review by second \(\d+(?:\.\d+)? min\):")
        self.assertIn((task_dir / "review.md").read_text().strip(), output)
        self.assertNotIn("Результат A", output)
        self.assertIn("== commits on ", output)

    def test_start_review_runs_in_same_supervisor_and_say_resumes_working_thread(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Рабочий ход завершён"},
            {"mode": "complete", "reply": "Ревью завершено: дефектов нет"},
            {"mode": "complete", "reply": "Исправление завершено"}])
        self.start("review-start", "--review")
        output = self.wait("review-start")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertFalse(executions[0]["review"])
        self.assertTrue(executions[1]["review"])
        self.assertFalse(executions[1]["resume"])
        self.assertEqual(executions[0]["ppid"], executions[1]["ppid"])
        self.assertNotEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertIn("Рабочий ход завершён", output)
        self.assertIn("Ревью завершено: дефектов нет", output)
        self.assertIn("== review by main (", output)
        self.assertLess(output.index("Рабочий ход завершён"), output.index("== review by main ("))

        self.cli("say", "review-start", "Проверь ещё один случай")
        followup = self.wait("review-start")
        execution = self.invocations()[-1]
        self.assertTrue(execution["resume"])
        self.assertEqual(execution["thread"], executions[0]["thread"])
        self.assertEqual(execution["prompt"], "Проверь ещё один случай")
        self.assertEqual(len(self.state("review-start")["reviews"]), 1)
        self.assertIn("Исправление завершено", followup)
        self.assertNotIn("== review by", followup)
        self.assertNotIn("Ревью завершено: дефектов нет", followup)

    def test_say_review_runs_after_completed_followup(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Начальный ход"},
            {"mode": "complete", "reply": "Итог нового хода"},
            {"mode": "complete", "reply": "Ревью нового хода"}])
        self.start("review-say")
        self.wait("review-say")
        self.cli("say", "review-say", "Доработай проверку", "--review")
        output = self.wait("review-say")
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        self.assertTrue(executions[1]["resume"])
        self.assertTrue(executions[2]["review"])
        self.assertEqual(executions[1]["ppid"], executions[2]["ppid"])
        self.assertIn("Итог нового хода", output)
        self.assertIn("Ревью нового хода", output)
        self.assertNotIn("Начальный ход", output)

    def test_fix_round_sends_findings_to_working_thread_and_carries_rejection_to_fresh_review(self):
        self.configure(language="en")
        self.configure_notify()
        review = self.review_result()
        decisions = [{"id": "F1", "decision": "fixed", "note": "Handle empty input before indexing."},
                     {"id": "F2", "decision": "rejected", "note": "The last token is a sentinel, not output."}]
        fixed = self.work_result(findings=decisions)
        clean = self.review_result(False)
        command = "printf 'passed\\n' >> check-runs"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review},
            {"mode": "complete", "reply": fixed},
            {"mode": "complete", "reply": clean}])

        self.start("fix-clean", "--review", "--fix-rounds", "1", "--check", command)
        output = self.wait("fix-clean")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, True])
        work, initial_review, correction, final_review = executions
        self.assertTrue(correction["resume"])
        self.assertEqual(correction["thread"], work["thread"])
        self.assertEqual(correction["argv"][:2], ["exec", "resume"])
        self.assertEqual(len({event["ppid"] for event in executions}), 1,
                         "one supervisor must finish the whole cycle before wait returns")
        for finding in review["findings"]:
            for field in ("id", "severity", "title", "detail", "verify"):
                self.assertIn(finding[field], correction["prompt"])
            self.assertIn("{file}:{line}".format(**finding), correction["prompt"])
        prompt = correction["prompt"].lower()
        for instruction in ("code", "test", "reject", "commit", "push"):
            self.assertIn(instruction, prompt)
        self.assertNotRegex(correction["prompt"], "[А-Яа-яЁё]")
        self.assertIn("F2", final_review["prompt"])
        self.assertIn(decisions[1]["note"], final_review["prompt"])
        self.assertRegex(final_review["prompt"].lower(), r"reason|rationale")
        for event in (initial_review, final_review):
            self.assertIn(command, event["prompt"])
            self.assertIn("passed", event["prompt"].lower())
            self.assertRegex(event["prompt"].lower(), r"(?:do not|don't).*rerun.*(?:full|entire)")
        self.assertFalse(final_review["resume"])
        self.assertEqual(len({work["thread"], initial_review["thread"], final_review["thread"]}), 3)
        for event in executions:
            self.assert_output_schema(event, "fix-clean", review=event["review"])
        state = self.state("fix-clean")
        self.assertEqual(state["fix_rounds"], 1)
        self.assertEqual(state["result"], fixed)
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["rounds"][0]["review"], review)
        self.assertEqual(state["rounds"][0]["decisions"], decisions)
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "passed")
        self.assertEqual(len(state["checks"]), 2)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(),
                         ["passed", "passed"])
        messages = [json.loads(line) for line in
                    (self.home / "claude-tasks" / "fix-clean" / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "review")
        self.assertEqual(messages[0]["text"], correction["prompt"])
        self.assertRegex(output, r"== round 1: 2 findings .*→ fixed 1, rejected 1")
        self.assertIn("F2", output)
        self.assertIn(decisions[1]["note"], output)
        self.assertIn("== check `" + command + "`: passed", output)
        self.assertIn(clean["summary"], output)
        self.assertRegex(output, r"== auto-fix: 1 rounds?, findings 2 → 0")
        self.assertIn("clean", output.lower())
        self.notification("fix-clean", "turn_finished")
        self.until(lambda: sum(event["event"] == "review_done" for event in self.notifications("fix-clean")) == 2)
        notifications = self.notifications("fix-clean")
        self.assertEqual([event["round"] for event in notifications if event["event"] == "review_done"],
                         [0, 1])
        self.assertEqual(sum(event["event"] == "turn_finished" for event in notifications), 1)

    def test_fix_round_limit_runs_exactly_two_corrections_and_prints_final_defects(self):
        self.configure()
        reviews = [self.review_result() for _ in range(3)]
        for number, review in enumerate(reviews):
            review["summary"] = "Review generation %s still has defects." % number
        reviews[-1]["findings"][0]["detail"] = "Final review detail must be printed completely."
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Updated the relevant code."}
                     for finding in reviews[0]["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": reviews[0]},
            {"mode": "complete", "reply": self.work_result(findings=decisions)},
            {"mode": "complete", "reply": reviews[1]},
            {"mode": "complete", "reply": self.work_result(findings=decisions)},
            {"mode": "complete", "reply": reviews[2]}])

        self.start("fix-bounded", "--review", "--fix-rounds", "2")
        output = self.wait("fix-bounded")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, True, False, True])
        self.assertEqual(len(self.review_invocations()), 3)
        self.assertEqual(len({event["thread"] for event in self.review_invocations()}), 3)
        self.assertTrue(all(event["resume"] and event["thread"] == executions[0]["thread"]
                            for event in (executions[2], executions[4])))
        state = self.state("fix-bounded")
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(state["reviews"][-1]["result"], reviews[-1])
        self.assertEqual(state["auto_fix_stop"], "rounds_exhausted")
        for finding in reviews[-1]["findings"]:
            self.assertIn("[{severity}] {id} {file}:{line} — {title}".format(**finding), output)
            self.assertIn(finding["detail"], output)
            self.assertIn(finding["verify"], output)
        self.assertIn(reviews[-1]["summary"], output)
        self.assertIn("== round 1:", output)
        self.assertIn("== round 2:", output)
        self.assertIn("== auto-fix: 2 rounds, findings 2 → 2 → 2", output)

    def test_second_review_limits_scope_and_only_p3_stops_further_fixes(self):
        for language in ("en", "ru"):
            with self.subTest(language=language):
                self.configure(language=language)
                (self.home / "exec-count").unlink(missing_ok=True)
                review = self.review_result()
                minor_review = self.review_result()
                minor_review["findings"] = [minor_review["findings"][1]]
                minor_review["summary"] = "Only a minor issue remains."
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": review},
                    {"mode": "complete", "reply": self.work_result(findings=[
                        {"id": "F1", "decision": "fixed", "note": "Fixed the acceptance criterion."},
                        {"id": "F2", "decision": "rejected", "note": "out of scope"}],
                        remaining=["Let Claude decide the optional edge case."])},
                    {"mode": "complete", "reply": minor_review}])
                before = len(self.invocations())
                slug = "fix-only-p3-" + language
                self.start(slug, "--review", "--fix-rounds", "2")
                output = self.wait(slug)
                executions = self.invocations()[before:]
                self.assertEqual([event["review"] for event in executions], [False, True, False, True])
                initial_review, correction, second_review = executions[1:]
                if language == "en":
                    for text in ("regressions introduced by the fixes", "acceptance criteria", "only as P3"):
                        self.assertIn(text, second_review["prompt"])
                    self.assertNotIn("From the second review onward", initial_review["prompt"])
                    self.assertIn("disproportionate complexity", correction["prompt"])
                else:
                    for text in ("регрессии, внесённые исправлениями", "«Готово, когда»", "только как P3"):
                        self.assertIn(text, second_review["prompt"])
                    self.assertNotIn("Начиная со второго ревью", initial_review["prompt"])
                    self.assertIn("непропорциональной сложности", correction["prompt"])
                self.assertIn("out of scope", correction["prompt"])
                self.assertIn("remaining", correction["prompt"])
                state = self.state(slug)
                self.assertEqual(state["auto_fix_stop"], "only P3")
                self.assertEqual(len(state["rounds"]), 1)
                self.assertEqual(state["reviews"][-1]["result"], minor_review)
                self.assertIn("only P3", output)
                self.assertIn(minor_review["findings"][0]["detail"], output)

    def test_initial_review_with_only_p3_does_not_start_a_fix_round(self):
        self.configure()
        review = self.review_result()
        for finding in review["findings"]:
            finding["severity"] = "P3"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review}])
        self.start("fix-initial-p3", "--review", "--fix-rounds", "2")
        output = self.wait("fix-initial-p3")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        state = self.state("fix-initial-p3")
        self.assertEqual(state["auto_fix_stop"], "only P3")
        self.assertEqual(state["rounds"], [])
        self.assertIn("only P3", output)
        for finding in review["findings"]:
            self.assertIn(finding["detail"], output)

    def test_say_can_start_a_new_fix_cycle_and_default_say_does_not_inherit_it(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(summary="Initial work")},
            {"mode": "complete", "reply": self.work_result(summary="Requested followup")},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "reply": self.work_result(findings=[
                {"id": "F1", "decision": "fixed", "note": "Updated parser"},
                {"id": "F2", "decision": "fixed", "note": "Updated formatter"}])},
            {"mode": "complete", "reply": self.review_result(False)},
            {"mode": "complete", "reply": self.work_result(summary="Ordinary followup")}])
        self.start("fix-say")
        self.wait("fix-say")
        thread = self.state("fix-say")["thread"]
        self.cli("say", "fix-say", "Finish the followup", "--review", "--fix-rounds", "1")
        output = self.wait("fix-say")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, False, True, False, True])
        self.assertTrue(executions[1]["resume"])
        self.assertTrue(executions[3]["resume"])
        self.assertEqual(executions[3]["thread"], thread)
        self.assertEqual(len(self.state("fix-say")["rounds"]), 1)
        self.assertRegex(output, r"== auto-fix: 1 rounds?, findings 2 → 0")
        self.cli("say", "fix-say", "One ordinary final followup")
        output = self.wait("fix-say")
        self.assertEqual(len(self.invocations()), 6)
        self.assertEqual(len(self.review_invocations()), 2)
        self.assertEqual(self.state("fix-say")["fix_rounds"], 0)
        self.assertNotIn("== auto-fix:", output)
        self.assertNotIn("== round ", output)
        self.assertIn("Ordinary followup", output)

    def test_failed_check_after_fix_uses_last_forty_lines_for_next_round_then_reviews(self):
        self.configure()
        review = self.review_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review},
            {"mode": "complete", "create_files": ["fail-check"], "reply": self.work_result(findings=[
                {"id": "F1", "decision": "fixed", "note": "First fix"},
                {"id": "F2", "decision": "fixed", "note": "Second fix"}])},
            {"mode": "complete", "remove_files": ["fail-check"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = ("if test -f fail-check; then i=1; while [ \"$i\" -le 45 ]; "
                   "do printf 'retry-line-%02d\\n' \"$i\"; i=$((i + 1)); done; "
                   "printf 'retry-stderr\\n' >&2; exit 1; fi; printf 'check-passed\\n'")
        self.start("fix-check-retry", "--review", "--fix-rounds", "2", "--check", command)
        output = self.wait("fix-check-retry")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, False, True])
        retry = executions[3]
        self.assertTrue(retry["resume"])
        self.assertEqual(retry["thread"], executions[0]["thread"])
        self.assertIn("check.log", retry["prompt"])
        self.assertIn("retry-line-07", retry["prompt"])
        self.assertIn("retry-line-45", retry["prompt"])
        self.assertIn("retry-stderr", retry["prompt"])
        self.assertNotIn("retry-line-06", retry["prompt"])
        for finding in review["findings"]:
            self.assertNotIn(finding["title"], retry["prompt"], "retry must focus on the failed check")
        state = self.state("fix-check-retry")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "failed", "passed"])
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "failed")
        self.assertIsNone(state["rounds"][1]["review"])
        self.assertEqual(state["rounds"][1]["check"]["outcome"], "passed")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertIn("== auto-fix: 2 rounds", output)
        self.assertIn("== round 2:", output)
        self.assertIn("FAILED", output)
        self.assertIn("No defects found.", output)

    def test_failed_check_on_last_fix_round_does_not_start_another_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "create_files": ["fail-check"], "reply": self.work_result()}])
        command = "if test -f fail-check; then printf 'last-round-check-failure\\n'; exit 1; fi"
        self.start("fix-check-exhausted", "--review", "--fix-rounds", "1", "--check", command)
        output = self.wait("fix-check-exhausted")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False])
        state = self.state("fix-check-exhausted")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "failed"])
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "failed")
        self.assertIn("check", state["auto_fix_stop"])
        self.assertIn("last-round-check-failure", output)
        self.assertIn("FAILED", output)
        self.assertRegex(output, r"== auto-fix: 1 rounds?")
        self.assertIn("Found two defects.", output, "the last completed review must remain visible")

    def test_incomplete_fix_results_stop_the_loop_and_print_questions(self):
        self.configure()
        for status in ("needs_decision", "blocked", "partial"):
            with self.subTest(status=status):
                (self.home / "exec-count").unlink(missing_ok=True)
                questions = ["Which public API should accept the new argument?"]
                result = self.work_result(status, questions=questions, remaining=["Wait for the API decision."])
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": self.review_result()},
                    {"mode": "complete", "reply": result}])
                slug = "fix-incomplete-" + status.replace("_", "-")
                before = len(self.invocations())
                self.start(slug, "--review", "--fix-rounds", "2")
                output = self.wait(slug)
                self.assertEqual(len(self.invocations()) - before, 3)
                state = self.state(slug)
                self.assertEqual(state["result"], result)
                self.assertEqual(state["auto_fix_stop"], "result " + status)
                self.assertEqual(len(state["rounds"]), 1)
                self.assertIn(questions[0], output)
                self.assertIn(result["remaining"][0], output)
                self.assertRegex(output, r"== auto-fix: 1 rounds?")

    def test_stop_during_fix_round_stops_cycle_before_more_checks_or_reviews(self):
        self.configure()
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "hold"}])
        self.start("fix-stopped", "--review", "--fix-rounds", "2", "--check",
                   "printf 'check\\n' >> check-runs")
        self.until(lambda: len(self.invocations()) == 3)
        correction = self.invocations()[-1]
        self.assertTrue(correction["resume"])
        self.cli("stop", "fix-stopped", timeout=20)
        output = self.wait("fix-stopped")
        state = self.state("fix-stopped")
        self.assertEqual(state["outcome"], "stopped")
        self.assertEqual(state["auto_fix_stop"], "stopped")
        self.assertEqual(len(self.invocations()), 3)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(), ["check"])
        self.assertIn("stopped", output)
        self.notification("fix-stopped", "turn_finished")
        self.assertEqual(sum(event["event"] == "turn_finished" for event in self.notifications("fix-stopped")), 1)

    def test_rotation_during_fix_hands_findings_to_new_account_and_finishes_cycle(self):
        self.configure()
        review = self.review_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review},
            {"mode": "usage_error"}])
        self.account(self.other, "B", self.reset + 3600, runs=[
            {"mode": "complete", "reply": self.work_result(findings=[
                {"id": "F1", "decision": "fixed", "note": "Fixed after account rotation"},
                {"id": "F2", "decision": "fixed", "note": "Also fixed after account rotation"}])},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("fix-rotation", "--review", "--fix-rounds", "1")
        output = self.wait("fix-rotation")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, False, True])
        interrupted, replacement, final_review = executions[2:]
        self.assertTrue(interrupted["resume"])
        self.assertEqual(interrupted["thread"], executions[0]["thread"])
        self.assertFalse(replacement["resume"])
        self.assertNotEqual(replacement["thread"], interrupted["thread"])
        self.assertEqual(replacement["home"], str(self.other))
        self.assertEqual(final_review["home"], str(self.other))
        for finding in review["findings"]:
            for field in ("id", "severity", "title", "detail", "verify"):
                self.assertIn(finding[field], replacement["prompt"])
            self.assertIn("{file}:{line}".format(**finding), replacement["prompt"])
        self.assertRegex(interrupted["prompt"], "[А-Яа-яЁё]")
        state = self.state("fix-rotation")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["account"], "second")
        self.assertEqual(len(state["rounds"]), 1, "rotation must not spend another fix round")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertIn("== rotated main → second:", output)
        self.assertIn("No defects found.", output)

    def test_zero_fix_rounds_keeps_one_review_without_a_correction(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()}])
        self.start("fix-zero", "--review", "--fix-rounds", "0")
        output = self.wait("fix-zero")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertEqual(self.state("fix-zero")["fix_rounds"], 0)
        self.assertFalse(self.state("fix-zero").get("rounds"))
        self.assertNotIn("== auto-fix:", output)

    def test_fix_rounds_validate_review_requirement_and_range_before_starting_or_saying(self):
        self.configure()
        self.start("fix-validation")
        self.wait("fix-validation")
        state = self.state("fix-validation")
        before = len(self.invocations())
        for options in (("--fix-rounds", "0"), ("--fix-rounds", "1"),
                        ("--review", "--fix-rounds", "-1"), ("--review", "--fix-rounds", "6"),
                        ("--review", "--fix-rounds", "1.5")):
            for command in (("start", "fix-invalid", "--brief", str(self.brief), "--repo", str(self.repo)),
                            ("say", "fix-validation", "Invalid fix cycle")):
                with self.subTest(command=command[0], options=options):
                    result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(command + options),
                                            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("fix-rounds", result.stdout + result.stderr)
                    self.assertEqual(len(self.invocations()), before)
                    self.assertEqual(self.state("fix-validation"), state)
                    self.assertFalse((self.home / "claude-tasks" / "fix-invalid").exists())
        self.assertFalse((self.home / "claude-tasks" / "fix-validation" / "messages.jsonl").exists())

    def test_reconnect_errors_do_not_fail_a_completed_turn(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Ход после переподключений", "reconnects": 2},
            {"mode": "complete", "reply": "Ревью после переподключений"}])
        self.start("review-reconnect", "--review")
        output = self.wait("review-reconnect")
        self.assertEqual(self.state("review-reconnect")["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        self.assertIn("Ревью после переподключений", output)

    def test_failed_working_turn_does_not_start_automatic_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "failed"}])
        self.start("review-failed", "--review", "--check", "touch unexpected-check")
        output = self.cli("wait", "review-failed", "--timeout", "10")
        self.assertEqual(self.state("review-failed")["outcome"], "failed")
        self.assertIn("fixture task failed", output)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertFalse(self.state("review-failed").get("reviews"))
        self.assertFalse(self.state("review-failed").get("checks"))
        self.assertFalse((Path(self.state("review-failed")["worktree"]) / "unexpected-check").exists())
        self.assertNotIn("== review by", output)

    def test_stopped_working_turn_does_not_start_automatic_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("review-stopped", "--review", "--check", "touch unexpected-check")
        self.until(lambda: bool(self.invocations()))
        self.cli("stop", "review-stopped", timeout=20)
        self.wait("review-stopped")
        self.assertEqual(self.state("review-stopped")["outcome"], "stopped")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertFalse(self.state("review-stopped").get("checks"))
        self.assertFalse((Path(self.state("review-stopped")["worktree"]) / "unexpected-check").exists())

    def test_paused_working_turn_does_not_start_automatic_review(self):
        self.configure(wait_for_reset=False)
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("review-paused", "--review", "--check", "touch unexpected-check")
        output = self.wait("review-paused")
        self.assertEqual(self.state("review-paused")["outcome"], "exhausted")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertNotIn("== review by", output)
        self.assertFalse(self.state("review-paused").get("checks"))
        self.assertFalse((Path(self.state("review-paused")["worktree"]) / "unexpected-check").exists())

    def test_review_requires_idle_task(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("review-idle")
        self.until(lambda: bool(self.invocations()))
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "review", "review-idle"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("running", result.stderr)
        self.assertFalse(self.review_invocations())
        self.assertEqual(self.state("review-idle")["turns"], 1)
        self.cli("stop", "review-idle", timeout=20)
        self.wait("review-idle")

    def test_review_limit_restarts_fresh_on_another_account(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Рабочий результат"},
            {"mode": "limit", "rollout_used": 99, "messages": ["Частичная находка"]}])
        self.account(self.other, "B", self.reset + 3600,
                     runs=[{"mode": "complete", "reply": "Полное ревью на другой подписке"}])
        self.start("review-limit")
        self.wait("review-limit")
        working = self.state("review-limit")
        self.cli("review", "review-limit")
        output = self.wait("review-limit")
        executions = self.review_invocations()
        self.assertEqual(len(executions), 2)
        self.assertEqual([entry["home"] for entry in executions], [str(self.home), str(self.other)])
        self.assertNotEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertTrue(all(not entry["resume"] for entry in executions))
        self.assertEqual(executions[0]["prompt"], executions[1]["prompt"])
        self.assertNotIn("Предыдущий исполнитель", executions[1]["prompt"])
        self.assertNotIn("Частичная находка", executions[1]["prompt"])
        state = self.state("review-limit")
        self.assertEqual([entry["account"] for entry in state["reviews"]], ["main", "second"])
        self.assertTrue(all(entry.get("started") and entry.get("finished") for entry in state["reviews"]))
        self.assertEqual((state["account"], state["thread"]), (working["account"], working["thread"]))
        self.assertIn("== review by second (", output)
        self.assertIn("Полное ревью на другой подписке", output)
        self.assertNotIn("Рабочий результат", output)

    def test_review_waits_for_account_reset_then_starts_fresh(self):
        self.configure()
        self.start("review-reset")
        self.wait("review-reset")
        working = self.state("review-reset")
        reset = time.time() + 4
        self.account(self.home, "A", reset + 20, used=99)
        self.account(self.other, "B", reset, used=99, auto_reset=True)
        self.cli("review", "review-reset")
        first = self.wait("review-reset")
        self.assert_sleeping("review-reset", first, reset, account="second")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())

        second = self.wait("review-reset", timeout=10, through_sleep=True)
        execution = self.review_invocations()[0]
        self.assertEqual(execution["home"], str(self.other))
        self.assertFalse(execution["resume"])
        self.assertEqual(self.state("review-reset")["thread"], working["thread"])
        self.assertIn("== review by second (", second)
        self.assertIn("Результат B", second)

    def test_stop_review_uses_supervisor_and_keeps_working_thread(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete"}, {"mode": "hold"}])
        self.start("review-stop")
        self.wait("review-stop")
        working = self.state("review-stop")
        self.cli("review", "review-stop")
        self.until(lambda: bool(self.review_invocations()))
        self.assertIn("running", self.cli("status"))
        self.cli("stop", "review-stop", timeout=20)
        self.wait("review-stop")
        state = self.state("review-stop")
        review = self.review_invocations()[0]
        self.assertEqual(state["outcome"], "stopped")
        self.assertFalse(state.get("pid"))
        self.assertEqual((state["account"], state["thread"]), (working["account"], working["thread"]))
        self.assertTrue(state["reviews"][0].get("finished"))
        self.assertTrue(any(event.get("command") == "signal" and event.get("thread") == review["thread"]
                            and event.get("signal") == signal.SIGINT for event in self.trace()))

    def test_review_falls_back_when_base_and_prompt_conflict(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Рабочий результат"},
            {"mode": "complete", "reply": "Ревью через обычный exec"}])
        self.start("review-fallback")
        self.wait("review-fallback")
        fixture = json.loads((self.home / "fixture.json").read_text())
        fixture["review_prompt_conflict"] = True
        (self.home / "fixture.json").write_text(json.dumps(fixture))
        working = self.state("review-fallback")
        self.cli("review", "review-fallback")
        output = self.wait("review-fallback")
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        rejected, fallback = executions[1:]
        self.assertEqual(rejected["mode"], "review_prompt_conflict")
        self.assertFalse(rejected["thread"])
        self.assertFalse(fallback["review"])
        self.assertFalse(fallback["resume"])
        self.assertEqual(fallback["cwd"], working["worktree"])
        self.assertEqual(Path(fallback["output"]).name, "review.md")
        self.assertEqual(fallback["prompt"], rejected["prompt"])
        self.assert_output_schema(rejected, "review-fallback", review=True)
        self.assert_output_schema(fallback, "review-fallback", review=True)
        self.assertIn("git diff " + working["base"], fallback["prompt"])
        self.assertEqual(fallback["ppid"], rejected["ppid"])
        state = self.state("review-fallback")
        self.assertEqual(state["thread"], working["thread"])
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(state["reviews"][0]["thread"], fallback["thread"])
        self.assertIn("== review by main (", output)
        self.assertIn("Ревью через обычный exec", output)
        self.assertNotIn("== errors:", output)

    def test_mid_turn_limit_sleeps_and_resumes_same_thread(self):
        self.configure()  # Omitting wait_for_reset enables automatic continuation.
        self.assertTrue(self.worker().accounts_config()["wait_for_reset"])
        reset = time.time() + 4
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "usage_error", "messages": ["Работа до лимита"]},
            {"mode": "complete", "reply": "Продолжение после сброса завершено"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("reset-resume")
        first = self.wait("reset-resume")
        # A loaded CI worker can wake before wait or the assertions run.
        # The recorded sleep proves the pause without requiring it to persist.
        sleeps = [entry for entry in self.state("reset-resume")["rotations"]
                  if entry.get("event") == "sleep"]
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0]["until"], reset + 0.1, places=3)
        self.assertEqual(sleeps[0]["account"], "main")
        self.assertTrue(sleeps[0]["reason"])
        self.assertIn("== sleeping until ", first)

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-resume")
        executions = self.invocations()
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assertEqual(executions[1]["home"], str(self.home))
        for execution in executions:
            self.assert_output_schema(execution, "reset-resume")
        self.assertEqual(executions[1]["prompt"],
                         "Лимит подписки сбросился. Продолжай с того места, где остановился.")
        self.assertIn("Продолжение после сброса завершено", second)
        self.assertNotIn("auto-continue at", second)
        state = self.state("reset-resume")
        self.assertEqual(state["outcome"], "completed")
        self.assertFalse(state.get("sleeping"))
        self.assertEqual([entry["event"] for entry in state["rotations"]], ["sleep", "wake"])

    def test_exhausted_say_sleeps_and_sends_its_original_message(self):
        self.configure()
        self.start("reset-say")
        self.wait("reset-say")
        original_thread = self.state("reset-say")["thread"]
        reset = time.time() + 4
        self.account(self.home, "A", reset, used=99, auto_reset=True,
                     runs=[{"mode": "complete", "reply": "Новое сообщение выполнено"}])
        self.account(self.other, "B", self.reset, used=99)
        message = "Выполни именно это сообщение после сброса"
        self.cli("say", "reset-say", message)
        first = self.wait("reset-say")
        self.assert_sleeping("reset-say", first, reset)
        self.assertEqual(len(self.invocations()), 1, "no exec may run before a subscription resets")
        self.assertIn("(Codex did not run this turn)", first)

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-say")
        execution = self.invocations()[-1]
        self.assertTrue(execution["resume"])
        self.assertEqual(execution["thread"], original_thread)
        self.assertEqual(execution["prompt"], message)
        self.assertIn("Новое сообщение выполнено", second)
        self.assertEqual(self.state("reset-say")["outcome"], "completed")

    def test_first_reset_on_other_account_starts_new_thread_with_handoff(self):
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", reset + 20, auto_reset=True,
                     runs=[{"mode": "usage_error", "messages": ["Незавершённая работа A"]}])
        self.account(self.other, "B", reset, used=99, auto_reset=True)
        self.start("reset-switch")
        first = self.wait("reset-switch")
        self.assert_sleeping("reset-switch", first, reset, account="second")
        self.assertEqual(len(self.invocations()), 1)

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-switch")
        prompt = self.assert_rotation("reset-switch", second)
        self.assertIn(self.brief.read_text().strip(), prompt)
        self.assertIn("Предыдущий исполнитель остановился посреди работы", prompt)
        self.assertIn("Незавершённая работа A", prompt)
        self.assertEqual(self.state("reset-switch")["outcome"], "completed")

    def test_initially_exhausted_start_keeps_brief_and_wait_can_follow_sleep(self):
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", reset, used=99, auto_reset=True)
        self.account(self.other, "B", self.reset, used=99)
        self.start("reset-start")
        first = self.wait("reset-start")
        self.assert_sleeping("reset-start", first, reset)
        self.assertFalse(self.invocations())

        second = self.wait("reset-start", through_sleep=True, timeout=10)
        self.assertEqual(len(self.invocations()), 1)
        execution = self.invocations()[0]
        self.assertFalse(execution["resume"])
        expected = (self.home / "claude-tasks" / "reset-start" / "brief.md").read_text()
        self.assertEqual(execution["prompt"], expected)
        self.assertIn("Результат A", second)
        self.assertNotIn("auto-continue at", second)
        self.assertEqual(self.state("reset-start")["outcome"], "completed")

    def test_stop_during_sleep_cancels_automatic_exec(self):
        self.env["CODEX_TASK_SLEEP_STEP"] = "60"
        self.configure()
        self.account(self.home, "A", self.reset, used=99, auto_reset=True)
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("stop-sleep")
        self.assert_sleeping("stop-sleep", self.wait("stop-sleep"), self.reset)
        self.assertFalse(self.invocations())
        self.cli("stop", "stop-sleep", timeout=20)
        self.wait("stop-sleep")
        state = self.state("stop-sleep")
        self.assertEqual(state["outcome"], "stopped")
        self.assertFalse(state.get("pid"))
        self.assertFalse(state.get("sleeping"))
        self.assertFalse(self.invocations())

    def test_say_during_sleep_requires_stop_first(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("say-sleep")
        state = self.assert_sleeping("say-sleep", self.wait("say-sleep"), self.reset)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "say", "say-sleep", "Не отправлять"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        expected = datetime.datetime.fromtimestamp(state["sleeping"]["until"]).strftime("%Y-%m-%d %H:%M")
        self.assertIn("waiting for limit reset until " + expected +
                      "; stop first to send a new message", result.stderr)
        self.assertFalse((self.home / "claude-tasks" / "say-sleep" / "messages.jsonl").exists())
        self.assertEqual(self.state("say-sleep")["turns"], 1)
        self.assertFalse(self.invocations())
        self.cli("stop", "say-sleep", timeout=20)
        self.wait("say-sleep")

    def test_delayed_reset_sleeps_again_before_continuing(self):
        self.configure()
        reset = time.time() + 3
        release = reset + 3
        self.account(self.home, "A", reset, auto_reset=True, release_at=release, runs=[
            {"mode": "usage_error"}, {"mode": "complete", "reply": "Задержанный сброс завершён"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("delayed-reset")
        self.assert_sleeping("delayed-reset", self.wait("delayed-reset"), reset)
        self.until(lambda: len([entry for entry in self.state("delayed-reset")["rotations"]
                               if entry.get("event") == "sleep"]) >= 2, timeout=8)
        retry = self.wait("delayed-reset")
        self.assert_sleeping("delayed-reset", retry, release)
        self.assertEqual(len(self.invocations()), 1)
        self.assertTrue(any(event.get("command") == "quota-snapshot" and
                            event["primary"]["usedPercent"] == 99 and
                            event["primary"]["resetsAt"] == release for event in self.trace()))

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        output = self.wait("delayed-reset")
        self.assertIn("Задержанный сброс завершён", output)
        self.assertEqual(self.invocations()[0]["thread"], self.invocations()[1]["thread"])
        self.assertTrue(self.invocations()[1]["resume"])
        state = self.state("delayed-reset")
        self.assertEqual(state["outcome"], "completed")
        sleeps = [entry for entry in state["rotations"] if entry.get("event") == "sleep"]
        self.assertEqual(len(sleeps), 2)
        self.assertLess(sleeps[0]["until"], sleeps[1]["until"])

    def test_mid_turn_rotation_preserves_work_and_followups(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "OLD-OUTPUT-OUTSIDE-LAST-THREE"},
            {"mode": "complete", "reply": "First follow-up completed"},
            {"mode": "limit", "modify": True,
             "messages": ["Последняя мысль один", "Последняя мысль два", "Последняя мысль три"]}])
        self.start("mid-turn")
        self.wait("mid-turn")
        self.cli("say", "mid-turn", "Первое сообщение Claude")
        self.wait("mid-turn")
        self.cli("say", "mid-turn", "Второе сообщение Claude")
        self.until(lambda: any(event.get("command") == "quota-written" for event in self.trace()))
        output = self.wait("mid-turn")
        prompt = self.assert_rotation("mid-turn", output)
        self.assertIn(self.brief.read_text().strip(), prompt)
        self.assertIn("Продолжение", prompt)
        self.assertIn("Первое сообщение Claude", prompt)
        self.assertIn("Второе сообщение Claude", prompt)
        self.assertLess(prompt.index("Первое сообщение Claude"), prompt.index("Второе сообщение Claude"))
        self.assertIn("checkpoint from account A", prompt)
        self.assertIn(" M tracked.txt", prompt)
        self.assertIn("?? pending.txt", prompt)
        self.assertIn("git status", prompt)
        self.assertIn("git diff", prompt)
        for message in ("Последняя мысль один", "Последняя мысль два", "Последняя мысль три"):
            self.assertIn(message, prompt)
        self.assertNotIn("OLD-OUTPUT-OUTSIDE-LAST-THREE", prompt)
        interrupted_thread = self.invocations()[-2]["thread"]
        self.assertTrue(any(event.get("command") == "signal" and event.get("thread") == interrupted_thread
                            and event.get("signal") == signal.SIGINT for event in self.trace()))
        messages = [json.loads(line) for line in
                    (self.home / "claude-tasks" / "mid-turn" / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertIn("Первое сообщение Claude", json.dumps(messages[0], ensure_ascii=False))
        self.assertIn("Второе сообщение Claude", json.dumps(messages[1], ensure_ascii=False))

    def test_usage_limit_failure_rotates_to_fresh_thread(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.start("usage-error")
        output = self.wait("usage-error")
        self.assert_rotation("usage-error", output)
        self.assertEqual(len(self.invocations()), 2)

    def test_all_accounts_exhausted_records_earliest_reset(self):
        self.configure(credits=False, wait_for_reset=False)
        self.account(self.home, "A", self.reset + 3600, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("all-limited")
        output = self.wait("all-limited")
        state = self.state("all-limited")
        self.assertTrue(state.get("exhausted"))
        self.assertFalse(state.get("rotations"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("== all accounts at limit; earliest reset", output)
        self.assertIn(datetime.datetime.fromtimestamp(self.reset).strftime("%Y-%m-%d %H:%M"), output)
        self.assertIn("(second)", output)
        self.assertIn("continue with say after that", output)

    def test_all_accounts_exhausted_before_start_has_no_previous_executor(self):
        self.configure(wait_for_reset=False)
        self.account(self.home, "A", self.reset + 3600, used=99)
        self.account(self.other, "B", self.reset, used=99)
        self.start("initially-limited")
        output = self.wait("initially-limited")
        self.assertIn("== all accounts at limit; earliest reset", output)
        self.assertTrue(self.state("initially-limited").get("exhausted"))
        self.assertFalse(self.state("initially-limited")["threads"])
        self.assertFalse(self.invocations())

        self.account(self.other, "B", self.reset)
        message = "Продолжить после первого сброса лимита"
        self.cli("say", "initially-limited", message)
        self.wait("initially-limited")
        self.assertEqual(self.state("initially-limited")["account"], "second")
        self.assertEqual(len(self.invocations()), 1)
        prompt = self.invocations()[0]["prompt"]
        self.assertIn(self.brief.read_text().strip(), prompt)
        self.assertIn("### Текущее сообщение — выполни его\n\n" + message, prompt)
        self.assertNotIn("Предыдущий исполнитель остановился посреди работы", prompt)

    def test_start_selects_earlier_reset_of_longest_window(self):
        self.configure()
        first = self.account(self.home, "A", self.reset + 7200)
        first["secondary"]["resetsAt"] = self.reset - 100
        (self.home / "fixture.json").write_text(json.dumps(first))
        self.account(self.other, "B", self.reset)
        self.start("choose-reset")
        self.wait("choose-reset")
        self.assertEqual(self.state("choose-reset")["account"], "second")
        self.assertEqual(self.invocations()[0]["home"], str(self.other))
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(self.other.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.other / "config.toml").read_bytes(), (self.home / "config.toml").read_bytes())
        for name in ("AGENTS.md", "skills", "rules"):
            self.assertTrue((self.other / name).is_symlink(), name)
            self.assertEqual((self.other / name).resolve(), (self.home / name).resolve())

    def test_pause_after_a_turn_does_not_repeat_the_old_result(self):
        self.configure(wait_for_reset=False)
        self.start("pause-later")
        first = self.wait("pause-later")
        old = first.split("min\n", 1)[1].split("\n==", 1)[0].strip()
        self.assertTrue(old)
        self.account(self.home, "A", self.reset + 3600, used=99)
        self.account(self.other, "B", self.reset, used=99)
        self.cli("say", "pause-later", "Ещё одно сообщение")
        output = self.wait("pause-later")
        self.assertIn("== all accounts at limit", output)
        self.assertIn("(Codex did not run this turn)", output)
        self.assertNotIn(old, output)

    def test_say_rotates_when_current_account_is_exhausted(self):
        self.configure()
        self.start("say-switch")
        self.wait("say-switch")
        self.cli("say", "say-switch", "Предыдущее сообщение Claude")
        self.wait("say-switch")
        self.account(self.home, "A", self.reset, used=99)
        self.cli("say", "say-switch", "Новое сообщение при смене подписки")
        output = self.wait("say-switch")
        prompt = self.assert_rotation("say-switch", output)
        self.assertIn("Предыдущий исполнитель остановился посреди работы", prompt)
        self.assertIn("Предыдущее сообщение Claude", prompt)
        self.assertIn("### Текущее сообщение — выполни его\n\nНовое сообщение при смене подписки", prompt)
        self.assertLess(prompt.index("Предыдущее сообщение Claude"),
                        prompt.index("Текущее сообщение — выполни его"))
        self.assertEqual(len(self.invocations()), 3)

    def test_without_accounts_file_resumes_legacy_thread(self):
        self.start("legacy")
        self.wait("legacy")
        state_path = self.home / "claude-tasks" / "legacy" / "state.json"
        state = json.loads(state_path.read_text())
        original_thread = state["thread"]
        state.pop("account", None)
        state.pop("threads", None)
        state_path.write_text(json.dumps(state))
        self.cli("say", "legacy", "Продолжить старую задачу")
        self.wait("legacy")
        invocations = self.invocations()
        self.assertEqual(len(invocations), 2)
        self.assertFalse(invocations[0]["resume"])
        self.assertTrue(invocations[1]["resume"])
        self.assertEqual(invocations[1]["thread"], original_thread)
        self.assertEqual(invocations[1]["prompt"], "Продолжить старую задачу")
        self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        self.assertFalse(self.state("legacy").get("rotations"))

    def test_stop_interrupts_child_without_rotation(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("stop-turn")
        self.until(lambda: bool(self.invocations()))
        self.cli("stop", "stop-turn", timeout=20)
        self.wait("stop-turn")
        state = self.state("stop-turn")
        self.assertFalse(state.get("rotations"))
        self.assertFalse(state.get("exhausted"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertTrue(any(event.get("command") == "signal" and event.get("signal") == signal.SIGINT
                            for event in self.trace()))

    def test_credits_true_keeps_the_same_live_exec(self):
        self.configure(credits=True)
        self.account(self.home, "A", self.reset,
                     runs=[{"mode": "hold", "rollout_used": 99}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("use-credits")
        # Two probes before exec, and two more when the local rollout reaches 99.
        self.until(lambda: len([event for event in self.trace()
                               if event.get("command") == "app-server"]) >= 4)
        state = self.state("use-credits")
        self.assertTrue(state.get("pid"))
        os.kill(state["pid"], 0)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(state.get("rotations"))
        self.assertFalse(state.get("exhausted"))
        self.assertFalse(any(event.get("command") == "signal" for event in self.trace()))
        self.cli("stop", "use-credits", timeout=20)
        self.wait("use-credits")
        self.assertEqual(len(self.invocations()), 1)

    def test_credits_false_stops_a_live_exhausted_exec(self):
        self.configure(credits=False, wait_for_reset=False)
        self.account(self.home, "A", self.reset,
                     runs=[{"mode": "hold", "rollout_used": 99}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("no-credits")
        output = self.wait("no-credits")
        self.assertTrue(self.state("no-credits").get("exhausted"))
        self.assertFalse(self.state("no-credits").get("rotations"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("all accounts at limit", output)
        self.assertTrue(any(event.get("command") == "signal" and event.get("signal") == signal.SIGINT
                            for event in self.trace()))

    def test_accounts_identifies_duplicates_missing_login_and_rpc_error(self):
        self.configure()
        self.account(self.other, "A", self.reset)
        config_path = self.home / "claude-accounts.json"
        config = json.loads(config_path.read_text())
        for name, changes in (("logged-out", {"account": None}),
                              ("rpc-error", {"rpc_error": True})):
            home = self.root / name
            home.mkdir()
            fixture = self.account(home, name, self.reset)
            fixture.update(changes)
            (home / "fixture.json").write_text(json.dumps(fixture))
            config["accounts"].append({"name": name, "home": str(home)})
        config_path.write_text(json.dumps(config))
        output = self.cli("accounts")
        rows = {line.split("\t")[0]: line for line in output.splitlines()[1:]}
        self.assertIn("a@example.test", rows["main"])
        self.assertIn("pro", rows["main"])
        self.assertIn("123.45", rows["main"])
        self.assertIn("available", rows["main"])
        self.assertIn("same account as main", rows["second"])
        self.assertIn("not logged in", rows["logged-out"])
        self.assertIn("unknown", rows["rpc-error"])
        self.assertNotIn("not logged in", rows["rpc-error"])
        self.assertFalse(self.invocations())

    def test_account_read_distinguishes_explicit_logout_from_rpc_errors_and_timeouts(self):
        worker = self.worker()
        account = {"name": "main", "home": str(self.home)}
        cases = (("logged-out", [{"account": None}], False),
                 ("read-error", [RuntimeError("temporary account RPC unavailable")], None),
                 ("read-timeout", [TimeoutError("account RPC timed out")], None),
                 ("quota-error", [{"account": {"type": "chatgpt"}},
                                   RuntimeError("temporary quota RPC unavailable")], None))
        for name, replies, expected in cases:
            with self.subTest(case=name):
                with mock.patch.object(worker, "account_rpc") as account_rpc:
                    account_rpc.return_value.__enter__.return_value.side_effect = replies
                    info = worker.account_read(account)
                self.assertIs(info["logged_in"], expected)
                if expected is None:
                    self.assertTrue(info.get("error"))
                else:
                    self.assertFalse(info.get("error"))
        self.assertFalse(self.trace())

    def test_unknown_account_rpc_status_sleeps_then_recovers_without_requiring_login(self):
        self.env["CODEX_TASK_ACCOUNT_RETRY_SECONDS"] = "4"
        self.configure()
        for home in (self.home, self.other):
            path = home / "fixture.json"
            fixture = json.loads(path.read_text())
            fixture["rpc_error"] = True
            path.write_text(json.dumps(fixture))
        self.start("unknown-account-recovery")
        first = self.wait("unknown-account-recovery")
        state = self.state("unknown-account-recovery")
        self.assertEqual(state["outcome"], "sleeping")
        self.assertLessEqual(state["sleeping"]["until"] - time.time(), 4)
        self.assertFalse(state.get("needs_login"))
        self.assertFalse(self.invocations())
        self.assertNotIn("needs login", first)
        for home in (self.home, self.other):
            path = home / "fixture.json"
            fixture = json.loads(path.read_text())
            fixture.pop("rpc_error")
            path.write_text(json.dumps(fixture))
        output = self.wait("unknown-account-recovery", through_sleep=True, timeout=10)
        state = self.state("unknown-account-recovery")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("Результат A", output)
        self.assertFalse(state.get("needs_login"))

    def test_unknown_account_status_retries_six_times_at_five_minute_intervals(self):
        worker = self.worker()
        self.assertEqual(worker.ACCOUNT_RETRY_SECONDS, 300)
        self.assertEqual(worker.MAX_ACCOUNT_RETRIES, 6)
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        sleeps = []
        st = {"slug": "unknown-account-budget", "account": "main", "thread": "", "rotations": []}
        unknown = [{"name": "main", "logged_in": None, "error": "temporary account RPC unavailable"},
                   {"name": "second", "logged_in": None, "error": "temporary account RPC unavailable"}]

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep), \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots", return_value=unknown) as snapshots:
            chosen = worker.wait_for_account(st, unknown, cfg, "temporary account RPC unavailable", lambda: False)
        self.assertIsNone(chosen)
        self.assertEqual(snapshots.call_count, 6)
        self.assertEqual(sum(sleeps), 6 * 300)
        self.assertTrue(all(0 < seconds <= 60 for seconds in sleeps))
        self.assertEqual(st["outcome"], "failed")
        self.assertIn("6", st["error"])
        self.assertRegex(st["error"], r"(?i)(temporar|unavailable|unknown|retr)")
        self.assertFalse(st.get("needs_login"))

    def test_stop_during_unknown_account_retry_does_not_probe_or_require_login(self):
        worker = self.worker()
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        stopped = [False]
        st = {"slug": "unknown-account-stop", "account": "main", "thread": "", "rotations": []}
        unknown = [{"name": "main", "logged_in": None, "error": "temporary account RPC unavailable"}]

        def sleep(seconds):
            clock[0] += seconds
            stopped[0] = True

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep) as sleeper, \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots") as snapshots:
            chosen = worker.wait_for_account(st, unknown, cfg, "temporary account RPC unavailable", lambda: stopped[0])
        self.assertIsNone(chosen)
        sleeper.assert_called_once_with(60)
        snapshots.assert_not_called()
        self.assertFalse(st.get("needs_login"))

    def test_signal_group_falls_back_to_a_live_leader_on_permission_error(self):
        worker = self.worker()
        for live in (True, False):
            with self.subTest(live=live):
                proc = mock.Mock(pid=12345)
                proc.poll.return_value = None if live else 0
                with mock.patch.object(worker.os, "killpg", side_effect=PermissionError), \
                        mock.patch.object(worker.os, "kill") as kill:
                    self.assertIs(worker.signal_group(proc, 0), live)
                    kill.assert_not_called()
                    self.assertIs(worker.signal_group(proc, signal.SIGTERM), live)
                    if live:
                        kill.assert_called_once_with(proc.pid, signal.SIGTERM)
                    else:
                        kill.assert_not_called()

    def test_reset_credit_settings_default_to_manual_use_and_twelve_hours(self):
        self.configure()
        cfg = self.worker().accounts_config()
        self.assertIs(cfg["use_reset_credits"], False)
        self.assertEqual(cfg["reset_credit_min_hours"], 12)

    def test_free_reset_uses_earliest_expiry_across_accounts_and_hands_off_without_sleep(self):
        self.configure(use_reset_credits=True)
        self.configure_notify()
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, runs=[{
            "mode": "usage_error", "messages": ["Незавершённая работа перед бесплатным сбросом"]}])
        self.account(self.other, "B", reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_later", reset + 10 * 86400))
        credit = self.reset_credit("RateLimitResetCredit_earliest", reset + 86400)
        self.reset_credits(self.other, credit,
                           self.reset_credit("RateLimitResetCredit_last", reset + 20 * 86400))
        self.start("free-reset-switch")
        output = self.wait("free-reset-switch")
        prompt = self.assert_rotation("free-reset-switch", output)
        self.assertIn("Незавершённая работа перед бесплатным сбросом", prompt)
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["home"], str(self.other))
        self.assertEqual(calls[0]["params"]["creditId"], credit["id"])
        key = calls[0]["params"]["idempotencyKey"]
        self.assertEqual(uuid.UUID(key).version, 4)
        attempts = [entry for entry in calls[0]["journal_before"] if entry["event"] == "attempt"]
        self.assertEqual(len(attempts), 1, "persist the request before spending a reset")
        self.assertEqual((attempts[0]["slug"], attempts[0]["account"]), ("free-reset-switch", "second"))
        self.assertEqual((attempts[0]["creditId"], attempts[0]["idempotencyKey"]), (credit["id"], key))
        self.assertIsInstance(attempts[0]["at"], (int, float))
        self.assertEqual([entry["outcome"] for entry in self.reset_journal() if entry["event"] == "result"],
                         ["success"])
        state = self.state("free-reset-switch")
        resets = [entry for entry in state["rotations"] if entry.get("event") == "reset_credit"]
        self.assertEqual(len(resets), 1)
        self.assertEqual((resets[0]["account"], resets[0]["credit_id"], resets[0]["expires_at"]),
                         ("second", credit["id"], credit["expiresAt"]))
        self.assertFalse(any(entry.get("event") == "sleep" for entry in state["rotations"]))
        self.assertIn("== used a free limit reset on second (" + credit["id"] + "), expires ", output)
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(self.notification("free-reset-switch", "reset_credit_used")["account"], "second")

    def test_free_reset_on_current_account_resumes_its_existing_thread(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, runs=[
            {"mode": "usage_error"}, {"mode": "complete", "reply": "Бесплатный сброс помог"}])
        self.account(self.other, "B", reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_current", reset + 86400))
        self.start("free-reset-resume")
        output = self.wait("free-reset-resume")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertEqual(executions[1]["prompt"],
                         "Лимит подписки сбросился. Продолжай с того места, где остановился.")
        self.assertIn("Бесплатный сброс помог", output)
        self.assertEqual(len(self.consume_calls()), 1)
        self.assertFalse(any(entry.get("event") == "sleep"
                             for entry in self.state("free-reset-resume")["rotations"]))

    def test_automatic_reset_rechecks_quota_under_lock_and_keeps_the_credit_if_available(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        credit = self.reset_credit("RateLimitResetCredit_keep_after_fresh_read", reset + 86400)
        self.reset_credits(self.home, credit, clear_quota_when_reset_locked=True)
        self.start("fresh-quota-before-consume")
        output = self.wait("fresh-quota-before-consume")
        self.assertEqual(self.state("fresh-quota-before-consume")["outcome"], "completed")
        self.assertIn("Результат A", output)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.consume_calls())
        self.assertFalse(self.reset_journal())
        snapshots = [event["primary"]["usedPercent"] for event in self.trace()
                     if event.get("command") == "quota-snapshot" and event["home"] == str(self.home)]
        self.assertIn(99, snapshots)
        self.assertIn(0, snapshots)
        self.assertLess(snapshots.index(99), snapshots.index(0))
        fixture = json.loads((self.home / "fixture.json").read_text())
        self.assertEqual(fixture["rateLimitResetCredits"]["credits"][0]["status"], "available")
        self.assertFalse(any(entry.get("event") == "sleep"
                             for entry in self.state("fresh-quota-before-consume")["rotations"]))

    def test_disabled_free_resets_keep_the_original_sleep(self):
        self.configure(use_reset_credits=False)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_disabled", reset + 86400))
        self.start("free-reset-disabled")
        self.assert_sleeping("free-reset-disabled", self.wait("free-reset-disabled"), reset)
        self.assertFalse(self.consume_calls())
        self.assertFalse(self.invocations())

    def test_near_natural_reset_keeps_the_credit_and_sleeps(self):
        self.configure(use_reset_credits=True, reset_credit_min_hours=12)
        self.account(self.home, "A", self.reset, used=99)
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_keep", self.reset + 86400))
        self.start("free-reset-too-soon")
        self.assert_sleeping("free-reset-too-soon", self.wait("free-reset-too-soon"), self.reset)
        self.assertFalse(self.consume_calls())

    def test_reset_credit_error_or_unchanged_quota_sleeps_after_one_attempt(self):
        for slug, changes in (("free-reset-rpc-error", {"consume_error": True}),
                              ("free-reset-no-quota", {"consume_no_reset": True})):
            with self.subTest(slug=slug):
                before = len(self.consume_calls())
                self.configure(use_reset_credits=True)
                reset = int(time.time()) + 30 * 3600
                self.account(self.home, "A", reset, used=99)
                self.account(self.other, "B", reset + 3600, used=99)
                self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_" + slug, reset + 86400),
                                   self.reset_credit("RateLimitResetCredit_spare_" + slug, reset + 2 * 86400),
                                   **changes)
                self.start(slug)
                self.assert_sleeping(slug, self.wait(slug), reset)
                self.assertEqual(len(self.consume_calls()) - before, 1, "one reset attempt per sleep episode")
                self.cli("stop", slug, timeout=20)
                self.wait(slug)

    def test_reset_credit_timeout_retries_with_the_original_idempotency_key(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        credit = self.reset_credit("RateLimitResetCredit_unknown", self.reset + 86400)
        self.reset_credits(self.home, credit, consume_timeout=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        first = self.consume_calls()
        self.assertEqual(len(first), 1)
        self.assertTrue(any(entry.get("outcome") == "unknown" for entry in self.reset_journal()))
        path = self.home / "fixture.json"
        fixture = json.loads(path.read_text())
        fixture["consume_timeout"] = False
        path.write_text(json.dumps(fixture))
        self.cli("reset-credit", "main", "--yes")
        calls = self.consume_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["params"], calls[1]["params"])
        self.assertTrue(calls[1]["params"]["idempotencyKey"])
        results = [entry["outcome"] for entry in self.reset_journal() if entry["event"] == "result"]
        self.assertEqual(results, ["unknown", "success"])

    def test_reset_credit_reconciles_an_applied_timeout_without_spending_the_next_credit(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        first = self.reset_credit("RateLimitResetCredit_applied", self.reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_spare", self.reset + 2 * 86400)
        self.reset_credits(self.home, first, spare, consume_timeout_applied=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        self.assertEqual(len(self.consume_calls()), 1)
        self.assertEqual(self.consume_calls()[0]["params"]["creditId"], first["id"])
        preview = self.cli("reset-credit", "main")
        self.assertTrue(preview)
        self.assertEqual(len(self.consume_calls()), 1, "a preview cannot retry or spend another reset")
        before = len(self.trace())
        output = self.cli("reset-credit", "main", "--yes")
        self.assertEqual(len(self.consume_calls()), 1, "resolve the unfinished reset before selecting the spare")
        results = [entry for entry in self.reset_journal()
                   if entry.get("event") == "result" and entry.get("creditId") == first["id"]]
        self.assertEqual(results[-1]["outcome"], "consumed (inferred)")
        fixture = json.loads((self.home / "fixture.json").read_text())
        credits = {credit["id"]: credit for credit in fixture["rateLimitResetCredits"]["credits"]}
        self.assertEqual(credits[first["id"]]["status"], "consumed")
        self.assertEqual(credits[spare["id"]]["status"], "available")
        self.assertIn("WEEKLY %", output)
        self.assertIn("main", output)
        self.assertTrue(any(event.get("command") == "quota-snapshot" and event["primary"]["usedPercent"] == 0
                            for event in self.trace()[before:]), "print fresh limits after reconciliation")

    def test_automatic_run_reconciles_an_applied_timeout_before_executing_available_quota(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        first = self.reset_credit("RateLimitResetCredit_auto_applied", reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_auto_spare", reset + 2 * 86400)
        self.reset_credits(self.home, first, spare, consume_timeout_applied=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        self.start("reconcile-before-exec")
        output = self.wait("reconcile-before-exec")
        self.assertEqual(self.state("reconcile-before-exec")["outcome"], "completed")
        self.assertIn("Результат A", output)
        self.assertEqual(len(self.consume_calls()), 1)
        executions = self.invocations()
        self.assertEqual(len(executions), 1)
        inferred = [entry for entry in executions[0]["journal_before"]
                    if entry.get("creditId") == first["id"] and entry.get("event") == "result"
                    and entry.get("outcome") == "consumed (inferred)"]
        self.assertEqual(len(inferred), 1, "resolve the old request even when the account already has quota")
        fixture = json.loads((self.home / "fixture.json").read_text())
        spare_status = next(credit["status"] for credit in fixture["rateLimitResetCredits"]["credits"]
                            if credit["id"] == spare["id"])
        self.assertEqual(spare_status, "available")

    def test_automatic_inferred_reset_with_exhausted_quota_sleeps_without_spending_a_spare(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        first = self.reset_credit("RateLimitResetCredit_inferred_no_quota", reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_unused_spare", reset + 2 * 86400)
        self.reset_credits(self.home, first, spare, consume_timeout_applied=True, consume_no_reset=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        self.start("inferred-reset-no-quota")
        output = self.wait("inferred-reset-no-quota")
        self.assert_sleeping("inferred-reset-no-quota", output, reset)
        self.assertEqual(len(self.consume_calls()), 1, "inferring the first reset uses the one action before sleep")
        self.assertEqual(self.consume_calls()[0]["params"]["creditId"], first["id"])
        inferred = [entry for entry in self.reset_journal()
                    if entry.get("creditId") == first["id"] and entry.get("event") == "result"
                    and entry.get("outcome") == "consumed (inferred)"]
        self.assertEqual(len(inferred), 1)
        fixture = json.loads((self.home / "fixture.json").read_text())
        credits = {credit["id"]: credit for credit in fixture["rateLimitResetCredits"]["credits"]}
        self.assertEqual(credits[first["id"]]["status"], "consumed")
        self.assertEqual(credits[spare["id"]]["status"], "available")
        self.assertEqual(fixture["primary"]["usedPercent"], 99)
        self.assertFalse(self.invocations())

    def test_automatic_pending_reset_reuses_its_key_before_considering_an_earlier_spare(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        pending = self.reset_credit("RateLimitResetCredit_pending", reset + 10 * 86400)
        spare = self.reset_credit("RateLimitResetCredit_earlier_spare", reset + 86400)
        self.reset_credits(self.home, pending, spare, consume_timeout=True)
        key = str(uuid.uuid4())
        intent = {"slug": "interrupted-task", "account": "main", "creditId": pending["id"],
                  "idempotencyKey": key, "at": time.time()}
        journal = [dict(intent, event="attempt"), dict(intent, event="result", outcome="unknown")]
        (self.home / "codex-worker-resets.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in journal))
        preview = self.cli("reset-credit", "main")
        self.assertIn(pending["id"], preview)
        self.assertFalse(self.consume_calls(), "previewing an unfinished available reset cannot retry it")
        self.assertEqual(self.reset_journal(), journal)
        self.start("reconcile-pending-reset")
        output = self.wait("reconcile-pending-reset")
        self.assert_sleeping("reconcile-pending-reset", output, reset)
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1, "reconciling a pending request consumes the one attempt before sleep")
        self.assertEqual(calls[0]["params"], {"creditId": pending["id"], "idempotencyKey": key})
        self.assertFalse(self.invocations())
        fixture = json.loads((self.home / "fixture.json").read_text())
        self.assertTrue(all(credit["status"] == "available" for credit in fixture["rateLimitResetCredits"]["credits"]))

    def test_reset_journal_unterminated_or_truncated_tail_preserves_the_next_attempt(self):
        self.configure()
        journal = self.home / "codex-worker-resets.jsonl"
        old_attempt = {"event": "result", "outcome": "success", "slug": "old-task", "account": "main", "at": time.time(),
                       "creditId": "RateLimitResetCredit_old", "idempotencyKey": str(uuid.uuid4())}
        tails = {"unterminated": json.dumps(old_attempt),
                 "truncated": json.dumps(old_attempt) + '\n{"event":"attempt","creditId":"partial'}
        for name, tail in tails.items():
            with self.subTest(tail=name):
                before = len(self.consume_calls())
                journal.write_text(tail)
                self.account(self.home, "A", self.reset, used=99)
                credit = self.reset_credit("RateLimitResetCredit_tail_" + name, self.reset + 86400)
                self.reset_credits(self.home, credit, consume_timeout=True)
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                        cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("timed out", result.stdout + result.stderr)
                calls = self.consume_calls()[before:]
                self.assertEqual(len(calls), 1)
                attempts = [entry for entry in calls[0]["journal_before"]
                            if entry.get("event") == "attempt" and entry.get("creditId") == credit["id"]]
                self.assertEqual(len(attempts), 1, "a partial final line must not swallow the new durable intent")
                self.assertEqual(attempts[0]["idempotencyKey"], calls[0]["params"]["idempotencyKey"])
                self.assertIn(old_attempt, self.reset_journal(), "preserve the preceding valid record")
                fixture_path = self.home / "fixture.json"
                fixture = json.loads(fixture_path.read_text())
                fixture["consume_timeout"] = False
                fixture_path.write_text(json.dumps(fixture))
                self.cli("reset-credit", "main", "--yes")
                calls = self.consume_calls()[before:]
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0]["params"], calls[1]["params"])
                attempts = [entry for entry in self.reset_journal()
                            if entry.get("event") == "attempt" and entry.get("creditId") == credit["id"]]
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0]["idempotencyKey"], attempts[1]["idempotencyKey"])

    def test_reset_credit_reuses_a_persisted_attempt_without_a_result_after_a_crash(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        credit = self.reset_credit("RateLimitResetCredit_crashed", self.reset + 86400)
        self.reset_credits(self.home, credit)
        key = str(uuid.uuid4())
        attempt = {"event": "attempt", "slug": "interrupted-task", "account": "main", "at": time.time(),
                   "creditId": credit["id"], "idempotencyKey": key}
        (self.home / "codex-worker-resets.jsonl").write_text(json.dumps(attempt) + "\n")
        self.cli("reset-credit", "main", "--yes")
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"], {"creditId": credit["id"], "idempotencyKey": key})
        self.assertEqual(len([entry for entry in calls[0]["journal_before"] if entry["event"] == "attempt"]), 2)
        results = [entry for entry in self.reset_journal() if entry["event"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["outcome"], results[0]["idempotencyKey"]), ("success", key))

    def test_reset_credit_can_be_cancelled_while_another_process_holds_the_journal_lock(self):
        journal = self.home / "codex-worker-resets.jsonl"
        holder_code = """import fcntl
import select
import sys
with open(sys.argv[1], "a+") as journal:
    fcntl.flock(journal, fcntl.LOCK_EX)
    print("locked", flush=True)
    select.select([sys.stdin], [], [], 5)
"""
        holder = subprocess.Popen([PYTHON, "-I", "-c", holder_code, str(journal)],
                                  cwd=self.repo, env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline(), "locked\n")
            worker = self.worker()
            cancelled = [False]

            def cancel_while_waiting(seconds):
                cancelled[0] = True

            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(worker.time, "sleep", side_effect=cancel_while_waiting), \
                    mock.patch.object(worker, "account_rpc") as account_rpc:
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    worker.consume_reset_credit({"name": "main", "home": str(self.home)},
                                                self.reset_credit("RateLimitResetCredit_cancelled", self.reset),
                                                slug="cancelled-reset", cancelled=lambda: cancelled[0])
            self.assertTrue(cancelled[0], "the operation must wait for the contended lock")
            account_rpc.assert_not_called()
            self.assertEqual(journal.read_text(), "", "cancel before recording an intent or sending an RPC")
            self.assertFalse(self.consume_calls())
            self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        finally:
            holder.communicate(timeout=5)

    def test_reset_credit_cli_previews_then_spends_only_with_yes(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        earliest = self.reset_credit("RateLimitResetCredit_preview", self.reset + 86400)
        later = self.reset_credit("RateLimitResetCredit_explicit", self.reset + 2 * 86400)
        self.reset_credits(self.home, later, earliest,
                           self.reset_credit("RateLimitResetCredit_expired", int(time.time()) - 100),
                           self.reset_credit("RateLimitResetCredit_used", self.reset + 100, status="consumed"))
        preview = self.cli("reset-credit", "main")
        self.assertIn(earliest["id"], preview)
        self.assertIn("Full reset", preview)
        self.assertFalse(self.consume_calls())
        self.assertFalse(self.reset_journal())
        output = self.cli("reset-credit", "main", "--yes")
        self.assertEqual(self.consume_calls()[0]["params"]["creditId"], earliest["id"])
        self.assertIn('"fixtureReceipt"', output)
        self.assertIn('"fixtureApplied"', output)
        self.assertIn("receipt-for-" + earliest["id"], output)
        trace = self.trace()
        position = next(i for i, event in enumerate(trace) if event.get("command") == "consume")
        self.assertTrue(any(event.get("command") == "quota-snapshot" and event["primary"]["usedPercent"] == 0
                            for event in trace[position + 1:]), "refresh limits after consuming the credit")
        self.assertIn("main", output)
        self.cli("reset-credit", "main", "--credit", later["id"], "--yes")
        self.assertEqual(self.consume_calls()[-1]["params"]["creditId"], later["id"])
        self.assertEqual(len(self.consume_calls()), 2)

    def test_accounts_displays_available_resets_and_nearest_expiry(self):
        self.configure()
        expires = self.reset + 13 * 86400
        credits = [self.reset_credit("RateLimitResetCredit_one", expires),
                   self.reset_credit("RateLimitResetCredit_two", expires + 9 * 86400)]
        self.reset_credits(self.home, *credits)
        worker = self.worker()
        with mock.patch.dict(os.environ, self.env):
            info = worker.account_read({"name": "main", "home": str(self.home)})
        self.assertEqual(info["reset_credits"], {"availableCount": 2, "credits": credits})
        output = self.cli("accounts")
        lines = output.splitlines()
        headers = lines[0].split("\t")
        self.assertIn("RESETS", headers)
        index = headers.index("RESETS")
        rows = {line.split("\t")[0]: line.split("\t") for line in lines[1:]}
        expires_date = datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
        self.assertEqual(rows["main"][index], "2 (exp " + expires_date + ")")
        self.assertEqual(rows["second"][index], "0")

    def test_auth_race_retries_once_on_the_same_thread_in_both_languages(self):
        prompts = {"ru": "Ход прервался из-за ошибки входа. Продолжай с того места, где остановился.",
                   "en": "The turn was interrupted by an authentication error. Continue from where you stopped."}
        for language in ("ru", "en"):
            with self.subTest(language=language):
                self.configure(language=language)
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "auth_error"}, {"mode": "complete", "reply": "Recovered after authentication"}])
                before = len(self.invocations())
                slug = "auth-retry-" + language
                self.start(slug)
                output = self.wait(slug)
                executions = self.invocations()[before:]
                self.assertEqual(len(executions), 2)
                self.assertEqual(executions[0]["thread"], executions[1]["thread"])
                self.assertTrue(executions[1]["resume"])
                self.assertEqual(executions[1]["home"], str(self.home))
                for execution in executions:
                    self.assert_output_schema(execution, slug)
                self.assertEqual(executions[1]["prompt"], prompts[language])
                self.assertIn("Recovered after authentication", output)
                self.assertFalse(self.state(slug).get("needs_login"))
                self.assertFalse(self.state(slug)["rotations"])

    def test_successful_work_clears_consecutive_auth_failures_before_automatic_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "auth_error", "error": "Failed to refresh token"},
            {"mode": "complete", "reply": "Work completed after authentication recovery"},
            {"mode": "auth_error", "error": "401 Unauthorized"},
            {"mode": "complete", "reply": "Review completed after its own authentication recovery"}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("auth-work-review-counter", "--review")
        output = self.wait("auth-work-review-counter")
        state = self.state("auth-work-review-counter")
        self.assertEqual(state["outcome"], "completed")
        self.assertFalse(state.get("needs_login"))
        self.assertIn("Work completed after authentication recovery", output)
        self.assertIn("Review completed after its own authentication recovery", output)
        executions = self.invocations()
        self.assertEqual(len(executions), 4)
        self.assertEqual([execution["review"] for execution in executions], [False, False, True, True])
        for execution in executions:
            self.assert_output_schema(execution, "auth-work-review-counter", review=execution["review"])
        self.assertTrue(all(execution["home"] == str(self.home) for execution in executions))
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertFalse(any(entry.get("event") == "sleep" for entry in state["rotations"]))

    def test_auth_error_before_thread_started_retries_with_the_original_task_prompt(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "auth_error", "before_thread": True, "event": "error"},
            {"mode": "complete", "reply": "Original task completed after early authentication failure"}])
        self.start("auth-before-thread")
        output = self.wait("auth-before-thread")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertFalse(executions[0]["resume"])
        self.assertFalse(executions[1]["resume"], "the failed attempt never created a resumable thread")
        self.assertEqual(executions[1]["prompt"], executions[0]["prompt"])
        self.assertIn(self.brief.read_text().strip(), executions[1]["prompt"])
        self.assertEqual(executions[1]["home"], str(self.home))
        for execution in executions:
            self.assert_output_schema(execution, "auth-before-thread")
        self.assertIn("Original task completed after early authentication failure", output)
        state = self.state("auth-before-thread")
        self.assertEqual(len(state["threads"]), 1)
        self.assertEqual(state["thread"], executions[1]["thread"])
        self.assertFalse(state["rotations"])

    def test_auth_before_thread_then_quota_sleep_preserves_the_original_brief(self):
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "auth_error", "before_thread": True, "used_after": 99},
            {"mode": "complete", "reply": "Original brief completed after quota recovery"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("auth-before-thread-sleep")
        self.assert_sleeping("auth-before-thread-sleep", self.wait("auth-before-thread-sleep"), reset)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.state("auth-before-thread-sleep")["threads"])
        output = self.wait("auth-before-thread-sleep", through_sleep=True, timeout=10)
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertFalse(executions[1]["resume"])
        self.assertEqual(executions[1]["prompt"], executions[0]["prompt"])
        self.assertIn(self.brief.read_text().strip(), executions[1]["prompt"])
        self.assertIn("Original brief completed after quota recovery", output)
        self.assertEqual(self.state("auth-before-thread-sleep")["outcome"], "completed")

    def test_auth_logout_switches_account_and_wait_explains_login(self):
        self.configure()
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "auth_error", "account_after": None, "messages": ["Работа до ошибки входа"],
            "event": "error", "error": "OAuth refresh token was rejected"}])
        self.start("auth-logout")
        output = self.wait("auth-logout")
        prompt = self.assert_rotation("auth-logout", output)
        self.assertIn("Работа до ошибки входа", prompt)
        self.assertIn("== account main needs login: run login main", output)
        self.assertIn("main", self.state("auth-logout")["needs_login"])
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual(self.notification("auth-logout", "needs_login")["account"], "main")

    def test_say_reuses_a_logged_in_account_after_needs_login_is_cleared(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "auth_error", "account_after": None, "messages": ["Работа до потери входа"]}])
        self.start("auth-login-restored")
        first = self.wait("auth-login-restored")
        self.assert_rotation("auth-login-restored", first)
        self.assertIn("main", self.state("auth-login-restored")["needs_login"])
        self.account(self.home, "A", self.reset)
        self.account(self.other, "B", self.reset + 3600, used=99)
        message = "Продолжи на аккаунте с восстановленным входом"
        self.cli("say", "auth-login-restored", message)
        output = self.wait("auth-login-restored")
        state = self.state("auth-login-restored")
        self.assertEqual(state["account"], "main")
        self.assertNotIn("main", state.get("needs_login", []))
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        self.assertEqual(executions[-1]["home"], str(self.home))
        self.assertFalse(executions[-1]["resume"])
        self.assertIn(self.brief.read_text().strip(), executions[-1]["prompt"])
        self.assertIn(message, executions[-1]["prompt"])
        self.assertIn("Результат A", output)
        self.assertNotIn("== account main needs login", output)

    def test_second_consecutive_auth_failure_excludes_account_until_the_next_turn(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "auth_error", "error": "Failed to refresh token"},
            {"mode": "auth_error", "error": "401 Unauthorized"},
            {"mode": "complete", "reply": "This account must not get a third attempt"}])
        self.start("auth-twice")
        output = self.wait("auth-twice")
        self.assert_rotation("auth-twice", output)
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertTrue(executions[1]["resume"])
        self.assertIn("== account main needs login: run login main", output)
        self.assertIn("main", self.state("auth-twice").get("needs_login", []))
        self.account(self.home, "A", self.reset)
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.cli("say", "auth-twice", "Продолжи после нового опроса входа")
        self.wait("auth-twice")
        self.assertEqual(self.state("auth-twice")["account"], "main")
        self.assertNotIn("main", self.state("auth-twice").get("needs_login", []))
        self.assertEqual(len(self.invocations()), 4)

    def test_permanent_auth_failures_on_all_logged_in_accounts_stop_after_four_executions(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "auth_error", "error": "Failed to refresh token"}])
        self.account(self.other, "B", self.reset + 3600,
                     runs=[{"mode": "auth_error", "error": "401 Unauthorized"}])
        self.start("auth-all-permanent")
        output = self.cli("wait", "auth-all-permanent", "--timeout", "15")
        state = self.state("auth-all-permanent")
        self.assertEqual(state["outcome"], "failed")
        self.assertIn("all accounts fail with auth errors", state.get("error", "").lower())
        self.assertIn("run login main", output)
        self.assertIn("run login second", output)
        self.assertCountEqual(state.get("needs_login", []), ["main", "second"])
        self.assertFalse(state.get("sleeping"))
        executions = self.invocations()
        self.assertEqual(len(executions), 4)
        self.assertEqual([execution["home"] for execution in executions],
                         [str(self.home), str(self.home), str(self.other), str(self.other)])
        self.assertTrue(executions[1]["resume"])
        self.assertTrue(executions[3]["resume"])

    def test_auth_exclusion_applies_to_every_home_with_the_same_account_identity(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "auth_error", "error": "Failed to refresh token"}])
        self.account(self.other, "A", self.reset + 3600)
        self.start("auth-duplicate-identity")
        output = self.cli("wait", "auth-duplicate-identity", "--timeout", "10")
        state = self.state("auth-duplicate-identity")
        self.assertEqual(state["outcome"], "failed")
        self.assertIn("auth", state.get("error", "").lower())
        self.assertIn("run login main", output)
        self.assertFalse(state.get("sleeping"))
        self.assertFalse(any(entry.get("event") == "sleep" for entry in state["rotations"]))
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(all(execution["home"] == str(self.home) for execution in executions))
        self.assertTrue(executions[1]["resume"])

    def test_auth_transition_cap_stops_accounts_that_alternate_available_quota(self):
        worker = self.worker()
        cfg = {"enabled": True, "switch_at": 99, "credits": False, "wait_for_reset": True,
               "accounts": [{"name": "main", "home": str(self.home)},
                            {"name": "second", "home": str(self.other)}]}
        st = {"slug": "auth-transition-cap", "account": "main", "thread": "original-thread",
              "threads": [], "rotations": []}
        probes = [0]

        def snapshots(config, cancelled, prepare=False):
            probes[0] += 1
            return [{"name": account["name"], "home": account["home"], "logged_in": True,
                     "account_id": account["name"], "limits": {"primary": {
                         "used": 99 if probes[0] > 1 and account["name"] == st["account"] else 0,
                         "minutes": 10080, "reset": self.reset}}}
                    for account in cfg["accounts"]]

        def monitor(*args, **kwargs):
            self.assertLessEqual(probes[0], 6, "the auth transition cap must bound the loop")
            return "auth", None

        with mock.patch.object(worker, "snapshots", side_effect=snapshots), \
                mock.patch.object(worker, "monitor", side_effect=monitor) as attempts, \
                mock.patch.object(worker, "handoff", return_value="handoff with the original task"), \
                mock.patch.object(worker, "save"):
            worker.run_turn(st, cfg, "original prompt", None, False, lambda: False, lambda seconds: None)
        self.assertEqual(st["outcome"], "failed")
        self.assertLessEqual(attempts.call_count, 1 + 2 * len(cfg["accounts"]))
        self.assertLessEqual(len([entry for entry in st["rotations"] if "from" in entry]),
                             2 * len(cfg["accounts"]))
        self.assertIn("auth", st.get("error", "").lower())
        self.assertIn("run login", st.get("error", ""))

    def test_auth_logout_waits_only_for_the_other_logged_in_account(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "auth_error", "account_after": None,
            "error": "Your authentication session could not be refreshed automatically. "
                     "Please log out and sign in again."}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("auth-then-sleep")
        output = self.wait("auth-then-sleep")
        self.assert_sleeping("auth-then-sleep", output, self.reset + 3600, account="second")
        self.assertIn("== account main needs login: run login main", output)
        self.assertEqual(len(self.invocations()), 1)

    def test_explicit_logout_after_auth_excludes_the_account_through_a_quota_sleep(self):
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "auth_error", "account_after": None, "messages": ["Работа до явного выхода"]}])
        self.account(self.other, "B", reset, used=99, auto_reset=True)
        self.start("auth-logout-then-wake")
        first = self.wait("auth-logout-then-wake")
        self.assert_sleeping("auth-logout-then-wake", first, reset, account="second")
        self.assertEqual(len(self.invocations()), 1)
        self.account(self.home, "A", self.reset)
        output = self.wait("auth-logout-then-wake", through_sleep=True, timeout=10)
        prompt = self.assert_rotation("auth-logout-then-wake", output)
        self.assertIn("Работа до явного выхода", prompt)
        self.assertEqual(len(self.invocations()), 2)
        self.assertIn("main", self.state("auth-logout-then-wake").get("needs_login", []))
        self.assertEqual(self.state("auth-logout-then-wake")["outcome"], "completed")

    def test_auth_failure_with_every_account_logged_out_fails_clearly(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "auth_error", "account_after": None, "logout_other_homes": [str(self.other)],
            "error": "Your access token could not be refreshed. Please log out and sign in again."}])
        self.start("auth-all-gone")
        output = self.cli("wait", "auth-all-gone", "--timeout", "10")
        state = self.state("auth-all-gone")
        self.assertEqual(state["outcome"], "failed")
        self.assertRegex(state.get("error", ""), r"(?i)(log.?in|signed? in|authentication)")
        self.assertIn("== account main needs login: run login main", output)
        self.assertFalse(state.get("sleeping"))
        self.assertEqual(len(self.invocations()), 1)

    def test_auth_failure_in_single_account_mode_keeps_legacy_failure(self):
        (self.home / "codex-worker.json").write_text('{"language":"en"}')
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "auth_error", "error": "401 unauthorized"}])
        self.start("auth-single")
        output = self.cli("wait", "auth-single", "--timeout", "10")
        state = self.state("auth-single")
        self.assertEqual(state["outcome"], "failed")
        self.assertIn("401 unauthorized", output)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(state.get("needs_login"))
        self.assertFalse(state["rotations"])
        self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))

    def test_secondary_window_and_credit_helpers(self):
        # Helpers normalize both app-server's camelCase and rollout's snake_case.
        worker = self.worker()
        cfg = {"switch_at": 99}
        raw = {"primary": {"used_percent": 10, "window_minutes": 300, "resets_at": self.reset},
               "secondary": {"used_percent": 99, "window_minutes": 10080, "resets_at": self.reset + 3600},
               "credits": {"hasCredits": True, "balance": "12.34", "unlimited": False}}
        info = {"logged_in": True, "limits": worker.normalize_limits(raw)}
        self.assertTrue(worker.exhausted(info, cfg))
        self.assertEqual(worker.reset_time(info, cfg), self.reset + 3600)
        self.assertEqual(worker.longest_window(info)["reset"], self.reset + 3600)
        info["limits"]["primary"].update(used=100, reset=self.reset + 7200)
        self.assertEqual(worker.reset_time(info, cfg), self.reset + 7200)
        for credit, expected in (({"hasCredits": True, "balance": "12.34"}, True),
                                 ({"hasCredits": True, "balance": "0"}, False),
                                 ({"hasCredits": True, "balance": "invalid"}, False),
                                 ({"hasCredits": False, "balance": "12.34"}, False),
                                 ({"unlimited": True}, True)):
            with self.subTest(credits=credit):
                self.assertEqual(worker.has_credits({"limits": {"credits": credit}}), expected)
        clear = worker.normalize_limits({"primary": {"usedPercent": 3, "windowDurationMins": 10080,
                                                     "resetsAt": self.reset}})
        self.assertTrue(worker.exhausted({"limits": clear, "ordinary": False}, cfg))
        clear["reached"] = "workspace_member_credits_depleted"
        self.assertTrue(worker.exhausted({"limits": clear}, cfg))

    def test_unknown_stale_and_nearby_resets_wait_before_rechecking(self):
        worker = self.worker()
        cfg = {"wait_for_reset": True, "switch_at": 99}
        for reset, duration in ((None, 900), (90, 60), (110, 10)):
            with self.subTest(reset=reset):
                clock = [100.0]
                sleeps = []
                st = {"account": "main", "thread": "current-thread", "rotations": []}
                limited = {"name": "main", "logged_in": True, "limits": {"primary": {
                    "used": 99, "minutes": 10080, "reset": reset}}}
                ready = dict(limited, limits={"primary": {"used": 0, "minutes": 10080, "reset": 2000}})

                def sleep(seconds):
                    sleeps.append(seconds)
                    clock[0] += seconds

                with mock.patch.object(worker, "SLEEP_STEP", 60), \
                        mock.patch.object(worker, "RESET_MARGIN", 0), \
                        mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                        mock.patch.object(worker.time, "sleep", side_effect=sleep), \
                        mock.patch.object(worker, "save"), \
                        mock.patch.object(worker, "snapshots", return_value=[ready]) as snapshots:
                    chosen = worker.wait_for_account(st, [limited], cfg, "quota", lambda: False)
                self.assertIs(chosen, ready)
                self.assertEqual(sum(sleeps), duration)
                self.assertTrue(all(0 < seconds <= 60 for seconds in sleeps))
                self.assertEqual(st["rotations"][0]["until"], 100 + duration)
                self.assertEqual(st["reset_wait_seconds"], duration)
                snapshots.assert_called_once()

    def test_reset_wait_uses_wall_clock_after_mac_sleep_and_prefers_current_thread(self):
        worker = self.worker()
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [100.0]
        st = {"account": "main", "thread": "current-thread", "rotations": []}
        limited = {"name": "main", "logged_in": True, "limits": {"primary": {
            "used": 99, "minutes": 10080, "reset": 1000}}}
        current = dict(limited, limits={"primary": {"used": 0, "minutes": 10080, "reset": 9000}})
        other = dict(current, name="second", limits={"primary": {
            "used": 0, "minutes": 10080, "reset": 6000}})

        def mac_sleep(seconds):
            self.assertEqual(seconds, 60)
            clock[0] += 5000  # Wall time advances while the machine is asleep.

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker, "RESET_MARGIN", 120), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "monotonic", side_effect=AssertionError("use wall time")), \
                mock.patch.object(worker.time, "sleep", side_effect=mac_sleep) as sleeper, \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots", return_value=[other, current]):
            chosen = worker.wait_for_account(st, [limited], cfg, "quota", lambda: False)
        sleeper.assert_called_once_with(60)
        self.assertIs(chosen, current, "keep the existing thread when both subscriptions become available")
        self.assertEqual(st["rotations"][0]["until"], 1120)
        self.assertEqual(st["reset_wait_seconds"], 5000)
        self.assertEqual(st["outcome"], "running")
        self.assertFalse(st.get("sleeping"))

    def test_reset_wait_exhausts_eight_day_budget_across_sleep_episodes(self):
        worker = self.worker()
        self.assertEqual(worker.MAX_RESET_WAIT_SECONDS, 8 * 86400)
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        sleeps = []
        st = {"account": "main", "thread": "current-thread", "rotations": [],
              "reset_wait_seconds": worker.MAX_RESET_WAIT_SECONDS - 120}
        limited = {"name": "main", "logged_in": True, "limits": {"primary": {
            "used": 99, "minutes": 10080, "reset": 1060}}}

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker, "RESET_MARGIN", 0), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep), \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots", return_value=[limited]) as snapshots:
            chosen = worker.wait_for_account(st, [limited], cfg, "quota", lambda: False)
        self.assertIsNone(chosen)
        self.assertEqual(sleeps, [60, 60])
        self.assertEqual(st["reset_wait_seconds"], 8 * 86400)
        self.assertEqual(st["outcome"], "exhausted")
        self.assertIn("8 days", st["exhausted"]["reason"])
        self.assertFalse(st.get("sleeping"))
        self.assertEqual(len([entry for entry in st["rotations"] if entry["event"] == "sleep"]), 2)
        snapshots.assert_called_once()

    def test_status_and_wait_report_sleeping_supervisor_gone(self):
        worker = self.worker()
        st = {"slug": "gone-sleeper", "pid": 12345, "outcome": "sleeping", "rotations": [],
              "sleeping": {"until": self.reset, "account": "main", "reason": "quota"},
              "turns": 1, "branch": "test-branch", "base": "HEAD", "account": "main",
              "thread": "current-thread", "worktree": str(self.repo)}
        with mock.patch.object(worker, "load", side_effect=lambda slug: dict(st)), \
                mock.patch.object(worker, "alive", return_value=False), \
                mock.patch.object(worker, "save") as save, \
                mock.patch.object(worker, "last_message", return_value=""), \
                mock.patch.object(worker, "events", return_value=[]), \
                mock.patch.object(worker, "sh", return_value=""), \
                mock.patch("builtins.print") as printer:
            worker.cmd_status(SimpleNamespace(slug=st["slug"]))
            status = "\n".join(str(call.args[0]) for call in printer.call_args_list)
            self.assertIn("paused, supervisor gone; continue with say", status)
            printer.reset_mock()
            worker.cmd_wait(SimpleNamespace(slug=st["slug"], timeout=0, through_sleep=False))
            output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("== paused, supervisor gone; continue with say", output)
        self.assertNotIn("auto-continue at", output)
        self.assertIsNone(save.call_args[0][0]["pid"])

    def test_reset_wait_budget_includes_slow_account_probe(self):
        worker = self.worker()
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        st = {"account": "main", "thread": "current-thread", "rotations": [],
              "reset_wait_seconds": worker.MAX_RESET_WAIT_SECONDS - 90}
        limited = {"name": "main", "logged_in": True, "limits": {"primary": {
            "used": 99, "minutes": 10080, "reset": 1060}}}
        ready = dict(limited, limits={"primary": {"used": 0, "minutes": 10080, "reset": 2000}})

        def sleep(seconds):
            clock[0] += seconds

        def snapshots(config, cancelled, prepare=False):
            self.assertTrue(prepare)
            self.assertFalse(cancelled())
            clock[0] += 30
            self.assertTrue(cancelled(), "a slow quota probe must respect the same wait deadline")
            return [ready]

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker, "RESET_MARGIN", 0), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep), \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots", side_effect=snapshots):
            chosen = worker.wait_for_account(st, [limited], cfg, "quota", lambda: False)
        self.assertIsNone(chosen, "do not launch an executor once the wait budget has expired")
        self.assertEqual(st["reset_wait_seconds"], 8 * 86400)
        self.assertEqual(st["outcome"], "exhausted")
        self.assertIn("8 days", st["exhausted"]["reason"])

    def test_monitor_quota_cancellation_preserves_stop_priority_and_unrelated_errors(self):
        worker = self.worker()
        for case in ("budget", "budget-return", "stop", "error"):
            with self.subTest(case=case):
                clock = [0.0]
                stopped = [False]
                slug = "quota-cancel-" + case
                (worker.TASKS / slug).mkdir(parents=True)
                st = {"slug": slug, "thread": "", "worktree": str(self.repo),
                      "threads": [], "rotations": []}
                proc = mock.Mock(pid=12345)
                proc.poll.return_value = None
                proc.stdout.fileno.return_value = 99
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector
                selector.select.return_value = [(SimpleNamespace(fileobj=proc.stdout),
                                                 worker.selectors.EVENT_READ)]

                def on_limit(local, reason, allow_credits, cancelled):
                    self.assertFalse(cancelled())
                    self.assertFalse(allow_credits)
                    if case == "error":
                        raise RuntimeError("unrelated quota failure")
                    clock[0] = 61.0
                    stopped[0] = case == "stop"
                    self.assertTrue(cancelled())
                    if case == "budget-return":
                        return {"reason": reason}
                    raise RuntimeError("stopped")

                with mock.patch.object(worker, "prepare_home"), \
                        mock.patch.object(worker, "save"), \
                        mock.patch.object(worker, "events", return_value=[]), \
                        mock.patch.object(worker, "terminate") as terminate, \
                        mock.patch.object(worker.subprocess, "Popen", return_value=proc), \
                        mock.patch.object(worker.selectors, "DefaultSelector", return_value=selector), \
                        mock.patch.object(worker.os, "read", return_value=(
                            b'{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}\n')), \
                        mock.patch.object(worker.time, "monotonic", side_effect=lambda: clock[0]):
                    args = (st, {"enabled": True, "switch_at": 99},
                            {"name": "main", "home": str(self.home)}, "prompt", None,
                            lambda: stopped[0], on_limit)
                    if case == "error":
                        with self.assertRaisesRegex(RuntimeError, "unrelated quota failure"):
                            worker.monitor(*args, control=worker.TurnControl(1, 3))
                    else:
                        result = worker.monitor(*args, control=worker.TurnControl(1, 3))
                        self.assertEqual(result, ("stopped" if case == "stop" else "budget_exhausted", None))
                terminate.assert_called_with(proc)
                self.assertNotIn("codex_pid", st)

    def test_monitor_bounds_rollout_searches_and_reads_during_long_step(self):
        worker = self.worker()
        duration = 600.0
        rollout = self.root / "unchanged-rollout.jsonl"
        rollout.write_text("The rollout stays unchanged throughout this step.\n")
        for resumed in (True, False):
            with self.subTest(resumed=resumed):
                clock = [0.0]
                reads = []
                searches = []
                cached_rollout = mock.Mock(wraps=rollout)
                emitted = [False]
                slug = "poll-resume" if resumed else "poll-new"
                (worker.TASKS / slug).mkdir(parents=True)
                st = {"slug": slug, "thread": "same-thread" if resumed else "",
                      "worktree": str(self.repo), "threads": [], "rotations": []}
                proc = mock.Mock(pid=12345)
                proc.stdout.fileno.return_value = 99
                proc.poll.side_effect = lambda: 0 if clock[0] >= duration else None
                proc.wait.return_value = 0
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector

                def select(timeout):
                    clock[0] += timeout
                    if not emitted[0] or clock[0] >= duration:
                        return [(SimpleNamespace(fileobj=proc.stdout), worker.selectors.EVENT_READ)]
                    return []

                def read(fd, size):
                    if emitted[0]:
                        return b""
                    emitted[0] = True
                    return b'{"type":"thread.started","thread_id":"same-thread"}\n'

                def find_rollout(home, thread):
                    self.assertEqual(thread, "same-thread")
                    searches.append(clock[0])
                    return cached_rollout if resumed or clock[0] >= 11 else None

                def read_limits(path):
                    self.assertIs(path, cached_rollout)
                    reads.append(clock[0])
                    return worker.normalize_limits({"primary": {"usedPercent": 10,
                        "windowDurationMins": 10080, "resetsAt": self.reset}})

                selector.select.side_effect = select
                with mock.patch.object(worker, "prepare_home"), \
                        mock.patch.object(worker, "save"), \
                        mock.patch.object(worker, "events", return_value=[]), \
                        mock.patch.object(worker, "terminate"), \
                        mock.patch.object(worker.subprocess, "Popen", return_value=proc), \
                        mock.patch.object(worker.selectors, "DefaultSelector", return_value=selector), \
                        mock.patch.object(worker.os, "read", side_effect=read), \
                        mock.patch.object(worker.time, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(worker, "rollout_path", side_effect=find_rollout), \
                        mock.patch.object(worker, "rollout_limits", side_effect=read_limits):
                    result = worker.monitor(st, {"enabled": True, "switch_at": 99},
                                            {"name": "main", "home": str(self.home)}, "prompt", None,
                                            lambda: False, mock.Mock(side_effect=AssertionError("unexpected limit")))
                self.assertEqual(result, ("completed", None))
                if resumed:
                    self.assertEqual(len(searches), 1, "cache the existing rollout path")
                else:
                    self.assertGreaterEqual(len(searches), 2)
                    self.assertLessEqual(len(searches), 4, "stop searching after finding the rollout")
                    self.assertTrue(all(b - a >= 5 - 1e-8 for a, b in zip(searches, searches[1:])), searches)
                    self.assertTrue(reads, "read the newly discovered rollout")
                self.assertLessEqual(len(reads), int(duration / worker.RATE_POLL_SECONDS) + 1)
                self.assertLessEqual(cached_rollout.stat.call_count,
                                     int(duration / worker.RATE_POLL_SECONDS) + 2)
                self.assertTrue(all(b - a >= worker.RATE_POLL_SECONDS - 1e-8
                                    for a, b in zip(reads, reads[1:])), reads)

    def test_wait_polls_each_second_and_clears_dead_legacy_pid(self):
        worker = self.worker()
        st = {"slug": "legacy-pid", "pid": 12345, "rotations": [], "turns": 1,
              "branch": "test-branch", "base": "HEAD", "worktree": str(self.repo)}
        running = [True]

        def sleep(seconds):
            self.assertEqual(seconds, 1)
            running[0] = False

        with mock.patch.object(worker, "load", side_effect=lambda slug: dict(st)), \
                mock.patch.object(worker, "alive", side_effect=lambda pid: bool(pid) and running[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep) as sleeper, \
                mock.patch.object(worker, "save") as save, \
                mock.patch.object(worker, "last_message", return_value=""), \
                mock.patch.object(worker, "events", return_value=[]), \
                mock.patch.object(worker, "sh", return_value=""), \
                mock.patch("builtins.print"):
            worker.cmd_wait(SimpleNamespace(slug=st["slug"], timeout=0))
        sleeper.assert_called_once_with(1)
        save.assert_called_once()
        self.assertIsNone(save.call_args[0][0].get("pid"))

    def test_stats_and_clean_include_review_threads_and_account_homes(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.start("all-threads")
        self.wait("all-threads")
        self.account(self.home, "A", self.reset)
        self.cli("review", "all-threads")
        self.wait("all-threads")
        state = self.state("all-threads")
        entries = state["threads"] + state["reviews"]
        homes = {"main": self.home, "second": self.other}
        worker = self.worker()
        self.assertEqual(worker.STATS, SCRIPT.with_name("codex-stats.py"))
        with mock.patch.object(worker.subprocess, "run") as run, mock.patch("builtins.print") as printer:
            worker.cmd_stats(SimpleNamespace(slug="all-threads"))
        files = []
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], [PYTHON, "-I", str(worker.STATS)])
            files.append(Path(call.args[0][3]))
        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertEqual(len(files), 3)
        for entry in entries:
            path = next(path for path in files if entry["thread"] in path.name)
            self.assertTrue(path.is_file())
            self.assertIn(entry["thread"], path.name)
            self.assertEqual(path.parents[4], homes[entry["account"]])
            self.assertIn(entry["account"], output)
        self.cli("clean", "all-threads")
        archived = [(event["home"], event["thread"]) for event in self.trace()
                    if event.get("command") == "archive"]
        self.assertCountEqual(archived, [(str(homes[entry["account"]]), entry["thread"])
                                        for entry in entries])
        self.assertFalse(Path(state["worktree"]).exists())
        self.assertFalse((self.home / "claude-tasks" / "all-threads").exists())
        self.assertEqual(self.git("branch", "--list", state["branch"]), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
