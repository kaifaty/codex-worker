"""Exercise the CLI help comparison against real and synthetic Git trees."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare_cli_help


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_SCRIPT = """import argparse
import os
from pathlib import Path
import sys

home = Path(os.environ['HOME'])
assert home.name == 'home'
assert Path(os.environ['CODEX_TASK_HOME']) == home / '.codex'
assert not Path(os.environ['CODEX_TASK_CODEX']).exists()
assert 'CODEX_TASK_CLOCK_FILE' not in os.environ

parser = argparse.ArgumentParser(description='Synthetic CLI')
commands = parser.add_subparsers(dest='command', required=True)
for name in ('alpha', 'beta', '_run'):
    commands.add_parser(name, description='Original command help')
parser.parse_args()
"""


class CompareCliHelpTests(unittest.TestCase):
    def test_head_help_is_identical(self):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "tests/compare_cli_help.py"),
             "--base", "HEAD"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertRegex(result.stdout.decode("ascii"),
                         r"\APASS: [1-9][0-9]* commands identical\n\Z")
        self.assertEqual(result.stderr, b"")

    def synthetic_repository(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-help-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        script = root / compare_cli_help.SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text(SYNTHETIC_SCRIPT, encoding="utf-8")
        for arguments in (
                ["init", "--quiet"], ["add", "plugins"],
                ["-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
                 "-c", "user.email=test@example.com", "commit", "--quiet",
                 "--no-gpg-sign", "-m", "Add synthetic CLI"]):
            subprocess.run(["git"] + arguments, cwd=str(root), check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return root, script

    def compare_output(self, root):
        output = io.StringIO()
        with mock.patch.object(compare_cli_help, "ROOT", root), \
                contextlib.redirect_stdout(output):
            status = compare_cli_help.main(["--base", "HEAD"])
        return status, output.getvalue()

    def test_all_baseline_commands_run_with_an_isolated_home(self):
        root, _ = self.synthetic_repository()
        with mock.patch.dict("os.environ", {"CODEX_TASK_CLOCK_FILE": "/unused"}):
            status, output = self.compare_output(root)
        self.assertEqual(status, 0)
        self.assertEqual(output, "PASS: 4 commands identical\n")
        self.assertEqual(list(root.rglob("*.pyc")), [])

    def test_stdout_stderr_and_exit_code_differences_are_reported(self):
        root, script = self.synthetic_repository()
        changes = (
            (SYNTHETIC_SCRIPT.replace("Original command help", "Changed command help"),
             ("stdout:", "--- base/stdout", "+b'Changed command help")),
            (SYNTHETIC_SCRIPT.replace(
                "parser.parse_args()",
                "if sys.argv[1:2] == ['beta']:\n"
                "    sys.stderr.buffer.write(b'changed\\xff\\r\\n')\n"
                "parser.parse_args()"),
             ("stderr:", "--- base/stderr", "+b'changed\\xff\\r\\n'")),
            (SYNTHETIC_SCRIPT.replace(
                "parser.parse_args()",
                "if sys.argv[1:2] == ['beta']:\n"
                "    parser.exit(7)\n"
                "parser.parse_args()"),
             ("exit code: 0 -> 7",)),
        )
        for source, expected in changes:
            with self.subTest(expected=expected):
                script.write_text(source, encoding="utf-8")
                status, output = self.compare_output(root)
                self.assertEqual(status, 1)
                self.assertIn("FAIL: codex_task.py beta --help", output)
                for text in expected:
                    self.assertIn(text, output)


if __name__ == "__main__":
    unittest.main()
