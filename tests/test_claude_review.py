"""Claude review subprocesses use a separate credential and leave work to Codex."""
import datetime
import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from rotation_support import PYTHON, SCRIPT, RotationTestCase


TEST_TOKEN = "fixture-claude-reviewer-oauth-secret"
FAKE_SECURITY = r'''import json
import os
import sys
from pathlib import Path

with Path(os.environ["CODEX_FAKE_SECURITY_TRACE"]).open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:] != ["find-generic-password", "-s", "codex-worker-claude-reviewer", "-a", "fixture-user", "-w"]:
    sys.exit("unexpected fake keychain lookup")
print("fixture-claude-reviewer-oauth-secret")
'''

FAKE_CLAUDE = r'''import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

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

trace({"command": "review", "argv": sys.argv[1:], "cwd": os.getcwd(),
       "token_matches": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "fixture-claude-reviewer-oauth-secret",
       "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
       "inherited_provider_keys": sorted(key for key in os.environ
           if (key.startswith("CLAUDE") or key.startswith("ANTHROPIC"))
           and key not in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")),
       "stdin": sys.stdin.read()})
stopped = [False]

def interrupted(signum, frame):
    trace({"command": "signal", "signal": signum})
    stopped[0] = True

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, interrupted)
emit({"type": "system", "subtype": "init", "session_id": str(uuid.uuid4()), "model": "fake-claude"})
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
    trace({"command": "holding"})
    while not stopped[0]:
        time.sleep(0.02)
    sys.exit(130)
if mode == "modify":
    Path(run.get("path", "reviewer-change.txt")).write_text("Reviewer must leave this visible.\n")
if mode == "move_head":
    import subprocess
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "Reviewer moved HEAD"], check=True)
if mode == "invalid_json":
    print('{"type":"result",broken', flush=True)
    sys.exit(0)
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
      "input": {"command": "git diff --stat"}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "fixture diff"}]}})
result = {"type": "result", "subtype": "success", "is_error": mode == "is_error",
          "structured_output": run.get("reply"), "num_turns": 3,
          "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30, "output_tokens": 40},
          "total_cost_usd": run.get("cost_usd", 0.125)}
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "StructuredOutput",
      "input": run.get("reply")}]}})
if mode == "rejected_buffered":
    os.write(1, (json.dumps({"type": "rate_limit_event", "rate_limit_info": limits}) + "\n"
                + json.dumps(result) + "\n").encode())
else:
    emit(result)
'''


