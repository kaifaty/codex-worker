"""Claude review subprocesses use a separate credential and leave work to Codex."""
import contextlib
import datetime
import fcntl
import io
import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from rotation_support import PYTHON, SCRIPT, RotationTestCase
from worker_loader import WORKER_BOOTSTRAP, load_worker


TEST_TOKEN = "fixture-claude-reviewer-oauth-secret"
FAKE_SECURITY = r'''import json
import os
import sys
from pathlib import Path

with Path(os.environ["CODEX_FAKE_SECURITY_TRACE"]).open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\n")
if (sys.argv[1:3] != ["find-generic-password", "-s"]
        or not sys.argv[3].startswith("codex-worker-claude-")
        or sys.argv[4:] != ["-a", "fixture-user", "-w"]):
    sys.exit("unexpected fake keychain lookup")
fixture = json.loads(Path(os.environ["CODEX_FAKE_CLAUDE_FIXTURE"]).read_text())
lookup = fixture.get("security", {}).get(sys.argv[3], {})
if lookup.get("unavailable"):
    sys.exit(44)
print("fixture-claude-reviewer-oauth-secret")
'''

FAKE_CLAUDE = r'''import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

clock = time
if os.environ.get("CODEX_TASK_CLOCK_FILE"):
''' + "\n".join("    " + line for line in WORKER_BOOTSTRAP.splitlines()) + r'''
    worker = load_worker(name="fake_claude_clock", script=os.environ["CODEX_FAKE_WORKER_SCRIPT"])
    clock = worker.clock.CLOCK

fixture_path = Path(os.environ["CODEX_FAKE_CLAUDE_FIXTURE"])
fixture = json.loads(fixture_path.read_text())
counter = fixture_path.with_suffix(".count")
number = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(number + 1))
runs = fixture["runs"]
run = runs[min(number, len(runs) - 1)]
trace_path = Path(os.environ["CODEX_FAKE_CLAUDE_TRACE"])

def trace(value):
    value.update(pid=os.getpid(), pgid=os.getpgrp())
    with trace_path.open("a") as stream:
        stream.write(json.dumps(value) + "\n")

def emit(value):
    print(json.dumps(value), flush=True)

session_id = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else str(uuid.uuid4())
system_prompt = (Path(sys.argv[sys.argv.index("--append-system-prompt-file") + 1]).read_text()
                 if "--append-system-prompt-file" in sys.argv else None)
trace({"command": "review", "argv": sys.argv[1:], "cwd": os.getcwd(),
       "session_id": session_id, "system_prompt": system_prompt,
       "token_matches": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "fixture-claude-reviewer-oauth-secret",
       "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
       "disable_background_tasks": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
       "inherited_provider_keys": sorted(key for key in os.environ
           if (key.startswith("CLAUDE") or key.startswith("ANTHROPIC"))
           and key not in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
                          "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")),
       "stdin": sys.stdin.read()})
stopped = [False]

def interrupted(signum, frame):
    trace({"command": "signal", "signal": signum})
    stopped[0] = True

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake-claude"})
if run.get("delay"):
    clock.sleep(run["delay"])
if run.get("gate"):
    while not Path(run["gate"]).exists():
        time.sleep(0.01)
mode = run.get("mode", "complete")
if mode == "auth_error":
    status = run.get("status", 401)
    for attempt in range(2):
        emit({"type": "system", "subtype": "api_retry", "attempt": attempt + 1,
              "error": "authentication_failed", "error_status": status})
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "api_error_status": status})
    sys.exit(1)
limits = run.get("limits", fixture.get("limits"))
if limits and mode != "rejected_buffered":
    emit({"type": "rate_limit_event", "rate_limit_info": limits})
if mode == "rejected_shutdown":
    while not stopped[0]:
        time.sleep(0.02)
if mode == "hold":
    child = os.fork() if run.get("child") else None
    if child == 0:
        trace({"command": "holding_child"})
        while not stopped[0]:
            time.sleep(0.02)
        sys.exit(130)
    trace({"command": "holding"})
    while not stopped[0]:
        time.sleep(0.02)
    if child is not None:
        os.waitpid(child, 0)
    sys.exit(130)
if mode == "modify":
    Path(run.get("path", "reviewer-change.txt")).write_text("Reviewer must leave this visible.\n")
if mode == "move_head":
    import subprocess
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "Reviewer moved HEAD"], check=True)
if mode == "invalid_json":
    print('{"type":"result",broken', flush=True)
    sys.exit(0)
if run.get("non_json"):
    print(run["non_json"], flush=True)
for filename, content in run.get("write_files", {}).items():
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    Path(filename).write_text(content)
if run.get("commit_files"):
    import subprocess
    subprocess.run(["git", "add", "--"] + run["commit_files"], check=True)
    subprocess.run(["git", "commit", "-qm", "Implement fixture task"], check=True)
if run.get("push"):
    import subprocess
    subprocess.run(["git", "push", "--quiet", "-u", "origin", "HEAD"], check=True)
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
      "input": {"command": "git diff --stat"}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "fixture diff"}]}})
if run.get("assistant_text"):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": run["assistant_text"]}]}})
result = {"type": "result", "subtype": "success", "is_error": mode == "is_error",
          "result": run.get("text", "OK"),
          "structured_output": run.get("reply"), "num_turns": 3,
          "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30, "output_tokens": 40},
          "total_cost_usd": run.get("cost_usd", 0.125)}
if run.get("omit_structured_output"):
    result.pop("structured_output")
if run.get("omit_result_text"):
    result.pop("result")
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "StructuredOutput",
      "input": run.get("reply")}]}})
if mode == "rejected_buffered":
    os.write(1, (json.dumps({"type": "rate_limit_event", "rate_limit_info": limits}) + "\n"
                + json.dumps(result) + "\n").encode())
else:
    emit(result)
sys.exit(run.get("exit_code", 0))
'''


