"""Notification delivery and account attribution tests."""
import io
import json
import os
import subprocess
import time
from types import SimpleNamespace
from unittest import mock

from rotation_support import PYTHON, RotationTestCase


class NotificationTests(RotationTestCase):
    def test_notify_reports_completed_turn_and_failed_check_with_task_fields(self):
        self.configure()
        self.configure_notify()
        self.start("notify-check", "--check", "test -f done.txt")
        self.wait("notify-check")
        finished = self.notification("notify-check", "turn_finished")
        failed = self.notification("notify-check", "check_failed")
        self.assertEqual(finished["outcome"], "completed")
        state = self.state("notify-check")
        for event in (finished, failed):
            self.assertEqual(event["repo"], str(self.repo))
            self.assertEqual(event["branch"], state["branch"])
            self.assertEqual(event["account"], "main")
            self.assertIsInstance(event["at"], (int, float))
            self.assertTrue(event["message"].startswith("notify-check: "))
            self.assertNotIn("\n", event["message"])
        self.assertIn("check", finished["message"].lower())

    def test_notify_reports_rotation_pause_wake_and_review(self):
        self.configure()
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.start("notify-rotation")
        self.wait("notify-rotation")
        rotated = self.notification("notify-rotation", "rotated")
        self.assertEqual((rotated["from"], rotated["to"]), ("main", "second"))

        reset = int(time.time()) + 3
        self.account(self.home, "A", reset, used=99, auto_reset=True)
        self.account(self.other, "B", reset + 3600, used=99)
        reset = self.reset_after_ready(lambda: self.start("notify-sleep", "--review"), reset, 3)
        self.wait("notify-sleep")
        paused = self.notification("notify-sleep", "paused")
        self.assertAlmostEqual(paused["until"], reset + 0.1, places=3)
        self.wait("notify-sleep", timeout=10, through_sleep=True)
        self.notification("notify-sleep", "woke")
        self.notification("notify-sleep", "review_done")
        self.notification("notify-sleep", "turn_finished")

    def test_notify_events_filters_unrequested_event_types(self):
        self.configure()
        self.configure_notify(events=["turn_finished"])
        self.start("notify-filter", "--check", "test -f done.txt")
        self.wait("notify-filter")
        self.notification("notify-filter", "turn_finished")
        self.assertEqual([event["event"] for event in self.notifications("notify-filter")],
                         ["turn_finished"])

    def test_final_notifications_survive_clean_at_idle_state_publication(self):
        self.configure()
        receiver = self.root / "receive-after-clean.py"
        receiver.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "gate, output, task, worktree = map(Path, sys.argv[1:5])\n"
            "deadline = time.monotonic() + 10\n"
            "while not gate.exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise RuntimeError('cleanup never released notification receiver')\n"
            "    time.sleep(0.01)\n"
            "event = json.loads(sys.argv[-1])\n"
            "event['receiver_cwd'] = os.getcwd()\n"
            "event['task_removed'] = not task.exists()\n"
            "event['worktree_removed'] = not worktree.exists()\n"
            "os.write(1, ('stdout: ' + event['event'] + '\\n').encode())\n"
            "os.write(2, ('stderr: ' + event['event'] + '\\n').encode())\n"
            "with output.open('a') as stream:\n"
            "    stream.write(json.dumps(event) + '\\n')\n")
        cases = (("work", None, {"turn_finished"}),
                 ("review", None, {"review_done", "turn_finished"}),
                 ("failed-check", "exit 1", {"check_failed", "turn_finished"}))
        for mode, check, expected in cases:
            with self.subTest(mode=mode):
                slug = "notify-clean-" + mode
                branch = "codex/" + slug
                task = self.home / "claude-tasks" / slug
                task.mkdir(parents=True)
                worktree = self.home / "worktrees" / ("claude-" + slug) / self.repo.name
                worktree.parent.mkdir(parents=True)
                self.git("worktree", "add", "--quiet", "-b", branch, str(worktree), "main")
                gate = self.root / (slug + ".released")
                output = self.root / (slug + ".jsonl")
                self.configure_notify(command=[PYTHON, str(receiver), str(gate), str(output),
                                               str(task), str(worktree)])
                worker = self.worker()
                state = {"slug": slug, "repo": str(self.repo), "worktree": str(worktree),
                         "branch": branch, "base": "main", "account": "main", "thread": "",
                         "threads": [], "reviews": [], "rotations": [], "checks": [],
                         "pid": os.getpid(), "kind": "review" if mode == "review" else "work",
                         "outcome": "running", "check": check, "check_timeout": 1,
                         "pending": {"prompt": "fixture turn", "effort": None,
                                     "review_after": mode == "failed-check"}}
                worker.save(state)
                real_save = worker.save
                publications = []

                def completed_turn(current, *args, **kwargs):
                    current["outcome"] = "completed"
                    if current["kind"] == "review":
                        # Exercise fallback to this turn's review metadata.
                        current["reviews"].append({"account": "second", "thread": "fixture-review"})

                def save_and_clean(current):
                    real_save(current)
                    if current.get("pid") is not None:
                        return
                    publications.append({"state": json.loads(json.dumps(current)),
                                         "launched": {json.loads(proc.args[-1])["event"]
                                                      for proc in worker.NOTIFY_PROCESSES}})
                    try:
                        worker.cmd_clean(SimpleNamespace(slug=slug, keep_branch=False))
                    finally:
                        gate.touch()

                # Keep the same descriptors open after clean unlinks their paths.
                with (task / "notify.log").open("a+b", buffering=0) as notify_log, \
                        (task / "stderr.log").open("w+") as errors, \
                        mock.patch.object(worker.sys, "stderr", errors), \
                        mock.patch.object(worker.sys, "stdout", io.StringIO()), \
                        mock.patch.dict(os.environ, self.env), \
                        mock.patch.object(worker.signal, "signal"), \
                        mock.patch.object(worker, "run_turn", side_effect=completed_turn), \
                        mock.patch.object(worker, "save", side_effect=save_and_clean):
                    try:
                        worker.cmd__run(SimpleNamespace(slug=slug))
                    finally:
                        gate.touch()
                        self.extra_pids.update(proc.pid for proc in worker.NOTIFY_PROCESSES)
                        for proc in worker.NOTIFY_PROCESSES:
                            self.assertEqual(proc.wait(timeout=5), 0)
                    errors.flush()
                    errors.seek(0)
                    self.assertNotIn("notification log failed", errors.read())
                    notify_log.seek(0)
                    log = notify_log.read().decode()
                self.assertEqual(len(publications), 1)
                self.assertEqual(publications[0]["launched"], expected,
                                 "every final hook must start before pid=None becomes visible")
                self.assertFalse(task.exists())
                self.assertFalse(worktree.exists())
                events = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual({event["event"] for event in events}, expected)
                for event in events:
                    self.assertEqual(event["receiver_cwd"], "/")
                    self.assertTrue(event["task_removed"])
                    self.assertTrue(event["worktree_removed"])
                    self.assertIn("stdout: " + event["event"], log)
                    self.assertIn("stderr: " + event["event"], log)
                    if mode == "review":
                        self.assertEqual(event["account"], "second")
                        self.assertIn("on second", event["message"])
                self.assertIsNone(publications[0]["state"]["pid"])
                self.assertEqual(publications[0]["state"]["outcome"], "completed")

    def test_standalone_review_turn_finished_uses_its_executor_account(self):
        self.configure()
        self.configure_notify()
        slug = "notify-review-account"
        self.start(slug)
        self.wait(slug)
        self.notification(slug, "turn_finished")
        working = self.state(slug)
        self.assertEqual(working["account"], "main")
        self.account(self.home, "A", self.reset, used=99)
        self.cli("review", slug)
        self.wait(slug)
        self.until(lambda: len([event for event in self.notifications(slug)
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications(slug) if event["event"] == "turn_finished"][-1]
        self.assertEqual(finished["account"], "second")
        self.assertIn("on second", finished["message"])
        self.assertEqual(self.state(slug)["account"], "main")
        self.assertEqual(self.state(slug)["thread"], working["thread"])
        self.assertEqual(self.review_invocations()[-1]["home"], str(self.other))

    def assert_review_pause_wake_account(self, slug, angles=None):
        self.enable_fake_time()
        self.configure()
        self.configure_notify()
        self.start(slug)
        self.wait(slug)
        working = self.state(slug)
        reset = int(time.time()) + 3
        self.account(self.home, "A", reset + 3600, used=99)
        runs = [{"mode": "usage_error", "limit_after": 0.05},
                {"mode": "complete", "reply": self.review_result(False)}]
        if angles:
            runs.insert(0, {"mode": "complete", "reply": self.review_result(False)})
        self.account(self.other, "B", reset, runs=runs, auto_reset=True)
        args = ("--review-angles", angles) if angles else ()
        self.reset_after_ready(lambda: self.cli("review", slug, *args), reset, 3)
        self.finish_clock_delay(slug, 3 if angles else 2, key="limit_after")
        self.wait(slug)
        sleeping = self.state(slug)
        self.assertTrue(sleeping.get("sleeping"))
        self.assertEqual(sleeping["reviews"][-1]["angle"], "concurrency" if angles else "correctness")
        self.advance_clock_to_reset(slug)
        self.wait(slug, timeout=10, through_sleep=True)
        for name in ("paused", "woke", "review_done"):
            event = self.notification(slug, name)
            self.assertEqual(event["account"], "second")
            self.assertEqual(event["kind"], "review")
        self.until(lambda: len([event for event in self.notifications(slug)
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications(slug) if event["event"] == "turn_finished"][-1]
        self.assertEqual(finished["account"], "second")
        state = self.state(slug)
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["account"], "main")
        self.assertEqual(state["thread"], working["thread"])

    def test_review_pause_and_wake_use_its_executor_account(self):
        self.assert_review_pause_wake_account("notify-review-sleep")

    def test_second_review_angle_pause_and_wake_use_its_executor_account(self):
        self.assert_review_pause_wake_account("notify-angle-sleep", "correctness,concurrency")

    def test_current_turn_account_uses_only_current_review_records(self):
        worker = self.worker()
        state = {"account": "main", "kind": "review", "review_start": 1,
                 "reviews": [{"account": "previous"}]}
        self.assertEqual(worker.current_turn_account(state), "main")
        state["reviews"].append({"account": "second"})
        self.assertEqual(worker.current_turn_account(state), "second")
        state["review_account"] = "third"
        self.assertEqual(worker.current_turn_account(state), "third")
        state["kind"] = "work"
        self.assertEqual(worker.current_turn_account(state), "main")

    def test_missing_notify_executable_is_logged_without_failing_work(self):
        self.configure()
        missing = str(self.root / "missing-notification-executable")
        self.configure_notify(command=[missing])
        self.start("notify-missing")
        self.wait("notify-missing")
        self.assertEqual(self.state("notify-missing")["outcome"], "completed")
        log = self.home / "claude-tasks" / "notify-missing" / "notify.log"
        self.assertTrue(log.is_file())
        self.assertIn(missing, log.read_text())

    def test_slow_notify_does_not_block_supervisor_completion(self):
        self.configure()
        received = self.root / "slow-notification.json"
        code = ("import json, os, sys, time; from pathlib import Path; "
                "event = json.loads(sys.argv[-1]); event['receiver_pid'] = os.getpid(); "
                "Path(sys.argv[1]).write_text(json.dumps(event)); time.sleep(60)")
        self.configure_notify(command=[PYTHON, "-c", code, str(received)])
        self.start("notify-slow")
        self.wait("notify-slow", timeout=10)
        self.until(lambda: received.exists() and received.read_text().endswith("}"))
        event = json.loads(received.read_text())
        child = event["receiver_pid"]
        self.extra_pids.add(child)
        self.assertEqual(event["event"], "turn_finished")
        self.assertTrue(self.process_is_live(child), "the supervisor must finish before the slow hook")

    def test_notify_reaps_finished_children_without_waiting_for_live_ones(self):
        worker = self.worker()
        finished, running = mock.Mock(), mock.Mock()
        finished.poll.return_value = 0
        running.poll.return_value = None
        worker.NOTIFY_PROCESSES[:] = [finished, running]
        worker.reap_notifications()
        self.assertEqual(worker.NOTIFY_PROCESSES, [running])
        finished.poll.assert_called_once_with()
        running.poll.assert_called_once_with()
        finished.wait.assert_not_called()
        running.wait.assert_not_called()

    def test_macos_notify_escapes_quotes_backslashes_and_line_breaks(self):
        worker = self.worker()
        state = {"slug": "notify-macos", "repo": str(self.repo),
                 "branch": "codex/notify-macos", "account": "main", "worktree": str(self.repo)}
        (self.home / "claude-tasks" / state["slug"]).mkdir(parents=True)
        with mock.patch.object(worker.subprocess, "Popen") as popen:
            worker.notify(state, {"notify": "macos"}, "turn_finished",
                          'completed "quoted" C:\\tmp\nwith newline')
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [
            "osascript", "-e",
            'display notification "notify-macos: completed \\"quoted\\" C:\\\\tmp with newline" '
            'with title "codex-worker"'])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
