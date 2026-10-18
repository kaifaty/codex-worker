# How reviews, angles and fix rounds run

When to choose angles and fix rounds, and when the cycle stops, is in `SKILL.md`. This file covers what
the supervisor does in between. Before each automatic review, angle, or fix round, it checks the
remaining task weekly budget. Exhaustion skips that phase and stops fixes with `auto_fix_stop: "budget"`.

## Automatic review

`--review` on `start` or `say` runs a review in a fresh thread after a `done` result and a passing
check, or after an unstructured result, for compatibility. Every review prompt names the command and
result of the passing saved check and tells the reviewer not to rerun the full suite; only focused
checks that confirm a specific finding run again.

## Angles

Angles run one after another in fresh threads, with ordinary account rotation and reset waiting. Each
angle gets its own review record. The supervisor combines the findings, merges duplicate locations and
titles, and labels each finding with its angles. A refused angle is marked `refused` without stopping
the others, and the verdict uses the other results.

## Fix rounds

Each round sends the review's findings straight to the implementation thread, with the same account
rotation and reset waiting as `say`. Codex must verify each finding against the code, fix real defects
(with tests where useful), reject incorrect findings with a one-line reason, then commit and push. A
finding that needs disproportionate complexity for a case outside "Done when" is rejected as
`out of scope`, with the reason in its finding decision and the deferred work in `remaining`.

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
P3 findings keep their existing behavior. `wait` names the finding, originating round, and short commit
SHA. Claude chooses the next action as described in `SKILL.md`.

`wait` also shows each round's code additions and deletions, excluding guarded files (built-in test
patterns and `--guard` paths). This size is informational and does not stop the cycle.
