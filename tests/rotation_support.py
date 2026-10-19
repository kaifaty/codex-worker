"""Shared fixtures and helpers for the rotation CLI tests."""
import datetime
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from unittest import mock

from process_cleanup import cleanup_processes


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "codex-worker" / "skills" / "codex-worker"
SCRIPT = SKILL / "scripts" / "codex_task.py"
PYTHON = sys.executable
IMPOSSIBLE_PID = 2 ** 31 - 1


@lru_cache(maxsize=1)
def worker_code():
    # Reuse compilation, not module globals: subtests need independent mutable
    # queues and every fixture needs its own environment-derived paths/defaults.
    return compile(SCRIPT.read_bytes(), str(SCRIPT), "exec", dont_inherit=True)


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
    reset_gate = value.get("rate_limits_gate")
    if value.get("auto_reset") and (not reset_gate or Path(reset_gate).exists()):
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
            gate = value.get("rate_limits_gate")
            trigger = value.get("rate_limits_gate_after_trigger")
            if trigger and not Path(trigger).exists():
                gate = None
            if not gate and (home / "exec-count").exists():
                gate = value.get("rate_limits_gate_after_exec")
            if gate and not Path(gate).exists():
                trace({"command": "quota-waiting", "gate": gate, "source": "rpc"})
                while not Path(gate).exists():
                    time.sleep(0.02)
                value = fixture()
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

    # Arm deadlines before either rollout publication or authentication writes.
    # Gating only RPCs lets a cached rollout retain the pre-arm reset timestamp.
    reset_gate = value.get("rate_limits_gate")
    if reset_gate and not Path(reset_gate).exists():
        trace({"command": "quota-waiting", "gate": reset_gate, "source": "exec"})
        while not Path(reset_gate).exists() and not stopped[0]:
            time.sleep(0.02)
        if stopped[0]:
            return 130
        value = fixture()

    def rollout_limits(initial=False, weekly_used=None):
        if run.get("omit_rollout"):
            return
        current = fixture()
        raw = limits(current)
        if "rollout_used" in run:
            raw["primary"] = dict(raw["primary"], usedPercent=run["rollout_used"])
        used_key = "weekly_used_before" if initial else "weekly_used_after"
        if used_key in run:
            raw["primary"] = dict(raw["primary"], usedPercent=run[used_key])
        if weekly_used is not None:
            raw["primary"] = dict(raw["primary"], usedPercent=weekly_used)
        trace({"command": "rollout-snapshot", "thread": thread, "primary": raw["primary"]})
        windows = {}
        for name in ("primary", "secondary"):
            window = raw[name]
            windows[name] = ({"used_percent": window["usedPercent"],
                              "window_minutes": window["windowDurationMins"],
                              "resets_at": window["resetsAt"]} if window else None)
        windows["credits"] = raw["credits"]
        windows["rate_limit_reached_type"] = raw["rateLimitReachedType"]
        usage_key = "token_usage_before" if initial else "token_usage_after"
        usage = run.get(usage_key, {"total_tokens": 12})
        append(rollout, {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         "type": "event_msg", "payload": {"type": "token_count",
                         "info": {"total_token_usage": usage},
                         "rate_limits": windows}})

    def message(text):
        emit({"type": "item.completed", "item": {"id": str(uuid.uuid4()),
              "type": "agent_message", "text": text}})
        if not run.get("omit_rollout"):
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

    def refusal():
        emit({"type": "turn.failed", "error": {"message": run.get("error",
             "This content was flagged for possible cybersecurity risk. Please try a different prompt.")}})
        return 1

    def capacity_failure():
        deadline = time.monotonic() + run.get("delay", 0)
        while not stopped[0] and time.monotonic() < deadline:
            time.sleep(0.02)
        if stopped[0]:
            return 130
        if any(key in run for key in ("token_usage_after", "weekly_used_after")):
            rollout_limits()
        error = {"message": run.get("error",
                 "Selected model is at capacity. Please try a different model.")}
        trace({"command": "capacity-error", "thread": thread})
        if run.get("event") == "error":
            emit(dict(error, type="error"))
        else:
            emit({"type": "turn.failed", "error": error})
        return 1

    if run["mode"] == "auth_error" and run.get("before_thread"):
        return auth_failure()
    if run["mode"] == "refused" and run.get("before_thread"):
        return refusal()
    if run["mode"] == "capacity_error" and run.get("before_thread"):
        return capacity_failure()
    if run.get("rollout_gate"):
        trace({"command": "rollout-waiting", "thread": thread})
        while not Path(run["rollout_gate"]).exists() and not stopped[0]:
            time.sleep(0.02)
        if stopped[0]:
            return 130
    rollout_limits(initial=True)
    for used in run.get("weekly_samples_after_initial", []):
        rollout_limits(weekly_used=used)
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
        subprocess.run(["git", "commit", "-qm", run.get("commit_subject", "Apply fake worker changes")], check=True)
    for files in run.get("commit_batches", []):
        for name, contents in files.items():
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        subprocess.run(["git", "add", "--"] + list(files), check=True)
        subprocess.run(["git", "commit", "-qm", "Commit another fake worker change"], check=True)
    if run.get("push"):
        subprocess.run(["git", "push", "--quiet", "-u", "origin", "HEAD"], check=True)
    for name, contents in run.get("post_commit_write_files", {}).items():
        Path(name).write_text(contents)
    if run["mode"] == "refused":
        return refusal()
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
    if run["mode"] == "capacity_error":
        return capacity_failure()
    if "weekly_update_after" in run:
        deadline = time.monotonic() + run["weekly_update_after"]
        while not stopped[0] and time.monotonic() < deadline:
            time.sleep(0.02)
        if not stopped[0]:
            rollout_limits()
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
    completed = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
    if run.get("final_usage_after_completion"):
        # The model turn can finish before the CLI flushes its last quota
        # sample and exits. Keep that shutdown window visible to the monitor.
        emit(completed)
    if any(key in run for key in ("token_usage_before", "token_usage_after",
                                 "weekly_used_before", "weekly_used_after")):
        rollout_limits()
    if run.get("final_usage_after_completion"):
        time.sleep(0.2)
    else:
        emit(completed)
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
import subprocess
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
if args[:2] == ["repo", "view"]:
    if os.environ.get("CODEX_FAKE_GH_REPO_ERROR"):
        print("fixture remote is not accessible through GitHub", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"nameWithOwner": "fixture/repo"}))
