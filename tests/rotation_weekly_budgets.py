"""Weekly usage and budget tests."""
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rotation_support import IMPOSSIBLE_PID, PYTHON, SCRIPT, RotationTestCase


class WeeklyBudgetTests(RotationTestCase):
    def record_weekly_phase_usage(self, worker, st, phase, account, thread, path, seed_first=True):
        before = st.get("weekly_used_percent", 0)
        seeded = not seed_first

        def record(limits):
            nonlocal seeded
            if worker.weekly_used(limits) is not None:
                worker.record_weekly_usage(st, account, limits, seed=not seeded)
                seeded = True

        worker.rollout_limits(path, on_limits=record)
        phase.add_usage(account, thread, None, path, 0,
                        weekly_spent=st.get("weekly_used_percent", 0) - before)

    def test_weekly_budget_kills_exec_group_wraps_same_thread_checks_and_skips_review(self):
        self.configure()
        result = self.work_result("partial", summary="Saved the weekly-budget checkpoint.",
                                  remaining=["Complete the remaining integration."])
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "spawn_child": True, "weekly_used_before": 10,
             "weekly_used_after": 20.4, "weekly_update_after": 0.35},
            {"mode": "complete", "reply": result, "weekly_used_before": 20.4,
             "weekly_used_after": 20.6, "write_files": {"weekly-wrapped.txt": "saved\n"}}])
        self.start("weekly-cap", "--max-weekly", "10", "--review", "--fix-rounds", "2",
                   "--check", "test -f weekly-wrapped.txt && printf 'checked\\n' > weekly-check")
        self.until(lambda: any(event.get("command") == "exec-child" for event in self.trace()))
        child = next(event["child_pid"] for event in self.trace() if event.get("command") == "exec-child")
        self.extra_pids.add(child)
        output = self.wait("weekly-cap", timeout=15)
        state = self.state("weekly-cap")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["max_task_weekly_percent"], 10)
        self.assertEqual(state["turn_stop"]["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stop"]["weekly"], 10)
        self.assertAlmostEqual(state["turn_stop"]["spent"], 10.4)
        self.assertNotIn("minutes", state["turn_stop"])
        self.assertAlmostEqual(state["weekly_used_percent"], 10.6)
        self.assertEqual(state["result"], result)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual((Path(state["worktree"]) / "weekly-check").read_text(), "checked\n")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assert_output_schema(executions[1], "weekly-cap")
        self.assertEqual(executions[1]["prompt"],
                         self.worker().wrapup_prompt(state, state["turn_stop"], {"language": "ru"}))
        self.assertFalse(self.review_invocations())
        self.assertIn("== turn stopped: weekly budget 10% exhausted (spent 10.4%)", output)
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(self.process_is_live(executions[0]["pid"]))
        self.assertTrue(any(event.get("command") == "signal" and
                            event.get("pid") == executions[0]["pid"] for event in self.trace()))
        self.until(lambda: len(self.telemetry_journal()) == 4)
        self.assertEqual([(row["phase"], row["outcome"]) for row in self.phase_telemetry_journal()],
                         [("work", "budget_exhausted"), ("wrapup", "completed"), ("check", "passed")])
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual((turn["max_task_weekly_percent"], turn["budget_stop"]), (10, "weekly"))
        report = json.loads(self.cli("report", "--slug", "weekly-cap", "--json"))
        task = report["tasks"][0]
        self.assertEqual((task["max_task_weekly_percent"], task["budget_stop"]), (10, "weekly"))
        self.assertAlmostEqual(task["weekly_used_percent"], state["weekly_used_percent"])
        text = self.cli("report", "--slug", "weekly-cap")
        self.assertIn("weekly", text)
        self.assertIn("budget %", text)
        self.assertRegex(text, r"weekly-cap\s+.*\s10\s+weekly")

    def test_weekly_budget_usage_matches_report_across_accounts_say_and_stale_sample(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 12, "weekly_used_after": 15}])
        self.account(self.other, "B", self.reset + 3600, used=95, runs=[
            {"mode": "complete", "reply": self.review_result(False),
             "weekly_used_before": 95, "weekly_used_after": 2}])
        self.start("weekly-accounts", "--max-weekly", "50")
        self.wait("weekly-accounts")
        self.assertEqual(self.state("weekly-accounts")["weekly_used_percent"], 2)
        path = self.home / "fixture.json"
        account = json.loads(path.read_text())
        account["primary"].update(usedPercent=12, resetsAt=self.reset + 7200)
        path.write_text(json.dumps(account))
        self.cli("review", "weekly-accounts")
        self.wait("weekly-accounts")
        self.assertEqual(self.review_invocations()[0]["home"], str(self.other))
        self.assertEqual(self.state("weekly-accounts")["weekly_used_percent"], 2,
                         "a lower sample without a newer reset does not lower the account mark")
        self.cli("say", "weekly-accounts", "Continue the same task")
        self.wait("weekly-accounts")
        state = self.state("weekly-accounts")
        self.assertEqual(state["weekly_used_percent"], 5)
        self.assertEqual([event["home"] for event in self.invocations()],
                         [str(self.home), str(self.other), str(self.home)])
        report = json.loads(self.cli("report", "--slug", "weekly-accounts", "--json"))
        self.assertEqual(state["weekly_used_percent"], report["tasks"][0]["weekly_used_percent"])
        self.assertEqual({row["account"]: row["weekly_used_percent"] for row in report["accounts"]},
                         {"main": 5, "second": 0})

    def test_weekly_budget_combines_live_usage_across_rotation_within_one_phase(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "usage_error", "weekly_used_before": 10, "weekly_used_after": 12}])
        self.account(self.other, "B", self.reset + 3600, used=20, runs=[
            {"mode": "hold", "weekly_used_before": 22, "weekly_used_after": 23.5,
             "weekly_update_after": 0.35},
            {"mode": "complete", "reply": self.work_result("partial"),
             "weekly_used_before": 23.5, "weekly_used_after": 24}])
        self.start("weekly-rotation", "--max-weekly", "5", "--review")
        output = self.wait("weekly-rotation", timeout=15)
        state = self.state("weekly-rotation")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stop"]["weekly"], 5)
        self.assertEqual(state["turn_stop"]["spent"], 5.5)
        self.assertEqual(state["weekly_used_percent"], 6)
        self.assertIn("== turn stopped: weekly budget 5% exhausted (spent 5.5%)", output)
        executions = self.invocations()
        self.assertEqual([event["home"] for event in executions],
                         [str(self.home), str(self.other), str(self.other)])
        self.assertNotEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertTrue(executions[2]["resume"])
        self.assertEqual(executions[1]["thread"], executions[2]["thread"])
        self.assertFalse(self.review_invocations())
        report = json.loads(self.cli("report", "--slug", "weekly-rotation", "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], state["weekly_used_percent"])
        self.assertEqual({row["account"]: row["weekly_used_percent"] for row in report["accounts"]},
                         {"main": 2, "second": 4})

    def test_weekly_budget_on_usage_error_wraps_without_waiting_for_subscription_reset(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "usage_error", "weekly_used_before": 10, "weekly_used_after": 16},
            {"mode": "complete", "reply": self.work_result("partial"),
             "weekly_used_before": 16, "weekly_used_after": 16}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("weekly-before-sleep", "--max-weekly", "5", "--review")
        output = self.wait("weekly-before-sleep", timeout=12)
        state = self.state("weekly-before-sleep")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stop"]["weekly"], 5)
        self.assertEqual(state["turn_stop"]["spent"], 6)
        self.assertEqual(state["weekly_used_percent"], 6)
        self.assertIn("== turn stopped: weekly budget 5% exhausted (spent 6%)", output)
        executions = self.invocations()
        self.assertEqual([event["home"] for event in executions], [str(self.home), str(self.home)])
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertFalse(state.get("sleeping"))
        self.assertFalse(state["rotations"])
        self.assertFalse(self.review_invocations())

    def test_final_weekly_budget_after_failed_exec_with_rotation_disabled_wraps_and_checks(self):
        (self.home / "codex-worker.json").write_text(json.dumps({"language": "en"}))
        result = self.work_result("partial", summary="Saved the quota-error checkpoint.",
                                  remaining=["Continue after increasing the task budget."])
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "usage_error", "weekly_used_before": 10, "weekly_used_after": 16},
            {"mode": "complete", "reply": result,
             "weekly_used_before": 16, "weekly_used_after": 16}])
        self.start("weekly-failed-exec", "--max-weekly", "5", "--review", "--check", "true")
        output = self.wait("weekly-failed-exec", timeout=12)
        state = self.state("weekly-failed-exec")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stop"]["outcome"], "budget_exhausted")
        self.assertEqual((state["turn_stop"]["weekly"], state["turn_stop"]["spent"]), (5, 6))
        self.assertEqual(state["weekly_used_percent"], 6)
        self.assertEqual(state["result"], result)
        self.assertEqual([(check["cmd"], check["outcome"]) for check in state["checks"]],
                         [("true", "passed")])
        self.assertEqual(state["review_skipped"], "budget_exhausted")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertEqual(executions[1]["prompt"],
                         self.worker().wrapup_prompt(state, state["turn_stop"], {"language": "en"}))
        self.assertFalse(self.review_invocations())
        self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        self.assertFalse(state["rotations"])
        self.assertIn("== turn stopped: weekly budget 5% exhausted (spent 6%)", output)
        self.until(lambda: len(self.telemetry_journal()) == 4)
        self.assertEqual([(row["phase"], row["outcome"]) for row in self.phase_telemetry_journal()],
                         [("work", "budget_exhausted"), ("wrapup", "completed"), ("check", "passed")])
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual((turn["max_task_weekly_percent"], turn["budget_stop"]), (5, "weekly"))
        task = json.loads(self.cli("report", "--slug", "weekly-failed-exec", "--json"))["tasks"][0]
        self.assertEqual((task["outcome"], task["max_task_weekly_percent"], task["budget_stop"]),
                         ("budget_exhausted", 5, "weekly"))
        self.assertEqual(task["weekly_used_percent"], 6)

    def test_final_weekly_budget_after_completed_exec_preserves_result_and_checks_without_wrapup(self):
        (self.home / "codex-worker.json").write_text(json.dumps({"language": "en"}))
        result = self.work_result(summary="Finished before the final quota sample was flushed.")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": result, "weekly_used_before": 10,
             "weekly_used_after": 16, "final_usage_after_completion": True}])
        self.start("weekly-completed-exec", "--max-weekly", "5", "--check", "true")
        output = self.wait("weekly-completed-exec", timeout=12)
        state = self.state("weekly-completed-exec")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stop"]["outcome"], "budget_exhausted")
        self.assertEqual((state["turn_stop"]["weekly"], state["turn_stop"]["spent"]), (5, 6))
        self.assertEqual(state["weekly_used_percent"], 6)
        self.assertEqual(state["result"], result)
        self.assertEqual([(check["cmd"], check["outcome"]) for check in state["checks"]],
                         [("true", "passed")])
        self.assertEqual(len(self.invocations()), 1, "a completed turn needs no wrap-up execution")
        self.assertFalse(self.review_invocations())
        self.assertIn("== turn stopped: weekly budget 5% exhausted (spent 6%)", output)
        self.assertIn(result["summary"], output)
        self.until(lambda: len(self.telemetry_journal()) == 3)
        self.assertEqual([(row["phase"], row["outcome"]) for row in self.phase_telemetry_journal()],
                         [("work", "completed"), ("check", "passed")])
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual((turn["max_task_weekly_percent"], turn["budget_stop"]), (5, "weekly"))
        task = json.loads(self.cli("report", "--slug", "weekly-completed-exec", "--json"))["tasks"][0]
        self.assertEqual((task["outcome"], task["max_task_weekly_percent"], task["budget_stop"]),
                         ("budget_exhausted", 5, "weekly"))
        self.assertEqual(task["weekly_used_percent"], 6)

    def test_weekly_marks_ignore_late_and_overlapping_phase_samples(self):
        worker = self.worker()
        slug = "weekly-overlapping-samples"
        (worker.TASKS / slug).mkdir(parents=True)
        st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo), "created": time.time(),
              "account": "main"}
        for turn, values in enumerate(((10, 17), (12, 14)), 1):
            path = self.root / ("overlapping-%d.jsonl" % turn)
            path.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": value, "window_minutes": 10080,
                            "resets_at": self.reset}}}}) + "\n" for value in values))
            st["turns"] = turn
            with worker.record_phase(st, "work", lambda: False) as phase:
                self.record_weekly_phase_usage(worker, st, phase, "main", "thread-%d" % turn, path)
                phase.finish(outcome="completed")
            worker.save(st)
            st = worker.load(slug)
        self.assertEqual(st["weekly_used_percent"], 7)
        rows = self.phase_telemetry_journal()
        self.assertEqual([row["weekly_spent"] for row in rows], [7, 0])
        self.assertEqual([row["usage"][0]["weekly_spent"] for row in rows], [7, 0])
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["summary"]["weekly_used_percent"], 7)
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], st["weekly_used_percent"])
        self.assertEqual(report["accounts"][0]["weekly_used_percent"], 7)

    def test_weekly_marks_shuffled_samples_keep_the_same_total(self):
        worker = self.worker()
        for slug, values in (("weekly-original-order", (10, 17, 12, 14)),
                             ("weekly-shuffled-order", (14, 12, 17, 10))):
            with self.subTest(order=values):
                (worker.TASKS / slug).mkdir(parents=True)
                st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo),
                      "created": time.time(), "account": "main", "turns": 1}
                seed = worker.normalize_limits({"primary": {"used_percent": 10,
                                               "window_minutes": 10080, "resets_at": self.reset}})
                self.assertEqual(worker.record_weekly_usage(st, "main", seed), 0)
                path = self.root / (slug + ".jsonl")
                path.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": value, "window_minutes": 10080,
                                "resets_at": self.reset}}}}) + "\n" for value in values))
                with worker.record_phase(st, "work", lambda: False) as phase:
                    self.record_weekly_phase_usage(worker, st, phase, "main", "thread", path,
                                                   seed_first=False)
                    phase.finish(outcome="completed")
                rows = [row for row in self.phase_telemetry_journal() if row["slug"] == slug]
                self.assertEqual([(row["weekly_spent"], row["usage"][0]["weekly_spent"])
                                  for row in rows], [(7, 7)])
                self.assertEqual(st["weekly_used_percent"], 7)
                task = json.loads(self.cli("report", "--slug", slug, "--json"))["tasks"][0]
                self.assertEqual(task["weekly_used_percent"], 7)

    def test_weekly_marks_keep_accounts_and_windows_separate(self):
        worker = self.worker()
        slug = "weekly-account-windows"
        (worker.TASKS / slug).mkdir(parents=True)
        st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo), "created": time.time()}
        cases = [("main", 10, 17, self.reset, 7), ("second", 20, 22, self.reset, 2),
                 ("main", 12, 14, self.reset, 0), ("main", 2, 4, None, 0),
                 ("main", 17, 18, self.reset + 3600, 1),
                 ("main", 2, 4, self.reset + 604800, 2),
                 ("main", 4, 5, self.reset + 604800, 1),
                 ("main", 10, 17, self.reset, 0)]
        expected = 0
        for turn, (account, before, after, reset, delta) in enumerate(cases, 1):
            with self.subTest(account=account, before=before, after=after, reset=reset):
                path = self.root / ("window-%d.jsonl" % turn)
                path.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": value, "window_minutes": 10080,
                                "resets_at": reset}}}}) + "\n" for value in (before, after)))
                st.update(account=account, turns=turn)
                with worker.record_phase(st, "work", lambda: False) as phase:
                    self.record_weekly_phase_usage(worker, st, phase, account, "thread-%d" % turn, path)
                    phase.finish(outcome="completed")
                expected += delta
                self.assertEqual(st["weekly_used_percent"], expected)
                self.assertEqual(self.phase_telemetry_journal()[-1]["weekly_spent"], delta)
                worker.save(st)
                st = worker.load(slug)
        # This is the sole compatibility contract for the persisted mark format.
        expected_marks = {
            "main": {"reset": self.reset + 604800, "used": 5},
            "second": {"reset": self.reset, "used": 22}}
        self.assertEqual(json.loads(worker.state_path(slug).read_text())["weekly_marks"], expected_marks)
        self.assertEqual(worker.load(slug)["weekly_marks"], expected_marks)
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], 13)
        self.assertEqual({row["account"]: row["weekly_used_percent"] for row in report["accounts"]},
                         {"main": 11, "second": 2})

    def test_weekly_budget_counts_snapshot_to_first_rollout_sample(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, used=8, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12,
             "final_usage_after_completion": True}])
        self.start("weekly-snapshot-seed", "--max-weekly", "3", "--review")
        self.wait("weekly-snapshot-seed")
        state = self.state("weekly-snapshot-seed")
        self.assertEqual(state["weekly_used_percent"], 4)
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["turn_stop"]["spent"], 4)
        self.assertFalse(self.review_invocations())
        self.assertEqual(len(self.invocations()), 1)
        rows = self.phase_telemetry_journal()
        self.assertEqual([row["weekly_spent"] for row in rows], [4])
        self.assertEqual(rows[0]["usage"][0]["weekly_spent"], 4)
        report = json.loads(self.cli("report", "--slug", "weekly-snapshot-seed", "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], state["weekly_used_percent"])

    def weekly_exec_gap_scenario(self, snapshots):
        if snapshots:
            self.configure(language="en")
        else:
            (self.home / "codex-worker.json").write_text(json.dumps({"language": "en"}))
        slug = "weekly-gap-snapshot" if snapshots else "weekly-gap-first-sample"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12,
             "final_usage_after_completion": True},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 20, "weekly_samples_after_initial": [12],
             "weekly_used_after": 21, "final_usage_after_completion": True}])
        self.start(slug, "--max-weekly", "5")
        self.wait(slug)
        self.assertEqual(self.state(slug)["weekly_used_percent"], 2)
        path = self.home / "fixture.json"
        fixture = json.loads(path.read_text())
        fixture["primary"]["usedPercent"] = 20
        path.write_text(json.dumps(fixture))
        self.cli("say", slug, "Continue after other tasks used the account")
        self.wait(slug)
        state = self.state(slug)
        self.assertEqual(state["weekly_used_percent"], 3)
        self.assertEqual(state["outcome"], "completed")
        self.assertNotIn("turn_stop", state)
        self.assertEqual(len(self.invocations()), 2)
        self.assertTrue(self.invocations()[1]["resume"])
        self.assertEqual(self.invocations()[0]["thread"], self.invocations()[1]["thread"])
        self.until(lambda: len(self.telemetry_journal()) == 4)
        self.assertEqual([row["weekly_spent"] for row in self.phase_telemetry_journal()], [2, 1])
        self.assertEqual([row["usage"][0]["weekly_spent"] for row in self.phase_telemetry_journal()], [2, 1])
        task = json.loads(self.cli("report", "--slug", slug, "--json"))["tasks"][0]
        self.assertEqual(task["weekly_used_percent"], state["weekly_used_percent"])
        quota_calls = [event for event in self.trace() if event.get("command") == "quota-snapshot"]
        self.assertEqual(len(quota_calls), 4 if snapshots else 0,
                         "seeding must reuse account-selection snapshots, without another quota RPC")
        if snapshots:
            self.assertEqual([event["primary"]["usedPercent"] for event in quota_calls
                              if event["home"] == str(self.home)], [10, 20])
        second_thread = self.invocations()[1]["thread"]
        self.assertEqual([event["primary"]["usedPercent"] for event in self.trace()
                          if event.get("command") == "rollout-snapshot"
                          and event.get("thread") == second_thread], [10, 12, 20, 12, 21])

    def test_weekly_marks_reseed_exec_from_selection_snapshot_without_charging_pause(self):
        self.weekly_exec_gap_scenario(snapshots=True)

    def test_weekly_marks_reseed_exec_from_first_fresh_sample_without_charging_pause(self):
        self.weekly_exec_gap_scenario(snapshots=False)

    def test_weekly_marks_reseed_same_account_auth_retry_from_fresh_snapshot(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "auth_error", "weekly_used_before": 12, "used_after": 20},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 20, "weekly_samples_after_initial": [12],
             "weekly_used_after": 21, "final_usage_after_completion": True}])
        self.start("weekly-auth-reseed", "--max-weekly", "5")
        self.wait("weekly-auth-reseed")
        state = self.state("weekly-auth-reseed")
        self.assertEqual(state["weekly_used_percent"], 3)
        self.assertEqual(state["outcome"], "completed")
        self.assertNotIn("turn_stop", state)
        self.assertFalse(state["rotations"])
        self.assertEqual(len(self.invocations()), 2)
        self.assertTrue(self.invocations()[1]["resume"])
        self.until(lambda: len(self.telemetry_journal()) == 2)
        self.assertEqual(self.phase_telemetry_journal()[0]["weekly_spent"], 3)
        self.assertEqual([entry["weekly_spent"] for entry in
                          self.phase_telemetry_journal()[0]["usage"]], [2, 1])
        quota_calls = [event for event in self.trace() if event.get("command") == "quota-snapshot"]
        self.assertEqual(len(quota_calls), 4)
        self.assertEqual([event["primary"]["usedPercent"] for event in quota_calls
                          if event["home"] == str(self.home)], [10, 20])
        task = json.loads(self.cli("report", "--slug", "weekly-auth-reseed", "--json"))["tasks"][0]
        self.assertEqual(task["weekly_used_percent"], state["weekly_used_percent"])

    def test_weekly_marks_count_growth_on_both_sides_of_reset_within_exec(self):
        worker = self.worker()
        slug = "weekly-reset-inside-exec"
        (worker.TASKS / slug).mkdir(parents=True)
        st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo),
              "created": time.time(), "account": "main", "turns": 1}
        samples = [(90, self.reset), (92, self.reset), (1, self.reset + 604800),
                   (3, self.reset + 604800), (95, self.reset)]
        path = self.root / "reset-inside-exec.jsonl"
        path.write_text("".join(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"primary": {
                "used_percent": used, "window_minutes": 10080, "resets_at": reset}}}}) + "\n"
            for used, reset in samples))
        with worker.record_phase(st, "work", lambda: False) as phase:
            self.record_weekly_phase_usage(worker, st, phase, "main", "thread", path)
            phase.finish(outcome="completed")
        self.assertEqual(self.phase_telemetry_journal()[0]["usage"][0]["weekly_spent"], 4)
        self.assertEqual(st["weekly_used_percent"], 4)
        self.assertEqual(self.phase_telemetry_journal()[0]["weekly_spent"], 4)
        task = json.loads(self.cli("report", "--slug", slug, "--json"))["tasks"][0]
        self.assertEqual(task["weekly_used_percent"], 4)

    def test_weekly_budget_counts_new_window_before_automatic_review(self):
        (self.home / "codex-worker.json").write_text(json.dumps({"language": "en"}))
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12,
             "final_usage_after_completion": True},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 2, "weekly_used_after": 4,
             "final_usage_after_completion": True}])
        self.start("weekly-new-window", "--max-weekly", "3")
        self.wait("weekly-new-window")
        self.assertEqual(self.state("weekly-new-window")["weekly_used_percent"], 2)
        path = self.home / "fixture.json"
        account = json.loads(path.read_text())
        account["primary"].update(usedPercent=2, resetsAt=self.reset + 604800)
        path.write_text(json.dumps(account))
        self.cli("say", "weekly-new-window", "Continue after the weekly reset", "--review")
        output = self.wait("weekly-new-window")
        state = self.state("weekly-new-window")
        self.assertEqual(state["weekly_used_percent"], 4)
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual((state["turn_stop"]["outcome"], state["turn_stop"]["weekly"],
                          state["turn_stop"]["spent"]), ("budget_exhausted", 3, 4))
        self.assertEqual(len(self.invocations()), 2)
        self.assertFalse(self.review_invocations())
        self.assertIn("== turn stopped: weekly budget 3% exhausted (spent 4%)", output)
        self.until(lambda: len(self.telemetry_journal()) == 4)
        self.assertEqual([(row["usage"][0]["weekly_used_before"], row["usage"][0]["weekly_used_after"])
                          for row in self.phase_telemetry_journal()], [(10, 12), (2, 4)])
        task = json.loads(self.cli("report", "--slug", "weekly-new-window", "--json"))["tasks"][0]
        self.assertEqual(task["weekly_used_percent"], state["weekly_used_percent"])
        self.assertEqual((task["outcome"], task["budget_stop"]), ("budget_exhausted", "weekly"))

    def test_weekly_marks_count_new_window_recovered_above_previous_percentage(self):
        worker = self.worker()
        slug = "weekly-reset-recovered"
        (worker.TASKS / slug).mkdir(parents=True)
        st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo), "created": time.time()}
        for turn, (before, after, reset) in enumerate(
                [(10, 12, self.reset), (2, 14, self.reset + 604800)], 1):
            path = self.root / ("recovered-%d.jsonl" % turn)
            path.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": value, "window_minutes": 10080,
                            "resets_at": reset}}}}) + "\n" for value in (before, after)))
            st.update(account="main", turns=turn)
            with worker.record_phase(st, "work", lambda: False) as phase:
                self.record_weekly_phase_usage(worker, st, phase, "main", "thread-%d" % turn, path)
                phase.finish(outcome="completed")
            worker.save(st)
            st = worker.load(slug)
        self.assertEqual(st["weekly_used_percent"], 14)
        rows = self.phase_telemetry_journal()
        self.assertEqual([(row["usage"][0]["weekly_used_before"], row["usage"][0]["weekly_used_after"])
                          for row in rows], [(10, 12), (2, 14)])
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], st["weekly_used_percent"])

    def test_weekly_budget_late_stopped_sample_does_not_overlap_wrapup(self):
        worker = self.worker()
        slug = "weekly-late-stop"
        (worker.TASKS / slug).mkdir(parents=True)
        account = {"name": "main", "home": str(self.home)}
        st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo), "base": "main",
              "kind": "work", "account": "main", "thread": "same-thread", "threads": [],
              "reviews": [], "rotations": [], "rounds": [], "turns": 1, "outcome": "running",
              "max_task_weekly_percent": 5}
        rollout = self.root / "late-rollout.jsonl"

        def quota(used):
            return json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                               "rate_limits": {"primary": {"used_percent": used,
                                                          "window_minutes": 10080}}}}) + "\n"

        def append_quota(*used):
            with rollout.open("a") as stream:
                stream.write("".join(quota(value) for value in used))

        append_quota(10)
        result = self.work_result("partial")
        completion = (json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(result)}}) +
            '\n{"type":"turn.completed"}\n').encode()
        exec_chunks = [b'{"type":"turn.started"}\n']
        wrap_chunks = [completion, b""]
        stopped_exec = [False]
        finished_wrap = [False]
        observed = []
        proc = mock.Mock(pid=IMPOSSIBLE_PID)
        proc.stdout.fileno.return_value = 99
        proc.poll.side_effect = lambda: 0 if stopped_exec[0] else None
        wrap = mock.Mock(pid=IMPOSSIBLE_PID)
        wrap.stdout.fileno.return_value = 100
        wrap.poll.side_effect = lambda: 0 if finished_wrap[0] else None
        wrap.wait.return_value = 0
        selector = mock.MagicMock()
        selector.__enter__.return_value = selector
        selector.select.side_effect = lambda timeout: (
            [(SimpleNamespace(fileobj=proc.stdout), worker.selectors.EVENT_READ)] if exec_chunks else [])
        wrap_selector = mock.MagicMock()
        wrap_selector.__enter__.return_value = wrap_selector
        wrap_selector.select.side_effect = lambda timeout: (
            [(SimpleNamespace(fileobj=wrap.stdout), worker.selectors.EVENT_READ)] if wrap_chunks else [])

        def read(fd, size):
            if fd == 99:
                observed.append("exec quota 16, no completion")
                append_quota(10, 16)
                return exec_chunks.pop(0)
            self.assertEqual(fd, 100)
            chunk = wrap_chunks.pop(0)
            if chunk:
                observed.append("wrap-up quotas 16, 18")
                self.assertTrue(stopped_exec[0])
                self.assertEqual(st["weekly_used_percent"], 7)
                append_quota(16, 18)
            else:
                finished_wrap[0] = True
            return chunk

        def terminate(process):
            if process is proc:
                stopped_exec[0] = True
                observed.append("late quota 17 after exec stopped")
                append_quota(17)

        with mock.patch.object(worker, "prepare_home"), \
                mock.patch.object(worker, "terminate", side_effect=terminate), \
                mock.patch.object(worker.subprocess, "Popen", side_effect=[proc, wrap]) as popen, \
                mock.patch.object(worker.selectors, "DefaultSelector", side_effect=[selector, wrap_selector]), \
                mock.patch.object(worker.os, "read", side_effect=read), \
                mock.patch.object(worker.fcntl, "ioctl", return_value=bytes(4)), \
                mock.patch.object(worker, "rollout_path", return_value=rollout):
            worker.run_turn(st, {"enabled": False, "accounts": [account]}, "work", None,
                            False, lambda: False, lambda seconds: None)
        self.assertEqual(observed, ["exec quota 16, no completion", "late quota 17 after exec stopped",
                                    "wrap-up quotas 16, 18"])
        self.assertEqual(popen.call_count, 2)
        self.assertIn("resume", popen.call_args_list[1].args[0])
        self.assertEqual(st["outcome"], "budget_exhausted")
        self.assertEqual(st["result"], result)
        self.assertEqual(st["weekly_used_percent"], 8)
        rows = self.phase_telemetry_journal()
        self.assertEqual([(row["phase"], row["weekly_spent"]) for row in rows],
                         [("work", 7), ("wrapup", 1)])
        self.assertEqual([row["usage"][0]["weekly_spent"] for row in rows], [7, 1])
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], st["weekly_used_percent"])

    def test_final_weekly_budget_drains_completion_before_counting_fix_once(self):
        worker = self.worker()
        slug = "weekly-completion-order"
        (worker.TASKS / slug).mkdir(parents=True)
        account = {"name": "main", "home": str(self.home)}
        st = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo),
              "base": "main", "kind": "work", "account": "main", "thread": "same-thread",
              "threads": [], "reviews": [], "rotations": [], "rounds": [], "turns": 1,
              "outcome": "running", "max_task_weekly_percent": 5}

        def quota(used):
            return json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                               "rate_limits": {"primary": {"used_percent": used,
                                                          "window_minutes": 10080}}}}) + "\n"

        work = self.root / "work-rollout.jsonl"
        work.write_text(quota(10) + quota(11))
        with worker.record_phase(st, "work", lambda: False) as phase:
            self.record_weekly_phase_usage(worker, st, phase, "main", "work-thread", work)
            phase.finish(outcome="completed")
        st["kind"] = "review"
        review = self.root / "review-rollout.jsonl"
        review.write_text(quota(11) + quota(12))
        with worker.record_phase(st, "review", lambda: False) as phase:
            self.record_weekly_phase_usage(worker, st, phase, "main", "review-thread", review)
            phase.finish(outcome="completed")
        self.assertEqual(st["weekly_used_percent"], 2)
        st.update(kind="work", rounds=[{}])
        rollout = self.root / "fix-rollout.jsonl"
        rollout.write_text(quota(12))
        result = self.work_result()
        message = (json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(result)}}) + "\n").encode()
        chunks = [message]
        observed = []
        exited = [False]
        proc = mock.Mock(pid=IMPOSSIBLE_PID)
        proc.stdout.fileno.return_value = 99
        proc.poll.side_effect = lambda: 0 if exited[0] else None
        proc.wait.return_value = 0
        selector = mock.MagicMock()
        selector.__enter__.return_value = selector
        selector.select.side_effect = lambda timeout: (
            [(SimpleNamespace(fileobj=proc.stdout), worker.selectors.EVENT_READ)] if chunks else [])

        def read(fd, size):
            chunk = chunks.pop(0)
            self.assertLessEqual(len(chunk), size)
            if chunk == message:
                observed.append("agent_message")
                with rollout.open("a") as stream:
                    stream.write(quota(12))
            elif chunk:
                observed.append("completion fragment")
                self.assertEqual(st["weekly_used_percent"], 6, "live samples update the task total immediately")
            else:
                exited[0] = True
            return chunk

        read_rollout_limits = worker.rollout_limits

        def read_limits(path, on_limits=None, offset=0):
            self.assertIs(path, rollout)
            if observed == ["agent_message"]:
                # Completion precedes the final quota, but remains unread.
                # Splitting it across reads requires the full stdout drain.
                chunks.extend([b'{"type":"turn.', b'completed"}\n', b""])
                with rollout.open("a") as stream:
                    stream.write(quota(16))
                observed.append("final quota")
            return read_rollout_limits(path, on_limits=on_limits, offset=offset)

        with mock.patch.object(worker, "prepare_home"), \
                mock.patch.object(worker, "terminate"), \
                mock.patch.object(worker.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(worker.selectors, "DefaultSelector", return_value=selector), \
                mock.patch.object(worker.os, "read", side_effect=read), \
                mock.patch.object(worker.fcntl, "ioctl", return_value=(
                    len(b'{"type":"turn.completed"}\n').to_bytes(4, sys.byteorder))), \
                mock.patch.object(worker, "rollout_path", return_value=rollout), \
                mock.patch.object(worker, "rollout_limits", side_effect=read_limits), \
                mock.patch.object(worker, "publish_pr") as publish_pr, \
                mock.patch.object(worker, "wrapup_turn", side_effect=AssertionError("unexpected wrap-up")) as wrapup:
            worker.run_turn(st, {"enabled": False, "accounts": [account]}, "finish the fix", None,
                            False, lambda: False, lambda seconds: None, new_turn=False)
        self.assertEqual(observed, ["agent_message", "final quota", "completion fragment", "completion fragment"])
        popen.assert_called_once()
        publish_pr.assert_called_once_with(st, mock.ANY)
        wrapup.assert_not_called()
        self.assertEqual(st["weekly_used_percent"], 6)
        self.assertEqual(st["outcome"], "budget_exhausted")
        self.assertEqual(st["turn_stop"]["spent"], 6)
        self.assertEqual(st["result"], result)
        rows = self.phase_telemetry_journal()
        self.assertEqual([(row["phase"], row["weekly_spent"]) for row in rows],
                         [("work", 1), ("review", 1), ("fix", 4)])
        self.assertEqual(len(rows[-1]["usage"]), 1)
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], 6)

    def test_final_weekly_budget_publishes_completed_workspace_work_before_check(self):
        self.configure(language="en", sandbox="workspace")
        subject = "Publish completed budgeted work"
        result = self.work_result(commit_message=subject)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "completed implementation\n"},
             "reply": result, "weekly_used_before": 10, "weekly_used_after": 16,
             "final_usage_after_completion": True}])
        command = ('test -z "$(git status --porcelain)" && '
                   'test "$(git log -1 --format=%s)" = "' + subject + '"')
        self.start("weekly-workspace-publish", "--max-weekly", "5", "--review", "--check", command)
        output = self.wait("weekly-workspace-publish")
        state = self.state("weekly-workspace-publish")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual((state["turn_stop"]["weekly"], state["turn_stop"]["spent"]), (5, 6))
        self.assertEqual(state["weekly_used_percent"], 6)
        self.assertEqual(state["result"], result)
        self.assertEqual([(check["cmd"], check["outcome"]) for check in state["checks"]],
                         [(command, "passed")])
        publish = state["publish"]
        self.assertEqual(publish["sha"], self.git("rev-parse", state["branch"]))
        self.assertEqual(self.git("log", "-1", "--format=%s", state["branch"]), subject)
        self.assertEqual(self.git("rev-list", "--count", state["branch"]), "2")
        self.assertEqual(self.git("-C", state["worktree"], "status", "--porcelain"), "")
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse", state["branch"]),
                         publish["sha"])
        self.assertTrue(publish["pushed"])
        self.assertEqual(publish["pr"], "https://example.test/pull/1")
        self.assertEqual(publish["error"], "")
        self.assertEqual([event["argv"][:2] for event in self.gh_invocations()],
                         [["repo", "view"], ["pr", "list"], ["pr", "create"]])
        self.assertIn("== committed " + publish["sha"] + " " + json.dumps(subject) +
                      "; pushed; PR " + publish["pr"], output)
        self.assertIn("== turn stopped: weekly budget 5% exhausted (spent 6%)", output)
        self.assertEqual(state["review_skipped"], "budget")
        self.assertFalse(state.get("rounds"))
        self.assertEqual(len(self.invocations()), 1, "publication must not need a wrap-up execution")
        self.assertFalse(self.review_invocations())
        task = json.loads(self.cli("report", "--slug", "weekly-workspace-publish", "--json"))["tasks"][0]
        self.assertEqual((task["outcome"], task["max_task_weekly_percent"], task["budget_stop"]),
                         ("budget_exhausted", 5, "weekly"))

    def test_final_weekly_budget_publishes_completed_workspace_fix_before_check(self):
        self.configure(language="en", sandbox="workspace")
        first_subject = "Implement budgeted sandbox work"
        fix_subject = "Fix budgeted sandbox review findings"
        review = self.review_result()
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Fixed the reported defect."}
                     for finding in review["findings"]]
        result = self.work_result(commit_message=fix_subject, findings=decisions)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": first_subject + "\n"},
             "reply": self.work_result(commit_message=first_subject),
             "weekly_used_before": 10, "weekly_used_after": 11},
            {"mode": "complete", "reply": review,
             "weekly_used_before": 11, "weekly_used_after": 12},
            {"mode": "complete", "write_files": {"tracked.txt": fix_subject + "\n"},
             "reply": result, "weekly_used_before": 12, "weekly_used_after": 16,
             "final_usage_after_completion": True}])
        command = ('test -z "$(git status --porcelain)" && '
                   'test "$(git log -1 --format=%s)" = "$(cat tracked.txt)"')
        self.start("weekly-workspace-fix", "--max-weekly", "5", "--review", "--fix-rounds", "2",
                   "--check", command)
        output = self.wait("weekly-workspace-fix")
        state = self.state("weekly-workspace-fix")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual((state["turn_stop"]["weekly"], state["turn_stop"]["spent"]), (5, 6))
        self.assertEqual(state["weekly_used_percent"], 6)
        self.assertEqual(state["result"], result)
        self.assertEqual(state["auto_fix_stop"], "budget")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "passed")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertEqual(self.git("log", "-2", "--format=%s", state["branch"]).splitlines(),
                         [fix_subject, first_subject])
        self.assertEqual(self.git("rev-list", "--count", state["branch"]), "3")
        self.assertEqual(self.git("-C", state["worktree"], "status", "--porcelain"), "")
        publish = state["publish"]
        self.assertEqual(publish["sha"], self.git("rev-parse", state["branch"]))
        self.assertEqual(self.git("--git-dir", str(self.root / "origin.git"), "rev-parse", state["branch"]),
                         publish["sha"])
        self.assertTrue(publish["pushed"])
        self.assertEqual(publish["pr"], "https://example.test/pull/1")
        self.assertEqual(publish["error"], "")
        self.assertEqual([event["argv"][:2] for event in self.gh_invocations()],
                         [["repo", "view"], ["pr", "list"], ["pr", "create"],
                          ["repo", "view"], ["pr", "list"]])
        self.assertIn("== committed " + publish["sha"] + " " + json.dumps(fix_subject) +
                      "; pushed; PR " + publish["pr"], output)
        self.assertIn("== turn stopped: weekly budget 5% exhausted (spent 6%)", output)
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False])
        self.assertTrue(self.invocations()[2]["resume"])
        self.assertEqual(self.invocations()[0]["thread"], self.invocations()[2]["thread"])
        self.assertEqual(len(self.sandbox_invocations()), 2)

    def test_weekly_budget_after_review_stops_fix_rounds_and_later_angles(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12},
            {"mode": "complete", "reply": self.review_result(),
             "weekly_used_before": 12, "weekly_used_after": 15,
             "final_usage_after_completion": True}])
        self.start("weekly-between-phases", "--max-weekly", "5", "--fix-rounds", "2",
                   "--review-angles", "correctness,concurrency")
        self.wait("weekly-between-phases")
        state = self.state("weekly-between-phases")
        self.assertEqual(state["weekly_used_percent"], 5)
        self.assertEqual(state["auto_fix_stop"], "budget")
        self.assertFalse(state.get("rounds"))
        self.assertEqual([entry["angle"] for entry in state["reviews"]], ["correctness"])
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertEqual(state["review_result"]["verdict"], "defects")

    def test_weekly_budget_rejects_exhausted_say_and_review_until_budget_is_raised(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 15,
             "final_usage_after_completion": True},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 15, "weekly_used_after": 16,
             "final_usage_after_completion": True},
            {"mode": "complete", "reply": self.review_result(False),
             "weekly_used_before": 16, "weekly_used_after": 16.5}])
        self.start("weekly-raise", "--max-weekly", "5", "--review")
        self.wait("weekly-raise")
        self.assertEqual(len(self.invocations()), 1, "automatic review must respect the task budget")
        for args in (("say", "weekly-raise", "Continue"), ("review", "weekly-raise"),
                     ("say", "weekly-raise", "Continue", "--max-weekly", "5")):
            with self.subTest(args=args):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(args), cwd=self.repo,
                                        env=self.env, text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                for word in ("budget", "spent", "5", "--max-weekly"):
                    self.assertIn(word, result.stderr.lower())
        self.assertEqual(len(self.invocations()), 1)
        self.cli("say", "weekly-raise", "Continue with a larger task budget", "--max-weekly", "6")
        self.wait("weekly-raise")
        self.assertEqual(self.state("weekly-raise")["max_task_weekly_percent"], 6)
        self.assertEqual(self.state("weekly-raise")["weekly_used_percent"], 6)
        self.cli("review", "weekly-raise", "--max-weekly", "8")
        self.wait("weekly-raise")
        self.assertEqual(self.state("weekly-raise")["max_task_weekly_percent"], 8)
        self.assertEqual(self.state("weekly-raise")["weekly_used_percent"], 6.5)
        self.assertEqual(len(self.invocations()), 3)

    def test_weekly_budget_polls_rollout_with_rotation_disabled(self):
        (self.home / "codex-worker.json").write_text(json.dumps({"language": "en"}))
        self.account(self.home, "A", self.reset, used=30, runs=[
            {"mode": "hold", "weekly_used_before": 30, "weekly_used_after": 32.5,
             "weekly_update_after": 0.35},
            {"mode": "complete", "reply": self.work_result("partial")}])
        self.start("weekly-no-rotation", "--max-weekly", "2")
        output = self.wait("weekly-no-rotation", timeout=12)
        state = self.state("weekly-no-rotation")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertAlmostEqual(state["turn_stop"]["spent"], 2.5)
        self.assertIn("== turn stopped: weekly budget 2% exhausted (spent 2.5%)", output)
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual(self.invocations()[0]["thread"], self.invocations()[1]["thread"])
        self.assertEqual(self.invocations()[1]["prompt"],
                         self.worker().wrapup_prompt(state, state["turn_stop"], {"language": "en"}))
        self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        self.assertFalse(state["rotations"])

    def test_weekly_budget_headroom_warning_uses_configured_language_and_defaults_to_english(self):
        worker = self.worker()
        infos = [{"logged_in": True, "limits": {"secondary": {"minutes": 10080, "used": 96}}},
                 {"logged_in": True, "limits": {"secondary": {"minutes": 10080, "used": 96}}}]
        english = "budget 10%, account headroom to switch_at 6%: task may sleep until reset"
        russian = "бюджет 10%, запас до switch_at по аккаунтам 6%: задача может уснуть до сброса"
        for language, expected in (("en", english), ("ru", russian), (None, english)):
            with self.subTest(language=language):
                cfg = {"switch_at": 99}
                if language is not None:
                    cfg["language"] = language
                state = {"max_task_weekly_percent": 10, "weekly_used_percent": 0}
                worker.warn_weekly_headroom(state, cfg, infos)
                self.assertEqual(state["weekly_budget_warning"], expected)
                state["weekly_used_percent"] = 5
                worker.warn_weekly_headroom(state, cfg, infos)
                self.assertNotIn("weekly_budget_warning", state)

    def test_weekly_budget_warns_only_when_remaining_budget_exceeds_account_reserve(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=94, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 94, "weekly_used_after": 98},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 98, "weekly_used_after": 98}])
        self.account(self.other, "B", self.reset + 3600, used=98)
        self.start("weekly-reserve", "--max-weekly", "10")
        output = self.wait("weekly-reserve")
        warning = "бюджет 10%, запас до switch_at по аккаунтам 6%: задача может уснуть до сброса"
        self.assertEqual(self.state("weekly-reserve")["weekly_budget_warning"], warning)
        self.assertEqual(output.count(warning), 1)
        self.assertEqual(self.cli("status", "weekly-reserve").count(warning), 1)
        reads = [event for event in self.trace() if event.get("command") == "quota-snapshot"]
        self.assertEqual(len(reads), 2, "the warning reuses the account-selection snapshots")
        self.assertEqual(self.state("weekly-reserve")["weekly_used_percent"], 4)
        self.cli("say", "weekly-reserve", "Five percentage points remain", "--max-weekly", "9")
        output = self.wait("weekly-reserve")
        self.assertFalse(self.state("weekly-reserve").get("weekly_budget_warning"))
        self.assertNotIn("запас до switch_at", output)
        self.assertNotIn("запас до switch_at", self.cli("status", "weekly-reserve"))
        self.assertEqual(len([event for event in self.trace()
                              if event.get("command") == "quota-snapshot"]), 4)

    def test_weekly_budget_uses_config_default_and_flag_overrides_persist_for_the_task(self):
        self.configure(language="en", max_task_weekly_percent=5)
        (self.home / "codex-worker.json").write_text((self.home / "claude-accounts.json").read_text())
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 12, "weekly_used_after": 13},
            {"mode": "complete", "reply": self.review_result(False),
             "weekly_used_before": 13, "weekly_used_after": 14},
            {"mode": "complete", "reply": self.work_result()}])
        self.start("weekly-config")
        self.wait("weekly-config")
        self.assertEqual(self.state("weekly-config")["max_task_weekly_percent"], 5)
        self.cli("say", "weekly-config", "Raise the task budget", "--max-weekly", "9")
        self.wait("weekly-config")
        self.assertEqual(self.state("weekly-config")["max_task_weekly_percent"], 9)
        self.cli("review", "weekly-config")
        self.wait("weekly-config")
        self.assertEqual(self.state("weekly-config")["max_task_weekly_percent"], 9,
                         "a follow-up without the flag keeps the task's overridden budget")
        self.start("weekly-start-override", "--max-weekly", "11")
        self.wait("weekly-start-override")
        self.assertEqual(self.state("weekly-start-override")["max_task_weekly_percent"], 11)

    def test_weekly_budget_rejects_invalid_flags_and_settings_before_starting_a_turn(self):
        self.configure(language="en")
        for value in ("0", "-1", "100.1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(command="start", value=value):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "start", "weekly-invalid",
                                         "--brief", str(self.brief), "--repo", str(self.repo),
                                         "--max-weekly=" + value], cwd=self.repo, env=self.env,
                                        text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-weekly", result.stderr)
                self.assertFalse((self.home / "claude-tasks" / "weekly-invalid").exists())
        self.start("weekly-valid", "--max-weekly", "100")
        self.wait("weekly-valid")
        for command in ("say", "review"):
            for value in ("0", "-1", "101", "nan", "inf"):
                with self.subTest(command=command, value=value):
                    args = [command, "weekly-valid"] + (["Continue"] if command == "say" else [])
                    result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + args + ["--max-weekly=" + value],
                                            cwd=self.repo, env=self.env, text=True, capture_output=True,
                                            timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("max-weekly", result.stderr)
        for value in (0, -1, 101, True, "10"):
            with self.subTest(setting=value):
                self.configure(language="en", max_task_weekly_percent=value)
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "start", "weekly-invalid-setting",
                                         "--brief", str(self.brief), "--repo", str(self.repo)],
                                        cwd=self.repo, env=self.env, text=True,
                                        capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max_task_weekly_percent", result.stderr)
                self.assertFalse((self.home / "claude-tasks" / "weekly-invalid-setting").exists())
        self.assertEqual(len(self.invocations()), 1)

    def test_weekly_usage_accumulates_across_say_without_a_task_budget(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 10, "weekly_used_after": 12},
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 12, "weekly_used_after": 14}])
        self.start("weekly-unbounded")
        self.wait("weekly-unbounded")
        self.cli("say", "weekly-unbounded", "Continue the task without a weekly cap")
        self.wait("weekly-unbounded")
        state = self.state("weekly-unbounded")
        self.assertIsNone(state.get("max_task_weekly_percent"))
        self.assertEqual(state["weekly_used_percent"], 4)
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
