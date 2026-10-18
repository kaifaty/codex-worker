"""Check process ownership without inspecting or signalling real processes."""
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_cleanup


class ProcessCleanupTests(unittest.TestCase):
    def _owned(self, root, commands, groups=()):
        snapshot = "\n".join("%d 1 %d S %s" % (pid, group, command)
                             for pid, group, command in commands)
        result = subprocess.CompletedProcess([], 0, snapshot, "")
        with mock.patch.object(process_cleanup, "_fixture_processes"), \
                mock.patch.object(process_cleanup.subprocess, "run", return_value=result), \
                mock.patch.object(process_cleanup.os, "waitpid", side_effect=ChildProcessError):
            return process_cleanup._owned_processes(root, set(), set(groups))

    def test_directory_ownership_does_not_include_similarly_named_workers(self):
        root = Path("/fixture/1")
        commands = [
            (424201, 424201, "python /fixture/1/child.py"),
            (424210, 424210, "python /fixture/10/child.py"),
            (424211, 424211, "python /fixture/11/child.py"),
            (424219, 424219, "python /fixture/19/child.py"),
            (424220, 424220, "python /fixture/1-other/child.py"),
            (424221, 424221, "python /elsewhere/fixture/1/child.py"),
        ]
        self.assertEqual(self._owned(root, commands), {424201})

    def test_directory_ownership_accepts_exact_argv_and_environment_paths_with_spaces(self):
        root = Path("/fixture with spaces/1")
        commands = [
            (424201, 424201, "python --directory=/fixture with spaces/1"),
            (424202, 424202, "python TMPDIR=/fixture with spaces/1/tmp"),
            (424203, 424203, "python '/fixture with spaces/1'"),
            (424204, 424204, "python TMPDIR=/fixture with spaces/10/tmp"),
        ]
        self.assertEqual(self._owned(root, commands), {424201, 424202, 424203})

    def test_directory_ownership_accepts_raw_and_resolved_root_spellings(self):
        commands = [
            (424201, 424201, "python TMPDIR=/var/fixture/1/tmp"),
            (424202, 424202, "python TMPDIR=/private/var/fixture/1/tmp"),
            (424203, 424203, "python TMPDIR=/private/var/fixture/10/tmp"),
        ]
        with mock.patch.object(Path, "resolve", return_value=Path("/private/var/fixture/1")):
            self.assertEqual(self._owned(Path("/var/fixture/1"), commands), {424201, 424202})

    def test_known_process_group_finds_children_after_leader_exits(self):
        commands = [(424201, 424200, "python -I -c import time; time.sleep(60)")]
        self.assertEqual(self._owned(Path("/fixture/1"), commands, groups=(424200,)), {424201})

    def test_cleanup_signals_known_process_group(self):
        with mock.patch.object(process_cleanup, "_owned_processes",
                               side_effect=[{424201}, set()]) as owned, \
                mock.patch.object(process_cleanup.os, "killpg") as killpg, \
                mock.patch.object(process_cleanup.os, "kill") as kill, \
                mock.patch.object(process_cleanup.time, "sleep"):
            self.assertEqual(process_cleanup.cleanup_processes(
                Path("/fixture/1"), process_groups=(424200,)), set())
        self.assertEqual(owned.call_args_list[0].args[2], {424200})
        killpg.assert_called_once_with(424200, signal.SIGTERM)
        kill.assert_called_once_with(424201, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