elif args[:2] == ["pr", "list"]:
    entries = state.get(branch, [])
    if not isinstance(entries, list):
        entries = [entries] if entries else []
    prs = []
    for entry in entries:
        entry = {"url": entry, "state": "OPEN"} if isinstance(entry, str) else dict(entry)
        entry.setdefault("number", 42)
        entry.setdefault("headRepository", {"id": "R_fixture", "name": "repo", "nameWithOwner": ""})
        entry.setdefault("headRepositoryOwner", {"login": "fixture"})
        prs.append(entry)
    if "--jq" in args:
        expression = args[args.index("--jq") + 1]
        print("\n".join(str(entry["number"]) + " " + entry["state"] + " " + entry["url"]
                        if "number" in expression else entry["url"] for entry in prs))
    else:
        fields = args[args.index("--json") + 1].split(",")
        print(json.dumps([{key: entry[key] for key in fields} for entry in prs]))
elif args[:2] == ["pr", "create"]:
    if os.environ.get("CODEX_FAKE_GH_CREATE_ERROR"):
        print("fixture PR creation failed", file=sys.stderr)
        sys.exit(1)
    entries = state.get(branch, [])
    if not isinstance(entries, list):
        entries = [entries] if entries else []
    for entry in entries:
        entry = {"url": entry, "state": "OPEN"} if isinstance(entry, str) else entry
        repository = entry.get("headRepository", {"name": "repo"}) or {}
        owner = entry.get("headRepositoryOwner", {"login": "fixture"}) or {}
        if (entry.get("state") == "OPEN" and repository.get("name", "").casefold() == "repo"
                and owner.get("login", "").casefold() == "fixture"):
            print('a pull request for branch "' + branch + '" into branch "main" already exists',
                  file=sys.stderr)
            sys.exit(1)
    url = "https://example.test/pull/" + str(len(state) + 1)
    state[branch] = {"url": url, "state": "OPEN",
                     "headRepository": {"id": "R_fixture", "name": "repo", "nameWithOwner": ""},
                     "headRepositoryOwner": {"login": "fixture"}}
    state_path.write_text(json.dumps(state))
    print(url)
elif args[:2] == ["pr", "edit"]:
    if os.environ.get("CODEX_FAKE_GH_EDIT_ERROR"):
        print("fixture PR edit failed", file=sys.stderr)
        sys.exit(1)
