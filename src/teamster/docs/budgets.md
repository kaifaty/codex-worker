# Time and weekly budgets

`--max-minutes M` counts active exec time across account rotations, excluding reset sleeps, account
waits and CI waits. Reviews and scouts use the separate `max_review_minutes` cap. Defaults are in
[settings.md](settings.md).

`--max-weekly P` caps the task in percentage points of the weekly limit, with 0 < P ≤ 100. Usage
accumulates across follow-ups, work, reviews, angles, fix rounds, wrap-ups, and accounts; see
[telemetry.md](telemetry.md#weekly-usage-accounting) for the accounting algorithm. An exhausted task
rejects `say` and `review` until `--max-weekly P` raises its budget above measured spend. `status` and
`wait` warn when the remaining budget exceeds account headroom up to `switch_at`.

Time limits, weekly exhaustion and repeated command failures produce the stops and bounded wrap-ups
described in [results.md](results.md#turn-outcomes). Automatic-phase budget checks are covered in
[review-cycle.md](review-cycle.md).
