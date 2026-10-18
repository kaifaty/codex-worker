# Task notifications

The user can configure task notifications with `notify` in `~/.codex/codex-worker.json`: `"macos"`, or
an argv list that receives the event JSON as its final argument. `notify_events` optionally filters
event types. Hooks run asynchronously and write their output or launch errors to the task's
`notify.log`; the repository README has examples and the full event list.

- `review_done` includes the review's `round` and `angles`.
- `turn_finished` fires once at the end of the whole cycle and includes `guarded_changed`, the number
  of changed guarded files, and the typed `outcome`, including `budget_exhausted` or `stuck`.
- Separate `budget_exhausted` and `stuck` events obey `notify_events` too.