class ClaudeReviewTests(RotationTestCase):
    def test_claude_work_turn_argv_environment_and_result(self):
        result = self.work_result()
        raw_line = '\n \t\n{"type" : "fixture", "note" : "café"}\r'
        self.claude_fixture(runs=[{"mode": "modify", "reply": result, "non_json": raw_line},
                                  {"reply": result}])
        worker = self.worker()
        account = worker.claude_accounts.accounts(worker.config.accounts_config())[0]
        task_dir = self.root / "work-turn-task"
        task_dir.mkdir()
        (self.repo / "AGENTS.md").write_text("Root task instructions.\n")
        prompts = []
        previous_events = b""
        with mock.patch.dict(os.environ, self.env, clear=True):
            for options in ({"model": "claude-work-model", "effort": "xhigh"},
                            {"session_id": "prior-session", "max_turns": 19, "effort": "max"}):
                response = worker.claude_review.claude_runner.run_work_turn(
                    account, self.repo, task_dir, "Implement the task.", lambda: False, **options)
                self.assertEqual(response["outcome"], "completed")
                self.assertEqual(response["result"], result)
                telemetry = response["telemetry"]
                self.assertTrue(telemetry["session_id"])
                self.assertEqual(telemetry["model"], "fake-claude")
                self.assertEqual(telemetry["reasoning_effort"], options["effort"])
                self.assertEqual(telemetry["usage"], {
                    "input_tokens": 100, "output_tokens": 40,
                    "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30})
                self.assertEqual(telemetry["cost_usd"], 0.125)
                self.assertEqual(telemetry["num_turns"], 3)
                events = (task_dir / "claude-events.jsonl").read_bytes()
                self.assertTrue(events.startswith(previous_events))
                turn_events = events[len(previous_events):]
                self.assertTrue(turn_events.endswith(b"\n"))
                if not previous_events:
                    self.assertIn((raw_line + "\n").encode(), turn_events)
                decoded = [json.loads(line) for line in turn_events.splitlines() if line.strip()]
                self.assertEqual([event["type"] for event in decoded],
                                 ["system", "rate_limit_event"] + (["fixture"] if not previous_events else [])
                                 + ["assistant", "user", "assistant", "result"])
                self.assertEqual(decoded[-1]["structured_output"], result)
                for secret in (TEST_TOKEN, "host-account-secret", str(self.claude_config_dir)):
                    self.assertNotIn(secret.encode(), events)
                previous_events = events
                entry = self.claude_invocations()[-1]
                self.assertTrue(entry["token_matches"])
                self.assertEqual(entry["config_dir"], str(self.claude_config_dir))
                self.assertEqual(entry["disable_background_tasks"], "1")
                self.assertEqual(entry["inherited_provider_keys"], [])
                self.assertEqual(entry["cwd"], str(self.repo))
                self.assertEqual(entry["pid"], entry["pgid"])
                self.assertEqual(entry["stdin"], "")
                argv = entry["argv"]
                self.assertEqual(argv[:2], ["-p", "Implement the task."])
                self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
                self.assertIn("--verbose", argv)
                self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]),
                                 worker.result_schema.RESULT_SCHEMA)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
                self.assertIn("--strict-mcp-config", argv)
                self.assertNotIn("--mcp-config", argv)
                self.assertNotIn("--allowedTools", argv)
                self.assertNotIn("--disallowedTools", argv)
                for key in ("session_id", "max_turns", "model", "effort"):
                    flag = "--" + {"session_id": "resume", "max_turns": "max-turns"}.get(key, key)
                    if key in options:
                        self.assertEqual(argv[argv.index(flag) + 1], str(options[key]))
                    else:
                        self.assertNotIn(flag, argv)
                path = Path(argv[argv.index("--append-system-prompt-file") + 1])
                self.assertEqual(path.parent, task_dir)
                prompts.append(path)
                text = entry["system_prompt"]
                self.assertIn(
                    "This turn runs headless: background commands and waiting for a notification end the turn "
                    "and kill whatever runs in the background. Run long jobs in the foreground; if one outlasts "
                    "a single command's timeout, start it with `nohup … > LOG 2>&1 &` and poll it in a blocking "
                    "loop of at most 10 minutes per call until it ends.", text)
                self.assertIn("git worktree", text)
                self.assertIn("Root task instructions.\n", text)
                self.assertIn("nested AGENTS.md", text)
                self.assertFalse(path.exists())
        self.assertNotEqual(*prompts)
        self.assertEqual((self.repo / "reviewer-change.txt").read_text(),
                         "Reviewer must leave this visible.\n")
        self.assertEqual(len(self.security_trace_path.read_text().splitlines()), 2)

    def test_claude_work_turn_system_prompt_bounds_root_instructions(self):
        self.claude_fixture(runs=[{"reply": self.work_result()}])
        worker = self.worker()
        account = worker.claude_accounts.accounts(worker.config.accounts_config())[0]
        task_dir = self.root / "work-turn-task"
        task_dir.mkdir()
        root_rules = self.repo / "AGENTS.md"
        root_rules.write_bytes(b"a" * 65536 + b"ROOT-CONTENT-PAST-LIMIT")
        with mock.patch.dict(os.environ, self.env, clear=True):
            response = worker.claude_review.claude_runner.run_work_turn(
                account, self.repo, task_dir, "Work.", lambda: False)
            self.assertEqual(response["outcome"], "completed")
            argv = self.claude_invocations()[-1]["argv"]
            text = self.claude_invocations()[-1]["system_prompt"]
            self.assertIn("a" * 65536, text)
            self.assertIn("[AGENTS.md truncated at 64 KiB]", text)
            self.assertNotIn("ROOT-CONTENT-PAST-LIMIT", text)
            root_rules.unlink()
            response = worker.claude_review.claude_runner.run_work_turn(
                account, self.repo, task_dir, "Work again.", lambda: False)
            self.assertEqual(response["outcome"], "completed")
            argv = self.claude_invocations()[-1]["argv"]
            text = self.claude_invocations()[-1]["system_prompt"]
            self.assertIn("git worktree", text)
            self.assertIn("nested AGENTS.md", text)
            self.assertEqual(len(text.strip().split("\n\n")), 2)
            self.assertNotIn("truncated", text)

    def test_claude_work_turn_ignores_non_json_but_review_rejects_it(self):
        diagnostic = "Unexpected stream output " + TEST_TOKEN
        self.claude_fixture(runs=[{"reply": self.work_result(), "non_json": diagnostic},
                                  {"reply": self.review_result(False), "non_json": diagnostic}])
        worker = self.worker()
        account = worker.claude_accounts.accounts(worker.config.accounts_config())[0]
        task_dir = self.root / "work-turn-task"
        task_dir.mkdir()
        with mock.patch.dict(os.environ, self.env, clear=True):
            response = worker.claude_review.claude_runner.run_work_turn(
                account, self.repo, task_dir, "Work.", lambda: False)
            self.assertEqual(response["outcome"], "completed")
            self.assertEqual(response["result"], self.work_result())
            self.assertEqual((task_dir / "stderr.log").read_text(),
                             "Unexpected stream output [REDACTED]\n")
            self.assertFalse(list(task_dir.glob("claude-system-*.md")))
            events = (task_dir / "claude-events.jsonl").read_bytes()
            self.assertNotIn(TEST_TOKEN.encode(), events)
            self.assertIn(b"Unexpected stream output [REDACTED]\n", events)
            response = worker.claude_review.claude_runner.run_claude(
                account, self.repo, "Review.", lambda: False, reviewing=True)
            self.assertEqual(response["outcome"], "unavailable")
            self.assertEqual(response["reason"], "invalid JSON output")
            self.assertIsNone(response["result"])
            self.assertEqual((task_dir / "claude-events.jsonl").read_bytes(), events)
            self.assertFalse((self.repo / "claude-events.jsonl").exists())

    def test_claude_event_log_unwritable_path_preserves_work_and_review_results(self):
        replies = [self.work_result(), self.review_result(False)]
        self.claude_fixture(runs=[{"reply": reply} for reply in replies])
        worker = self.worker()
        account = worker.claude_accounts.accounts(worker.config.accounts_config())[0]
        task_dir = self.root / "work-turn-task"
        (task_dir / "claude-events.jsonl").mkdir(parents=True)
        with mock.patch.dict(os.environ, self.env, clear=True):
            for reviewing, reply in enumerate(replies):
                with self.subTest(reviewing=bool(reviewing)):
                    if reviewing:
                        response = worker.claude_review.claude_runner.run_claude(
                            account, self.repo, "Review.", lambda: False, reviewing=True,
                            task_dir=task_dir)
                    else:
                        response = worker.claude_review.claude_runner.run_work_turn(
                            account, self.repo, task_dir, "Work.", lambda: False)
                    self.assertEqual(response["outcome"], "completed")
                    self.assertEqual(response["result"], reply)
                    self.assertEqual((task_dir / "stderr.log").read_text().splitlines(),
                                     ["Could not append to claude-events.jsonl."] * (reviewing + 1))

    def test_claude_work_turn_unknown_results_and_unavailable_accounts(self):
        rejected = {"status": "rejected", "utilization": 1, "resetsAt": self.reset,
                    "unifiedWindows": {"seven_day": {"utilization": 1, "resetsAt": self.reset}}}
        self.claude_fixture(runs=[
            {"reply": {"status": "done"}, "text": "Final invalid answer " + TEST_TOKEN},
            {"omit_structured_output": True, "omit_result_text": True,
             "assistant_text": "Last assistant answer."},
            {"omit_structured_output": True, "text": "Failed final answer.", "exit_code": 9},
            {"mode": "is_error", "text": "Reported work failure."},
            {"limits": rejected, "reply": self.work_result()},
            {"mode": "auth_error", "status": 401}])
        worker = self.worker()
        account = worker.claude_accounts.accounts(worker.config.accounts_config())[0]
        task_dir = self.root / "work-turn-task"
        task_dir.mkdir()
        with mock.patch.dict(os.environ, self.env, clear=True):
            for expected_raw in ("Final invalid answer [REDACTED]", "Last assistant answer."):
                response = worker.claude_review.claude_runner.run_work_turn(
                    account, self.repo, task_dir, "Work.", lambda: False)
                self.assertEqual(response["outcome"], "unknown")
                self.assertEqual(response["reason"], "invalid structured output")
                self.assertIsNone(response["result"])
                self.assertEqual(response["raw_message"], expected_raw)
                self.assertIn(expected_raw, worker.result_schema.result_lines(None, response["raw_message"]))
            for expected_raw in ("Failed final answer.", "Reported work failure."):
                response = worker.claude_review.claude_runner.run_work_turn(
                    account, self.repo, task_dir, "Work.", lambda: False)
                self.assertEqual(response["outcome"], "unknown")
                self.assertEqual(response["reason"], "work turn failed")
                self.assertIsNone(response["result"])
                self.assertEqual(response["raw_message"], expected_raw)
            response = worker.claude_review.claude_runner.run_work_turn(
                account, self.repo, task_dir, "Work.", lambda: False)
            self.assertEqual(response["outcome"], "unavailable")
            self.assertEqual(response["reason"], "usage limit")
            self.assertIsNone(response["result"])
            limits = worker.claude_accounts.read_limits()[account["name"]]
            for key, value in rejected.items():
                self.assertEqual(limits[key], value)
            response = worker.claude_review.claude_runner.run_work_turn(
                account, self.repo, task_dir, "Work.", lambda: False)
            self.assertEqual(response["outcome"], "unavailable")
            self.assertEqual(response["reason"], "authentication failed")
        self.assertFalse(list(task_dir.glob("claude-system-*.md")))

    def test_claude_work_turn_stop_deadline_and_invalid_effort(self):
        self.enable_fake_clock(auto_advance=False)
        self.claude_fixture(runs=[{"mode": "hold", "child": True}])
        worker = self.worker()
        account = worker.claude_accounts.accounts(worker.config.accounts_config())[0]
        task_dir = self.root / "work-turn-task"
        task_dir.mkdir()
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ValueError, "effort.*low.*medium.*high.*xhigh.*max"):
                worker.claude_review.claude_runner.run_work_turn(
                    account, self.repo, task_dir, "Work.", lambda: False, effort="ultra")
            self.assertFalse(self.claude_invocations())
            self.assertFalse(self.security_trace_path.exists())
            for expected in ("stopped", "budget_exhausted"):
                self.claude_trace_path.unlink(missing_ok=True)
                advanced = [False]

                def stopped():
                    ready = any(event["command"] == "holding_child" for event in self.claude_trace())
                    if ready and expected == "budget_exhausted" and not advanced[0]:
                        self.advance_fake_clock(61)
                        advanced[0] = True
                    return ready and expected == "stopped"

                response = worker.claude_review.claude_runner.run_work_turn(
                    account, self.repo, task_dir, "Work.", stopped,
                    deadline=worker.clock.CLOCK.monotonic() + 60)
                self.assertEqual(response["outcome"], expected)
                trace = self.claude_trace()
                parent = self.claude_invocations()[-1]
                child = next(event for event in trace if event["command"] == "holding_child")
                self.extra_pids.update((parent["pid"], child["pid"]))
                self.assertEqual(parent["pid"], parent["pgid"])
                self.assertEqual(child["pgid"], parent["pgid"])
                for pid in (parent["pid"], child["pid"]):
                    self.assertFalse(self.process_is_live(pid))
                    self.assertTrue(any(event["command"] == "signal" and event["signal"] == 2
                                        and event["pid"] == pid for event in trace))
                self.assertFalse(list(task_dir.glob("claude-system-*.md")))

    def claude_fixture(self, runs=None, **settings):
        self.claude = self.root / "claude"
        self.claude.write_text("#!" + PYTHON + "\n" + FAKE_CLAUDE)
        self.claude.chmod(0o700)
        security = self.root / "security"
        security.write_text("#!" + PYTHON + "\n" + FAKE_SECURITY)
        security.chmod(0o700)
        self.claude_trace_path = self.root / "claude-trace.jsonl"
        self.claude_fixture_path = self.root / "claude-fixture.json"
        self.security_trace_path = self.root / "security-trace.jsonl"
        self.claude_config_dir = self.root / "claude-account-reviewer"
        self.env.update(HOME=str(self.root / "user-home"), USER="fixture-user",
                        CODEX_TASK_CLAUDE=str(self.claude),
                        CODEX_FAKE_CLAUDE_TRACE=str(self.claude_trace_path),
                        CODEX_FAKE_CLAUDE_FIXTURE=str(self.claude_fixture_path),
                        CODEX_FAKE_SECURITY_TRACE=str(self.security_trace_path),
                        ANTHROPIC_BASE_URL="https://host-account.example.test",
                        ANTHROPIC_API_KEY="host-account-secret", CLAUDECODE="1",
                        CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="0",
                        CLAUDE_CODE_ENTRYPOINT="desktop", CLAUDE_OTHER="host-setting")
        self.claude_limits = {"status": "allowed_warning", "rateLimitType": "seven_day",
                              "utilization": 0.5, "resetsAt": self.reset,
                              "unifiedWindows": {
                                  "five_hour": {"utilization": 0.25, "resetsAt": self.reset - 3600},
                                  "seven_day": {"utilization": 0.5, "resetsAt": self.reset}}}
        self.claude_fixture_path.write_text(json.dumps({
            "runs": runs or [{"reply": self.review_result(False)}], "limits": self.claude_limits}))
        (self.home / "claude-tasks" / "claude-limits.json").unlink(missing_ok=True)
        options = {"config_dir": str(self.claude_config_dir)}
        options.update(settings.pop("claude", {}))
        self.configure(language="en", claude=options, **settings)
        # Review tests script actual reviews; freshness tests expire these snapshots.
        worker = self.worker()
        for account in worker.claude_accounts.accounts(worker.config.accounts_config()):
            worker.claude_accounts.save_limits(account["name"], {"unifiedWindows": {}})

    def claude_trace(self):
        if not self.claude_trace_path.exists():
            return []
        return [json.loads(line) for line in self.claude_trace_path.read_text().splitlines()]

    def claude_invocations(self):
        return [entry for entry in self.claude_trace() if entry["command"] == "review"]

    def claude_pool_fixture(self, runs=None, names=("first", "second")):
        accounts = [{"name": name, "config_dir": str(self.root / ("claude-" + name)),
                     "token_keychain": "codex-worker-claude-" + name}
                    for name in names]
        self.claude_fixture(runs=runs, claude={"accounts": accounts})
        return accounts

    def assert_claude_review_telemetry(self, count):
        self.until(lambda: len([row for row in self.phase_telemetry_journal()
                                if row["phase"] == "review"]) == count)
        reviews = [row for row in self.phase_telemetry_journal() if row["phase"] == "review"]
        self.assertEqual([row["reviewer"] for row in reviews], ["claude"] * count)
        for row in reviews:
            self.assertEqual(row["model"], "fake-claude")
            self.assertEqual(row["usage"][0]["model"], "fake-claude")
            self.assertEqual(row["cost_usd"], 0.125)
            self.assertEqual(row["tokens"], {"input": 150, "cached_input": 30, "output": 40,
                                             "reasoning": None})
            self.assertIsNone(row["weekly_used_before"])
            self.assertIsNone(row["weekly_used_after"])
            self.assertEqual(row["weekly_spent"], 0)

    def test_claude_review_findings_are_fixed_by_codex_with_isolated_credentials(self):
        self.claude_fixture(runs=[{"reply": self.review_result()},
                                  {"reply": self.review_result(False)}],
                            claude={"model": "claude-test", "effort": "high", "max_turns": 17})
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Fixed by Codex."}
                     for finding in self.review_result()["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result(findings=decisions)}])
        start_output = self.start("claude-fix", "--review", "--reviewer", "claude", "--fix-rounds", "1",
                                  "--model", "work-model", "--review-model", "codex-review-model")
        output = self.wait("claude-fix")
        state = self.state("claude-fix")
        self.assertEqual(state["reviewer"], "claude")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(state["rounds"][0]["decisions"], decisions)
        codex = self.invocations()
        self.assertEqual([entry["review"] for entry in codex], [False, False])
        for entry in codex:
            self.assertEqual(entry["argv"][entry["argv"].index("--model") + 1], "work-model")
        self.assertTrue(codex[1]["resume"])
        self.assertIn(self.review_result()["findings"][0]["detail"], codex[1]["prompt"])
        invocations = self.claude_invocations()
        self.assertEqual(len(invocations), 2)
        self.assertRegex(output, r"== review by claude/default \(\d+(?:\.\d+)? min\)")
        self.assertNotRegex(output, r"review by claude[^\n]*%")
        for entry in invocations:
            self.assertTrue(entry["token_matches"])
            self.assertEqual(entry["config_dir"], str(self.claude_config_dir))
            self.assertEqual(entry["disable_background_tasks"], "1")
            self.assertEqual(entry["inherited_provider_keys"], [])
            self.assertEqual(entry["stdin"], "")
            self.assertEqual(entry["cwd"], state["worktree"])
            self.assertEqual(entry["pid"], entry["pgid"])
            argv = entry["argv"]
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep,Glob,Bash")
            self.assertEqual(argv[argv.index("--disallowedTools") + 1], "Edit,Write,NotebookEdit")
            self.assertEqual(argv[argv.index("--max-turns") + 1], "17")
            self.assertEqual(argv[argv.index("--model") + 1], "claude-test")
            self.assertEqual(argv[argv.index("--effort") + 1], "high")
            schema = json.loads(argv[argv.index("--json-schema") + 1])
            self.assertEqual(set(schema["properties"]), set(self.review_result()))
        self.assertEqual(len(self.security_trace_path.read_text().splitlines()), 2)
        self.assert_claude_review_telemetry(2)
        reviews = [row for row in self.phase_telemetry_journal() if row["phase"] == "review"]
        self.assertEqual([row["reasoning_effort"] for row in reviews], ["high", "high"])
        self.assertEqual([row["usage"][0]["reasoning_effort"] for row in reviews], ["high", "high"])
        status = self.cli("status", "claude-fix")
        self.assertIn(self.review_result(False)["summary"], status)
        self.assertEqual(json.loads((self.home / "claude-tasks" / "claude-fix" / "review.md").read_text()),
                         state["reviews"][-1]["result"])
        events = [json.loads(line) for line in
                  (self.home / "claude-tasks/claude-fix/claude-events.jsonl").read_text().splitlines()]
        self.assertEqual([event["structured_output"] for event in events if event["type"] == "result"],
                         [self.review_result(), self.review_result(False)])
        self.assertNotIn(TEST_TOKEN, start_output + output)
        for directory in (self.home, self.other):
            for path in directory.rglob("*"):
                if path.is_file():
                    self.assertNotIn(TEST_TOKEN.encode(), path.read_bytes(), str(path))
        self.assertIn("== main:", self.cli("stats", "claude-fix"))
        self.cli("clean", "claude-fix")
        self.assertFalse((self.home / "claude-tasks" / "claude-fix").exists())

    def test_claude_review_angles_each_use_a_process_and_codex_fixes_the_findings(self):
        self.claude_fixture(runs=[{"reply": self.review_result()}, {"reply": self.review_result()},
                                  {"reply": self.review_result(False)}, {"reply": self.review_result(False)}])
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Fixed by Codex."}
                     for finding in self.review_result()["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result(findings=decisions)}])
        self.start("claude-angles", "--reviewer", "claude", "--review-angles", "correctness,concurrency",
                   "--fix-rounds", "1")
        output = self.wait("claude-angles")
        state = self.state("claude-angles")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"][0]["review"]["findings"]), 2)
        self.assertEqual([entry["review"] for entry in self.invocations()], [False, False])
        claude = self.claude_invocations()
        self.assertEqual(len(claude), 4)
        self.assertEqual(len({entry["pid"] for entry in claude}), 4)
        for index, angle in enumerate(("correctness", "concurrency", "correctness", "concurrency")):
            self.assertIn("Review angle: " + angle, claude[index]["argv"][1])
            self.assertRegex(output, r"== review angle " + angle + r" by claude/default \(\d+(?:\.\d+)? min\)")
        self.assert_claude_review_telemetry(4)
        self.assertEqual([row["reasoning_effort"] for row in self.phase_telemetry_journal()
                          if row["phase"] == "review"], [None] * 4)

    def test_claude_unavailable_events_fall_back_to_codex(self):
        cases = (("rejected", {"limits": {"status": "rejected", "rateLimitType": "seven_day",
                                           "utilization": 1, "resetsAt": self.reset}}),
                 ("auth401", {"mode": "auth_error", "status": 401}),
                 ("auth403", {"mode": "auth_error", "status": 403}),
                 ("invalid-json", {"mode": "invalid_json"}),
                 ("invalid-schema", {"reply": {"verdict": "clean"}}),
                 ("is-error", {"mode": "is_error"}))
        for label, run in cases:
            with self.subTest(case=label):
                self.claude_fixture(runs=[dict({"reply": self.review_result(False)}, **run)])
                self.claude_fixture_path.with_suffix(".count").unlink(missing_ok=True)
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": self.review_result(False)}])
                slug = "claude-fallback-" + label
                self.start(slug, "--review", "--reviewer", "claude")
                output = self.wait(slug)
                self.assertEqual(self.state(slug)["review_result"]["verdict"], "clean")
                self.assertTrue(self.invocations()[-1]["review"])
                if label in ("rejected", "auth401", "auth403"):
                    self.assertRegex(output, r"== claude account default limited until [^\n]+; review by codex")
                else:
                    self.assertRegex(output, r"== reviewer claude unavailable: [^\n]+; review by codex")
                self.assertNotIn(TEST_TOKEN, output)
                rows = [row for row in self.phase_telemetry_journal()
                        if row["slug"] == slug and row["phase"] == "review"]
                self.assertTrue(any(row["reviewer"] == "codex" for row in rows), rows)
                if label == "rejected":
                    report = json.loads(self.cli("report", "--slug", slug, "--json"))
                    self.assertEqual(report["tasks"][0]["last_verdict"], "clean")
                    self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"0": 1})
                    self.assertEqual({reviewer: value["reviews"] for reviewer, value
                                      in report["summary"]["reviewers"].items()},
                                     {"codex": 1, "claude": 1})
                    self.cli("clean", slug)
                    self.assertFalse((self.home / "claude-tasks" / slug).exists())

    def test_claude_missing_after_start_preflight_falls_back_to_codex(self):
        self.claude_fixture()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        remove = "from pathlib import Path; Path(" + repr(str(self.claude)) + ").unlink()"
        check = shlex.quote(PYTHON) + " -I -c " + shlex.quote(remove)
        self.start("claude-vanished", "--review", "--reviewer", "claude", "--check", check)
        output = self.wait("claude-vanished")
        self.assertFalse(self.claude.exists())
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertRegex(output, r"== reviewer claude unavailable: [^\n]+; review by codex")

    def test_claude_rejected_preserves_buffered_and_shutdown_usage(self):
        for mode in ("rejected_buffered", "rejected_shutdown"):
            with self.subTest(mode=mode):
                reply = self.review_result(False)
                if mode == "rejected_shutdown":
                    # The final JSON exceeds pipe capacity and must be read
                    # while the supervisor waits for graceful shutdown.
                    reply["summary"] = "Large final review. " * 20000
                self.claude_fixture(runs=[{"mode": mode, "reply": reply,
                                           "limits": {"status": "rejected"}}])
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": self.review_result(False)}])
                slug = "claude-" + mode.replace("_", "-")
                self.start(slug, "--review", "--reviewer", "claude")
                output = self.wait(slug)
                self.assertRegex(output, r"== claude account default limited until [^\n]+; review by codex")
                rows = [row for row in self.phase_telemetry_journal()
                        if row["slug"] == slug and row["phase"] == "review"]
                self.assertEqual([row["reviewer"] for row in rows], ["claude", "codex"])
                self.assertEqual(rows[0]["cost_usd"], 0.125)
                self.assertEqual(rows[0]["tokens"], {"input": 150, "cached_input": 30,
                                                     "output": 40, "reasoning": None})
                self.assertEqual(self.state(slug)["reviews"][0]["outcome"], "unavailable")
                self.assertIsNone(self.state(slug)["reviews"][0]["result"])
                report = json.loads(self.cli("report", "--slug", slug, "--json"))
                self.assertEqual(report["summary"]["reviewers"]["claude"]["cost_usd"], 0.125)

    def test_claude_missing_binary_is_a_clear_start_and_review_error(self):
        self.claude_fixture()
        self.start("claude-missing-review")
        self.wait("claude-missing-review")
        self.env["CODEX_TASK_CLAUDE"] = str(self.root / "missing-claude")
        commands = (("start", "claude-missing-start", "--brief", str(self.brief), "--repo", str(self.repo),
                     "--reviewer", "claude"),
                    ("review", "claude-missing-review", "--reviewer", "claude"))
        for args in commands:
            with self.subTest(command=args[0]):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args), cwd=self.repo,
                                        env=self.env, text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("claude", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.home / "claude-tasks" / "claude-missing-start").exists())

    def test_claude_modifying_worktree_or_head_fails_without_codex_fallback(self):
        for mode in ("modify", "move_head"):
            with self.subTest(mode=mode):
                self.claude_fixture(runs=[{"mode": mode, "reply": self.review_result(False)}])
                self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
                slug = "claude-modified-" + mode.replace("_", "-")
                self.start(slug, "--review", "--reviewer", "claude")
                output = self.cli("wait", slug, "--timeout", "10")
                state = self.state(slug)
                self.assertEqual(state["outcome"], "failed")
                self.assertIn("reviewer modified the worktree", state.get("error", "") + output)
                self.assertFalse(self.review_invocations())
                if mode == "modify":
                    self.assertEqual((Path(state["worktree"]) / "reviewer-change.txt").read_text(),
                                     "Reviewer must leave this visible.\n")

    def test_claude_snapshot_does_not_execute_worker_written_git_filters(self):
        self.claude_fixture(sandbox="workspace")
        self.commit_fixture({".gitattributes": "tracked.txt filter=project\n",
                             "clean.sh": "#!/bin/sh\ncat\n"})
        self.git("config", "filter.project.clean", "/bin/sh clean.sh")
        marker = self.root / "outside-claude-worktree.marker"
        marker.write_text("untouched\n")
        script = "#!/bin/sh\nprintf 'filter executed\\n' > " + shlex.quote(str(marker)) + "\ncat\n"
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"tracked.txt": "worker change\n", "clean.sh": script},
            "reply": self.work_result(commit_message="Do not run a worker-written filter")}])
        self.start("claude-filter-guard", "--review", "--reviewer", "claude")
        output = self.wait("claude-filter-guard")
        state = self.state("claude-filter-guard")
        self.assertEqual(state["outcome"], "completed")
        self.assertIn("filter driver project is configured", state["publish"]["error"])
        self.assertIn("== review by claude/default", output)
        self.assertEqual(marker.read_text(), "untouched\n")
        self.assertEqual((Path(state["worktree"]) / "tracked.txt").read_text(), "worker change\n")

    def test_claude_review_accepts_uninitialized_local_submodule(self):
        source = self.root / "submodule-source"
        source.mkdir()
        self.git("-C", str(source), "init", "--quiet")
        (source / "module.txt").write_text("submodule contents\n")
        self.git("-C", str(source), "add", "module.txt")
        self.git("-C", str(source), "commit", "--quiet", "-m", "Add submodule contents")
        self.git("-c", "protocol.file.allow=always", "submodule", "add", str(source), "local-submodule")
        self.git("commit", "--quiet", "-m", "Add local submodule")
        self.git("push", "--quiet", "origin", "main")
        self.claude_fixture()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-submodule", "--review", "--reviewer", "claude")
        output = self.wait("claude-submodule")
        state = self.state("claude-submodule")
        worktree = Path(state["worktree"])
        self.assertFalse((worktree / "local-submodule" / ".git").exists())
        self.assertTrue(self.git("-C", str(worktree), "submodule", "status").startswith("-"))
        self.assertEqual(state["outcome"], "completed")
        self.assertIn("== review by claude/default", output)
        self.assertNotIn("review by codex", output)
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertFalse(self.review_invocations())

    def test_claude_snapshot_hashes_no_files_in_clean_worktree(self):
        worker = self.worker()
        with mock.patch.object(worker.claude_review.hashlib, "sha256",
                               wraps=worker.claude_review.hashlib.sha256) as sha256:
            worker.claude_review.worktree_snapshot({"worktree": str(self.repo)})
        sha256.assert_not_called()

    def test_claude_overwriting_dirty_or_untracked_contents_fails(self):
        for path in ("tracked.txt", "reviewer-change.txt", "nested/reviewer-change.txt"):
            with self.subTest(path=path):
                self.claude_fixture(runs=[{"mode": "modify", "path": path,
                                           "reply": self.review_result(False)}])
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[{
                    "mode": "complete", "write_files": {path: "Codex work must be preserved.\n"},
                    "reply": self.work_result()}])
                slug = "claude-dirty-" + path.replace("/", "-").replace(".txt", "")
                self.start(slug)
                self.wait(slug)
                worktree = self.state(slug)["worktree"]
                before = self.git("-C", worktree, "status", "--porcelain")
                self.cli("review", slug, "--reviewer", "claude")
                output = self.cli("wait", slug, "--timeout", "10")
                state = self.state(slug)
                self.assertEqual(self.git("-C", worktree, "status", "--porcelain"), before)
                self.assertEqual((Path(worktree) / path).read_text(), "Reviewer must leave this visible.\n")
                self.assertEqual(state["outcome"], "failed")
                self.assertIn("reviewer modified the worktree", output)
                self.assertIsNone(state["reviews"][-1]["result"])
                self.assertFalse(self.review_invocations())

    def test_stop_interrupts_claude_review_process_group_without_fallback(self):
        self.claude_fixture(runs=[{"mode": "hold"}])
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-stop", "--review", "--reviewer", "claude")
        self.until(lambda: any(event["command"] == "holding" for event in self.claude_trace()))
        child = self.claude_invocations()[0]
        self.assertTrue(self.process_is_live(child["pid"]))
        self.cli("stop", "claude-stop", timeout=20)
        output = self.wait("claude-stop", timeout=10)
        self.assertEqual(self.state("claude-stop")["outcome"], "stopped")
        self.assertFalse(self.process_is_live(child["pid"]))
        self.assertEqual(child["pid"], child["pgid"])
        self.assertTrue(any(event["command"] == "signal" and event["signal"] == 2
                            for event in self.claude_trace()))
        self.assertFalse(self.review_invocations())
        self.assertNotIn("review by codex", output)

    def test_claude_review_timeout_preserves_budget_outcome_and_terminates_process(self):
        self.fixture_timeout = 180
        self.enable_fake_clock(auto_advance=False)
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "60"
        self.claude_fixture(runs=[{"mode": "hold"}], max_review_minutes=2)
        work = self.work_result()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": work}])
        self.start("claude-timeout", "--review", "--reviewer", "claude")
        self.until(lambda: any(event["command"] == "holding" for event in self.claude_trace()))
        self.advance_fake_clock(120.1)
        output = self.wait("claude-timeout")
        state = self.state("claude-timeout")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stops"], [dict(
            state["turn_stop"], turn=1, at=state["turn_stops"][0]["at"])])
        self.assertGreaterEqual(state["turn_stops"][0]["at"], state["turn_started"])
        self.assertLessEqual(state["turn_stops"][0]["at"], state["turn_finished"])
        self.assertEqual(state["result"], work)
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertFalse(self.process_is_live(self.claude_invocations()[0]["pid"]))
        self.assertFalse(self.review_invocations())
        self.assertNotIn("review by codex", output)
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)

    def test_claude_pool_retries_share_one_angle_time_budget(self):
        for failure in ("limit", "token"):
            with self.subTest(failure=failure):
                runs = [{"delay": .8, "reply": self.review_result(False)}]
                if failure == "limit":
                    runs.insert(0, {"delay": .8, "limits": {"status": "rejected", "resetsAt": self.reset}})
                self.claude_pool_fixture(runs=runs)
                self.claude_fixture_path.with_suffix(".count").unlink(missing_ok=True)
                self.claude_trace_path.unlink(missing_ok=True)
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
                slug = "claude-pool-timeout-" + failure
                self.start(slug)
                self.wait(slug)
                self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
                self.enable_fake_clock()
                worker = self.worker()
                cfg = worker.config.accounts_config()
                cfg["max_review_minutes"] = 1
                state = self.state(slug)
                state.update(kind="review", outcome="running", review_angle="correctness")
                original_run = worker.claude_review.subprocess.run

                def run(argv, *args, **kwargs):
                    if argv[0] == "security":
                        if failure == "token" and argv[3] == "codex-worker-claude-first":
                            worker.clock.CLOCK.sleep(.8)
                            return subprocess.CompletedProcess(argv, 44, stdout="")
                        return subprocess.CompletedProcess(argv, 0, stdout=TEST_TOKEN + "\n")
                    return original_run(argv, *args, **kwargs)

                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(worker.claude_review.subprocess, "run", side_effect=run):
                    outcome = worker.review_cycle.claude_review_turn(state, cfg, "Review.", lambda: False, set())
                self.assertEqual(outcome, "budget_exhausted")
                self.assertEqual(state["outcome"], "budget_exhausted")
                self.assertEqual([entry["outcome"] for entry in state["reviews"]],
                                 ["unavailable", "budget_exhausted"])
                self.assertEqual(state["turn_stop"], {"outcome": "budget_exhausted", "minutes": 1})
                self.assertEqual(len(state["turn_stops"]), 1)
                self.assertEqual(len(self.claude_invocations()), 2 if failure == "limit" else 1)
                self.assertFalse(self.review_invocations())
                self.assertTrue(all(not self.process_is_live(entry["pid"]) for entry in self.claude_invocations()))

    def test_say_inherits_claude_reviewer_and_explicit_codex_override(self):
        self.claude_fixture(reviewer="codex")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("claude-say", "--reviewer", "claude")
        self.wait("claude-say")
        self.cli("say", "claude-say", "Continue and review", "--review")
        output = self.wait("claude-say")
        self.assertEqual(self.state("claude-say")["reviewer"], "claude")
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertIn("== review by claude/default", output)
        self.cli("say", "claude-say", "Continue using Codex review", "--review", "--reviewer", "codex")
        self.wait("claude-say")
        self.assertEqual(self.state("claude-say")["reviewer"], "codex")
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertEqual(len(self.review_invocations()), 1)

    def test_claude_limits_accounts_and_default_cli_settings(self):
        self.claude_fixture(reviewer="claude")
        self.worker().claude_accounts.save_limits("default", {"unifiedWindows": {
            "five_hour": {"resetsAt": self.reset}, "seven_day": {"resetsAt": self.reset}}})
        self.assertIn("claude default: weight 1; 5h unknown", self.cli("accounts"))
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-limits", "--review")
        self.wait("claude-limits")
        work = self.state("claude-limits")["result"]
        self.cli("review", "claude-limits", "--reviewer", "claude")
        self.wait("claude-limits")
        self.assertEqual(self.state("claude-limits")["result"], work)
        self.assertEqual(len(self.claude_invocations()), 2)
        saved = json.loads((self.home / "claude-tasks" / "claude-limits.json").read_text())
        self.assertEqual(saved["default"]["unifiedWindows"], self.claude_limits["unifiedWindows"])
        output = self.cli("accounts")
        line = next(line for line in output.splitlines() if line.startswith("claude default"))
        self.assertIn("25%", line)
        self.assertIn("50%", line)
        for window in self.claude_limits["unifiedWindows"].values():
            reset = datetime.datetime.fromtimestamp(window["resetsAt"]).strftime("%Y-%m-%d %H:%M")
            self.assertIn(reset, line)
        argv = self.claude_invocations()[0]["argv"]
        self.assertEqual(argv[argv.index("--max-turns") + 1], "40")
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_claude_rate_limit_event_huge_number_is_ignored(self):
        clock = self.enable_fake_clock()
        worker = self.worker()
        huge = 10 ** 400
        event = json.loads(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "utilization": huge, "resetsAt": huge,
            "unifiedWindows": {
                "five_hour": {"utilization": huge, "resetsAt": self.reset},
                "seven_day": {"utilization": .5, "resetsAt": huge}}}}))
        self.assertIsNone(worker.claude_review.event_unavailable(event, "default"))
        self.assertEqual(worker.claude_accounts.read_limits()["default"], {
            "status": "allowed", "updated_at": clock.time(), "unifiedWindows": {
                "five_hour": {"resetsAt": self.reset}, "seven_day": {"utilization": .5}}})

    def test_claude_pool_selects_both_windows_then_earliest_long_reset(self):
        self.claude_pool_fixture()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        cfg.update(soft_switch_at=80, switch_at=99)
        path = self.home / "claude-tasks" / "claude-limits.json"
        path.parent.mkdir(exist_ok=True)

        def limits(short, long, reset):
            return {"unifiedWindows": {"five_hour": {"utilization": short, "resetsAt": self.reset},
                                       "seven_day": {"utilization": long, "resetsAt": reset}}}

        cases = [
            ({"first": limits(.8, .2, self.reset),
              "second": limits(.2, .5, self.reset + 3600)}, (), (), "second"),
            ({"first": limits(.2, .2, self.reset),
              "second": limits(.2, .5, self.reset + 3600)}, (), (), "first"),
            ({"first": limits(.2, .9, self.reset),
              "second": limits(.8, .5, self.reset + 3600)}, (), (), "second"),
            ({"first": limits(.99, .1, self.reset)}, ("second",), (), None),
            ({}, ("second",), (), "first"),
            ({}, (), ("first",), "second"),
            ({"first": limits(1, 1, self.reset - 9000)}, ("second",), (), None),
        ]
        for entries, excluded, used, expected in cases:
            with self.subTest(entries=entries, excluded=excluded, used=used):
                path.write_text(json.dumps(entries))
                account = worker.claude_accounts.select(cfg, "review", exclude=excluded, prefer_unused=used)
                self.assertEqual(account["name"] if account else None, expected)
        expired = limits(1, 1, self.reset - 9000)
        expired["unifiedWindows"]["five_hour"]["resetsAt"] = self.reset - 9000
        path.write_text(json.dumps({"first": dict(expired, status="rejected", limited_until=self.reset - 9000)}))
        self.assertEqual(worker.claude_accounts.select(cfg, "review", exclude=("second",))["name"], "first")

    def test_claude_pool_reads_legacy_settings_and_limit_file(self):
        self.claude_fixture()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        self.assertEqual(cfg["claude"]["accounts"], [{
            "name": "default", "config_dir": str(self.claude_config_dir),
            "token_keychain": "codex-worker-claude-reviewer"}])
        path = self.home / "claude-tasks" / "claude-limits.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.claude_limits))
        self.assertEqual(worker.claude_accounts.read_limits(), {"default": self.claude_limits})
        worker.claude_accounts.save_limits("default", self.claude_limits)
        self.assertIn("default", json.loads(path.read_text()))
        self.configure(claude={})
        defaults = worker.config.accounts_config()["claude"]["accounts"]
        self.assertEqual(defaults[0]["name"], "default")
        self.assertTrue(defaults[0]["config_dir"].endswith("/.claude-accounts/reviewer"))
        self.assertEqual(defaults[0]["token_keychain"], "codex-worker-claude-reviewer")

    def test_claude_even_spends_weighted_weekly_quota_before_reset(self):
        self.claude_pool_fixture()
        clock = self.enable_fake_clock()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        first, second = cfg["claude"]["accounts"]
        first["weight"], second["weight"] = 5, 20
        now = clock.time()

        def save(name, used, hours, short=.1):
            worker.claude_accounts.save_limits(name, {"unifiedWindows": {
                "five_hour": {"utilization": short, "resetsAt": now + 3600},
                "seven_day": {"utilization": used, "resetsAt": now + hours * 3600}}})

        save("first", .13, 120)
        save("second", 0, 168)
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "second")
        save("first", .1, 6)
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "first")
        first.pop("weight")
        second.pop("weight")
        save("first", .1, 120)
        save("second", .1, 168)
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "first")
        save("first", .1, 168, short=.2)
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "second")
        save("first", .1, 168)
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "first")
        save("first", 1, -1)
        save("second", .5, 24)
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "second")

    def test_claude_fresh_selection_refreshes_stale_limits_and_keeps_failed_snapshot(self):
        self.claude_pool_fixture(runs=[{"limits": {"status": "allowed", "unifiedWindows": {
            "seven_day": {"utilization": .9, "resetsAt": self.reset}}}}])
        clock = self.enable_fake_clock()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        path = worker.config.TASKS / "claude-limits.json"
        entries = {name: {"unifiedWindows": {"seven_day": {
            "utilization": used, "resetsAt": self.reset}}}
                   for name, used in (("first", .1), ("second", .2))}
        entries["second"]["updated_at"] = clock.time()
        path.write_text(json.dumps(entries))
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "first")
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(worker.claude_review.select_account(cfg, "review")["name"], "second")
            self.assertEqual(worker.claude_review.select_account(cfg, "review")["name"], "second")
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertEqual(worker.claude_accounts.read_limits()["first"]["updated_at"], clock.time())
        self.assertEqual(worker.claude_accounts.read_limits()["first"]["unifiedWindows"]["seven_day"]["utilization"], .9)

        entries["first"]["updated_at"] = clock.time() - 1801
        path.write_text(json.dumps(entries))
        fixture = json.loads(self.claude_fixture_path.read_text())
        fixture["runs"] = [{"mode": "invalid_json", "limits": {}}]
        self.claude_fixture_path.write_text(json.dumps(fixture))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(worker.claude_review.select_account(cfg, "review")["name"], "first")
        self.assertEqual(len(self.claude_invocations()), 2)
        self.assertEqual(worker.claude_accounts.read_limits()["first"],
                         dict(entries["first"], probe_attempted_at=clock.time()))

    def test_claude_fresh_failed_probe_is_not_repeated_before_next_refresh(self):
        self.claude_pool_fixture(runs=[{"mode": "invalid_json", "limits": {}}], names=("first",))
        clock = self.enable_fake_clock()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        stale = {"updated_at": clock.time() - 1801, "unifiedWindows": {
            "seven_day": {"utilization": .1, "resetsAt": self.reset}}}
        (worker.config.TASKS / "claude-limits.json").write_text(json.dumps({"first": stale}))
        with mock.patch.dict(os.environ, self.env, clear=True):
            for _ in range(2):
                self.assertEqual(worker.claude_review.select_account(cfg, "review")["name"], "first")
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertEqual(worker.claude_accounts.read_limits()["first"],
                         dict(stale, probe_attempted_at=clock.time()))

    def test_claude_fresh_stop_terminates_probe_and_selects_from_cached_limits(self):
        self.claude_pool_fixture(runs=[{"mode": "hold", "limits": {}}])
        clock = self.enable_fake_clock()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        entries = {name: {"updated_at": clock.time() - 1801, "unifiedWindows": {
            "seven_day": {"utilization": used, "resetsAt": self.reset}}}
                   for name, used in (("first", .1), ("second", .5))}
        (worker.config.TASKS / "claude-limits.json").write_text(json.dumps(entries))

        def stopped():
            return any(event["command"] == "holding" for event in self.claude_trace())

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(worker.config, "CLAUDE_PROBE_TIMEOUT", 10):
            started = time.monotonic()
            account = worker.claude_review.select_account(cfg, "review", stopped=stopped)
            self.assertLess(time.monotonic() - started, worker.config.CLAUDE_PROBE_TIMEOUT)
        self.assertEqual(account["name"], "first")
        self.assertEqual(len(self.claude_invocations()), 1)
        child = self.claude_invocations()[0]
        self.assertEqual(child["pid"], child["pgid"])
        self.assertFalse(self.process_is_live(child["pid"]))
        self.assertTrue(any(event["command"] == "signal" and event["signal"] == 2
                            for event in self.claude_trace()))
        self.assertEqual(worker.claude_accounts.read_limits(), {
            "first": dict(entries["first"], probe_attempted_at=clock.time()), "second": entries["second"]})

    def test_claude_fresh_concurrent_selections_probe_once_without_holding_limits_lock(self):
        gate = self.root / "release-probe"
        self.claude_pool_fixture(runs=[{"gate": str(gate)}], names=("first",))
        worker = self.worker()
        worker.claude_accounts.clear_limits("first")
        start = self.root / "start-select"
        code = (WORKER_BOOTSTRAP + "import sys, time\nfrom pathlib import Path\n"
                "worker = load_worker(script=" + repr(str(SCRIPT)) + ")\n"
                "Path(sys.argv[1]).touch()\n"
                "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
                "print(worker.claude_review.select_account(worker.config.accounts_config(), 'review')['name'])\n")
        processes = []
        try:
            for number in range(2):
                process = subprocess.Popen([PYTHON, "-I", "-c", code, str(self.root / ("ready-" + str(number))),
                                            str(start)], cwd=self.root, env=self.env,
                                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                self.extra_pids.add(process.pid)
            self.until(lambda: all((self.root / ("ready-" + str(number))).exists() for number in range(2)))
            start.touch()
            self.until(lambda: len(self.claude_invocations()) == 1)
            # Saving another account must finish while the request is still blocked.
            save_code = (WORKER_BOOTSTRAP + "worker = load_worker(script=" + repr(str(SCRIPT)) + ")\n"
                         "worker.claude_accounts.save_limits('other', {'status': 'allowed'})\n")
            saved = subprocess.run([PYTHON, "-I", "-c", save_code], cwd=self.root, env=self.env,
                                   text=True, capture_output=True, timeout=5)
            self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        finally:
            start.touch()
            gate.touch()
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual(stdout.strip(), "first")
        self.assertEqual(len(self.claude_invocations()), 1)

    def test_claude_fresh_accounts_json_and_refresh_only_stale_unblocked_accounts(self):
        self.claude_pool_fixture(names=("old", "fresh", "blocked"))
        clock = self.enable_fake_clock()
        settings_path = self.home / "claude-accounts.json"
        settings = json.loads(settings_path.read_text())
        settings["claude"]["accounts"][0]["weight"] = 20
        settings_path.write_text(json.dumps(settings))
        worker = self.worker()
        self.assertEqual(worker.config.accounts_config()["claude"]["limits_max_age_minutes"], 30)
        path = worker.config.TASKS / "claude-limits.json"
        entries = worker.claude_accounts.read_limits()
        entries["old"]["updated_at"] = clock.time() - 1801
        entries["blocked"].update(updated_at=clock.time() - 1801, limited_until=self.reset)
        path.write_text(json.dumps(entries))
        reported = json.loads(self.cli("accounts", "--json"))["claude"]
        self.assertEqual([entry["weight"] for entry in reported], [20, 1, 1])
        self.assertEqual([entry["stale"] for entry in reported], [True, False, True])
        self.assertEqual(reported[0]["updated_at"], datetime.datetime.fromtimestamp(
            entries["old"]["updated_at"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(self.claude_invocations(), [])
        self.assertRegex(self.cli("accounts"), r"claude old: weight 20; .*as of .*stale")
        reported = json.loads(self.cli("accounts", "--refresh", "--json"))["claude"]
        self.assertEqual([entry["stale"] for entry in reported], [False, False, True])
        self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()],
                         [settings["claude"]["accounts"][0]["config_dir"]])
        self.cli("accounts", "--refresh")
        self.assertEqual(len(self.claude_invocations()), 1)
        settings["claude"]["limits_max_age_minutes"] = 0
        settings_path.write_text(json.dumps(settings))
        self.cli("accounts", "--refresh")
        self.assertEqual(len(self.claude_invocations()), 3)

    def test_claude_quota_settings_reject_invalid_numbers(self):
        accounts = self.claude_pool_fixture()
        worker = self.worker()
        for field, invalid in (("weight", (0, True, float("nan"))),
                               ("limits_max_age_minutes", (-1, "30", float("inf")))):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    claude = {"accounts": [dict(accounts[0], weight=value)]} if field == "weight" else {field: value}
                    self.configure(claude=claude)
                    with self.assertRaisesRegex(SystemExit, field):
                        worker.config.accounts_config()

    def test_claude_pool_stale_allowed_snapshot_keeps_active_limit(self):
        self.claude_pool_fixture()
        self.enable_fake_clock()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        worker.claude_accounts.save_limits("first", {"status": "rejected", "resetsAt": self.reset})
        worker.claude_accounts.mark_limited("first")
        marked = worker.claude_accounts.read_limits()["first"]
        stale = dict(self.claude_limits, status="allowed_warning", resetsAt=self.reset)
        worker.claude_accounts.save_limits("first", stale, marked["limited_at"] - 1)
        worker.claude_accounts.save_limits("first", stale)
        self.assertEqual(worker.claude_accounts.read_limits()["first"]["limited_until"], self.reset)
        self.assertIsNone(worker.claude_accounts.select(cfg, "review", exclude=("second",)))
        worker.claude_accounts.save_limits("first", stale, marked["limited_at"] + 1)
        self.assertEqual(worker.claude_accounts.select(cfg, "review", exclude=("second",))["name"], "first")

    def test_claude_pool_retries_limited_and_unauthorized_accounts_once(self):
        cases = (("limit", {"limits": {"status": "rejected", "resetsAt": self.reset}}),
                 ("auth", {"mode": "auth_error", "status": 401}))
        for label, first in cases:
            with self.subTest(case=label):
                names = ("first", "codex" if label == "auth" else "second")
                accounts = self.claude_pool_fixture(runs=[first, {"reply": self.review_result(False)}], names=names)
                self.claude_fixture_path.with_suffix(".count").unlink(missing_ok=True)
                self.claude_trace_path.unlink(missing_ok=True)
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
                slug = "claude-pool-" + label
                self.start(slug, "--review", "--reviewer", "claude")
                output = self.wait(slug)
                self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()],
                                 [account["config_dir"] for account in accounts])
                self.assertEqual([entry["account"] for entry in self.state(slug)["reviews"]], list(names))
                self.assertEqual(self.state(slug)["review_result"]["verdict"], "clean")
                switch = [line for line in output.splitlines() if line.startswith("== claude account ")]
                self.assertEqual(len(switch), 1, output)
                self.assertRegex(switch[0], r"^== claude account first limited until .+; review by " + names[1] + r"$")
                self.assertEqual(self.review_invocations(), [])
                rows = [row for row in self.phase_telemetry_journal()
                        if row["slug"] == slug and row["phase"] == "review"]
                self.assertEqual([row["account"] for row in rows], list(names))

    def missing_token_fixture(self, missing=("default",)):
        accounts = self.claude_pool_fixture(names=("default", "first"))
        settings = json.loads((self.home / "claude-accounts.json").read_text())
        settings["claude"]["limits_max_age_minutes"] = 120
        (self.home / "claude-accounts.json").write_text(json.dumps(settings))
        fixture = json.loads(self.claude_fixture_path.read_text())
        fixture["security"] = {"codex-worker-claude-" + name: {"unavailable": True} for name in missing}
        self.claude_fixture_path.write_text(json.dumps(fixture))
        return accounts

    def security_lookups(self):
        return [json.loads(line)[2] for line in self.security_trace_path.read_text().splitlines()]

    def test_claude_pool_skips_missing_token_account_for_one_hour_then_marks_it_again(self):
        accounts = self.missing_token_fixture()
        clock = self.enable_fake_clock()
        worker = self.worker()
        hour = worker.claude_accounts.TOKEN_RETRY_SECONDS
        self.assertEqual(hour, 3600)
        worker.claude_accounts.save_limits("default", self.claude_limits)
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-pool-token", "--review", "--reviewer", "claude")
        output = self.wait("claude-pool-token")
        self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()], [accounts[1]["config_dir"]])
        state = self.state("claude-pool-token")
        self.assertEqual([entry["account"] for entry in state["reviews"]], ["default", "first"])
        self.assertEqual(state["review_result"]["verdict"], "clean")
        self.assertFalse(self.review_invocations())
        marked = worker.claude_accounts.read_limits()["default"]
        self.assertTrue(marked["token_unavailable"])
        self.assertEqual(marked["limited_until"], marked["limited_at"] + hour)
        self.assertEqual(state["reviews"][0]["limited_until"], marked["limited_until"])
        switches = [line for line in output.splitlines() if line.startswith("== claude account ")]
        self.assertEqual(switches, ["== claude account default limited until " +
                                    worker.config.local_time(marked["limited_until"]) +
                                    " (token unavailable); review by first"])
        # A fresh allowed snapshot does not lift the token mark early.
        worker.claude_accounts.save_limits("default", self.claude_limits, marked["limited_at"] + 1)
        cfg = worker.config.accounts_config()
        self.assertIsNone(worker.claude_accounts.select(cfg, "review", exclude=("first",)))
        listing = next(line for line in self.cli("accounts").splitlines() if line.startswith("claude default:"))
        self.assertTrue(listing.endswith("; limited until " + worker.config.local_time(marked["limited_until"]) +
                                         " (token unavailable)"), listing)
        reported = json.loads(self.cli("accounts", "--json"))["claude"][0]
        self.assertTrue(reported["token_unavailable"])
        self.assertEqual(reported["limited_until"], datetime.datetime.fromtimestamp(
            marked["limited_until"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.cli("review", "claude-pool-token", "--reviewer", "claude")
        self.wait("claude-pool-token")
        self.assertEqual(self.security_lookups(), ["codex-worker-claude-default", "codex-worker-claude-first",
                                                   "codex-worker-claude-first"])
        clock.sleep(marked["limited_until"] - clock.time() + 1)
        self.assertEqual(worker.claude_accounts.select(cfg, "review", exclude=("first",))["name"], "default")
        listing = next(line for line in self.cli("accounts").splitlines() if line.startswith("claude default:"))
        self.assertNotIn("limited", listing)
        reported = json.loads(self.cli("accounts", "--json"))["claude"][0]
        self.assertIsNone(reported["token_unavailable"])
        self.assertIsNone(reported["limited_until"])
        self.cli("review", "claude-pool-token", "--reviewer", "claude")
        output = self.wait("claude-pool-token")
        self.assertEqual(self.security_lookups()[3:], ["codex-worker-claude-default", "codex-worker-claude-first"])
        self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()],
                         [accounts[1]["config_dir"]] * 3)
        again = worker.claude_accounts.read_limits()["default"]
        self.assertTrue(again["token_unavailable"])
        self.assertGreater(again["limited_at"], marked["limited_until"])
        self.assertEqual(again["limited_until"], again["limited_at"] + hour)
        self.assertIn("== claude account default limited until " + worker.config.local_time(again["limited_until"]) +
                      " (token unavailable); review by first", output)
        self.assertFalse(self.review_invocations())

    def test_claude_pool_reviews_on_account_whose_token_returns_after_one_hour(self):
        accounts = self.missing_token_fixture(missing=())
        clock = self.enable_fake_clock()
        worker = self.worker()
        worker.claude_accounts.save_limits("default", self.claude_limits)
        until = worker.claude_accounts.mark_limited("default", token_unavailable=True)
        self.assertEqual(until, worker.claude_accounts.read_limits()["default"]["limited_at"] + 3600)
        clock.sleep(until - clock.time() + 1)
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-pool-token-back", "--review", "--reviewer", "claude")
        output = self.wait("claude-pool-token-back")
        self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()], [accounts[0]["config_dir"]])
        state = self.state("claude-pool-token-back")
        self.assertEqual([entry["account"] for entry in state["reviews"]], ["default"])
        self.assertEqual(state["review_result"]["verdict"], "clean")
        self.assertNotIn("== claude account ", output)
        self.assertEqual(self.security_lookups(), ["codex-worker-claude-default"])
        cleared = worker.claude_accounts.read_limits()["default"]
        for key in ("token_unavailable", "token_retry_at", "limited_until", "limited_at"):
            self.assertNotIn(key, cleared)
        reported = json.loads(self.cli("accounts", "--json"))["claude"][0]
        self.assertIsNone(reported["token_unavailable"])
        self.assertIsNone(reported["limited_until"])

    def test_claude_token_read_keeps_a_later_rate_limit(self):
        self.claude_pool_fixture()
        clock = self.enable_fake_clock()
        worker = self.worker()
        pool = worker.claude_accounts
        cfg = worker.config.accounts_config()
        later = clock.time() + 5 * 3600
        # Token mark first, then a rate limit reported by a concurrent review.
        token_until = pool.mark_limited("first", token_unavailable=True)
        pool.save_limits("first", {"status": "rejected", "resetsAt": later})
        self.assertEqual(pool.mark_limited("first"), later)
        # Rate limit first, then a token failure on the same account.
        pool.save_limits("second", {"status": "rejected", "resetsAt": later})
        self.assertEqual(pool.mark_limited("second"), later)
        self.assertEqual(pool.mark_limited("second", token_unavailable=True), later)
        for name in ("first", "second"):
            with self.subTest(account=name):
                self.assertTrue(pool.token_marked(pool.read_limits()[name]))
                pool.token_available(name)
                info = pool.read_limits()[name]
                self.assertNotIn("token_unavailable", info)
                self.assertEqual(info["limited_until"], later)
                self.assertFalse(pool.token_marked(info))
                self.assertTrue(pool.limited(info))
        clock.sleep(token_until - clock.time() + 1)
        self.assertIsNone(pool.select(cfg, "review"))
        listing = self.cli("accounts")
        self.assertNotIn("token unavailable", listing)
        self.assertTrue(all(entry["token_unavailable"] is None
                            for entry in json.loads(self.cli("accounts", "--json"))["claude"]))
        # An earlier rate limit is part of the token mark and is cleared with it.
        pool.clear_limits("first")
        pool.save_limits("first", {"status": "allowed"})
        pool.mark_limited("first", token_unavailable=True)
        pool.token_available("first")
        self.assertFalse(pool.limited(pool.read_limits()["first"]))
        self.assertEqual(pool.select(cfg, "review")["name"], "first")

    def test_claude_token_mark_bounds_an_unknown_rate_limit_reset_to_one_hour(self):
        self.claude_pool_fixture()
        clock = self.enable_fake_clock()
        pool = self.worker().claude_accounts
        cfg = self.worker().config.accounts_config()
        # Token mark first, then a rejection without any known reset.
        token_until = pool.mark_limited("first", token_unavailable=True)
        pool.save_limits("first", {"status": "rejected"})
        self.assertEqual(pool.mark_limited("first"), token_until)
        # A rejection without a known reset first, then a token failure.
        pool.save_limits("second", {"status": "rejected"})
        self.assertIsNone(pool.mark_limited("second"))
        self.assertEqual(pool.mark_limited("second", token_unavailable=True), token_until)
        for name in ("first", "second"):
            info = pool.read_limits()[name]
            self.assertEqual(info["limited_until"], info["token_retry_at"])
        self.assertIsNone(pool.select(cfg, "review"))
        clock.sleep(token_until - clock.time() + 1)
        self.assertEqual({pool.select(cfg, "review", exclude=(name,))["name"] for name in ("first", "second")},
                         {"first", "second"})

    def test_claude_expired_token_flag_lets_a_fresh_allowance_lift_a_later_rate_limit(self):
        self.claude_pool_fixture()
        clock = self.enable_fake_clock()
        pool = self.worker().claude_accounts
        cfg = self.worker().config.accounts_config()
        token_until = pool.mark_limited("first", token_unavailable=True)
        clock.sleep(token_until - clock.time() + 1)
        later = clock.time() + 5 * 3600
        pool.save_limits("first", {"status": "rejected", "resetsAt": later})
        self.assertEqual(pool.mark_limited("first"), later)
        marked = pool.read_limits()["first"]
        self.assertTrue(marked["token_unavailable"])
        self.assertFalse(pool.token_marked(marked))
        self.assertIsNone(pool.select(cfg, "review", exclude=("second",)))
        pool.save_limits("first", {"status": "allowed", "resetsAt": later}, marked["limited_at"] + 1)
        self.assertNotIn("limited_until", pool.read_limits()["first"])
        self.assertEqual(pool.select(cfg, "review", exclude=("second",))["name"], "first")

    def test_claude_token_mark_from_an_earlier_version_expires_after_one_hour(self):
        self.claude_pool_fixture()
        clock = self.enable_fake_clock()
        worker = self.worker()
        now = clock.time()
        worker.config.TASKS.mkdir(parents=True, exist_ok=True)
        (worker.config.TASKS / "claude-limits.json").write_text(json.dumps({
            "first": {"limited_until": None, "limited_at": now - 60, "token_unavailable": True},
            "second": {"limited_until": None, "limited_at": now - 3601, "token_unavailable": True}}))
        limits = worker.claude_accounts.read_limits()
        self.assertTrue(worker.claude_accounts.limited(limits["first"]))
        self.assertEqual(limits["first"]["limited_until"], now - 60 + 3600)
        self.assertFalse(worker.claude_accounts.limited(limits["second"]))
        cfg = worker.config.accounts_config()
        self.assertEqual(worker.claude_accounts.select(cfg, "review")["name"], "second")

    def test_claude_pool_falls_back_only_after_every_account_is_limited(self):
        self.claude_pool_fixture(runs=[{"limits": {"status": "rejected", "resetsAt": self.reset}}])
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("claude-pool-empty", "--review", "--reviewer", "claude")
        output = self.wait("claude-pool-empty")
        self.assertEqual(len(self.claude_invocations()), 2)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual([entry["account"] for entry in self.state("claude-pool-empty")["reviews"]],
                         ["first", "second", "main"])
        self.assertEqual(len([line for line in output.splitlines() if line.startswith("== claude account ")]), 2)
        self.assertRegex(output, r"== claude account second limited until [^\n]+; review by codex")
        accounts = self.cli("accounts")
        for name in ("first", "second"):
            line = next(line for line in accounts.splitlines() if line.startswith("claude " + name + ":"))
            self.assertIn("limited", line)

    def test_claude_pool_spreads_angles_and_lists_per_account_limits(self):
        accounts = self.claude_pool_fixture()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-pool-angles", "--reviewer", "claude", "--review-angles", "correctness,concurrency")
        output = self.wait("claude-pool-angles")
        self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()],
                         [account["config_dir"] for account in accounts])
        state = self.state("claude-pool-angles")
        self.assertEqual([entry["account"] for entry in state["review_result"]["angles"]], ["first", "second"])
        self.assertIn("== review angle correctness by claude/first", output)
        self.assertIn("== review angle concurrency by claude/second", output)
        listing = self.cli("accounts")
        for name in ("first", "second"):
            line = next(line for line in listing.splitlines() if line.startswith("claude " + name + ":"))
            self.assertIn("25%", line)
            self.assertIn("50%", line)
            for window in self.claude_limits["unifiedWindows"].values():
                self.assertIn(datetime.datetime.fromtimestamp(window["resetsAt"]).strftime("%Y-%m-%d %H:%M"), line)

    def test_report_splits_reviewer_cost_and_treats_historical_reviews_as_codex(self):
        self.claude_fixture()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-report", "--review", "--reviewer", "claude")
        self.wait("claude-report")
        self.assert_claude_review_telemetry(1)
        rows = self.telemetry_journal()
        claude = next(row for row in rows if row["phase"] == "review")
        legacy = dict(claude, slug="legacy-codex", thread="historical-codex-thread")
        legacy.pop("reviewer")
        legacy.pop("cost_usd")
        rows.append(legacy)
        (self.home / "codex-worker-log.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows))
        report = json.loads(self.cli("report", "--json"))
        task = next(task for task in report["tasks"] if task["slug"] == "claude-report")
        self.assertEqual(task["reviews"][0]["account"], "default")
        self.assertEqual(claude["account"], "default")
        self.assertEqual(report["summary"]["reviewers"], {
            "codex": {"reviews": 1, "cost_usd": 0}, "claude": {"reviews": 1, "cost_usd": 0.125}})
        text = self.cli("report")
        self.assertIn("Reviews by reviewer: codex 1 ($0.00); claude 1 ($0.12)", text)


    def write_duty(self, family, account, checked_at=None, claude=None, codex=None):
        now = time.time()
        record = {"account": account, "family": family, "since": now,
                  "checked_at": now if checked_at is None else checked_at, "reason": "fixture duty",
                  "rooms": {"claude": claude or {}, "codex": codex or {}}}
        (self.home / "duty.json").write_text(json.dumps(record))
        return record

    def set_codex_used(self, home, used):
        path = home / "fixture.json"
        value = json.loads(path.read_text())
        value["primary"]["usedPercent"] = used
        path.write_text(json.dumps(value))

    def duty_snapshot(self, weekly, updated=None, short=.1, short_reset=None):
        return {"updated_at": time.time() if updated is None else updated, "unifiedWindows": {
            "five_hour": {"utilization": short, "resetsAt": short_reset or self.reset},
            "seven_day": {"utilization": weekly, "resetsAt": self.reset + 86400}}}

    def test_duty_elect_families_margins_ties_weights_and_unavailable_holders(self):
        self.claude_pool_fixture()
        worker = self.worker()
        duty, cfg, now = worker.duty, worker.config.accounts_config(), time.time()
        settings = cfg["duty"]
        self.assertEqual(settings, {"relative_margin": 25, "absolute_margin": 100,
                                    "claude_only_task_types": ["visual", "layout"]})

        def candidate(name, room, family="claude", unavailable=None):
            return {"family": family, "name": name, "room": room, "unavailable": unavailable}

        def elected(candidates, holder=None, family="claude"):
            current = {"account": holder, "family": family} if holder else None
            return duty.duty_label(duty.elect(candidates, current, settings))

        codex = candidate("main", 5000, "codex")
        self.assertEqual(elected([codex, candidate("first", 10)]), "claude/first")
        self.assertEqual(elected([codex, candidate("first", 0, unavailable="limited")]), "codex/main")
        self.assertEqual(elected([codex, candidate("first", 1)], "main", "codex"), "claude/first")
        for holder_room, best_room, switched in ((400, 500, True), (400, 499, False),
                                                 (10, 14, False), (1000, 1200, False), (1000, 1300, True)):
            with self.subTest(holder=holder_room, best=best_room):
                self.assertEqual(elected([candidate("first", holder_room), candidate("second", best_room)], "first"),
                                 "claude/second" if switched else "claude/first")
        self.assertEqual(duty.elect([candidate("first", 0, unavailable="limited")],
                                    {"account": "first", "family": "claude"}, settings),
                         {"account": None, "family": None, "reason": "no account available"})
        self.assertEqual(elected([candidate("second", 50), candidate("first", 50)]), "claude/first")
        self.assertEqual(elected([candidate("first", 50), candidate("second", 50)], "second"), "claude/second")

        first, second = cfg["claude"]["accounts"]
        fresh = duty.claude_duty_candidate(cfg, second, self.duty_snapshot(.5), now, now)
        self.assertEqual(fresh, {"family": "claude", "name": "second", "room": 35, "unavailable": None})
        partial = self.duty_snapshot(.5)
        del partial["unifiedWindows"]["five_hour"]
        self.assertEqual(duty.claude_duty_candidate(cfg, second, partial, now, now), fresh)
        for label, snapshot in (
                ("limit", dict(self.duty_snapshot(.1), limited_until=now + 600)),
                ("token", dict(self.duty_snapshot(.1), token_unavailable=True, token_retry_at=now + 600,
                               limited_until=now + 600)),
                ("five-hour", self.duty_snapshot(.1, short=.99)),
                ("stale", self.duty_snapshot(.1, updated=now - 3600)),
                ("unknown", {"updated_at": now})):
            with self.subTest(unavailable=label):
                holder = duty.claude_duty_candidate(cfg, first, snapshot, now, now)
                self.assertIsNotNone(holder["unavailable"])
                self.assertEqual(elected([holder, fresh], "first"), "claude/second")
        # A snapshot saved by this tick's refresh is fresh even when every snapshot ages at once.
        cfg["claude"]["limits_max_age_minutes"] = 0
        self.assertIsNone(duty.claude_duty_candidate(cfg, first, self.duty_snapshot(.1), now, now - 1)["unavailable"])
        self.assertEqual(duty.claude_duty_candidate(cfg, first, self.duty_snapshot(.1, updated=now - 2), now,
                                                    now - 1)["unavailable"], "limits stale")
        cfg["claude"]["limits_max_age_minutes"] = 30
        first["weight"], second["weight"] = 20, 5
        large = duty.claude_duty_candidate(cfg, first, self.duty_snapshot(.5), now, now)
        small = duty.claude_duty_candidate(cfg, second, self.duty_snapshot(.1), now, now)
        self.assertEqual((large["room"], small["room"]), (700, 375))
        self.assertEqual(elected([small, large]), "claude/first")

        cfg.update(switch_at=99, soft_switch_at=80)
        info = {"name": "main", "logged_in": True, "weight": 2, "limits": {
            "primary": {"used": 30, "minutes": 300, "reset": None},
            "secondary": {"used": 50, "minutes": 10080, "reset": None}}}
        self.assertEqual(duty.codex_duty_candidate(cfg, info),
                         {"family": "codex", "name": "main", "room": 60, "unavailable": None})
        for label, changes in (("five-hour", {"limits": dict(info["limits"], primary={"used": 99, "minutes": 300})}),
                               ("login", {"logged_in": False}), ("unknown", {"logged_in": None}),
                               ("limited", {"ordinary": False})):
            with self.subTest(codex=label):
                self.assertIsNotNone(duty.codex_duty_candidate(cfg, dict(info, **changes))["unavailable"])

    def test_duty_tick_records_changes_once_and_skips_failed_refreshes(self):
        self.claude_pool_fixture(runs=[{"mode": "invalid_json", "limits": {}}], names=("first", "second", "unknown"))
        limits_path = self.home / "claude-tasks" / "claude-limits.json"
        limits_path.write_text(json.dumps({"first": self.duty_snapshot(.1),
                                           "second": self.duty_snapshot(0, updated=time.time() - 3600)}))
        duty_path, log = self.home / "duty.json", self.home / "duty-log.jsonl"
        duty_path.write_text("{broken")
        output = self.cli("duty", "tick")
        record = json.loads(duty_path.read_text())
        self.assertEqual(duty_path.read_text(), json.dumps(record, indent=2) + "\n")
        self.assertEqual((record["family"], record["account"]), ("claude", "first"), output)
        self.assertEqual(record["rooms"], {"claude": {"first": 75}, "codex": {"main": 89, "second": 89}})
        self.assertIn("duty changed to claude/first since", output)
        # The stale and the unknown account were probed, failed, and stayed out of the election.
        self.assertEqual(len(self.claude_invocations()), 2)
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([(entry["from"], entry["to"]) for entry in entries], [(None, "claude/first")])
        self.assertEqual(entries[0]["rooms"], record["rooms"])
        self.assertEqual(entries[0]["at"], record["since"])

        limits = json.loads(limits_path.read_text())
        limits["second"] = self.duty_snapshot(0)
        limits_path.write_text(json.dumps(limits))
        self.assertIn("kept: second room 85 vs 75 misses the relative and absolute margins", self.cli("duty", "tick"))
        kept = json.loads(duty_path.read_text())
        self.assertEqual((kept["account"], kept["since"]), ("first", record["since"]))
        self.assertGreaterEqual(kept["checked_at"], record["checked_at"])
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertEqual(len(self.claude_invocations()), 2)

        settings_path = self.home / "claude-accounts.json"
        settings = json.loads(settings_path.read_text())
        settings["claude"]["accounts"][1]["weight"] = 20
        settings_path.write_text(json.dumps(settings))
        self.cli("duty", "tick")
        switched = json.loads(duty_path.read_text())
        self.assertEqual(switched["account"], "second")
        self.assertEqual(switched["reason"], "second room 1700 vs 75 passes both margins")
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([(entry["from"], entry["to"]) for entry in entries],
                         [(None, "claude/first"), ("claude/first", "claude/second")])
        # A tick that fails between the record and the log line never logs one change twice.
        worker = self.worker()
        cfg = worker.config.accounts_config()
        limits = json.loads(limits_path.read_text())
        limits["second"]["limited_until"] = self.reset
        limits_path.write_text(json.dumps(limits))
        with mock.patch.object(worker.duty, "duty_log_path", side_effect=OSError("interrupted")), \
                self.assertRaises(OSError):
            worker.duty.run_duty_tick(cfg, [], time.time())
        self.assertEqual(json.loads(duty_path.read_text())["account"], "first")
        self.cli("duty", "tick")
        self.assertEqual(json.loads(duty_path.read_text())["account"], "first")
        self.assertEqual(len(log.read_text().splitlines()), 2)
        # A second tick while one holds the lock changes nothing.
        before = duty_path.read_text()
        with (self.home / "duty.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.cli("duty", "tick"), "duty tick: another duty tick is running; nothing changed\n")
        self.assertEqual(duty_path.read_text(), before)
        # A fresh snapshot is not probed, but a missing token is marked as probes do, so the
        # only working Codex account on duty still takes new work.
        fixture = json.loads(self.claude_fixture_path.read_text())
        fixture["security"] = {"codex-worker-claude-" + name: {"unavailable": True}
                               for name in ("first", "second", "unknown")}
        self.claude_fixture_path.write_text(json.dumps(fixture))
        self.set_codex_used(self.other, 99)
        self.cli("duty", "tick")
        self.assertEqual(json.loads(duty_path.read_text())["account"], "main")
        self.assertEqual(len(log.read_text().splitlines()), 3)
        self.assertTrue(all(worker.claude_accounts.token_marked(info)
                            for info in worker.claude_accounts.read_limits().values()))
        infos = [{"name": name, "logged_in": True, "account_id": name, "limits": {
            "primary": {"used": used, "minutes": 10080, "reset": self.reset},
            "secondary": {"used": 2, "minutes": 300, "reset": self.reset}}}
            for name, used in (("main", 10), ("second", 99))]
        self.assertEqual(worker.accounts.available(infos, cfg, worker.duty.codex_reserved(infos, cfg))["name"],
                         "main")

    def test_duty_selection_skips_duty_for_new_claude_work_and_reviews_with_exceptions(self):
        self.claude_pool_fixture()
        worker = self.worker()
        cfg = worker.config.accounts_config()
        limits = {"first": self.duty_snapshot(.1), "second": self.duty_snapshot(.5)}
        limits_path = worker.config.TASKS / "claude-limits.json"
        limits_path.write_text(json.dumps(limits))

        def choices(task_type=None, own=None):
            review = worker.claude_review.select_account(cfg, "review", task_type=task_type)
            work = worker.account_rotation.claude_choice({"claude_account": own, "task_type": task_type},
                                                         cfg, lambda: False)
            return review and review["name"], work and work["name"]

        with mock.patch.dict(os.environ, self.env, clear=True):
            baseline = choices()
            self.assertEqual(baseline, ("first", "first"))
            self.write_duty("claude", "first", codex={"main": 89})
            self.assertEqual(choices(), ("second", "second"))
            self.assertEqual(choices(own="first"), ("second", "first"))
            limits["second"]["limited_until"] = self.reset
            limits_path.write_text(json.dumps(limits))
            # Reviews fall back to Codex and work waits, as with an exhausted pool.
            self.assertEqual(choices(), (None, None))
            self.assertEqual(choices(task_type="visual"), ("first", "first"))
            for home in (self.home, self.other):
                self.set_codex_used(home, 99)
            self.assertEqual(choices(), ("first", "first"))
            self.set_codex_used(self.home, 10)
            self.assertEqual(choices(), (None, None))
            cfg["soft_switch_at"] = 80
            self.set_codex_used(self.home, 85)
            self.assertEqual(choices(), (None, None))
            cfg["soft_switch_at"] = 99
            del limits["second"]["limited_until"]
            limits_path.write_text(json.dumps(limits))
            self.write_duty("claude", "second", codex={"main": 89})
            self.assertEqual(choices(), ("first", "first"))
            (self.home / "duty.json").write_text("{broken")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(choices(), baseline)
                self.assertEqual(choices(), baseline)
            self.assertEqual(stderr.getvalue().count("duty.json is unreadable"), 1, stderr.getvalue())

        def info(name, used):
            return {"name": name, "logged_in": True, "account_id": name, "limits": {
                "primary": {"used": used, "minutes": 10080, "reset": self.reset},
                "secondary": {"used": 2, "minutes": 300, "reset": self.reset}}}

        def codex_choice(infos):
            chosen = worker.accounts.available(infos, cfg, worker.duty.codex_reserved(infos, cfg))
            return chosen and chosen["name"]

        infos = [info("main", 10), info("second", 10)]
        self.write_duty("codex", "main")
        self.assertEqual(codex_choice(infos), "second")
        # Claude is available now, so the duty account still skips new work.
        self.assertIsNone(codex_choice([info("main", 10), info("second", 99)]))
        limits_path.write_text(json.dumps({name: self.duty_snapshot(.1, updated=time.time() - 3600)
                                           for name in ("first", "second")}))
        self.assertEqual(codex_choice([info("main", 10), info("second", 99)]), "main")

        # A Codex sleep is planned without the reset of a duty account the task may not take.
        limits_path.write_text(json.dumps(limits))
        now = time.time()
        sleeping = [dict(info(name, 99), limits={
            "primary": {"used": 99, "minutes": 10080, "reset": now + delay},
            "secondary": {"used": 2, "minutes": 300, "reset": now + delay}}) for name, delay in (("main", 600),
                                                                                            ("second", 3600))]
        st = {"slug": "duty-sleep", "account": "second", "thread": "", "rotations": []}
        (worker.config.TASKS / "duty-sleep").mkdir()
        slept = []
        self.assertIsNone(worker.account_rotation.wait_for_account(
            st, sleeping, cfg, "all accounts unavailable", lambda: bool(slept), slept.append))
        self.assertEqual(st["sleeping"]["account"], "second")
        self.assertAlmostEqual(st["sleeping"]["until"], now + 3600 + worker.config.RESET_MARGIN, delta=1)

        # An automatic reset credit is never spent on a duty account the task may not take.
        later = now + 30 * 3600
        credited = [dict(info(name, 99), limits={
            "primary": {"used": 99, "minutes": 10080, "reset": later},
            "secondary": {"used": 2, "minutes": 300, "reset": later}}, reset_credits={
            "availableCount": 1, "credits": [{"id": name + "-credit", "status": "available",
                                              "resetType": "codexRateLimits", "expiresAt": now + expires}]})
            for name, expires in (("main", 3600), ("second", 7200))]
        with mock.patch.object(worker.credit_resets, "reset_credit_action",
                               return_value={"outcome": "failed", "credit": {"id": "spent"}}) as action:
            self.assertIsNone(worker.account_rotation.try_reset_credit(
                st, credited, dict(cfg, use_reset_credits=True), lambda: False))
        self.assertEqual(action.call_args.args[0]["name"], "second")

        # A Claude sleep waits for the duty account when an exception will let the task take it.
        self.write_duty("claude", "first")
        limits_path.write_text(json.dumps({"first": dict(self.duty_snapshot(.1), limited_until=now + 600),
                                           "second": dict(self.duty_snapshot(.5), limited_until=now + 3600)}))
        for task_type, codex_used in ((None, 99), ("visual", 10)):
            with self.subTest(task_type=task_type), mock.patch.dict(os.environ, self.env, clear=True):
                self.set_codex_used(self.home, codex_used)
                st, slept = {"slug": "duty-sleep", "task_type": task_type, "rotations": []}, []
                self.assertIsNone(worker.account_rotation.wait_for_claude_account(
                    st, cfg, "all Claude accounts unavailable", lambda: bool(slept), slept.append))
                self.assertEqual(st["sleeping"]["account"], "first")
                self.assertAlmostEqual(st["sleeping"]["until"], now + 600 + worker.config.RESET_MARGIN, delta=1)

    def test_duty_codex_tasks_reviews_and_rotation_skip_the_duty_account(self):
        third = self.root / "third-home"
        third.mkdir()
        self.configure(language="en", accounts=[{"name": "main", "home": str(self.home)},
                                                {"name": "second", "home": str(self.other)},
                                                {"name": "third", "home": str(third)}])
        work = {"mode": "complete", "reply": self.work_result()}
        self.account(self.home, "A", self.reset, runs=[work, work, {"mode": "limit"}])
        self.account(self.other, "B", self.reset + 3600, runs=[
            {"mode": "complete", "reply": self.review_result(False)}, work])
        self.account(third, "C", self.reset + 7200, runs=[work])
        rooms = {"main": 89, "second": 89, "third": 89}
        self.start("duty-own")
        self.wait("duty-own")
        self.assertEqual(self.state("duty-own")["account"], "main")
        self.write_duty("codex", "main", codex=rooms)
        thread = self.state("duty-own")["thread"]
        self.cli("say", "duty-own", "Continue on the same account.")
        self.wait("duty-own")
        state = self.state("duty-own")
        self.assertEqual((state["account"], state["thread"]), ("main", thread))
        self.assertTrue(self.invocations()[-1]["resume"])
        self.cli("review", "duty-own")
        self.wait("duty-own")
        self.assertEqual(self.state("duty-own")["reviews"][-1]["account"], "second")
        self.start("duty-new")
        self.wait("duty-new")
        self.assertEqual(self.state("duty-new")["account"], "second")
        # The former duty account takes new work again; a limit rotates past the new duty account.
        self.write_duty("codex", "second", codex=rooms)
        self.start("duty-rotate")
        self.wait("duty-rotate")
        state = self.state("duty-rotate")
        self.assertEqual(state["account"], "third")
        self.assertEqual([(entry["from"], entry["to"]) for entry in state["rotations"] if "from" in entry],
                         [("main", "third")])
        self.assertEqual([entry["home"] for entry in self.invocations()],
                         [str(self.home), str(self.home), str(self.other), str(self.other),
                          str(self.home), str(third)])

    def test_duty_claude_work_rotation_sleeps_past_the_duty_and_own_task_stays(self):
        self.fixture_timeout = 90
        rejected = {"status": "rejected", "resetsAt": self.reset,
                    "unifiedWindows": {"seven_day": {"utilization": 1, "resetsAt": self.reset}}}
        accounts = self.claude_pool_fixture(runs=[{"limits": rejected, "reply": self.work_result()},
                                                  {"reply": self.work_result()}, {"reply": self.work_result()}])
        now = time.time()
        (self.home / "claude-tasks" / "claude-limits.json").write_text(json.dumps({
            "first": self.duty_snapshot(.1, short_reset=now + 900),
            "second": self.duty_snapshot(.3, short_reset=now + 1800)}))
        self.write_duty("claude", "first", claude={"first": 75, "second": 55}, codex={"main": 89})
        self.start("duty-claude-new", "--executor", "claude")
        self.cli("wait", "duty-claude-new", "--timeout", "60", timeout=75)
        state = self.state("duty-claude-new")
        self.assertEqual((state["outcome"], state["claude_account"]), ("sleeping", "second"))
        # The task sleeps until the other account returns, not until the duty account's next reset.
        self.assertEqual(state["sleeping"]["account"], "second")
        self.assertAlmostEqual(state["sleeping"]["until"], self.reset + 0.1, delta=1)
        self.assertEqual([entry["config_dir"] for entry in self.claude_invocations()], [accounts[1]["config_dir"]])
        self.cli("stop", "duty-claude-new")
        self.wait("duty-claude-new")

        self.cli("duty", "clear")
        self.start("duty-claude-own", "--executor", "claude", "--parallel")
        self.wait("duty-claude-own")
        session = self.state("duty-claude-own")["session_id"]
        self.assertEqual(self.state("duty-claude-own")["claude_account"], "first")
        self.write_duty("claude", "first", claude={"first": 75}, codex={"main": 89})
        self.cli("say", "duty-claude-own", "Continue on the duty account.")
        self.wait("duty-claude-own")
        self.assertEqual(self.state("duty-claude-own")["claude_account"], "first")
        resumed = self.claude_invocations()[-1]
        self.assertEqual(resumed["config_dir"], accounts[0]["config_dir"])
        self.assertEqual(resumed["argv"][resumed["argv"].index("--resume") + 1], session)

    def test_duty_status_clear_accounts_and_launchd_plist(self):
        self.claude_pool_fixture()
        self.assertEqual(self.cli("duty", "status"), "no duty account: duty.json is absent\n")
        self.assertIsNone(json.loads(self.cli("accounts", "--json"))["duty"])
        record = self.write_duty("claude", "first", checked_at=time.time() - 100 * 60,
                                 claude={"first": 75}, codex={"main": 89})
        status = self.cli("duty", "status")
        self.assertIn("duty claude/first since ", status)
        self.assertIn("rooms: claude first 75; codex main 89\n", status)
        self.assertRegex(status, r"warning: no duty check for 10\d minutes; re-election is not running")
        report = json.loads(self.cli("duty", "status", "--json"))
        self.assertEqual((report["duty"], report["stale"], report["unreadable"]), (record, True, False))
        self.assertEqual(json.loads(self.cli("accounts", "--json"))["duty"], record)
        self.assertRegex(self.cli("accounts"), r"\nclaude first: [^\n]*; on duty\n")
        self.write_duty("codex", "second", codex={"main": 89, "second": 89})
        self.assertNotIn("warning", self.cli("duty", "status"))
        self.assertFalse(json.loads(self.cli("duty", "status", "--json"))["stale"])
        self.assertRegex(self.cli("accounts"), r"\nsecond\t[^\n]*\tavailable \(on duty\)\n")
        log = self.home / "duty-log.jsonl"
        log.write_text("{}\n")
        self.assertIn("duty cleared", self.cli("duty", "clear"))
        self.assertFalse((self.home / "duty.json").exists())
        self.assertEqual(log.read_text(), "{}\n")
        self.assertEqual(self.cli("duty", "status"), "no duty account: duty.json is absent\n")

        launchctl = self.root / "launchctl"
        launchctl.write_text("#!/bin/sh\ntouch " + shlex.quote(str(self.root / "launchctl-called")) + "\n")
        launchctl.chmod(0o700)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "duty", "plist", "--minutes", "15"],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = plistlib.loads(result.stdout.encode())
        home, log = Path(self.env["HOME"]), str(self.home / "duty-launchd.log")
        self.assertEqual((job["StartInterval"], job["RunAtLoad"]), (900, True))
        self.assertEqual((job["StandardOutPath"], job["StandardErrorPath"]), (log, log))
        environment = job["EnvironmentVariables"]
        self.assertEqual(environment["CODEX_TASK_HOME"], str(self.home))
        for directory in ("/opt/homebrew/bin", str(home / ".local/bin"), "/usr/bin"):
            self.assertIn(directory, environment["PATH"].split(os.pathsep))
        installed = Path(PYTHON).parent / "codex-worker"
        expected = ([str(installed), "duty", "tick"] if installed.is_file()
                    else [PYTHON, "-I", str(SCRIPT), "duty", "tick"])
        worker = load_worker(self.env, name="duty_plist_worker")
        interpreter = self.root / "venv" / "bin" / "python"
        created = subprocess.run([PYTHON, "-m", "venv", "--without-pip", str(interpreter.parent.parent)],
                                 cwd=self.root, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertFalse(interpreter.with_name("codex-worker").exists())
        with mock.patch.object(worker.commands.sys, "executable", str(interpreter)):
            fallback = worker.commands.duty_launchd_plist(15)
            started = time.time()
            launched = subprocess.run(fallback["ProgramArguments"], cwd=self.root, env=self.env,
                                      text=True, capture_output=True, timeout=30)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            elected = json.loads((self.home / "duty.json").read_text())
            self.assertGreaterEqual(elected["checked_at"], started)
            status = json.loads(self.cli("duty", "status", "--json"))
            self.assertEqual(status["duty"], elected)
            self.assertFalse(status["stale"])
            self.assertEqual(fallback["ProgramArguments"],
                             [str(interpreter), "-I", str(SCRIPT), "duty", "tick"])
            installed = interpreter.with_name("codex-worker")
            installed.write_text("#!/bin/sh\n")
            stable = worker.commands.duty_launchd_plist(15)
            self.assertEqual(stable["ProgramArguments"], [str(installed), "duty", "tick"])
        self.assertEqual(job["ProgramArguments"], expected)
        self.assertFalse((self.root / "launchctl-called").exists())
        self.assertFalse((home / "Library").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
