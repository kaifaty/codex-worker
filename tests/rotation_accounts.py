"""Account authentication, reset waits and monitor tests."""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from rotation_support import IMPOSSIBLE_PID, PYTHON, SCRIPT, RotationTestCase


class AccountAuthTests(RotationTestCase):
    def test_claude_login_registers_accounts_without_capturing_credentials(self):
        worker = self.worker()
        trace = self.root / "login-trace.jsonl"
        terminal = self.root / "login-terminal"
        terminal.touch()
        token = "fixture-interactive-claude-token"
        for name in ("claude", "security"):
            executable = self.root / name
            executable.write_text("#!" + PYTHON + "\n" +
                                  "import json, os, sys\n" +
                                  "from pathlib import Path\n" +
                                  "with Path(" + repr(str(trace)) + ").open('a') as stream:\n" +
                                  "    stream.write(json.dumps({'command': " + repr(name) +
                                  ", 'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n" +
                                  ("print(" + repr(token) + ")\n" if name == "claude" else
                                   "print('Keychain password:', file=sys.stderr)\n") +
                                  "print('fixture-login-diagnostic', file=sys.stderr)\n")
            executable.chmod(0o700)
        legacy = {"config_dir": str(self.root / "legacy-claude"),
                  "token_keychain": "fixture-legacy-service", "model": "fixture-model"}
        path = self.home / "codex-worker.json"
        path.write_text(json.dumps({"language": "en", "claude": legacy}))
        environment = dict(self.env, HOME=str(self.root), USER="fixture-user",
                           CODEX_TASK_CLAUDE=str(self.root / "claude"),
                           CLAUDE_CODE_OAUTH_TOKEN=token, ANTHROPIC_API_KEY=token)
        stdout, stderr = self.root / "login-stdout", self.root / "login-stderr"

        @contextmanager
        def capture_descriptors():
            sys.stdout.flush()
            sys.stderr.flush()
            saved = [os.dup(descriptor) for descriptor in (1, 2)]
            try:
                with stdout.open("ab") as output, stderr.open("ab") as errors:
                    os.dup2(output.fileno(), 1)
                    os.dup2(errors.fileno(), 2)
                    yield
                    sys.stdout.flush()
                    sys.stderr.flush()
            finally:
                for descriptor, original in zip((1, 2), saved):
                    os.dup2(original, descriptor)
                    os.close(original)

        custom = self.root / "custom-claude"
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(worker.claude_review, "probe", return_value=None), \
                mock.patch.object(worker.commands.os, "ttyname", return_value="/dev/fixture-tty"), \
                mock.patch.object(worker.commands, "open", create=True,
                                  side_effect=lambda path, mode, **kwargs: terminal.open(mode, **kwargs)) as opened_terminal, \
                mock.patch.object(worker.commands.subprocess, "run", wraps=worker.commands.subprocess.run) as launched, \
                capture_descriptors():
            for arguments in (("claude-login", "second"),
                              ("claude-login", "third", "--config-dir", str(custom),
                               "--keychain", "fixture-custom-service")):
                with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py"] + list(arguments)):
                    worker.cli.main()
            worker.claude_accounts.mark_limited("second", token_unavailable=True)
            self.assertTrue(worker.claude_accounts.token_marked(worker.claude_accounts.read_limits()["second"]))
            self.assertTrue(worker.claude_accounts.limited(worker.claude_accounts.read_limits()["second"]))
            with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-login", "second"]):
                worker.cli.main()
        settings = json.loads(path.read_text())
        self.assertEqual(settings["language"], "en")
        self.assertNotIn("accounts", settings, "Claude login must not enable Codex rotation")
        self.assertEqual(settings["claude"]["model"], "fixture-model")
        self.assertEqual(settings["claude"]["accounts"], [
            dict(name="default", config_dir=legacy["config_dir"], token_keychain=legacy["token_keychain"]),
            {"name": "second", "config_dir": str(self.root / ".claude-accounts" / "second"),
             "token_keychain": "codex-worker-claude-second"},
            {"name": "third", "config_dir": str(custom), "token_keychain": "fixture-custom-service"}])
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual([row["command"] for row in rows], ["claude", "security"] * 3)
        for index, name in enumerate(("second", "third", "second")):
            setup, keychain = rows[index * 2:index * 2 + 2]
            account = next(account for account in settings["claude"]["accounts"] if account["name"] == name)
            self.assertEqual(setup["argv"], ["setup-token"])
            self.assertEqual(keychain["argv"], ["add-generic-password", "-U", "-s",
                                                account["token_keychain"], "-a", "fixture-user", "-w"])
            for row in (setup, keychain):
                self.assertEqual(row["env"]["CLAUDE_CONFIG_DIR"], account["config_dir"])
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", row["env"])
                self.assertNotIn("ANTHROPIC_API_KEY", row["env"])
                self.assertNotIn(token, json.dumps(row))
        self.assertNotIn("second", worker.claude_accounts.read_limits())
        self.assertEqual([call.args[0] for call in opened_terminal.call_args_list], ["/dev/fixture-tty"] * 3)
        for invocation in launched.call_args_list:
            self.assertIs(invocation.kwargs["stdin"], invocation.kwargs["stdout"])
            self.assertIs(invocation.kwargs["stdin"], invocation.kwargs["stderr"])
            self.assertEqual(invocation.kwargs["stdin"].name, str(terminal))
        self.assertEqual(launched.call_count, 6)
        captured = stdout.read_text() + stderr.read_text()
        self.assertNotIn(token, captured + path.read_text())
        self.assertNotIn("fixture-login-diagnostic", captured)
        self.assertNotIn("Keychain password:", captured)
        self.assertIn(token, terminal.read_text(), "only the person's terminal receives the token")
        path.write_text(json.dumps({"claude": {"model": "fixture-model"}}))
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(worker.claude_review, "probe", return_value=None), \
                mock.patch.object(worker.commands, "open", create=True,
                                  side_effect=lambda path, mode, **kwargs: terminal.open(mode, **kwargs)), \
                mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-login", "second"]), \
                capture_descriptors():
            worker.cli.main()
        self.assertEqual(json.loads(path.read_text())["claude"], {
            "model": "fixture-model", "accounts": [
                {"name": "default", "config_dir": str(self.root / ".claude-accounts" / "reviewer"),
                 "token_keychain": "codex-worker-claude-reviewer"},
                {"name": "second", "config_dir": str(self.root / ".claude-accounts" / "second"),
                 "token_keychain": "codex-worker-claude-second"}]})

    def test_claude_login_opens_the_terminal_by_its_device_path(self):
        worker = self.worker()

        def ttyname(descriptor):
            if descriptor == 0:
                raise OSError(25, "Inappropriate ioctl for device")
            return f"/dev/fixture-tty{descriptor}"

        with mock.patch.object(worker.commands.os, "ttyname", side_effect=ttyname):
            self.assertEqual(worker.commands.terminal_device(), "/dev/fixture-tty1")
        with mock.patch.object(worker.commands.os, "ttyname", side_effect=OSError(25, "not a terminal")):
            self.assertEqual(worker.commands.terminal_device(), "/dev/tty")

    def test_concurrent_claude_logins_merge_the_latest_settings_after_interaction(self):
        worker = self.worker()
        ready, release = self.root / "login-ready", self.root / "login-release"
        terminal = self.root / "login-terminal"
        terminal.touch()
        claude = self.root / "claude"
        claude.write_text("#!" + PYTHON + "\n" +
                          "import os, time\nfrom pathlib import Path\n" +
                          "if Path(os.environ['CLAUDE_CONFIG_DIR']).name == 'first':\n" +
                          "    Path(" + repr(str(ready)) + ").touch()\n" +
                          "    while not Path(" + repr(str(release)) + ").exists():\n" +
                          "        time.sleep(0.02)\n")
        security = self.root / "security"
        security.write_text("#!" + PYTHON + "\n")
        for executable in (claude, security):
            executable.chmod(0o700)
        path = self.home / "codex-worker.json"
        default = {"name": "default", "config_dir": "~/.claude-accounts/reviewer",
                   "token_keychain": "codex-worker-claude-reviewer"}
        settings = {"language": "en", "claude": {"model": "fixture-model", "accounts": [default]}}
        path.write_text(json.dumps(settings))
        environment = dict(self.env, HOME=str(self.root), USER="fixture-user", CODEX_TASK_CLAUDE=str(claude))
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(worker.claude_review, "probe", return_value=None), \
                mock.patch.object(worker.commands, "open", create=True,
                                  side_effect=lambda path, mode, **kwargs: terminal.open(mode, **kwargs)), \
                mock.patch("builtins.print"), ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(worker.commands.cmd_claude_login,
                                    SimpleNamespace(name="first", config_dir=None, keychain=None))
            try:
                self.until(ready.exists)
                settings["switch_at"] = 87
                settings["claude"]["model"] = "fixture-updated-model"
                path.write_text(json.dumps(settings))
                worker.commands.cmd_claude_login(SimpleNamespace(name="third", config_dir=None, keychain=None))
                self.assertFalse(first.done(), "interactive login must not hold the settings lock")
            finally:
                release.touch()
            first.result(timeout=10)
        saved = json.loads(path.read_text())
        pool = saved["claude"].pop("accounts")
        self.assertEqual(saved, {"language": "en", "switch_at": 87,
                                 "claude": {"model": "fixture-updated-model"}})
        self.assertEqual([account["name"] for account in pool], ["default", "third", "first"])
        self.assertEqual(pool[0], default, "unrelated account paths must remain unchanged")

    def claude_probe_fixture(self):
        token = "fixture-claude-probe-private-token"
        fixture = self.root / "probe-fixture.json"
        fixture.write_text("{}")
        trace = self.root / "probe-trace.jsonl"
        settings = self.home / "codex-worker.json"
        for command in ("claude", "security"):
            executable = self.root / command
            executable.write_text("#!" + PYTHON + "\n" +
                "import json, os, sys\nfrom pathlib import Path\n" +
                "fixture = json.loads(Path(" + repr(str(fixture)) + ").read_text())\n" +
                "token = " + repr(token) + "\n" +
                "row = {'command': " + repr(command) + ", 'argv': sys.argv[1:]}\n" +
                "row['cwd'] = os.getcwd()\n" +
                "row['config_dir'] = os.environ.get('CLAUDE_CONFIG_DIR')\n" +
                "row['token_matches'] = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == token\n" +
                "row['stdin_null'] = os.fstat(0).st_rdev == os.stat(os.devnull).st_rdev\n" +
                "row['stderr_null'] = os.fstat(2).st_rdev == os.stat(os.devnull).st_rdev\n" +
                "row['host_settings'] = [key for key in os.environ if key.startswith(('CLAUDE', 'ANTHROPIC')) " +
                "and key not in ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN')]\n" +
                "settings = Path(" + repr(str(settings)) + ")\n" +
                "row['saved'] = settings.exists() and any(account['name'] == 'probe' for account in " +
                "json.loads(settings.read_text()).get('claude', {}).get('accounts', []))\n" +
                "with Path(" + repr(str(trace)) + ").open('a') as stream:\n" +
                "    stream.write(json.dumps(row) + '\\n')\n" +
                "if row['command'] == 'security':\n" +
                "    if sys.argv[1] == 'find-generic-password':\n" +
                "        print(token, file=sys.stderr)\n" +
                "        if fixture.get('missing_token'): sys.exit(1)\n" +
                "        print(token)\n" +
                "elif sys.argv[1] == 'setup-token':\n" +
                "    print(token)\n" +
                "else:\n" +
                "    for event in fixture.get('events', []): print(json.dumps(event), flush=True)\n" +
                "    print(token, file=sys.stderr)\n" +
                "    if fixture.get('hold'):\n" +
                "        Path(" + repr(str(fixture.with_suffix('.ready'))) + ").touch()\n" +
                "        import time\n" +
                "        while True: time.sleep(1)\n" +
                "    sys.exit(fixture.get('exit_code', 0))\n")
            executable.chmod(0o700)
        self.env.update(HOME=str(self.root), USER="fixture-user",
                        CODEX_TASK_CLAUDE=str(self.root / "claude"),
                        CLAUDE_CODE_OAUTH_TOKEN="host-secret", CLAUDECODE="1",
                        ANTHROPIC_API_KEY="host-secret", ANTHROPIC_BASE_URL="https://host.invalid")
        return token, fixture, trace

    def test_claude_probe_reports_limits_and_failures_without_capturing_credentials(self):
        token, fixture, trace = self.claude_probe_fixture()
        account = {"name": "probe", "config_dir": str(self.root / "claude-probe-account"),
                   "token_keychain": "fixture-probe-service"}
        settings = self.home / "codex-worker.json"
        settings.write_text(json.dumps({"claude": {"model": "fixture-probe-model", "accounts": [account]}}))
        worker = self.worker()
        cfg = worker.config.accounts_config()
        self.assertEqual(worker.accounts.claude_account_lines(cfg),
                         ["claude probe: no requests yet; run claude-probe probe"])
        self.assertEqual(worker.commands.accounts_json([], cfg)["claude"], [{
            "name": "probe", "five_hour": None, "seven_day": None,
            "limited_until": None, "token_unavailable": None}])
        windows = {"five_hour": {"utilization": 0.25, "resetsAt": self.reset - 3600},
                   "seven_day": {"utilization": 0.5, "resetsAt": self.reset}}
        allowed = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": windows, "diagnostic": token}}
        result = {"type": "result", "is_error": False, "result": "OK", "diagnostic": token}
        cases = [
            ("allowed", {"events": [allowed, result]}, None),
            ("rejected", {"events": [{"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "unifiedWindows": windows, "diagnostic": token}}]}, "usage limit"),
            ("401", {"events": [{"type": "system", "subtype": "api_retry", "error_status": 401,
                                  "error": token}]}, "authentication failed"),
            ("403", {"events": [{"type": "result", "api_error_status": 403, "is_error": True,
                                  "result": token}]}, "authentication failed"),
            ("missing", {"missing_token": True}, "token unavailable"),
            ("nonzero", {"events": [result], "exit_code": 7}, "request failed"),
            ("error", {"events": [{"type": "result", "is_error": True, "result": token}]}, "request failed")]
        for label, scenario, reason in cases:
            with self.subTest(case=label):
                worker.claude_accounts.clear_limits("probe")
                trace.unlink(missing_ok=True)
                fixture.write_text(json.dumps(scenario))
                if label == "allowed":
                    worker.claude_accounts.mark_limited("probe", token_unavailable=True)
                completed = subprocess.run([PYTHON, "-I", str(SCRIPT), "claude-probe", "probe"],
                                           cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15)
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode == 0, reason is None, output)
                self.assertEqual(len(output.strip().splitlines()), 1, output)
                if reason is None:
                    self.assertTrue(output.startswith("Claude account probe: token ok; 5h 25%"), output)
                    self.assertIn("; 7d 50%", output)
                    info = worker.claude_accounts.read_limits()["probe"]
                    self.assertEqual(info["status"], "allowed")
                    self.assertEqual(info["unifiedWindows"], windows)
                    self.assertFalse(info.get("token_unavailable"))
                    self.assertNotIn("limited_until", info)
                else:
                    self.assertIn("Claude account probe: " + reason, output)
                rows = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertEqual(rows[0]["argv"], ["find-generic-password", "-s", "fixture-probe-service",
                                                    "-a", "fixture-user", "-w"])
                self.assertEqual([row["command"] for row in rows],
                                 ["security"] if label == "missing" else ["security", "claude"])
                for row in rows:
                    self.assertEqual(row["config_dir"], account["config_dir"])
                    self.assertEqual(row["host_settings"], [])
                if label != "missing":
                    invocation = rows[1]
                    self.assertTrue(invocation["token_matches"])
                    self.assertTrue(invocation["stdin_null"])
                    self.assertTrue(invocation["stderr_null"])
                    for option, value in (("-p", "Reply with the single word OK."),
                                          ("--output-format", "stream-json"), ("--max-turns", "1"),
                                          ("--model", "fixture-probe-model"), ("--tools", ""),
                                          ("--permission-mode", "dontAsk")):
                        self.assertEqual(invocation["argv"][invocation["argv"].index(option) + 1], value)
                    self.assertIn("--verbose", invocation["argv"])
                    self.assertNotIn("Read,Grep,Glob,Bash", invocation["argv"])
                    self.assertNotEqual(invocation["cwd"], str(self.root))
                    self.assertFalse(Path(invocation["cwd"]).exists())
                state = "".join(path.read_text() for path in self.home.rglob("*") if path.is_file())
                self.assertNotIn(token, output + json.dumps(rows) + state)

    def test_claude_probe_timeout_preserves_received_limit_windows(self):
        _, fixture, _ = self.claude_probe_fixture()
        account = {"name": "probe", "config_dir": str(self.root / "claude-probe-account"),
                   "token_keychain": "fixture-probe-service"}
        (self.home / "codex-worker.json").write_text(json.dumps({"claude": {"accounts": [account]}}))
        windows = {"five_hour": {"utilization": 0.25, "resetsAt": self.reset - 3600},
                   "seven_day": {"utilization": 0.5, "resetsAt": self.reset}}
        fixture.write_text(json.dumps({"hold": True, "events": [{
            "type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": windows}}]}))
        worker = self.worker()
        original_communicate = worker.claude_review.subprocess.Popen.communicate

        def communicate(process, *args, **kwargs):
            if process.args[0] == str(self.root / "claude"):
                self.until(fixture.with_suffix(".ready").exists, timeout=30)
            return original_communicate(process, *args, **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(worker.config, "CLAUDE_PROBE_TIMEOUT", 0.1), \
                mock.patch.object(worker.claude_review.subprocess.Popen, "communicate", new=communicate), \
                mock.patch.object(worker.claude_review.subprocess, "run",
                                  wraps=worker.claude_review.subprocess.run) as launched, \
                mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-probe", "probe"]), \
                redirect_stdout(output), redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            worker.cli.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(output.getvalue(), "Claude account probe: request failed\n")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(worker.claude_accounts.read_limits()["probe"]["unifiedWindows"], windows)
        self.assertEqual([call.kwargs["timeout"] for call in launched.call_args_list],
                         [worker.config.ACCOUNT_TIMEOUT, 0.1])

    def test_claude_login_probes_after_saving_and_preserves_account_on_probe_failure(self):
        token, fixture, trace = self.claude_probe_fixture()
        worker = self.worker()
        terminal = self.root / "login-terminal"
        terminal.touch()
        for failure in (False, True):
            with self.subTest(failure=failure):
                trace.unlink(missing_ok=True)
                settings = self.home / "codex-worker.json"
                settings.unlink(missing_ok=True)
                fixture.write_text(json.dumps({"events": [{"type": "result", "is_error": failure,
                                                           "result": token if failure else "OK"}]}))
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(worker.commands, "open", create=True,
                                          side_effect=lambda path, mode, **kwargs: terminal.open(mode, **kwargs)), \
                        mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-login", "probe"]), \
                        redirect_stdout(output), redirect_stderr(errors):
                    if failure:
                        with self.assertRaises(SystemExit) as stopped:
                            worker.cli.main()
                        self.assertNotEqual(stopped.exception.code, 0)
                    else:
                        worker.cli.main()
                pool = json.loads(settings.read_text())["claude"]["accounts"]
                self.assertEqual([account["name"] for account in pool], ["default", "probe"])
                rows = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertEqual([row["command"] for row in rows], ["claude", "security", "security", "claude"])
                self.assertEqual(rows[0]["argv"], ["setup-token"])
                self.assertEqual(rows[2]["argv"][0], "find-generic-password")
                self.assertTrue(rows[-1]["saved"])
                captured = output.getvalue() + errors.getvalue()
                self.assertIn("request failed" if failure else "token ok", captured)
                state = "".join(path.read_text() for path in self.home.rglob("*") if path.is_file())
                self.assertNotIn(token, captured + json.dumps(rows) + state)

    def test_auth_race_retries_once_on_the_same_thread_in_both_languages(self):
        worker = self.worker()
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
                self.assert_resumed_same_thread(executions[0], executions[1], self.home)
                for execution in executions:
                    self.assert_output_schema(execution, slug)
                self.assertEqual(executions[1]["prompt"], worker.prompts.auth_resume_prompt(worker.config.accounts_config()))
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
        self.assert_resumed_same_thread(executions[0], executions[1], self.home)
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
        reset = self.reset_after_ready(lambda: self.start("auth-before-thread-sleep"), reset, 4, after_exec=True)
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
        self.assert_resumed_same_thread(executions[0], executions[1], self.home)
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

        with mock.patch.object(worker.accounts, "snapshots", side_effect=snapshots), \
                mock.patch.object(worker.turn_monitor, "monitor", side_effect=monitor) as attempts, \
                mock.patch.object(worker.threads, "handoff", return_value="handoff with the original task"), \
                mock.patch.object(worker.task_state, "save"):
            worker.turns.run_turn(st, cfg, "original prompt", None, False, lambda: False, lambda seconds: None)
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
        reset = self.reset_after_ready(lambda: self.start("auth-logout-then-wake"), reset, 4, after_exec=True)
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
        info = {"logged_in": True, "limits": worker.accounts.normalize_limits(raw)}
        self.assertTrue(worker.accounts.exhausted(info, cfg))
        self.assertEqual(worker.accounts.reset_time(info, cfg), self.reset + 3600)
        self.assertEqual(worker.accounts.longest_window(info)["reset"], self.reset + 3600)
        info["limits"]["primary"].update(used=100, reset=self.reset + 7200)
        self.assertEqual(worker.accounts.reset_time(info, cfg), self.reset + 7200)
        for credit, expected in (({"hasCredits": True, "balance": "12.34"}, True),
                                 ({"hasCredits": True, "balance": "0"}, False),
                                 ({"hasCredits": True, "balance": "invalid"}, False),
                                 ({"hasCredits": False, "balance": "12.34"}, False),
                                 ({"unlimited": True}, True)):
            with self.subTest(credits=credit):
                self.assertEqual(worker.accounts.has_credits({"limits": {"credits": credit}}), expected)
        clear = worker.accounts.normalize_limits({"primary": {"usedPercent": 3, "windowDurationMins": 10080,
                                                     "resetsAt": self.reset}})
        self.assertTrue(worker.accounts.exhausted({"limits": clear, "ordinary": False}, cfg))
        clear["reached"] = "workspace_member_credits_depleted"
        self.assertTrue(worker.accounts.exhausted({"limits": clear}, cfg))

    def set_fake_wall(self, stamp):
        path = Path(self.env["CODEX_TASK_CLOCK_FILE"])
        value = json.loads(path.read_text())
        value["wall"] = stamp
        path.write_text(json.dumps(value))

    def credit_info(self, name, balance, used=99, has_credits=True, unlimited=False):
        home = str(self.home if name == "main" else self.other)
        return {"name": name, "home": home, "logged_in": True, "account_id": name,
                "limits": {"primary": {"used": used, "minutes": 10080, "reset": self.reset},
                           "credits": {"hasCredits": has_credits, "unlimited": unlimited, "balance": balance}}}

    def test_credit_floor_gate_and_spendable_credits(self):
        worker = self.worker()
        cfg = {"credits": True, "credits_floor": 50, "credits_floor_until": None}
        for balance, expected, spendable in (("80", True, 30), ("50", False, 0), ("50.01", True, 0.01),
                                             ("20", False, 0), (None, False, None), ("invalid", False, None),
                                             ("nan", False, None)):
            with self.subTest(balance=balance):
                info = self.credit_info("main", balance)
                self.assertEqual(worker.accounts.has_credits(info, cfg), expected)
                self.assertEqual(worker.accounts.spendable_credits(info, cfg), spendable)
                self.assertEqual(worker.accounts.at_credit_floor(info, cfg), balance in ("50", "20"))
        self.assertTrue(worker.accounts.has_credits(self.credit_info("main", None, unlimited=True), cfg))
        self.assertFalse(worker.accounts.at_credit_floor(self.credit_info("main", "1", unlimited=True), cfg))
        self.assertFalse(worker.accounts.has_credits(self.credit_info("main", "80", has_credits=False), cfg))
        # Without a floor, the gate keeps its original rule: a known balance above 0.
        for balance, expected in (("0.5", True), ("0", False), (None, False)):
            with self.subTest(floor=0, balance=balance):
                info = self.credit_info("main", balance)
                self.assertEqual(worker.accounts.has_credits(info, dict(cfg, credits_floor=0)), expected)
                self.assertEqual(worker.accounts.has_credits(info), expected)
        self.assertEqual(worker.accounts.credit_floor_reason(self.credit_info("main", "20"), cfg),
                         "credits of main at the floor 50")
        self.assertIsNone(worker.accounts.credit_floor_reason(self.credit_info("main", "0"), cfg))
        self.assertIsNone(worker.accounts.credit_floor_reason(self.credit_info("main", "20"),
                                                              dict(cfg, credits=False)))
        infos = [self.credit_info("main", "90"), self.credit_info("second", "200"),
                 dict(self.credit_info("third", "900"), duplicate="main")]
        self.assertEqual(worker.accounts.credit_account(infos, cfg)["name"], "second")
        self.assertEqual(worker.accounts.credit_account(infos, cfg, {"second"})["name"], "main")
        self.assertIsNone(worker.accounts.credit_account(infos, dict(cfg, credits=False)))
        self.assertIsNone(worker.accounts.credit_account(infos, dict(cfg, credits_floor=500)))

    def test_credit_floor_applies_through_its_local_end_date(self):
        self.enable_fake_clock()
        worker = self.worker()
        cfg = {"credits": True, "credits_floor": 50, "credits_floor_until": "2030-06-15"}
        info = self.credit_info("main", "20")
        for stamp, floor in ((time.mktime((2030, 6, 15, 0, 0, 0, 0, 0, -1)), 50),
                             (time.mktime((2030, 6, 15, 23, 59, 59, 0, 0, -1)), 50),
                             (time.mktime((2030, 6, 16, 0, 0, 0, 0, 0, -1)), 0)):
            with self.subTest(local=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))):
                self.set_fake_wall(stamp)
                self.assertEqual(worker.accounts.credits_floor(cfg), floor)
                self.assertEqual(worker.accounts.has_credits(info, cfg), floor == 0)
                self.assertEqual(worker.accounts.spendable_credits(info, cfg), 20 if floor == 0 else 0)
        self.assertEqual(worker.accounts.credits_floor(dict(cfg, credits_floor_until=None)), 50)

    def test_credit_floor_settings_are_validated(self):
        worker = self.worker()
        path = self.home / "codex-worker.json"
        path.write_text(json.dumps({}))
        cfg = worker.config.accounts_config()
        self.assertEqual((cfg["credits_floor"], cfg["credits_floor_until"]), (0, None))
        path.write_text(json.dumps({"credits_floor": 12.5, "credits_floor_until": "2030-02-28"}))
        cfg = worker.config.accounts_config()
        self.assertEqual((cfg["credits_floor"], cfg["credits_floor_until"]), (12.5, "2030-02-28"))
        for value in (-1, "5", True, None, float("inf"), float("nan")):
            with self.subTest(credits_floor=value):
                path.write_text(json.dumps({"credits_floor": value}))
                with self.assertRaises(SystemExit) as error:
                    worker.config.accounts_config()
                self.assertIn("credits_floor must be a finite nonnegative number", str(error.exception))
        for value in ("2030-02-30", "2030-13-01", "2030-6-15", "15.06.2030", "", 20300615, True):
            with self.subTest(credits_floor_until=value):
                path.write_text(json.dumps({"credits_floor": 5, "credits_floor_until": value}))
                with self.assertRaises(SystemExit) as error:
                    worker.config.accounts_config()
                self.assertIn("credits_floor_until must be a date YYYY-MM-DD or null", str(error.exception))

    def run_credit_floor_turn(self, balances, monitor_balances=None, **settings):
        """Run one turn with fake snapshots; the main exec re-reads limits twice while it runs."""
        worker = self.worker()
        cfg = {"enabled": True, "switch_at": 99, "credits": True, "credits_floor": 200,
               "credits_floor_until": None, "wait_for_reset": True,
               "accounts": [{"name": "main", "home": str(self.home)},
                            {"name": "second", "home": str(self.other)}]}
        cfg.update(settings)
        st = {"slug": "credit-floor", "account": "main", "thread": "original-thread",
              "threads": [], "rotations": []}
        balances = dict(balances)
        decisions, execs, sleeps = [], [], []
        stop = [False]

        def snapshots(config, cancelled, prepare=False):
            return [self.credit_info(name, balances[name]) for name in ("main", "second")]

        def monitor(st, cfg, account, prompt, effort, stopped, on_limit, **kwargs):
            execs.append(account["name"])
            if account["name"] != "main":
                return "completed", None
            local = self.credit_info("main", "0")
            for balance in monitor_balances or ():
                balances["main"] = balance
                decision = on_limit(local, "weekly limit 99%", True, lambda: False)
                decisions.append(decision)
                if decision is not None:
                    return "limit", decision
            return "completed", None

        def sleep(seconds):
            sleeps.append(seconds)
            stop[0] = True

        with mock.patch.object(worker.accounts, "snapshots", side_effect=snapshots), \
                mock.patch.object(worker.turn_monitor, "monitor", side_effect=monitor), \
                mock.patch.object(worker.threads, "handoff", return_value="handoff with the original task"), \
                mock.patch.object(worker.task_state, "save"):
            worker.turns.run_turn(st, cfg, "original prompt", None, False, lambda: stop[0], sleep)
        return st, execs, decisions, sleeps

    def test_live_exec_on_credits_rotates_to_another_account_at_the_floor(self):
        st, execs, decisions, _ = self.run_credit_floor_turn({"main": "300", "second": "500"},
                                                             monitor_balances=("300", "200"))
        self.assertEqual(execs, ["main", "second"])
        self.assertIsNone(decisions[0], "a balance above the floor keeps the live exec")
        self.assertEqual(decisions[1]["chosen"]["name"], "second")
        self.assertEqual(st["account"], "second")
        self.assertEqual(st["outcome"], "completed")
        self.assertEqual([(r["from"], r["to"], r["reason"]) for r in st["rotations"]],
                         [("main", "second", "credits of main at the floor 200")])

    def test_turn_start_skips_an_account_at_the_credit_floor(self):
        st, execs, _, _ = self.run_credit_floor_turn({"main": "150", "second": "500"})
        self.assertEqual(execs, ["second"])
        self.assertEqual([(r["from"], r["to"], r["reason"]) for r in st["rotations"]],
                         [("main", "second", "credits of main at the floor 200")])
        self.assertEqual(st["outcome"], "completed")

    def test_live_exec_at_the_credit_floor_without_spendable_credits_follows_the_exhausted_path(self):
        st, execs, decisions, sleeps = self.run_credit_floor_turn(
            {"main": "300", "second": "150"}, monitor_balances=("200",))
        self.assertEqual(execs, ["main"])
        self.assertIsNone(decisions[0]["chosen"])
        self.assertTrue(sleeps)
        sleep = next(r for r in st["rotations"] if r.get("event") == "sleep")
        self.assertEqual(sleep["reason"], "credits of main at the floor 200")
        st, execs, _, sleeps = self.run_credit_floor_turn(
            {"main": "300", "second": "150"}, monitor_balances=("200",), wait_for_reset=False)
        self.assertEqual(execs, ["main"])
        self.assertFalse(sleeps)
        self.assertEqual(st["outcome"], "exhausted")
        self.assertEqual(st["exhausted"]["reason"], "credits of main at the floor 200")
        # Without credits the floor never applies: the turn waits before any exec, as before.
        st, execs, _, _ = self.run_credit_floor_turn({"main": "300", "second": "500"},
                                                     credits=False, wait_for_reset=False)
        self.assertEqual(execs, [])
        self.assertEqual(st["exhausted"]["reason"], "all accounts unavailable")
        # An unknown balance never passes an active floor, also for the other account.
        st, execs, _, _ = self.run_credit_floor_turn({"main": None, "second": None}, wait_for_reset=False)
        self.assertEqual(execs, [])
        self.assertEqual(st["exhausted"]["reason"], "all accounts unavailable")

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

                with mock.patch.object(worker.config, "SLEEP_STEP", 60), \
                        mock.patch.object(worker.config, "RESET_MARGIN", 0), \
                        mock.patch.object(worker.clock.time, "time", side_effect=lambda: clock[0]), \
                        mock.patch.object(worker.clock.time, "sleep", side_effect=sleep), \
                        mock.patch.object(worker.task_state, "save"), \
                        mock.patch.object(worker.accounts, "snapshots", return_value=[ready]) as snapshots:
                    chosen = worker.account_rotation.wait_for_account(st, [limited], cfg, "quota", lambda: False)
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

        with mock.patch.object(worker.config, "SLEEP_STEP", 60), \
                mock.patch.object(worker.config, "RESET_MARGIN", 120), \
                mock.patch.object(worker.clock.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.clock.time, "monotonic", side_effect=AssertionError("use wall time")), \
                mock.patch.object(worker.clock.time, "sleep", side_effect=mac_sleep) as sleeper, \
                mock.patch.object(worker.task_state, "save"), \
                mock.patch.object(worker.accounts, "snapshots", return_value=[other, current]):
            chosen = worker.account_rotation.wait_for_account(st, [limited], cfg, "quota", lambda: False)
        sleeper.assert_called_once_with(60)
        self.assertIs(chosen, current, "keep the existing thread when both subscriptions become available")
        self.assertEqual(st["rotations"][0]["until"], 1120)
        self.assertEqual(st["reset_wait_seconds"], 5000)
        self.assertEqual(st["outcome"], "running")
        self.assertFalse(st.get("sleeping"))

    def test_reset_wait_exhausts_eight_day_budget_across_sleep_episodes(self):
        worker = self.worker()
        self.assertEqual(worker.config.MAX_RESET_WAIT_SECONDS, 8 * 86400)
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        sleeps = []
        st = {"account": "main", "thread": "current-thread", "rotations": [],
              "reset_wait_seconds": worker.config.MAX_RESET_WAIT_SECONDS - 120}
        limited = {"name": "main", "logged_in": True, "limits": {"primary": {
            "used": 99, "minutes": 10080, "reset": 1060}}}

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch.object(worker.config, "SLEEP_STEP", 60), \
                mock.patch.object(worker.config, "RESET_MARGIN", 0), \
                mock.patch.object(worker.clock.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.clock.time, "sleep", side_effect=sleep), \
                mock.patch.object(worker.task_state, "save"), \
                mock.patch.object(worker.accounts, "snapshots", return_value=[limited]) as snapshots:
            chosen = worker.account_rotation.wait_for_account(st, [limited], cfg, "quota", lambda: False)
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
        st = {"slug": "gone-sleeper", "pid": IMPOSSIBLE_PID, "outcome": "sleeping", "rotations": [],
              "sleeping": {"until": self.reset, "account": "main", "reason": "quota"},
              "turns": 1, "branch": "test-branch", "base": "HEAD", "account": "main",
              "thread": "current-thread", "worktree": str(self.repo)}
        with mock.patch.object(worker.task_state, "load", side_effect=lambda slug: dict(st)), \
                mock.patch.object(worker.procs, "alive", return_value=False), \
                mock.patch.object(worker.task_state, "save") as save, \
                mock.patch.object(worker.threads, "last_message", return_value=""), \
                mock.patch.object(worker.threads, "events", return_value=[]), \
                mock.patch.object(worker.procs, "sh", return_value=""), \
                mock.patch("builtins.print") as printer:
            worker.commands.cmd_status(SimpleNamespace(slug=st["slug"]))
            status = "\n".join(str(call.args[0]) for call in printer.call_args_list)
            self.assertIn("paused, supervisor gone; continue with say", status)
            printer.reset_mock()
            worker.wait_view.cmd_wait(SimpleNamespace(slug=st["slug"], timeout=0, through_sleep=False))
            output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("== paused, supervisor gone; continue with say", output)
        self.assertNotIn("auto-continue at", output)
        self.assertIsNone(save.call_args[0][0]["pid"])

    def test_reset_wait_budget_includes_slow_account_probe(self):
        worker = self.worker()
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        st = {"account": "main", "thread": "current-thread", "rotations": [],
              "reset_wait_seconds": worker.config.MAX_RESET_WAIT_SECONDS - 90}
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

        with mock.patch.object(worker.config, "SLEEP_STEP", 60), \
                mock.patch.object(worker.config, "RESET_MARGIN", 0), \
                mock.patch.object(worker.clock.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.clock.time, "sleep", side_effect=sleep), \
                mock.patch.object(worker.task_state, "save"), \
                mock.patch.object(worker.accounts, "snapshots", side_effect=snapshots):
            chosen = worker.account_rotation.wait_for_account(st, [limited], cfg, "quota", lambda: False)
        self.assertIsNone(chosen, "do not launch an executor once the wait budget has expired")
        self.assertEqual(st["reset_wait_seconds"], 8 * 86400)
        self.assertEqual(st["outcome"], "exhausted")
        self.assertIn("8 days", st["exhausted"]["reason"])

    def test_monitor_quota_cancellation_preserves_stop_priority_and_unrelated_errors(self):
        worker = self.worker()
        for case in ("budget", "budget-return", "stop", "interrupt", "error"):
            with self.subTest(case=case):
                clock = [0.0]
                stopped = [False]
                slug = "quota-cancel-" + case
                (worker.config.TASKS / slug).mkdir(parents=True)
                st = {"slug": slug, "pid": IMPOSSIBLE_PID, "thread": "", "worktree": str(self.repo),
                      "threads": [], "rotations": []}
                if case == "stop":
                    st["stop_request"] = {"at": time.time(), "pid": st["pid"]}
                worker.task_state.save(st)
                proc = mock.Mock(pid=IMPOSSIBLE_PID)
                proc.poll.return_value = None
                proc.stdout.fileno.return_value = 99
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector
                selector.select.return_value = [(SimpleNamespace(fileobj=proc.stdout),
                                                 worker.turn_monitor.selectors.EVENT_READ)]

                def on_limit(local, reason, allow_credits, cancelled):
                    self.assertFalse(cancelled())
                    self.assertFalse(allow_credits)
                    if case == "error":
                        raise RuntimeError("unrelated quota failure")
                    clock[0] = 61.0
                    stopped[0] = case in ("stop", "interrupt")
                    self.assertTrue(cancelled())
                    if case == "budget-return":
                        return {"reason": reason}
                    raise RuntimeError("stopped")

                with mock.patch.object(worker.accounts, "prepare_home"), \
                        mock.patch.object(worker.task_state, "save"), \
                        mock.patch.object(worker.threads, "events", return_value=[]), \
                        mock.patch.object(worker.procs, "terminate") as terminate, \
                        mock.patch.object(worker.turn_monitor.subprocess, "Popen", return_value=proc), \
                        mock.patch.object(worker.turn_monitor.selectors, "DefaultSelector", return_value=selector), \
                        mock.patch.object(worker.os, "read", return_value=(
                            b'{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}\n')), \
                        mock.patch.object(worker.turn_monitor.time, "monotonic", side_effect=lambda: clock[0]):
                    args = (st, {"enabled": True, "switch_at": 99},
                            {"name": "main", "home": str(self.home)}, "prompt", None,
                            lambda: stopped[0], on_limit)
                    if case == "error":
                        with self.assertRaisesRegex(RuntimeError, "unrelated quota failure"):
                            worker.turn_monitor.monitor(*args, control=worker.turn_monitor.TurnControl(1, 3))
                    else:
                        result = worker.turn_monitor.monitor(*args, control=worker.turn_monitor.TurnControl(1, 3))
                        outcome = {"stop": "stopped", "interrupt": "interrupted"}.get(case, "budget_exhausted")
                        self.assertEqual(result, (outcome, None))
                terminate.assert_called_with(proc)
                self.assertNotIn("codex_pid", st)

    def test_monitor_bounds_rollout_searches_and_reads_during_long_step(self):
        worker = self.worker()
        self.assertEqual(worker.config.RATE_POLL_SECONDS, 30)
        self.assertEqual(worker.config.ROLLOUT_SEARCH_SECONDS, 5)
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
                (worker.config.TASKS / slug).mkdir(parents=True)
                st = {"slug": slug, "thread": "same-thread" if resumed else "",
                      "worktree": str(self.repo), "threads": [], "rotations": []}
                proc = mock.Mock(pid=IMPOSSIBLE_PID)
                proc.stdout.fileno.return_value = 99
                proc.poll.side_effect = lambda: 0 if clock[0] >= duration else None
                proc.wait.return_value = 0
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector

                def select(timeout):
                    clock[0] += timeout
                    if not emitted[0] or clock[0] >= duration:
                        return [(SimpleNamespace(fileobj=proc.stdout), worker.turn_monitor.selectors.EVENT_READ)]
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

                def read_limits(path, on_limits=None, offset=0):
                    self.assertIs(path, cached_rollout)
                    reads.append(clock[0])
                    limits = worker.accounts.normalize_limits({"primary": {"usedPercent": 10,
                        "windowDurationMins": 10080, "resetsAt": self.reset}})
                    if on_limits is not None:
                        on_limits(limits)
                    return limits

                selector.select.side_effect = select
                with mock.patch.object(worker.accounts, "prepare_home"), \
                        mock.patch.object(worker.task_state, "save"), \
                        mock.patch.object(worker.threads, "events", return_value=[]), \
                        mock.patch.object(worker.procs, "terminate"), \
                        mock.patch.object(worker.turn_monitor.subprocess, "Popen", return_value=proc), \
                        mock.patch.object(worker.turn_monitor.selectors, "DefaultSelector", return_value=selector), \
                        mock.patch.object(worker.os, "read", side_effect=read), \
                        mock.patch.object(worker.turn_monitor.time, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(worker.threads, "rollout_path", side_effect=find_rollout), \
                        mock.patch.object(worker.threads, "rollout_limits", side_effect=read_limits):
                    result = worker.turn_monitor.monitor(st, {"enabled": True, "switch_at": 99},
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
                self.assertLessEqual(len(reads), int(duration / worker.config.RATE_POLL_SECONDS) + 1)
                self.assertLessEqual(cached_rollout.stat.call_count,
                                     int(duration / worker.config.RATE_POLL_SECONDS) + 2)
                self.assertTrue(all(b - a >= worker.config.RATE_POLL_SECONDS - 1e-8
                                    for a, b in zip(reads, reads[1:])), reads)

    def test_wait_polls_each_second_and_clears_dead_legacy_pid(self):
        worker = self.worker()
        self.assertEqual(worker.config.WAIT_POLL_SECONDS, 1)
        st = {"slug": "legacy-pid", "pid": IMPOSSIBLE_PID, "rotations": [], "turns": 1,
              "branch": "test-branch", "base": "HEAD", "worktree": str(self.repo)}
        running = [True]

        def sleep(seconds):
            self.assertEqual(seconds, 1)
            running[0] = False

        with mock.patch.object(worker.task_state, "load", side_effect=lambda slug: dict(st)), \
                mock.patch.object(worker.procs, "alive", side_effect=lambda pid: bool(pid) and running[0]), \
                mock.patch.object(worker.wait_view.time, "sleep", side_effect=sleep) as sleeper, \
                mock.patch.object(worker.task_state, "save") as save, \
                mock.patch.object(worker.threads, "last_message", return_value=""), \
                mock.patch.object(worker.threads, "events", return_value=[]), \
                mock.patch.object(worker.procs, "sh", return_value=""), \
                mock.patch("builtins.print"):
            worker.wait_view.cmd_wait(SimpleNamespace(slug=st["slug"], timeout=0))
        sleeper.assert_called_once_with(1)
        save.assert_called_once()
        self.assertIsNone(save.call_args[0][0].get("pid"))

    def test_claude_rename_moves_limits_without_changing_account_credentials(self):
        worker = self.worker()
        path = self.home / "codex-worker.json"
        original = {"name": "first", "config_dir": "~/.claude-accounts/original",
                    "token_keychain": "fixture-original-service"}
        second = {"name": "second", "config_dir": str(self.root / "second-claude"),
                  "token_keychain": "fixture-second-service"}
        settings = {"language": "en", "claude": {"model": "fixture-model",
                    "accounts": [original, second]}}
        path.write_text(json.dumps(settings))
        limits = {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.25, "resetsAt": self.reset},
            "seven_day": {"utilization": 0.75, "resetsAt": self.reset + 86400}}}
        worker.claude_accounts.save_limits("first", limits)
        worker.claude_accounts.save_limits("second", {"status": "rejected", "resetsAt": self.reset})
        before = worker.claude_accounts.read_limits()
        with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-rename", "first", "renamed"]), \
                mock.patch("builtins.print"):
            worker.cli.main()
        settings["claude"]["accounts"][0] = dict(original, name="renamed")
        self.assertEqual(json.loads(path.read_text()), settings)
        self.assertEqual(worker.claude_accounts.read_limits(), {
            "renamed": before["first"], "second": before["second"]})

    def test_claude_rename_rejects_missing_duplicate_and_invalid_names(self):
        worker = self.worker()
        path = self.home / "codex-worker.json"
        path.write_text(json.dumps({"claude": {"accounts": [
            {"name": name, "config_dir": str(self.root / name), "token_keychain": "fixture-" + name}
            for name in ("first", "second")]}}))
        worker.claude_accounts.save_limits("first", {"status": "allowed"})
        limits_path = worker.config.TASKS / "claude-limits.json"
        before = path.read_text(), limits_path.read_text()
        for old, new, message in (("missing", "new", "does not exist"),
                                  ("first", "second", "already exists"),
                                  ("first", "Invalid_Name", "only a-z")):
            with self.subTest(old=old, new=new), \
                    mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-rename", old, new]), \
                    self.assertRaisesRegex(SystemExit, message):
                worker.cli.main()
            self.assertEqual((path.read_text(), limits_path.read_text()), before)

    def test_claude_rename_materializes_implicit_default_and_legacy_limits(self):
        worker = self.worker()
        path = self.home / "codex-worker.json"
        directory = str(self.root / "legacy-claude")
        path.write_text(json.dumps({"reviewer": "claude", "claude": {
            "config_dir": directory, "token_keychain": "fixture-legacy-service", "model": "fixture-model"}}))
        limits = {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.5, "resetsAt": self.reset}}}
        worker.config.TASKS.mkdir(parents=True, exist_ok=True)
        (worker.config.TASKS / "claude-limits.json").write_text(json.dumps(limits))
        with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-rename", "default", "renamed"]), \
                mock.patch("builtins.print"):
            worker.cli.main()
        saved = json.loads(path.read_text())
        self.assertEqual(saved["claude"]["accounts"], [
            {"name": "renamed", "config_dir": directory, "token_keychain": "fixture-legacy-service"}])
        self.assertEqual(saved["claude"]["model"], "fixture-model")
        self.assertEqual(saved["reviewer"], "claude")
        self.assertEqual(worker.claude_accounts.read_limits(), {"renamed": limits})

    def test_claude_remove_preserves_directory_and_keychain_and_refuses_last_account(self):
        worker = self.worker()
        path = self.home / "codex-worker.json"
        directory = self.root / "removed-claude"
        directory.mkdir()
        credentials = directory / "credentials.json"
        credentials.write_text("fixture-owned-by-claude")
        removed = {"name": "first", "config_dir": str(directory), "token_keychain": "fixture-first-service"}
        retained = {"name": "second", "config_dir": str(self.root / "second-claude"),
                    "token_keychain": "fixture-second-service"}
        settings = {"reviewer": "claude", "claude": {"model": "fixture-model", "accounts": [removed, retained]}}
        path.write_text(json.dumps(settings))
        worker.claude_accounts.save_limits("first", {"status": "allowed"})
        worker.claude_accounts.save_limits("second", {"status": "rejected", "resetsAt": self.reset})
        retained_limits = worker.claude_accounts.read_limits()["second"]
        with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-remove", "first"]), \
                mock.patch.object(worker.commands.subprocess, "run") as launched, \
                mock.patch.object(worker.commands.subprocess, "Popen") as spawned, \
                mock.patch("builtins.print") as output:
            worker.cli.main()
        launched.assert_not_called()
        spawned.assert_not_called()
        self.assertEqual(credentials.read_text(), "fixture-owned-by-claude")
        settings["claude"]["accounts"] = [retained]
        self.assertEqual(json.loads(path.read_text()), settings)
        self.assertEqual(worker.claude_accounts.read_limits(), {"second": retained_limits})
        printed = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn(str(directory), printed)
        self.assertIn(removed["token_keychain"], printed)
        with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-remove", "second"]), \
                self.assertRaisesRegex(SystemExit, "switch reviewer to codex"):
            worker.cli.main()
        self.assertEqual(json.loads(path.read_text()), settings)
        self.assertEqual(worker.claude_accounts.read_limits(), {"second": retained_limits})

    def test_claude_remove_refuses_last_implicit_default(self):
        worker = self.worker()
        path = self.home / "codex-worker.json"
        settings = {"reviewer": "claude", "claude": {"config_dir": str(self.root / "legacy-claude"),
                    "token_keychain": "fixture-legacy-service"}}
        path.write_text(json.dumps(settings))
        worker.claude_accounts.save_limits("default", {"status": "allowed"})
        with mock.patch.object(worker.cli.sys, "argv", ["codex_task.py", "claude-remove", "default"]), \
                self.assertRaisesRegex(SystemExit, "switch reviewer to codex"):
            worker.cli.main()
        self.assertEqual(json.loads(path.read_text()), settings)
        self.assertEqual(worker.claude_accounts.read_limits(), {"default": {"status": "allowed"}})
