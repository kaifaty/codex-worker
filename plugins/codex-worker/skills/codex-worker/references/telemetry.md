# Telemetry and `report`

Use `report` to check whether fix rounds and review angles pay for their cost before changing
defaults. It prints persistent stage totals, then tasks ordered by weekly-window usage, then tokens.
In `Tasks by cost`, `slug` is followed by `repo` (the repository directory name used by `--repo`)
and `started` (task creation time in local time, `MM-DD HH:MM`, or `-` when unknown).
Task rows include the task weekly budget and the budget stop kind (`minutes` or `weekly`).
JSON task records retain the `created` timestamp.

## Compare models, reasoning effort, and task types

Set an optional label with `start --task-type LABEL`. It accepts 1–32 characters from `[a-z0-9-]`,
without a fixed list; examples are `feature`, `fix`, `refactor`, `mechanical`, `tests`, `bench`,
`logic`, `ml`, and `docs`. The label persists in state and is inherited by `say` and `review`.
`say --task-type LABEL` replaces it for subsequent journal rows; report task records use the latest label.

Every model phase and final `turn` row includes `model`, `reasoning_effort`, and `task_type`.
Execution metadata from Codex takes precedence over explicit CLI overrides and the invoked account's
`config.toml`, including its selected profile. `--effort` is an explicit reasoning override.
Native `codex exec review` uses the configured `review_model` (or `model`) and effort: its outer
session context describes the parent, not the reviewer. Ordinary review fallback uses its own session context.
Claude reviews record the reviewer's known model and effort. Unknown values are null, including when
configuration or session metadata cannot be read; telemetry does not change model selection.
On Python 3.9–3.10, the configuration fallback reads simple string settings; configurations with
multiline strings remain unknown unless an explicit override or session context supplies the value.
Usage entries retain metadata for individual invocations when a phase rotates accounts.
No prompts or additional command output are stored in the journal.

`report --json` adds `task_type` and the ordered unique lists `models` and `reasoning_efforts`
to each task. Unknown model or effort values appear as null. Older rows without these fields remain readable.

```sh
python3 -I "$T" report --by effort,task-type
python3 -I "$T" report --by model --since 30d --repo example --json
```

`--by` accepts `task-type`, `effort`, or `model`, combined with commas. The grouped table contains task
counts, the fraction whose final result is `done`, median rounds, median active minutes, and median
weekly percentage points. It uses the same `--since`, `--repo`, and `--slug` filters as ordinary reports;
`--json` includes the groups. Missing values form the `unknown` group.
A task is counted once per observed combination of dimensions, using its latest task label; model and
effort pairs come from real invocations. A task that used several groups contributes its full cost to
each, so group counts and costs are not additive. Without `--by`, the text report is unchanged.

## The journal

The supervisor appends numeric counters and identifiers to `<CODEX_TASK_HOME>/codex-worker-log.jsonl`
(by default `~/.codex/`), without prompts or messages; `clean` preserves this journal.
State and final turn journal rows retain `turn_stops` across turns (`turn`, stop details, `at`, and for `stuck` the last 300 output characters as `output_tail`); wrap-up stops add `phase: "wrapup"` with the same turn number and preserve the parent's `turn_stop`; `report --json` includes the list, and [empty searches](results.md#turn-outcomes) also allow leading `NAME=value` assignments or `env NAME=value` prefixes, but no `env` options.
`clean` also saves task and scout records in `~/.codex/claude-tasks/.archive/<slug>-YYYYmmddTHHMMSSZ` (UTC), except `events.jsonl` and `state.lock`.
Journal errors, including a busy lock, go to the task's `stderr.log` without blocking execution.
Creation time identifies each task, so reusing a slug after `clean` produces a separate report row.

## Reading the numbers

- Reports cover the last seven days by default; date filters start at UTC midnight.
- Turns include work, fixes, wrap-ups, individual review angles, and scouts. Active time also includes
  checks and excludes quota sleeps and CI waits.
- Weekly usage and the task budget use the same accounting described below. Missing rollout
  measurements stay null in the journal and add no measured usage.
- The rounds-to-clean distribution counts each task's first clean review round: every completed angle
  in that round must be clean and none interrupted. Model refusals follow the supervisor's merged
  verdict. The final turn journal row and report record `auto_fix_stop`, including `escalate`;
  escalated turns do not count as rounds to clean.
- Final turn rows record the CI outcome. `report` prints `CI passed: N/M`.

## Weekly usage accounting

`record_weekly_usage` keeps a high-water mark for each account and weekly window. At the beginning of
every exec, it seeds the mark without charge from the account-selection snapshot, or the first valid
weekly sample when no snapshot is available. In the same window the seed takes the greater of the
saved mark and the fresh baseline, so gaps between execs are excluded.

Later samples charge only growth above the mark and raise it to the greater value. Repeated or lower
samples add zero. A reset timestamp advancing by more than an hour starts a new mark without charging
the reset sample; samples from a window more than an hour older are ignored. If a reset timestamp is
missing, the existing mark is retained and growth can still be charged; a window change requires
comparable reset timestamps.

The measurement counts growth of the account's weekly percentage during task phases. Shared account
activity affects estimates, and parallel tasks on one account count against each other. Cap scope and
exhaustion behavior are in [budgets.md](budgets.md).
