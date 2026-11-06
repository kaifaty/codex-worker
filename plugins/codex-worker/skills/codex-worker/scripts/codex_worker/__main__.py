"""Installed package entrypoint for the codex-worker command and python -m codex_worker."""
from codex_worker import clock, procs, result_schema, executors, config, sandboxing, notifications, accounts, claude_accounts, duty, credit_resets, threads, budgets, task_state, claude_review, telemetry_log, publishing, checking, account_rotation, prompts, turn_monitor, turns, review_cycle, ci_runs, supervisor, wait_view, commands, cli


if __name__ == "__main__":
    cli.main()
