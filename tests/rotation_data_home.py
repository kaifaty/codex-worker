"""The data home: resolving rules, separate homes and migrate-home."""
import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rotation_support import PYTHON, SCRIPT, RotationTestCase
from worker_loader import load_worker


UNSET = {"TEAMSTER_HOME": "", "CODEX_TASK_HOME": ""}


def tree(root):
    """Every entry under root: file bytes, symlink targets and directories, without following links."""
    entries = {}
    for directory, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if path.is_symlink():
                entries[relative] = "-> " + os.readlink(path)
            elif path.is_dir():
                entries[relative] = "dir"
            else:
                entries[relative] = path.read_bytes()
    return entries


class DataHomeTests(RotationTestCase):
    def user_worker(self, user, **variables):
        return load_worker(dict(UNSET, HOME=str(user), **variables), name="data_home_worker")

    def legacy_user_home(self):
        """Move the fixture's Codex home to ~/.codex of a temporary HOME, with no home variables."""
        self.user = self.root / "user"
        self.user.mkdir()
        codex = self.user / ".codex"
        os.rename(self.home, codex)
        self.home = codex
        self.env["HOME"] = str(self.user)
        for name in UNSET:
            self.env.pop(name, None)
        self.configure()
        (self.home / "claude-accounts.json").rename(self.home / "codex-worker.json")

    def legacy_fixture(self, user):
        """A legacy data home with every kind of item next to Codex's own files."""
        codex = user / ".codex"
        for name in ("claude-tasks/x", "claude-tasks/.archive/y-20260101T000000Z", "worktrees/claude-x/repo",
                     "sessions/2026", "checks", "briefs"):
            (codex / name).mkdir(parents=True)
        (codex / "claude-tasks/x/state.json").write_text(json.dumps({"slug": "x", "pid": None}))
        (codex / "claude-tasks/.archive/y-20260101T000000Z/state.json").write_text('{"slug": "y"}')
        (codex / "worktrees/claude-x/repo/file.txt").write_text("work\n")
        for name, text in (("codex-worker.json", "{}"), ("claude-accounts.json", "{}"),
                           ("codex-worker-log.jsonl", "{}\n"), ("codex-worker-resets.jsonl", "{}\n"),
                           ("codex-worker-rules.md", "rules\n"), ("duty.json", "{}"), ("duty-log.jsonl", "{}\n"),
                           ("duty-launchd.log", "tick\n"), ("board-machines.json", "{}"),
                           ("board-server.log", "served\n"), ("codex-worker.json.bak-1", "{}"),
                           ("config.toml", "model = 'fake'\n"), ("sessions/2026/rollout.jsonl", "{}\n"),
                           ("checks/check.log", "ok\n"), ("briefs/brief.md", "brief\n")):
            (codex / name).write_text(text)
        (user / ".codex-accounts/second").mkdir(parents=True)
        (user / ".codex-accounts/second/config.toml").write_text("model = 'fake'\n")
        return codex

    def migrate(self, user, apply=True, **variables):
        worker = self.user_worker(user, **variables)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            worker.home_migration.cmd_migrate_home(SimpleNamespace(apply=apply))
        return output.getvalue()

    def assert_refused(self, user, reason, **variables):
        before = tree(user)
        for apply in (False, True):
            with self.subTest(reason=reason, apply=apply):
                with self.assertRaises(SystemExit) as raised:
                    self.migrate(user, apply=apply, **variables)
                self.assertIn(reason, str(raised.exception))
                self.assertEqual(tree(user), before)

    def report(self):
        """The report without the window it was generated for."""
        report = json.loads(self.cli("report", "--json"))
        for key in ("since", "until"):
            report.pop(key, None)
        return report

    def test_resolving_rules(self):
        user = self.root / "user"
        codex, data, other = user / ".codex", user / ".teamster", self.root / "other"
        for directory in (codex, other):
            directory.mkdir(parents=True)

        def homes(**variables):
            config = self.user_worker(user, **variables).config
            return (config.CODEX_HOME, config.DATA_HOME, config.LAYOUT, config.TASKS.name,
                    config.settings_path().name, config.data_path("telemetry").name)

        legacy = ("claude-tasks", "codex-worker.json", "codex-worker-log.jsonl")
        new = ("tasks", "config.json", "log.jsonl")
        self.assertEqual(homes(), (codex, data, "new") + new)
        for marker in (user / ".teamster.migrating", codex / "claude-tasks", codex / "codex-worker.json"):
            with self.subTest(marker=marker.name):
                marker.mkdir()
                self.assertEqual(homes(), (codex, codex, "legacy") + legacy)
                marker.rmdir()
        (codex / "claude-accounts.json").write_text("{}")
        (codex / "claude-tasks").mkdir()
        self.assertEqual(homes(), (codex, codex, "legacy", "claude-tasks", "claude-accounts.json",
                                   "codex-worker-log.jsonl"))
        data.mkdir()
        self.assertEqual(homes(), (codex, data, "new") + new)
        self.assertEqual(homes(TEAMSTER_HOME=str(other)), (codex, other, "new") + new)
        self.assertEqual(homes(TEAMSTER_HOME=str(other), CODEX_TASK_HOME=str(data)), (data, other, "new") + new)
        self.assertEqual(homes(CODEX_TASK_HOME=str(other)), (other, other, "legacy") + legacy)
        # An old launchd job keeps CODEX_TASK_HOME and follows the marker left by migrate-home.
        (codex / "teamster-home").write_text(str(other) + "\n")
        self.assertEqual(homes(CODEX_TASK_HOME=str(codex)), (codex, other, "new") + new)

    def test_both_home_variables_keep_files_apart(self):
        data = self.root / "data"
        self.configure()
        data.mkdir()
        (self.home / "claude-accounts.json").rename(data / "config.json")
        # A relative TEAMSTER_HOME reaches the supervisor, which runs in the worktree, resolved.
        self.env["TEAMSTER_HOME"] = os.path.relpath(data, self.repo)
        codex_before = set(os.listdir(self.home))
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}])
        self.start("apart")
        self.cli("wait", "apart", "--timeout", "90")
        self.cli("verdict", "apart", "accepted")
        state = json.loads((data / "tasks/apart/state.json").read_text())
        self.assertEqual(state["outcome"], "completed")
        self.assertEqual(Path(state["worktree"]).parent.parent, data / "worktrees")
        self.assertTrue((data / "log.jsonl").is_file())
        ours = {name for layout in self.worker().config.LAYOUTS.values() for name in layout.values() if name}
        self.assertEqual(ours & set(os.listdir(self.home)), set())
        self.assertTrue({"sessions", "exec-count"} <= set(os.listdir(self.home)) - codex_before)
        self.assertEqual({"config.toml", "fixture.json", "sessions", "exec-count"} & set(os.listdir(data)), set())
        # launchd runs the job from another directory, so the plist holds absolute homes and logs.
        job = plistlib.loads(self.cli("duty", "plist").encode())
        environment = job["EnvironmentVariables"]
        for directory in (self.repo, job.get("WorkingDirectory", "/")):
            self.assertEqual(Path(directory, environment["TEAMSTER_HOME"]), data)
            self.assertEqual(Path(directory, environment["CODEX_TASK_HOME"]), self.home)
        self.assertEqual(job["StandardOutPath"], str(data / "duty-launchd.log"))

    def test_migrate_home_refusals_change_nothing(self):
        user = self.root / "user"
        codex = self.legacy_fixture(user)
        for name in ("TEAMSTER_HOME", "CODEX_TASK_HOME"):
            self.assert_refused(user, name + " is set", **{name: str(codex)})
        real_stat = os.stat

        def other_device(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if str(path) == str(codex):
                return os.stat_result((result.st_mode, result.st_ino, result.st_dev + 1) + tuple(result)[3:])
            return result

        with mock.patch.object(os, "stat", side_effect=other_device):
            self.assert_refused(user, "are on different devices")
        state = codex / "claude-tasks/x/state.json"
        scout = codex / "claude-tasks/s/state.json"
        scout.parent.mkdir()
        # A sleeping task and a scout both keep a live supervisor.
        state.write_text(json.dumps({"slug": "x", "pid": os.getpid(), "outcome": "sleeping"}))
        scout.write_text(json.dumps({"slug": "s", "kind": "scout", "pid": os.getpid()}))
        self.assert_refused(user, "tasks are running: s, x")
        state.write_text(json.dumps({"slug": "x", "pid": None}))
        scout.unlink()
        scout.parent.rmdir()
        (user / ".teamster").mkdir()
        (user / ".teamster/note.txt").write_text("someone else's\n")
        self.assert_refused(user, "exists and is not empty")
        (user / ".teamster/note.txt").unlink()
        self.assert_refused(user, "remove the empty directory first")
        (user / ".teamster").rmdir()
        # Plan mode lists the moves and changes nothing.
        before = tree(user)
        self.assertIn("nothing changed", self.migrate(user, apply=False))
        self.assertEqual(tree(user), before)
        empty = self.root / "empty-user"
        (empty / ".codex").mkdir(parents=True)
        (empty / ".codex/config.toml").write_text("model = 'fake'\n")
        self.assert_refused(empty, "already in the new layout")
        self.migrate(user)
        self.assert_refused(user, "already in the new layout")

    def test_migrate_home_crash_after_each_rename_resumes(self):
        reference = self.root / "reference"
        self.legacy_fixture(reference)
        renames = []
        real_rename = os.rename

        def counted(source, target):
            renames.append((source, target))
            return real_rename(source, target)

        with mock.patch.object(os, "rename", side_effect=counted):
            self.migrate(reference)
        expected = {name: value.replace(str(reference).encode(), b"<user>") if isinstance(value, bytes)
                    else value.replace(str(reference), "<user>") for name, value in tree(reference).items()}
        self.assertNotIn(".codex/claude-tasks", expected)
        self.assertEqual(expected[".teamster/worktrees"], "-> <user>/.codex/worktrees")
        self.assertEqual(expected[".codex/teamster-home"], b"<user>/.teamster\n")
        record = json.loads(expected[".teamster/migration.json"])
        self.assertEqual(len(record["moved"]), 10)
        self.assertIn({"from": "<user>/.codex/codex-worker.json", "to": "<user>/.teamster/config.json"},
                      record["moved"])
        for kept in (".codex/claude-accounts.json", ".codex/codex-worker.json.bak-1", ".codex/checks/check.log",
                     ".codex/briefs/brief.md", ".codex/sessions/2026/rollout.jsonl", ".codex/config.toml",
                     ".codex-accounts/second/config.toml", ".codex/worktrees/claude-x/repo/file.txt"):
            self.assertIn(kept, expected)
        # One rename writes the record, ten move items, one publishes the home and one writes the marker.
        self.assertEqual(len(renames), 13)
        for crash in range(1, len(renames) + 1):
            with self.subTest(crash=crash):
                user = self.root / f"crash-{crash}"
                codex = self.legacy_fixture(user)
                calls = []

                def failing(source, target):
                    calls.append(source)
                    if len(calls) == crash:
                        raise OSError("fixture crash")
                    return real_rename(source, target)

                with mock.patch.object(os, "rename", side_effect=failing), self.assertRaises(OSError):
                    self.migrate(user)
                config = self.user_worker(user).config
                if not (user / ".teamster").exists():
                    self.assertEqual((config.DATA_HOME, config.LAYOUT), (codex, "legacy"))
                    if (codex / "claude-tasks").exists():
                        self.assertEqual(self.user_worker(user).task_state.load("x")["slug"], "x")
                    with self.assertRaises(SystemExit) as raised, config.home_lock():
                        pass
                    self.assertIn("migration is unfinished", str(raised.exception))
                elif not (codex / "teamster-home").exists():
                    # Old jobs with CODEX_TASK_HOME=~/.codex still resolve to legacy and must not write there.
                    legacy = self.user_worker(user, CODEX_TASK_HOME=str(codex)).config
                    self.assertEqual(legacy.LAYOUT, "legacy")
                    with self.assertRaises(SystemExit) as raised, legacy.home_lock():
                        pass
                    self.assertIn("migration is unfinished", str(raised.exception))
                self.assertIn("finishes it", self.migrate(user, apply=False))
                self.migrate(user)
                actual = {name: value.replace(str(user).encode(), b"<user>") if isinstance(value, bytes)
                          else value.replace(str(user), "<user>") for name, value in tree(user).items()}
                self.assertEqual(actual, expected)

    def test_start_waiting_for_the_migration_lock_stops(self):
        """A start that resolved the legacy home before the move must not write there after it."""
        self.legacy_user_home()
        lock = self.home / "codex-worker.lock"
        command = [PYTHON, "-I", str(SCRIPT), "start", "late", "--brief", str(self.brief), "--repo", str(self.repo)]
        started = []
        real_rename = os.rename

        def first_move_waits_for_start(source, target):
            if not started:
                # The migration holds the settings lock now; start resolves the legacy home and waits on it.
                started.append(subprocess.Popen(command, cwd=self.repo, env=self.env, text=True,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                pid = str(started[0].pid)
                self.until(lambda: str(lock) in subprocess.run(["lsof", "-p", pid], capture_output=True,
                                                               text=True).stdout, timeout=30)
                self.assertIsNone(started[0].poll())
            return real_rename(source, target)

        with mock.patch.object(os, "rename", side_effect=first_move_waits_for_start):
            self.migrate(self.user)
        stdout, stderr = started[0].communicate(timeout=30)
        self.assertNotEqual(started[0].returncode, 0, stdout)
        self.assertIn("the data home moved after this command started; run codex-worker again", stderr)
        self.assertFalse((self.home / "claude-tasks").exists())
        self.assertFalse((self.user / ".teamster/tasks/late").exists())
        self.assertFalse((self.home / "worktrees/claude-late").exists())

    def test_migrate_home_end_to_end_keeps_tasks_working(self):
        self.legacy_user_home()
        disk = self.root / "disk/worktrees"
        disk.mkdir(parents=True)
        (self.home / "worktrees").symlink_to(disk)
        self.account(self.home, "A", self.reset, runs=[{"mode": "complete", "reply": self.work_result()}] * 3)
        self.start("keep")
        self.wait("keep")
        self.start("gone")
        self.wait("gone")
        self.cli("clean", "gone")
        old_tasks = self.home / "claude-tasks"
        before_report = self.report()
        self.assertIn('"keep"', json.dumps(before_report))
        plist = plistlib.loads(self.cli("duty", "plist").encode())["EnvironmentVariables"]
        self.assertEqual(plist["CODEX_TASK_HOME"], str(self.home))
        self.assertNotIn("TEAMSTER_HOME", plist)

        user_before, disk_before = tree(self.user), tree(disk)
        self.assertIn("nothing changed", self.cli("migrate-home"))
        self.assertEqual(tree(self.user), user_before)
        output = self.cli("migrate-home", "--apply")
        for label in ("codex-worker.board", "codex-worker.duty"):
            self.assertIn(f"launchctl kickstart -k gui/$(id -u)/{label}", output)
        data = self.user / ".teamster"
        self.assertFalse(old_tasks.exists())
        self.assertEqual(os.readlink(self.home / "worktrees"), str(disk))
        self.assertEqual(Path(os.path.realpath(data / "worktrees")), disk.resolve())
        self.assertEqual(tree(disk), disk_before)

        # Fresh processes resolve the new home; records keep their old worktree paths.
        self.assertEqual(self.report(), before_report)
        self.assertIn("keep", self.cli("status"))
        stored = [path for path in (data / "tasks").rglob("*") if path.is_file()
                  and str(old_tasks).encode() in path.read_bytes()]
        self.assertEqual(stored, [])
        state = json.loads((data / "tasks/keep/state.json").read_text())
        self.assertTrue(Path(state["worktree"]).is_dir())
        self.cli("say", "keep", "Continue the task.")
        self.cli("wait", "keep", "--timeout", "90")
        state = json.loads((data / "tasks/keep/state.json").read_text())
        self.assertEqual((state["outcome"], state["turns"]), ("completed", 2))
        self.assertFalse(state.get("error"))
        archived = [json.loads(path.read_text()) for path in (data / "tasks/.archive").glob("gone-*/state.json")]
        self.assertEqual([record["slug"] for record in archived], ["gone"])
        self.assertTrue((data / "log.jsonl").is_file())
        self.assertFalse((self.home / "codex-worker-log.jsonl").exists())
        plist = plistlib.loads(self.cli("duty", "plist").encode())["EnvironmentVariables"]
        self.assertEqual(plist["TEAMSTER_HOME"], str(data))
        self.assertNotIn("CODEX_TASK_HOME", plist)
