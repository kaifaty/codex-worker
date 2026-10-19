"""Prove module-hop normalization preserves meaningful assertion changes."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


COMPARATOR = Path(__file__).with_name("compare_rotation_assertions.py")
SCRIPTS = Path("plugins/codex-worker/skills/codex-worker/scripts")
BEFORE = '''\
class Example:
    def helper(self):
        self.assertEqual(worker.save(), 1)

    def test_references(self):
        worker.save()
        self.worker.save()
        self.worker().save()
        with mock.patch.object(worker, "save"):
            self.assertEqual(worker.save(), 1)
        with mock.patch.object(self.worker, "save"):
            self.assertEqual(self.worker.save(), 1)
        self.state(slug)
        self.assertEqual(other.task_state.save(), 1)
        self.assertEqual(worker.external.save(), 1)
'''
AFTER = BEFORE.replace("worker.save", "worker.task_state.save").replace(
    "worker().save", "worker().task_state.save").replace(
    "patch.object(worker,", "patch.object(worker.task_state,").replace(
    "patch.object(self.worker,", "patch.object(self.worker.task_state,")


class CompareRotationAssertionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-compare-assertions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tests = self.root / "tests"
        tests.mkdir()
        shutil.copyfile(COMPARATOR, tests / COMPARATOR.name)
        self.source = tests / "rotation_example.py"
        self.source.write_text(BEFORE, encoding="utf-8")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git("-c", "user.name=Comparator Test", "-c", "user.email=test@example.com",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--no-gpg-sign",
                 "-m", "Baseline")
        self.base = self.git("rev-parse", "HEAD").strip()
        modules = self.root / SCRIPTS / "codex_worker"
        modules.mkdir(parents=True)
        for name in ("task_state", "state"):
            (modules / (name + ".py")).write_text("", encoding="utf-8")
        self.source.write_text(AFTER, encoding="utf-8")

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments], cwd=str(self.root), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout

    def compare(self, strip=True):
        command = [sys.executable, "-I", str(self.root / "tests" / COMPARATOR.name),
                   "--base", self.base, "--json"]
        if strip:
            command.append("--strip-module-hops")
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_module_hops_leave_test_and_helper_bodies_identical(self):
        code, result = self.compare()
        self.assertEqual(code, 0)
        self.assertEqual(result["changed_tests"], [])
        self.assertEqual(result["changed_helpers"], [])
        self.assertEqual(result["before_test_count"], 1)
        self.assertEqual(result["after_test_count"], 1)

    def test_without_flag_preserves_existing_comparison(self):
        code, result = self.compare(strip=False)
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 1)
        self.assertEqual(len(result["changed_helpers"]), 1)

    def test_changed_assertion_remains_visible(self):
        self.source.write_text(AFTER.replace("self.assertEqual(worker.task_state.save(), 1)",
                                             "self.assertEqual(worker.task_state.save(), 2)"),
                               encoding="utf-8")
        code, result = self.compare()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 1)
        self.assertEqual(len(result["changed_helpers"]), 1)
        self.assertEqual(len(result["changed_tests"][0]["added_assertions"]), 1)

    def test_only_one_known_module_hop_at_a_worker_expression_is_stripped(self):
        changes = (
            ("self.state(slug)", "self(slug)"),
            ("other.task_state.save()", "other.save()"),
            ("worker.external.save()", "worker.save()"),
            ("worker.task_state.save()", "worker.task_state.task_state.save()"),
        )
        for original, replacement in changes:
            with self.subTest(original=original):
                self.source.write_text(AFTER.replace(original, replacement), encoding="utf-8")
                _, result = self.compare()
                self.assertEqual(len(result["changed_tests"]), 1)


if __name__ == "__main__":
    unittest.main()
