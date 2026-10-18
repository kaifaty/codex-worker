# Telemetry and `report`

Use `report` to check whether fix rounds and review angles pay for their cost before changing
defaults. It prints persistent stage totals, then tasks ordered by weekly-window usage, then tokens.

## The journal

The supervisor appends numeric counters and identifiers to `<CODEX_TASK_HOME>/codex-worker-log.jsonl`
(by default `~/.codex/`), without prompts, messages, or command output; `clean` preserves this journal.
Journal errors, including a busy lock, go to the task's `stderr.log` without blocking execution.
Creation time identifies each task, so reusing a slug after `clean` produces a separate report row.

## Reading the numbers

- Reports cover the last seven days by default; date filters start at UTC midnight.
- Turns include work, fixes, wrap-ups, individual review angles, and scouts. Active time also includes
  checks and excludes quota sleeps.
- Weekly usage sums positive within-stage account deltas, omitting reset crossings; shared account
  activity can affect these estimates. Missing rollout measurements stay null in the journal and add no
  measured usage.
- The rounds-to-clean distribution counts each task's first clean review round: every completed angle
  in that round must be clean and none interrupted. Model refusals follow the supervisor's merged
  verdict.