elif args[:2] in (["pr", "view"], ["pr", "checks"], ["run", "view"]):
    scenario_path = Path(os.environ["CODEX_FAKE_GH_CI"])
    scenario = json.loads(scenario_path.read_text())
    if args[:2] == ["pr", "view"]:
        sha = scenario.get("head_sha") or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True).strip()
        print(json.dumps({"headRefOid": sha}))
    elif args[:2] == ["pr", "checks"]:
        snapshots = scenario.get("checks", [[]])
        index = scenario.get("polls", 0)
        checks = snapshots[min(index, len(snapshots) - 1)]
        scenario["polls"] = index + 1
        scenario_path.write_text(json.dumps(scenario))
        print(json.dumps(checks))
        if any(check["bucket"] == "pending" for check in checks):
            sys.exit(8)
        if any(check["bucket"] in ("fail", "cancel") for check in checks):
            sys.exit(1)
    else:
        job = args[args.index("--job") + 1]
        repository = (args[args.index("--repo") + 1] if "--repo" in args
                      else os.environ.get("GH_REPO", "fixture/repo"))
        if repository.startswith("https://"):
            repository = repository[len("https://"):]
        if repository.count("/") == 1:
            repository = "github.com/" + repository
        if repository != "github.com/fixture/repo":
            print("HTTP 404: job " + job + " not found in " + repository, file=sys.stderr)
            sys.exit(1)
        print(scenario.get("jobs", {}).get(job, "fixture failed job " + job))
else:
    sys.exit("unsupported fake gh invocation")
