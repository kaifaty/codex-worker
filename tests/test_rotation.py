#!/usr/bin/env python3
"""Exercise account rotation through the public CLI, without a real Codex login.

Run with python3 -I tests/test_rotation.py. CODEX_TEST_JOBS controls the parallel
worker count; set it to 1 to use the standard sequential unittest runner.
CODEX_TEST_TIMEOUT limits each pooled test to 300 seconds by default.
"""
import runpy
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
import rotation_lifecycle
import rotation_reset_credits
import rotation_accounts
import rotation_telemetry
import test_runner
import test_stress_selection
from test_runner import RunnerTests
from test_stress_selection import StressSelectionTests


# Keep historical unittest selectors and IDs while tests live in topic modules.
class RotationTests(
        rotation_workspace.WorkspaceTests,
        rotation_weekly_budgets.WeeklyBudgetTests,
        rotation_time_budgets.TimeBudgetTests,
        rotation_results.ResultScoutConfigTests,
        rotation_checks.CheckGuardTests,
        rotation_notifications.NotificationTests,
        rotation_reviews.ReviewFixTests,
        rotation_lifecycle.LifecycleTests,
        rotation_reset_credits.ResetCreditTests,
        rotation_accounts.AccountAuthTests,
        rotation_telemetry.TelemetryReportTests):
    pass


# Source order is also the order used by the AST-based stress selection.
TEST_MODULES = (tuple(sys.modules[cls.__module__] for cls in RotationTests.__bases__)
                + (test_runner, test_stress_selection))


if __name__ == "__main__":
    runner = runpy.run_path(str(Path(__file__).with_name("rotation_runner.py")))
    runner["run"](sys.modules[__name__])
