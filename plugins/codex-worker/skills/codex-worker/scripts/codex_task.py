#!/usr/bin/env python3
"""Worker entrypoint; run codex_task.py --help for the synopsis in codex_worker/cli.py."""
import os, sys


sys.dont_write_bytecode = True
while os.path.dirname(os.path.abspath(__file__)) in sys.path:
    sys.path.remove(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_worker import clock, procs, result_schema, config, sandboxing, notifications, accounts, credit_resets, threads, budgets, task_state, telemetry_log, publishing, checking, account_rotation, prompts, turn_monitor, turns, review_cycle, ci_runs, supervisor, wait_view, commands, cli


if __name__ == "__main__":
    cli.main()
