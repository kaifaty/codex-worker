#!/usr/bin/env python3
"""Install the package into a temporary venv and run a task through its codex-worker command.

Run with python3 -I tests/package_smoke.py. Building the wheel fetches setuptools, so this
needs network access and is not part of test_rotation.py. Everything it writes stays in
a temporary directory: the venv, HOME, CODEX_TASK_HOME, the repository and the fake Codex.
"""
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_cleanup import cleanup_processes
from rotation_support import FAKE_CODEX


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins/codex-worker/skills/codex-worker/scripts/codex_worker"
VERSION = re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                    re.MULTILINE)[1]
HOOKS_CHECK = r"""import importlib.metadata, json
entry, = [entry for entry in importlib.metadata.distribution("codex-worker").entry_points
          if entry.group == "console_scripts" and entry.name == "codex-worker"]
entry.load()
from codex_worker import executors
required = ("turn", "wrapup", "last_message", "messages", "pr_messages")
print(json.dumps({"value": entry.value, "missing": {
    name: [operation for operation in required
           if operation not in executors.get(name).hooks
           or not callable(getattr(*executors.get(name).hooks[operation]))]
    for name in ("codex", "claude")}}))
"""


class PackageSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-package-smoke-")
        self.root = Path(self.tmp.name).resolve()
        self.pids = set()

    def tearDown(self):
        live = cleanup_processes(self.root, self.pids)
        self.tmp.cleanup()
        self.assertFalse(live, "task processes still running: %s" % sorted(live))

    def run_checked(self, args, **kwargs):
        result = subprocess.run(args, text=True, capture_output=True, timeout=kwargs.pop("timeout", 120), **kwargs)
        self.assertEqual(result.returncode, 0, "command: %s\nstdout:\n%s\nstderr:\n%s"
                         % (args, result.stdout, result.stderr))
        return result

    def install(self):
        # Build from a copy so setuptools' build/ and egg-info never land in the checkout.
        source = self.root / "source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
            ".git", "node_modules", "build", "dist", "*.egg-info", "__pycache__", ".venv"))
        venv = self.root / "venv"
        self.run_checked([sys.executable, "-m", "venv", str(venv)])
        pip_home = self.root / "pip-home"
        pip_home.mkdir()
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(pip_home),
               "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
        self.run_checked([str(venv / "bin/python"), "-I", "-m", "pip", "install", "--quiet", str(source)],
                         env=env, cwd=str(self.root), timeout=600)
        return venv

    def test_installed_command_runs_a_task(self):
        venv = self.install()
        command = venv / "bin/codex-worker"
        python = venv / "bin/python"
        self.assertTrue(command.is_file())

        home = self.root / "home"
        codex_home = self.root / "codex-home"
        outside = self.root / "outside"
        fake_bin = self.root / "fake-bin"
        for directory in (home, codex_home, outside, fake_bin):
            directory.mkdir()
        trace = self.root / "fake-trace.jsonl"
        fake = fake_bin / "codex"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_CODEX)
        fake.chmod(0o700)
        git_dir = str(Path(shutil.which("git")).parent)
        env = {"PATH": os.pathsep.join([str(fake_bin), git_dir, "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
               "HOME": str(home), "CODEX_TASK_HOME": str(codex_home), "CODEX_TASK_CODEX": str(fake),
               "CODEX_FAKE_TRACE": str(trace), "LC_ALL": "C",
               "CODEX_TASK_WAIT_POLL_SECONDS": "0.05", "CODEX_TASK_RATE_POLL_SECONDS": "0.05",
               "CODEX_TASK_ROLLOUT_SEARCH_SECONDS": "0.05",
               "GIT_AUTHOR_NAME": "Package Smoke", "GIT_AUTHOR_EMAIL": "smoke@example.test",
               "GIT_COMMITTER_NAME": "Package Smoke", "GIT_COMMITTER_EMAIL": "smoke@example.test",
               "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}

        def git(*args, cwd):
            self.run_checked(["git"] + list(args), cwd=str(cwd), env=env)

        repo = self.root / "repo"
        repo.mkdir()
        git("init", "--quiet", "--initial-branch=main", cwd=repo)
        (repo / "tracked.txt").write_text("initial\n")
        git("add", "tracked.txt", cwd=repo)
        git("commit", "--quiet", "-m", "initial", cwd=repo)
        origin = self.root / "origin.git"
        git("init", "--quiet", "--bare", "--initial-branch=main", str(origin), cwd=self.root)
        git("remote", "add", "origin", str(origin), cwd=repo)
        git("push", "--quiet", "-u", "origin", "main", cwd=repo)
        git("remote", "set-head", "origin", "main", cwd=repo)
        brief = self.root / "brief.md"
        brief.write_text("Create smoke.txt.\nDone when:\n- smoke.txt exists.\n")
        result = {"status": "done", "summary": "Created smoke.txt.",
                  "checks": [{"command": "test -f smoke.txt", "outcome": "passed", "details": "1 file"}],
                  "pr": "", "pr_title": "", "pr_body": "", "questions": [], "remaining": [],
                  "findings": [], "commit_message": ""}
        (codex_home / "fixture.json").write_text(json.dumps({
            "account": {"type": "chatgpt", "email": "smoke@example.test", "planType": "pro"},
            "accountId": "smoke", "ordinaryUsageAllowed": True,
            "primary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 4102444800},
            "secondary": None, "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "runs": [{"mode": "complete", "reply": result, "write_files": {"smoke.txt": "smoke\n"},
                      "commit_files": ["smoke.txt"]}]}))

        def cli(*args):
            # The installed command runs outside the repository, without PYTHONPATH.
            output = self.run_checked([str(command)] + list(args), cwd=str(outside), env=env)
            for line in output.stdout.splitlines():
                pid = line.rpartition(", pid ")[2]
                if pid.isdigit():
                    self.pids.add(int(pid))
            return output

        help_text = cli("--help").stdout
        self.assertIn("start <slug>", help_text)

        installed = [path for path in venv.rglob("codex_worker") if (path / "__main__.py").is_file()]
        self.assertEqual(len(installed), 1, installed)
        installed = installed[0]
        for language in ("en", "ru"):
            with self.subTest(language=language):
                (codex_home / "codex-worker.json").write_text(json.dumps({"language": language}))
                slug = "smoke-" + language
                started = cli("start", slug, "--brief", str(brief), "--repo", str(repo),
                              "--no-push", "--check", "test -f smoke.txt")
                self.assertIn("  wait: codex-worker wait %s   " % slug, started.stdout)
                self.assertEqual(started.stderr, "")
                state_path = codex_home / "claude-tasks" / slug / "state.json"
                self.assertEqual(json.loads(state_path.read_text())["plugin_version"], VERSION)
                supervisor = int(re.search(r", pid ([0-9]+)", started.stdout)[1])
                self.assertNotEqual(supervisor, os.getpid())

                waited = cli("wait", slug, "--timeout", "120").stdout
                state = json.loads(state_path.read_text())
                self.assertEqual(state["outcome"], "completed", waited)
                self.assertIsNone(state.get("pid"), waited)
                self.assertEqual(state["result"]["status"], "done", waited)
                self.assertEqual(state["plugin_version"], VERSION)
                self.assertIn("smoke.txt", subprocess.run(
                    ["git", "show", "--name-only", "--format=", state["branch"]], cwd=str(repo), env=env,
                    text=True, capture_output=True, check=True).stdout)
                events = [json.loads(line) for line in trace.read_text().splitlines()]
                execs = [event for event in events if event.get("command") == "exec"]
                # The detached supervisor started from the installed __main__.py ran the turn.
                self.assertEqual(execs[-1]["ppid"], supervisor)
                rules_name = "rules.ru.md" if language == "ru" else "rules.md"
                rules = (installed / rules_name).read_text(encoding="utf-8")
                self.assertEqual(rules, (PACKAGE / rules_name).read_text(encoding="utf-8"))
                self.assertIn(rules.splitlines()[0], execs[-1]["prompt"])

                self.assertIn(slug, cli("status", slug).stdout)
                # stats runs the moved codex_stats.py on the task thread's rollout.
                stats = cli("stats", slug).stdout
                self.assertIn("== main: " + state["thread"], stats)
                self.assertIn("total: ", stats)
                report = json.loads(cli("report", "--slug", slug, "--json").stdout)["summary"]
                self.assertEqual((report["tasks"], report["checks"]["passed"]), (1, 1), report)
                self.assertIn("verdict accepted recorded for " + slug, cli("verdict", slug, "accepted").stdout)
                cli("clean", slug)
                self.assertFalse(Path(state["worktree"]).exists())

        hooks = json.loads(self.run_checked([str(python), "-I", "-c", HOOKS_CHECK],
                                            cwd=str(outside), env=env).stdout)
        self.assertEqual(hooks, {"value": "codex_worker.__main__:cli.main",
                                 "missing": {"codex": [], "claude": []}})

        plist_text = cli("duty", "plist").stdout
        self.assertNotIn("plugins", plist_text)
        program = plistlib.loads(plist_text.encode("utf-8"))["ProgramArguments"]
        self.assertEqual(program[1:2] + program[3:], ["-I", "duty", "tick"])
        self.assertEqual(Path(program[2]), (installed / "__main__.py").resolve())
        for path in (program[0], program[2]):
            # The venv interpreter is a link to the base Python; its directory is the venv's.
            self.assertIn(venv, Path(path).parent.resolve().parents, path)


if __name__ == "__main__":
    unittest.main()
