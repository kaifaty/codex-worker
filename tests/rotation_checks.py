"""Local checks and guarded file tests."""
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rotation_support import IMPOSSIBLE_PID, PYTHON, SCRIPT, RotationTestCase


class CheckGuardTests(RotationTestCase):
    def test_check_options_reject_invalid_timeouts_and_conflicting_say_flags(self):
        for timeout in ("0", "-1", "nan", "inf"):
            with self.subTest(timeout=timeout):
                result = subprocess.run(
                    [PYTHON, "-I", str(SCRIPT), "start", "invalid-check", "--brief", str(self.brief),
                     "--repo", str(self.repo), "--check", "true", "--check-timeout", timeout],
                    cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("positive finite", result.stderr)
        result = subprocess.run(
            [PYTHON, "-I", str(SCRIPT), "say", "invalid-check", "Continue", "--check", "true", "--no-check"],
            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)
        self.assertFalse(self.invocations(), "invalid options must not launch Codex")

    def test_check_passes_after_completed_work_before_automatic_review(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "create_files": ["done.txt"]},
            {"mode": "complete", "reply": "Review after objective check"}])
        command = "test -f done.txt"
        self.start("check-passed", "--review", "--check", command)
        output = self.wait("check-passed")
        state = self.state("check-passed")
        self.assertEqual(state["check"], command)
        self.assertEqual(state["check_timeout"], 1800)
        self.assertEqual(len(state["checks"]), 1, "review must not rerun the working check")
        check = state["checks"][0]
        self.assertEqual((check["cmd"], check["exit"], check["outcome"]), (command, 0, "passed"))
        self.assertGreaterEqual(check["seconds"], 0)
        self.assertIsInstance(check["at"], (int, float))
        self.assertRegex(output, r"== check `test -f done\.txt`: passed \(exit 0, \d+(?:\.\d+)? s\)")
        self.assertTrue((self.home / "claude-tasks" / "check-passed" / "check.log").is_file())
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertEqual(self.sandbox_invocations(), [])
        for event in self.invocations():
            self.assert_no_workspace_sandbox(event)
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn(command, prompt)
        self.assertEqual(prompt, self.worker().review_prompt(state, {"language": "en"}))
        self.assertLess(output.index("== check `"), output.index("== review by "))

    def test_guarded_changes_decode_latin1_patches_for_every_diff_layer(self):
        original = ("# coding: latin-1\n"
                    "import unittest\n"
                    "class Latin1Tests(unittest.TestCase):\n"
                    "    def test_cafe(self):\n"
                    "        self.assertEqual('café', 'café')\n")
        changed = original.replace("    def test_cafe(self):\n",
                                   "    @unittest.skip('café')\n    def test_cafe(self):\n")
        changed = changed.replace("self.assertEqual('café', 'café')", "self.assertIn('café', 'café')")
        path = self.repo / "tests/test_latin1.py"
        path.parent.mkdir()
        path.write_bytes(original.encode("latin-1"))
        self.git("add", "--", "tests/test_latin1.py")
        self.git("commit", "--quiet", "-m", "Add Latin-1 test fixture")
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        path.write_bytes(changed.encode("latin-1"))
        for layer in ("working", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                self.assertEqual(len(changes), 1)
                self.assertEqual((changes[0]["path"], changes[0]["added"], changes[0]["removed"]),
                                 ("tests/test_latin1.py", 2, 1))
                self.assertEqual(len(changes[0]["disabled"]), 1)
                self.assertEqual(changes[0]["disabled"][0]["line"], 4)
                self.assertIn("@unittest.skip", changes[0]["disabled"][0]["text"])
            if layer == "working":
                self.git("add", "--", "tests/test_latin1.py")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Change Latin-1 test fixture")

    def test_guarded_changed_latin1_skip_condition_stays_disabled_in_every_diff_layer(self):
        original = ("# coding: latin-1\n"
                    "import unittest\n"
                    "class Latin1Tests(unittest.TestCase):\n"
                    "    @unittest.skipIf('é' == 'ñ', 'condition')\n"
                    "    def test_condition(self):\n"
                    "        self.assertTrue(True)\n"
                    "if __name__ == '__main__':\n"
                    "    result = unittest.main(exit=False).result\n"
                    "    print(result.testsRun, len(result.skipped))\n")
        changed = original.replace("'é'", "'ñ'")
        path = self.repo / "tests/test_latin1.py"
        path.parent.mkdir()
        path.write_bytes(original.encode("latin-1"))
        self.git("add", "--", "tests/test_latin1.py")
        self.git("commit", "--quiet", "-m", "Add Latin-1 conditional skip fixture")
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        for contents, skipped in ((original, 0), (changed, 1)):
            path.write_bytes(contents.encode("latin-1"))
            result = subprocess.run([PYTHON, "-I", str(path)], cwd=self.repo, env=self.env,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "1 %d" % skipped)
        for layer in ("working", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]["moved"], 0)
                self.assertEqual(len(changes[0]["disabled"]), 1)
                self.assertEqual(changes[0]["disabled"][0]["line"], 4)
                self.assertTrue(changes[0]["disabled"][0]["text"].startswith("@unittest.skipIf("))
                output = "\n".join(worker.guarded_lines(changes))
                self.assertEqual(output.encode("utf-8").decode("utf-8"), output)
                self.assertFalse(any(line.startswith("== guarded skip markers moved:")
                                     for line in worker.guarded_lines(changes)))
            if layer == "working":
                self.git("add", "--", "tests/test_latin1.py")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Change Latin-1 skip condition")

    def test_guarded_identical_latin1_markers_move_in_every_diff_layer(self):
        original = ("# coding: latin-1\n"
                    "import unittest\n"
                    "class Latin1Tests(unittest.TestCase):\n"
                    "    marker = \"@unittest.skip('café')\"\n"
                    "    @unittest.skipIf('é' == 'ñ', 'condition')\n"
                    "    def test_condition(self):\n"
                    "        self.assertTrue(True)\n")
        source = self.repo / "tests/test_z_latin1.py"
        source.parent.mkdir()
        source.write_bytes(original.encode("latin-1"))
        self.git("add", "--", "tests/test_z_latin1.py")
        self.git("commit", "--quiet", "-m", "Add Latin-1 move fixture")
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        source.rename(self.repo / "tests/test_a_latin1.py")
        for layer in ("working-untracked", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                self.assertEqual(len(changes), 2)
                self.assertEqual(sum(len(item["disabled"]) for item in changes), 0)
                self.assertEqual({item["path"]: item["moved"] for item in changes},
                                 {"tests/test_z_latin1.py": 0, "tests/test_a_latin1.py": 2})
                self.assertEqual(worker.guarded_lines(changes)[-1], "== guarded skip markers moved: 2")
            if layer == "working-untracked":
                self.git("add", "--all", "--", "tests")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Move Latin-1 fixture")

    def test_guarded_changed_latin1_untracked_markers_are_not_moved(self):
        cases = {
            "condition": ("@unittest.skipIf('é' == 'ñ', 'condition')\ndef test_a():\n    assert True\n", 3),
            "literal": ("def test_a():\n    marker = \"@unittest.skip('é')\"\n    assert marker\n", 4)}
        for name, (contents, _) in cases.items():
            source = self.repo / ("tests/test_z_%s.py" % name)
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(("# coding: latin-1\nimport unittest\n" + contents).encode("latin-1"))
        self.git("add", "--all", "--", "tests")
        self.git("commit", "--quiet", "-m", "Add Latin-1 untracked move fixtures")
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        for name, (contents, _) in cases.items():
            (self.repo / ("tests/test_z_%s.py" % name)).unlink()
            (self.repo / ("tests/test_a_%s.py" % name)).write_bytes(
                ("# coding: latin-1\nimport unittest\n" + contents.replace("'é'", "'ñ'")).encode("latin-1"))
        changes = worker.guarded_changes(state)
        by_path = {item["path"]: item for item in changes}
        self.assertEqual(sum(item["moved"] for item in changes), 0)
        self.assertEqual(sum(len(item["disabled"]) for item in changes), 2)
        output = "\n".join(worker.guarded_lines(changes))
        self.assertEqual(output.encode("utf-8").decode("utf-8"), output)
        self.assertNotIn("== guarded skip markers moved:", output)
        for name, (_, line_number) in cases.items():
            with self.subTest(marker=name):
                disabled = by_path["tests/test_a_%s.py" % name]["disabled"]
                self.assertEqual(len(disabled), 1)
                self.assertEqual(disabled[0]["line"], line_number)
                self.assertIn("@unittest.skip", disabled[0]["text"])

    def test_guarded_skip_markers_move_between_files_in_every_diff_layer(self):
        original = ("import unittest\n\n"
                    "def test_literal():\n"
                    "    marker = \"@unittest.skip('x')\"\n"
                    "    assert marker\n\n"
                    "@unittest.skip('existing')\n"
                    "def test_skipped():\n"
                    "    assert True\n")
        self.commit_fixture({"tests/test_z_original.py": original})
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        (self.repo / "tests/test_z_original.py").unlink()
        (self.repo / "tests/test_a_moved.py").write_text(original.replace("    ", "        "))
        for layer in ("working-untracked", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                by_path = {item["path"]: item for item in changes}
                self.assertEqual(set(by_path), {"tests/test_z_original.py", "tests/test_a_moved.py"})
                self.assertTrue(by_path["tests/test_z_original.py"]["deleted"])
                self.assertEqual(by_path["tests/test_z_original.py"]["moved"], 0)
                self.assertEqual(by_path["tests/test_a_moved.py"]["moved"], 2)
                for item in changes:
                    self.assertIs(type(item["moved"]), int)
                    self.assertEqual(item["disabled"], [])
                lines = worker.guarded_lines(changes)
                self.assertEqual(lines[-1], "== guarded skip markers moved: 2")
                self.assertEqual(len(lines), 3)
                self.assertFalse(any(line.startswith("== guarded tests disabled:") for line in lines))
                self.assertNotIn("@unittest.", "\n".join(lines))
            if layer == "working-untracked":
                self.git("add", "--all", "--", "tests")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Move guarded marker fixtures")

    def test_guarded_moved_markers_keep_new_skip_and_line_number_in_every_diff_layer(self):
        original = "def test_literal():\n    marker = \"@unittest.skip('x')\"\n    assert marker\n"
        self.commit_fixture({"tests/test_z_original.py": original})
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        (self.repo / "tests/test_z_original.py").unlink()
        (self.repo / "tests/test_a_moved.py").write_text(
            original + "\n@unittest.skip('new')\ndef test_new():\n    assert True\n")
        for layer in ("working-untracked", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                moved = next(item for item in changes if item["path"] == "tests/test_a_moved.py")
                self.assertEqual(moved["moved"], 1)
                self.assertEqual(moved["disabled"], [{"line": 5, "text": "@unittest.skip('new')"}])
                self.assertEqual(sum(len(item["disabled"]) for item in changes), 1)
                self.assertEqual(worker.guarded_lines(changes)[-2:], [
                    "== guarded tests disabled: tests/test_a_moved.py:5 @unittest.skip('new')",
                    "== guarded skip markers moved: 1"])
            if layer == "working-untracked":
                self.git("add", "--all", "--", "tests")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Move fixture and add a skip")

    def test_guarded_changed_skip_is_disabled_in_every_diff_layer(self):
        original = "@unittest.skip('a')\ndef test_a():\n    assert True\n"
        self.commit_fixture({"tests/test_a.py": original})
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        (self.repo / "tests/test_a.py").write_text(original.replace("skip('a')", "skip('b')"))
        for layer in ("working", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]["moved"], 0)
                self.assertEqual(changes[0]["disabled"], [{"line": 1, "text": "@unittest.skip('b')"}])
                lines = worker.guarded_lines(changes)
                self.assertEqual(lines[-1], "== guarded tests disabled: tests/test_a.py:1 @unittest.skip('b')")
                self.assertFalse(any(line.startswith("== guarded skip markers moved:") for line in lines))
            if layer == "working":
                self.git("add", "--", "tests/test_a.py")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Change skip reason")

    def test_guarded_deleted_marker_cancels_only_one_added_copy_in_every_diff_layer(self):
        original = "def test_a():\n    marker = \"@unittest.skip('x')\"\n    assert marker\n"
        self.commit_fixture({"tests/test_z_original.py": original})
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        (self.repo / "tests/test_z_original.py").unlink()
        (self.repo / "tests/test_a_moved.py").write_text(
            original + "\ndef test_b():\n    marker = \"@unittest.skip('x')\"\n    assert marker\n")
        for layer in ("working-untracked", "index", "committed"):
            with self.subTest(layer=layer):
                changes = worker.guarded_changes(state)
                moved = next(item for item in changes if item["path"] == "tests/test_a_moved.py")
                self.assertEqual(moved["moved"], 1)
                self.assertEqual(moved["disabled"], [{"line": 6, "text": "marker = \"@unittest.skip('x')\""}])
                self.assertEqual(sum(len(item["disabled"]) for item in changes), 1)
                self.assertEqual(worker.guarded_lines(changes)[-1], "== guarded skip markers moved: 1")
            if layer == "working-untracked":
                self.git("add", "--all", "--", "tests")
            elif layer == "index":
                self.git("commit", "--quiet", "-m", "Move and duplicate marker fixture")

    def test_guarded_marker_deletions_do_not_cancel_additions_in_other_diff_layers(self):
        layers = ("committed", "index", "working")
        cases = [(removed, added) for removed in layers for added in layers if removed != added]
        fixtures = {}
        for removed, added in cases:
            marker = "@unittest.skip('%s-to-%s')" % (removed, added)
            fixtures["tests/test_z_%s_%s.py" % (removed, added)] = marker + "\ndef test_a():\n    assert True\n"
        self.commit_fixture(fixtures)
        worker = self.worker()
        state = {"worktree": str(self.repo), "base": self.git("rev-parse", "HEAD")}
        for layer in layers:
            for removed, added in cases:
                if removed == layer:
                    (self.repo / ("tests/test_z_%s_%s.py" % (removed, added))).unlink()
                if added == layer:
                    (self.repo / ("tests/test_a_%s_%s.py" % (removed, added))).write_text(
                        fixtures["tests/test_z_%s_%s.py" % (removed, added)])
            if layer != "working":
                self.git("add", "--all", "--", "tests")
                if layer == "committed":
                    self.git("commit", "--quiet", "-m", "Prepare cross-layer marker changes")
        changes = worker.guarded_changes(state)
        by_path = {item["path"]: item for item in changes}
        self.assertEqual(sum(item["moved"] for item in changes), 0)
        self.assertEqual(sum(len(item["disabled"]) for item in changes), 6)
        for removed, added in cases:
            with self.subTest(removed=removed, added=added):
                path = "tests/test_a_%s_%s.py" % (removed, added)
                self.assertEqual(by_path[path]["disabled"], [
                    {"line": 1, "text": "@unittest.skip('%s-to-%s')" % (removed, added)}])
        self.assertFalse(any(line.startswith("== guarded skip markers moved:")
                             for line in worker.guarded_lines(changes)))

    def test_latin1_guarded_patch_finishes_check_review_and_notification(self):
        self.configure(language="en")
        self.configure_notify()
        original = ("# coding: latin-1\n"
                    "import unittest\n"
                    "class Latin1Tests(unittest.TestCase):\n"
                    "    def test_cafe(self):\n"
                    "        self.assertEqual('café', 'café')\n"
                    "if __name__ == '__main__':\n"
                    "    unittest.main()\n")
        changed = original.replace("    def test_cafe(self):\n",
                                   "    @unittest.skip('café')\n    def test_cafe(self):\n")
        changed = changed.replace("self.assertEqual('café', 'café')", "self.assertIn('café', 'café')")
        path = self.repo / "tests/test_latin1.py"
        path.parent.mkdir()
        path.write_bytes(original.encode("latin-1"))
        self.git("add", "--", "tests/test_latin1.py")
        self.git("commit", "--quiet", "-m", "Add Latin-1 test fixture")
        self.git("push", "--quiet", "origin", "main")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tests/test_latin1.py": changed},
             "write_encodings": {"tests/test_latin1.py": "latin-1"},
             "commit_files": ["tests/test_latin1.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-latin1", "--review", "--check", PYTHON + " -I tests/test_latin1.py")
        output = self.wait("guarded-latin1")
        state = self.state("guarded-latin1")
        self.assertIsNone(state["pid"])
        self.assertFalse(state.get("error"))
        self.assertEqual(state["checks"][-1]["outcome"], "passed")
        self.assertIn("== guarded files changed: tests/test_latin1.py (+2 −1)", output)
        self.assertIn("== guarded tests disabled: tests/test_latin1.py:4 @unittest.skip", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertIn("tests/test_latin1.py", self.review_invocations()[0]["prompt"])
        self.assertEqual(self.notification("guarded-latin1", "turn_finished")["guarded_changed"], 1)
        self.assertEqual((Path(state["worktree"]) / "tests/test_latin1.py").read_bytes(),
                         changed.encode("latin-1"))

    def test_guarded_changes_print_counts_and_reach_review_and_notification(self):
        self.configure(language="en")
        self.configure_notify()
        self.commit_fixture({"tests/test_a.py": "def test_a():\n    assert True\n",
                             "src/a.py": "value = 1\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {
                "tests/test_a.py": "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n",
                "src/a.py": "value = 2\n"}, "commit_files": ["tests/test_a.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-counts", "--review")
        output = self.wait("guarded-counts")
        guarded = [line for line in output.splitlines() if line.startswith("== guarded files changed:")]
        self.assertEqual(guarded, ["== guarded files changed: tests/test_a.py (+2 −1)"])
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn("tests/test_a.py", prompt)
        self.assertEqual(prompt, self.worker().review_prompt(
            self.state("guarded-counts"), {"language": "en"}))
        self.assertEqual(self.notification("guarded-counts", "turn_finished")["guarded_changed"], 1)

    def test_disabled_test_markers_ignore_prose_and_match_test_annotations(self):
        pattern = self.worker().DISABLED_TEST
        for line in ("only P3 remains", "Contend only for the reset journal lock",
                     "skip the step", "skipped = True", "xfail = False"):
            with self.subTest(line=line):
                self.assertIsNone(pattern.search(line))
        for line in ("@unittest.skip('reason')", "@unittest.skipIf(True, 'reason')",
                     "@unittest.skipUnless(False, 'reason')", "@unittest.expectedFailure",
                     "self.skipTest('reason')", "@pytest.mark.skip", "@pytest.mark.skipif(True)",
                     "@pytest.mark.xfail", "pytestmark = pytest.mark.skip(reason='fixture')",
                     "pytest.param(1, marks=pytest.mark.xfail)", "pytest.skip('reason')", "it.skip('case', fn)",
                     "it.only('case', fn)", "xit('case', fn)", "xdescribe('suite', fn)",
                     "@Disabled", "@Ignore", "t.Skip('reason')"):
            with self.subTest(line=line):
                self.assertIsNotNone(pattern.search(line))

    def test_guarded_deleted_files_and_added_skips_have_separate_warnings(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_a.py": "def test_a():\n    assert True\n",
                             "tests/test_removed.py": "def test_removed():\n    assert True\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {
                "tests/test_a.py": "@unittest.skip('temporarily disabled')\ndef test_a():\n    assert True\n"},
            "remove_files": ["tests/test_removed.py"]}])
        self.start("guarded-disabled")
        output = self.wait("guarded-disabled")
        deleted = [line for line in output.splitlines() if line.startswith("== guarded files deleted:")]
        disabled = [line for line in output.splitlines() if line.startswith("== guarded tests disabled:")]
        self.assertEqual(len(deleted), 1)
        self.assertIn("tests/test_removed.py", deleted[0])
        self.assertEqual(len(disabled), 1)
        self.assertIn("tests/test_a.py", disabled[0])
        self.assertNotIn("tests/test_removed.py", disabled[0])

    def test_check_paths_and_repeatable_guard_globs_protect_custom_files(self):
        self.configure(language="en")
        self.commit_fixture({"scripts/verify.sh": "printf 'original check\\n'\n",
                             "fixtures/nested/sample.json": "{}\n",
                             "config/check.toml": "enabled = true\n",
                             "src/a.py": "value = 1\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {
                "scripts/verify.sh": "printf 'replacement check\\n'\n",
                "fixtures/nested/sample.json": '{"changed": true}\n',
                "config/check.toml": "enabled = false\n",
                "src/a.py": "value = 2\n"}}])
        self.start("guarded-custom", "--check", "sh scripts/verify.sh", "--guard", "fixtures/**",
                   "--guard", "config/*.toml")
        output = self.wait("guarded-custom")
        state = self.state("guarded-custom")
        self.assertEqual(state["guards"], ["fixtures/**", "config/*.toml"])
        guarded = next(line for line in output.splitlines() if line.startswith("== guarded files changed:"))
        for name in ("scripts/verify.sh", "fixtures/nested/sample.json", "config/check.toml"):
            self.assertIn(name + " (+1 −1)", guarded)
        self.assertNotIn("src/a.py", guarded)
        self.assertEqual(state["checks"][-1]["outcome"], "passed")

    def test_check_paths_find_assignments_options_and_literal_equals_paths(self):
        files = {"scripts/verify.sh": "exit 0\n", "scripts/check=literal.sh": "exit 0\n",
                 "verify=literal.sh": "exit 0\n", "config/check.toml": "enabled = true\n"}
        self.commit_fixture(files)
        command = ('CHECK_SCRIPT=scripts/verify.sh EMPTY= sh "$CHECK_SCRIPT" '
                   '--config=config/check.toml scripts/check=literal.sh verify=literal.sh')
        paths = self.worker().check_paths({"worktree": str(self.repo), "check": command})
        self.assertEqual(paths, sorted(files))
        self.assertNotIn(".", paths, "an empty assignment must not guard every repository file")

    def test_check_paths_only_inspect_relative_top_level_tokens(self):
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        worker = self.worker()
        for command in ("sh -c 'sh scripts/verify.sh'",
                        "sh " + shlex.quote(str(self.repo / "scripts/verify.sh")),
                        "sh scripts/verify.sh 'unterminated"):
            with self.subTest(command=command):
                state = {"worktree": str(self.repo), "check": command}
                self.assertEqual(worker.check_paths(state), [])
                self.assertNotIn("check_paths", state)

    def test_check_paths_separate_shell_punctuation_from_paths(self):
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        worker = self.worker()
        for command in ("sh scripts/verify.sh;", "sh scripts/verify.sh&&true",
                        "sh scripts/verify.sh|cat", "cat>scripts/verify.sh"):
            with self.subTest(command=command):
                state = {"worktree": str(self.repo), "check": command}
                self.assertEqual(worker.check_paths(state), ["scripts/verify.sh"])
                self.assertNotIn("check_paths", state)

    def test_check_paths_return_union_without_mutating_state(self):
        self.commit_fixture({"scripts/first.sh": "exit 0\n", "scripts/second.sh": "exit 0\n"})
        worker = self.worker()
        state = {"worktree": str(self.repo), "check": "sh scripts/first.sh"}
        self.assertEqual(worker.check_paths(state), ["scripts/first.sh"])
        self.assertNotIn("check_paths", state)
        state["check_paths"] = ["scripts/first.sh"]
        state["check"] = "sh scripts/second.sh"
        snapshot = json.loads(json.dumps(state))
        self.assertEqual(worker.check_paths(state), ["scripts/first.sh", "scripts/second.sh"])
        self.assertEqual(state, snapshot)
        (self.repo / "scripts/first.sh").unlink()
        (self.repo / "scripts/second.sh").unlink()
        state["check"] = "sh 'unterminated"
        snapshot = json.loads(json.dumps(state))
        self.assertEqual(worker.check_paths(state), ["scripts/first.sh"])
        self.assertEqual(state, snapshot)

    def test_guarded_changes_review_prompt_and_status_do_not_write_state(self):
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        (self.repo / "scripts/verify.sh").write_text("exit 1\n")
        worker = self.worker()
        state = {"slug": "guarded-read-only", "worktree": str(self.repo),
                 "branch": "test-branch", "base": self.git("rev-parse", "HEAD"),
                 "account": "main", "turns": 1, "pid": None,
                 "check": "sh scripts/verify.sh", "check_paths": []}
        snapshot = json.loads(json.dumps(state))
        with mock.patch.object(worker.task_state, "save") as save:
            self.assertEqual([change["path"] for change in worker.guarded_changes(state)],
                             ["scripts/verify.sh"])
            self.assertIn("scripts/verify.sh", worker.review_prompt(state, {"language": "en"}))
            with mock.patch.object(worker.task_state, "load", return_value=state), \
                    mock.patch.object(worker.procs, "sh", return_value=""), \
                    mock.patch.object(worker.threads, "last_message", return_value=""), \
                    mock.patch("builtins.print"):
                worker.cmd_status(SimpleNamespace(slug=state["slug"]))
        save.assert_not_called()
        self.assertEqual(state, snapshot)

    def test_wait_does_not_overwrite_a_concurrent_say_when_discovering_check_paths(self):
        worker = self.worker()
        slug = "guarded-wait-say-race"
        (worker.config.TASKS / slug).mkdir(parents=True)
        state = {"slug": slug, "worktree": str(self.repo), "branch": "test-branch",
                 "base": self.git("rev-parse", "HEAD"), "account": "main", "thread": "existing-thread",
                 "rotations": [], "turns": 1, "pid": None, "outcome": "completed",
                 "check": "sh scripts/verify.sh", "check_paths": []}
        worker.task_state.save(state)
        script = self.repo / "scripts/verify.sh"
        script.parent.mkdir()
        script.write_text("exit 0\n")
        new_pid = IMPOSSIBLE_PID
        original_popen = worker.subprocess.Popen
        original_guarded_changes = worker.guarded_changes
        launches = []

        def popen(command, *args, **kwargs):
            if len(command) > 3 and command[3] == "_run":
                launches.append(command)
                return SimpleNamespace(pid=new_pid)
            return original_popen(command, *args, **kwargs)

        def describe_old_state(old_state):
            worker.cmd_say(SimpleNamespace(
                slug=slug, file=None, message="Continue working", effort=None, no_check=False,
                check=None, check_timeout=None, review=False, fix_rounds=None, no_base_check=False))
            return original_guarded_changes(old_state)

        with mock.patch.object(worker.config, "accounts_config", return_value={"language": "en"}), \
                mock.patch.object(worker.procs, "alive", side_effect=lambda pid: pid == new_pid), \
                mock.patch.object(worker.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(worker, "guarded_changes", side_effect=describe_old_state), \
                mock.patch.object(worker.task_state, "save", wraps=worker.task_state.save) as save, \
                mock.patch("builtins.print") as printer:
            worker.cmd_wait(SimpleNamespace(slug=slug, timeout=0, through_sleep=False))
        self.assertEqual(len(launches), 1)
        save.assert_called_once()
        current = worker.task_state.load(slug)
        self.assertEqual((current["turns"], current["pid"], current["outcome"]),
                         (2, new_pid, "running"))
        self.assertEqual(current["check_paths"], [], "only the supervisor may save newly found paths")
        output = "\n".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("== guarded files changed: scripts/verify.sh (+1 −0)", output)

    def test_check_assignment_script_is_guarded_restored_and_reviewed(self):
        self.configure(language="en")
        self.configure_notify()
        self.commit_fixture({"scripts/verify.sh": "exit 1\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"scripts/verify.sh": "exit 0\n"},
             "commit_files": ["scripts/verify.sh"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = "CHECK_SCRIPT=scripts/verify.sh sh -c 'sh \"$CHECK_SCRIPT\"'"
        self.start("guarded-assignment", "--review", "--check", command, "--base-check")
        output = self.wait("guarded-assignment")
        state = self.state("guarded-assignment")
        self.assertEqual(state["check"], command, "the saved command must remain unchanged")
        self.assertEqual(state["check_paths"], ["scripts/verify.sh"])
        check = state["checks"][-1]
        self.assertEqual((check["outcome"], check["exit"]), ("passed", 0))
        self.assertEqual((check["base_check"]["outcome"], check["base_check"]["exit"]), ("failed", 1))
        self.assertIn("== guarded files changed: scripts/verify.sh (+1 −1)", output)
        self.assertIn("== base tests: FAILED", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertIn("scripts/verify.sh", self.review_invocations()[0]["prompt"])
        self.assertEqual(self.review_invocations()[0]["prompt"],
                         self.worker().review_prompt(state, {"language": "en"}))
        self.assertLess(output.index("== base tests: FAILED"), output.index("== review by "))
        self.assertEqual(self.notification("guarded-assignment", "turn_finished")["guarded_changed"], 1)
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)
        self.assertEqual((Path(state["worktree"]) / "scripts/verify.sh").read_text(), "exit 0\n")

    def test_builtin_guards_cover_nested_tests_names_and_ci_and_untracked_files(self):
        self.configure(language="en")
        guarded_names = ("pkg/tests/a.py", "pkg/test/a.py", "pkg/__tests__/a.js", "pkg/spec/a.rb",
                         "pkg/test_widget.py", "pkg/widget_test.go", "pkg/widget.test.js",
                         "pkg/widget.spec.ts", ".github/workflows/check.yml", ".gitlab-ci.yml",
                         ".forgejo/workflows/check.yml")
        self.commit_fixture({name: "old\n" for name in guarded_names})
        changes = {name: "new\n" for name in guarded_names}
        changes.update({"tests/test_new.py": "new test\n", "src/a.py": "new source\n",
                        "contest/a.py": "unrelated\n"})
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "write_files": changes}])
        self.start("guarded-patterns")
        output = self.wait("guarded-patterns")
        guarded = next(line for line in output.splitlines() if line.startswith("== guarded files changed:"))
        for name in guarded_names:
            self.assertIn(name + " (+1 −1)", guarded)
        self.assertIn("tests/test_new.py (+1 −0)", guarded)
        self.assertNotIn("src/a.py", guarded)
        self.assertNotIn("contest/a.py", guarded)

    def test_base_check_restores_base_tests_reports_failure_and_still_reviews(self):
        self.configure(language="en")
        base_test = ("from pathlib import Path\n"
                     "assert Path('tests/test_removed.py').is_file(), 'deleted test was not restored'\n"
                     "assert not Path('tests/test_new.py').exists(), 'new test was not removed'\n"
                     "print('base guarded files restored', flush=True)\n"
                     "assert Path('src/a.txt').read_text() == 'expected\\n', 'original assertion failed'\n")
        self.commit_fixture({"tests/test_a.py": base_test, "tests/test_removed.py": "# original test\n",
                             "src/a.txt": "expected\n"})
        changes = {"tests/test_a.py": "print('weakened check passed')\n",
                   "tests/test_new.py": "# replacement test\n", "src/a.txt": "broken\n"}
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": changes, "remove_files": ["tests/test_removed.py"],
             "commit_files": list(changes) + ["tests/test_removed.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        command = PYTHON + " -I tests/test_a.py"
        self.start("guarded-base", "--review", "--check", command, "--base-check")
        output = self.wait("guarded-base")
        state = self.state("guarded-base")
        self.assertTrue(state["base_check"])
        check = state["checks"][-1]
        self.assertEqual((check["outcome"], check["exit"]), ("passed", 0))
        self.assertEqual(check["base_check"]["outcome"], "failed")
        self.assertNotEqual(check["base_check"]["exit"], 0)
        self.assertGreaterEqual(check["base_check"]["seconds"], 0)
        self.assertIn("== base tests: FAILED", output)
        self.assertIn("base guarded files restored", output)
        self.assertIn("original assertion failed", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertLess(output.index("== base tests: FAILED"), output.index("== review by "))
        self.assertNotIn("== review skipped:", output)
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)
        self.assertEqual((Path(state["worktree"]) / "tests/test_a.py").read_text(), changes["tests/test_a.py"])
        self.assertFalse((Path(state["worktree"]) / "tests/test_removed.py").exists())
        self.assertTrue((Path(state["worktree"]) / "tests/test_new.py").is_file())
        telemetry = self.phase_telemetry_journal()
        self.assertEqual([(row["phase"], row["outcome"]) for row in telemetry],
                         [("work", "completed"), ("check", "passed"),
                          ("base_check", "failed"), ("review", "completed")])
        self.assertEqual(telemetry[2]["check_outcome"], "failed")
        self.assertGreaterEqual(telemetry[2]["check_seconds"], 0)

    def test_base_check_skips_absolute_task_paths_and_still_reviews(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_a.py": "assert False, 'base assertion'\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tests/test_a.py": "print('weakened test passed')\n"},
             "commit_files": ["tests/test_a.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        slug = "guarded-absolute"
        worktree = self.home / "worktrees" / ("claude-" + slug) / self.repo.name
        command = shlex.quote(PYTHON) + " -I " + shlex.quote(str(worktree / "tests/test_a.py"))
        reason = "check command uses the task worktree's absolute path"
        self.start(slug, "--review", "--check", command, "--base-check")
        output = self.wait(slug)
        state = self.state(slug)
        check = state["checks"][-1]
        self.assertEqual(state["check"], command, "the saved command must remain unchanged")
        self.assertEqual((check["outcome"], check["exit"]), ("passed", 0))
        self.assertEqual(check["base_check"], {"outcome": "skipped", "reason": reason})
        self.assertIn("== base tests: skipped (" + reason + ")", output)
        self.assertEqual(len(self.review_invocations()), 1)
        prompt = self.review_invocations()[0]["prompt"]
        self.assertEqual(prompt, self.worker().review_prompt(state, {"language": "en"}))
        self.assertIn(reason, prompt)
        self.assertNotIn("== review skipped:", output)
        self.assertFalse((self.home / "claude-tasks" / slug / "base-check.log").exists())
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)
        self.assertIn("weakened test", (worktree / "tests/test_a.py").read_text())

    def test_say_disables_base_check_and_keeps_it_disabled(self):
        self.configure(language="en")
        self.start("guarded-disable-base", "--check", "true", "--base-check")
        output = self.wait("guarded-disable-base")
        self.assertIn("== base tests: passed", output)
        self.assertEqual(self.state("guarded-disable-base")["checks"][0]["base_check"]["outcome"], "passed")
        self.cli("say", "guarded-disable-base", "Disable the baseline comparison", "--no-base-check")
        self.assertNotIn("== base tests:", self.wait("guarded-disable-base"))
        self.cli("say", "guarded-disable-base", "Continue with the saved settings")
        self.assertNotIn("== base tests:", self.wait("guarded-disable-base"))
        state = self.state("guarded-disable-base")
        self.assertFalse(state.get("base_check"))
        self.assertEqual(len(state["checks"]), 3)
        self.assertTrue(all(not check.get("base_check") for check in state["checks"][1:]))

    def test_no_guarded_change_has_no_warning_and_notifies_zero(self):
        self.configure(language="en")
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"src/a.py": "value = 2\n"}}])
        self.start("guarded-none")
        output = self.wait("guarded-none")
        self.assertNotIn("== guarded files changed:", output)
        self.assertNotIn("== guarded files deleted:", output)
        self.assertNotIn("== guarded tests disabled:", output)
        self.assertEqual(self.notification("guarded-none", "turn_finished")["guarded_changed"], 0)

    def test_guarded_index_changes_survive_restoring_working_contents(self):
        self.configure(language="en")
        self.configure_notify()
        original = "def test_a():\n    assert True\n"
        self.commit_fixture({"tests/test_a.py": original})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete"}, {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-index")
        self.wait("guarded-index")
        self.notification("guarded-index", "turn_finished")
        worktree = Path(self.state("guarded-index")["worktree"])
        path = worktree / "tests/test_a.py"
        path.write_text("@unittest.skip('staged weakening')\n" + original)
        staged = subprocess.run(["git", "add", "--", "tests/test_a.py"], cwd=worktree,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        path.write_text(original)
        combined = subprocess.run(["git", "diff", "HEAD", "--", "tests/test_a.py"], cwd=worktree,
                                  env=self.env, text=True, capture_output=True)
        self.assertEqual(combined.returncode, 0, combined.stderr)
        self.assertEqual(combined.stdout, "", "fixture must conceal the staged edit from git diff HEAD")
        output = self.wait("guarded-index")
        guarded = next(line for line in output.splitlines() if line.startswith("== guarded files changed:"))
        disabled = next(line for line in output.splitlines() if line.startswith("== guarded tests disabled:"))
        self.assertIn("tests/test_a.py", guarded)
        self.assertIn("tests/test_a.py", disabled)
        self.cli("review", "guarded-index")
        self.wait("guarded-index")
        self.assertIn("tests/test_a.py", self.review_invocations()[0]["prompt"])
        self.until(lambda: len([event for event in self.notifications("guarded-index")
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications("guarded-index") if event["event"] == "turn_finished"]
        self.assertEqual(finished[-1]["guarded_changed"], 1)

    def test_check_root_directory_path_guards_non_test_files(self):
        self.configure(language="en")
        self.commit_fixture({"src/a.py": "value = 1\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "write_files": {"src/a.py": "value = 2\n"}}])
        command = "test -d . && : " + "x" * 300
        self.start("guarded-root-path", "--check", command)
        output = self.wait("guarded-root-path")
        self.assertIn("== guarded files changed: src/a.py (+1 −1)", output)
        self.assertEqual(self.state("guarded-root-path")["checks"][-1]["outcome"], "passed")

    def test_check_path_stays_guarded_after_worker_deletes_it(self):
        self.configure(language="en")
        self.commit_fixture({"scripts/verify.sh": "exit 0\n"})
        self.account(self.home, "A", self.reset, runs=[{
            "mode": "complete", "remove_files": ["scripts/verify.sh"]}])
        self.start("guarded-deleted-check", "--check", "test ! -e scripts/verify.sh")
        output = self.wait("guarded-deleted-check")
        self.assertIn("== guarded files changed: scripts/verify.sh (+0 −1)", output)
        self.assertIn("== guarded files deleted: scripts/verify.sh", output)
        self.assertEqual(self.state("guarded-deleted-check")["checks"][-1]["outcome"], "passed")

    def test_new_check_path_stays_guarded_after_later_turn_deletes_it(self):
        self.configure(language="en")
        self.configure_notify()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"scripts/verify.sh": "exit 0\n"},
             "commit_files": ["scripts/verify.sh"]},
            {"mode": "complete", "remove_files": ["scripts/verify.sh"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        slug = "guarded-later-deleted-check"
        state_path = self.home / "claude-tasks" / slug / "state.json"
        saved_path_check = ("import json; from pathlib import Path; "
                            "assert json.loads(Path(%r).read_text())['check_paths'] == ['scripts/verify.sh']"
                            % str(state_path))
        command = (PYTHON + " -I -c " + shlex.quote(saved_path_check) +
                   " && { test ! -e scripts/verify.sh || sh scripts/verify.sh; }")
        self.start(slug, "--check", command)
        self.wait(slug)
        self.assertEqual(self.state(slug)["check_paths"], ["scripts/verify.sh"])
        self.notification(slug, "turn_finished")
        self.cli("say", slug, "Remove the generated check script", "--review")
        output = self.wait(slug)
        state = self.state(slug)
        self.assertEqual(state["check_paths"], ["scripts/verify.sh"])
        self.assertEqual([check["outcome"] for check in state["checks"]], ["passed", "passed"])
        self.assertIn("== guarded files changed: scripts/verify.sh (+1 −1)", output)
        self.assertIn("== guarded files deleted: scripts/verify.sh", output)
        self.assertEqual(len(self.review_invocations()), 1)
        self.assertIn("scripts/verify.sh", self.review_invocations()[0]["prompt"])
        self.until(lambda: len([event for event in self.notifications(slug)
                                if event["event"] == "turn_finished"]) == 2)
        finished = [event for event in self.notifications(slug) if event["event"] == "turn_finished"]
        self.assertEqual(finished[-1]["guarded_changed"], 1)

    def test_base_check_timeout_prints_tail_reviews_and_removes_worktree(self):
        self.configure(language="en")
        self.commit_fixture({"tests/test_a.py": "import time\nprint('base timeout marker', flush=True)\ntime.sleep(60)\n"})
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "write_files": {"tests/test_a.py": "print('fast modified test')\n"},
             "commit_files": ["tests/test_a.py"]},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("guarded-base-timeout", "--review", "--check", PYTHON + " -I tests/test_a.py",
                   "--base-check", "--check-timeout", "0.5")
        output = self.wait("guarded-base-timeout")
        state = self.state("guarded-base-timeout")
        check = state["checks"][-1]
        self.assertEqual(check["outcome"], "passed")
        self.assertEqual(check["base_check"]["outcome"], "timeout")
        self.assertGreaterEqual(check["base_check"]["seconds"], 0.5)
        self.assertIn("== base tests: FAILED", output)
        self.assertIn("base timeout marker", output)
        self.assertEqual(len(self.review_invocations()), 1)
        worktrees = self.git("worktree", "list", "--porcelain")
        paths = {line[len("worktree "):] for line in worktrees.splitlines() if line.startswith("worktree ")}
        self.assertEqual(paths, {str(self.repo), state["worktree"]})
        self.assertNotIn("detached", worktrees)

    def test_check_failure_prints_last_forty_lines_and_skips_review(self):
        self.configure()
        command = ("i=1; while [ \"$i\" -le 45 ]; do printf 'check-line-%02d\\n' \"$i\"; "
                   "i=$((i + 1)); done; printf 'failure-stderr\\n' >&2; test -f done.txt")
        self.start("check-failed", "--review", "--check", command)
        output = self.wait("check-failed")
        check = self.state("check-failed")["checks"][-1]
        self.assertEqual((check["cmd"], check["exit"], check["outcome"]), (command, 1, "failed"))
        self.assertIn("== check `" + command + "`: FAILED (exit 1, ", output)
        self.assertIn("check-line-07", output)
        self.assertIn("check-line-45", output)
        self.assertIn("failure-stderr", output)
        self.assertNotIn("check-line-06", output)
        self.assertIn("== review skipped: check failed", output)
        self.assertFalse(self.review_invocations())
        self.assertNotIn("== review by", output)

    def test_check_timeout_kills_the_shell_process_group_and_child(self):
        self.configure()
        command = 'sleep 60 & child=$!; echo "$child" > check-child.pid; wait "$child"'
        self.start("check-timeout", "--review", "--check", command, "--check-timeout", "0.5")
        child = self.check_child("check-timeout")
        output = self.wait("check-timeout")
        state = self.state("check-timeout")
        self.assertEqual(state["checks"][-1]["outcome"], "timeout")
        self.assertGreaterEqual(state["checks"][-1]["seconds"], 0.5)
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(state.get("check_pid"))
        self.assertIn("FAILED", output)
        self.assertIn("timeout", output)
        self.assertIn("== review skipped:", output)
        self.assertFalse(self.review_invocations())

    def test_say_inherits_replaces_and_disables_check_and_timeout(self):
        self.configure()
        first = "printf 'first\\n' >> check-runs"
        second = "printf 'second\\n' >> check-runs"
        self.start("check-say", "--check", first, "--check-timeout", "7")
        self.wait("check-say")
        self.cli("say", "check-say", "Run another working turn")
        inherited = self.wait("check-say")
        self.assertIn("== check `" + first + "`: passed", inherited)
        self.assertEqual(len(self.state("check-say")["checks"]), 2)
        self.assertEqual(self.state("check-say")["check_timeout"], 7)
        self.cli("say", "check-say", "Use the replacement check", "--check", second,
                 "--check-timeout", "9")
        self.wait("check-say")
        self.assertEqual(self.state("check-say")["check"], second)
        self.assertEqual(self.state("check-say")["check_timeout"], 9)
        self.cli("say", "check-say", "Continue without checking", "--no-check")
        disabled = self.wait("check-say")
        self.assertFalse(self.state("check-say").get("check"))
        self.assertNotIn("== check `", disabled)
        self.cli("say", "check-say", "The disabled check stays disabled")
        self.assertNotIn("== check `", self.wait("check-say"))
        state = self.state("check-say")
        self.assertEqual(len(state["checks"]), 3)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text().splitlines(),
                         ["first", "first", "second"])

    def test_stop_during_check_kills_its_group_and_records_stopped(self):
        self.configure()
        command = 'sleep 60 & child=$!; echo "$child" > check-child.pid; wait "$child"'
        self.start("check-stopped", "--review", "--check", command)
        child = self.check_child("check-stopped")
        self.until(lambda: bool(self.state("check-stopped").get("check_pid")))
        self.cli("stop", "check-stopped", timeout=20)
        self.wait("check-stopped")
        state = self.state("check-stopped")
        self.assertEqual(state["outcome"], "stopped")
        self.assertEqual(state["checks"][-1]["outcome"], "stopped")
        self.until(lambda: not self.process_is_live(child))
        self.assertFalse(state.get("check_pid"))
        self.assertFalse(self.review_invocations())

    def test_explicit_review_does_not_run_a_persisted_check(self):
        self.configure(language="en")
        self.start("check-explicit-review", "--check", "printf 'checked\\n' >> check-runs")
        self.wait("check-explicit-review")
        self.cli("review", "check-explicit-review")
        output = self.wait("check-explicit-review")
        state = self.state("check-explicit-review")
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual((Path(state["worktree"]) / "check-runs").read_text(), "checked\n")
        self.assertNotIn("== check `", output)
        self.assertEqual(len(self.review_invocations()), 1)
        prompt = self.review_invocations()[0]["prompt"]
        self.assertIn("printf 'checked\\n' >> check-runs", prompt)
        self.assertEqual(prompt, self.worker().review_prompt(state, {"language": "en"}))

    def test_review_without_saved_check_has_no_passed_suite_instruction(self):
        self.configure(language="en")
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(False)}])
        self.start("review-no-check", "--review")
        self.wait("review-no-check")
        prompt = self.review_invocations()[0]["prompt"]
        self.assertEqual(prompt, self.worker().review_prompt(
            self.state("review-no-check"), {"language": "en"}))
