"""Review angles and automatic fix cycle tests."""
import json
import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from rotation_support import PYTHON, SCRIPT, RotationTestCase


class ReviewFixTests(RotationTestCase):
    def advance_review_base(self, files):
        checkout = self.root / "base-checkout"
        if not checkout.exists():
            self.git("clone", "--quiet", str(self.root / "origin.git"), str(checkout))
        for name, contents in files.items():
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self.git("-C", str(checkout), "add", "--", *files)
        self.git("-C", str(checkout), "commit", "--quiet", "-m", "Advance review base")
        self.git("-C", str(checkout), "push", "--quiet", "origin", "main")
        return self.git("-C", str(checkout), "rev-parse", "HEAD")

    def test_base_merge_fetches_advanced_base_and_leaves_clean_divergence_unmerged(self):
        self.configure(language="en")
        gate = self.root / "finish-work"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "delay_gate": str(gate),
             "write_files": {"task.txt": "task change\n"}, "commit_files": ["task.txt"],
             "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        calls = self.record_git_calls()
        self.cli("start", "base-clean", "--brief", str(self.brief), "--repo", str(self.repo),
                 "--review", timeout=60)
        self.until(lambda: len(self.invocations()) == 1, timeout=60)
        upstream = self.advance_review_base({"upstream.txt": "base change\n"})
        gate.touch()
        self.wait("base-clean")

        state = self.state("base-clean")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertEqual([(entry["base"], entry["outcome"]) for entry in state["base_merges"]],
                         [("origin/main", "passed")])
        self.assertEqual(self.git("rev-parse", "origin/main"), upstream)
        self.assertEqual(len(self.git("-C", state["worktree"], "rev-list", "--parents", "-n", "1", "HEAD")
                             .split()), 2, "a clean merge check must not merge the base")
        self.assertFalse((Path(state["worktree"]) / "upstream.txt").exists())
        commands = [json.loads(line)["argv"] for line in calls.read_text().splitlines()]
        self.assertTrue(any("fetch" in command for command in commands))
        self.assertTrue(any("merge-tree" in command and "--write-tree" in command
                            and command[-2:] == ["HEAD", "origin/main"] for command in commands))

    def test_base_merge_conflict_uses_a_merge_round_then_saved_check_and_first_review(self):
        self.configure(language="en")
        gate = self.root / "finish-work"
        command = "printf 'full-saved-check\\n' >> check-runs"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "delay_gate": str(gate),
             "write_files": {"tracked.txt": "task change\n"}, "commit_files": ["tracked.txt"],
             "reply": self.work_result()},
            {"mode": "complete", "merge_base": "origin/main",
             "write_files": {"tracked.txt": "task change\nbase change\n"},
             "commit_files": ["tracked.txt"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.cli("start", "base-merge", "--brief", str(self.brief), "--repo", str(self.repo),
                 "--review", "--fix-rounds", "1", "--check", command, timeout=60)
        self.until(lambda: len(self.invocations()) == 1, timeout=60)
        upstream = self.advance_review_base({"tracked.txt": "base change\n"})
        gate.touch()
        output = self.wait("base-merge")

        state = self.state("base-merge")
        work, merge, review = self.invocations()
        self.assertEqual([event["review"] for event in (work, merge, review)], [False, False, True])
        self.assertTrue(merge["resume"])
        self.assertEqual(merge["thread"], work["thread"])
        self.assertIn("origin/main", merge["prompt"])
        self.assertIn("tracked.txt", merge["prompt"])
        self.assertIn("merge commit", merge["prompt"])
        self.assertIn("rebase", merge["prompt"])
        self.assertIn("force-push", merge["prompt"])
        self.assertIn(command, merge["prompt"])
        self.assertEqual([entry["outcome"] for entry in state["base_merges"]], ["conflict", "passed"])
        self.assertEqual(state["base_merges"][0]["files"], ["tracked.txt"])
        self.assertEqual(len(state["rounds"]), 1)
        self.assertIsNone(state["rounds"][0]["review"])
        self.assertEqual(state["rounds"][0]["base_merge"], state["base_merges"][0])
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "passed")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(),
                         ["full-saved-check", "full-saved-check"])
        parents = self.git("-C", state["worktree"], "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[2], upstream)
        self.assertEqual((Path(state["worktree"]) / "tracked.txt").read_text(),
                         "task change\nbase change\n")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertIn("== round 1:", output)

    def test_base_merge_conflict_without_rounds_stops_before_start_or_say_review(self):
        self.configure(language="en")
        for label, command, rounds in (("omitted", "start", ()),
                                       ("zero", "say", ("--fix-rounds", "0"))):
            with self.subTest(label=label):
                (self.home / "exec-count").unlink(missing_ok=True)
                gate = self.root / ("finish-work-" + label)
                runs = [{"mode": "complete", "delay_gate": str(gate),
                         "write_files": {"tracked.txt": "task " + label + "\n"},
                         "commit_files": ["tracked.txt"], "reply": self.work_result()}]
                if command == "say":
                    runs.insert(0, {"mode": "complete", "reply": self.work_result()})
                self.account(self.home, "A", self.reset, runs=runs)
                before = len(self.invocations())
                slug = "base-stop-" + label
                if command == "start":
                    self.cli("start", slug, "--brief", str(self.brief), "--repo", str(self.repo),
                             "--review", *rounds, timeout=60)
                else:
                    self.cli("start", slug, "--brief", str(self.brief), "--repo", str(self.repo), timeout=60)
                    self.wait(slug)
                    self.cli("say", slug, "Finish the task", "--review", *rounds, timeout=60)
                self.until(lambda: len(self.invocations()) == before + len(runs), timeout=60)
                self.advance_review_base({"tracked.txt": "base " + label + "\n"})
                gate.touch()
                output = self.wait(slug)
                state = self.state(slug)
                self.assertFalse(any(event["review"] for event in self.invocations()[before:]))
                self.assertEqual(state["auto_fix_stop"], "base conflict")
                self.assertEqual(state["review_skipped"], "base conflict")
                self.assertFalse(state.get("rounds"))
                self.assertEqual(state["base_merges"][-1]["files"], ["tracked.txt"])
                self.assertIn("origin/main", output)
                self.assertIn("tracked.txt", output)
                self.assertIn("base conflict", output)

    def test_base_merge_checks_again_after_a_fix_and_stops_when_rounds_are_exhausted(self):
        self.configure(language="en")
        gate = self.root / "finish-fix"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "delay_gate": str(gate),
             "write_files": {"tracked.txt": "task fix\n"}, "commit_files": ["tracked.txt"],
             "reply": self.work_result()}])
        self.cli("start", "base-exhausted", "--brief", str(self.brief), "--repo", str(self.repo),
                 "--review", "--fix-rounds", "1", timeout=60)
        self.until(lambda: len(self.invocations()) == 3, timeout=60)
        self.advance_review_base({"tracked.txt": "base change\n"})
        gate.touch()
        output = self.wait("base-exhausted")

        state = self.state("base-exhausted")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False])
        self.assertEqual([entry["outcome"] for entry in state["base_merges"]], ["passed", "conflict"])
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["auto_fix_stop"], "base conflict")
        self.assertIn("origin/main", output)
        self.assertIn("tracked.txt", output)

    def test_base_merge_conflict_after_a_fix_consumes_the_next_round_before_repeat_review(self):
        self.configure(language="en")
        gate = self.root / "finish-fix"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "delay_gate": str(gate),
             "write_files": {"tracked.txt": "task fix\n"}, "commit_files": ["tracked.txt"],
             "reply": self.work_result()},
            {"mode": "complete", "merge_base": "origin/main",
             "write_files": {"tracked.txt": "task fix\nbase change\n"},
             "commit_files": ["tracked.txt"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.cli("start", "base-repeat", "--brief", str(self.brief), "--repo", str(self.repo),
                 "--review", "--fix-rounds", "2", timeout=60)
        self.until(lambda: len(self.invocations()) == 3, timeout=60)
        self.advance_review_base({"tracked.txt": "base change\n"})
        gate.touch()
        self.wait("base-repeat")

        state = self.state("base-repeat")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False, False, True])
        self.assertEqual([entry["outcome"] for entry in state["base_merges"]],
                         ["passed", "conflict", "passed"])
        self.assertEqual(len(state["rounds"]), 2)
        self.assertIsNotNone(state["rounds"][0]["review"])
        self.assertIsNone(state["rounds"][1]["review"])
        self.assertEqual(state["rounds"][1]["base_merge"]["files"], ["tracked.txt"])
        self.assertEqual(state["auto_fix_stop"], "clean")

    def test_base_merge_fetch_failure_is_recorded_and_review_still_runs(self):
        self.configure(language="en")
        gate = self.root / "finish-work"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "delay_gate": str(gate),
             "write_files": {"tracked.txt": "task change\n"}, "commit_files": ["tracked.txt"],
             "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.cli("start", "base-fetch-failed", "--brief", str(self.brief), "--repo", str(self.repo),
                 "--review", timeout=60)
        self.until(lambda: len(self.invocations()) == 1, timeout=60)
        self.advance_review_base({"tracked.txt": "base change\n"})
        self.git("remote", "set-url", "origin", str(self.root / "missing-origin.git"))
        gate.touch()
        self.wait("base-fetch-failed")

        state = self.state("base-fetch-failed")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertEqual(len(state["base_merges"]), 1)
        self.assertEqual(state["base_merges"][0]["outcome"], "skipped")
        self.assertIn("fetch", state["base_merges"][0]["reason"])
        self.assertEqual(state["outcome"], "completed")
        self.assertFalse(state.get("auto_fix_stop"))

    def test_base_merge_unavailable_merge_tree_is_recorded_and_review_still_runs(self):
        self.configure(language="en")
        calls = self.record_git_calls()
        wrapper = self.root / "git"
        wrapper.write_text(wrapper.read_text().replace(
            "os.execv(",
            "if 'merge-tree' in sys.argv:\n"
            "    print('fixture Git has no --write-tree support', file=sys.stderr)\n"
            "    sys.exit(129)\n"
            "os.execv("))
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.cli("start", "base-old-git", "--brief", str(self.brief), "--repo", str(self.repo),
                 "--review", timeout=60)
        self.wait("base-old-git")

        state = self.state("base-old-git")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertEqual(state["base_merges"][0]["outcome"], "skipped")
        self.assertIn("merge-tree", state["base_merges"][0]["reason"])
        self.assertEqual(state["outcome"], "completed")
        self.assertTrue(any("merge-tree" in json.loads(line)["argv"]
                            for line in calls.read_text().splitlines()))

    def test_base_merge_does_not_gate_standalone_review(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tracked.txt": "task change\n"},
             "commit_files": ["tracked.txt"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.cli("start", "base-standalone", "--brief", str(self.brief), "--repo", str(self.repo), timeout=60)
        self.wait("base-standalone")
        self.advance_review_base({"tracked.txt": "base change\n"})
        calls = self.record_git_calls()
        self.cli("review", "base-standalone", timeout=60)
        self.wait("base-standalone")

        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertFalse(self.state("base-standalone").get("base_merges"))
        commands = [json.loads(line)["argv"] for line in calls.read_text().splitlines()]
        self.assertFalse(any("fetch" in command or "merge-tree" in command for command in commands))

    def provenance_review(self, locations):
        review = self.review_result()
        review["findings"] = [dict(review["findings"][0], id="F" + str(index),
                                   severity=severity, file=filename, line=line)
                              for index, (severity, filename, line) in enumerate(locations, 1)]
        return review

    def provenance_runs(self, repeated_review, fix_files=None, workspace=False):
        source = "".join("VALUE_%s = %s\n" % (number, number) for number in range(1, 31))
        filename = "src/provenance.py"
        changes = fix_files if fix_files is not None else {
            filename: source.replace("VALUE_15 = 15\n", "VALUE_15 = 150\n")}
        initial_review = self.provenance_review([("P2", filename, 5)])
        decision = [{"id": "F1", "decision": "fixed", "note": "Corrected the reported code."}]
        work = {"mode": "complete", "write_files": {filename: source},
                "reply": self.work_result(commit_message="Implement provenance fixture" if workspace else "")}
        fix = {"mode": "complete", "write_files": changes,
               "reply": self.work_result(findings=decision,
                                         commit_message="Fix provenance fixture" if workspace else "")}
        if not workspace:
            work["commit_files"] = [filename]
            fix["commit_files"] = list(changes)
        return [work, {"mode": "complete", "reply": initial_review}, fix,
                {"mode": "complete", "reply": repeated_review},
                {"mode": "complete", "reply": self.work_result(findings=decision)},
                {"mode": "complete", "reply": self.review_result(False)}]

    def test_review_angles_use_fresh_focused_threads_and_merge_duplicate_findings(self):
        self.configure(language="en")
        self.configure_notify()
        correctness = self.review_result()
        concurrency = self.review_result()
        concurrency["findings"][0]["title"] = correctness["findings"][0]["title"].upper()
        concurrency["findings"][1].update(
            id="F9", severity="P2", file="src/worker.py", line=28,
            title="Concurrent recovery overwrites live state")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": correctness},
            {"mode": "complete", "reply": concurrency}])

        self.start("angles-merge", "--review-angles", "correctness,concurrency")
        output = self.wait("angles-merge")
        executions = self.invocations()
        self.assertEqual([entry["review"] for entry in executions], [False, True, True])
        self.assertEqual(len({entry["thread"] for entry in executions}), 3)
        self.assertTrue(all(not entry["resume"] for entry in executions))
        self.assertEqual(len({entry["ppid"] for entry in executions}), 1)
        self.assertIn("Focus on behavior and acceptance criteria", executions[1]["prompt"])
        self.assertIn("Focus on races, processes, signals", executions[2]["prompt"])
        self.assertNotIn("Focus on races, processes, signals", executions[1]["prompt"])
        for entry in executions[1:]:
            self.assertIn(str(self.home / "claude-tasks" / "angles-merge" / "brief.md"), entry["prompt"])
            self.assert_output_schema(entry, "angles-merge", review=True)
        state = self.state("angles-merge")
        self.assertIn(self.worker().prompts.review_prompt(state, {"language": "en"}), executions[-1]["prompt"])
        self.assertEqual([entry["angle"] for entry in state["reviews"]],
                         ["correctness", "concurrency"])
        self.assertEqual([entry["thread"] for entry in executions[1:]],
                         [entry["thread"] for entry in state["reviews"]],
                         "Review threads must appear in the append-only trace in preset order")
        result = state["review_result"]
        self.assertEqual(result["verdict"], "defects")
        self.assertEqual([entry["angle"] for entry in result["angles"]],
                         ["correctness", "concurrency"])
        self.assertEqual([finding["id"] for finding in result["findings"]], ["F1", "F2", "F3"])
        self.assertEqual([finding["angle"] for finding in result["findings"]],
                         ["correctness,concurrency", "correctness", "concurrency"])
        self.assertEqual(result["findings"][0]["title"], correctness["findings"][0]["title"])
        for angle in ("correctness", "concurrency"):
            self.assertRegex(output, r"== review angle " + angle + r" by main \(\d+(?:\.\d+)? min\): 2 defects")
        self.assertIn("[correctness,concurrency]", output)
        self.assertIn("[concurrency]", output)
        self.assertEqual(output.count(correctness["findings"][0]["detail"]), 1)
        event = self.notification("angles-merge", "review_done")
        self.assertEqual(event["angles"], ["correctness", "concurrency"])
        self.assertEqual((event["verdict"], event["findings"]), ("defects", 3))

    def test_review_angles_auto_counts_committed_and_dirty_lines_at_default_and_configured_threshold(self):
        cases = (("below", 399, None, 1), ("threshold", 400, None, 3),
                 ("configured", 14, 14, 3))
        for label, lines, threshold, expected_count in cases:
            with self.subTest(label=label):
                settings = {} if threshold is None else {"review_angles_min_lines": threshold}
                self.configure(language="en", **settings)
                (self.home / "exec-count").unlink(missing_ok=True)
                committed = min(200, lines // 2)
                contents = "".join("line %s\n" % index for index in range(lines))
                initial = "".join("line %s\n" % index for index in range(committed))
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "write_files": {"size.txt": initial},
                     "commit_files": ["size.txt"], "post_commit_write_files": {"size.txt": contents},
                     "reply": self.work_result()},
                    {"mode": "complete", "reply": self.review_result(False)}])
                count = len(self.review_invocations())
                slug = "angles-auto-" + label
                self.start(slug, "--review-angles", "auto")
                output = self.wait(slug)
                reviews = self.review_invocations()[count:]
                self.assertEqual(len(reviews), expected_count)
                state = self.state(slug)
                if expected_count == 3:
                    self.assertEqual([entry["angle"] for entry in state["reviews"]],
                                     ["correctness", "concurrency", "tests"])
                    self.assertIn("Focus on ineffective or weakened tests", reviews[2]["prompt"])
                else:
                    self.assertIn("== review by main (", output)
                    self.assertNotIn("== review angle", output)

    def test_review_angles_fix_round_keeps_only_finding_angles_and_correctness(self):
        self.configure(language="en")
        defect = self.review_result()
        defect["findings"] = defect["findings"][:1]
        fixed = self.work_result(findings=[
            {"id": "F1", "decision": "fixed", "note": "Serialize state recovery."}])
        clean = self.review_result(False)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": clean},
            {"mode": "complete", "reply": defect},
            {"mode": "complete", "reply": clean},
            {"mode": "complete", "reply": fixed},
            {"mode": "complete", "reply": clean},
            {"mode": "complete", "reply": clean}])

        self.start("angles-fix", "--review-angles", "correctness,concurrency,tests", "--fix-rounds", "1")
        self.wait("angles-fix")
        state = self.state("angles-fix")
        self.assertEqual([entry["angle"] for entry in state["reviews"]],
                         ["correctness", "concurrency", "tests", "correctness", "concurrency"])
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"]), 1)
        previous = state["rounds"][0]["review"]
        self.assertEqual(previous["findings"][0]["angle"], "concurrency")
        self.assertEqual(state["review_result"]["verdict"], "clean")
        executions = self.invocations()
        self.assertEqual(len(executions), 7)
        self.assertTrue(executions[4]["resume"])
        self.assertEqual(executions[4]["thread"], executions[0]["thread"])
        self.assertIn("F1", executions[4]["prompt"])
        self.assertIn(defect["findings"][0]["title"], executions[4]["prompt"])
        self.assertEqual(len({entry["thread"] for entry in self.review_invocations()}), 5)

    def test_review_angles_single_and_auto_small_stay_single_after_a_fix_round(self):
        for selection in ("concurrency", "auto"):
            with self.subTest(selection=selection):
                self.configure(language="en")
                (self.home / "exec-count").unlink(missing_ok=True)
                defect = self.review_result()
                defect["findings"] = defect["findings"][:1]
                fixed = self.work_result(findings=[
                    {"id": "F1", "decision": "fixed", "note": "Fix the reported behavior."}])
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": defect},
                    {"mode": "complete", "reply": fixed},
                    {"mode": "complete", "reply": self.review_result(False)}])
                count = len(self.invocations())
                slug = "angles-single-fix-" + selection
                self.start(slug, "--review-angles", selection, "--fix-rounds", "1")
                self.wait(slug)
                executions = self.invocations()[count:]
                self.assertEqual([entry["review"] for entry in executions], [False, True, False, True])
                self.assertTrue(executions[2]["resume"])
                self.assertEqual(executions[2]["thread"], executions[0]["thread"])
                state = self.state(slug)
                self.assertEqual(len(state["reviews"]), 2)
                self.assertEqual(state["auto_fix_stop"], "clean")
                self.assertEqual(len(state["rounds"]), 1)

    def test_review_angles_auto_counts_untracked_text_without_adding_it_to_git(self):
        self.configure(language="en", review_angles_min_lines=10)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"new.txt": "new line\n" * 10},
             "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("angles-untracked", "--review-angles", "auto")
        self.wait("angles-untracked")
        state = self.state("angles-untracked")
        self.assertEqual([entry["angle"] for entry in state["reviews"]],
                         ["correctness", "concurrency", "tests"])
        worktree = Path(state["worktree"])
        self.assertEqual((worktree / "new.txt").read_text(), "new line\n" * 10)
        tracked = subprocess.run(["git", "ls-files", "--", "new.txt"], cwd=worktree,
                                 env=self.env, text=True, capture_output=True, check=True)
        self.assertEqual(tracked.stdout, "")

    def test_review_angles_minimum_lines_rejects_nonpositive_and_noninteger_settings(self):
        for threshold in (0, -1, True, "14"):
            with self.subTest(threshold=threshold):
                self.configure(review_angles_min_lines=threshold)
                result = subprocess.run([PYTHON, "-I", str(SCRIPT), "start", "angles-invalid-threshold",
                                         "--brief", str(self.brief), "--repo", str(self.repo),
                                         "--review-angles", "auto"],
                                        cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("review_angles_min_lines", result.stdout + result.stderr)
                self.assertEqual(self.invocations(), [])
                self.assertFalse((self.home / "claude-tasks" / "angles-invalid-threshold").exists())

    def test_review_angles_size_count_disables_git_drivers_and_skips_binary_files_and_symlinks(self):
        self.commit_fixture({".gitattributes": "tracked.txt filter=tripwire.worker diff=tripwire\n"})
        marker = self.root / "git-driver-ran"
        driver = self.root / "git-driver.py"
        driver.write_text("from pathlib import Path\n"
                          "Path(" + repr(str(marker)) + ").write_text('driver ran')\n"
                          "raise SystemExit(1)\n")
        command = shlex.join([PYTHON, str(driver)])
        for key in ("filter.tripwire.worker.clean", "filter.tripwire.worker.process",
                    "diff.tripwire.textconv", "diff.external"):
            self.git("config", key, command)
        self.git("config", "filter.tripwire.worker.required", "true")
        (self.repo / "tracked.txt").write_text("changed\nmore\n")
        worker = self.worker()
        state = {"review_angles": "auto", "base": "origin/main", "worktree": str(self.repo)}
        config = {"review_angles_min_lines": 3}
        expected = ["correctness", "concurrency", "tests"]
        self.assertEqual(worker.review_cycle.selected_review_angles(state, config), expected)
        self.assertFalse(marker.exists(), "counting a diff must not execute repository Git drivers")
        (self.repo / "tracked.txt").write_text("initial\n")
        (self.repo / "binary.bin").write_bytes(b"\0" + b"binary line\n" * 20)
        outside = self.root / "outside.txt"
        outside.write_text("outside line\n" * 20)
        (self.repo / "outside-link.txt").symlink_to(outside)
        self.assertEqual(worker.review_cycle.selected_review_angles(state, config), [])
        (self.repo / "new.txt").write_text("one\ntwo\nthree")
        self.assertEqual(worker.review_cycle.selected_review_angles(state, config), expected)
        self.assertFalse(marker.exists())

    def test_say_and_standalone_review_accept_explicit_angles_and_keep_working_thread(self):
        self.configure(language="en")
        clean = self.review_result(False)
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": clean},
            {"mode": "complete", "reply": clean},
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": clean}])
        self.start("angles-followup")
        self.wait("angles-followup")
        working = self.state("angles-followup")["thread"]
        self.cli("review", "angles-followup", "--review-angles", "security,tests")
        self.wait("angles-followup")
        self.assertIn("Focus on secrets, shell and path injection", self.review_invocations()[0]["prompt"])
        self.assertIn("Focus on ineffective or weakened tests", self.review_invocations()[1]["prompt"])
        self.cli("say", "angles-followup", "Check the followup", "--review-angles", "correctness")
        self.wait("angles-followup")
        executions = self.invocations()
        self.assertEqual([entry["review"] for entry in executions], [False, True, True, False, True])
        self.assertTrue(executions[3]["resume"])
        self.assertEqual(executions[3]["thread"], working)
        state = self.state("angles-followup")
        self.assertEqual(state["thread"], working)
        self.assertEqual([entry["angle"] for entry in state["reviews"]], ["security", "tests", "correctness"])
        self.assertEqual(len({entry["thread"] for entry in self.review_invocations()}), 3)

    def test_review_angles_unknown_and_scout_options_fail_without_a_turn(self):
        self.configure()
        self.start("angles-validation")
        self.wait("angles-validation")
        count = len(self.invocations())
        state = self.state("angles-validation")
        commands = (("start", "angles-invalid", "--brief", str(self.brief), "--repo", str(self.repo)),
                    ("say", "angles-validation", "Review this"),
                    ("review", "angles-validation"))
        for command in commands:
            with self.subTest(command=command[0]):
                result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(command) +
                                        ["--review-angles", "correctness,unknown-angle"],
                                        cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unknown-angle", result.stdout + result.stderr)
                self.assertEqual(len(self.invocations()), count)
                self.assertEqual(self.state("angles-validation"), state)
                self.assertFalse((self.home / "claude-tasks" / "angles-invalid").exists())
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), "scout", "angles-scout-invalid",
                                 "Find the parser", "--repo", str(self.repo), "--review-angles", "auto"],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review-angles", result.stdout + result.stderr)
        self.assertEqual(len(self.invocations()), count)
        self.assertFalse((self.home / "claude-tasks" / "angles-scout-invalid").exists())

    def test_review_angles_second_angle_quota_rotates_and_preserves_the_full_summary(self):
        self.configure(language="en")
        correctness = self.review_result()
        correctness["findings"] = correctness["findings"][:1]
        concurrency = self.review_result()
        concurrency["findings"] = concurrency["findings"][1:]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(),
             "token_usage_before": self.telemetry_usage(100, 20, 10, 1),
             "token_usage_after": self.telemetry_usage(150, 40, 30, 5),
             "weekly_used_before": 10, "weekly_used_after": 12},
            {"mode": "complete", "reply": correctness,
             "token_usage_before": self.telemetry_usage(1000, 200, 100, 20),
             "token_usage_after": self.telemetry_usage(1100, 250, 120, 25),
             "weekly_used_before": 12, "weekly_used_after": 14},
            {"mode": "usage_error", "token_usage_before": self.telemetry_usage(2000, 400, 200, 40),
             "token_usage_after": self.telemetry_usage(2030, 410, 210, 42),
             "weekly_used_before": 14, "weekly_used_after": 99}])
        self.account(self.other, "B", self.reset + 3600, used=20, runs=[
            {"mode": "complete", "reply": concurrency,
             "token_usage_before": self.telemetry_usage(5000, 1000, 500, 100),
             "token_usage_after": self.telemetry_usage(5200, 1100, 550, 115),
             "weekly_used_before": 20, "weekly_used_after": 23}])

        self.start("angles-rotate")
        self.wait("angles-rotate")
        working = self.state("angles-rotate")["thread"]
        self.cli("review", "angles-rotate", "--review-angles", "correctness,concurrency")
        output = self.wait("angles-rotate")
        reviews = self.review_invocations()
        self.assertEqual([entry["home"] for entry in reviews],
                         [str(self.home), str(self.home), str(self.other)])
        self.assertEqual(reviews[1]["prompt"], reviews[2]["prompt"])
        self.assertEqual(len({entry["thread"] for entry in reviews}), 3)
        self.assertTrue(all(not entry["resume"] for entry in reviews))
        state = self.state("angles-rotate")
        self.assertEqual(state["thread"], working)
        self.assertEqual([entry["angle"] for entry in state["reviews"]],
                         ["correctness", "concurrency", "concurrency"])
        self.assertEqual([entry["account"] for entry in state["reviews"]], ["main", "main", "second"])
        result = state["review_result"]
        self.assertEqual(result["verdict"], "defects")
        self.assertEqual([entry["id"] for entry in result["findings"]], ["F1", "F2"])
        self.assertEqual([entry["angle"] for entry in result["findings"]], ["correctness", "concurrency"])
        self.assertEqual([entry["outcome"] for entry in result["angles"]], ["completed", "completed"])
        self.assertIn("== review angle concurrency by second (", output)
        self.assertIn(correctness["findings"][0]["title"], output)
        self.assertIn(concurrency["findings"][0]["title"], output)
        self.until(lambda: len(self.phase_telemetry_journal()) == 3)
        rows = self.phase_telemetry_journal()
        self.assertEqual([row["phase"] for row in rows], ["work", "review", "review"])
        self.assertEqual([row["angle"] for row in rows], [None, "correctness", "concurrency"])
        rotated = rows[-1]
        self.assertEqual((rotated["rotations"], rotated["account"], rotated["thread"]),
                         (1, "second", reviews[-1]["thread"]))
        self.assertEqual(rotated["tokens"], {"input": 230, "cached_input": 110,
                                              "output": 60, "reasoning": 17})
        self.assertEqual([entry["account"] for entry in rotated["usage"]], ["main", "second"])
        self.assertEqual([entry["thread"] for entry in rotated["usage"]],
                         [reviews[1]["thread"], reviews[2]["thread"]])
        report = json.loads(self.cli("report", "--slug", "angles-rotate", "--json"))
        self.assertEqual(report["summary"]["turns"], 3)
        self.assertEqual(report["summary"]["tokens"]["total"], 480)
        self.assertEqual(report["summary"]["weekly_used_percent"], 92)
        self.assertEqual({row["account"]: (row["turns"], row["tokens"]["total"], row["weekly_used_percent"])
                          for row in report["accounts"]}, {"main": (3, 230, 89), "second": (1, 250, 3)})

    def test_model_cybersecurity_refusal_before_or_after_thread_does_not_rotate(self):
        self.configure()
        message = "This content was flagged for possible cybersecurity risk. " + "x" * 210 + "omitted suffix"
        for before_thread in (False, True):
            with self.subTest(before_thread=before_thread):
                (self.home / "exec-count").unlink(missing_ok=True)
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "refused", "before_thread": before_thread, "error": message}])
                count = len(self.invocations())
                slug = "refused-" + ("before" if before_thread else "after")
                self.start(slug, "--review")
                output = self.wait(slug)
                state = self.state(slug)
                self.assertEqual(state["outcome"], "refused")
                self.assertEqual(len(self.invocations()) - count, 1)
                self.assertFalse(state.get("rotations"))
                self.assertFalse(state.get("reviews"))
                self.assertIn("== refused by the model: " + message[:200], output)
                self.assertNotIn("omitted suffix", output)

    def test_review_angles_refusal_keeps_other_angles_and_marks_the_summary(self):
        self.configure(language="en")
        message = "This content was flagged for possible cybersecurity risk. Try a different prompt."
        defects = self.review_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "refused", "error": message},
            {"mode": "complete", "reply": defects},
            {"mode": "complete", "reply": self.review_result(False)}])

        self.start("angles-refused", "--review-angles", "security,concurrency,tests")
        output = self.wait("angles-refused")
        state = self.state("angles-refused")
        self.assertEqual(len(self.invocations()), 4)
        self.assertFalse(state.get("rotations"))
        self.assertEqual([entry["angle"] for entry in state["reviews"]], ["security", "concurrency", "tests"])
        self.assertEqual(state["reviews"][0]["outcome"], "refused")
        result = state["review_result"]
        self.assertEqual(result["verdict"], "defects")
        self.assertEqual([entry["outcome"] for entry in result["angles"]],
                         ["refused", "completed", "completed"])
        self.assertEqual(result["angles"][0]["refusal"], message)
        self.assertEqual([entry["angle"] for entry in result["findings"]], ["concurrency", "concurrency"])
        self.assertIn("== refused by the model: " + message, output)
        self.assertIn("== review angle concurrency by main (", output)
        self.assertIn("== review angle tests by main (", output)

    def test_review_angles_all_refused_stop_fix_rounds_without_a_correction(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "refused"},
            {"mode": "refused"}])

        self.start("angles-all-refused", "--review-angles", "correctness,security", "--fix-rounds", "1")
        output = self.wait("angles-all-refused")
        state = self.state("angles-all-refused")
        self.assertEqual(state["auto_fix_stop"], "review refused")
        self.assertEqual(len(self.invocations()), 3)
        self.assertFalse(state.get("rounds"))
        self.assertFalse(state.get("rotations"))
        self.assertEqual([entry["outcome"] for entry in state["review_result"]["angles"]],
                         ["refused", "refused"])
        self.assertIn("review refused", output)
        report = json.loads(self.cli("report", "--slug", "angles-all-refused", "--json"))
        self.assertEqual(report["summary"]["outcomes"], {"refused": 1})
        self.assertEqual(report["summary"]["reviews"]["total"], 0)
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {})

    def test_review_selects_available_account_and_preserves_working_thread(self):
        self.configure()
        self.start("review-alone")
        self.wait("review-alone")
        working = self.state("review-alone")
        self.account(self.home, "A", self.reset + 7200)
        self.account(self.other, "B", self.reset,
                     runs=[{"mode": "complete", "reply": "Дефект: tracked.txt:1 — проверь содержимое."}])

        self.cli("review", "review-alone", "--effort", "xhigh")
        output = self.wait("review-alone")
        execution = self.review_invocations()[0]
        state = self.state("review-alone")
        task_dir = self.home / "claude-tasks" / "review-alone"
        self.assertEqual(execution["argv"][:2], ["exec", "review"])
        self.assertEqual(execution["argv"][execution["argv"].index("--base") + 1], working["base"])
        self.assertIn("--json", execution["argv"])
        self.assertIn('model_reasoning_effort="xhigh"', execution["argv"])
        self.assertNotIn("-C", execution["argv"])
        self.assertEqual(execution["cwd"], working["worktree"])
        self.assertEqual(execution["home"], str(self.other))
        self.assertEqual(execution["output"], str(task_dir / "review.md"))
        self.assertIn(str(task_dir / "brief.md"), execution["prompt"])
        self.assertEqual(execution["prompt"], self.worker().prompts.review_prompt(state, {"language": "ru"}))
        self.assertFalse(execution["resume"])
        self.assertNotEqual(execution["thread"], working["thread"])
        self.assertEqual((state["thread"], state["account"], state["threads"]),
                         (working["thread"], working["account"], working["threads"]))
        self.assertEqual(len(state["reviews"]), 1)
        review = state["reviews"][0]
        self.assertEqual((review["account"], review["thread"]), ("second", execution["thread"]))
        self.assertLessEqual(review["started"], review["finished"])
        self.assertRegex(output, r"== review by second \(\d+(?:\.\d+)? min\):")
        self.assertIn((task_dir / "review.md").read_text().strip(), output)
        self.assertNotIn("Результат A", output)
        self.assertIn("== commits on ", output)

    def test_unstarted_review_does_not_reuse_previous_review_account(self):
        self.configure(wait_for_reset=False)
        self.configure_notify(events=["turn_finished"])
        self.start("review-unstarted-account")
        self.wait("review-unstarted-account")
        self.notification("review-unstarted-account", "turn_finished")
        self.account(self.home, "A", self.reset, used=99)
        self.account(self.other, "B", self.reset + 3600, runs=[{
            "mode": "complete", "reply": self.review_result(False)}])

        self.cli("review", "review-unstarted-account")
        self.wait("review-unstarted-account")
        self.until(lambda: len(self.notifications("review-unstarted-account")) == 2)
        self.assertIn("account second", self.cli("status", "review-unstarted-account"))
        self.assertEqual(self.notifications("review-unstarted-account")[-1]["account"], "second")
        self.assertEqual(len(self.review_invocations()), 1)
        executions = len(self.invocations())
        self.account(self.other, "B", self.reset + 3600, used=99)

        self.cli("review", "review-unstarted-account")
        output = self.wait("review-unstarted-account")
        self.until(lambda: len(self.notifications("review-unstarted-account")) == 3)
        self.assertIn("== all accounts at limit; earliest reset", output)
        self.assertNotIn("== review by", output)
        self.assertEqual(len(self.invocations()), executions)
        finished = self.notifications("review-unstarted-account")[-1]
        self.assertEqual(finished["outcome"], "exhausted")
        with self.subTest(observable="status"):
            self.assertIn("account ?", self.cli("status", "review-unstarted-account"))
        with self.subTest(observable="turn_finished"):
            self.assertIsNone(finished["account"])
        with self.subTest(observable="telemetry"):
            rows = [row for row in self.telemetry_journal()
                    if row["slug"] == "review-unstarted-account" and row["phase"] == "turn"]
            self.assertEqual(len(rows), 3)
            self.assertIsNone(rows[-1]["account"])

    def test_interrupted_review_angle_selection_does_not_reuse_previous_review_account(self):
        self.configure()
        self.configure_notify(events=["turn_finished"])
        slug = "review-angle-selection-signal"
        self.start(slug)
        self.wait(slug)
        self.notification(slug, "turn_finished")
        self.account(self.home, "A", self.reset, used=99)
        self.account(self.other, "B", self.reset + 3600, runs=[{
            "mode": "complete", "reply": self.review_result(False)}])
        self.cli("review", slug)
        self.wait(slug)
        self.until(lambda: len(self.notifications(slug)) == 2)
        self.assertIn("account second", self.cli("status", slug))
        self.assertEqual(self.notifications(slug)[-1]["account"], "second")
        self.assertEqual(len(self.review_invocations()), 1)
        executions = len(self.invocations())

        # Hold automatic angle selection before run_turn can choose an account.
        # Release only after the supervisor has acknowledged the external signal.
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        ready = self.root / "angle-selection-ready.json"
        release = self.root / "angle-selection-release"
        wrapper = self.root / "git"
        wrapper.write_text(
            "#!" + PYTHON + "\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "if 'config' in sys.argv and '--get-regexp' in sys.argv:\n"
            "    ready = Path(" + repr(str(ready)) + ")\n"
            "    temporary = ready.with_suffix('.tmp')\n"
            "    temporary.write_text(json.dumps({'ppid': os.getppid()}))\n"
            "    temporary.replace(ready)\n"
            "    while not Path(" + repr(str(release)) + ").exists():\n"
            "        time.sleep(0.02)\n"
            "os.execv(" + repr(real_git) + ", [" + repr(real_git) + "] + sys.argv[1:])\n")
        wrapper.chmod(0o700)
        launch = self.cli("review", slug, "--review-angles", "auto")
        supervisor = int(launch.strip().rsplit(", pid ", 1)[1])
        log = self.home / "claude-tasks" / slug / "stderr.log"
        try:
            self.until(ready.exists)
            self.assertEqual(json.loads(ready.read_text())["ppid"], supervisor)
            self.assertEqual(len(self.invocations()), executions)
            with self.subTest(observable="running status"):
                status = self.cli("status", slug)
                self.assertIn("running", status)
                self.assertIn("account ?", status)
            os.kill(supervisor, signal.SIGTERM)
            self.until(lambda: "name=SIGTERM" in log.read_text())
        finally:
            release.touch()

        output = self.wait(slug)
        self.until(lambda: len(self.notifications(slug)) == 3)
        self.assertIn("interrupted externally by SIGTERM", output)
        self.assertNotIn("== review by", output)
        self.assertEqual(len(self.invocations()), executions)
        finished = self.notifications(slug)[-1]
        self.assertEqual(finished["outcome"], "interrupted")
        with self.subTest(observable="status"):
            self.assertIn("account ?", self.cli("status", slug))
        with self.subTest(observable="turn_finished"):
            self.assertIsNone(finished["account"])
        with self.subTest(observable="telemetry"):
            rows = [row for row in self.telemetry_journal()
                    if row["slug"] == slug and row["phase"] == "turn"]
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]["outcome"], "interrupted")
            self.assertIsNone(rows[-1]["account"])

    def test_start_review_runs_in_same_supervisor_and_say_resumes_working_thread(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Рабочий ход завершён"},
            {"mode": "complete", "reply": "Ревью завершено: дефектов нет"},
            {"mode": "complete", "reply": "Исправление завершено"}])
        self.start("review-start", "--review")
        output = self.wait("review-start")
        executions = self.invocations()
        self.assertEqual(len(executions), 2)
        self.assertFalse(executions[0]["review"])
        self.assertTrue(executions[1]["review"])
        self.assertFalse(executions[1]["resume"])
        self.assertEqual(executions[0]["ppid"], executions[1]["ppid"])
        self.assertNotEqual(executions[0]["thread"], executions[1]["thread"])
        self.assertIn("Рабочий ход завершён", output)
        self.assertIn("Ревью завершено: дефектов нет", output)
        self.assertIn("== review by main (", output)
        self.assertLess(output.index("Рабочий ход завершён"), output.index("== review by main ("))

        self.cli("say", "review-start", "Проверь ещё один случай")
        followup = self.wait("review-start")
        execution = self.invocations()[-1]
        self.assertTrue(execution["resume"])
        self.assertEqual(execution["thread"], executions[0]["thread"])
        self.assertEqual(execution["prompt"], "Проверь ещё один случай")
        self.assertEqual(len(self.state("review-start")["reviews"]), 1)
        self.assertIn("Исправление завершено", followup)
        self.assertNotIn("== review by", followup)
        self.assertNotIn("Ревью завершено: дефектов нет", followup)

    def test_say_review_runs_after_completed_followup(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Начальный ход"},
            {"mode": "complete", "reply": "Итог нового хода"},
            {"mode": "complete", "reply": "Ревью нового хода"}])
        self.start("review-say")
        self.wait("review-say")
        self.cli("say", "review-say", "Доработай проверку", "--review")
        output = self.wait("review-say")
        executions = self.invocations()
        self.assertEqual(len(executions), 3)
        self.assertTrue(executions[1]["resume"])
        self.assertTrue(executions[2]["review"])
        self.assertEqual(executions[1]["ppid"], executions[2]["ppid"])
        self.assertIn("Итог нового хода", output)
        self.assertIn("Ревью нового хода", output)
        self.assertNotIn("Начальный ход", output)

    def test_fix_round_sends_findings_to_working_thread_and_carries_rejection_to_fresh_review(self):
        self.configure(language="en")
        self.configure_notify()
        review = self.review_result()
        decisions = [{"id": "F1", "decision": "fixed", "note": "Handle empty input before indexing."},
                     {"id": "F2", "decision": "rejected", "note": "The last token is a sentinel, not output."}]
        fixed = self.work_result(findings=decisions)
        clean = self.review_result(False)
        command = "printf 'passed\\n' >> check-runs"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review},
            {"mode": "complete", "reply": fixed},
            {"mode": "complete", "reply": clean}])

        self.start("fix-clean", "--review", "--fix-rounds", "1", "--check", command)
        output = self.wait("fix-clean")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, True])
        work, initial_review, correction, final_review = executions
        self.assertTrue(correction["resume"])
        self.assertEqual(correction["thread"], work["thread"])
        self.assertEqual(correction["argv"][:2], ["exec", "resume"])
        self.assertEqual(len({event["ppid"] for event in executions}), 1,
                         "one supervisor must finish the whole cycle before wait returns")
        state = self.state("fix-clean")
        worker = self.worker()
        cfg = {"language": "en"}
        for finding in review["findings"]:
            for field in ("id", "severity", "title", "detail", "verify"):
                self.assertIn(finding[field], correction["prompt"])
            self.assertIn("{file}:{line}".format(**finding), correction["prompt"])
        self.assertIn("F2", final_review["prompt"])
        self.assertIn(decisions[1]["note"], final_review["prompt"])
        for event in (initial_review, final_review):
            self.assertIn(command, event["prompt"])
        self.assertEqual(final_review["prompt"], worker.prompts.review_prompt(state, cfg))
        self.assertFalse(final_review["resume"])
        self.assertEqual(len({work["thread"], initial_review["thread"], final_review["thread"]}), 3)
        for event in executions:
            self.assert_output_schema(event, "fix-clean", review=event["review"])
        self.assertEqual(state["fix_rounds"], 1)
        self.assertEqual(state["result"], fixed)
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["rounds"][0]["review"], review)
        self.assertEqual(state["rounds"][0]["decisions"], decisions)
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "passed")
        self.assertEqual(len(state["checks"]), 2)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(),
                         ["passed", "passed"])
        messages = [json.loads(line) for line in
                    (self.home / "claude-tasks" / "fix-clean" / "messages.jsonl").read_text().splitlines()]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "review")
        self.assertEqual(messages[0]["text"], correction["prompt"])
        self.assertRegex(output, r"== round 1: 2 findings .*→ fixed 1, rejected 1")
        self.assertIn("F2", output)
        self.assertIn(decisions[1]["note"], output)
        self.assertIn("== check `" + command + "`: passed", output)
        self.assertIn(clean["summary"], output)
        self.assertRegex(output, r"== auto-fix: 1 rounds?, findings 2 → 0")
        self.assertIn("clean", output.lower())
        self.notification("fix-clean", "turn_finished")
        self.until(lambda: sum(event["event"] == "review_done" for event in self.notifications("fix-clean")) == 2)
        notifications = self.notifications("fix-clean")
        self.assertEqual([event["round"] for event in notifications if event["event"] == "review_done"],
                         [0, 1])
        self.assertEqual(sum(event["event"] == "turn_finished" for event in notifications), 1)

    def test_fix_round_escalates_findings_in_any_commit_from_the_previous_round(self):
        self.configure(language="en")
        self.configure_notify()
        git_trace = self.record_git_calls()
        review = self.provenance_review([("P2", "src/provenance.py", 15)])
        runs = self.provenance_runs(review)
        runs[2]["commit_batches"] = [{"src/helper.py": "HELPER = True\n"}]
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("fix-escalate", "--review", "--fix-rounds", "2")
        output = self.wait("fix-escalate")
        state = self.state("fix-escalate")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False, True])
        self.assertEqual(state["auto_fix_stop"], "escalate")
        self.assertEqual(len(state["rounds"]), 1)
        branch = state["branch"]
        commits = self.git("rev-list", branch + "~2.." + branch).splitlines()
        self.assertEqual(state["rounds"][0]["commits"], commits)
        self.assertEqual(len(commits), 2)
        source = self.git("rev-parse", "--short", branch + "~1")
        self.assertEqual(state["auto_fix_escalations"], [dict(review["findings"][0], round=1, commit=source)])
        message = ("== auto-fix: stop: escalate — F1 src/provenance.py:15 "
                   "targets code from fix round 1 (" + source + ")")
        self.assertIn(message, output)
        self.assertIn(review["findings"][0]["detail"], output)
        self.assertIn(review["findings"][0]["verify"], output)
        self.assertIn("; code +2 −1", output)
        blame = [json.loads(line)["argv"] for line in git_trace.read_text().splitlines()
                 if "blame" in json.loads(line)["argv"]]
        self.assertTrue(blame)
        for argv in blame:
            self.assertIn("--porcelain", argv)
            for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=false"):
                self.assertIn(setting, argv)
                self.assertEqual(argv[argv.index(setting) - 1], "-c")
        self.until(lambda: any(row["phase"] == "turn" for row in self.telemetry_journal()))
        turn = next(row for row in self.telemetry_journal() if row["phase"] == "turn")
        self.assertEqual(turn["auto_fix_stop"], "escalate")
        report = json.loads(self.cli("report", "--slug", "fix-escalate", "--json"))
        self.assertEqual(report["tasks"][0]["auto_fix_stop"], "escalate")
        self.assertEqual(report["summary"]["auto_fix_stops"], {"escalate": 1})
        self.assertEqual(report["summary"]["fix_rounds_to_clean"], {})
        self.assertIn("escalate", self.cli("report", "--slug", "fix-escalate"))
        self.notification("fix-escalate", "turn_finished")
        self.assertEqual(sum(event["event"] == "turn_finished"
                             for event in self.notifications("fix-escalate")), 1)
        self.assertNotIn("auto_fix_stop", self.notification("fix-escalate", "turn_finished"))

    def test_fix_round_continues_when_repeated_review_targets_original_work(self):
        self.configure(language="en")
        review = self.provenance_review([("P2", "src/provenance.py", 5)])
        self.account(self.home, "A", self.reset, runs=self.provenance_runs(review))
        self.start("fix-original-code", "--review", "--fix-rounds", "2")
        output = self.wait("fix-original-code")
        state = self.state("fix-original-code")
        self.assertEqual([event["review"] for event in self.invocations()],
                         [False, True, False, True, False, True])
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(len(state["rounds"][0]["commits"]), 1)
        self.assertEqual(state["rounds"][1]["commits"], [])
        self.assertFalse(state.get("auto_fix_escalations"))
        self.assertNotIn("stop: escalate", output)

    def test_fix_round_escalates_p1_p2_within_two_lines_and_at_file_edges(self):
        self.configure(language="en")
        filename = "src/provenance.py"
        review = self.provenance_review([("P1", filename, 3), ("P2", filename, 13),
                                         ("P2", filename, 17), ("P2", filename, 29)])
        runs = self.provenance_runs(review)
        source = runs[0]["write_files"][filename]
        for line in (1, 15, 30):
            source = source.replace("VALUE_%s = %s\n" % (line, line), "VALUE_%s = -1\n" % line)
        runs[2]["write_files"][filename] = source
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("fix-nearby-code", "--review", "--fix-rounds", "2")
        self.wait("fix-nearby-code")
        state = self.state("fix-nearby-code")
        self.assertEqual(state["auto_fix_stop"], "escalate")
        self.assertEqual(len(self.invocations()), 4)
        commit = self.git("rev-parse", "--short", state["branch"])
        self.assertEqual(state["auto_fix_escalations"],
                         [dict(finding, round=1, commit=commit) for finding in review["findings"]])

    def test_fix_round_p3_in_fix_code_keeps_only_p3_stop(self):
        self.configure(language="en")
        review = self.provenance_review([("P3", "src/provenance.py", 15),
                                         ("P3", "src/provenance.py", 5)])
        self.account(self.home, "A", self.reset, runs=self.provenance_runs(review))
        self.start("fix-provenance-p3", "--review", "--fix-rounds", "2")
        output = self.wait("fix-provenance-p3")
        state = self.state("fix-provenance-p3")
        self.assertEqual(state["auto_fix_stop"], "only P3")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(len(self.invocations()), 4)
        self.assertFalse(state.get("auto_fix_escalations"))
        self.assertIn("only P3", output)
        self.assertNotIn("stop: escalate", output)

    def test_fix_round_missing_finding_line_does_not_crash_or_escalate(self):
        self.configure(language="en")
        review = self.provenance_review([("P2", "src/provenance.py", 15)])
        del review["findings"][0]["line"]
        self.account(self.home, "A", self.reset, runs=self.provenance_runs(review))
        self.start("fix-missing-line", "--review", "--fix-rounds", "2")
        output = self.wait("fix-missing-line")
        state = self.state("fix-missing-line")
        self.assertEqual(state["auto_fix_stop"], "review unknown")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertFalse(state.get("auto_fix_escalations"))
        self.assertNotIn("stop: escalate", output)

    def test_fix_round_deleted_file_and_finding_beyond_eof_do_not_escalate(self):
        self.configure(language="en")
        filename = "src/provenance.py"
        removed = "src/removed.py"
        review = self.provenance_review([("P2", removed, 1), ("P2", filename, 31)])
        runs = self.provenance_runs(review)
        source = runs[0]["write_files"][filename]
        runs[0]["write_files"][removed] = "REMOVED = True\n"
        runs[0]["commit_files"].append(removed)
        runs[2]["write_files"][filename] = source.replace("VALUE_30 = 30\n", "VALUE_30 = -1\n")
        runs[2]["remove_files"] = [removed]
        runs[2]["commit_files"].append(removed)
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("fix-invalid-location", "--review", "--fix-rounds", "2")
        output = self.wait("fix-invalid-location")
        state = self.state("fix-invalid-location")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(len(self.invocations()), 6)
        self.assertFalse(state.get("auto_fix_escalations"))
        self.assertFalse((Path(state["worktree"]) / removed).exists())
        self.assertNotIn("stop: escalate", output)

    def test_fix_round_size_excludes_builtin_explicit_and_check_path_guards(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_provenance.py": "assert True\n",
                             "custom/guarded.data": "original guard\n",
                             "scripts/check_provenance.py": "print('base check passed')\n"})
        filename = "src/provenance.py"
        runs = self.provenance_runs(self.review_result(False))
        source = runs[0]["write_files"][filename]
        changes = {filename: source.replace("VALUE_15 = 15\n", "VALUE_15 = 150\nEXTRA = True\n"),
                   "tests/test_provenance.py": "assert True\nassert 1 == 1\nassert 2 == 2\n",
                   "custom/guarded.data": "replacement guard\nextra guard\n",
                   "scripts/check_provenance.py": "print('changed check passed')\nprint('more checks')\n"}
        runs[2]["write_files"] = changes
        runs[2]["commit_files"] = list(changes)
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("fix-code-size", "--review", "--fix-rounds", "2", "--guard", "custom/*.data",
                   "--check", "python3 scripts/check_provenance.py")
        output = self.wait("fix-code-size")
        state = self.state("fix-code-size")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertEqual(state["rounds"][0]["code"], {"added": 2, "removed": 1})
        self.assertIn("== round 1: 1 findings (P2 1) → fixed 1, rejected 0; code +2 −1", output)
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])

    def test_workspace_fix_round_supervisor_commit_is_used_for_escalation(self):
        self.configure(language="en", sandbox="workspace")
        review = self.provenance_review([("P2", "src/provenance.py", 15)])
        self.account(self.home, "A", self.reset, runs=self.provenance_runs(review, workspace=True))
        self.start("fix-workspace-escalate", "--review", "--fix-rounds", "2")
        output = self.wait("fix-workspace-escalate")
        state = self.state("fix-workspace-escalate")
        self.assertEqual(state["auto_fix_stop"], "escalate")
        self.assertEqual(len(self.invocations()), 4)
        commit = self.git("rev-parse", state["branch"])
        self.assertEqual(state["rounds"][0]["commits"], [commit])
        self.assertEqual(state["rounds"][0]["code"], {"added": 1, "removed": 1})
        self.assertEqual(state["publish"]["sha"], commit)
        self.assertTrue(state["publish"]["pushed"])
        short = self.git("rev-parse", "--short", commit)
        self.assertEqual(state["auto_fix_escalations"], [dict(review["findings"][0], round=1, commit=short)])
        self.assertIn("targets code from fix round 1 (" + short + ")", output)
        for invocation in self.invocations():
            self.assert_workspace_sandbox(invocation)

    def test_fix_round_escalates_older_round_code_even_when_round_limit_is_reached(self):
        self.configure(language="en")
        filename = "src/provenance.py"
        original_review = self.provenance_review([("P2", filename, 5)])
        runs = self.provenance_runs(original_review)
        source = runs[2]["write_files"][filename]
        runs[4]["write_files"] = {filename: source.replace("VALUE_25 = 25\n", "VALUE_25 = -1\n")}
        runs[4]["commit_files"] = [filename]
        final_review = self.provenance_review([("P2", filename, 15)])
        runs[5]["reply"] = final_review
        self.account(self.home, "A", self.reset, runs=runs)
        self.start("fix-older-round", "--review", "--fix-rounds", "2")
        output = self.wait("fix-older-round")
        state = self.state("fix-older-round")
        self.assertEqual(state["auto_fix_stop"], "escalate")
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(len(self.invocations()), 6)
        source_commit = self.git("rev-parse", "--short", state["branch"] + "~1")
        self.assertEqual(state["auto_fix_escalations"],
                         [dict(final_review["findings"][0], round=1, commit=source_commit)])
        self.assertIn("targets code from fix round 1 (" + source_commit + ")", output)

    def test_fix_round_limit_runs_exactly_two_corrections_and_prints_final_defects(self):
        self.configure()
        reviews = [self.review_result() for _ in range(3)]
        for number, review in enumerate(reviews):
            review["summary"] = "Review generation %s still has defects." % number
        reviews[-1]["findings"][0]["detail"] = "Final review detail must be printed completely."
        decisions = [{"id": finding["id"], "decision": "fixed", "note": "Updated the relevant code."}
                     for finding in reviews[0]["findings"]]
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": reviews[0]},
            {"mode": "complete", "reply": self.work_result(findings=decisions)},
            {"mode": "complete", "reply": reviews[1]},
            {"mode": "complete", "reply": self.work_result(findings=decisions)},
            {"mode": "complete", "reply": reviews[2]}])

        self.start("fix-bounded", "--review", "--fix-rounds", "2")
        output = self.wait("fix-bounded")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, True, False, True])
        self.assertEqual(len(self.review_invocations()), 3)
        self.assertEqual(len({event["thread"] for event in self.review_invocations()}), 3)
        self.assertTrue(all(event["resume"] and event["thread"] == executions[0]["thread"]
                            for event in (executions[2], executions[4])))
        state = self.state("fix-bounded")
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(state["reviews"][-1]["result"], reviews[-1])
        self.assertEqual(state["auto_fix_stop"], "rounds_exhausted")
        for finding in reviews[-1]["findings"]:
            self.assertIn("[{severity}] {id} {file}:{line} — {title}".format(**finding), output)
            self.assertIn(finding["detail"], output)
            self.assertIn(finding["verify"], output)
        self.assertIn(reviews[-1]["summary"], output)
        self.assertIn("== round 1:", output)
        self.assertIn("== round 2:", output)
        self.assertIn("== auto-fix: 2 rounds, findings 2 → 2 → 2", output)

    def test_second_review_limits_scope_and_only_p3_stops_further_fixes(self):
        for language in ("en", "ru"):
            with self.subTest(language=language):
                self.configure(language=language)
                (self.home / "exec-count").unlink(missing_ok=True)
                review = self.review_result()
                minor_review = self.review_result()
                minor_review["findings"] = [minor_review["findings"][1]]
                minor_review["summary"] = "Only a minor issue remains."
                decisions = [{"id": "F1", "decision": "fixed", "note": "Fixed the acceptance criterion."},
                             {"id": "F2", "decision": "rejected", "note": "out of scope"}]
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": review},
                    {"mode": "complete", "reply": self.work_result(findings=decisions,
                        remaining=["Let Claude decide the optional edge case."])},
                    {"mode": "complete", "reply": minor_review}])
                before = len(self.invocations())
                slug = "fix-only-p3-" + language
                self.start(slug, "--review", "--fix-rounds", "2")
                output = self.wait(slug)
                executions = self.invocations()[before:]
                self.assertEqual([event["review"] for event in executions], [False, True, False, True])
                initial_review, correction, second_review = executions[1:]
                state = self.state(slug)
                worker = self.worker()
                cfg = {"language": language}
                self.assertEqual(second_review["prompt"], worker.prompts.review_prompt(state, cfg))
                self.assertEqual(correction["prompt"], worker.prompts.fix_prompt(state, review, None, cfg))
                self.assertIn(str(self.home / "claude-tasks" / slug / "brief.md"), initial_review["prompt"])
                self.assertEqual(state["reviews"][0]["result"], review)
                self.assertNotIn(decisions[1]["note"], initial_review["prompt"])
                self.assertIn(decisions[1]["note"], second_review["prompt"])
                for finding in review["findings"]:
                    self.assertNotIn(finding["title"], initial_review["prompt"])
                    for field in ("id", "severity", "title", "detail", "verify"):
                        self.assertIn(finding[field], correction["prompt"])
                    self.assertIn("{file}:{line}".format(**finding), correction["prompt"])
                self.assertEqual(state["auto_fix_stop"], "only P3")
                self.assertEqual(len(state["rounds"]), 1)
                self.assertEqual(state["reviews"][-1]["result"], minor_review)
                self.assertIn("only P3", output)
                self.assertIn(minor_review["findings"][0]["detail"], output)

    def test_initial_review_with_only_p3_does_not_start_a_fix_round(self):
        self.configure()
        review = self.review_result()
        for finding in review["findings"]:
            finding["severity"] = "P3"
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review}])
        self.start("fix-initial-p3", "--review", "--fix-rounds", "2")
        output = self.wait("fix-initial-p3")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        state = self.state("fix-initial-p3")
        self.assertEqual(state["auto_fix_stop"], "only P3")
        self.assertEqual(state["rounds"], [])
        self.assertIn("only P3", output)
        for finding in review["findings"]:
            self.assertIn(finding["detail"], output)

    def test_say_can_start_a_new_fix_cycle_and_default_say_does_not_inherit_it(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result(summary="Initial work")},
            {"mode": "complete", "reply": self.work_result(summary="Requested followup")},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "reply": self.work_result(findings=[
                {"id": "F1", "decision": "fixed", "note": "Updated parser"},
                {"id": "F2", "decision": "fixed", "note": "Updated formatter"}])},
            {"mode": "complete", "reply": self.review_result(False)},
            {"mode": "complete", "reply": self.work_result(summary="Ordinary followup")}])
        self.start("fix-say")
        self.wait("fix-say")
        thread = self.state("fix-say")["thread"]
        self.cli("say", "fix-say", "Finish the followup", "--review", "--fix-rounds", "1")
        output = self.wait("fix-say")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, False, True, False, True])
        self.assertTrue(executions[1]["resume"])
        self.assertTrue(executions[3]["resume"])
        self.assertEqual(executions[3]["thread"], thread)
        self.assertEqual(len(self.state("fix-say")["rounds"]), 1)
        self.assertRegex(output, r"== auto-fix: 1 rounds?, findings 2 → 0")
        self.cli("say", "fix-say", "One ordinary final followup")
        output = self.wait("fix-say")
        self.assertEqual(len(self.invocations()), 6)
        self.assertEqual(len(self.review_invocations()), 2)
        self.assertEqual(self.state("fix-say")["fix_rounds"], 0)
        self.assertNotIn("== auto-fix:", output)
        self.assertNotIn("== round ", output)
        self.assertIn("Ordinary followup", output)

    def test_failed_check_after_fix_uses_last_forty_lines_for_next_round_then_reviews(self):
        self.configure()
        review = self.review_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review},
            {"mode": "complete", "create_files": ["fail-check"], "reply": self.work_result(findings=[
                {"id": "F1", "decision": "fixed", "note": "First fix"},
                {"id": "F2", "decision": "fixed", "note": "Second fix"}])},
            {"mode": "complete", "remove_files": ["fail-check"], "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = ("if test -f fail-check; then i=1; while [ \"$i\" -le 45 ]; "
                   "do printf 'retry-line-%02d\\n' \"$i\"; i=$((i + 1)); done; "
                   "printf 'retry-stderr\\n' >&2; exit 1; fi; printf 'check-passed\\n'")
        self.start("fix-check-retry", "--review", "--fix-rounds", "2", "--check", command)
        output = self.wait("fix-check-retry")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, False, True])
        retry = executions[3]
        self.assertTrue(retry["resume"])
        self.assertEqual(retry["thread"], executions[0]["thread"])
        state = self.state("fix-check-retry")
        self.assertIn(command, retry["prompt"])
        self.assertIn("retry-line-07", retry["prompt"])
        self.assertIn("retry-line-45", retry["prompt"])
        self.assertIn("retry-stderr", retry["prompt"])
        self.assertNotIn("retry-line-06", retry["prompt"])
        for finding in review["findings"]:
            self.assertNotIn(finding["title"], retry["prompt"], "retry must focus on the failed check")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "failed", "passed"])
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "failed")
        self.assertIsNone(state["rounds"][1]["review"])
        self.assertEqual(state["rounds"][1]["check"]["outcome"], "passed")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertIn("== auto-fix: 2 rounds", output)
        self.assertIn("== round 2:", output)
        self.assertIn("FAILED", output)
        self.assertIn("No defects found.", output)

    def test_failed_check_on_last_fix_round_does_not_start_another_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "complete", "create_files": ["fail-check"], "reply": self.work_result()}])
        command = "if test -f fail-check; then printf 'last-round-check-failure\\n'; exit 1; fi"
        self.start("fix-check-exhausted", "--review", "--fix-rounds", "1", "--check", command)
        output = self.wait("fix-check-exhausted")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True, False])
        state = self.state("fix-check-exhausted")
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "failed"])
        self.assertEqual(state["rounds"][0]["check"]["outcome"], "failed")
        self.assertIn("check", state["auto_fix_stop"])
        self.assertIn("last-round-check-failure", output)
        self.assertIn("FAILED", output)
        self.assertRegex(output, r"== auto-fix: 1 rounds?")
        self.assertIn("Found two defects.", output, "the last completed review must remain visible")

    def test_incomplete_fix_results_stop_the_loop_and_print_questions(self):
        self.configure()
        for status in ("needs_decision", "blocked", "partial"):
            with self.subTest(status=status):
                (self.home / "exec-count").unlink(missing_ok=True)
                questions = ["Which public API should accept the new argument?"]
                result = self.work_result(status, questions=questions, remaining=["Wait for the API decision."])
                self.account(self.home, "A", self.reset, runs=[
                    {"mode": "complete", "reply": self.work_result()},
                    {"mode": "complete", "reply": self.review_result()},
                    {"mode": "complete", "reply": result}])
                slug = "fix-incomplete-" + status.replace("_", "-")
                before = len(self.invocations())
                self.start(slug, "--review", "--fix-rounds", "2")
                output = self.wait(slug)
                self.assertEqual(len(self.invocations()) - before, 3)
                state = self.state(slug)
                self.assertEqual(state["result"], result)
                self.assertEqual(state["auto_fix_stop"], "result " + status)
                self.assertEqual(len(state["rounds"]), 1)
                self.assertIn(questions[0], output)
                self.assertIn(result["remaining"][0], output)
                self.assertRegex(output, r"== auto-fix: 1 rounds?")

    def test_stop_during_fix_round_stops_cycle_before_more_checks_or_reviews(self):
        self.configure()
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()},
            {"mode": "hold"}])
        self.start("fix-stopped", "--review", "--fix-rounds", "2", "--check",
                   "printf 'check\\n' >> check-runs")
        self.until(lambda: len(self.invocations()) == 3)
        correction = self.invocations()[-1]
        self.assertTrue(correction["resume"])
        self.cli("stop", "fix-stopped", timeout=20)
        output = self.wait("fix-stopped")
        state = self.state("fix-stopped")
        self.assertEqual(state["outcome"], "stopped")
        self.assertEqual(state["auto_fix_stop"], "stopped")
        self.assertEqual(len(self.invocations()), 3)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(), ["check"])
        self.assertIn("stopped", output)
        self.notification("fix-stopped", "turn_finished")
        self.assertEqual(sum(event["event"] == "turn_finished" for event in self.notifications("fix-stopped")), 1)

    def test_rotation_during_fix_hands_findings_to_new_account_and_finishes_cycle(self):
        self.configure()
        review = self.review_result()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": review},
            {"mode": "usage_error"}])
        self.account(self.other, "B", self.reset + 3600, runs=[
            {"mode": "complete", "reply": self.work_result(findings=[
                {"id": "F1", "decision": "fixed", "note": "Fixed after account rotation"},
                {"id": "F2", "decision": "fixed", "note": "Also fixed after account rotation"}])},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("fix-rotation", "--review", "--fix-rounds", "1")
        output = self.wait("fix-rotation")
        executions = self.invocations()
        self.assertEqual([event["review"] for event in executions], [False, True, False, False, True])
        interrupted, replacement, final_review = executions[2:]
        self.assertTrue(interrupted["resume"])
        self.assertEqual(interrupted["thread"], executions[0]["thread"])
        self.assertFalse(replacement["resume"])
        self.assertNotEqual(replacement["thread"], interrupted["thread"])
        self.assertEqual(replacement["home"], str(self.other))
        self.assertEqual(final_review["home"], str(self.other))
        for event in (interrupted, replacement):
            for finding in review["findings"]:
                for field in ("id", "severity", "title", "detail", "verify"):
                    self.assertIn(finding[field], event["prompt"])
                self.assertIn("{file}:{line}".format(**finding), event["prompt"])
        state = self.state("fix-rotation")
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(state["account"], "second")
        self.assertEqual(len(state["rounds"]), 1, "rotation must not spend another fix round")
        self.assertEqual(state["auto_fix_stop"], "clean")
        self.assertIn("== rotated main → second:", output)
        self.assertIn("No defects found.", output)

    def test_zero_fix_rounds_keeps_one_review_without_a_correction(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result()}])
        self.start("fix-zero", "--review", "--fix-rounds", "0")
        output = self.wait("fix-zero")
        self.assertEqual([event["review"] for event in self.invocations()], [False, True])
        self.assertEqual(self.state("fix-zero")["fix_rounds"], 0)
        self.assertFalse(self.state("fix-zero").get("rounds"))
        self.assertNotIn("== auto-fix:", output)

    def test_fix_rounds_validate_review_requirement_and_range_before_starting_or_saying(self):
        self.configure()
        self.start("fix-validation")
        self.wait("fix-validation")
        state = self.state("fix-validation")
        before = len(self.invocations())
        for options in (("--fix-rounds", "0"), ("--fix-rounds", "1"),
                        ("--review", "--fix-rounds", "-1"), ("--review", "--fix-rounds", "6"),
                        ("--review", "--fix-rounds", "1.5")):
            for command in (("start", "fix-invalid", "--brief", str(self.brief), "--repo", str(self.repo)),
                            ("say", "fix-validation", "Invalid fix cycle")):
                with self.subTest(command=command[0], options=options):
                    result = subprocess.run([PYTHON, "-I", str(SCRIPT)] + list(command + options),
                                            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=15)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("fix-rounds", result.stdout + result.stderr)
                    self.assertEqual(len(self.invocations()), before)
                    self.assertEqual(self.state("fix-validation"), state)
                    self.assertFalse((self.home / "claude-tasks" / "fix-invalid").exists())
        self.assertFalse((self.home / "claude-tasks" / "fix-validation" / "messages.jsonl").exists())

    def test_reconnect_errors_do_not_fail_a_completed_turn(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": "Ход после переподключений", "reconnects": 2},
            {"mode": "complete", "reply": "Ревью после переподключений"}])
        self.start("review-reconnect", "--review")
        output = self.wait("review-reconnect")
        self.assertEqual(self.state("review-reconnect")["outcome"], "completed")
        self.assertEqual(len(self.invocations()), 2)
        self.assertIn("Ревью после переподключений", output)

    def test_failed_working_turn_does_not_start_automatic_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "failed"}])
        self.start("review-failed", "--review", "--check", "touch unexpected-check")
        output = self.cli("wait", "review-failed", "--timeout", "10")
        self.assertEqual(self.state("review-failed")["outcome"], "failed")
        self.assertIn("fixture task failed", output)
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertFalse(self.state("review-failed").get("reviews"))
        self.assertFalse(self.state("review-failed").get("checks"))
        self.assertFalse((Path(self.state("review-failed")["worktree"]) / "unexpected-check").exists())
        self.assertNotIn("== review by", output)

    def test_stopped_working_turn_does_not_start_automatic_review(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[{"mode": "hold"}])
        self.start("review-stopped", "--review", "--check", "touch unexpected-check")
        self.until(lambda: bool(self.invocations()))
        self.cli("stop", "review-stopped", timeout=20)
        self.wait("review-stopped")
        self.assertEqual(self.state("review-stopped")["outcome"], "stopped")
        self.assertEqual(len(self.invocations()), 1)
        self.assertFalse(self.review_invocations())
        self.assertFalse(self.state("review-stopped").get("checks"))
        self.assertFalse((Path(self.state("review-stopped")["worktree"]) / "unexpected-check").exists())
