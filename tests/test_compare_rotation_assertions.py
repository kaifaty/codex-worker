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
EMBEDDED_BEFORE = '''\
class Example:
    def helper(self):
        self.assertEqual(run_script(
            "worker.save(x)\\n"
            "self.worker.save(x)\\n"
            "self.worker().save(x)\\n"
            "with mock.patch.object(worker, 'save'):\\n"
            "    worker.save(x)\\n"), 0)

    def test_embedded_script(self):
        self.assertEqual(run_script(
            "worker.save(x)\\n"
            "print('finished')\\n"), 0)
'''
EMBEDDED_AFTER = EMBEDDED_BEFORE.replace(
    "worker.save", "worker.task_state.save").replace(
    "worker().save", "worker().task_state.save").replace(
    "patch.object(worker,", "patch.object(worker.task_state,")


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

    def set_sources(self, before, after):
        self.source.write_text(before, encoding="utf-8")
        self.git("add", str(self.source.relative_to(self.root)))
        self.git("-c", "user.name=Comparator Test", "-c", "user.email=test@example.com",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--no-gpg-sign",
                 "-m", "Embedded script baseline")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.source.write_text(after, encoding="utf-8")

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

    def test_embedded_scripts_leave_test_and_helper_bodies_identical(self):
        self.set_sources(EMBEDDED_BEFORE, EMBEDDED_AFTER)
        code, result = self.compare()
        self.assertEqual(code, 0)
        self.assertEqual(result["changed_tests"], [])
        self.assertEqual(result["changed_helpers"], [])

    def test_embedded_scripts_without_flag_preserve_raw_changes(self):
        self.set_sources(EMBEDDED_BEFORE, EMBEDDED_AFTER)
        code, result = self.compare(strip=False)
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 1)
        self.assertEqual(len(result["changed_helpers"]), 1)

    def test_changed_embedded_script_argument_remains_visible(self):
        self.set_sources(EMBEDDED_BEFORE, EMBEDDED_AFTER.replace(
            "worker.task_state.save(x)", "worker.task_state.save(y)"))
        code, result = self.compare()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 1)
        self.assertEqual(len(result["changed_helpers"]), 1)
        self.assertEqual(len(result["changed_tests"][0]["added_assertions"]), 1)

    def test_unparseable_strings_preserve_module_hops(self):
        before = '''\
class Example:
    def test_message(self):
        self.assertEqual("Not Python: worker.save(x)!", "expected")
'''
        self.set_sources(before, before.replace("worker.save", "worker.task_state.save"))
        code, result = self.compare()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 1)
        self.assertEqual(len(result["changed_tests"][0]["added_assertions"]), 1)

    def test_parseable_strings_without_module_hops_preserve_raw_changes(self):
        before = '''\
class Example:
    def test_message(self):
        self.assertEqual("value = 1", "expected")
'''
        self.set_sources(before, before.replace("value = 1", "value=1"))
        code, result = self.compare()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 1)
        self.assertEqual(len(result["changed_tests"][0]["added_assertions"]), 1)

    def test_changed_parseable_assertion_literal_remains_visible(self):
        before = '''\
class Example(unittest.TestCase):
    def test_message(self):
        self.assertEqual("worker.save()", "worker.save()")
'''
        after = before.replace('self.assertEqual("worker.save()",',
                               'self.assertEqual("worker.task_state.save()",')
        for source, failures in ((before, 0), (after, 1)):
            with self.subTest(failures=failures):
                namespace = {"unittest": unittest}
                exec(source, namespace)
                result = unittest.TestResult()
                unittest.defaultTestLoader.loadTestsFromTestCase(
                    namespace["Example"]).run(result)
                self.assertEqual(result.testsRun, 1)
                self.assertEqual(len(result.failures), failures)
                self.assertEqual(result.errors, [])

        self.set_sources(before, after)
        for strip in (False, True):
            with self.subTest(strip=strip):
                code, result = self.compare(strip=strip)
                self.assertEqual(code, 1)
                self.assertEqual(len(result["changed_tests"]), 1)
                self.assertEqual(len(result["changed_tests"][0]["removed_assertions"]), 1)
                self.assertEqual(len(result["changed_tests"][0]["added_assertions"]), 1)

    def test_changed_parseable_assertion_data_remains_visible(self):
        cases = (
            ('"worker.save()", "worker.save()"',
             '"worker.save()", "worker.task_state.save()"'),
            ('first="worker.save()", second="worker.save()"',
             'first="worker.save()", second="worker.task_state.save()"'),
            ('1, 1, msg="worker.save()"',
             '1, 1, msg="worker.task_state.save()"'),
            ('["worker.save()"], ["worker.save()"]',
             '["worker.task_state.save()"], ["worker.save()"]'),
            ('{"key": ("worker.save()",)}, {"key": ("worker.save()",)}',
             '{"key": ("worker.task_state.save()",)}, {"key": ("worker.save()",)}'),
            ('{"worker.save()"}, {"worker.save()"}',
             '{"worker.task_state.save()"}, {"worker.save()"}'),
        )
        before, after = ["class Example:\n"], ["class Example:\n"]
        for index, (original, current) in enumerate(cases):
            template = ("    def test_message_{}(self):\n"
                        "        self.assertEqual({{}})\n").format(index)
            before.append(template.format(original))
            after.append(template.format(current))
        self.set_sources("".join(before), "".join(after))
        code, result = self.compare()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), len(cases))
        for changed in result["changed_tests"]:
            with self.subTest(name=changed["name"]):
                self.assertEqual(len(changed["removed_assertions"]), 1)
                self.assertEqual(len(changed["added_assertions"]), 1)

    def test_changed_parseable_bare_assert_literal_remains_visible(self):
        before = '''\
class Example:
    def test_message(self):
        assert "worker.save()" == "worker.save()"
'''
        self.set_sources(before, before.replace('assert "worker.save()"',
                                               'assert "worker.task_state.save()"'))
        _, result = self.compare()
        self.assertEqual(len(result["changed_tests"]), 1)

    def test_changed_wrapped_assertion_data_remains_visible(self):
        before, after = ["class Example:\n"], ["class Example:\n"]
        for index, function in enumerate(("str", "format_text")):
            template = ("    def test_wrapped_message_{index}(self):\n"
                        "        self.assertEqual({function}({value!r}), 'worker.save()')\n")
            before.append(template.format(index=index, function=function,
                                          value="worker.save()"))
            after.append(template.format(index=index, function=function,
                                         value="worker.task_state.save()"))
        self.set_sources("".join(before), "".join(after))
        code, result = self.compare()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["changed_tests"]), 2)
        for changed in result["changed_tests"]:
            with self.subTest(name=changed["name"]):
                self.assertEqual(len(changed["removed_assertions"]), 1)
                self.assertEqual(len(changed["added_assertions"]), 1)


if __name__ == "__main__":
    unittest.main()
