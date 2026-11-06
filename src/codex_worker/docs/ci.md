# CI waiting and repair

Enable and change the task's CI options as described in [settings.md](settings.md#task-ci-options).

CI starts after work, review and automatic fixes, only for a completed turn with a passing, `unavailable`
(exit 75) or absent local check and a published GitHub PR whose head SHA equals local HEAD. Otherwise state records CI as
`skipped` with a reason. The supervisor polls every 30 seconds, allows three minutes for checks to
appear (`none` if none appear), and waits for pending checks up to `ci_timeout_minutes` (`timeout`).
Waiting spends no task weekly budget and does not count toward `--max-minutes`; `stop` or an external
signal interrupts it. A changed PR head also skips the wait.

`--ci-command CMD` enables the same cycle on other hosts, replacing GitHub checks with `sh -c CMD`
in the worktree. The command must wait for CI and exit 0 for success or nonzero for failure; it gets
`CODEX_CI_SHA` (local HEAD) and `CODEX_CI_BRANCH`. Exit code 75 (EX_TEMPFAIL) instead records `skipped`
with reason `CI unavailable (exit 75)`, preserves the output excerpt, and does not start a repair round.
The supervisor verifies the actual `origin` branch
before and after the command: an unpushed or mismatched head is `skipped`, as is a head changed while
waiting. The same `ci_timeout_minutes`, budget exclusions, `stop` handling and repair rounds apply.
The result contains one check named after the command, with output selected by the excerpt rules below.

GitHub checks also skip repair when all failed or cancelled checks are unrelated to the branch.
An Actions job did not start if its annotation begins with `The job was not started because` or its
steps list is empty. If every failed check is such a job, CI records `skipped` with reason
`CI unavailable: jobs not started: <names>`. Otherwise, a check is red on the base when same-name
runs exist and all completed with `failure` on the PR's base commit (`baseRefOid`, the base its CI ran against).
A same-name success, neutral, skipped or other non-failure result prevents this classification.
If every failure is either red on the base or a job that did not start, CI records `skipped` with a reason beginning
`base red: <names> also fail on <base> <short sha>`, also naming jobs that did not start.
`wait` keeps the failed-check links and log excerpts for comparison with the base.

Request errors or timeouts, checks without an Actions job, and missing or unfinished checks on the
base retain the normal branch-failure behavior. If any branch failure remains, repair proceeds;
the prompt names checks red on the base and jobs that did not start separately and says they need
no fix in this branch.

Passing or skipped checks give `passed`; other failed or cancelled checks give `failed`. A remaining CI
round sends failed-check names and selected log lines to the same implementation thread. GitHub
Actions excerpts select failure blocks around unittest `FAIL:` / `ERROR:`, Python tracebacks and
`AssertionError`, `Error:`, pytest `E   ` / `FAILED`, and Actions `##[error]` annotations. They include
nearby separator/source lines and following context up to a blank line or the next block, plus test
summaries such as `Ran N tests`, `FAILED (...)`, and `N failed`, in original log order. GitHub's job,
step and timestamp prefixes do not affect matching; selected lines retain those prefixes.
Only the last summary of each kind is selected, so intermediate successful runs cannot crowd out
errors. Python exception-group tracebacks are recognized with their tree prefixes and nested frames.
Consecutive Python tracebacks form separate blocks even without blank lines between them; a unittest
failure header stays with its first traceback.

The same excerpt appears in `wait` and the CI repair prompt, stored under the existing `tail` state
key. Excerpts are capped at 150 lines per failed check and 400 total, including any omission notice.
If failure blocks exceed the available space, the first complete blocks that fit are kept with
`… N more failure lines omitted`. Logs with no recognized failure patterns use the original tail
behavior; external checks get a link. If no complete failure block fits, its beginning is kept within
the remaining space alongside the final summaries and omission notice.

Codex must fix and push failures caused by the branch, or change nothing and return `blocked` with
evidence for flaky or unrelated failures. Each CI turn runs the saved local check, publishes again,
then waits again; it does not trigger an automatic review. An incomplete result stops without another
CI wait. Weekly exhaustion prevents another repair turn.

See [results.md](results.md#ci-result) for output fields and examples, and
[lead-guide.md](lead-guide.md#the-loop) for the lead's merge rules.
