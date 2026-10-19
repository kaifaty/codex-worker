"""Keep command synopses and flag documentation aligned with the option registry."""
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock


SKILL = (Path(__file__).resolve().parents[1]
         / "plugins/codex-worker/skills/codex-worker")
SCRIPT = SKILL / "scripts/codex_task.py"
COMMANDS = ("start", "say", "review")


class OptionRegistryTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="codex-options-")
        self.addCleanup(home.cleanup)
        with mock.patch.dict(os.environ, {"CODEX_TASK_HOME": home.name}):
            spec = importlib.util.spec_from_file_location("option_worker", str(SCRIPT))
            self.worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.worker)

    def test_docstring_synopses_match_option_registry(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                expected = self.worker.command_synopsis(command)
                lines = [line.strip() for line in self.worker.__doc__.splitlines()
                         if line.strip().startswith("codex_task.py " + command + " ")]
                self.assertEqual(lines, [expected], "Expected synopsis: " + expected)

    def test_registered_flags_are_documented(self):
        paths = [SKILL / "SKILL.md"] + sorted((SKILL / "references").glob("*.md"))
        documentation = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        for option in self.worker.OPTION_REGISTRY:
            for command in COMMANDS:
                if command not in option["commands"]:
                    continue
                for flag in option["names"]:
                    with self.subTest(command=command, flag=flag):
                        self.assertRegex(
                            documentation, r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])",
                            "%s %s is missing from SKILL.md and references/*.md"
                            % (command, flag))


if __name__ == "__main__":
    unittest.main()
