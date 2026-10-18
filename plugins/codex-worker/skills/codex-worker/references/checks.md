# Checks, guarded files and base tests

## When the saved check runs

`--check-timeout SEC` defaults to 1800; check output goes to the task's `check.log`.
The saved check runs in the worktree after a completed implementation turn, before review. A failure
or timeout skips the review, and `wait` shows the last 40 lines of `check.log`.

- A `partial`, `blocked` or `needs_decision` result skips automatic review with
  `review_skipped: "result <status>"`; the saved check still runs.
- Failed, stopped and paused turns run no check.
- `budget_exhausted` and `stuck` still run the saved check as objective evidence, but skip automatic
  review and fix rounds.
- Inside an automatic fix cycle, other incomplete results stop the cycle at once, before checks.

## Guarded files

The supervisor guards:

- test directories: `tests/`, `test/`, `__tests__/`, `spec/`;
- test filenames: `test_*`, `*_test.*`, `*.test.*`, `*.spec.*`;
- CI paths: `.github/workflows/`, `.gitlab-ci.yml`, `.forgejo/workflows/`;
- existing relative repository paths among the check's top-level tokens, including `NAME=path` and
  `--opt=path`;
- patterns passed with repeatable `start --guard "GLOB"`.

Discovered paths and patterns persist in task state, even after the files are deleted. Commands nested
in `sh -c '…'` are not inspected; add their paths with `--guard`.

After a work turn, `wait` prints `guarded files changed` with each changed file's added and deleted
line counts across the base-to-HEAD diff and uncommitted changes. It flags deleted guarded files and
added `skip`/`xfail`/`only` markers separately. Review prompts name these files and ask the reviewer to
look for removed assertions, skips, wider tolerances, and mocks replacing the code under test.

Skip markers are matched separately in three diff layers: base to HEAD, HEAD to index, and index to
working tree. Untracked files belong to the working-tree layer. Within each layer, an added marker
is treated as moved when its full line after `strip()` matches a deleted line from any guarded file,
including a deleted file. Matching uses a multiset: each deleted line cancels at most one identical
added marker. Lines never cancel across layers, so a skip added to the index and then removed in the
working tree still appears in `guarded tests disabled`. Detection remains text-based, including
marker strings in test fixtures.

Moved markers are excluded from `guarded tests disabled`. When their total is positive, `wait`
prints one `== guarded skip markers moved: N` line after the disabled warning (or in its place when
there are no new disabled markers), without listing the moved lines.

## Base tests (`--base-check`)

After a passing ordinary check, the supervisor creates a temporary detached worktree at the task's
`HEAD`, restores guarded files from the base (removing new ones and restoring deleted ones), runs the
same command, then removes the worktree. The check record keeps the base run's outcome, exit code and
duration; `wait` prints `== base tests: passed/FAILED` with a log tail. A failure is a signal to
examine: a legitimate behavior change can explain it, and review still runs.

The option persists across turns. Use relative paths in the check: a command containing the task
worktree's absolute path gives `== base tests: skipped (…)` and is not rerun.
