#!/usr/bin/env python3
"""Exercise account rotation through the public CLI, without a real Codex login.

Run with python3 -I tests/test_rotation.py. CODEX_TEST_JOBS controls the parallel
worker count; set it to 1 to use the standard sequential unittest runner.
CODEX_TEST_TIMEOUT limits each pooled test to 300 seconds by default.
"""
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rotation_workspace
import rotation_weekly_budgets
import rotation_time_budgets
import rotation_results
import rotation_checks
import rotation_notifications
import rotation_reviews
import test_claude_review
import rotation_lifecycle
import rotation_reset_credits
import rotation_accounts
import rotation_capacity
import rotation_telemetry
import rotation_prompts
import test_runner
import test_stress_selection
import test_process_cleanup
import test_rollout
import test_options
import test_state_fields
import test_capacity_report
import test_clock
import test_module_layout
import test_worker_loader
import test_compare_module_split
import test_compare_rotation_assertions
import test_compare_cli_help
from test_runner import RunnerTests
from test_stress_selection import StressSelectionTests
from test_process_cleanup import ProcessCleanupTests
from test_rollout import RolloutTests
from test_options import OptionRegistryTests
from test_state_fields import StateFieldRegistryTests
from test_capacity_report import CapacityReportTests
from test_clock import ClockTests
from test_module_layout import ModuleLayoutTests
from test_worker_loader import WorkerLoaderTests
from test_compare_module_split import CompareModuleSplitTests
from test_compare_rotation_assertions import CompareRotationAssertionsTests
from test_compare_cli_help import CompareCliHelpTests
from rotation_prompts import PromptTests
from rotation_support import PYTHON, SCRIPT


# Keep historical unittest selectors and IDs while tests live in topic modules.
class RotationTests(
        rotation_workspace.WorkspaceTests,
        rotation_weekly_budgets.WeeklyBudgetTests,
        rotation_time_budgets.TimeBudgetTests,
        rotation_results.ResultScoutConfigTests,
        rotation_checks.CheckGuardTests,
        rotation_notifications.NotificationTests,
        rotation_reviews.ReviewFixTests,
        test_claude_review.ClaudeReviewTests,
        rotation_lifecycle.LifecycleTests,
        rotation_reset_credits.ResetCreditTests,
        rotation_accounts.AccountAuthTests,
        rotation_capacity.CapacityRetryTests,
        rotation_telemetry.TelemetryReportTests):
    def installed_plugin(self, directory, version):
        plugin = SCRIPT.parents[3]
        shutil.copytree(plugin, directory)
        manifest = directory / ".claude-plugin/plugin.json"
        metadata = json.loads(manifest.read_text())
        metadata["version"] = version
        manifest.write_text(json.dumps(metadata))
        return directory / SCRIPT.relative_to(plugin)

    def plugin_cli(self, script, *args):
        result = subprocess.run([PYTHON, "-I", str(script), *args], cwd=self.repo,
                                env=self.env, text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in result.stdout.splitlines():
            pid = line.rpartition(", pid ")[2]
            if pid.isdigit():
                self.extra_pids.add(int(pid))
        return result

    def test_plugin_version_warning_cache_layout(self):
        for label, current, dirname, siblings, newest in (
                ("newer", "1.20.0", "1.20.0", ("1.21.0", "1.25.0"), "1.25.0"),
                ("same", "1.25.0", "1.25.0", ("1.20.0", "1.25.0"), None),
                ("alone", "1.20.0", "1.20.0", (), None),
                ("nonnumeric", "1.20.0", "1.20.0", ("latest", "v2.0.0", "2.0"), None),
                ("numeric", "1.9.0", "1.9.0", ("1.10.0",), "1.10.0"),
                ("clone", "1.20.0", "checkout", ("1.25.0",), None)):
            with self.subTest(layout=label):
                cache = self.root / label
                script = self.installed_plugin(cache / dirname, current)
                relative_script = script.relative_to(cache / dirname)
                for sibling in siblings:
                    candidate = cache / sibling / relative_script
                    if not candidate.exists():
                        candidate.parent.mkdir(parents=True)
                        candidate.touch()
                if label == "nonnumeric":
                    # An incomplete installation must never be recommended.
                    (cache / "99.0.0").mkdir()
                result = self.plugin_cli(script, "status")
                expected = (f"== codex-worker {current} is running, {newest} is installed: "
                            f"T={cache / newest / relative_script}\n") if newest else ""
                self.assertEqual(result.stderr, expected)
                self.assertEqual(result.stdout, "")

    def test_plugin_version_warnings_follow_commands_and_task_state(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.review_result(defects=False)},
            {"mode": "complete", "reply": self.work_result()}])
        cache = self.root / "plugin-cache"
        old = self.installed_plugin(cache / "1.9.0", "1.9.0")
        new = self.installed_plugin(cache / "1.10.0", "1.10.0")
        warning = f"== codex-worker 1.9.0 is running, 1.10.0 is installed: T={new}\n"
        for args in (
                ("start", "version-task", "--brief", str(self.brief), "--repo", str(self.repo)),
                ("wait", "version-task"),
                ("say", "version-task", "Continue the task."),
                ("wait", "version-task"),
                ("review", "version-task"),
                ("wait", "version-task"),
                ("status", "version-task"),
                ("status",),
                ("scout", "version-scout", "Describe the code.", "--repo", str(self.repo)),
                ("wait", "version-scout")):
            with self.subTest(command=args):
                result = self.plugin_cli(old, *args)
                self.assertEqual(result.stderr, warning)
                self.assertTrue(result.stdout)
                if args[0] == "wait":
                    self.assertFalse(self.state(args[1]).get("error"), result.stdout)
                    self.assertNotEqual(self.state(args[1])["outcome"], "failed", result.stdout)
        self.assertEqual(self.state("version-task")["plugin_version"], "1.9.0")

        self.assertEqual(self.plugin_cli(new, "say", "version-task", "Continue with the new version.").stderr, "")
        task_warning = f"== codex-worker task started with 1.9.0, 1.10.0 is running: T={new}\n"
        for args in (("wait", "version-task"), ("status", "version-task"), ("status",)):
            with self.subTest(new_command=args):
                self.assertEqual(self.plugin_cli(new, *args).stderr, task_warning)
        self.assertEqual(self.state("version-task")["plugin_version"], "1.9.0")

        # Both an old task and an old command still produce just one warning.
        latest = self.installed_plugin(cache / "1.11.0", "1.11.0")
        for command in ("wait", "status"):
            self.assertEqual(self.plugin_cli(new, command, "version-task").stderr,
                             f"== codex-worker 1.10.0 is running, 1.11.0 is installed: T={latest}\n")

        state = self.state("version-task")
        del state["plugin_version"]
        (self.home / "claude-tasks/version-task/state.json").write_text(json.dumps(state))
        for command in ("wait", "status"):
            self.assertEqual(self.plugin_cli(latest, command, "version-task").stderr, "")


# Source order is also the order used by the AST-based stress selection.
TEST_MODULES = (tuple(sys.modules[cls.__module__] for cls in RotationTests.__bases__)
                + (rotation_prompts, test_runner, test_stress_selection, test_process_cleanup,
                   test_rollout, test_options, test_state_fields, test_capacity_report,
                   test_clock, test_module_layout, test_worker_loader,
                   test_compare_module_split, test_compare_rotation_assertions,
                   test_compare_cli_help))


if __name__ == "__main__":
    runner = runpy.run_path(str(Path(__file__).with_name("rotation_runner.py")))
    runner["run"](sys.modules[__name__])