class ClaudeReviewTests(RotationTestCase):
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
                        CLAUDE_CODE_ENTRYPOINT="desktop", CLAUDE_OTHER="host-setting")
        self.claude_limits = {"status": "allowed_warning", "rateLimitType": "seven_day",
                              "utilization": 0.5, "resetsAt": self.reset,
                              "unifiedWindows": {
                                  "five_hour": {"utilization": 0.25, "resetsAt": self.reset - 3600},
                                  "seven_day": {"utilization": 0.5, "resetsAt": self.reset}}}
        self.claude_fixture_path.write_text(json.dumps({
            "runs": runs or [{"reply": self.review_result(False)}], "limits": self.claude_limits}))
        options = {"config_dir": str(self.claude_config_dir)}
        options.update(settings.pop("claude", {}))
        self.configure(language="en", claude=options, **settings)

    def claude_trace(self):
        if not self.claude_trace_path.exists():
            return []
        return [json.loads(line) for line in self.claude_trace_path.read_text().splitlines()]

    def claude_invocations(self):
        return [entry for entry in self.claude_trace() if entry["command"] == "review"]

    def assert_claude_review_telemetry(self, count):
        self.until(lambda: len([row for row in self.phase_telemetry_journal()
                                if row["phase"] == "review"]) == count)
        reviews = [row for row in self.phase_telemetry_journal() if row["phase"] == "review"]
        self.assertEqual([row["reviewer"] for row in reviews], ["claude"] * count)
        for row in reviews:
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
        start_output = self.start("claude-fix", "--review", "--reviewer", "claude", "--fix-rounds", "1")
        output = self.wait("claude-fix")
        state = self.state("claude-fix")
        self.assertEqual(state["reviewer"], "claude")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(state["rounds"][0]["decisions"], decisions)
        codex = self.invocations()
        self.assertEqual([entry["review"] for entry in codex], [False, False])
        self.assertTrue(codex[1]["resume"])
        self.assertIn(self.review_result()["findings"][0]["detail"], codex[1]["prompt"])
        invocations = self.claude_invocations()
        self.assertEqual(len(invocations), 2)
        self.assertRegex(output, r"== review by claude \(\d+(?:\.\d+)? min\)")
        self.assertNotRegex(output, r"review by claude[^\n]*%")
        for entry in invocations:
            self.assertTrue(entry["token_matches"])
            self.assertEqual(entry["config_dir"], str(self.claude_config_dir))
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
        status = self.cli("status", "claude-fix")
        self.assertIn(self.review_result(False)["summary"], status)
        self.assertEqual(json.loads((self.home / "claude-tasks" / "claude-fix" / "review.md").read_text()),
                         state["reviews"][-1]["result"])
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
            self.assertRegex(output, r"== review angle " + angle + r" by claude \(\d+(?:\.\d+)? min\)")
        self.assert_claude_review_telemetry(4)

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
                self.assertIn("== reviewer claude unavailable: usage limit; review by codex", output)
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
                self.assertNotIn("review by codex", output)
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
        self.assertIn("== review by claude", output)
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
        self.assertIn("== review by claude", output)
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
                self.assertNotIn("review by codex", output)

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
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.claude_fixture(runs=[{"mode": "hold"}], max_review_minutes=2)
        work = self.work_result()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": work}])
        self.start("claude-timeout", "--review", "--reviewer", "claude")
        output = self.wait("claude-timeout", timeout=10)
        state = self.state("claude-timeout")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], work)
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertFalse(self.process_is_live(self.claude_invocations()[0]["pid"]))
        self.assertFalse(self.review_invocations())
        self.assertNotIn("review by codex", output)
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)

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
        self.assertIn("== review by claude", output)
        self.cli("say", "claude-say", "Continue using Codex review", "--review", "--reviewer", "codex")
        self.wait("claude-say")
        self.assertEqual(self.state("claude-say")["reviewer"], "codex")
        self.assertEqual(len(self.claude_invocations()), 1)
        self.assertEqual(len(self.review_invocations()), 1)

    def test_claude_limits_accounts_and_default_cli_settings(self):
        self.claude_fixture(reviewer="claude")
        self.assertIn("claude reviewer: unknown", self.cli("accounts"))
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("claude-limits", "--review")
        self.wait("claude-limits")
        work = self.state("claude-limits")["result"]
        self.cli("review", "claude-limits", "--reviewer", "claude")
        self.wait("claude-limits")
        self.assertEqual(self.state("claude-limits")["result"], work)
        self.assertEqual(len(self.claude_invocations()), 2)
        saved = json.loads((self.home / "claude-tasks" / "claude-limits.json").read_text())
        self.assertEqual(saved["unifiedWindows"], self.claude_limits["unifiedWindows"])
        output = self.cli("accounts")
        line = next(line for line in output.splitlines() if line.startswith("claude reviewer"))
        self.assertIn("25%", line)
        self.assertIn("50%", line)
        for window in self.claude_limits["unifiedWindows"].values():
            reset = datetime.datetime.fromtimestamp(window["resetsAt"]).strftime("%Y-%m-%d %H:%M")
            self.assertIn(reset, line)
        argv = self.claude_invocations()[0]["argv"]
        self.assertEqual(argv[argv.index("--max-turns") + 1], "40")
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

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
        self.assertEqual(report["summary"]["reviewers"], {
            "codex": {"reviews": 1, "cost_usd": 0}, "claude": {"reviews": 1, "cost_usd": 0.125}})
        text = self.cli("report")
        self.assertIn("Reviews by reviewer: codex 1 ($0.00); claude 1 ($0.12)", text)


if __name__ == "__main__":
    import unittest
    unittest.main()
