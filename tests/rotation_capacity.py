"""Retry transient model-capacity failures through the fake Codex CLI."""
import json
import os
import signal
from pathlib import Path

from rotation_support import RotationTestCase


class CapacityRetryTests(RotationTestCase):
    def test_capacity_detection_preserves_limit_and_authentication_error_priority(self):
        worker = self.worker()
        for event_type in ("turn.failed", "error"):
            for message, expected_reason, expected_auth in (
                    ("Selected model is AT CAPACITY", None, False),
                    ("You've hit your usage limit. Selected model is at capacity", "usage limit", False),
                    ("401 Unauthorized. Selected model is at capacity", None, True)):
                with self.subTest(event_type=event_type, message=message):
                    event = ({"type": event_type, "error": {"message": message}}
                             if event_type == "turn.failed" else {"type": event_type, "message": message})
                    stream = worker.MonitorEvents("")
                    result = worker.monitor_event({}, "main", event, stream,
                                                  worker.TurnControl(None, 3), False, False, 0)
                    self.assertIsNone(result)
                    self.assertEqual(stream.reason, expected_reason)
                    self.assertEqual(stream.authentication_failed, expected_auth)
                    self.assertEqual(stream.capacity_failed, not expected_reason and not expected_auth)
        self.assertFalse(worker.capacity_error({"type": "turn.completed", "message": "at capacity"}))
        self.assertFalse(worker.capacity_error({"type": "item.completed", "item": {
            "type": "agent_message", "text": "The selected model was at capacity earlier."}}))

    def test_capacity_after_thread_started_resumes_and_runs_checks_in_both_languages(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        for language, event in (("ru", "turn.failed"), ("en", "error")):
            with self.subTest(language=language, event=event):
                self.configure(language=language)
                (self.home / "exec-count").unlink(missing_ok=True)
                summary = "Recovered from model capacity in " + language
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "capacity_error", "event": event,
                     "error": "Selected model is AT CAPACITY. Please try a different model."},
                    {"mode": "complete", "reply": self.work_result(summary=summary)}])
                before = len(self.invocations())
                slug = "capacity-resume-" + language
                self.start(slug, "--check", "printf 'checked\\n' > capacity-check")
                output = self.wait(slug)
                executions = self.invocations()[before:]
                self.assertEqual(len(executions), 2)
                self.assert_resumed_same_thread(executions[0], executions[1], self.home)
                worker = self.worker()
                self.assertEqual(executions[1]["prompt"],
                                 worker.capacity_resume_prompt(worker.config.accounts_config()))
                for execution in executions:
                    self.assert_output_schema(execution, slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertEqual(state["capacity_retries"], 1)
                self.assertEqual(state["checks"][-1]["outcome"], "passed")
                self.assertEqual((Path(state["worktree"]) / "capacity-check").read_text(), "checked\n")
                self.assertFalse(state["rotations"])
                self.assertIn("== retried: model at capacity", output)
                self.assertLess(output.index("== retried: model at capacity"), output.index(summary))

    def test_capacity_before_thread_started_uses_a_new_exec_with_the_original_prompt(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error", "before_thread": True, "event": "error"},
            {"mode": "complete", "reply": self.work_result()}])
        self.start("capacity-before-thread")
        output = self.wait("capacity-before-thread")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(all(not execution["resume"] for execution in executions))
        self.assertNotEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertEqual(executions[1]["prompt"], executions[0]["prompt"])
        self.assertIn(self.brief.read_text().strip(), executions[1]["prompt"])
        self.assertTrue(all(execution["home"] == str(self.home) for execution in executions))
        state = self.state("capacity-before-thread")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["capacity_retries"], 1)
        self.assertEqual([entry["thread"] for entry in state["threads"]], [executions[1]["thread"]])
        self.assertIn("== retried: model at capacity", output)

    def test_second_capacity_failure_finishes_without_a_third_exec_or_checks(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error"}, {"mode": "capacity_error", "event": "error"},
            {"mode": "complete", "reply": "Unexpected third execution"}])
        self.start("capacity-twice", "--review", "--check", "touch unexpected-check")
        output = self.cli("wait", "capacity-twice", "--timeout", "10")
        state = self.state("capacity-twice")
        self.assertEqual(state["outcome"], "failed")
        self.assertFalse(state.get("pid"))
        self.assertEqual(state["capacity_retries"], 1)
        self.assertEqual(len(self.invocations()), 2)
        self.assert_resumed_same_thread(*self.invocations(), self.home)
        self.assertFalse(state.get("checks"))
        self.assertFalse(state.get("reviews"))
        self.assertFalse((Path(state["worktree"]) / "unexpected-check").exists())
        self.assertIn("== retried: model at capacity", output)
        self.assertNotIn("Unexpected third execution", output)

    def test_stop_during_capacity_pause_prevents_a_retry_exec(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "30"
        self.env["CODEX_TASK_SLEEP_STEP"] = "60"
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error"}, {"mode": "complete", "reply": "Unexpected retry"}])
        self.start("capacity-stop", "--review", "--check", "touch unexpected-check")
        self.until(lambda: self.state("capacity-stop").get("capacity_retries") == 1)
        self.assertEqual(self.state("capacity-stop")["outcome"], "running")
        self.cli("stop", "capacity-stop", timeout=10)
        output = self.wait("capacity-stop", timeout=10)
        state = self.state("capacity-stop")
        self.assertEqual(state["outcome"], "stopped")
        self.assertFalse(state.get("pid"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(state.get("checks"))
        self.assertFalse(state.get("reviews"))
        self.assertNotIn("Unexpected retry", output)

    def test_signal_during_capacity_pause_prevents_a_retry_exec(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "30"
        self.env["CODEX_TASK_SLEEP_STEP"] = "60"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error"}, {"mode": "complete", "reply": "Unexpected retry"}])
        self.start("capacity-signal", "--review", "--check", "touch unexpected-check")
        self.until(lambda: self.state("capacity-signal").get("capacity_retries") == 1)
        os.kill(self.state("capacity-signal")["pid"], signal.SIGTERM)
        output = self.wait("capacity-signal", timeout=10)
        state = self.state("capacity-signal")
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual(state["signal"]["name"], "SIGTERM")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(state.get("checks"))
        self.assertFalse(state.get("reviews"))
        self.assertIn("== interrupted externally by SIGTERM", output)

    def test_capacity_retries_without_account_configuration_or_quota_rpcs(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error"},
            {"mode": "complete", "reply": self.work_result()}])
        self.start("capacity-no-accounts", "--check", "true")
        output = self.wait("capacity-no-accounts")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assert_resumed_same_thread(executions[0], executions[1], self.home)
        self.assertFalse(any(entry.get("command") == "app-server" for entry in self.trace()))
        state = self.state("capacity-no-accounts")
        self.assertEqual((state["outcome"], state["capacity_retries"]), ("completed", 1))
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== retried: model at capacity", output)

    def test_work_review_fix_and_final_review_each_receive_one_capacity_retry(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.configure(language="en")
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Corrected the reported code."}
                     for finding in self.review_result()["findings"]]
        replies = (self.work_result(), self.review_result(),
                   self.work_result(findings=decisions), self.review_result(False))
        runs = []
        for reply in replies:
            runs.extend([{"mode": "capacity_error"}, {"mode": "complete", "reply": reply}])
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("capacity-phases", "--review", "--fix-rounds", "1", "--check", "true")
        output = self.wait("capacity-phases")
        executions = self.invocations()
        self.assertEqual(len(executions), 8)
        worker = self.worker()
        for index in (0, 2, 4, 6):
            self.assert_resumed_same_thread(executions[index], executions[index + 1], self.home)
            self.assertEqual(executions[index + 1]["prompt"],
                             worker.capacity_resume_prompt({"language": "en"}))
            for execution in executions[index:index + 2]:
                self.assert_output_schema(execution, "capacity-phases", review=index in (2, 6))
        self.assertEqual(executions[0]["thread"], executions[4]["thread"])
        self.assertNotEqual(executions[2]["thread"], executions[0]["thread"])
        self.assertNotEqual(executions[6]["thread"], executions[2]["thread"])
        state = self.state("capacity-phases")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["capacity_retries"], 4)
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(len(state["checks"]), 2)
        self.assertIn("== retried: model at capacity", output)
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual(turn["capacity_retries"], 4)
        phases = self.phase_telemetry_journal()
        self.assertEqual([row["phase"] for row in phases],
                         ["work", "check", "review", "fix", "check", "review"])
        self.assertEqual([row["capacity_retries"] for row in phases], [1, 0, 1, 1, 0, 1])
        report = json.loads(self.cli("report", "--slug", "capacity-phases", "--json"))
        self.assertEqual(report["summary"]["capacity_retries"], 4)
        self.assertEqual(report["tasks"][0]["capacity_retries"], 4)
        self.assertIn("Capacity retries: 4", self.cli("report", "--slug", "capacity-phases"))

    def test_capacity_retry_counter_resets_for_the_next_spawn(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error"}, {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result(summary="Next turn completed")}])
        self.start("capacity-next-turn")
        self.wait("capacity-next-turn")
        self.assertEqual(self.state("capacity-next-turn")["capacity_retries"], 1)
        self.cli("say", "capacity-next-turn", "Continue the same task")
        output = self.wait("capacity-next-turn")
        self.assertEqual(self.state("capacity-next-turn")["capacity_retries"], 0)
        self.assertEqual(len(self.invocations()), 3)
        self.assertNotIn("== retried: model at capacity", output)
        turns = [row for row in self.telemetry_journal() if row["phase"] == "turn"]
        self.assertEqual([row["capacity_retries"] for row in turns], [1, 0])

    def test_capacity_pause_is_excluded_from_active_time_and_the_time_budget(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "3"
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error"},
            {"mode": "complete", "delay": 0.3, "reply": self.work_result()}])
        self.start("capacity-pause-budget", "--max-minutes", "2.4")
        self.wait("capacity-pause-budget", timeout=12)
        state = self.state("capacity-pause-budget")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        phase = next(row for row in self.phase_telemetry_journal() if row["phase"] == "work")
        self.assertGreaterEqual(phase["sleep_seconds"], 3)
        self.assertLess(phase["active_seconds"], phase["finished"] - phase["started"] - 2.9)
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual(turn["capacity_retries"], 1)

    def test_active_time_budget_accumulates_across_capacity_retry(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en")
        result = self.work_result("partial", summary="Saved after the shared time budget")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error", "delay": 1.8},
            {"mode": "complete", "delay": 2.2, "reply": "Unexpected fresh time budget"},
            {"mode": "complete", "reply": result}])
        self.start("capacity-shared-time", "--max-minutes", "1.6", "--check", "true")
        output = self.wait("capacity-shared-time", timeout=15)
        state = self.state("capacity-shared-time")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["capacity_retries"], 1)
        self.assertEqual(state["result"], result)
        self.assertEqual(len(self.invocations()), 3)
        self.assertTrue(all(entry["thread"] == self.invocations()[0]["thread"]
                            for entry in self.invocations()))
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== turn stopped: time budget 1.6 min exhausted", output)
        self.assertNotIn("Unexpected fresh time budget", output)

    def test_weekly_budget_accumulates_across_capacity_retry(self):
        self.env["CODEX_TASK_CAPACITY_RETRY_SECONDS"] = "0.05"
        self.configure(language="en")
        result = self.work_result("partial", summary="Saved after the shared weekly budget")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "capacity_error", "weekly_used_before": 10, "weekly_used_after": 12},
            {"mode": "hold", "weekly_used_before": 12, "weekly_used_after": 15,
             "weekly_update_after": 0.3},
            {"mode": "complete", "reply": result,
             "weekly_used_before": 15, "weekly_used_after": 15}])
        self.start("capacity-shared-weekly", "--max-weekly", "4", "--check", "true")
        output = self.wait("capacity-shared-weekly", timeout=15)
        state = self.state("capacity-shared-weekly")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["capacity_retries"], 1)
        self.assertEqual((state["turn_stop"]["weekly"], state["turn_stop"]["spent"]), (4, 5))
        self.assertEqual(state["weekly_used_percent"], 5)
        self.assertEqual(state["result"], result)
        self.assertEqual(len(self.invocations()), 3)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== turn stopped: weekly budget 4% exhausted (spent 5%)", output)