'''


class RotationTestCase(unittest.TestCase):
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
                        CODEX_TASK_WAIT_POLL_SECONDS="0.02", CODEX_TASK_RATE_POLL_SECONDS="0.05",
                        CODEX_TASK_ROLLOUT_SEARCH_SECONDS="0.05",
                        GIT_AUTHOR_NAME="Rotation Test", GIT_AUTHOR_EMAIL="rotation@example.test",
                        GIT_COMMITTER_NAME="Rotation Test", GIT_COMMITTER_EMAIL="rotation@example.test")
        # The external process gets no opportunity to ask for a credential or pager.
        self.env.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
        # Later test-specific Git settings must not affect fixture creation.
        self._repo_env = self.env.copy()
        self._repo = None
        self.brief = self.root / "brief.md"
        self.brief.write_text("Реализовать переносимую задачу.\nСохранить весь исходный бриф.\n"
                              "Готово, когда:\n- весь исходный бриф сохранён.\n")
        # A reset well into the future also makes local-time assertions stable.
        self.reset = int(time.time()) + 7200
        self.account(self.home, "A", self.reset)
        self.account(self.other, "B", self.reset + 3600)

    @property
    def repo(self):
        if self._repo is None:
            # Publish the path before git() uses it as cwd. Configuration-only
            # tests never pay for either repository or any git subprocesses.
            self._repo = self.root / "repo"
            self._repo.mkdir()
            self.git("init", "--quiet", "--initial-branch=main", _env=self._repo_env)
            (self._repo / "tracked.txt").write_text("initial\n")
            self.git("add", "tracked.txt", _env=self._repo_env)
            self.git("commit", "--quiet", "-m", "initial", _env=self._repo_env)
            origin = self.root / "origin.git"
            self.git("init", "--quiet", "--bare", "--initial-branch=main", str(origin), _env=self._repo_env)
            self.git("remote", "add", "origin", str(origin), _env=self._repo_env)
            self.git("push", "--quiet", "-u", "origin", "main", _env=self._repo_env)
            self.git("remote", "set-head", "origin", "main", _env=self._repo_env)
        return self._repo

    def tearDown(self):
        # Supervisors publish final state after SIGTERM. Wait for every owned
        # writer and its tools before removing their directories.
        live = cleanup_processes(self.root, self.extra_pids)
        self.assertFalse(live, "task processes still running before temporary-directory cleanup: %s" % sorted(live))
        self.tmp.cleanup()

    def git(self, *args, _env=None):
        result = subprocess.run(["git"] + list(args), cwd=self.repo,
                                env=self.env if _env is None else _env,
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
        prepared = kwargs.get("prepared")
        if prepared is None:
            result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args), cwd=self.repo,
                                    env=self.env, text=True, capture_output=True,
                                    timeout=kwargs.get("timeout", 15))
        else:
            stdout, stderr = prepared.communicate(json.dumps(list(args)) + "\n",
                                                   timeout=kwargs.get("timeout", 15))
            result = subprocess.CompletedProcess([PYTHON, "-I", str(SCRIPT)] + list(args),
                                                 prepared.returncode, stdout, stderr)
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

    def exhaust_accounts(self, *accounts, **settings):
        """Create exhausted fixtures with each account's original reset time."""
        for home, account_id, reset in accounts:
            self.account(home, account_id, reset, used=99, **settings)

    def assert_resumed_same_thread(self, previous, resumed, home):
        self.assertEqual(previous["thread"], resumed["thread"])
        self.assertTrue(resumed["resume"])
        self.assertEqual(previous["home"], str(home))
        self.assertEqual(resumed["home"], str(home))

    def reset_after_ready(self, command, reset, delay, after_exec=False):
        # Arm before the first exec publication, or the initial quota request
        # when no account can run, after expensive subprocess startup.
        gate = self.root / ("quota-ready-" + uuid.uuid4().hex)
        for home in (self.home, self.other):
            path = home / "fixture.json"
            value = json.loads(path.read_text())
            value["rate_limits_gate"] = str(gate)
            if after_exec:
                value["rate_limits_gate_after_trigger"] = str(self.home / "exec-count")
            path.write_text(json.dumps(value))
        stopped = threading.Event()

        def arm():
            try:
                while not stopped.is_set():
                    if any(event.get("command") == "quota-waiting" and event.get("gate") == str(gate)
                           for event in self.trace()):
                        armed = time.time() + delay
                        for home in (self.home, self.other):
                            path = home / "fixture.json"
                            value = json.loads(path.read_text())
                            if not value.get("auto_reset"):
                                continue
                            for name in ("primary", "secondary"):
                                if value.get(name) and value[name].get("resetsAt") is not None:
                                    value[name]["resetsAt"] = armed + (value[name]["resetsAt"] - reset)
                            if "release_at" in value:
                                value["release_at"] = armed + (value["release_at"] - reset)
                            path.write_text(json.dumps(value))
                        return armed
                    stopped.wait(0.02)
            finally:
                gate.touch()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(arm)
            try:
                # Keep the CLI's existing startup timeout. Its startup must not
                # consume the short event wait used after it returns.
                command()
                self.until(future.done)
                return future.result()
            finally:
                stopped.set()

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

    def consume_calls(self):
        return [event for event in self.trace() if event.get("command") == "consume"]

    def telemetry_journal(self):
        path = self.home / "codex-worker-log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def phase_telemetry_journal(self):
        """Measured stages exclude the supervisor's final turn summary."""
        return [row for row in self.telemetry_journal() if row["phase"] != "turn"]

    def telemetry_usage(self, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens):
        return {"input_tokens": input_tokens, "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens, "reasoning_output_tokens": reasoning_output_tokens,
                "total_tokens": input_tokens + output_tokens}

    def worker(self):
        # Unit tests exercise the public polling defaults; only CLI subprocesses
        # use the short intervals from the integration fixture.
        env = dict(self.env)
        for key in ("CODEX_TASK_WAIT_POLL_SECONDS", "CODEX_TASK_RATE_POLL_SECONDS",
                    "CODEX_TASK_ROLLOUT_SEARCH_SECONDS", "CODEX_TASK_CI_POLL_SECONDS"):
            env.pop(key, None)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "dont_write_bytecode", True):
            spec = importlib.util.spec_from_file_location("rotation_worker", str(SCRIPT))
            worker = importlib.util.module_from_spec(spec)
            exec(worker_code(), worker.__dict__)
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
        # Keep fixture setup outside tests' command traces, as with eager setup.
        _ = self.repo
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

    def wait(self, slug, timeout=90, through_sleep=False, prepared=None):
        args = ["wait", slug, "--timeout", str(timeout)]
        if through_sleep:
            args.append("--through-sleep")
        pid = self.state(slug).get("pid")
        if pid:
            self.extra_pids.add(pid)
        output = self.cli(*args, timeout=timeout + 15, prepared=prepared)
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
                  "pr": "", "pr_title": "", "pr_body": "", "questions": [], "remaining": [],
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
            for name in ("pr_title", "pr_body"):
                self.assertEqual(properties[name]["type"], "string")
                self.assertIn(name, schema["required"])
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
