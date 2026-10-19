"""Exercise module-split evidence against synthetic Git histories."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_module_split


class CompareModuleSplitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="compare-module-split-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scripts = self.root / compare_module_split.SCRIPTS
        self.scripts.mkdir(parents=True)
        tests = self.root / "tests"
        tests.mkdir()
        self.tool = tests / "compare_module_split.py"
        shutil.copyfile(compare_module_split.__file__, self.tool)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Module Split Tests")
        self.git("config", "user.email", "module-split@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null"] + list(args),
            cwd=str(self.root), check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
        ).stdout.strip()

    def write_tree(self, sources):
        for path in self.scripts.rglob("*.py"):
            path.unlink()
        for name, source in sources.items():
            path = self.scripts / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")

    def commit_tree(self, sources):
        self.write_tree(sources)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Create comparison base")
        return self.git("rev-parse", "HEAD")

    def run_tool(self, base, *args):
        return subprocess.run(
            [sys.executable, "-I", str(self.tool), "--base", base] + list(args),
            cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )

    def compare(self, base, *allowed):
        return compare_module_split.compare(self.root, base, allowed)

    def assert_categories(self, result, **expected):
        counts = {category: len(entries)
                  for category, entries in result["categories"].items() if entries}
        self.assertEqual(counts, expected, result)

    def test_functions_move_with_rewritten_module_references(self):
        base = self.commit_tree({
            "codex_task.py": "def save():\n    return 1\n\ndef run():\n    return save()\n",
        })
        self.write_tree({
            "codex_task.py": "from codex_worker import commands\n",
            "codex_worker/task_state.py": "def save():\n    return 1\n",
            "codex_worker/commands.py": (
                "from . import task_state\n\ndef run():\n    return task_state.save()\n"),
        })
        result = self.compare(base)
        self.assert_categories(result, moved=2)
        command = self.run_tool(base)
        self.assertEqual(command.returncode, 0, command.stderr + command.stdout)
        self.assertIn("moved=2; same=0; changed=0", command.stdout)
        self.assertIn("Glue (base):", command.stdout)

    def test_changed_literal_is_visible_and_can_be_allowed(self):
        base = self.commit_tree({"codex_task.py": "def save():\n    return 1\n"})
        self.write_tree({"codex_task.py": "",
                         "codex_worker/task_state.py": "def save():\n    return 2\n"})
        result = self.compare(base)
        self.assert_categories(result, changed=1)
        self.assertFalse(result["passed"])
        self.assertIn("-    return 1", result["categories"]["changed"][0]["diff"])
        self.assertIn("+    return 2", result["categories"]["changed"][0]["diff"])
        self.assertNotEqual(self.run_tool(base).returncode, 0)
        allowed = self.run_tool(base, "--allow-changed", "save")
        self.assertEqual(allowed.returncode, 0, allowed.stderr + allowed.stdout)

    def test_missing_definition_fails(self):
        base = self.commit_tree({"codex_task.py": "def save():\n    return 1\n"})
        self.write_tree({"codex_task.py": ""})
        result = self.compare(base)
        self.assert_categories(result, missing=1)
        self.assertFalse(result["passed"])
        self.assertNotEqual(self.run_tool(base).returncode, 0)

    def test_duplicate_definition_fails(self):
        base = self.commit_tree({"codex_task.py": "def save():\n    return 1\n"})
        self.write_tree({
            "codex_task.py": "def save():\n    return 1\n",
            "codex_worker/task_state.py": "def save():\n    return 1\n",
        })
        result = self.compare(base)
        self.assert_categories(result, duplicate=1)
        self.assertEqual(len(result["categories"]["duplicate"][0]["after"]), 2)
        self.assertNotEqual(self.run_tool(base, "--allow-changed", "save").returncode, 0)

    def test_wrong_module_prefix_is_not_normalized(self):
        base = self.commit_tree({
            "codex_task.py": "def save():\n    return 1\n\ndef run():\n    return save()\n",
        })
        self.write_tree({
            "codex_task.py": "",
            "codex_worker/config.py": "",
            "codex_worker/task_state.py": "def save():\n    return 1\n",
            "codex_worker/commands.py": (
                "from . import config\n\ndef run():\n    return config.save()\n"),
        })
        result = self.compare(base)
        self.assert_categories(result, moved=1, changed=1)
        self.assertIn("config.save()", result["categories"]["changed"][0]["diff"])
        self.assertNotEqual(self.run_tool(base).returncode, 0)

    def test_already_split_base_is_normalized_independently(self):
        base = self.commit_tree({
            "codex_task.py": ("from codex_worker import task_state as state\n\n"
                              "def run():\n    return state.save()\n"),
            "codex_worker/task_state.py": "def save():\n    return 1\n",
        })
        self.write_tree({
            "codex_task.py": "from codex_worker import commands\n",
            "codex_worker/task_state.py": "def save():\n    return 1\n",
            "codex_worker/commands.py": (
                "import codex_worker.task_state as state\n\n"
                "def run():\n    return state.save()\n"),
        })
        result = self.compare(base)
        self.assert_categories(result, moved=1, same=1)
        command = self.run_tool(base)
        self.assertEqual(command.returncode, 0, command.stderr + command.stdout)

    def test_assignments_classes_and_docstrings_are_compared(self):
        source = ("A = B = 1\nC, D = (2, 3)\nE: int = 4\n"
                  "class Example:\n    \"\"\"Keep this documentation.\"\"\"\n    pass\n"
                  "async def run():\n    return A\n")
        base = self.commit_tree({"codex_task.py": source})
        self.write_tree({"codex_task.py": "", "codex_worker/config.py": source})
        result = self.compare(base)
        self.assert_categories(result, moved=5)
        self.assertIn(["A", "B"], [entry["names"] for entry in result["categories"]["moved"]])
        self.write_tree({"codex_task.py": "", "codex_worker/config.py": source.replace(
            "Keep this documentation.", "Changed documentation.")})
        result = self.compare(base)
        self.assert_categories(result, moved=4, changed=1)
        self.assertFalse(result["passed"])

    def test_new_definitions_are_allowed_and_reported(self):
        base = self.commit_tree({"codex_task.py": "VALUE = 1\n"})
        self.write_tree({"codex_task.py": "VALUE = 1\nEXTRA = 2\n"})
        result = self.compare(base)
        self.assert_categories(result, same=1, new=1)
        self.assertTrue(result["passed"])
        self.assertEqual(result["categories"]["new"][0]["names"], ["EXTRA"])

    def test_external_and_rebound_aliases_are_not_normalized(self):
        base = self.commit_tree({
            "codex_task.py": "def save():\n    return 1\n\ndef run():\n    return save()\n",
        })
        for import_statement, definition in (
            ("import external as state", "def run():\n    return state.save()\n"),
            ("from . import state", "def run(state):\n    return state.save()\n"),
        ):
            with self.subTest(import_statement=import_statement):
                self.write_tree({
                    "codex_task.py": "",
                    "codex_worker/state.py": "def save():\n    return 1\n",
                    "codex_worker/commands.py": import_statement + "\n" + definition,
                })
                result = self.compare(base)
                self.assert_categories(result, moved=1, changed=1)
                self.assertIn("state.save()", result["categories"]["changed"][0]["diff"])

    def test_entrypoint_alias_is_not_a_package_module_prefix(self):
        base = self.commit_tree({
            "codex_task.py": "def save():\n    return 1\n\ndef run():\n    return save()\n",
        })
        self.write_tree({
            "codex_task.py": "def save():\n    return 1\n",
            "codex_worker/commands.py": (
                "import codex_task as state\n\ndef run():\n    return state.save()\n"),
        })
        result = self.compare(base)
        self.assert_categories(result, same=1, changed=1)
        self.assertFalse(result["passed"])
        self.assertIn("state.save()", result["categories"]["changed"][0]["diff"])


if __name__ == "__main__":
    unittest.main()
