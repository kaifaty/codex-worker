"""Keep command synopses and flag documentation aligned with the option registry."""
from pathlib import Path
import re
import sys
import tempfile
import unittest


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_loader import load_worker


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/codex-worker/skills/codex-worker"
DOCS = ROOT / "src/codex_worker/docs"
COMMANDS = ("start", "say", "review")


class OptionRegistryTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory(prefix="codex-options-")
        self.addCleanup(home.cleanup)
        self.worker = load_worker({"CODEX_TASK_HOME": home.name}, name="option_worker")

    def test_docstring_synopses_match_option_registry(self):
        for command in ("--version", "docs [<name>]", "path"):
            self.assertIn("codex-worker " + command, self.worker.cli.__doc__)
        for command in COMMANDS:
            with self.subTest(command=command):
                expected = self.worker.cli.command_synopsis(command)
                lines = [line.strip() for line in self.worker.cli.__doc__.splitlines()
                         if line.strip().startswith("codex-worker " + command + " ")]
                self.assertEqual(lines, [expected], "Expected synopsis: " + expected)

    def test_registered_flags_are_documented(self):
        paths = [SKILL / "SKILL.md"] + sorted(DOCS.glob("*.md"))
        documentation = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        for option in self.worker.cli.OPTION_REGISTRY:
            for command in COMMANDS:
                if command not in option["commands"]:
                    continue
                for flag in option["names"]:
                    with self.subTest(command=command, flag=flag):
                        self.assertRegex(
                            documentation, r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])",
                            "%s %s is missing from SKILL.md and docs/*.md"
                            % (command, flag))


if __name__ == "__main__":
    unittest.main()
