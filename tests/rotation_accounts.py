"""Account authentication, reset waits and monitor tests."""
import time
from types import SimpleNamespace
from unittest import mock

from rotation_support import IMPOSSIBLE_PID, RotationTestCase


class AccountAuthTests(RotationTestCase):
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
