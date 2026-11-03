"""Claude review subprocesses use a separate credential and leave work to Codex."""
import datetime
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from rotation_support import PYTHON, SCRIPT, RotationTestCase
from worker_loader import WORKER_BOOTSTRAP


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
          "result": "OK",
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
        self.assertEqual(worker.claude_accounts.read_limits()["first"], entries["first"])

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


if __name__ == "__main__":
    import unittest
    unittest.main()
