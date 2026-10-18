# GitHub CI waiting and repair

Enable and change the task's CI options as described in [settings.md](settings.md#task-ci-options).

CI starts after work, review and automatic fixes, only for a completed turn with a passing or absent
local check and a published GitHub PR whose head SHA equals local HEAD. Otherwise state records CI as
`skipped` with a reason. The supervisor polls every 30 seconds, allows three minutes for checks to
appear (`none` if none appear), and waits for pending checks up to `ci_timeout_minutes` (`timeout`).
Waiting spends no task weekly budget and does not count toward `--max-minutes`; `stop` or an external
signal interrupts it. A changed PR head also skips the wait.

Passing or skipped checks give `passed`; failed or cancelled checks give `failed`. A remaining CI
round sends failed-check names and log tails to the same implementation thread. GitHub Actions tails
are capped at 150 lines per failed check and 400 total; external checks get a link.

Codex must fix and push failures caused by the branch, or change nothing and return `blocked` with
evidence for flaky or unrelated failures. Each CI turn runs the saved local check, publishes again,
then waits again; it does not trigger an automatic review. An incomplete result stops without another
CI wait. Weekly exhaustion prevents another repair turn.

See [results.md](results.md#ci-result) for output fields and examples, and
[SKILL.md](../SKILL.md#the-loop) for Claude's merge rules.
