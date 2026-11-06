"""Installed package entrypoint for the codex-worker command and python -m codex_worker."""
import os
import sys


sys.dont_write_bytecode = True
while os.path.dirname(os.path.abspath(__file__)) in sys.path:
    sys.path.remove(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codex_worker import clock, procs, result_schema, executors, config, sandboxing, notifications, accounts, claude_accounts, duty, credit_resets, threads, budgets, task_state, claude_review, telemetry_log, publishing, home_migration, checking, account_rotation, prompts, turn_monitor, turns, review_cycle, ci_runs, supervisor, wait_view, commands, cli


if __name__ == "__main__":
    cli.main()
