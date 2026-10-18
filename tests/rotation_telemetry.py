"""Telemetry, reports, statistics and cleanup tests."""
import datetime
import fcntl
import io
import json
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rotation_support import IMPOSSIBLE_PID, PYTHON, SCRIPT, RotationTestCase


class TelemetryReportTests(RotationTestCase):
    def test_telemetry_records_work_checks_reviews_and_fix_with_rollout_deltas(self):
        self.configure(language="en")
        decisions = [{"id": "F1", "decision": "fixed", "note": "Handle empty input."},
                     {"id": "F2", "decision": "rejected", "note": "The final token is a sentinel."}]
        usages = [(self.telemetry_usage(100, 40, 10, 5), self.telemetry_usage(150, 60, 30, 10)),
                  (self.telemetry_usage(1000, 300, 100, 50), self.telemetry_usage(1100, 350, 120, 60)),
                  (self.telemetry_usage(150, 60, 30, 10), self.telemetry_usage(230, 100, 50, 17)),
                  (self.telemetry_usage(500, 200, 50, 20), self.telemetry_usage(540, 210, 60, 22))]
        replies = [self.work_result(), self.review_result(),
                   self.work_result(findings=decisions), self.review_result(False)]
        windows = [(10, 12), (12, 13), (13, 15), (15, 16)]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": reply, "token_usage_before": usage[0],
             "token_usage_after": usage[1], "weekly_used_before": window[0],
             "weekly_used_after": window[1]}
            for reply, usage, window in zip(replies, usages, windows)])
        self.start("telemetry-fix", "--review", "--fix-rounds", "1", "--check", "true")
        self.wait("telemetry-fix")
        self.until(lambda: len(self.phase_telemetry_journal()) == 6)
        rows = self.phase_telemetry_journal()
        self.assertEqual([(row["phase"], row["round"], row["outcome"]) for row in rows],
                         [("work", 0, "completed"), ("check", 0, "passed"),
                          ("review", 0, "completed"), ("fix", 1, "completed"),
                          ("check", 1, "passed"), ("review", 1, "completed")])
        fields = {"v", "at", "slug", "repo", "kind", "phase", "round", "angle", "account",
                  "thread", "started", "finished", "active_seconds", "sleep_seconds", "outcome",
                  "result_status", "verdict", "findings", "decisions", "check_outcome",
                  "check_seconds", "rotations", "tokens", "weekly_used_before", "weekly_used_after"}
        for row in rows:
            self.assertTrue(fields <= set(row), row)
            self.assertEqual((row["v"], row["slug"], row["repo"]), (1, "telemetry-fix", "repo"))
            self.assertIsInstance(row["at"], (int, float))
            self.assertLessEqual(row["started"], row["finished"])
            self.assertGreaterEqual(row["active_seconds"], 0)
            self.assertEqual(row["sleep_seconds"], 0)
            self.assertEqual(row["rotations"], 0)
        model_rows = [row for row in rows if row["phase"] not in ("check", "base_check")]
        self.assertEqual([row["tokens"] for row in model_rows], [
            {"input": 50, "cached_input": 20, "output": 20, "reasoning": 5},
            {"input": 100, "cached_input": 50, "output": 20, "reasoning": 10},
            {"input": 80, "cached_input": 40, "output": 20, "reasoning": 7},
            {"input": 40, "cached_input": 10, "output": 10, "reasoning": 2}])
        self.assertEqual([(row["weekly_used_before"], row["weekly_used_after"]) for row in model_rows],
                         windows)
        self.assertEqual([row["kind"] for row in model_rows], ["work", "review", "work", "review"])
        self.assertEqual([row["account"] for row in model_rows], ["main"] * 4)
        self.assertEqual([row["angle"] for row in model_rows if row["phase"] == "review"],
                         ["correctness", "correctness"])
        self.assertEqual([row["verdict"] for row in model_rows if row["phase"] == "review"],
                         ["defects", "clean"])
        self.assertEqual([row["findings"] for row in model_rows if row["phase"] == "review"], [2, 0])
        self.assertEqual(model_rows[2]["decisions"], {"fixed": 1, "rejected": 1})
        self.assertEqual([row["result_status"] for row in model_rows if row["kind"] == "work"],
                         ["done", "done"])
        for row in (rows[1], rows[4]):
            self.assertEqual(row["check_outcome"], "passed")
            self.assertGreaterEqual(row["check_seconds"], 0)
        raw = (self.home / "codex-worker-log.jsonl").read_text()
        self.assertNotIn(self.work_result()["summary"], raw)
        self.assertNotIn(self.review_result()["findings"][0]["title"], raw)
        self.assertNotIn(decisions[1]["note"], raw)
        self.assertNotIn(self.brief.read_text().splitlines()[0], raw)

    def test_telemetry_excludes_subscription_sleep_from_active_time(self):
        self.configure(language="en")
        reset = time.time() + 5
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "usage_error", "limit_after": 0.1},
            {"mode": "complete", "delay": 0.2, "reply": self.work_result()}])
        self.account(self.other, "B", self.reset, used=99)
        reset = self.reset_after_ready(lambda: self.start("telemetry-sleep"), reset, 5, after_exec=True)
        self.wait("telemetry-sleep", timeout=12, through_sleep=True)
        self.until(lambda: len(self.phase_telemetry_journal()) == 1)
        row = self.phase_telemetry_journal()[0]
        elapsed = row["finished"] - row["started"]
        self.assertEqual((row["phase"], row["outcome"]), ("work", "completed"))
        self.assertGreater(row["sleep_seconds"], 0.5)
        self.assertLess(row["active_seconds"], elapsed - 0.5)
        self.assertAlmostEqual(row["active_seconds"] + row["sleep_seconds"], elapsed, delta=0.1)

    def test_telemetry_missing_rollout_and_unwritable_journal_do_not_fail_work(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "omit_rollout": True, "reply": self.work_result()}])
        self.start("telemetry-no-rollout")
        self.wait("telemetry-no-rollout")
        self.until(lambda: len(self.phase_telemetry_journal()) == 1)
        row = self.phase_telemetry_journal()[0]
        self.assertEqual(row["tokens"], {"input": None, "cached_input": None,
                                         "output": None, "reasoning": None})
        self.assertIsNone(row["weekly_used_before"])
        self.assertIsNone(row["weekly_used_after"])
        journal = self.home / "codex-worker-log.jsonl"
        journal.unlink()
        journal.mkdir()  # A directory is unwritable as a JSONL file even when tests run as root.
        self.start("telemetry-unwritable")
        self.wait("telemetry-unwritable")
        self.assertEqual(self.state("telemetry-unwritable")["outcome"], "completed")
        stderr = self.home / "claude-tasks" / "telemetry-unwritable" / "stderr.log"
        self.assertIn("codex-worker-log.jsonl", stderr.read_text())
        self.assertNotIn("Traceback", stderr.read_text())

    def test_telemetry_partial_write_preserves_next_stage(self):
        worker = self.worker()
        partial = {"slug": "telemetry-partial", "repo": str(self.repo), "account": "main",
                   "created": time.time()}
        script = """
import json, resource, runpy, signal, sys
worker = runpy.run_path(sys.argv[1])
state = json.loads(sys.argv[2])
(worker['TASKS'] / state['slug']).mkdir(parents=True)
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (100, 100))
worker['PhaseTelemetry'](state, 'work').finish(outcome='completed')
"""
        result = subprocess.run([PYTHON, "-I", "-c", script, str(SCRIPT), json.dumps(partial)],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        journal = self.home / "codex-worker-log.jsonl"
        damaged = journal.read_bytes()
        self.assertEqual(len(damaged), 100)
        self.assertFalse(damaged.endswith(b"\n"))
        stderr = self.home / "claude-tasks" / partial["slug"] / "stderr.log"
        self.assertIn("telemetry unavailable", stderr.read_text())

        recovered = dict(partial, slug="telemetry-recovered", created=time.time())
        (worker.TASKS / recovered["slug"]).mkdir()
        worker.PhaseTelemetry(recovered, "work").finish(outcome="completed")
        contents = journal.read_bytes()
        self.assertTrue(contents.startswith(damaged + b"\n"), "repair must only append to the damaged tail")
        lines = contents.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[-1])["slug"], recovered["slug"])
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual(report["summary"]["tasks"], 1)
        self.assertEqual(report["summary"]["turns"], 1)
        self.assertEqual(report["tasks"][0]["slug"], recovered["slug"])

    def test_telemetry_busy_journal_does_not_block_checks_or_stop(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        journal = self.home / "codex-worker-log.jsonl"
        slug = "telemetry-locked"
        with journal.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.start(slug, "--check", "true")
            # Work, check, and final turn records each have a bounded retry.
            self.wait(slug, timeout=10)
            self.assertEqual(self.state(slug)["outcome"], "completed")
            self.assertEqual(self.state(slug)["checks"][-1]["outcome"], "passed")
            stderr = self.home / "claude-tasks" / slug / "stderr.log"
            self.assertIn("telemetry unavailable", stderr.read_text())
            self.assertEqual(journal.stat().st_size, 0)

            command = shlex.quote(PYTHON) + " -I -c " + shlex.quote("import time; time.sleep(60)")
            self.cli("say", slug, "Run the check again", "--check", command)
            self.until(lambda: bool(self.state(slug).get("check_pid")))
            supervisor = self.state(slug)["pid"]
            self.cli("stop", slug, timeout=5)
            self.wait(slug, timeout=5)
            self.assertEqual(self.state(slug)["outcome"], "stopped")
            self.assertEqual(self.state(slug)["checks"][-1]["outcome"], "stopped")
            self.until(lambda: not self.process_is_live(supervisor))
            self.assertEqual(journal.stat().st_size, 0, "the lock remains held throughout completion and stop")

    def test_telemetry_retries_a_briefly_busy_journal_without_losing_the_record(self):
        worker = self.worker()
        state = {"slug": "telemetry-brief-lock", "repo": str(self.repo), "account": "main"}
        (worker.TASKS / state["slug"]).mkdir(parents=True)
        journal = self.home / "codex-worker-log.jsonl"
        blocked = threading.Event()
        flock = fcntl.flock

        def observe_lock(stream, operation):
            try:
                return flock(stream, operation)
            except BlockingIOError:
                blocked.set()
                raise

        with journal.open("a+b") as held, ThreadPoolExecutor(max_workers=1) as executor:
            flock(held, fcntl.LOCK_EX)
            with mock.patch.object(worker.fcntl, "flock", side_effect=observe_lock):
                future = executor.submit(worker.PhaseTelemetry(state, "work").finish, outcome="completed")
                try:
                    self.assertTrue(blocked.wait(5), "the writer must encounter the held journal lock")
                    time.sleep(0.3)
                    self.assertFalse(future.done(), "a brief lock must retry instead of dropping the row")
                    self.assertEqual(journal.stat().st_size, 0)
                finally:
                    flock(held, fcntl.LOCK_UN)
                future.result(timeout=3)
        self.assertEqual([(row["slug"], row["phase"], row["outcome"]) for row in self.telemetry_journal()],
                         [(state["slug"], "work", "completed")])
        self.assertFalse((worker.TASKS / state["slug"] / "stderr.log").exists())

    def test_telemetry_skips_lock_retry_when_stop_is_already_requested(self):
        worker = self.worker()
        state = {"slug": "telemetry-stopped-lock", "repo": str(self.repo), "account": "main"}
        (worker.TASKS / state["slug"]).mkdir(parents=True)
        journal = self.home / "codex-worker-log.jsonl"
        with journal.open("a+b") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            with mock.patch.object(worker.time, "sleep", side_effect=AssertionError("stop must not wait")):
                worker.PhaseTelemetry(state, "work", stopped=lambda: True).finish(outcome="stopped")
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(journal.stat().st_size, 0)
        stderr = (worker.TASKS / state["slug"] / "stderr.log").read_text()
        self.assertIn("telemetry unavailable", stderr)
        self.assertNotIn("stop must not wait", stderr)

    def test_telemetry_wrapup_interruption_overrides_not_run_and_completed_attempts(self):
        worker = self.worker()
        for outcome in ("stopped", "interrupted"):
            for timing in ("before", "after"):
                with self.subTest(outcome=outcome, timing=timing):
                    stopped = [timing == "before"]
                    state = {"slug": "wrapup-" + outcome + "-" + timing, "repo": str(self.repo),
                             "kind": "work", "account": "main", "thread": "existing-thread",
                             "pid": IMPOSSIBLE_PID, "created": time.time()}
                    if outcome == "stopped":
                        state["stop_request"] = {"at": time.time(), "pid": state["pid"]}
                    (worker.TASKS / state["slug"]).mkdir(parents=True)

                    def complete_then_stop(*args, **kwargs):
                        state["wrapup"]["result"] = self.work_result("partial")
                        stopped[0] = True
                        return "completed", None

                    with mock.patch.object(worker, "monitor", side_effect=complete_then_stop) as monitor:
                        worker.wrapup_turn(state, {"language": "en"}, {"name": "main"}, None,
                                           lambda: stopped[0], {"outcome": "budget_exhausted", "minutes": 1})
                    self.assertEqual(monitor.call_count, int(timing == "after"))
                    self.assertEqual(state["wrapup"]["outcome"], "not_run" if timing == "before" else "completed")
                    self.assertEqual(self.phase_telemetry_journal()[-1]["outcome"], outcome)
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"stopped": 2, "interrupted": 2})

    def test_clean_preserves_telemetry_and_report_skips_unreadable_lines(self):
        self.configure(language="en")
        self.start("telemetry-clean")
        self.wait("telemetry-clean")
        self.until(lambda: len(self.phase_telemetry_journal()) == 1)
        path = self.home / "codex-worker-log.jsonl"
        before = path.read_bytes()
        self.cli("clean", "telemetry-clean")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.home / "claude-tasks" / "telemetry-clean").exists())
        with path.open("a") as stream:
            stream.write("{broken JSON\nnull\n[1, 2]\n")
        report = json.loads(self.cli("report", "--repo", "repo", "--json"))
        self.assertEqual(report["summary"]["tasks"], 1)
        self.assertEqual(report["summary"]["turns"], 1)
        self.assertEqual(report["tasks"][0]["slug"], "telemetry-clean")
        self.assertIn("telemetry-clean", self.cli("report", "--repo", "repo"))

    def test_report_uses_merged_turn_result_after_an_angle_wrapup_is_refused(self):
        # Allow process startup under parallel load before the second angle's cap.
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "2"
        self.configure(language="en", max_review_minutes=2)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)},
            {"mode": "hold"}, {"mode": "refused"}])
        slug = "report-refused-wrapup"
        self.start(slug, "--review", "--review-angles", "correctness,security")
        output = self.wait(slug, timeout=15)
        state = self.state(slug)
        self.assertEqual((state["outcome"], state["review_result"]["verdict"]), ("completed", "clean"))
        self.assertEqual([entry["outcome"] for entry in state["review_result"]["angles"]],
                         ["completed", "refused"])
        self.assertEqual(state["wrapup"]["outcome"], "refused")
        self.assertIn("== review angle security by main", output)
        self.assertIn("== turn stopped: time budget 2 min exhausted", output)
        self.assertEqual(len(self.invocations()), 4)
        self.assertTrue(self.invocations()[-1]["resume"])
        turns = [row for row in self.telemetry_journal() if row["phase"] == "turn"]
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0]["outcome"], turns[0]["result_status"], turns[0]["verdict"]),
                         ("completed", "done", "clean"))
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"completed": 1})
        self.assertEqual(report["summary"]["result_status"], {"done": 1})
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"0": 1})
        self.assertEqual(report["tasks"][0]["last_verdict"], "clean")

    def test_report_prefers_latest_turn_result_without_adding_stage_cost(self):
        now = time.time()
        stages = [
            {"phase": "work", "turn": 1, "outcome": "completed", "result_status": "done",
             "active_seconds": 60, "tokens": {"input": 10, "output": 2}, "account": "main"},
            {"phase": "review", "turn": 1, "outcome": "completed", "verdict": "clean"},
            {"phase": "turn", "turn": 1, "kind": "review", "outcome": "completed",
             "result_status": "done", "verdict": "clean"},
            {"phase": "work", "turn": 2, "outcome": "failed"},
            {"phase": "turn", "turn": 2, "outcome": "failed", "result_status": None, "verdict": None}]
        rows = [dict(stage, v=1, at=now - 10 + index, repo="repo", slug="report-latest-turn",
                     created=now - 100, round=0) for index, stage in enumerate(stages)]
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"failed": 1})
        self.assertEqual(report["summary"]["result_status"], {})
        self.assertEqual(report["summary"]["turns"], 3)
        self.assertEqual(report["summary"]["tokens"]["total"], 12)
        self.assertAlmostEqual(report["summary"]["active_hours"], 1 / 60)
        self.assertEqual(report["accounts"][0]["turns"], 1)
        self.assertEqual(report["tasks"][0]["active_minutes"], 1)
        self.assertIsNone(report["tasks"][0]["last_verdict"])
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"0": 1})

    def test_report_falls_back_to_newer_stages_when_the_turn_result_is_missing(self):
        now = time.time()
        # A busy journal lost turn 2's final row after saving its failed stage.
        stages = [{"phase": "work", "turn": 1, "outcome": "completed", "result_status": "done"},
                  {"phase": "turn", "turn": 1, "outcome": "completed", "result_status": "done",
                   "verdict": None},
                  {"phase": "work", "turn": 2, "outcome": "failed", "result_status": None}]
        rows = [dict(stage, v=1, at=now - 10 + index, repo="repo", slug="report-lost-final",
                     created=now - 100, kind="work", round=0) for index, stage in enumerate(stages)]
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"failed": 1})
        self.assertEqual(report["summary"]["result_status"], {})
        self.assertEqual(report["summary"]["turns"], 2)
        self.assertEqual(report["tasks"][0]["outcome"], "failed")
        self.assertIsNone(report["tasks"][0]["result_status"])
        self.assertIsNone(report["tasks"][0]["last_verdict"])

    def test_report_wrapup_preserves_parent_outcome_unless_interrupted_or_refused(self):
        now = time.time()
        rows = []
        expected = {}
        for parent in ("budget_exhausted", "stuck"):
            for wrapup in ("completed", "failed", "budget_exhausted", "stuck", "stopped", "interrupted", "refused"):
                for kind in ("work", "review", "scout"):
                    slug = parent + "-" + wrapup + "-" + kind
                    expected[slug] = wrapup if wrapup in ("stopped", "interrupted", "refused") else parent
                    for phase, outcome in ((kind, parent), ("wrapup", wrapup)):
                        rows.append({"v": 1, "at": now - 100 + len(rows), "repo": "repo",
                                     "slug": slug, "created": now - 200, "phase": phase,
                                     "kind": kind, "turn": 1, "round": 0, "outcome": outcome})
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual({row["slug"]: row["outcome"] for row in report["tasks"]}, expected)
        self.assertEqual(report["summary"]["outcomes"],
                         {"budget_exhausted": 12, "stuck": 12, "stopped": 6, "interrupted": 6, "refused": 6})

    def test_report_stop_during_budget_wrapup_matches_actual_task_outcome(self):
        self.env["CODEX_TASK_MINUTE_SECONDS"] = "0.2"
        worker = self.worker()
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}, {"mode": "hold"}])
        self.start("report-wrapup-stop", "--max-minutes", "10")
        self.until(lambda: len(self.invocations()) == 2, timeout=8)
        with mock.patch("sys.stdout", new=io.StringIO()):
            worker.cmd_stop(SimpleNamespace(slug="report-wrapup-stop"))
        self.wait("report-wrapup-stop", timeout=12)
        self.until(lambda: len(self.phase_telemetry_journal()) == 2)
        self.assertEqual(self.state("report-wrapup-stop")["outcome"], "stopped")
        self.assertEqual([(row["phase"], row["outcome"]) for row in self.phase_telemetry_journal()],
                         [("work", "budget_exhausted"), ("wrapup", "stopped")])
        report = json.loads(self.cli("report", "--slug", "report-wrapup-stop", "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"stopped": 1})
        self.assertEqual(report["tasks"][0]["outcome"], "stopped")

    def test_report_clean_and_reused_slug_keep_task_lifetimes_and_costs_separate(self):
        self.configure(language="en")
        replies = [self.work_result(), self.review_result(),
                   self.work_result(), self.review_result(False)]
        runs = []
        weekly = 10
        for index, reply in enumerate(replies, 1):
            runs.append({"mode": "complete", "reply": reply,
                         "token_usage_before": self.telemetry_usage(0, 0, 0, 0),
                         "token_usage_after": self.telemetry_usage(index * 10, 0, 2, 1),
                         "weekly_used_before": weekly, "weekly_used_after": weekly + index})
            weekly += index
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("report-reused-slug", "--review")
        self.wait("report-reused-slug")
        self.until(lambda: len(self.phase_telemetry_journal()) == 2)
        first_created = self.state("report-reused-slug")["created"]
        self.cli("clean", "report-reused-slug")
        path = self.home / "fixture.json"
        account = json.loads(path.read_text())
        account["primary"]["usedPercent"] = 13
        path.write_text(json.dumps(account))
        self.start("report-reused-slug", "--review")
        self.wait("report-reused-slug")
        self.until(lambda: len(self.phase_telemetry_journal()) == 4)
        second_created = self.state("report-reused-slug")["created"]
        self.assertGreater(second_created, first_created)
        self.assertEqual([row["created"] for row in self.phase_telemetry_journal()],
                         [first_created, first_created, second_created, second_created])
        report = json.loads(self.cli("report", "--slug", "report-reused-slug", "--json"))
        self.assertEqual((report["summary"]["tasks"], report["summary"]["turns"]), (2, 4))
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"0": 1})
        tasks = sorted(report["tasks"], key=lambda row: row["created"])
        self.assertEqual([(row["created"], row["turns"], row["tokens"]["total"],
                           row["weekly_used_percent"], row["last_verdict"]) for row in tasks],
                         [(first_created, 2, 34, 3, "defects"), (second_created, 2, 74, 7, "clean")])
        self.assertEqual([row["created"] for row in report["tasks"]], [second_created, first_created])

    def test_report_text_distinguishes_reused_slugs_and_repositories_in_local_time(self):
        first_created = datetime.datetime(2026, 1, 2, 1, 34, tzinfo=datetime.timezone.utc).timestamp()
        second_created = first_created + 25 * 3600
        identities = [("repo", first_created), ("repo", second_created), ("other-repo", first_created)]
        rows = [{"v": 1, "at": time.time() - 10, "repo": repo, "slug": "same-slug",
                 "created": created, "phase": "work", "turn": 1, "outcome": "completed"}
                for repo, created in identities]
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual(report["summary"]["tasks"], 3)
        self.assertCountEqual([(task["repo"], task["created"]) for task in report["tasks"]], identities)
        self.assertTrue(all("started" not in task for task in report["tasks"]))
        for zone, first_started, second_started in (
                ("UTC0", "01-02 01:34", "01-03 02:34"),
                ("EST5", "01-01 20:34", "01-02 21:34")):
            with self.subTest(TZ=zone):
                self.env["TZ"] = zone  # Change only the CLI child's zone; tests run in parallel.
                text = self.cli("report")
                header, *task_lines = text.split("Tasks by cost:\n", 1)[1].splitlines()
                self.assertEqual(header.split("  ")[:3], ["slug", "repo", "started"])
                self.assertEqual(len(task_lines), 3)
                self.assertCountEqual([line.split("  ")[:3] for line in task_lines],
                                      [["same-slug", "repo", first_started],
                                       ["same-slug", "repo", second_started],
                                       ["same-slug", "other-repo", first_started]])

    def test_report_text_shows_dash_when_created_is_unknown(self):
        row = {"v": 1, "at": time.time() - 10, "repo": "repo", "slug": "legacy-slug",
               "phase": "work", "started": time.time() - 100, "outcome": "completed"}
        (self.home / "codex-worker-log.jsonl").write_text(json.dumps(row) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertIsNone(report["tasks"][0]["created"])
        text = self.cli("report")
        header, task_line = text.split("Tasks by cost:\n", 1)[1].splitlines()
        self.assertEqual(header.split("  ")[:3], ["slug", "repo", "started"])
        self.assertEqual(task_line.split("  ")[:3], ["legacy-slug", "repo", "-"])

    def test_report_legacy_turn_restart_splits_reused_slug_but_say_and_fix_stay_together(self):
        now = time.time()
        rows = []
        stages = [("work", 1, 0, None), ("review", 1, 0, "defects"),
                  ("fix", 1, 1, None), ("review", 1, 1, "clean"),
                  ("work", 2, 0, None), ("review", 2, 0, "clean"),
                  ("work", 1, 0, None), ("review", 1, 0, "defects")]
        for phase, turn, round_number, verdict in stages:
            rows.append({"v": 1, "at": now - 100 + len(rows), "repo": "repo", "slug": "legacy-reuse",
                         "phase": phase, "kind": "review" if phase == "review" else "work",
                         "turn": turn, "round": round_number, "outcome": "completed", "verdict": verdict,
                         "tokens": {"input": 10, "cached_input": 0, "output": 1, "reasoning": 0}})
        # Without a positive turn counter, earlier journals cannot establish a
        # restart; preserve their existing single-task interpretation.
        for phase in ("work", "review", "work", "review"):
            rows.append({"v": 1, "at": now - 100 + len(rows), "repo": "repo", "slug": "legacy-no-turn",
                         "phase": phase, "outcome": "completed"})
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--slug", "legacy-reuse", "--json"))
        self.assertEqual(report["summary"]["tasks"], 2)
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"1": 1})
        self.assertEqual([(row["created"], row["turns"], row["tokens"]["total"], row["last_verdict"])
                          for row in report["tasks"]],
                         [(None, 6, 66, "clean"), (None, 2, 22, "defects")])
        no_turn = json.loads(self.cli("report", "--slug", "legacy-no-turn", "--json"))
        self.assertEqual((no_turn["summary"]["tasks"], no_turn["summary"]["turns"]), (1, 4))

    def test_report_legacy_stages_join_tagged_continuation_without_merging_prior_or_reused_tasks(self):
        now = time.time()
        continuing_birth, reused_birth = now - 800, now - 700
        rows = []
        stages = [
            (now - 900, None, "work", 1, 0, None),
            (now - 890, None, "review", 1, 0, "defects"),
            (now - 790, None, "work", 1, 0, None),
            (now - 780, None, "review", 1, 0, "defects"),
            (now - 770, continuing_birth, "work", 2, 0, None),
            (now - 760, continuing_birth, "review", 2, 1, "clean"),
            (now - 690, reused_birth, "work", 1, 0, None),
            (now - 680, reused_birth, "review", 1, 0, "clean"),
        ]
        for at, created, phase, turn, round_number, verdict in stages:
            row = {"v": 1, "at": at, "repo": "repo", "slug": "upgraded-reused-slug",
                   "started": at - 5, "phase": phase, "turn": turn, "round": round_number,
                   "kind": "review" if phase == "review" else "work", "outcome": "completed",
                   "verdict": verdict, "findings": 1 if verdict == "defects" else 0,
                   "tokens": {"input": 10, "cached_input": 0, "output": 2, "reasoning": 1}}
            if created is not None:
                row["created"] = created
            if phase == "review":
                row.pop("started")  # Legacy records may have only their completion timestamp.
            rows.append(row)
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual((report["summary"]["tasks"], report["summary"]["turns"]), (3, 8))
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"0": 1, "1": 1})
        self.assertEqual({row["created"]: (row["turns"], row["tokens"]["total"], row["last_verdict"])
                          for row in report["tasks"]},
                         {None: (2, 24, "defects"), continuing_birth: (4, 48, "clean"),
                          reused_birth: (2, 24, "clean")})

    def test_report_weekly_spent_overrides_overlapping_deltas_and_keeps_legacy_fallback(self):
        now = time.time()
        rows = []
        for index, (before, after, spent) in enumerate(((10, 17, 7), (12, 14, 0), (20, 22, None)), 1):
            row = {"v": 1, "at": now - 10 + index, "created": now - 100,
                   "slug": "report-weekly-marks", "repo": "repo", "kind": "work", "phase": "work",
                   "turn": index, "account": "main", "outcome": "completed",
                   "active_seconds": 1, "weekly_used_before": before, "weekly_used_after": after,
                   "usage": [{"account": "main", "weekly_used_before": before,
                              "weekly_used_after": after}]}
            if spent is not None:
                row["weekly_spent"] = spent
                row["usage"][0]["weekly_spent"] = spent
            rows.append(row)
        rows.append({"v": 1, "at": now - 5, "created": now - 100,
                     "slug": "report-weekly-marks", "repo": "repo", "kind": "work", "phase": "work",
                     "turn": 4, "account": "second", "outcome": "completed", "active_seconds": 1,
                     "weekly_spent": 7, "usage": [
                         {"account": "main", "weekly_spent": 2,
                          "weekly_used_before": 40, "weekly_used_after": 49},
                         {"account": "second", "weekly_spent": 5,
                          "weekly_used_before": 60, "weekly_used_after": 69}]})
        (self.home / "codex-worker-log.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows))
        report = json.loads(self.cli("report", "--slug", "report-weekly-marks", "--json"))
        self.assertEqual(report["summary"]["weekly_used_percent"], 16)
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], 16)
        self.assertEqual({row["account"]: row["weekly_used_percent"] for row in report["accounts"]},
                         {"main": 11, "second": 5})

    def test_report_aggregates_cost_reviews_rounds_resets_and_filters(self):
        now = time.time()
        recent = now - 86400
        rows = []

        def add(slug, phase, seconds, tokens=None, account="main", weekly=(None, None),
                round_number=0, outcome="completed", status=None, verdict=None, findings=0,
                angle=None, repo="repo", at=None, turn=1):
            timestamp = recent + len(rows) if at is None else at
            rows.append({"v": 1, "at": timestamp, "slug": slug, "repo": repo,
                         "kind": "review" if phase == "review" else "work", "phase": phase,
                         "turn": turn, "round": round_number, "angle": angle, "account": account,
                         "thread": slug + "-thread", "started": timestamp - seconds,
                         "finished": timestamp, "active_seconds": seconds, "sleep_seconds": 0,
                         "outcome": outcome, "result_status": status, "verdict": verdict,
                         "findings": findings, "decisions": {"fixed": 0, "rejected": 0},
                         "check_outcome": outcome if phase == "check" else None,
                         "check_seconds": seconds if phase == "check" else None, "rotations": 0,
                         "tokens": dict(zip(("input", "cached_input", "output", "reasoning"), tokens))
                         if tokens is not None else {"input": None, "cached_input": None,
                                                     "output": None, "reasoning": None},
                         "weekly_used_before": weekly[0], "weekly_used_after": weekly[1]})

        add("alpha", "work", 120, (100, 40, 20, 5), weekly=(10, 12), status="done")
        add("alpha", "check", 30, outcome="passed")
        add("alpha", "review", 60, (50, 10, 10, 4), weekly=(12, 13), verdict="defects",
            findings=2, angle="correctness")
        add("alpha", "fix", 120, (100, 80, 20, 5), weekly=(13, 15), round_number=1, status="done")
        add("alpha", "check", 20, round_number=1, outcome="failed")
        add("alpha", "review", 60, (50, 20, 10, 4), weekly=(15, 16), round_number=1,
            verdict="clean", angle="correctness")
        add("beta", "review", 20, (10, 5, 2, 1), account="second", weekly=(2, 3),
            verdict="clean", angle="correctness")
        add("beta", "review", 20, (10, 5, 2, 1), account="second", weekly=(3, 4),
            verdict="defects", findings=1, angle="performance")
        add("beta", "work", 60, (200, 100, 40, 10), account="second", weekly=(95, 2),
            outcome="budget_exhausted", status="partial", turn=2)
        add("old", "work", 600, (1000, 500, 200, 20), weekly=(1, 5),
            outcome="failed", status="blocked", at=now - 8 * 86400)
        add("other-repo", "work", 60, (5, 0, 1, 0), weekly=(20, 30), status="done", repo="other")
        path = self.home / "codex-worker-log.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--repo", "repo", "--json"))
        summary = report["summary"]
        self.assertEqual((summary["tasks"], summary["turns"]), (2, 7))
        self.assertAlmostEqual(summary["active_hours"], 510 / 3600)
        self.assertEqual(summary["tokens"], {"input": 520, "cached_input": 260, "output": 104,
                                             "reasoning": 30, "total": 624, "cached_input_fraction": 0.5})
        self.assertEqual(summary["weekly_used_percent"], 8)
        self.assertEqual(summary["checks"], {"total": 2, "passed": 1, "pass_fraction": 0.5})
        self.assertEqual(summary["reviews"], {"total": 4, "with_defects": 2, "defect_fraction": 0.5,
                                              "findings": 3, "average_findings": 0.75})
        self.assertEqual(summary["fix_rounds_to_clean"], {"1": 1})
        self.assertEqual(summary["outcomes"], {"completed": 1, "budget_exhausted": 1})
        self.assertEqual(summary["result_status"], {"done": 1, "partial": 1})
        self.assertEqual({row["account"]: row["weekly_used_percent"] for row in report["accounts"]},
                         {"main": 6, "second": 2})
        self.assertEqual([row["slug"] for row in report["tasks"]], ["alpha", "beta"])
        self.assertEqual([(row["turns"], row["rounds"], row["tokens"]["total"], row["weekly_used_percent"],
                           row["last_verdict"]) for row in report["tasks"]],
                         [(4, 1, 360, 6, "clean"), (3, 0, 264, 2, "defects")])
        self.assertAlmostEqual(report["tasks"][0]["active_minutes"], 410 / 60)
        self.assertEqual(report["filters"], {"repo": "repo", "slug": None})
        month = json.loads(self.cli("report", "--since", "30d", "--repo", "repo", "--json"))
        self.assertEqual(month["summary"]["tasks"], 3)
        self.assertEqual(month["summary"]["tokens"]["total"], 1824)
        self.assertEqual(month["summary"]["weekly_used_percent"], 12)
        date = datetime.datetime.fromtimestamp(now - 2 * 86400, datetime.timezone.utc).date().isoformat()
        dated = json.loads(self.cli("report", "--since", date, "--repo", "repo", "--json"))
        self.assertEqual(dated["summary"], summary)
        beta = json.loads(self.cli("report", "--slug", "beta", "--json"))
        self.assertEqual(beta["summary"]["tasks"], 1)
        self.assertEqual(beta["summary"]["weekly_used_percent"], 2)
        self.assertEqual(beta["tasks"][0]["slug"], "beta")
        unfiltered = json.loads(self.cli("report", "--json"))
        self.assertEqual(unfiltered["summary"]["tasks"], 3)
        self.assertEqual(unfiltered["summary"]["weekly_used_percent"], 18)
        text = self.cli("report", "--repo", "repo")
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertLess(text.index("alpha"), text.index("beta"))

    def test_report_review_verdict_ignores_refused_angles_but_requires_completed_review(self):
        now = time.time()
        rows = []
        cases = {
            "partly-clean": [("completed", "clean"), ("refused", None)],
            "partly-defects": [("completed", "defects"), ("refused", None)],
            "all-refused": [("refused", None), ("refused", None)],
            "partly-failed": [("completed", "clean"), ("failed", None)],
            "partly-incomplete": [("completed", "clean"), ("completed", None)],
            "partly-stopped": [("completed", "clean"), ("stopped", "clean")],
            "partly-interrupted": [("completed", "clean"), ("interrupted", "clean")],
        }
        for slug, angles in cases.items():
            for index, (outcome, verdict) in enumerate(angles):
                rows.append({"v": 1, "at": now - 100 + len(rows), "repo": "repo", "slug": slug,
                             "phase": "review", "kind": "review", "turn": 1, "round": 0,
                             "angle": ("correctness", "concurrency")[index],
                             "outcome": outcome, "verdict": verdict,
                             "findings": 1 if verdict == "defects" else 0})
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual({row["slug"]: row["last_verdict"] for row in report["tasks"]},
                         {"partly-clean": "clean", "partly-defects": "defects", "all-refused": None,
                          "partly-failed": None, "partly-incomplete": None, "partly-stopped": None,
                          "partly-interrupted": None})
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {"0": 1})
        self.assertEqual(report["summary"]["reviews"]["total"], 6)
        self.assertEqual(report["summary"]["reviews"]["with_defects"], 1)
        self.assertEqual({row["slug"]: row["outcome"] for row in report["tasks"]},
                         {"partly-clean": "completed", "partly-defects": "completed", "all-refused": "refused",
                          "partly-failed": "failed", "partly-incomplete": "completed", "partly-stopped": "stopped",
                          "partly-interrupted": "interrupted"})

    def test_report_latest_work_result_clears_stale_status_and_review_preserves_it(self):
        now = time.time()
        cases = {
            "failed-work": [("work", "work", "failed", None)],
            "failed-fix": [("fix", "work", "failed", None)],
            "untyped-work": [("work", "work", "completed", None)],
            "scout": [("scout", "scout", "completed", None)],
            "review-check": [("review", "review", "completed", None),
                             ("check", "work", "failed", None)],
            "work-wrapup": [("wrapup", "work", "completed", "partial")],
            "untyped-work-wrapup": [("wrapup", "work", "completed", None)],
            "review-wrapup": [("wrapup", "review", "completed", None)],
            "scout-wrapup": [("wrapup", "scout", "completed", None)],
        }
        rows = []
        for slug, stages in cases.items():
            for phase, kind, outcome, status in [("work", "work", "completed", "done")] + stages:
                rows.append({"v": 1, "at": now - 100 + len(rows), "repo": "repo", "slug": slug,
                             "phase": phase, "kind": kind, "outcome": outcome, "result_status": status})
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual({row["slug"]: row["result_status"] for row in report["tasks"]},
                         {"failed-work": None, "failed-fix": None, "untyped-work": None, "scout": None,
                          "review-check": "done", "work-wrapup": "partial", "untyped-work-wrapup": None,
                          "review-wrapup": "done", "scout-wrapup": "done"})
        self.assertEqual(report["summary"]["result_status"], {"done": 3, "partial": 1})

    def test_report_stopped_checks_override_model_outcome_and_failures_preserve_it(self):
        now = time.time()
        cases = {
            "stopped-check": ("check", "stopped", "stopped"),
            "failed-check": ("check", "failed", "failed"),
            "timeout-check": ("check", "timeout", "timeout"),
            "stopped-base": ("base_check", "stopped", "failed"),
            "marked-stopped": ("check", "completed", "stopped"),
            "interrupted-check": ("check", "interrupted", "interrupted"),
            "interrupted-base": ("base_check", "interrupted", "failed"),
            "marked-interrupted": ("check", "completed", "interrupted"),
            "interrupted-marked-stopped": ("check", "interrupted", "stopped"),
            "recovered": ("check", "stopped", "stopped"),
        }
        rows = []
        for slug, (phase, outcome, check_outcome) in cases.items():
            stages = [{"phase": "work", "outcome": "completed", "result_status": "done"},
                      {"phase": phase, "outcome": outcome, "check_outcome": check_outcome}]
            if slug == "recovered":
                stages.append({"phase": "work", "outcome": "completed", "result_status": "done"})
            for stage in stages:
                rows.append(dict(stage, v=1, at=now - 100 + len(rows), repo="repo", slug=slug, kind="work"))
        (self.home / "codex-worker-log.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n")
        report = json.loads(self.cli("report", "--json"))
        self.assertEqual({row["slug"]: row["outcome"] for row in report["tasks"]},
                         {"stopped-check": "stopped", "failed-check": "completed", "timeout-check": "completed",
                          "stopped-base": "stopped", "marked-stopped": "stopped", "recovered": "completed",
                          "interrupted-check": "interrupted", "interrupted-base": "interrupted",
                          "marked-interrupted": "interrupted", "interrupted-marked-stopped": "interrupted"})
        self.assertEqual(report["summary"]["outcomes"], {"completed": 3, "stopped": 3, "interrupted": 4})

    def test_telemetry_rollout_usage_keeps_independent_counters_and_ignores_malformed_records(self):
        worker = self.worker()
        path = self.root / "telemetry-rollout.jsonl"

        def token_count(usage=None, rates=None):
            payload = {"type": "token_count"}
            if usage is not None:
                payload["info"] = {"total_token_usage": usage}
            if rates is not None:
                payload["rate_limits"] = rates
            return {"type": "event_msg", "payload": payload}

        historical = token_count(self.telemetry_usage(900, 800, 90, 50),
                                 {"primary": {"used_percent": 99, "window_minutes": 10080}})
        path.write_text(json.dumps(historical) + "\n")
        offset = path.stat().st_size
        records = [token_count({"input_tokens": 100}), None, [], {"payload": []},
                   token_count(self.telemetry_usage(125, 40, 10, 5), {
                       "primary": {"used_percent": 90, "window_minutes": 300},
                       "secondary": {"used_percent": 10, "window_minutes": 10080}}),
                   token_count(rates={"primary": {"used_percent": 12, "window_minutes": 10080}}),
                   token_count(self.telemetry_usage(150, 60, 30, 10)),
                   token_count({"input_tokens": True, "cached_input_tokens": -1,
                                "output_tokens": "bad", "reasoning_output_tokens": float("inf")},
                               {"primary": {"used_percent": 101, "window_minutes": 10080}})]
        with path.open("a") as stream:
            stream.write("{unreadable JSON\n")
            stream.write("\n".join(json.dumps(record) for record in records) + "\n")
        first, last = worker.rollout_usage(path, offset)
        self.assertEqual(first, {"tokens": {"input": 100, "cached_input": 40, "output": 10,
                                             "reasoning": 5}, "weekly": 10})
        self.assertEqual(last, {"tokens": {"input": 150, "cached_input": 60, "output": 30,
                                            "reasoning": 10}, "weekly": 12})
        self.assertEqual(worker.rollout_usage(self.root / "missing-rollout.jsonl"), (None, None))
        phase = worker.PhaseTelemetry({"slug": "rollout-fallback"}, "work")
        before = {"tokens": {"input": None, "cached_input": None, "output": 5, "reasoning": None},
                  "weekly": 7}
        phase.add_usage("main", "thread", before, path, offset)
        self.assertEqual(phase.usage[0]["tokens"], {"input": 50, "cached_input": 20,
                                                    "output": 25, "reasoning": 5})
        self.assertEqual((phase.usage[0]["weekly_used_before"], phase.usage[0]["weekly_used_after"]), (10, 12))
        usage_only = self.root / "usage-only-rollout.jsonl"
        usage_only.write_text("\n".join(json.dumps(token_count(usage)) for usage in
                                       [self.telemetry_usage(100, 40, 10, 5),
                                        self.telemetry_usage(150, 60, 30, 10)]) + "\n")
        phase.add_usage("main", "thread", before, usage_only, 0)
        self.assertEqual((phase.usage[1]["weekly_used_before"], phase.usage[1]["weekly_used_after"]), (None, None))

    def test_stats_and_clean_include_review_threads_and_account_homes(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.start("all-threads")
        self.wait("all-threads")
        self.account(self.home, "A", self.reset)
        self.cli("review", "all-threads")
        self.wait("all-threads")
        state = self.state("all-threads")
        entries = state["threads"] + state["reviews"]
        homes = {"main": self.home, "second": self.other}
        worker = self.worker()
        self.assertEqual(worker.STATS, SCRIPT.with_name("codex-stats.py"))
        with mock.patch.object(worker.subprocess, "run") as run, mock.patch("builtins.print") as printer:
            worker.cmd_stats(SimpleNamespace(slug="all-threads"))
        files = []
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], [PYTHON, "-I", str(worker.STATS)])
            files.append(Path(call.args[0][3]))
        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertEqual(len(files), 3)
        for entry in entries:
            path = next(path for path in files if entry["thread"] in path.name)
            self.assertTrue(path.is_file())
            self.assertIn(entry["thread"], path.name)
            self.assertEqual(path.parents[4], homes[entry["account"]])
            self.assertIn(entry["account"], output)
        self.cli("clean", "all-threads")
        archived = [(event["home"], event["thread"]) for event in self.trace()
                    if event.get("command") == "archive"]
        self.assertCountEqual(archived, [(str(homes[entry["account"]]), entry["thread"])
                                        for entry in entries])
        self.assertFalse(Path(state["worktree"]).exists())
        self.assertFalse((self.home / "claude-tasks" / "all-threads").exists())
        self.assertEqual(self.git("branch", "--list", state["branch"]), "")
