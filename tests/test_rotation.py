#!/usr/bin/env python3
"""Exercise account rotation through the public CLI, without a real Codex login.

Run with python3 -I tests/test_rotation.py. The mid-turn case deliberately
waits for the supervisor's real 30-second rollout poll.
"""
import datetime
import importlib.util
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
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
    append(trace_path, value)

def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

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
            current_limits = limits(value)
            trace({"command": "quota-snapshot", "primary": current_limits["primary"]})
            result = {"accountId": value["accountId"],
                      "ordinaryUsageAllowed": value.get("ordinaryUsageAllowed", True),
                      "rateLimits": current_limits}
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
    trace(dict(invocation, thread=thread, mode=run["mode"]))

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
        # Failed assertions must not leave detached supervisors or fake turns.
        tasks = self.home / "claude-tasks"
        for path in tasks.glob("*/state.json"):
            try:
                state = json.loads(path.read_text())
                if state.get("pid"):
                    os.kill(int(state["pid"]), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        for event in self.trace():
            if event.get("command") == "exec":
                try:
                    os.kill(int(event["pid"]), signal.SIGTERM)
                except OSError:
                    pass
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

    def configure(self, credits=False, wait_for_reset=None, language="ru"):
        config = {
            "switch_at": 99, "credits": credits, "language": language,
            "accounts": [{"name": "main", "home": str(self.home)},
                         {"name": "second", "home": str(self.other)}]}
        if wait_for_reset is not None:
            config["wait_for_reset"] = wait_for_reset
        (self.home / "claude-accounts.json").write_text(json.dumps(config))

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
        self.start("review-failed", "--review")
        output = self.cli("wait", "review-failed", "--timeout", "10")
        self.assertEqual(self.state("review-failed")["outcome"], "failed")
        self.assertIn("fixture task failed", output)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertFalse(self.state("review-failed").get("reviews"))
        self.assertNotIn("== review by", output)

    def test_stopped_working_turn_does_not_start_automatic_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("review-stopped", "--review")
        self.until(lambda: bool(self.invocations()))
        self.cli("stop", "review-stopped", timeout=20)
        self.wait("review-stopped")
        self.assertEqual(self.state("review-stopped")["outcome"], "stopped")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())

    def test_paused_working_turn_does_not_start_automatic_review(self):
        self.configure(wait_for_reset=False)
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("review-paused", "--review")
        output = self.wait("review-paused")
        self.assertEqual(self.state("review-paused")["outcome"], "exhausted")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertNotIn("== review by", output)

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
        self.assertIn("not logged in", rows["rpc-error"])
        self.assertFalse(self.invocations())

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
