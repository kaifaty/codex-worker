# How reviews, angles and fix rounds run

Claude's default cycle and response to stops are in [SKILL.md](../SKILL.md). This file covers what
the supervisor does in between. Before each automatic review, angle, or fix round, it checks the
remaining task weekly budget. Exhaustion skips that phase and stops fixes with `auto_fix_stop: "budget"`.

## Automatic review

`--review` on `start` or `say` runs a review in a fresh thread after a `done` result and a passing
check, or after an unstructured result, for compatibility. Every review prompt names the command and
result of the passing saved check and tells the reviewer not to rerun the full suite; only focused
checks that confirm a specific finding run again.

Before the first automatic review and every repeat review after a fix, the supervisor fetches the
task's base and runs `git merge-tree --write-tree HEAD <base>`. A clean merge proceeds to review
without changing the task branch. If fetching or merge-tree fails (for example, without network
access or with an older Git), the supervisor records the reason, skips this check and proceeds to
review. Standalone `review`, the CI cycle and PR publication do not run this check.

When the base conflicts and a `--fix-rounds` round remains, the supervisor uses that round to merge
the base instead of starting review. The implementation prompt lists the conflicting files and asks
for an ordinary merge commit, without rebase or force-push, preserving the intent of both sides and
running the saved check in full. The merge is recorded in `rounds` and counts toward the same fix
limit. The saved check and review then follow as after any other fix round, including another base
merge check before review.

## Review provider

`start`, `say`, and `review` accept `--reviewer codex|claude`. The default is the `reviewer` setting
(`codex`); the task saves the selection, and follow-ups and standalone reviews inherit it unless
overridden. `--reviewer` selects the provider; `--review` still enables automatic review.

With `--reviewer claude`, each review uses a fresh Claude Code CLI process with the same review prompt
and result schema. Work and fix rounds remain in Codex. The reviewer uses its isolated Anthropic
subscription; its usage does not add to the task's Codex weekly percentage. `wait` prints
`== review by claude (N min)` or `== review angle X by claude (N min)`.

The supervisor removes inherited `CLAUDE*` and `ANTHROPIC*` environment variables, reads the reviewer
token from macOS Keychain immediately before launch, and supplies the isolated config directory and
token through the environment. Claude runs with `--permission-mode dontAsk`, allows
`Read,Grep,Glob,Bash`, and disallows `Edit,Write,NotebookEdit`. The supervisor disables Git filters
and compares `HEAD`, `git status --porcelain`, and raw contents of tracked and nonignored untracked
files before and after review: any change fails with `reviewer modified the worktree`, preserving
the worktree for inspection.

A rejected rate limit, 401/403 authentication failure, missing executable during the cycle, error
result, or invalid structured output falls back to an ordinary Codex review. `wait` reports
`== reviewer claude unavailable: <reason>; review by codex`. Review timeouts, `stop`, and external
signals keep their usual outcomes and do not fall back. Cancellation sends SIGINT to Claude's
process group, waits up to 10 seconds, then sends SIGTERM if needed.

Account setup is in [accounts.md](accounts.md#claude-reviewer-account); optional model, effort and turn
limits are in [settings.md](settings.md#claude-reviewer).

## Angles

`--review-angles auto` runs `correctness,concurrency,tests` when the diff from base, including
uncommitted changes, reaches `review_angles_min_lines` changed lines; below that it runs one ordinary
review. For a specific risk, pass a comma-separated list instead:

- `correctness`: behavior and the brief's "Done when" conditions;
- `concurrency`: races, processes, signals, shared files and state, retries, and recovery;
- `security`: secrets, shell and path injection, untrusted input, permissions, and sandbox boundaries;
- `tests`: weakened or ineffective tests, uncovered brief conditions, and mocks replacing tested code.

On `start` and `say`, `--review-angles` implies `--review`.

Angles run one after another in fresh Codex threads, with ordinary account rotation and reset waiting,
or in separate Claude CLI processes with `--reviewer claude`. Each angle gets its own review record.
The supervisor combines the findings, merges duplicate locations and
titles, and labels each finding with its angles. A refused angle is marked `refused` without stopping
the others, and the verdict uses the other results.

## Fix rounds

`--fix-rounds N` (0–5, default 0) needs `--review`.

Each round sends the review's findings straight to the implementation thread, with the same account
rotation and reset waiting as `say`. Codex must verify each finding against the code, fix real defects
(with tests where useful), reject incorrect findings with a one-line reason, then commit and push.

Severity follows a realistic trigger: P1/P2 require behavior reachable with inputs or states from this
repository, the brief or normal use; hypothetical or synthetic inputs are P3. Unrequested scope is a
finding too: options, settings, abstractions, files or generality that "Done when" does not need are P2
when they add an unrequested interface or behavior, otherwise P3; say what to remove. Fixes use the
smallest change and narrow the solution: if a fix needs a new mechanism, make the unsupported case
fail loudly or reject the finding as `out of scope`, with the reason in its finding decision and the
work in `remaining`. Fix unrequested-scope findings by deleting the extra code.

The saved check runs before the next review, which starts in a fresh thread. That review receives the
rejected findings and their reasons, with an instruction to explain any disagreement before raising one
again. From the second review on, reviewers look for regressions introduced by fixes and unmet "Done
when" conditions; new edge cases outside the brief are P3 only. After a review with angles, the next
review runs only the angles that found defects, plus `correctness`; an ordinary single review stays a
single review.

A check failure after a fix spends the next available round on the last 40 lines of `check.log`
instead of review findings. If no round remains, the cycle stops without another review.

Each round records the commits added to the task branch, including supervisor commits in workspace
sandbox mode. After a fix review, a P1/P2 finding with a file and line stops the cycle with
`auto_fix_stop: "escalate"` if `git blame` at `HEAD` attributes any line within two lines of its
location to a commit from an earlier fix round. Missing files and out-of-range lines do not match;
P3 findings keep their existing behavior. Claude chooses the next action as described in
[SKILL.md](../SKILL.md#stop-rules).

Round size is informational and does not stop the cycle; see
[results.md](results.md#what-wait-prints) for the output.

## Cycle stops

The automatic cycle stops on a `clean` review, only P3 findings (`auto_fix_stop: "only P3"`), the round
limit, escalation or the budget gate described above,
a review where every angle refused (`auto_fix_stop: "review refused"`), an incomplete implementation
result, or a non-completed [turn outcome](results.md#turn-outcomes).

A base conflict without an available fix round stops before review with
`auto_fix_stop: "base conflict"`, including when `--fix-rounds` is omitted or zero. `wait` names the
base and conflicting files so Claude can decide how to continue.
