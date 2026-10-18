"""Typed results, scout and configuration tests."""
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rotation_support import PYTHON, SCRIPT, SKILL, RotationTestCase


class ResultScoutConfigTests(RotationTestCase):
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
        worker = self.worker()
        self.assertEqual(prompt, worker.scout_prompt(question, worker.accounts_config()))
        self.assertIn(reply, output)
        self.assert_scout_output(output)
        telemetry = self.phase_telemetry_journal()
        self.assertEqual([(row["kind"], row["phase"], row["outcome"]) for row in telemetry],
                         [("scout", "scout", "completed")])
        self.assertIsNone(telemetry[0]["result_status"])
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
        worker = self.worker()
        self.assertEqual(prompt, worker.scout_prompt(question.read_text(), worker.accounts_config()))
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
                     ("say", "scout-no-writes", "Review angles", "--review-angles", "auto"),
                     ("say", "scout-no-writes", "Enable CI", "--ci"),
                     ("say", "scout-no-writes", "Disable CI", "--no-ci"),
                     ("say", "scout-no-writes", "Set CI repairs", "--ci-rounds", "0"),
                     ("review", "scout-no-writes", "--review-angles", "correctness,concurrency"),
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
        reset = self.reset_after_ready(lambda: self.cli("say", "scout-sleep", followup), reset, 3)
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
                prompt = worker.handoff(st)
                if language == "ru":
                    self.assertRegex(prompt, "[А-Яа-яЁё]")
                else:
                    self.assertNotRegex(prompt, "[А-Яа-яЁё]")
                self.assertIn("git diff", prompt)
                self.assertEqual(worker.review_prompt(st), worker.review_prompt(st, cfg))
                self.assertEqual(worker.reset_resume_prompt(), worker.reset_resume_prompt(cfg))

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
        self.assertNotRegex(self.invocations()[0]["prompt"], "[А-Яа-яЁё]")
        worker = self.worker()
        self.assertEqual(self.invocations()[1]["prompt"], worker.review_prompt(
            self.state("single-account"), worker.accounts_config()))
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
