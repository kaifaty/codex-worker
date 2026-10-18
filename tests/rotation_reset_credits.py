"""Free reset credits and reset journal tests."""
import datetime
import json
import os
import subprocess
import time
import uuid
from unittest import mock

from rotation_support import PYTHON, SCRIPT, RotationTestCase


class ResetCreditTests(RotationTestCase):
    def reset_credits(self, home, *credits, **changes):
        path = home / "fixture.json"
        value = json.loads(path.read_text())
        value["rateLimitResetCredits"] = {
            "availableCount": sum(credit.get("status") == "available" for credit in credits),
            "credits": list(credits)}
        value.update(changes)
        path.write_text(json.dumps(value))

    def reset_credit(self, credit_id, expires, **changes):
        credit = {"id": credit_id, "resetType": "codexRateLimits", "status": "available",
                  "grantedAt": int(time.time()) - 86400, "expiresAt": expires,
                  "title": "Full reset", "description": "A free full reset from the fixture."}
        credit.update(changes)
        return credit

    def reset_journal(self):
        path = self.home / "codex-worker-resets.jsonl"
        entries = []
        for line in path.read_text().splitlines() if path.exists() else []:
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
        return entries

    def test_reset_credit_settings_default_to_manual_use_and_twelve_hours(self):
        self.configure()
        cfg = self.worker().accounts_config()
        self.assertIs(cfg["use_reset_credits"], False)
        self.assertEqual(cfg["reset_credit_min_hours"], 12)

    def test_free_reset_uses_earliest_expiry_across_accounts_and_hands_off_without_sleep(self):
        self.configure(use_reset_credits=True)
        self.configure_notify()
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, runs=[{
            "mode": "usage_error", "messages": ["Незавершённая работа перед бесплатным сбросом"]}])
        self.account(self.other, "B", reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_later", reset + 10 * 86400))
        credit = self.reset_credit("RateLimitResetCredit_earliest", reset + 86400)
        self.reset_credits(self.other, credit,
                           self.reset_credit("RateLimitResetCredit_last", reset + 20 * 86400))
        self.start("free-reset-switch")
        output = self.wait("free-reset-switch")
        prompt = self.assert_rotation("free-reset-switch", output)
        self.assertIn("Незавершённая работа перед бесплатным сбросом", prompt)
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["home"], str(self.other))
        self.assertEqual(calls[0]["params"]["creditId"], credit["id"])
        key = calls[0]["params"]["idempotencyKey"]
        self.assertEqual(uuid.UUID(key).version, 4)
        attempts = [entry for entry in calls[0]["journal_before"] if entry["event"] == "attempt"]
        self.assertEqual(len(attempts), 1, "persist the request before spending a reset")
        self.assertEqual((attempts[0]["slug"], attempts[0]["account"]), ("free-reset-switch", "second"))
        self.assertEqual((attempts[0]["creditId"], attempts[0]["idempotencyKey"]), (credit["id"], key))
        self.assertIsInstance(attempts[0]["at"], (int, float))
        self.assertEqual([entry["outcome"] for entry in self.reset_journal() if entry["event"] == "result"],
                         ["success"])
        state = self.state("free-reset-switch")
        resets = [entry for entry in state["rotations"] if entry.get("event") == "reset_credit"]
        self.assertEqual(len(resets), 1)
        self.assertEqual((resets[0]["account"], resets[0]["credit_id"], resets[0]["expires_at"]),
                         ("second", credit["id"], credit["expiresAt"]))
        self.assertFalse(any(entry.get("event") == "sleep" for entry in state["rotations"]))
        self.assertIn("== used a free limit reset on second (" + credit["id"] + "), expires ", output)
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(self.notification("free-reset-switch", "reset_credit_used")["account"], "second")

    def test_free_reset_on_current_account_resumes_its_existing_thread(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, runs=[
            {"mode": "usage_error"}, {"mode": "complete", "reply": "Бесплатный сброс помог"}])
        self.account(self.other, "B", reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_current", reset + 86400))
        self.start("free-reset-resume")
        output = self.wait("free-reset-resume")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertTrue(executions[1]["resume"])
        self.assertEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertEqual(executions[1]["prompt"],
                         "Лимит подписки сбросился. Продолжай с того места, где остановился.")
        self.assertIn("Бесплатный сброс помог", output)
        self.assertEqual(len(self.consume_calls()), 1)
        self.assertFalse(any(entry.get("event") == "sleep"
                             for entry in self.state("free-reset-resume")["rotations"]))

    def test_automatic_reset_rechecks_quota_under_lock_and_keeps_the_credit_if_available(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        credit = self.reset_credit("RateLimitResetCredit_keep_after_fresh_read", reset + 86400)
        self.reset_credits(self.home, credit, clear_quota_when_reset_locked=True)
        self.start("fresh-quota-before-consume")
        output = self.wait("fresh-quota-before-consume")
        self.assertEqual(self.state("fresh-quota-before-consume")["outcome"], "completed")
        self.assertIn("Результат A", output)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.consume_calls())
        self.assertFalse(self.reset_journal())
        snapshots = [event["primary"]["usedPercent"] for event in self.trace()
                     if event.get("command") == "quota-snapshot" and event["home"] == str(self.home)]
        self.assertIn(99, snapshots)
        self.assertIn(0, snapshots)
        self.assertLess(snapshots.index(99), snapshots.index(0))
        fixture = json.loads((self.home / "fixture.json").read_text())
        self.assertEqual(fixture["rateLimitResetCredits"]["credits"][0]["status"], "available")
        self.assertFalse(any(entry.get("event") == "sleep"
                             for entry in self.state("fresh-quota-before-consume")["rotations"]))

    def test_disabled_free_resets_keep_the_original_sleep(self):
        self.configure(use_reset_credits=False)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_disabled", reset + 86400))
        self.start("free-reset-disabled")
        self.assert_sleeping("free-reset-disabled", self.wait("free-reset-disabled"), reset)
        self.assertFalse(self.consume_calls())
        self.assertFalse(self.invocations())

    def test_automatic_reset_retries_an_unknown_attempt_created_after_credit_selection(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 7 * 86400
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        pending = self.reset_credit("RateLimitResetCredit_concurrent_pending", reset + 2 * 86400)
        spare = self.reset_credit("RateLimitResetCredit_selected_spare", reset + 86400)
        self.reset_credits(self.home, pending, spare)
        key = str(uuid.uuid4())
        intent = {"slug": "concurrent-task", "account": "main", "creditId": pending["id"],
                  "idempotencyKey": key, "at": time.time()}
        journal = [dict(intent, event="attempt"), dict(intent, event="result", outcome="unknown")]
        worker = self.worker()
        cfg = worker.accounts_config()
        st = {"slug": "concurrent-pending-reset", "account": "main", "rotations": []}
        (worker.TASKS / st["slug"]).mkdir(parents=True)
        reset_credit_action = worker.reset_credit_action
        selected = []

        def add_concurrent_attempt(account, credit_id=None, *args, **kwargs):
            if credit_id is not None:
                selected.append(credit_id)
                (self.home / "codex-worker-resets.jsonl").write_text(
                    "".join(json.dumps(entry) + "\n" for entry in journal))
            return reset_credit_action(account, credit_id, *args, **kwargs)

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(worker, "reset_credit_action", side_effect=add_concurrent_attempt):
            infos = worker.snapshots(cfg, prepare=True)
            refreshed = worker.try_reset_credit(st, infos, cfg, lambda: False)
        self.assertEqual(selected, [spare["id"]], "inject the pending attempt after automatic selection")
        self.assertIsNotNone(refreshed)
        self.assertEqual(worker.available(refreshed, cfg)["name"], "main")
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"], {"creditId": pending["id"], "idempotencyKey": key})
        self.assertEqual(st["rotations"][0]["credit_id"], pending["id"])
        self.assertEqual(st["rotations"][0]["outcome"], "success")
        fixture = json.loads((self.home / "fixture.json").read_text())
        credits = {credit["id"]: credit for credit in fixture["rateLimitResetCredits"]["credits"]}
        self.assertEqual(credits[pending["id"]]["status"], "consumed")
        self.assertEqual(credits[spare["id"]]["status"], "available")

    def test_near_natural_reset_keeps_the_credit_and_sleeps(self):
        self.configure(use_reset_credits=True, reset_credit_min_hours=12)
        self.account(self.home, "A", self.reset, used=99)
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_keep", self.reset + 86400))
        self.start("free-reset-too-soon")
        self.assert_sleeping("free-reset-too-soon", self.wait("free-reset-too-soon"), self.reset)
        self.assertFalse(self.consume_calls())

    def test_reset_credit_error_or_unchanged_quota_sleeps_after_one_attempt(self):
        for slug, changes in (("free-reset-rpc-error", {"consume_error": True}),
                              ("free-reset-no-quota", {"consume_no_reset": True})):
            with self.subTest(slug=slug):
                before = len(self.consume_calls())
                self.configure(use_reset_credits=True)
                reset = int(time.time()) + 30 * 3600
                self.account(self.home, "A", reset, used=99)
                self.account(self.other, "B", reset + 3600, used=99)
                self.reset_credits(self.home, self.reset_credit("RateLimitResetCredit_" + slug, reset + 86400),
                                   self.reset_credit("RateLimitResetCredit_spare_" + slug, reset + 2 * 86400),
                                   **changes)
                self.start(slug)
                self.assert_sleeping(slug, self.wait(slug), reset)
                self.assertEqual(len(self.consume_calls()) - before, 1, "one reset attempt per sleep episode")
                self.cli("stop", slug, timeout=20)
                self.wait(slug)

    def test_reset_credit_timeout_retries_with_the_original_idempotency_key(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        credit = self.reset_credit("RateLimitResetCredit_unknown", self.reset + 86400)
        self.reset_credits(self.home, credit, consume_timeout=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        first = self.consume_calls()
        self.assertEqual(len(first), 1)
        self.assertTrue(any(entry.get("outcome") == "unknown" for entry in self.reset_journal()))
        path = self.home / "fixture.json"
        fixture = json.loads(path.read_text())
        fixture["consume_timeout"] = False
        path.write_text(json.dumps(fixture))
        self.cli("reset-credit", "main", "--yes")
        calls = self.consume_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["params"], calls[1]["params"])
        self.assertTrue(calls[1]["params"]["idempotencyKey"])
        results = [entry["outcome"] for entry in self.reset_journal() if entry["event"] == "result"]
        self.assertEqual(results, ["unknown", "success"])

    def test_reset_credit_rejects_another_explicit_credit_while_an_attempt_is_unknown(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        pending = self.reset_credit("RateLimitResetCredit_pending", self.reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_spare", self.reset + 2 * 86400)
        self.reset_credits(self.home, pending, spare)
        intent = {"slug": "interrupted-task", "account": "main", "creditId": pending["id"],
                  "idempotencyKey": str(uuid.uuid4()), "at": time.time()}
        journal = [dict(intent, event="attempt"), dict(intent, event="result", outcome="unknown")]
        (self.home / "codex-worker-resets.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in journal))
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--credit", spare["id"], "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"credit {pending['id']} has a pending attempt with an unknown outcome; "
                      f"resolve it first (reset-credit main --credit {pending['id']} "
                      "--yes retries it with the same key)", result.stdout + result.stderr)
        self.assertFalse(self.consume_calls())
        self.assertEqual(self.reset_journal(), journal)
        fixture = json.loads((self.home / "fixture.json").read_text())
        self.assertTrue(all(credit["status"] == "available" for credit in fixture["rateLimitResetCredits"]["credits"]))

    def test_reset_credit_explicit_pending_credit_retries_with_the_original_key(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        pending = self.reset_credit("RateLimitResetCredit_pending", self.reset + 2 * 86400)
        spare = self.reset_credit("RateLimitResetCredit_spare", self.reset + 86400)
        self.reset_credits(self.home, pending, spare)
        key = str(uuid.uuid4())
        intent = {"slug": "interrupted-task", "account": "main", "creditId": pending["id"],
                  "idempotencyKey": key, "at": time.time()}
        journal = [dict(intent, event="attempt"), dict(intent, event="result", outcome="unknown")]
        (self.home / "codex-worker-resets.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in journal))
        self.cli("reset-credit", "main", "--credit", pending["id"], "--yes")
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"], {"creditId": pending["id"], "idempotencyKey": key})
        results = [entry["outcome"] for entry in self.reset_journal() if entry["event"] == "result"]
        self.assertEqual(results, ["unknown", "success"])
        fixture = json.loads((self.home / "fixture.json").read_text())
        credits = {credit["id"]: credit for credit in fixture["rateLimitResetCredits"]["credits"]}
        self.assertEqual(credits[pending["id"]]["status"], "consumed")
        self.assertEqual(credits[spare["id"]]["status"], "available")

    def test_reset_credit_reconciles_an_applied_timeout_without_spending_the_next_credit(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        first = self.reset_credit("RateLimitResetCredit_applied", self.reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_spare", self.reset + 2 * 86400)
        self.reset_credits(self.home, first, spare, consume_timeout_applied=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        self.assertEqual(len(self.consume_calls()), 1)
        self.assertEqual(self.consume_calls()[0]["params"]["creditId"], first["id"])
        preview = self.cli("reset-credit", "main")
        self.assertTrue(preview)
        self.assertEqual(len(self.consume_calls()), 1, "a preview cannot retry or spend another reset")
        before = len(self.trace())
        output = self.cli("reset-credit", "main", "--yes")
        self.assertEqual(len(self.consume_calls()), 1, "resolve the unfinished reset before selecting the spare")
        results = [entry for entry in self.reset_journal()
                   if entry.get("event") == "result" and entry.get("creditId") == first["id"]]
        self.assertEqual(results[-1]["outcome"], "consumed (inferred)")
        fixture = json.loads((self.home / "fixture.json").read_text())
        credits = {credit["id"]: credit for credit in fixture["rateLimitResetCredits"]["credits"]}
        self.assertEqual(credits[first["id"]]["status"], "consumed")
        self.assertEqual(credits[spare["id"]]["status"], "available")
        self.assertIn("WEEKLY %", output)
        self.assertIn("main", output)
        self.assertTrue(any(event.get("command") == "quota-snapshot" and event["primary"]["usedPercent"] == 0
                            for event in self.trace()[before:]), "print fresh limits after reconciliation")

    def test_automatic_run_reconciles_an_applied_timeout_before_executing_available_quota(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        first = self.reset_credit("RateLimitResetCredit_auto_applied", reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_auto_spare", reset + 2 * 86400)
        self.reset_credits(self.home, first, spare, consume_timeout_applied=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        self.start("reconcile-before-exec")
        output = self.wait("reconcile-before-exec")
        self.assertEqual(self.state("reconcile-before-exec")["outcome"], "completed")
        self.assertIn("Результат A", output)
        self.assertEqual(len(self.consume_calls()), 1)
        executions = self.invocations()
        self.assertEqual(len(executions), 1)
        inferred = [entry for entry in executions[0]["journal_before"]
                    if entry.get("creditId") == first["id"] and entry.get("event") == "result"
                    and entry.get("outcome") == "consumed (inferred)"]
        self.assertEqual(len(inferred), 1, "resolve the old request even when the account already has quota")
        fixture = json.loads((self.home / "fixture.json").read_text())
        spare_status = next(credit["status"] for credit in fixture["rateLimitResetCredits"]["credits"]
                            if credit["id"] == spare["id"])
        self.assertEqual(spare_status, "available")

    def test_automatic_inferred_reset_with_exhausted_quota_sleeps_without_spending_a_spare(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        first = self.reset_credit("RateLimitResetCredit_inferred_no_quota", reset + 86400)
        spare = self.reset_credit("RateLimitResetCredit_unused_spare", reset + 2 * 86400)
        self.reset_credits(self.home, first, spare, consume_timeout_applied=True, consume_no_reset=True)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)
        self.start("inferred-reset-no-quota")
        output = self.wait("inferred-reset-no-quota")
        self.assert_sleeping("inferred-reset-no-quota", output, reset)
        self.assertEqual(len(self.consume_calls()), 1, "inferring the first reset uses the one action before sleep")
        self.assertEqual(self.consume_calls()[0]["params"]["creditId"], first["id"])
        inferred = [entry for entry in self.reset_journal()
                    if entry.get("creditId") == first["id"] and entry.get("event") == "result"
                    and entry.get("outcome") == "consumed (inferred)"]
        self.assertEqual(len(inferred), 1)
        fixture = json.loads((self.home / "fixture.json").read_text())
        credits = {credit["id"]: credit for credit in fixture["rateLimitResetCredits"]["credits"]}
        self.assertEqual(credits[first["id"]]["status"], "consumed")
        self.assertEqual(credits[spare["id"]]["status"], "available")
        self.assertEqual(fixture["primary"]["usedPercent"], 99)
        self.assertFalse(self.invocations())

    def test_automatic_pending_reset_reuses_its_key_before_considering_an_earlier_spare(self):
        self.configure(use_reset_credits=True)
        reset = int(time.time()) + 30 * 3600
        self.account(self.home, "A", reset, used=99)
        self.account(self.other, "B", reset + 3600, used=99)
        pending = self.reset_credit("RateLimitResetCredit_pending", reset + 10 * 86400)
        spare = self.reset_credit("RateLimitResetCredit_earlier_spare", reset + 86400)
        self.reset_credits(self.home, pending, spare, consume_timeout=True)
        key = str(uuid.uuid4())
        intent = {"slug": "interrupted-task", "account": "main", "creditId": pending["id"],
                  "idempotencyKey": key, "at": time.time()}
        journal = [dict(intent, event="attempt"), dict(intent, event="result", outcome="unknown")]
        (self.home / "codex-worker-resets.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in journal))
        preview = self.cli("reset-credit", "main")
        self.assertIn(pending["id"], preview)
        self.assertFalse(self.consume_calls(), "previewing an unfinished available reset cannot retry it")
        self.assertEqual(self.reset_journal(), journal)
        self.start("reconcile-pending-reset")
        output = self.wait("reconcile-pending-reset")
        self.assert_sleeping("reconcile-pending-reset", output, reset)
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1, "reconciling a pending request consumes the one attempt before sleep")
        self.assertEqual(calls[0]["params"], {"creditId": pending["id"], "idempotencyKey": key})
        self.assertFalse(self.invocations())
        fixture = json.loads((self.home / "fixture.json").read_text())
        self.assertTrue(all(credit["status"] == "available" for credit in fixture["rateLimitResetCredits"]["credits"]))

    def test_reset_journal_unterminated_or_truncated_tail_preserves_the_next_attempt(self):
        self.configure()
        journal = self.home / "codex-worker-resets.jsonl"
        old_attempt = {"event": "result", "outcome": "success", "slug": "old-task", "account": "main", "at": time.time(),
                       "creditId": "RateLimitResetCredit_old", "idempotencyKey": str(uuid.uuid4())}
        tails = {"unterminated": json.dumps(old_attempt),
                 "truncated": json.dumps(old_attempt) + '\n{"event":"attempt","creditId":"partial'}
        for name, tail in tails.items():
            with self.subTest(tail=name):
                before = len(self.consume_calls())
                journal.write_text(tail)
                self.account(self.home, "A", self.reset, used=99)
                credit = self.reset_credit("RateLimitResetCredit_tail_" + name, self.reset + 86400)
                self.reset_credits(self.home, credit, consume_timeout=True)
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "reset-credit", "main", "--yes"],
                                        cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("timed out", result.stdout + result.stderr)
                calls = self.consume_calls()[before:]
                self.assertEqual(len(calls), 1)
                attempts = [entry for entry in calls[0]["journal_before"]
                            if entry.get("event") == "attempt" and entry.get("creditId") == credit["id"]]
                self.assertEqual(len(attempts), 1, "a partial final line must not swallow the new durable intent")
                self.assertEqual(attempts[0]["idempotencyKey"], calls[0]["params"]["idempotencyKey"])
                self.assertIn(old_attempt, self.reset_journal(), "preserve the preceding valid record")
                fixture_path = self.home / "fixture.json"
                fixture = json.loads(fixture_path.read_text())
                fixture["consume_timeout"] = False
                fixture_path.write_text(json.dumps(fixture))
                self.cli("reset-credit", "main", "--yes")
                calls = self.consume_calls()[before:]
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0]["params"], calls[1]["params"])
                attempts = [entry for entry in self.reset_journal()
                            if entry.get("event") == "attempt" and entry.get("creditId") == credit["id"]]
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0]["idempotencyKey"], attempts[1]["idempotencyKey"])

    def test_reset_credit_reuses_a_persisted_attempt_without_a_result_after_a_crash(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        credit = self.reset_credit("RateLimitResetCredit_crashed", self.reset + 86400)
        self.reset_credits(self.home, credit)
        key = str(uuid.uuid4())
        attempt = {"event": "attempt", "slug": "interrupted-task", "account": "main", "at": time.time(),
                   "creditId": credit["id"], "idempotencyKey": key}
        (self.home / "codex-worker-resets.jsonl").write_text(json.dumps(attempt) + "\n")
        self.cli("reset-credit", "main", "--yes")
        calls = self.consume_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"], {"creditId": credit["id"], "idempotencyKey": key})
        self.assertEqual(len([entry for entry in calls[0]["journal_before"] if entry["event"] == "attempt"]), 2)
        results = [entry for entry in self.reset_journal() if entry["event"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["outcome"], results[0]["idempotencyKey"]), ("success", key))

    def test_reset_credit_can_be_cancelled_while_another_process_holds_the_journal_lock(self):
        journal = self.home / "codex-worker-resets.jsonl"
        holder_code = """import fcntl
import select
import sys
with open(sys.argv[1], "a+") as journal:
    fcntl.flock(journal, fcntl.LOCK_EX)
    print("locked", flush=True)
    select.select([sys.stdin], [], [], 5)
"""
        holder = subprocess.Popen([PYTHON, "-I", "-c", holder_code, str(journal)],
                                  cwd=self.repo, env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline(), "locked\n")
            worker = self.worker()
            cancelled = [False]

            def cancel_while_waiting(seconds):
                cancelled[0] = True

            with mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(worker.time, "sleep", side_effect=cancel_while_waiting), \
                    mock.patch.object(worker, "account_rpc") as account_rpc:
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    worker.reset_credit_action({"name": "main", "home": str(self.home)},
                                               credit_id="RateLimitResetCredit_cancelled",
                                               slug="cancelled-reset", cancelled=lambda: cancelled[0],
                                               confirmed=True)
            self.assertTrue(cancelled[0], "the operation must wait for the contended lock")
            account_rpc.assert_not_called()
            self.assertEqual(journal.read_text(), "", "cancel before recording an intent or sending an RPC")
            self.assertFalse(self.consume_calls())
            self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        finally:
            holder.communicate(timeout=5)

    def test_reset_credit_cli_previews_then_spends_only_with_yes(self):
        self.configure()
        self.account(self.home, "A", self.reset, used=99)
        earliest = self.reset_credit("RateLimitResetCredit_preview", self.reset + 86400)
        later = self.reset_credit("RateLimitResetCredit_explicit", self.reset + 2 * 86400)
        self.reset_credits(self.home, later, earliest,
                           self.reset_credit("RateLimitResetCredit_expired", int(time.time()) - 100),
                           self.reset_credit("RateLimitResetCredit_used", self.reset + 100, status="consumed"))
        preview = self.cli("reset-credit", "main")
        self.assertIn(earliest["id"], preview)
        self.assertIn("Full reset", preview)
        self.assertFalse(self.consume_calls())
        self.assertFalse(self.reset_journal())
        output = self.cli("reset-credit", "main", "--yes")
        self.assertEqual(self.consume_calls()[0]["params"]["creditId"], earliest["id"])
        self.assertIn('"fixtureReceipt"', output)
        self.assertIn('"fixtureApplied"', output)
        self.assertIn("receipt-for-" + earliest["id"], output)
        trace = self.trace()
        position = next(i for i, event in enumerate(trace) if event.get("command") == "consume")
        self.assertTrue(any(event.get("command") == "quota-snapshot" and event["primary"]["usedPercent"] == 0
                            for event in trace[position + 1:]), "refresh limits after consuming the credit")
        self.assertIn("main", output)
        self.cli("reset-credit", "main", "--credit", later["id"], "--yes")
        self.assertEqual(self.consume_calls()[-1]["params"]["creditId"], later["id"])
        self.assertEqual(len(self.consume_calls()), 2)

    def test_accounts_displays_available_resets_and_nearest_expiry(self):
        self.configure()
        expires = self.reset + 13 * 86400
        credits = [self.reset_credit("RateLimitResetCredit_one", expires),
                   self.reset_credit("RateLimitResetCredit_two", expires + 9 * 86400)]
        self.reset_credits(self.home, *credits)
        worker = self.worker()
        with mock.patch.dict(os.environ, self.env):
            info = worker.account_read({"name": "main", "home": str(self.home)})
        self.assertEqual(info["reset_credits"], {"availableCount": 2, "credits": credits})
        output = self.cli("accounts")
        lines = output.splitlines()
        headers = lines[0].split("\t")
        self.assertIn("RESETS", headers)
        index = headers.index("RESETS")
        rows = {line.split("\t")[0]: line.split("\t") for line in lines[1:]}
        expires_date = datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
        self.assertEqual(rows["main"][index], "2 (exp " + expires_date + ")")
        self.assertEqual(rows["second"][index], "0")
