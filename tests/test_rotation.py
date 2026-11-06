#!/usr/bin/env python3
"""Exercise account rotation through the public CLI, without a real Codex login.

Run with python3 -I tests/test_rotation.py. CODEX_TEST_JOBS controls the parallel
worker count; set it to 1 to use the standard sequential unittest runner.
CODEX_TEST_TIMEOUT limits each pooled test to 300 seconds by default.
"""
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


# Isolated Python omits the script directory from its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Every fixture uses temporary homes; an inherited data home would take precedence.
os.environ.pop("TEAMSTER_HOME", None)

import rotation_workspace
import rotation_data_home
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
import test_executors
import test_lead_role
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
from test_executors import ExecutorTests
from test_lead_role import LeadRoleTests
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
        rotation_telemetry.TelemetryReportTests,
        rotation_data_home.DataHomeTests):
    def version_cli(self, *args):
        result = subprocess.run([PYTHON, "-I", str(SCRIPT), *args], cwd=self.repo,
                                env=self.env, text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in result.stdout.splitlines():
            pid = line.rpartition(", pid ")[2]
            if pid.isdigit():
                self.extra_pids.add(int(pid))
        return result

    def test_old_task_version_has_no_warning(self):
        self.configure()
        self.account(self.home, "A", self.reset, runs=[
            {"mode": "complete", "reply": self.work_result()},
            {"mode": "complete", "reply": self.work_result()}])
        self.cli("start", "version-task", "--brief", str(self.brief), "--repo", str(self.repo))
        self.cli("wait", "version-task")
        self.assertEqual(self.state("version-task")["plugin_version"], self.worker().config.plugin_version())
        state_path = self.home / "claude-tasks/version-task/state.json"
        for version in ("1.9.0", None):
            state = self.state("version-task")
            if version is None:
                del state["plugin_version"]
            else:
                state["plugin_version"] = version
            state_path.write_text(json.dumps(state))
            for args in (("status", "version-task"), ("status",),
                         ("wait", "version-task"), ("stats", "version-task")):
                with self.subTest(version=version, command=args):
                    self.assertEqual(self.version_cli(*args).stderr, "")
        self.assertEqual(self.version_cli("say", "version-task", "Continue the task.").stderr, "")
        self.assertEqual(self.version_cli("wait", "version-task").stderr, "")
        self.assertNotIn("plugin_version", self.state("version-task"))
        self.assertEqual(self.version_cli("clean", "version-task").stderr, "")


# Source order is also the order used by the AST-based stress selection.
TEST_MODULES = (tuple(sys.modules[cls.__module__] for cls in RotationTests.__bases__)
                + (rotation_prompts, test_runner, test_stress_selection, test_process_cleanup,
                   test_rollout, test_options, test_state_fields, test_capacity_report,
                   test_clock, test_module_layout, test_worker_loader,
                   test_compare_module_split, test_compare_rotation_assertions,
                   test_compare_cli_help, test_executors, test_lead_role))


if __name__ == "__main__":
    runner = runpy.run_path(str(Path(__file__).with_name("rotation_runner.py")))
    runner["run"](sys.modules[__name__])
