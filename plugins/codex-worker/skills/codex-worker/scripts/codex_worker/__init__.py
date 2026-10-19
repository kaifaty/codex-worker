"""Worker modules by import layer; existing modules are marked implemented.

Layer 0:
    clock (implemented): real and simulated clocks.
    procs (implemented): process execution, signals, and termination.
    result_schema (implemented): result schemas, parsing, and output lines.
Layer 1:
    config (implemented): paths, settings, binaries, and account configuration.
Layer 2:
    sandboxing (implemented): workspace filters and sandbox rules.
    notifications (implemented): notification processes and delivery.
    accounts (implemented): account homes, RPC, limits, and availability.
Layer 3:
    credit_resets (implemented): credit reset journal and actions.
    threads (implemented): thread messages, rollout files, and handoffs.
    budgets (implemented): weekly and elapsed-time budget monitoring.
Layer 4:
    task_state (implemented): task state fields, persistence, and locks.
Layer 5:
    telemetry_log (planned): journal rows and phase telemetry.
    publishing (planned): completed turns and work or pull request publishing.
Layer 6:
    checking (planned): guarded paths and check or base-check execution.
    account_rotation (planned): account errors, waits, and login refresh.
Layer 7:
    prompts (planned): prompt builders and review angles.
    turn_monitor (planned): turn control and monitoring.
Layer 8:
    turns (planned): work turns, wrap-up turns, and turn execution.
Layer 9:
    review_cycle (planned): review angles, review turns, and automatic fixes.
    ci_runs (planned): CI waits, failure excerpts, prompts, and fix cycles.
Layer 10:
    supervisor (planned): detached task supervision and spawning.
    wait_view (planned): wait output rendering and the wait command.
Layer 11:
    commands (planned): task commands, descriptions, and main repositories.
Layer 12:
    cli (planned): argument types, option registry, dispatch, and command table.

Import own modules as whole modules at module top, using ``from . import name``
in the package and ``from codex_worker import name`` in the entrypoint. Access
owned definitions through their module so patches reach every caller. Lower
layers never import higher layers, imports remain acyclic, and package modules
never import the entrypoint. Do not rebind module aliases or use dynamic
namespaces. Only config.py may use __file__; usage text stays in the entrypoint.
"""
