# Telemetry and `report`

Use `report` to check whether fix rounds and review angles pay for their cost before changing
defaults. It prints persistent stage totals, then tasks ordered by weekly-window usage, then tokens.
In `Tasks by cost`, `slug` is followed by `repo` (the repository directory name used by `--repo`)
and `started` (task creation time in local time, `MM-DD HH:MM`, or `-` when unknown).
Task rows include the last review verdict, the [lead verdict](#lead-verdicts), the task weekly budget and the budget stop kind (`minutes` or `weekly`).
JSON task records retain the `created` timestamp.

## Compare models, reasoning effort, and task types

Set an optional label with `start --task-type LABEL`. It accepts 1–32 characters from `[a-z0-9-]`,
without a fixed list; examples are `feature`, `fix`, `refactor`, `mechanical`, `tests`, `bench`,
`logic`, `ml`, and `docs`. The label persists in state and is inherited by `say` and `review`.
`say --task-type LABEL` replaces it for subsequent journal rows; report task records use the latest label.

Every model phase and final `turn` row includes `model`, `reasoning_effort`, and `task_type`.
Execution metadata from Codex takes precedence over explicit CLI overrides and the invoked account's
`config.toml`, including its selected profile. `--effort` is an explicit reasoning override.
Native `codex exec review` inherits its parent's model and session context unless `review_model`
selects a different model; that override and the configured effort then supply the fallback values.
Ordinary review fallback uses its own session context.
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

`--by` accepts `task-type`, `effort`, `model`, `executor`, or `reviewer`, combined with commas. It uses the
same `--since`, `--repo`, and `--slug` filters as ordinary reports; `--json` includes the groups. Missing
values form the `unknown` group.
A task is counted once per observed combination of dimensions, using its latest task label. `model` and
`effort` pairs come from real invocations of the task's work phases only: the first turn, `say` turns, fix
rounds, CI repairs, and work wrap-ups; reviews and scouts do not place a task in a model group. A task
whose journal has no work phase falls back to its final work `turn` rows. A task that used several groups
contributes its full cost to each, so group counts and costs are not additive.

## Compare executors and reviewers

```sh
python3 -I "$T" report --by executor --since 30d
python3 -I "$T" report --by executor,reviewer,task-type
```

`executor` is the `provider` of the task's work phases: `claude` for the Claude account pool
(`start --executor claude`), otherwise `codex`, including older rows without `provider`. `reviewer` is the
provider of its review phases (`codex` or `claude`), or `none` when the task had no review.

The `--since` window decides which tasks a report includes and limits their costs, tokens, minutes and
weekly percentages to rows inside it. The executor, reviewer, work model and effort, first-review findings,
follow-ups and lead verdict come from all of a task's journal rows, so a task whose work happened before the window
keeps its executor and model.

The grouped table has these columns (JSON group keys in brackets):

| Column | Meaning |
|---|---|
| `tasks` [`tasks`] | Tasks in the group. |
| `done` [`done_fraction`] | Fraction whose final result is `done`. |
| `median rounds` [`median_rounds`] | Median review/fix rounds. |
| `median minutes` [`median_active_minutes`] | Median active minutes. |
| `median weekly %` [`median_weekly_used_percent`] | Median weekly percentage points of the account plan that ran each phase. |
| `accepted %` [`accepted_fraction`] | Fraction of tasks with the lead verdict `accepted` among tasks with a verdict. |
| `verdicts` [`verdicts`] | Tasks in the group with a lead verdict. |
| `findings` [`average_first_review_findings`] | Mean number of P1 and P2 findings in the task's first review round, merged across its completed angles: a defect found by several angles counts once, at its strongest severity. Tasks without a first review, or with journal rows older than the severity count, are left out. |
| `checks %` [`check_pass_fraction`] | Passed `--check` runs among all of the group's `--check` runs; base checks are not counted. |
| `ci %` [`ci_pass_fraction`] | Passed CI results among the group's recorded CI results, counted like `CI passed` in the summary. |
| `follow-ups` [`median_follow_ups`] | Median number of `say` turns the lead sent after the task's first `done` result; tasks that never reached `done` are left out. Fix rounds and CI repairs are not follow-ups. |

Compare `accepted %` only when each group has at least 5 lead verdicts.
`n/a` means the group has no samples. Weekly percentages stay percentages of each account's own plan: a
Claude percentage and a Codex percentage, or percentages of accounts with plans of different size, measure
different amounts of work and are not comparable. Compare executors by quality columns and minutes, and
compare weekly percentages only within one provider and plan size.

## Lead verdicts

```sh
python3 -I "$T" verdict SLUG accepted
python3 -I "$T" verdict SLUG revised --note "renamed the option"
```

Record a verdict when the work is merged or dropped:

- `accepted` — merged as is;
- `revised` — merged after the lead's edits;
- `redone` — the work had to be redone elsewhere;
- `dropped` — not merged.

`verdict` appends `{"kind": "verdict", "at", "slug", "repo", "created", "verdict", "note"}` to the
journal. It works for an active task and, after `clean`, for any slug still in the journal; it then uses
the slug's latest journal row. An unknown slug or verdict is an error. A later verdict replaces an earlier
one in reports. `clean` prints a one-line reminder when the task has no verdict; scouts are skipped.
Reports show it in the `lead verdict` column of `Tasks by cost`; JSON task records include
`lead_verdict`, `lead_note`, `executors`, `reviewers`, `first_review_findings`, and `follow_ups`.
A verdict belongs to the task with the same repository, slug and creation time. When no reported task
has that creation time, for example because older journal rows have none, it belongs to the newest task
with that slug started no later than the verdict; a task without a creation time starts at its first journal row. `clean` uses the same rule for its reminder; when the
journal cannot be read, it prints one line saying the verdict could not be checked and still exits 0.

## The journal

The supervisor appends numeric counters and identifiers to `<CODEX_TASK_HOME>/codex-worker-log.jsonl`
(by default `~/.codex/`), without prompts or messages; `clean` preserves this journal.
State and final turn journal rows retain `turn_stops` across turns (`turn`, stop details, `at`, and for `stuck` the last 300 output characters as `output_tail`); wrap-up stops add `phase: "wrapup"` with the same turn number and preserve the parent's `turn_stop`; `report --json` includes the list, and [empty searches](results.md#turn-outcomes) also allow leading `NAME=value` assignments or `env NAME=value` prefixes, but no `env` options.
`clean` also saves task and scout records in `~/.codex/claude-tasks/.archive/<slug>-YYYYmmddTHHMMSSZ` (UTC), except `events.jsonl` and `state.lock`.
Journal errors, including a busy lock, go to the task's `stderr.log` without blocking execution.
Creation time identifies each task, so reusing a slug after `clean` produces a separate report row.

## Reading the numbers

- Reports cover the last seven days by default; date filters start at UTC midnight.
- Account totals are grouped by provider and account name. JSON account rows include `provider`
  (`codex` or `claude`); historical rows without provider or reviewer metadata count as Codex.
  Text reports label Claude accounts as `claude/NAME`, keeping identically named subscriptions separate.
- Turns include work, fixes, wrap-ups, individual review angles, and scouts. Active time also includes
  checks and excludes quota sleeps and CI waits.
- Weekly usage and the task budget use the same accounting described below. Missing rollout
  measurements stay null in the journal and add no measured usage.
- The rounds-to-clean distribution counts each task's first clean review round: every completed angle
  in that round must be clean and none interrupted. Model refusals follow the supervisor's merged
  verdict. The final turn journal row and report record `auto_fix_stop`, including `escalate`;
  escalated turns do not count as rounds to clean.
- Final turn rows record the CI outcome. `report` prints `CI passed: N/M`.
- Review rows record `findings` and `severe_findings`, the number of P1 and P2 findings the angle adds to the round's merged review.

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
