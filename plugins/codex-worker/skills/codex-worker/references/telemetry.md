# Telemetry and `report`

Use `report` to check whether fix rounds and review angles pay for their cost before changing
defaults. It prints persistent stage totals, then tasks ordered by weekly-window usage, then tokens.
In `Tasks by cost`, `slug` is followed by `repo` (the repository directory name used by `--repo`)
and `started` (task creation time in local time, `MM-DD HH:MM`, or `-` when unknown).
Task rows include the task weekly budget and the budget stop kind (`minutes` or `weekly`).
JSON task records retain the `created` timestamp.

## The journal

The supervisor appends numeric counters and identifiers to `<CODEX_TASK_HOME>/codex-worker-log.jsonl`
(by default `~/.codex/`), without prompts, messages, or command output; `clean` preserves this journal.
Journal errors, including a busy lock, go to the task's `stderr.log` without blocking execution.
Creation time identifies each task, so reusing a slug after `clean` produces a separate report row.

## Reading the numbers

- Reports cover the last seven days by default; date filters start at UTC midnight.
- Turns include work, fixes, wrap-ups, individual review angles, and scouts. Active time also includes
  checks and excludes quota sleeps and CI waits.
- Weekly usage sums positive within-stage account deltas, omitting reset crossings; shared account
  activity can affect these estimates. Missing rollout measurements stay null in the journal and add no
  measured usage. The task weekly budget uses the same cumulative sum across phases and accounts,
  including follow-ups and wrap-ups.
- The rounds-to-clean distribution counts each task's first clean review round: every completed angle
  in that round must be clean and none interrupted. Model refusals follow the supervisor's merged
  verdict. The final turn journal row and report record `auto_fix_stop`, including `escalate`;
  escalated turns do not count as rounds to clean.
- Final turn rows record the CI outcome. `report` prints `CI passed: N/M`; CI waiting itself uses no
  task weekly budget or implementation time budget.
