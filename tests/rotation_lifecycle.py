"""Supervisor lifecycle, signals, rotation and subscription sleep tests."""
import datetime
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rotation_support import IMPOSSIBLE_PID, PYTHON, SCRIPT, RotationTestCase


class LifecycleTests(RotationTestCase):
    @contextmanager
    def prepared_cli(self):
        # Import the real command before a short observation window begins.
        # Arguments still pass through its public parser in a separate process.
        bootstrap = ("import json, runpy, sys\n"
                     "script = sys.argv[1]\n"
                     "namespace = runpy.run_path(script)\n"
                     "print('ready', flush=True)\n"
                     "sys.argv = [script] + json.loads(sys.stdin.readline())\n"
                     "namespace['main']()\n")
        proc = subprocess.Popen([PYTHON, "-I", "-c", bootstrap, str(SCRIPT)],
                                cwd=self.repo, env=self.env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        self.extra_pids.add(proc.pid)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                self.until(lambda: bool(selector.select(0)))
                self.assertEqual(proc.stdout.readline(), "ready\n")
            yield proc
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

    def run_supervisor_fixture(self, slug, instrumentation):
        # Run real signal handlers in a separate process, without changing the
        # test runner's handlers or replacing the supervisor/monitor loop.
        state = {"slug": slug, "repo": str(self.repo), "worktree": str(self.repo),
                 "kind": "scout", "branch": "", "base": "main", "account": "main",
                 "thread": "", "threads": [], "rotations": [], "reviews": [], "checks": [],
                 "created": time.time(), "turns": 1, "turn_started": time.time(),
                 "outcome": "running", "pending": {"prompt": "fixture turn", "effort": None}}
        (self.home / "claude-tasks" / slug).mkdir(parents=True)
        bootstrap = r'''
import importlib.util, json, os, signal, sys, time
from pathlib import Path
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("fixture_worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
state = json.loads(sys.argv[2])
state["pid"] = os.getpid()
worker.save(state)
observed = {}
''' + instrumentation + r'''
try:
    worker.cmd__run(SimpleNamespace(slug=state["slug"]))
finally:
    sys.settrace(None)
for proc in list(worker.NOTIFY_PROCESSES):
    proc.wait(timeout=5)
print(json.dumps(observed))
'''
        result = subprocess.run([PYTHON, "-I", "-c", bootstrap, str(SCRIPT), json.dumps(state)],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_signal_during_live_worker_state_serialization_keeps_supervision(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        slug = "signal-in-state-save"
        observed = self.run_supervisor_fixture(slug, r'''
# Python 3.14 also uses its C encoder for indented JSON. Exercise the Python
# encoder used on 3.9/3.12 so the trace can deliver the signal mid-iteration.
json.encoder.c_make_encoder = None
def trace(frame, event, arg):
    if event == "line" and frame.f_code.co_name == "_iterencode_dict":
        current = frame.f_locals.get("dct", frame.f_locals.get("d"))
        if (isinstance(current, dict) and current.get("codex_pid")
                and frame.f_locals.get("key") == "slug" and not observed):
            # key is bound only after the dict iterator has started. This used
            # to raise when the handler inserted signal into that same dict.
            pid = current["codex_pid"]
            observed.update(pid=pid, pgid=os.getpgid(pid), alive_before=worker.alive(pid))
            os.kill(os.getpid(), signal.SIGTERM)
    return trace
sys.settrace(trace)
''')
        self.extra_pids.add(observed["pid"])
        self.assertTrue(observed["alive_before"])
        state = self.state(slug)
        self.assertEqual(state["outcome"], "interrupted")
        self.assertIsNone(state["pid"])
        self.assertNotIn("codex_pid", state)
        self.assertNotIn("error", state)
        self.assertEqual(state["signal"]["name"], "SIGTERM")
        self.assertIn("SIGTERM", (self.home / "claude-tasks" / slug / "stderr.log").read_text())
        self.assertFalse(self.process_is_live(observed["pid"]), "interrupted worker escaped its supervisor")
        with self.assertRaises(ProcessLookupError):
            os.killpg(observed["pgid"], 0)

    def test_monitor_cleans_up_worker_if_initial_pid_save_fails(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        slug = "worker-pid-save-failed"
        observed = self.run_supervisor_fixture(slug, r'''
real_save = worker.save
def fail_once(current):
    if current.get("codex_pid") and not observed:
        pid = current["codex_pid"]
        observed.update(pid=pid, pgid=os.getpgid(pid), alive_before=worker.alive(pid))
        raise OSError("fixture initial worker PID save failure")
    return real_save(current)
worker.save = fail_once
''')
        self.extra_pids.add(observed["pid"])
        self.assertTrue(observed["alive_before"])
        state = self.state(slug)
        self.assertEqual(state["outcome"], "failed")
        self.assertIsNone(state["pid"])
        self.assertNotIn("codex_pid", state)
        self.assertEqual(state["error"], "fixture initial worker PID save failure")
        self.assertFalse(self.process_is_live(observed["pid"]), "save failure left the worker running")
        with self.assertRaises(ProcessLookupError):
            os.killpg(observed["pgid"], 0)

    def test_signal_before_finalization_freeze_preserves_signal_metadata(self):
        self.configure(language="en")
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": "fixture result"}])
        slug = "signal-before-finalization-freeze"
        observed = self.run_supervisor_fixture(slug, r'''
real_sigmask = worker.signal.pthread_sigmask
stop_signals = {signal.SIGINT, signal.SIGTERM}
def sigmask(how, requested):
    if how == signal.SIG_BLOCK and stop_signals <= set(requested) and not observed:
        # The first signal before the finalization boundary still determines
        # the turn outcome and must be copied to state before publication.
        observed["before"] = time.time()
        os.kill(os.getpid(), signal.SIGTERM)
        observed["after"] = time.time()
    return real_sigmask(how, requested)
worker.signal.pthread_sigmask = sigmask
''')
        self.assertTrue(observed, "fixture did not reach the finalization freeze")
        state = self.state(slug)
        self.assertEqual(state["outcome"], "interrupted")
        self.assertIsNone(state["pid"])
        self.assertEqual(state["signal"]["name"], "SIGTERM")
        self.assertEqual(state["signal"]["signum"], signal.SIGTERM)
        self.assertGreaterEqual(state["signal"]["at"], observed["before"])
        self.assertLessEqual(state["signal"]["at"], observed["after"])
        self.assertIn("SIGTERM", (self.home / "claude-tasks" / slug / "stderr.log").read_text())
        interrupted = self.notification(slug, "interrupted")
        self.assertEqual(interrupted["signal"], state["signal"])
        expected_time = datetime.datetime.fromtimestamp(state["signal"]["at"]).strftime("%Y-%m-%d %H:%M")
        message = ("interrupted externally by SIGTERM at " + expected_time +
                   "; the worktree is kept — check git status and continue with say")
        self.assertIn(message, interrupted["message"])
        self.assertNotIn("unknown", interrupted["message"])
        self.assertIn("== " + message, self.wait(slug))
        events = self.notifications(slug)
        finished = [event for event in events if event["event"] == "turn_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["outcome"], "interrupted")
        self.assertEqual(len([event for event in events if event["event"] == "interrupted"]), 1)
        rows = [row for row in self.telemetry_journal() if row["slug"] == slug and row["phase"] == "turn"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "interrupted")
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"interrupted": 1})
        self.assertEqual(report["tasks"][0]["outcome"], "interrupted")
        invocations = self.invocations()
        self.assertEqual(len(invocations), 1)
        self.assertFalse(self.process_is_live(invocations[0]["pid"]), "worker escaped its supervisor")

    def test_signal_during_finalization_is_logged_without_republishing(self):
        self.configure(language="en")
        self.configure_notify()
        # Check the actual hook child, whose mask differs from the supervisor's
        # frozen finalization mask and must still accept cancellation signals.
        (self.root / "receive-notification.py").write_text(
            "import json, signal, sys\n"
            "from pathlib import Path\n"
            "event = json.loads(sys.argv[-1])\n"
            "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())\n"
            "event['blocked_signals'] = sorted(int(sig) for sig in blocked & {signal.SIGINT, signal.SIGTERM})\n"
            "with Path(sys.argv[1]).open('a') as stream:\n"
            "    stream.write(json.dumps(event) + '\\n')\n")
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": "fixture result"}])
        for point in ("record", "notify", "hook-launched", "reap"):
            with self.subTest(point=point):
                slug = "finalization-signal-" + point
                observed = self.run_supervisor_fixture(slug, "point = " + repr(point) + r'''
observed.update(events=[], fired=False, finished_launched=False)
real_record = worker.record_turn_result
real_notify = worker.notify
real_popen = worker.subprocess.Popen
real_reap = worker.reap_notifications
stop_signals = {signal.SIGINT, signal.SIGTERM}
def interrupt(current):
    observed["fired"] = True
    observed["outcome_at_signal"] = current["outcome"]
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    observed["masked"] = stop_signals <= blocked
    os.kill(os.getpid(), signal.SIGTERM)
    observed["pending"] = signal.SIGTERM in signal.sigpending()
def record(current, stopped):
    if point == "record" and not observed["fired"]:
        interrupt(current)
    return real_record(current, stopped)
def notify(current, cfg, event, message, **fields):
    if point == "notify" and event == "turn_finished" and not observed["fired"]:
        interrupt(current)
    result = real_notify(current, cfg, event, message, **fields)
    if event == "turn_finished":
        observed["finished_launched"] = True
    return result
def popen(args, *a, **kw):
    try:
        payload = json.loads(args[-1])
    except (TypeError, ValueError):
        payload = None
    is_hook = isinstance(payload, dict) and payload.get("slug") == state["slug"] and "event" in payload
    proc = real_popen(args, *a, **kw)
    if is_hook:
        observed["events"].append(payload)
        if point == "hook-launched" and payload["event"] == "turn_finished" and not observed["fired"]:
            interrupt(payload)
    return proc
def reap():
    result = real_reap()
    if point == "reap" and observed["finished_launched"] and not observed["fired"]:
        interrupt(observed["events"][-1])
    return result
worker.record_turn_result = record
worker.notify = notify
worker.subprocess.Popen = popen
worker.reap_notifications = reap
''')
                self.assertTrue(observed["fired"], "fixture did not reach the finalization checkpoint")
                self.assertEqual(observed["outcome_at_signal"], "completed")
                self.assertTrue(observed["masked"], "finalization did not block cancellation signals")
                self.assertTrue(observed["pending"], "fixture did not queue a blocked SIGTERM")
                state = self.state(slug)
                self.assertEqual(state["outcome"], "completed")
                self.assertIsNone(state["pid"])
                self.assertNotIn("signal", state)
                log = (self.home / "claude-tasks" / slug / "stderr.log").read_text()
                self.assertEqual(log.count("supervisor signal:"), 1)
                self.assertIn("SIGTERM", log)
                rows = [row for row in self.telemetry_journal() if row["slug"] == slug and row["phase"] == "turn"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["outcome"], "completed")
                events = self.notifications(slug)
                finished = [event for event in events if event["event"] == "turn_finished"]
                self.assertEqual(len(finished), 1)
                self.assertEqual(finished[0]["outcome"], "completed")
                self.assertEqual(finished[0]["blocked_signals"], [])
                self.assertFalse(any(event["event"] == "interrupted" for event in events))
                self.assertEqual(len([event for event in observed["events"]
                                      if event["event"] == "turn_finished"]), 1)
                report = json.loads(self.cli("report", "--slug", slug, "--json"))
                self.assertEqual(report["summary"]["outcomes"], {"completed": 1})
                self.assertEqual(report["tasks"][0]["outcome"], "completed")
                output = self.wait(slug)
                self.assertIn("fixture result", output)
                self.assertNotIn("interrupted externally", output)

    def test_external_signal_interrupts_work_keeps_changes_and_skips_review(self):
        self.configure(language="en")
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "hold", "write_files": {"pending.txt": "unfinished work\n"}}])
        slug = "work-interrupted"
        self.start(slug, "--review", "--fix-rounds", "2", "--check", "touch unexpected-check")
        worktree = Path(self.state(slug)["worktree"])
        self.until(lambda: (worktree / "pending.txt").exists())
        supervisor = self.state(slug)["pid"]
        before = time.time()
        os.kill(supervisor, signal.SIGTERM)
        output = self.wait(slug)
        state = self.state(slug)
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual(state["signal"]["signum"], signal.SIGTERM)
        self.assertEqual(state["signal"]["name"], "SIGTERM")
        self.assertGreaterEqual(state["signal"]["at"], before)
        self.assertLessEqual(state["signal"]["at"], time.time())
        self.assertNotIn("stop_request", state)
        stderr = (self.home / "claude-tasks" / slug / "stderr.log").read_text()
        self.assertIn("SIGTERM", stderr)
        self.assertIn("signum=" + str(int(signal.SIGTERM)), stderr)
        self.assertRegex(stderr, r"ppid=\d+")
        self.assertRegex(stderr, r"pgid=\d+")
        expected_time = datetime.datetime.fromtimestamp(state["signal"]["at"]).strftime("%Y-%m-%d %H:%M")
        self.assertIn("== interrupted externally by SIGTERM at " + expected_time +
                      "; the worktree is kept — check git status and continue with say", output)
        self.assertEqual((worktree / "pending.txt").read_text(), "unfinished work\n")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(state.get("reviews"))
        self.assertFalse(state.get("checks"))
        self.assertFalse(state.get("rounds"))
        self.assertFalse((worktree / "unexpected-check").exists())
        self.assertEqual(self.notification(slug, "turn_finished")["outcome"], "interrupted")
        self.assertEqual(self.notification(slug, "interrupted")["outcome"], "interrupted")
        rows = [row for row in self.telemetry_journal() if row["slug"] == slug]
        self.assertEqual([(row["phase"], row["outcome"]) for row in rows],
                         [("work", "interrupted"), ("turn", "interrupted")])
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"interrupted": 1})

    def test_external_signal_interrupts_single_review_and_second_angle(self):
        self.configure(language="en")
        for angled in (False, True):
            with self.subTest(angled=angled):
                (self.home / "exec-count").unlink(missing_ok=True)
                runs = [{"mode": "complete", "reply": self.work_result()}]
                if angled:
                    runs.append({"mode": "complete", "reply": self.review_result()})
                runs.append({"mode": "hold"})
                self.account(self.home, "A", self.reset, runs=runs)
                slug = "second-angle-interrupted" if angled else "review-interrupted"
                options = ("--review-angles", "correctness,concurrency") if angled else ("--review",)
                before = len(self.invocations())
                self.start(slug, *options, "--fix-rounds", "2")
                self.until(lambda: len(self.invocations()) == before + len(runs))
                review_thread = self.invocations()[-1]["thread"]
                # reviews is published only after the supervisor handles thread.started.
                self.until(lambda: any(entry["thread"] == review_thread
                                       for entry in self.state(slug)["reviews"]))
                os.kill(self.state(slug)["pid"], signal.SIGTERM)
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "interrupted")
                self.assertEqual(state["signal"]["name"], "SIGTERM")
                self.assertEqual(state["reviews"][-1]["outcome"], "interrupted")
                self.assertFalse(state.get("rounds"), "interrupted reviews cannot start a fix round")
                self.assertEqual(len(self.invocations()), before + len(runs))
                self.assertIn("== interrupted externally by SIGTERM at ", output)
                if angled:
                    self.assertEqual([entry["outcome"] for entry in state["review_result"]["angles"]],
                                     ["completed", "interrupted"])
                rows = [row for row in self.telemetry_journal() if row["slug"] == slug]
                self.assertEqual(rows[-1]["phase"], "turn")
                self.assertEqual(rows[-1]["outcome"], "interrupted")
                reviews = [row for row in rows if row["phase"] == "review"]
                self.assertEqual([row["outcome"] for row in reviews],
                                 ["completed", "interrupted"] if angled else ["interrupted"])
                if angled:
                    self.assertEqual(reviews[-1]["angle"], "concurrency")
                report = json.loads(self.cli("report", "--slug", slug, "--json"))
                self.assertEqual(report["summary"]["outcomes"], {"interrupted": 1})
                self.assertEqual(report["tasks"][0]["outcome"], "interrupted")

    def test_external_signal_before_second_review_thread_started_matches_late_signal(self):
        self.configure(language="en")
        observed = []
        for before_thread in (True, False):
            with self.subTest(before_thread=before_thread):
                (self.home / "exec-count").unlink(missing_ok=True)
                release = self.root / "second-angle-thread-release"
                held = {"mode": "hold"}
                if before_thread:
                    # This existing fake gate returns on cancellation without ever
                    # emitting thread.started; elapsed time cannot open the window.
                    held["rollout_gate"] = str(release)
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": self.review_result()}, held])
                slug = "second-angle-signal-" + ("early" if before_thread else "late")
                before = len(self.invocations())
                self.start(slug, "--review-angles", "correctness,concurrency", "--fix-rounds", "2")
                self.until(lambda: len(self.invocations()) == before + 3)
                review_thread = self.invocations()[-1]["thread"]
                if before_thread:
                    self.until(lambda: any(event["command"] == "rollout-waiting"
                                           and event["thread"] == review_thread for event in self.trace()))
                    self.assertFalse(release.exists())
                    self.assertNotIn(review_thread, [entry["thread"] for entry in self.state(slug)["reviews"]])
                else:
                    self.until(lambda: any(entry["thread"] == review_thread
                                           for entry in self.state(slug)["reviews"]))
                os.kill(self.state(slug)["pid"], signal.SIGTERM)
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "interrupted")
                self.assertEqual(state["signal"]["name"], "SIGTERM")
                self.assertFalse(state.get("rounds"), "interrupted reviews cannot start a fix round")
                self.assertEqual(len(self.invocations()), before + 3)

                expected_time = datetime.datetime.fromtimestamp(state["signal"]["at"]).strftime("%Y-%m-%d %H:%M")
                self.assertIn("== interrupted externally by SIGTERM at " + expected_time, output)
                self.assertRegex(output, r"== review angle concurrency by main \(\d+(?:\.\d+)? min\): "
                                 r"interrupted \(result unknown\)")
                self.assertIn("== auto-fix: 0 rounds,", output)
                self.assertIn("; stop: interrupted", output)
                result = state["review_result"]
                self.assertEqual([(entry["angle"], entry["outcome"]) for entry in result["angles"]],
                                 [("correctness", "completed"), ("concurrency", "interrupted")])
                self.assertIsNone(result["angles"][-1]["result"])

                rows = [row for row in self.telemetry_journal() if row["slug"] == slug]
                self.assertEqual([(row["phase"], row["outcome"]) for row in rows],
                                 [("work", "completed"), ("review", "completed"),
                                  ("review", "interrupted"), ("turn", "interrupted")])
                reviews = [row for row in rows if row["phase"] == "review"]
                self.assertEqual([row["angle"] for row in reviews], ["correctness", "concurrency"])
                self.assertEqual(reviews[-1]["thread"], None if before_thread else review_thread)
                self.assertIsNone(reviews[-1]["verdict"])
                self.assertIsNone(reviews[-1]["findings"])
                events = self.home / "claude-tasks" / slug / "events.jsonl"
                started_threads = [event["thread_id"] for event in map(json.loads, events.read_text().splitlines())
                                   if event.get("type") == "thread.started"]
                self.assertEqual(review_thread in started_threads, not before_thread)
                report = json.loads(self.cli("report", "--slug", slug, "--json"))
                self.assertEqual(report["summary"]["outcomes"], {"interrupted": 1})
                self.assertEqual(report["summary"]["fix_rounds_to_clean"], {})
                self.assertEqual(report["tasks"][0]["outcome"], "interrupted")
                self.assertEqual(report["tasks"][0]["rounds"], 0)

                # Compare every wait line and the semantic result/report fields.
                # Durations, timestamps, thread IDs and usage legitimately differ.
                observed.append({
                    "wait": re.sub(r"\d+\.\d+ min", "<duration>", output.replace(slug, "<slug>")
                                   .replace(expected_time, "<signal time>")),
                    "review_result": dict(result, angles=[
                        {key: value for key, value in entry.items() if key not in ("started", "finished")}
                        for entry in result["angles"]]),
                    "telemetry": [
                        {key: row.get(key) for key in ("phase", "kind", "turn", "round", "angle", "account",
                                                       "outcome", "result_status", "verdict", "findings",
                                                       "auto_fix_stop")}
                        for row in rows if row["phase"] in ("review", "turn")],
                    "report_summary": {key: report["summary"][key] for key in (
                        "tasks", "turns", "checks", "ci", "reviews", "fix_rounds_to_clean",
                        "auto_fix_stops", "outcomes", "result_status")},
                    "report_task": {key: report["tasks"][0][key] for key in (
                        "outcome", "rounds", "turns", "last_verdict", "auto_fix_stop", "result_status")},
                })
        for observable in observed[0]:
            with self.subTest(observable=observable):
                self.assertEqual(observed[0][observable], observed[1][observable])

    def test_spawn_clears_previous_stop_request_before_external_signal(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}, {"mode": "hold"}])
        slug = "stale-stop-request"
        self.start(slug)
        self.until(lambda: len(self.invocations()) == 1)
        supervisor = self.state(slug)["pid"]
        self.cli("stop", slug, timeout=20)
        self.wait(slug)
        old = self.state(slug)
        self.assertEqual(old["outcome"], "stopped")
        self.assertEqual(old["stop_request"]["pid"], supervisor)
        self.assertEqual(old["signal"]["name"], "SIGINT")
        self.cli("say", slug, "Continue the interrupted work")
        self.until(lambda: len(self.invocations()) == 2)
        current = self.state(slug)
        self.assertNotIn("stop_request", current)
        self.assertNotIn("signal", current)
        os.kill(current["pid"], signal.SIGTERM)
        self.wait(slug)
        state = self.state(slug)
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual(state["signal"]["name"], "SIGTERM")
        self.assertNotIn("stop_request", state)

    def test_supervisor_preserves_first_signal_and_logs_each_before_cancellation(self):
        worker = self.worker()
        slug = "first-external-signal"
        (worker.TASKS / slug).mkdir(parents=True)
        state = {"slug": slug, "pid": os.getpid(), "repo": str(self.repo), "worktree": str(self.repo),
                 "kind": "scout", "outcome": "running", "pending": {"prompt": "question", "effort": None}}
        worker.save(state)
        handlers = {}
        first = []
        current_signal = []
        cancellation = threading.Event()
        set_cancellation = cancellation.set

        def cancel_after_log():
            log = worker.TASKS / slug / "stderr.log"
            self.assertIn(signal.Signals(current_signal[0]).name, log.read_text().splitlines()[-1])
            set_cancellation()

        cancellation.set = cancel_after_log

        def signal_handler(signum, handler):
            handlers[signum] = handler

        def interrupt(st, cfg, prompt, effort, current_message, stopped, sleep):
            log = worker.TASKS / slug / "stderr.log"
            for signum in (signal.SIGTERM, signal.SIGINT):
                current_signal[:] = [signum]
                handlers[signum](signum, None)
                self.assertIn(signal.Signals(signum).name, log.read_text())
                self.assertTrue(stopped())
                if not first:
                    first.append(dict(st["signal"]))
                self.assertEqual(st["signal"], first[0])

        with mock.patch.object(worker.signal, "signal", side_effect=signal_handler), \
                mock.patch.object(worker.threading, "Event", return_value=cancellation), \
                mock.patch.object(worker, "accounts_config", return_value={"enabled": False}), \
                mock.patch.object(worker, "run_turn", side_effect=interrupt), \
                mock.patch.object(worker, "record_turn_result"), \
                mock.patch.object(worker, "notify"), \
                mock.patch.object(worker, "guarded_changes", return_value=[]):
            worker.cmd__run(SimpleNamespace(slug=slug))
        state = worker.load(slug)
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual(state["signal"], first[0])
        self.assertEqual(state["signal"]["name"], "SIGTERM")
        self.assertEqual(len((worker.TASKS / slug / "stderr.log").read_text().splitlines()), 2)

    def test_stop_request_is_durable_before_signal_and_survives_supervisor_save(self):
        worker = self.worker()
        slug = "stop-save-race"
        (worker.TASKS / slug).mkdir(parents=True)
        state = {"slug": slug, "pid": IMPOSSIBLE_PID, "outcome": "running"}
        worker.save(state)
        supervisor_state = dict(state)

        def signal_after_request(pid, signum):
            self.assertEqual((pid, signum), (state["pid"], signal.SIGINT))
            latest = worker.load(slug)
            self.assertEqual(latest["stop_request"]["pid"], pid)
            supervisor_state["checkpoint"] = "concurrent supervisor progress"
            worker.save(supervisor_state)
            self.assertEqual(worker.load(slug)["stop_request"], latest["stop_request"])

        with mock.patch.object(worker, "alive", side_effect=[True, False]), \
                mock.patch.object(worker.os, "killpg", side_effect=signal_after_request) as killpg, \
                mock.patch.object(worker.time, "sleep"), mock.patch("builtins.print"):
            worker.cmd_stop(SimpleNamespace(slug=slug))
        killpg.assert_called_once()
        self.assertEqual(worker.load(slug)["checkpoint"], "concurrent supervisor progress")
        self.assertEqual(worker.stop_outcome(dict(state), lambda: True), "stopped")
        self.assertEqual(worker.stop_outcome(dict(state), lambda: False), None)
        next_turn = dict(state, pid=os.getpid())
        self.assertEqual(worker.stop_outcome(next_turn, lambda: True), "interrupted")

    def test_interrupted_notification_obeys_notify_events_filter(self):
        self.configure(language="en")
        self.configure_notify(events=["interrupted"])
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        slug = "interrupted-notify-filter"
        self.start(slug)
        self.until(lambda: bool(self.invocations()))
        os.kill(self.state(slug)["pid"], signal.SIGTERM)
        self.wait(slug)
        self.notification(slug, "interrupted")
        self.assertEqual([event["event"] for event in self.notifications(slug)], ["interrupted"])

    def test_paused_working_turn_does_not_start_automatic_review(self):
        self.configure(wait_for_reset=False)
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("review-paused", "--review", "--check", "touch unexpected-check")
        output = self.wait("review-paused")
        self.assertEqual(self.state("review-paused")["outcome"], "exhausted")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertNotIn("== review by", output)
        self.assertFalse(self.state("review-paused").get("checks"))
        self.assertFalse((Path(self.state("review-paused")["worktree"]) / "unexpected-check").exists())

    def test_review_requires_idle_task(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("review-idle")
        self.until(lambda: bool(self.invocations()))
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "review", "review-idle"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("running", result.stderr)
        self.assertFalse(self.review_invocations())
        self.assertEqual(self.state("review-idle")["turns"], 1)
        self.cli("stop", "review-idle", timeout=20)
        self.wait("review-idle")

    def test_review_limit_restarts_fresh_on_another_account(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Рабочий результат"},
            {"mode": "limit", "rollout_used": 99, "messages": ["Частичная находка"]}])
        self.account(self.other, "B", self.reset + 3600,
                     runs=[{"mode": "complete", "reply": "Полное ревью на другой подписке"}])
        self.start("review-limit")
        self.wait("review-limit")
        working = self.state("review-limit")
        self.cli("review", "review-limit")
        output = self.wait("review-limit")
        executions = self.review_invocations()
        self.assertEqual(len(executions), 2)
        self.assertEqual([entry["home"] for entry in executions], [str(self.home), str(self.other)])
        self.assertNotEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertTrue(all(not entry["resume"] for entry in executions))
        self.assertEqual(executions[0]["prompt"], executions[1]["prompt"])
        self.assertNotIn("Предыдущий исполнитель", executions[1]["prompt"])
        self.assertNotIn("Частичная находка", executions[1]["prompt"])
        state = self.state("review-limit")
        self.assertEqual([entry["account"] for entry in state["reviews"]], ["main", "second"])
        self.assertTrue(all(entry.get("started") and entry.get("finished") for entry in state["reviews"]))
        self.assertEqual((state["account"], state["thread"]), (working["account"], working["thread"]))
        self.assertIn("== review by second (", output)
        self.assertIn("Полное ревью на другой подписке", output)
        self.assertNotIn("Рабочий результат", output)

    def test_review_waits_for_account_reset_then_starts_fresh(self):
        self.configure()
        self.start("review-reset")
        self.wait("review-reset")
        working = self.state("review-reset")
        reset = time.time() + 4
        self.account(self.home, "A", reset + 20, used=99)
        self.account(self.other, "B", reset, used=99, auto_reset=True)
        reset = self.reset_after_ready(lambda: self.cli("review", "review-reset"), reset, 4)
        first = self.wait("review-reset")
        self.assert_sleeping("review-reset", first, reset, account="second")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())

        second = self.wait("review-reset", timeout=10, through_sleep=True)
        execution = self.review_invocations()[0]
        self.assertEqual(execution["home"], str(self.other))
        self.assertFalse(execution["resume"])
        self.assertEqual(self.state("review-reset")["thread"], working["thread"])
        self.assertIn("== review by second (", second)
        self.assertIn("Результат B", second)

    def test_stop_review_uses_supervisor_and_keeps_working_thread(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete"}, {"mode": "hold"}])
        self.start("review-stop")
        self.wait("review-stop")
        working = self.state("review-stop")
        self.cli("review", "review-stop")
        self.until(lambda: bool(self.review_invocations()))
        self.assertIn("running", self.cli("status"))
        self.cli("stop", "review-stop", timeout=20)
        self.wait("review-stop")
        state = self.state("review-stop")
        review = self.review_invocations()[0]
        self.assertEqual(state["outcome"], "stopped")
        self.assertFalse(state.get("pid"))
        self.assertEqual((state["account"], state["thread"]), (working["account"], working["thread"]))
        self.assertTrue(state["reviews"][0].get("finished"))
        self.assertTrue(any(event.get("command") == "signal" and event.get("thread") == review["thread"]
                            and event.get("signal") == signal.SIGINT for event in self.trace()))

    def test_review_falls_back_when_base_and_prompt_conflict(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Рабочий результат"},
            {"mode": "complete", "reply": "Ревью через обычный exec"}])
        self.start("review-fallback")
        self.wait("review-fallback")
        fixture = json.loads((self.home / "fixture.json").read_text())
        fixture["review_prompt_conflict"] = True
        (self.home / "fixture.json").write_text(json.dumps(fixture))
        working = self.state("review-fallback")
        self.cli("review", "review-fallback")
        output = self.wait("review-fallback")
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        rejected, fallback = executions[1:]
        self.assertEqual(rejected["mode"], "review_prompt_conflict")
        self.assertFalse(rejected["thread"])
        self.assertFalse(fallback["review"])
        self.assertFalse(fallback["resume"])
        self.assertEqual(fallback["cwd"], working["worktree"])
        self.assertEqual(Path(fallback["output"]).name, "review.md")
        self.assertEqual(fallback["prompt"], rejected["prompt"])
        self.assert_output_schema(rejected, "review-fallback", review=True)
        self.assert_output_schema(fallback, "review-fallback", review=True)
        self.assertIn("git diff " + working["base"], fallback["prompt"])
        self.assertEqual(fallback["ppid"], rejected["ppid"])
        state = self.state("review-fallback")
        self.assertEqual(state["thread"], working["thread"])
        self.assertEqual(len(state["reviews"]), 1)
        self.assertEqual(state["reviews"][0]["thread"], fallback["thread"])
        self.assertIn("== review by main (", output)
        self.assertIn("Ревью через обычный exec", output)
        self.assertNotIn("== errors:", output)

    def test_review_fallback_reuses_selection_snapshot_for_weekly_budget(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, used=8, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "weekly_used_before": 8, "weekly_used_after": 8},
            {"mode": "complete", "reply": self.review_result(False),
             "weekly_used_before": 10, "weekly_used_after": 12,
             "final_usage_after_completion": True}])
        slug = "review-fallback-weekly"
        self.start(slug, "--max-weekly", "3")
        self.wait(slug)
        self.assertEqual(self.state(slug).get("weekly_used_percent", 0), 0)
        fixture_path = self.home / "fixture.json"
        fixture = json.loads(fixture_path.read_text())
        fixture["review_prompt_conflict"] = True
        fixture_path.write_text(json.dumps(fixture))
        self.cli("review", slug)
        output = self.wait(slug)
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        rejected, fallback = executions[1:]
        self.assertEqual(rejected["mode"], "review_prompt_conflict")
        self.assertFalse(rejected["thread"])
        self.assertFalse(fallback["review"])
        self.assertEqual(fallback["prompt"], rejected["prompt"])
        state = self.state(slug)
        self.assertEqual(state["weekly_used_percent"], 4)
        self.assertEqual(state["outcome"], "budget_exhausted")
        self.assertEqual((state["turn_stop"]["weekly"], state["turn_stop"]["spent"]), (3, 4))
        self.assertIn("== turn stopped: weekly budget 3% exhausted (spent 4%)", output)
        self.assertEqual([row["weekly_spent"] for row in self.phase_telemetry_journal()], [0, 4])
        report = json.loads(self.cli("report", "--slug", slug, "--json"))
        self.assertEqual(report["tasks"][0]["weekly_used_percent"], 4)
        quota_calls = [event for event in self.trace() if event.get("command") == "quota-snapshot"]
        self.assertEqual(len(quota_calls), 4, "fallback must reuse the selection snapshot")

    def test_mid_turn_limit_sleeps_and_resumes_same_thread(self):
        self.configure()  # Omitting wait_for_reset enables automatic continuation.
        self.assertTrue(self.worker().accounts_config()["wait_for_reset"])
        reset = time.time() + 4
        self.account(self.home, "A", reset, auto_reset=True, runs=[
            {"mode": "usage_error", "messages": ["Работа до лимита"]},
            {"mode": "complete", "reply": "Продолжение после сброса завершено"}])
        self.account(self.other, "B", self.reset, used=99)
        reset = self.reset_after_ready(lambda: self.start("reset-resume"), reset, 4, after_exec=True)
        first = self.wait("reset-resume")
        # A loaded CI worker can wake before wait or the assertions run.
        # The recorded sleep proves the pause without requiring it to persist.
        sleeps = [entry for entry in self.state("reset-resume")["rotations"]
                  if entry.get("event") == "sleep"]
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0]["until"], reset + 0.1, places=3)
        self.assertEqual(sleeps[0]["account"], "main")
        self.assertTrue(sleeps[0]["reason"])
        self.assertIn("== sleeping until ", first)

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-resume")
        executions = self.invocations()
        self.assert_resumed_same_thread(executions[0], executions[1], self.home)
        for execution in executions:
            self.assert_output_schema(execution, "reset-resume")
        worker = self.worker()
        self.assertEqual(executions[1]["prompt"], worker.reset_resume_prompt(worker.accounts_config()))
        self.assertIn("Продолжение после сброса завершено", second)
        self.assertNotIn("auto-continue at", second)
        state = self.state("reset-resume")
        self.assertEqual(state["outcome"], "completed")
        self.assertFalse(state.get("sleeping"))
        self.assertEqual([entry["event"] for entry in state["rotations"]], ["sleep", "wake"])

    def test_exhausted_say_sleeps_and_sends_its_original_message(self):
        self.configure()
        self.start("reset-say")
        self.wait("reset-say")
        original_thread = self.state("reset-say")["thread"]
        reset = time.time() + 4
        self.account(self.home, "A", reset, used=99, auto_reset=True,
                     runs=[{"mode": "complete", "reply": "Новое сообщение выполнено"}])
        self.account(self.other, "B", self.reset, used=99)
        message = "Выполни именно это сообщение после сброса"
        reset = self.reset_after_ready(lambda: self.cli("say", "reset-say", message), reset, 4)
        first = self.wait("reset-say")
        self.assert_sleeping("reset-say", first, reset)
        self.assertEqual(len(self.invocations()), 1, "no exec may run before a subscription resets")
        self.assertIn("(Codex did not run this turn)", first)

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-say")
        execution = self.invocations()[-1]
        self.assertTrue(execution["resume"])
        self.assertEqual(execution["thread"], original_thread)
        self.assertEqual(execution["prompt"], message)
        self.assertIn("Новое сообщение выполнено", second)
        self.assertEqual(self.state("reset-say")["outcome"], "completed")

    def test_first_reset_on_other_account_starts_new_thread_with_handoff(self):
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", reset + 20, auto_reset=True,
                     runs=[{"mode": "usage_error", "messages": ["Незавершённая работа A"]}])
        self.account(self.other, "B", reset, used=99, auto_reset=True)
        reset = self.reset_after_ready(lambda: self.start("reset-switch"), reset, 4, after_exec=True)
        first = self.wait("reset-switch")
        self.assert_sleeping("reset-switch", first, reset, account="second")
        self.assertEqual(len(self.invocations()), 1)

        self.until(lambda: len(self.invocations()) == 2, timeout=10)
        second = self.wait("reset-switch")
        prompt = self.assert_rotation("reset-switch", second)
        self.assertIn(self.brief.read_text().strip(), prompt)
        self.assertIn("Предыдущий исполнитель остановился посреди работы", prompt)
        self.assertIn("Незавершённая работа A", prompt)
        self.assertEqual(self.state("reset-switch")["outcome"], "completed")

    def test_initially_exhausted_start_keeps_brief_and_wait_can_follow_sleep(self):
        self.configure()
        reset = time.time() + 4
        self.account(self.home, "A", reset, used=99, auto_reset=True)
        self.account(self.other, "B", self.reset, used=99)
        reset = self.reset_after_ready(lambda: self.start("reset-start"), reset, 4)
        first = self.wait("reset-start")
        self.assert_sleeping("reset-start", first, reset)
        self.assertFalse(self.invocations())

        second = self.wait("reset-start", through_sleep=True, timeout=10)
        self.assertEqual(len(self.invocations()), 1)
        execution = self.invocations()[0]
        self.assertFalse(execution["resume"])
        expected = (self.home / "claude-tasks" / "reset-start" / "brief.md").read_text()
        self.assertEqual(execution["prompt"], expected)
        self.assertIn("Результат A", second)
        self.assertNotIn("auto-continue at", second)
        self.assertEqual(self.state("reset-start")["outcome"], "completed")

    def test_stop_during_sleep_cancels_automatic_exec(self):
        self.env["CODEX_TASK_SLEEP_STEP"] = "60"
        self.configure()
        self.account(self.home, "A", self.reset, used=99, auto_reset=True)
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("stop-sleep")
        self.assert_sleeping("stop-sleep", self.wait("stop-sleep"), self.reset)
        self.assertFalse(self.invocations())
        self.cli("stop", "stop-sleep", timeout=20)
        self.wait("stop-sleep")
        state = self.state("stop-sleep")
        self.assertEqual(state["outcome"], "stopped")
        self.assertFalse(state.get("pid"))
        self.assertFalse(state.get("sleeping"))
        self.assertFalse(self.invocations())

    def test_say_during_sleep_requires_stop_first(self):
        self.configure()
        self.exhaust_accounts((self.home, "A", self.reset),
                              (self.other, "B", self.reset + 3600))
        self.start("say-sleep")
        state = self.assert_sleeping("say-sleep", self.wait("say-sleep"), self.reset)
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "say", "say-sleep", "Не отправлять"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        expected = datetime.datetime.fromtimestamp(state["sleeping"]["until"]).strftime("%Y-%m-%d %H:%M")
        self.assertIn("waiting for limit reset until " + expected +
                      "; stop first to send a new message", result.stderr)
        self.assertFalse((self.home / "claude-tasks" / "say-sleep" / "messages.jsonl").exists())
        self.assertEqual(self.state("say-sleep")["turns"], 1)
        self.assertFalse(self.invocations())
        self.cli("stop", "say-sleep", timeout=20)
        self.wait("say-sleep")

    def test_delayed_reset_sleeps_again_before_continuing(self):
        self.configure()
        with self.prepared_cli() as waiter:
            reset = time.time() + 3
            release = reset + 3
            self.account(self.home, "A", reset, auto_reset=True, release_at=release, runs=[
                {"mode": "usage_error"}, {"mode": "complete", "reply": "Задержанный сброс завершён"}])
            self.account(self.other, "B", self.reset, used=99)
            reset = self.reset_after_ready(lambda: self.start("delayed-reset"), reset, 3, after_exec=True)
            release = reset + 3
            self.assert_sleeping("delayed-reset", self.wait("delayed-reset"), reset)
            self.until(lambda: len([entry for entry in self.state("delayed-reset")["rotations"]
                                   if entry.get("event") == "sleep"]) >= 2, timeout=8)
            retry = self.wait("delayed-reset", prepared=waiter)
            self.assert_sleeping("delayed-reset", retry, release)
            self.assertEqual(len(self.invocations()), 1)
            self.assertTrue(any(event.get("command") == "quota-snapshot" and
                                event["primary"]["usedPercent"] == 99 and
                                event["primary"]["resetsAt"] == release for event in self.trace()))

            self.until(lambda: len(self.invocations()) == 2, timeout=10)
            output = self.wait("delayed-reset")
            self.assertIn("Задержанный сброс завершён", output)
            executions = self.invocations()
            self.assert_resumed_same_thread(executions[0], executions[1], self.home)
            state = self.state("delayed-reset")
            self.assertEqual(state["outcome"], "completed")
            sleeps = [entry for entry in state["rotations"] if entry.get("event") == "sleep"]
            self.assertEqual(len(sleeps), 2)
            self.assertLess(sleeps[0]["until"], sleeps[1]["until"])

    def test_mid_turn_rotation_preserves_work_and_followups(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "OLD-OUTPUT-OUTSIDE-LAST-THREE"},
            {"mode": "complete", "reply": "First follow-up completed"},
            {"mode": "limit", "modify": True,
             "messages": ["Последняя мысль один", "Последняя мысль два", "Последняя мысль три"]}])
        self.start("mid-turn")
        self.wait("mid-turn")
        self.cli("say", "mid-turn", "Первое сообщение Claude")
        self.wait("mid-turn")
        self.cli("say", "mid-turn", "Второе сообщение Claude")
        self.until(lambda: any(event.get("command") == "quota-written" for event in self.trace()))
        output = self.wait("mid-turn")
        prompt = self.assert_rotation("mid-turn", output)
        self.assertIn(self.brief.read_text().strip(), prompt)
        self.assertIn("Продолжение", prompt)
        self.assertIn("Первое сообщение Claude", prompt)
        self.assertIn("Второе сообщение Claude", prompt)
        self.assertLess(prompt.index("Первое сообщение Claude"), prompt.index("Второе сообщение Claude"))
        self.assertIn("checkpoint from account A", prompt)
        self.assertIn(" M tracked.txt", prompt)
        self.assertIn("?? pending.txt", prompt)
        self.assertIn("git status", prompt)
        self.assertIn("git diff", prompt)
        for message in ("Последняя мысль один", "Последняя мысль два", "Последняя мысль три"):
            self.assertIn(message, prompt)
        self.assertNotIn("OLD-OUTPUT-OUTSIDE-LAST-THREE", prompt)
        interrupted_thread = self.invocations()[-2]["thread"]
        self.assertTrue(any(event.get("command") == "signal" and event.get("thread") == interrupted_thread
                            and event.get("signal") == signal.SIGINT for event in self.trace()))
        messages = [json.loads(line) for line in
                    (self.home / "claude-tasks" / "mid-turn" / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertIn("Первое сообщение Claude", json.dumps(messages[0], ensure_ascii=False))
        self.assertIn("Второе сообщение Claude", json.dumps(messages[1], ensure_ascii=False))

    def test_usage_limit_failure_rotates_to_fresh_thread(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.start("usage-error")
        output = self.wait("usage-error")
        self.assert_rotation("usage-error", output)
        self.assertEqual(len(self.invocations()), 2)

    def test_all_accounts_exhausted_records_earliest_reset(self):
        self.configure(credits=False, wait_for_reset=False)
        self.account(self.home, "A", self.reset + 3600, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset, used=99)
        self.start("all-limited")
        output = self.wait("all-limited")
        state = self.state("all-limited")
        self.assertTrue(state.get("exhausted"))
        self.assertFalse(state.get("rotations"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("== all accounts at limit; earliest reset", output)
        self.assertIn(datetime.datetime.fromtimestamp(self.reset).strftime("%Y-%m-%d %H:%M"), output)
        self.assertIn("(second)", output)
        self.assertIn("continue with say after that", output)

    def test_all_accounts_exhausted_before_start_has_no_previous_executor(self):
        self.configure(wait_for_reset=False)
        self.exhaust_accounts((self.home, "A", self.reset + 3600),
                              (self.other, "B", self.reset))
        self.start("initially-limited")
        output = self.wait("initially-limited")
        self.assertIn("== all accounts at limit; earliest reset", output)
        self.assertTrue(self.state("initially-limited").get("exhausted"))
        self.assertFalse(self.state("initially-limited")["threads"])
        self.assertFalse(self.invocations())

        self.account(self.other, "B", self.reset)
        message = "Продолжить после первого сброса лимита"
        self.cli("say", "initially-limited", message)
        self.wait("initially-limited")
        self.assertEqual(self.state("initially-limited")["account"], "second")
        self.assertEqual(len(self.invocations()), 1)
        prompt = self.invocations()[0]["prompt"]
        self.assertIn(self.brief.read_text().strip(), prompt)
        self.assertIn("### Текущее сообщение — выполни его\n\n" + message, prompt)
        self.assertNotIn("Предыдущий исполнитель остановился посреди работы", prompt)

    def test_start_selects_earlier_reset_of_longest_window(self):
        self.configure()
        first = self.account(self.home, "A", self.reset + 7200)
        first["secondary"]["resetsAt"] = self.reset - 100
        (self.home / "fixture.json").write_text(json.dumps(first))
        self.account(self.other, "B", self.reset)
        self.start("choose-reset")
        self.wait("choose-reset")
        self.assertEqual(self.state("choose-reset")["account"], "second")
        self.assertEqual(self.invocations()[0]["home"], str(self.other))
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(self.other.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.other / "config.toml").read_bytes(), (self.home / "config.toml").read_bytes())
        for name in ("AGENTS.md", "skills", "rules"):
            self.assertTrue((self.other / name).is_symlink(), name)
            self.assertEqual((self.other / name).resolve(), (self.home / name).resolve())

    def test_unstarted_automatic_review_does_not_repeat_a_handled_rotation_failure(self):
        self.configure(language="en", wait_for_reset=False)
        self.account(self.home, "A", self.reset, runs=[{"mode": "usage_error"}])
        self.account(self.other, "B", self.reset + 3600, runs=[{
            "mode": "complete", "reply": self.work_result()}])
        check = self.root / "exhaust-accounts.py"
        check.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "for name in sys.argv[1:]:\n"
            "    path = Path(name)\n"
            "    value = json.loads(path.read_text())\n"
            "    value['primary']['usedPercent'] = 99\n"
            "    path.write_text(json.dumps(value))\n")
        command = shlex.join([PYTHON, "-I", str(check), str(self.home / "fixture.json"),
                              str(self.other / "fixture.json")])
        slug = "rotation-before-unstarted-review"
        self.start(slug, "--review", "--check", command)
        output = self.wait(slug)
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertEqual([entry["home"] for entry in executions],
                         [str(self.home), str(self.other)])
        self.assertEqual([entry["mode"] for entry in executions], ["usage_error", "complete"])
        self.assertFalse(self.review_invocations())
        self.assertIn("== check `" + command + "`: passed (exit 0,", output)
        self.assertIn("== all accounts at limit; earliest reset", output)
        self.assertIn("(Codex did not run review)", output)
        self.assertNotIn("== errors:", output)
        self.assertNotIn('"type": "turn.failed"', output)

    def test_say_without_an_exec_does_not_repeat_the_previous_failure(self):
        for wait_for_reset in (False, True):
            with self.subTest(wait_for_reset=wait_for_reset):
                self.configure(language="en", wait_for_reset=wait_for_reset)
                self.account(self.home, "A", self.reset, runs=[{"mode": "failed"}])
                self.account(self.other, "B", self.reset + 3600)
                slug = "failed-before-say-" + str(wait_for_reset).lower()
                before = len(self.invocations())
                self.start(slug)
                failed = self.cli("wait", slug, "--timeout", "10")
                self.assertNotIn("still running", failed)
                self.assertIn('"type": "turn.failed"', failed)
                self.assertIn("fixture task failed", failed)
                self.assertEqual(len(self.invocations()), before + 1)

                self.exhaust_accounts((self.home, "A", self.reset),
                                      (self.other, "B", self.reset + 3600))
                self.cli("say", slug, "Try again when an account is available")
                try:
                    output = self.wait(slug)
                    expected = "== sleeping until " if wait_for_reset else "== all accounts at limit"
                    self.assertIn(expected, output)
                    self.assertIn("(Codex did not run this turn)", output)
                    self.assertEqual(len(self.invocations()), before + 1)
                    self.assertNotIn("== errors:", output)
                    self.assertNotIn("fixture task failed", output)
                finally:
                    if wait_for_reset:
                        self.cli("stop", slug)

    def test_pause_after_a_turn_does_not_repeat_the_old_result(self):
        self.configure(wait_for_reset=False)
        self.start("pause-later")
        first = self.wait("pause-later")
        old = first.split("min\n", 1)[1].split("\n==", 1)[0].strip()
        self.assertTrue(old)
        self.exhaust_accounts((self.home, "A", self.reset + 3600),
                              (self.other, "B", self.reset))
        self.cli("say", "pause-later", "Ещё одно сообщение")
        output = self.wait("pause-later")
        self.assertIn("== all accounts at limit", output)
        self.assertIn("(Codex did not run this turn)", output)
        self.assertNotIn(old, output)

    def test_say_rotates_when_current_account_is_exhausted(self):
        self.configure()
        self.start("say-switch")
        self.wait("say-switch")
        self.cli("say", "say-switch", "Предыдущее сообщение Claude")
        self.wait("say-switch")
        self.account(self.home, "A", self.reset, used=99)
        self.cli("say", "say-switch", "Новое сообщение при смене подписки")
        output = self.wait("say-switch")
        prompt = self.assert_rotation("say-switch", output)
        self.assertIn("Предыдущий исполнитель остановился посреди работы", prompt)
        self.assertIn("Предыдущее сообщение Claude", prompt)
        self.assertIn("### Текущее сообщение — выполни его\n\nНовое сообщение при смене подписки", prompt)
        self.assertLess(prompt.index("Предыдущее сообщение Claude"),
                        prompt.index("Текущее сообщение — выполни его"))
        self.assertEqual(len(self.invocations()), 3)

    def test_without_accounts_file_resumes_legacy_thread(self):
        self.start("legacy")
        self.wait("legacy")
        state_path = self.home / "claude-tasks" / "legacy" / "state.json"
        state = json.loads(state_path.read_text())
        original_thread = state["thread"]
        state.pop("account", None)
        state.pop("threads", None)
        state_path.write_text(json.dumps(state))
        self.cli("say", "legacy", "Продолжить старую задачу")
        self.wait("legacy")
        invocations = self.invocations()
        self.assertEqual(len(invocations), 2)
        self.assertFalse(invocations[0]["resume"])
        self.assertTrue(invocations[1]["resume"])
        self.assertEqual(invocations[1]["thread"], original_thread)
        self.assertEqual(invocations[1]["prompt"], "Продолжить старую задачу")
        self.assertFalse(any(event.get("command") == "app-server" for event in self.trace()))
        self.assertFalse(self.state("legacy").get("rotations"))

    def test_stop_interrupts_child_without_rotation(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("stop-turn")
        self.until(lambda: bool(self.invocations()))
        self.cli("stop", "stop-turn", timeout=20)
        self.wait("stop-turn")
        state = self.state("stop-turn")
        self.assertFalse(state.get("rotations"))
        self.assertFalse(state.get("exhausted"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertTrue(any(event.get("command") == "signal" and event.get("signal") == signal.SIGINT
                            for event in self.trace()))

    def test_credits_true_keeps_the_same_live_exec(self):
        self.configure(credits=True)
        self.account(self.home, "A", self.reset,
                     runs=[{"mode": "hold", "rollout_used": 99}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("use-credits")
        # Two probes before exec, and two more when the local rollout reaches 99.
        self.until(lambda: len([event for event in self.trace()
                               if event.get("command") == "app-server"]) >= 4)
        state = self.state("use-credits")
        self.assertTrue(state.get("pid"))
        os.kill(state["pid"], 0)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(state.get("rotations"))
        self.assertFalse(state.get("exhausted"))
        self.assertFalse(any(event.get("command") == "signal" for event in self.trace()))
        self.cli("stop", "use-credits", timeout=20)
        self.wait("use-credits")
        self.assertEqual(len(self.invocations()), 1)

    def test_credits_false_stops_a_live_exhausted_exec(self):
        self.configure(credits=False, wait_for_reset=False)
        self.account(self.home, "A", self.reset,
                     runs=[{"mode": "hold", "rollout_used": 99}])
        self.account(self.other, "B", self.reset + 3600, used=99)
        self.start("no-credits")
        output = self.wait("no-credits")
        self.assertTrue(self.state("no-credits").get("exhausted"))
        self.assertFalse(self.state("no-credits").get("rotations"))
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("all accounts at limit", output)
        self.assertTrue(any(event.get("command") == "signal" and event.get("signal") == signal.SIGINT
                            for event in self.trace()))

    def test_accounts_identifies_duplicates_missing_login_and_rpc_error(self):
        self.configure()
        self.account(self.other, "A", self.reset)
        config_path = self.home / "claude-accounts.json"
        config = json.loads(config_path.read_text())
        for name, changes in (("logged-out", {"account": None}),
                              ("rpc-error", {"rpc_error": True})):
            home = self.root / name
            home.mkdir()
            fixture = self.account(home, name, self.reset)
            fixture.update(changes)
            (home / "fixture.json").write_text(json.dumps(fixture))
            config["accounts"].append({"name": name, "home": str(home)})
        config_path.write_text(json.dumps(config))
        output = self.cli("accounts")
        rows = {line.split("\t")[0]: line for line in output.splitlines()[1:]}
        self.assertIn("a@example.test", rows["main"])
        self.assertIn("pro", rows["main"])
        self.assertIn("123.45", rows["main"])
        self.assertIn("available", rows["main"])
        self.assertIn("same account as main", rows["second"])
        self.assertIn("not logged in", rows["logged-out"])
        self.assertIn("unknown", rows["rpc-error"])
        self.assertNotIn("not logged in", rows["rpc-error"])
        self.assertFalse(self.invocations())

    def test_account_read_distinguishes_explicit_logout_from_rpc_errors_and_timeouts(self):
        worker = self.worker()
        account = {"name": "main", "home": str(self.home)}
        cases = (("logged-out", [{"account": None}], False),
                 ("read-error", [RuntimeError("temporary account RPC unavailable")], None),
                 ("read-timeout", [TimeoutError("account RPC timed out")], None),
                 ("quota-error", [{"account": {"type": "chatgpt"}},
                                   RuntimeError("temporary quota RPC unavailable")], None))
        for name, replies, expected in cases:
            with self.subTest(case=name):
                with mock.patch.object(worker, "account_rpc") as account_rpc:
                    account_rpc.return_value.__enter__.return_value.side_effect = replies
                    info = worker.account_read(account)
                self.assertIs(info["logged_in"], expected)
                if expected is None:
                    self.assertTrue(info.get("error"))
                else:
                    self.assertFalse(info.get("error"))
        self.assertFalse(self.trace())

    def test_unknown_account_rpc_status_sleeps_then_recovers_without_requiring_login(self):
        self.env["CODEX_TASK_ACCOUNT_RETRY_SECONDS"] = "4"
        self.configure()
        for home in (self.home, self.other):
            path = home / "fixture.json"
            fixture = json.loads(path.read_text())
            fixture["rpc_error"] = True
            path.write_text(json.dumps(fixture))
        self.start("unknown-account-recovery")
        first = self.wait("unknown-account-recovery")
        state = self.state("unknown-account-recovery")
        self.assertEqual(state["outcome"], "sleeping")
        self.assertLessEqual(state["sleeping"]["until"] - time.time(), 4)
        self.assertFalse(state.get("needs_login"))
        self.assertFalse(self.invocations())
        self.assertNotIn("needs login", first)
        for home in (self.home, self.other):
            path = home / "fixture.json"
            fixture = json.loads(path.read_text())
            fixture.pop("rpc_error")
            path.write_text(json.dumps(fixture))
        output = self.wait("unknown-account-recovery", through_sleep=True, timeout=10)
        state = self.state("unknown-account-recovery")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("Результат A", output)
        self.assertFalse(state.get("needs_login"))

    def test_unknown_account_status_retries_six_times_at_five_minute_intervals(self):
        worker = self.worker()
        self.assertEqual(worker.ACCOUNT_RETRY_SECONDS, 300)
        self.assertEqual(worker.MAX_ACCOUNT_RETRIES, 6)
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        sleeps = []
        st = {"slug": "unknown-account-budget", "account": "main", "thread": "", "rotations": []}
        unknown = [{"name": "main", "logged_in": None, "error": "temporary account RPC unavailable"},
                   {"name": "second", "logged_in": None, "error": "temporary account RPC unavailable"}]

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep), \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots", return_value=unknown) as snapshots:
            chosen = worker.wait_for_account(st, unknown, cfg, "temporary account RPC unavailable", lambda: False)
        self.assertIsNone(chosen)
        self.assertEqual(snapshots.call_count, 6)
        self.assertEqual(sum(sleeps), 6 * 300)
        self.assertTrue(all(0 < seconds <= 60 for seconds in sleeps))
        self.assertEqual(st["outcome"], "failed")
        self.assertIn("6", st["error"])
        self.assertRegex(st["error"], r"(?i)(temporar|unavailable|unknown|retr)")
        self.assertFalse(st.get("needs_login"))

    def test_stop_during_unknown_account_retry_does_not_probe_or_require_login(self):
        worker = self.worker()
        cfg = {"wait_for_reset": True, "switch_at": 99}
        clock = [1000.0]
        stopped = [False]
        st = {"slug": "unknown-account-stop", "account": "main", "thread": "", "rotations": []}
        unknown = [{"name": "main", "logged_in": None, "error": "temporary account RPC unavailable"}]

        def sleep(seconds):
            clock[0] += seconds
            stopped[0] = True

        with mock.patch.object(worker, "SLEEP_STEP", 60), \
                mock.patch.object(worker.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep) as sleeper, \
                mock.patch.object(worker, "save"), \
                mock.patch.object(worker, "snapshots") as snapshots:
            chosen = worker.wait_for_account(st, unknown, cfg, "temporary account RPC unavailable", lambda: stopped[0])
        self.assertIsNone(chosen)
        sleeper.assert_called_once_with(60)
        snapshots.assert_not_called()
        self.assertFalse(st.get("needs_login"))

    def test_signal_group_falls_back_to_a_live_leader_on_permission_error(self):
        worker = self.worker()
        for live in (True, False):
            with self.subTest(live=live):
                proc = mock.Mock(pid=IMPOSSIBLE_PID)
                proc.poll.return_value = None if live else 0
                with mock.patch.object(worker.os, "killpg", side_effect=PermissionError), \
                        mock.patch.object(worker.os, "kill") as kill:
                    self.assertIs(worker.signal_group(proc, 0), live)
                    kill.assert_not_called()
                    self.assertIs(worker.signal_group(proc, signal.SIGTERM), live)
                    if live:
                        kill.assert_called_once_with(proc.pid, signal.SIGTERM)
                    else:
                        kill.assert_not_called()
