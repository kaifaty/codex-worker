"""Active time budgets, wrap-up and stuck-command tests."""
import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rotation_support import PYTHON, SCRIPT, RotationTestCase


class TimeBudgetTests(RotationTestCase):
    def test_time_budget_kills_exec_group_wraps_same_thread_checks_and_skips_review(self):
        # The 90-minute phase still fits its wait; wrap-up includes process startup.
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.125"
        self.configure(language="en")
        self.configure_notify()
        result = self.work_result("partial", summary="Saved the working checkpoint.",
                                  remaining=["Complete the remaining integration."])
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "spawn_child": True},
            {"mode": "complete", "reply": result, "write_files": {"wrapped.txt": "saved\n"}}])
        self.start("time-cap", "--max-minutes", "90", "--review", "--fix-rounds", "2",
                   "--check", "test -f wrapped.txt && printf 'checked\\n' > cap-check")
        self.until(lambda: any(event.get("command") == "exec-child" for event in self.trace()))
        child = next(event["child_pid"] for event in self.trace() if event.get("command") == "exec-child")
        self.extra_pids.add(child)
        output = self.wait("time-cap", timeout=15)
        state = self.state("time-cap")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], result)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual((Path(state["worktree"]) / "cap-check").read_text(), "checked\n")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assert_output_schema(executions[1], "time-cap")
        worker = self.worker()
        self.assertEqual(executions[1]["prompt"],
                         worker.prompts.wrapup_prompt(state, state["turn_stop"], worker.config.accounts_config()))
        self.assertFalse(self.review_invocations())
        self.assertIn("== turn stopped: time budget 90 min exhausted", output)
        self.assertIn(result["summary"], output)
        self.assertIn(result["remaining"][0], output)
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(self.process_is_live(executions[0]["pid"]))
        self.assertTrue(any(event.get("command") == "signal" and
                            event.get("pid") == executions[0]["pid"] for event in self.trace()))
        self.assertEqual(self.notification("time-cap", "turn_finished")["outcome"], "budget_exhausted")
        self.assertEqual(self.notification("time-cap", "budget_exhausted")["outcome"], "budget_exhausted")
        telemetry = self.phase_telemetry_journal()
        self.assertEqual([(row["phase"], row["outcome"]) for row in telemetry],
                         [("work", "budget_exhausted"), ("wrapup", "completed"), ("check", "passed")])
        self.assertEqual(telemetry[1]["result_status"], "partial")
        self.assertEqual([row["round"] for row in telemetry], [0, 0, 0])
        report = json.loads(self.cli("report", "--slug", "time-cap", "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"budget_exhausted": 1})
        self.assertEqual(report["summary"]["result_status"], {"partial": 1})
        self.assertEqual(report["tasks"][0]["budget_stop"], "minutes")
        self.assertEqual(state["turn_stops"], [dict(state["turn_stop"], turn=1)])
        self.assertEqual(report["tasks"][0]["turn_stops"], state["turn_stops"])
        self.assertIsNone(report["tasks"][0]["max_task_weekly_percent"])
        self.assertEqual(next(row for row in self.telemetry_journal()
                              if row["phase"] == "turn")["budget_stop"], "minutes")

    def test_time_budget_interrupts_quota_refresh_with_a_locked_reset_journal(self):
        clock = self.enable_fake_clock()
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en", use_reset_credits=True)
        gate = self.root / "publish-rollout"
        result = self.work_result("partial", summary="Wrapped up while the reset journal remained locked.")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "spawn_child": True, "rollout_used": 99, "rollout_gate": str(gate)},
            {"mode": "complete", "reply": result}])
        with (self.home / "codex-worker-resets.jsonl").open("a+") as journal:
            with ThreadPoolExecutor(max_workers=1) as executor:
                def publish_when_ready():
                    self.until(lambda: any(event.get("command") == "rollout-waiting"
                                           for event in self.trace()))
                    # Preflight has finished. Acquire contention and publish in
                    # the same readiness callback, before waiting for the CLI.
                    fcntl.flock(journal, fcntl.LOCK_EX)
                    before = len(self.trace())
                    gate.touch()
                    return before

                ready = executor.submit(publish_when_ready)
                starter = subprocess.Popen(
                    [PYTHON, "-I", str(SCRIPT), "start", "cap-locked-journal", "--brief", str(self.brief),
                     "--repo", str(self.repo), "--max-minutes", "2"],
                    cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.extra_pids.add(starter.pid)
                try:
                    before = ready.result()
                    stdout, stderr = starter.communicate(timeout=5)
                    self.assertEqual(starter.returncode, 0, stdout + stderr)
                    self.until(lambda: sum(event.get("command") == "quota-snapshot"
                                           for event in self.trace()[before:]) >= 2)
                    self.until(lambda: any(event.get("command") == "exec-child" for event in self.trace()))
                    child = next(event["child_pid"] for event in self.trace()
                                 if event.get("command") == "exec-child")
                    self.extra_pids.add(child)
                    self.assertTrue(self.process_is_live(self.invocations()[0]["pid"]))
                    log = self.home / "claude-tasks" / "cap-locked-journal" / "stderr.log"
                    self.until(lambda: log.exists() and "reset journal: waiting for lock" in log.read_text())
                    clock.sleep(4.1)
                    output = self.wait("cap-locked-journal", timeout=10)
                    state = self.state("cap-locked-journal")
                    self.assertEqual(state["outcome"], "budget_exhausted")
                    self.assertEqual(state["wrapup"]["outcome"], "completed")
                    self.assertEqual(state["result"], result)
                    executions = self.invocations()
                    self.assertEqual(len(executions), 2)
                    self.assertTrue(executions[1]["resume"])
                    self.assertEqual(executions[1]["thread"], executions[0]["thread"])
                    worker = self.worker()
                    self.assertEqual(executions[1]["prompt"], worker.prompts.wrapup_prompt(
                        state, state["turn_stop"], worker.config.accounts_config()))
                    self.assertIn("== turn stopped: time budget 2 min exhausted", output)
                    self.assertIn(result["summary"], output)
                    self.assertFalse(self.process_is_live(executions[0]["pid"]))
                    self.until(lambda: not self.process_is_live(child))
                    self.assertFalse(self.consume_calls())
                finally:
                    if starter.poll() is None:
                        starter.terminate()
                    starter.communicate(timeout=5)

    def test_time_budget_interrupts_a_slow_account_rpc_after_a_usage_error(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en")
        gate = self.root / "release-quota-rpc"
        result = self.work_result("partial", summary="Wrapped up without waiting for the quota RPC.")
        fixture = self.account(self.home, "A", self.reset, runs=[
            {"mode": "usage_error", "limit_after": 0.05}, {"mode": "complete", "reply": result}])
        fixture["rate_limits_gate_after_exec"] = str(gate)
        (self.home / "fixture.json").write_text(json.dumps(fixture))
        self.start("cap-slow-quota-rpc", "--max-minutes", "2")
        self.until(lambda: any(event.get("command") == "quota-waiting" for event in self.trace()))
        blocked_rpc = next(event["pid"] for event in self.trace() if event.get("command") == "quota-waiting")
        self.assertTrue(self.process_is_live(blocked_rpc))
        output = self.wait("cap-slow-quota-rpc", timeout=10)
        state = self.state("cap-slow-quota-rpc")
        self.assertFalse(gate.exists(), "the deadline must cancel the RPC before its gate opens")
        self.assertFalse(self.process_is_live(blocked_rpc))
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], result)
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[1]["thread"], executions[0]["thread"])
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)
        self.assertIn(result["summary"], output)

    def test_time_budget_does_not_count_sleep_until_subscription_reset(self):
        clock = self.enable_fake_clock()
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "1"
        self.configure()
        reset = clock.time() + 4
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "usage_error", "limit_after": 0.1},
            {"mode": "complete", "delay": 0.6, "reply": "Finished after a long quota sleep"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("cap-reset-sleep", "--max-minutes", "2.5")
        output = self.wait("cap-reset-sleep", timeout=12, through_sleep=True)
        state = self.state("cap-reset-sleep")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual([entry["event"] for entry in state["rotations"]], ["sleep", "wake"])
        self.assertIn("Finished after a long quota sleep", output)
        self.assertNotIn("time budget", output)

    def test_reset_readiness_publishes_only_armed_rollout_deadlines(self):
        self.configure()
        reset = time.time() + 4
        # Without a usage-error event, the supervisor must cache the exhausted
        # rollout before it can request account quotas and decide to sleep.
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "limit"}, {"mode": "complete", "reply": "Resumed from the armed rollout"}])
        self.account(self.other, "B", self.reset, used=99)
        reset = self.reset_after_ready(
            lambda: self.start("armed-rollout"), reset, 4, after_exec=True)
        self.wait("armed-rollout", through_sleep=True, timeout=12)
        state = self.state("armed-rollout")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual([entry["event"] for entry in state["rotations"]], ["sleep", "wake"])
        self.assertAlmostEqual(state["rotations"][0]["until"], reset + 0.1, places=3)
        trace = self.trace()
        ready = next(index for index, event in enumerate(trace)
                     if event.get("command") == "quota-waiting" and event.get("source") == "exec")
        published = [(index, event["primary"]) for index, event in enumerate(trace)
                     if event.get("command") == "rollout-snapshot" and event["home"] == str(self.home)]
        self.assertTrue(any(primary["usedPercent"] == 99 for _, primary in published))
        self.assertTrue(all(index > ready and primary["resetsAt"] == reset
                            for index, primary in published), published)

    def test_time_budget_accumulates_exec_time_across_account_rotation(self):
        self.enable_fake_clock()
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en")
        # Each exec fits a fresh budget; together they must exceed the shared cap.
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error", "limit_after": 1.8}])
        gate = self.root / "start-second-delay"
        self.account(self.other, "B", self.reset, runs=[
            {"mode": "complete", "delay": 2.2, "delay_gate": str(gate),
             "reply": "This would finish if the budget restarted"},
            {"mode": "complete", "reply": self.work_result("partial")}])
        self.start("cap-rotation", "--max-minutes", "1.7")
        self.until(lambda: len(self.invocations()) == 2 and
                   self.state("cap-rotation")["thread"] == self.invocations()[1]["thread"])
        gate.touch()
        self.wait("cap-rotation", timeout=12)
        executions = self.invocations()
        self.assertEqual(self.state("cap-rotation")["outcome"], "budget_exhausted")
        self.assertEqual(len(executions), 3)
        self.assertEqual(executions[1]["home"], str(self.other))
        self.assertFalse(executions[1]["resume"])
        self.assertTrue(executions[2]["resume"])
        self.assertEqual(executions[2]["thread"], executions[1]["thread"])

    def test_wrap_up_has_its_own_ten_minute_cap_without_recursing(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.configure(language="en")
        command = {"command": "check-main", "exit_code": 2, "aggregated_output": "same main failure"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": [command] * 3}, {"mode": "hold", "spawn_child": True}])
        self.start("cap-wrap-timeout", "--check", "printf 'checked\\n' > checked")
        output = self.wait("cap-wrap-timeout", timeout=12)
        state = self.state("cap-wrap-timeout")
        self.assertEqual(state["outcome"], "stuck")
        self.assertEqual(state["wrapup"]["outcome"], "budget_exhausted")
        primary, wrapup = state["turn_stops"]
        self.assertEqual(primary, dict(state["turn_stop"], turn=1))
        self.assertEqual((state["turn_stop"]["outcome"], state["turn_stop"]["command"]),
                         ("stuck", command["command"]))
        self.assertEqual((wrapup["turn"], wrapup["phase"], wrapup["outcome"], wrapup["minutes"]),
                         (1, "wrapup", "budget_exhausted", 10))
        self.assertGreaterEqual(wrapup["at"], primary["at"])
        self.assertLessEqual(wrapup["at"], state["turn_finished"])
        self.assertEqual(len(self.invocations()), 2, "a timed-out wrap-up must not start another wrap-up")
        self.assertIsNone(state["result"])
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== turn stopped: repeated failure ×3: " + command["command"], output)
        child = next(event["child_pid"] for event in self.trace() if event.get("command") == "exec-child")
        self.extra_pids.add(child)
        self.until(lambda: not self.process_is_live(child))
        self.assertTrue(all(not self.process_is_live(event["pid"]) for event in self.invocations()))

    def test_repeated_failure_during_wrap_up_stops_without_another_wrap_up(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.configure()
        command = {"command": "check-wrap", "exit_code": 2, "aggregated_output": "same wrap failure"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "hold", "commands": [command] * 3}])
        self.start("cap-wrap-stuck", "--max-minutes", "10")
        output = self.wait("cap-wrap-stuck", timeout=12)
        state = self.state("cap-wrap-stuck")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["wrapup"]["outcome"], "stuck")
        primary, wrapup = state["turn_stops"]
        self.assertEqual(primary, dict(state["turn_stop"], turn=1))
        self.assertEqual((state["turn_stop"]["outcome"], state["turn_stop"]["minutes"]),
                         ("budget_exhausted", 10))
        self.assertEqual((wrapup["turn"], wrapup["phase"], wrapup["outcome"],
                          wrapup["command"], wrapup["output_tail"]),
                         (1, "wrapup", "stuck", command["command"], command["aggregated_output"]))
        self.assertIsNone(state["result"])
        self.assertEqual(len(self.invocations()), 2)
        self.assertIn("== turn stopped: time budget 10 min exhausted", output)

    def test_repeated_command_failure_uses_output_tail_and_survives_other_commands(self):
        self.configure(language="en")
        self.configure_notify()
        command = "python3 check_widget.py --empty-input"
        tail = "same error tail\n" + "x" * 2048
        failures = [{"command": command, "exit_code": 7, "aggregated_output": prefix + tail}
                    for prefix in ("first attempt\n", "second attempt\n", "third attempt\n")]
        events = [failures[0], {"command": "git status", "exit_code": 0}, failures[1],
                  {"command": "another-command", "exit_code": 9}, failures[2]]
        result = self.work_result("blocked", summary="Stopped after three identical failures.",
                                  remaining=["Resolve the fixture command failure."])
        empty_failure = {"command": command, "exit_code": 7, "aggregated_output": ""}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": events}, {"mode": "complete", "reply": result},
            {"mode": "hold", "commands": [empty_failure] * 3}, {"mode": "complete", "reply": result}])
        self.start("repeat-cap", "--review", "--check", "printf 'checked\\n' > checked")
        output = self.wait("repeat-cap", timeout=12)
        state = self.state("repeat-cap")
        executions = self.invocations()
        self.assertEqual(state["outcome"], "stuck")
        self.assertEqual(state["result"], result)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        prompt = executions[1]["prompt"]
        self.assertIn(command, prompt)
        worker = self.worker()
        self.assertEqual(prompt, worker.prompts.wrapup_prompt(state, state["turn_stop"], worker.config.accounts_config()))
        self.assertIn("== turn stopped: repeated failure ×3: " + command, output)
        self.assertIn(result["summary"], output)
        self.assertFalse(self.review_invocations())
        self.assertEqual(self.notification("repeat-cap", "stuck")["outcome"], "stuck")
        self.assertEqual(self.notification("repeat-cap", "turn_finished")["outcome"], "stuck")
        self.assertEqual(state["turn_stops"], [dict(state["turn_stop"], turn=1)])
        first_stop = state["turn_stops"][0]
        self.assertEqual(first_stop["output_tail"], tail[-300:])
        self.assertGreaterEqual(first_stop["at"], state["turn_started"])
        self.assertLessEqual(first_stop["at"], state["turn_finished"])
        self.cli("say", "repeat-cap", "Retry the command")
        self.wait("repeat-cap", timeout=12)
        state = self.state("repeat-cap")
        self.assertEqual(state["turn_stops"], [first_stop, dict(state["turn_stop"], turn=2)])
        self.assertEqual(state["turn_stops"][1]["output_tail"], "")
        report = json.loads(self.cli("report", "--slug", "repeat-cap", "--json"))
        self.assertEqual(report["tasks"][0]["turn_stops"], state["turn_stops"])

    def test_different_failures_success_and_other_event_types_do_not_trigger_stuck(self):
        self.configure()
        failure = {"command": "check-widget", "exit_code": 1, "aggregated_output": "error A"}
        cases = {
            "commands": [dict(failure, command="check-%s" % number) for number in range(3)],
            "output": [dict(failure, aggregated_output="error %s" % number) for number in (1, 2, 1, 2, 1)],
            "exit": [dict(failure, exit_code=number) for number in (1, 2, 1, 2, 1)],
            "success": [failure, failure, dict(failure, exit_code=0), failure, failure],
            "changed-between": [failure, dict(failure, aggregated_output="error B"), failure],
            "unfinished": [dict(failure, event_type="item.started") for _ in range(3)],
            "other-item": [dict(failure, item_type="other_execution") for _ in range(3)]}
        for name, commands in cases.items():
            with self.subTest(case=name):
                (self.home / "exec-count").unlink(missing_ok=True)
                before = len(self.invocations())
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "commands": commands, "reply": "No loop detected"}])
                slug = "repeat-distinct-" + name
                self.start(slug)
                output = self.wait(slug)
                self.assertEqual(self.state(slug)["outcome"], "completed")
                self.assertEqual(len(self.invocations()) - before, 1)
                self.assertIn("No loop detected", output)

    def test_empty_search_results_do_not_trigger_stuck(self):
        worker = self.worker()
        cases = [(command, 1, "", False) for command in (
            "pgrep -f fixture", "grep fixture", "egrep fixture", "fgrep fixture", "rg fixture",
            "git grep fixture", "/bin/zsh -lc 'pgrep -f fixture'", "/bin/sh -c 'git grep fixture'",
            "ps | grep fixture", "/bin/zsh -lc 'ps | grep fixture'",
            "LC_ALL=C pgrep -fl '^/[^ ]*/fixture( |$)'",
            "LC_ALL=C LABEL='two words' pgrep -x fixture", "env LC_ALL=C rg fixture",
            '/bin/zsh -lc "LC_ALL=C pgrep -fl \'^/[^ ]*/fixture( |$)\'"',
            '/bin/zsh -lc "env LC_ALL=C LABEL=\'two words\' rg fixture"',
            "ps | LC_ALL=C grep fixture", "ps | env LC_ALL=C rg fixture",
            "pgrep -fl x 2>&1", "pgrep x >&1", "pgrep x &>search.log",
            "rg foo 2>/dev/null", "ps aux | grep x 2>&1", '/bin/zsh -lc "rg foo 2>&1"')]
        cases += [("pgrep fixture", 1, " \t\n", False),
                  ("grep fixture", 1, "search failed", True),
                  ("pgrep fixture", 2, "", True),
                  ("false", 1, "", True),
                  ("env -i pgrep fixture", 1, "", True),
                  ("FOO=1 make", 1, "", True),
                  ("LC_ALL=C LABEL='two words'", 1, "", True),
                  ("env LC_ALL=C", 1, "", True),
                  ("grep fixture | false", 1, "", True),
                  ("pgrep fixture; false", 1, "", True),
                  ("pgrep fixture && false", 1, "", True),
                  ("pgrep fixture || false", 1, "", True),
                  ("pgrep x & sleep 1", 1, "", True),
                  ("grep x || true", 1, "", True),
                  ("pgrep fixture\nfalse", 1, "", True),
                  ("/bin/zsh -lc 'pgrep fixture; false'", 1, "", True)]
        for command, code, output, stuck in cases:
            with self.subTest(command=command, code=code, output=output):
                control = worker.turn_monitor.TurnControl(None, 3)
                event = {"type": "item.completed", "item": {
                    "type": "command_execution", "command": command,
                    "exit_code": code, "aggregated_output": output}}
                self.assertEqual([control.repeated(event) for _ in range(3)], [False, False, stuck])
                self.assertEqual(control.reason is not None, stuck)

    def test_repeat_limit_and_russian_wrap_prompt_follow_settings(self):
        self.configure(repeat_limit=2)
        command = {"command": "pytest tests/widget.py", "exit_code": 2,
                   "aggregated_output": "missing fixture"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": [command, command]},
            {"mode": "complete", "reply": self.work_result("blocked")}])
        self.start("repeat-setting")
        output = self.wait("repeat-setting", timeout=12)
        state = self.state("repeat-setting")
        self.assertEqual(state["outcome"], "stuck")
        self.assertIn("== turn stopped: repeated failure ×2: " + command["command"], output)
        prompt = self.invocations()[-1]["prompt"]
        self.assertIn(command["command"], prompt)
        worker = self.worker()
        self.assertEqual(prompt, worker.prompts.wrapup_prompt(state, state["turn_stop"], worker.config.accounts_config()))

    def test_max_minutes_uses_config_default_and_say_override_for_one_turn(self):
        clock = self.enable_fake_clock()
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.configure(language="en", max_turn_minutes=18)
        legacy = self.home / "claude-accounts.json"
        (self.home / "codex-worker.json").write_text(legacy.read_text())
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "complete", "reply": self.work_result("partial")},
            # Longer than the default 18-minute budget, shorter than the override.
            {"mode": "complete", "delay": 4.8, "reply": "Say override completed"},
            {"mode": "hold"}, {"mode": "complete", "reply": self.work_result("partial")}])
        self.start("cap-config")
        self.until(lambda: len(self.invocations()) == 1 and
                   self.state("cap-config")["thread"] == self.invocations()[0]["thread"])
        clock.sleep(3.7)
        output = self.wait("cap-config", timeout=12)
        self.assertEqual(self.state("cap-config")["outcome"], "budget_exhausted")
        self.assertIn("== turn stopped: time budget 18 min exhausted", output)
        self.cli("say", "cap-config", "Use a larger budget", "--max-minutes", "48")
        self.assertIn("Say override completed", self.wait("cap-config", timeout=12))
        self.assertEqual(self.state("cap-config")["outcome"], "completed")
        self.cli("say", "cap-config", "Use the configured default again")
        self.until(lambda: len(self.invocations()) == 4 and
                   self.state("cap-config").get("codex_pid") == self.invocations()[3]["pid"])
        clock.sleep(3.7)
        self.wait("cap-config", timeout=12)
        self.assertEqual(self.state("cap-config")["outcome"], "budget_exhausted")
        self.assertEqual(len(self.invocations()), 5)

    def test_start_max_minutes_does_not_limit_later_say_without_a_default(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold"}, {"mode": "complete", "reply": self.work_result("partial")},
            # The unbounded follow-up must outlast the initial 12-minute cap.
            {"mode": "complete", "delay": 4.8, "reply": "The later turn has no budget"}])
        self.start("cap-no-default", "--max-minutes", "12")
        self.wait("cap-no-default", timeout=12)
        self.cli("say", "cap-no-default", "Continue without a time limit")
        output = self.wait("cap-no-default", timeout=12)
        self.assertEqual(self.state("cap-no-default")["outcome"], "completed")
        self.assertIn("The later turn has no budget", output)
        self.assertEqual(len(self.invocations()), 3)

    def test_max_minutes_rejects_nonpositive_and_nonfinite_values_before_start_or_say(self):
        self.configure()
        for value in ("0", "-1", "nan", "inf", "-inf", "invalid"):
            with self.subTest(command="start", value=value):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "start", "cap-invalid",
                                         "--brief", str(self.brief), "--repo", str(self.repo),
                                         "--max-minutes=" + value], cwd=self.repo, env=self.env,
                                        text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-minutes", result.stderr)
                self.assertFalse((self.home / "claude-tasks" / "cap-invalid").exists())
        self.assertFalse(self.invocations())
        self.start("cap-valid-say")
        self.wait("cap-valid-say")
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(command="say", value=value):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "say", "cap-valid-say", "Continue",
                                         "--max-minutes=" + value], cwd=self.repo, env=self.env,
                                        text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("max-minutes", result.stderr)
        self.assertEqual(len(self.invocations()), 1)

    def test_review_and_scout_default_to_their_own_thirty_minute_budget(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.configure(language="en", max_turn_minutes=1000)
        work = self.work_result()
        checkpoint = self.review_result()
        checkpoint["summary"] = "Review incomplete: found two defects before the time budget expired."
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": work},
            {"mode": "hold"}, {"mode": "complete", "reply": checkpoint},
            {"mode": "hold"}, {"mode": "complete", "reply": "Scouting checkpoint"}])
        self.start("cap-review-default", "--review")
        review_output = self.wait("cap-review-default", timeout=12)
        state = self.state("cap-review-default")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], work, "review wrap-up must preserve the work result")
        self.assertEqual(state["wrapup"]["result"], checkpoint)
        self.assertEqual(state["reviews"][-1]["result"], checkpoint)
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        self.assertTrue(executions[1]["review"])
        self.assertTrue(executions[2]["resume"])
        self.assertEqual(executions[2]["thread"], executions[1]["thread"])
        self.assert_output_schema(executions[2], "cap-review-default", review=True)
        self.assertIn(checkpoint["summary"], review_output)
        self.assertIn("== turn stopped: time budget 30 min exhausted", review_output)
        self.cli("scout", "cap-scout-default", "Inspect the current configuration")
        scout_output = self.wait("cap-scout-default", timeout=12)
        self.assertEqual(self.state("cap-scout-default")["outcome"], "budget_exhausted")
        executions = self.invocations()
        self.assertEqual(len(executions), 5)
        self.assertEqual(executions[4]["thread"], executions[3]["thread"])
        self.assert_scout_read_only(executions[4])
        self.assertIn("Scouting checkpoint", scout_output)
        self.assertIn("== turn stopped: time budget 30 min exhausted", scout_output)

    def test_review_budget_before_rotated_thread_started_does_not_resume_previous_review(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en", max_review_minutes=2)
        work = self.work_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": work},
            {"mode": "usage_error", "limit_after": 0.05}])
        gate = self.root / "release-rotated-review-before-thread"
        self.account(self.other, "B", self.reset, runs=[
            {"mode": "hold", "rollout_gate": str(gate)},
            {"mode": "complete", "reply": self.review_result()}])
        self.start("cap-review-before-thread", "--review")
        self.until(lambda: any(event.get("command") == "rollout-waiting"
                               and event["home"] == str(self.other) for event in self.trace()))
        output = self.wait("cap-review-before-thread", timeout=12)
        state = self.state("cap-review-before-thread")
        executions = self.invocations()
        self.assertFalse(gate.exists(), "the replacement review must never publish thread.started")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["wrapup"]["outcome"], "not_run")
        self.assertIsNone(state["wrapup"]["result"])
        self.assertEqual(state["result"], work)
        self.assertEqual(state["review_account"], "second")
        self.assertEqual(len(executions), 3, "no wrap-up may resume a previous account's review")
        self.assertEqual([event["review"] for event in executions], [False, True, True])
        self.assertFalse(any(event["resume"] for event in executions))
        self.assertEqual(executions[-1]["home"], str(self.other))
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(state["reviews"][0]["thread"], executions[1]["thread"])
        self.assertNotEqual(state["reviews"][0]["thread"], executions[2]["thread"])
        self.assertTrue(all(not self.process_is_live(event["pid"]) for event in executions))
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)

    def test_wrap_up_prompts_use_the_schema_for_work_or_review_in_both_languages(self):
        worker = self.worker()
        reasons = [
            {"outcome": "budget_exhausted", "minutes": 30},
            {"outcome": "stuck", "command": "fixture-check", "count": 3,
             "exit_code": 7, "output_sha256": "a" * 64}]
        for language in ("en", "ru"):
            for reason in reasons:
                with self.subTest(language=language, reason=reason["outcome"]):
                    cfg = {"language": language}
                    work = worker.prompts.wrapup_prompt({"kind": "work"}, reason, cfg)
                    review = worker.prompts.wrapup_prompt({"kind": "review"}, reason, cfg)
                    self.assertEqual(worker.prompts.wrapup_prompt({}, reason, cfg), work)
                    self.assertNotEqual(work, review)
                    if reason["outcome"] == "stuck":
                        for key in ("command", "count", "exit_code", "output_sha256"):
                            for prompt in (work, review):
                                self.assertIn(str(reason[key]), prompt)

    def test_configured_review_budget_is_independent_of_work_and_applies_to_scout(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en", max_turn_minutes=0.2, max_review_minutes=3)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            # Both exceed the work cap while remaining inside the review budget.
            {"mode": "complete", "delay": 1.6, "reply": self.review_result(False)},
            {"mode": "complete", "delay": 1.6, "reply": "Longer scouting completed"}])
        self.start("cap-review-independent", "--max-minutes", "0.6", "--review")
        self.wait("cap-review-independent", timeout=12)
        self.assertEqual(self.state("cap-review-independent")["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        self.cli("scout", "cap-scout-independent", "Inspect configuration independently")
        output = self.wait("cap-scout-independent", timeout=12)
        self.assertEqual(self.state("cap-scout-independent")["outcome"], "completed")
        self.assertIn("Longer scouting completed", output)
        self.assertEqual(len(self.invocations()), 3)

    def test_cap_notifications_obey_notify_events_filter(self):
        self.configure()
        self.configure_notify(events=["turn_finished"])
        command = {"command": "check-loop", "exit_code": 1, "aggregated_output": "same failure"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "commands": [command] * 3},
            {"mode": "complete", "reply": self.work_result("blocked")}])
        self.start("cap-notify-filter")
        self.wait("cap-notify-filter", timeout=12)
        self.assertEqual(self.notification("cap-notify-filter", "turn_finished")["outcome"], "stuck")
        self.assertEqual([event["event"] for event in self.notifications("cap-notify-filter")], ["turn_finished"])

    def test_cap_settings_validate_values_and_keep_safe_defaults(self):
        worker = self.worker()
        config = worker.config.accounts_config()
        self.assertIsNone(config["max_turn_minutes"])
        self.assertEqual(config["max_review_minutes"], 30)
        self.assertEqual(config["repeat_limit"], 3)
        path = self.home / "codex-worker.json"
        invalid = {"max_turn_minutes": (0, -1, True, "10", float("inf"), float("nan")),
                   "max_review_minutes": (None, 0, -1, False, "30", float("inf")),
                   "repeat_limit": (0, -1, True, 1.5, "3")}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    path.write_text(json.dumps({key: value}))
                    with self.assertRaises(SystemExit) as raised:
                        worker.config.accounts_config()
                    self.assertIn(key, str(raised.exception))

    def test_budget_during_automatic_fix_runs_check_then_stops_the_cycle(self):
        # A real wrap-up can take longer than the old half-second startup budget.
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        self.configure(language="en")
        result = self.work_result("partial", summary="Saved the bounded correction.")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "hold"},
            {"mode": "complete", "delay": 0.6, "reply": result}])
        self.start("cap-during-fix", "--max-minutes", "10", "--review", "--fix-rounds", "2",
                   "--check", "printf 'checked\\n' >> check-runs")
        output = self.wait("cap-during-fix", timeout=12)
        state = self.state("cap-during-fix")
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual(state["result"], result)
        self.assertEqual(len(state["checks"]), 2)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual(len(self.invocations()), 4)
        self.assertIn(result["summary"], output)
