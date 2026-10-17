#!/usr/bin/env python3
"""Exercise account rotation through the public CLI, without a real Codex login.

Run with python3 -I tests/test_rotation.py. The mid-turn case deliberately
waits for the supervisor's real 30-second rollout poll.
"""
import datetime
import importlib.util
import io
import json
import os
import runpy
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
    invocation = {"command": "exec", "resume": resumed, "review": review,
                  "prompt": prompt, "pid": os.getpid(), "ppid": os.getppid(),
                  "argv": sys.argv[1:], "cwd": os.getcwd(),
                  "output": str(output) if output else None}
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
    rollout_limits()
    # Publish the ready fixture before the supervisor starts looking for its rollout.
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
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
    if run["mode"] in ("limit", "hold"):
        deadline = time.monotonic() + 100
        while not stopped[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        if stopped[0]:
            return 130
        emit({"type": "turn.failed", "error": {"message": "fake turn was never interrupted"}})
        return 2
    time.sleep(0.05)
    for n in range(run.get("reconnects", 0)):
        emit({"type": "error", "message": f"Reconnecting... {n + 1}/5 (stream disconnected before completion)"})
    reply = run.get("reply", "done")
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
elif command == "archive":
    trace({"command": "archive", "thread": sys.argv[2]})
else:
    sys.exit("unsupported fake codex command: " + command)
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
        self.env = dict(os.environ, CODEX_TASK_HOME=str(self.home),
                        CODEX_TASK_CODEX=str(self.fake), CODEX_FAKE_TRACE=str(self.trace_path),
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
            result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat="],
                                    text=True, capture_output=True, check=True)
            table = {}
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) == 4:
                    table[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3])
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

    def cli(self, *args, **kwargs):
        result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args), cwd=self.repo,
                                env=self.env, text=True, capture_output=True,
                                timeout=kwargs.get("timeout", 15))
        self.assertEqual(result.returncode, 0,
                         "command: %s\nstdout:\n%s\nstderr:\n%s" % (args, result.stdout, result.stderr))
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
                  use_reset_credits=None, reset_credit_min_hours=None):
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
        self.assertEqual(self.state("scout-sleep")["outcome"], "sleeping")
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
        self.configure()
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
        self.assertLess(output.index("== check `"), output.index("== review by "))

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
        self.configure()
        self.start("check-explicit-review", "--check", "printf 'checked\\n' >> check-runs")
        self.wait("check-explicit-review")
        self.cli("review", "check-explicit-review")
        output = self.wait("check-explicit-review")
        state = self.state("check-explicit-review")
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text(), "checked\n")
        self.assertNotIn("== check `", output)
        self.assertEqual(len(self.review_invocations()), 1)

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
        sleeping = self.assert_sleeping("reset-resume", first, reset)
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("sleeping until ", self.cli("status"))
        self.assertEqual([entry["event"] for entry in sleeping["rotations"]], ["sleep"])

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-resume")
        executions = self.invocations()
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assertEqual(executions[1]["home"], str(self.home))
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
