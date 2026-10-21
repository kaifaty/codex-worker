# Results, outcomes and wrap-ups

Implementation and review turns run with `--output-schema` and strict schemas saved in the task
directory. Scouts keep their plain-text reports.

## Implementation result

The supervisor saves valid implementation JSON in state as `result`; `status` shows the last one.

| Field | Content |
|---|---|
| `status` | `done` (every "Done when" condition met), `partial` (some work done, can continue), `blocked` (an external change is needed, or two attempts failed), `needs_decision` (a decision is missing from the brief) |
| `summary` | short plain-language summary |
| `checks` | `{command, outcome, details}`; `outcome` is `passed`, `failed` or `not_run`, `details` carries numbers |
| `pr` | compatibility field; the worker returns an empty string |
| `pr_title` | required string; PR title, or empty to use the latest commit subject; when creating a PR, first reuse the latest nonempty title from this task's earlier results |
| `pr_body` | required string; description of the whole PR, not just this turn; empty leaves an existing PR unchanged; when creating one, reuse the latest nonempty body from this task's earlier results, then fall back to `summary` |
| `commit_message` | repository-style message, subject on the first line; empty when the worker committed or nothing changed |
| `questions` | questions for Claude |
| `remaining` | unfinished work, including findings deferred as `out of scope` |
| `findings` | in a fix turn, `{id, decision, note}` per supplied finding, `decision` being `fixed` or `rejected` with a one-line reason; an empty array in ordinary turns |

After every work turn, including fixes and wrap-ups, the supervisor creates or updates the PR only
when `origin` has the local branch's HEAD. The `publish` receipt records its URL, action (`created`,
`updated`, `unchanged`, or `skipped`), and error. `wait` prints `== PR created <URL>`,
`== PR updated <URL>`, or `== PR skipped: <reason>`; its final `PR:` uses the receipt URL.
Missing or unauthorized GitHub CLI and publication errors do not fail the turn. Closed and merged
PRs are left unchanged.
For a new PR, title and body fall back independently to earlier results across the task's threads.
Known non-GitHub `origin` hosts are skipped with `not a GitHub remote: <host>` before invoking the
GitHub CLI; SSH aliases are resolved with `ssh -G`. Commit and push behavior is unchanged.

## CI result

When the [task CI setting](settings.md#task-ci-options) is enabled, state includes `ci`: the PR head
SHA, outcome (`pending` while waiting, then `passed`, `failed`, `timeout`, `none`, or `skipped`), checks
and states, cumulative wait duration, repair-round count, and stop reason. The reason identifies a
pass, exhausted repair rounds, an incomplete repair result, a budget stop, timeout, absent checks, or
why waiting was skipped. See [ci.md](ci.md) for waiting and repair mechanics.

`wait` prints, for example, `== CI 3749d91: passed (8/8, 6.2 min)` or
`== CI 3749d91: FAILED 1/8: test (macos-latest, 3.9)`, followed by failed-check log tails.
External checks show their names and links. Claude's merge decisions remain in
[SKILL.md](../SKILL.md#the-loop).

## Review result

Valid review JSON is saved in the task's review record.

| Field | Content |
|---|---|
| `verdict` | `clean` or `defects` |
| `findings` | `{id, severity, file, line, title, detail, verify}`; ids are `F1`, `F2`…, severity is `P1`, `P2` or `P3` |
| `summary` | short summary of the review |

An invalid or non-JSON final message gives `result: null`; `wait` labels it `unknown` and prints the
original text.

## Turn outcomes

These are all values written to the task's top-level `outcome`. They describe supervisor execution,
separately from the implementation `result.status`, saved checks, CI and reset-credit outcomes.

| Outcome | Meaning | What Claude does |
|---|---|---|
| `running` | A work, scout or review phase is active. | Let the active phase finish. |
| `sleeping` | No account is currently usable; the supervisor is waiting to retry or for a reset. | Follow [accounts.md](accounts.md#waiting-for-a-reset) for `auto-continue at` and `wait --through-sleep`. |
| `completed` | Exec completed successfully; an implementation result or review may still be incomplete or unknown. | Read `result` or the review and continue the [default loop](../SKILL.md#the-loop). |
| `failed` | Exec or the supervisor failed, or no usable authenticated account remains. | Read the error and logs; resolve the cause, including any requested login, before a follow-up. |
| `refused` | The model refused for possible cybersecurity risk; no retry or account rotation follows. `wait` shows the first 200 characters of the refusal. | Read the refusal and reassess the task rather than starting another automatic fix cycle. |
| `stopped` | A durable `stop` request addressed this supervisor. | Inspect the preserved work and decide whether to continue with `say`. |
| `interrupted` | A signal interrupted the supervisor without a matching `stop` request. | Check the preserved worktree with `git status`, then continue with `say`; if it repeats, inspect the signal line in `stderr.log`. |
| `exhausted` | Account capacity is unavailable and reset waiting is disabled or exceeded eight days. | Inspect `accounts` and the recorded reset time; resolve availability before continuing with `say`. |
| `budget_exhausted` | The turn reached its time cap, or the task reached its weekly budget. | Use the check, wrap-up and remaining work to decide whether to split the task. For a time stop, split or send a budgeted `say`; for a weekly stop, raise the saved budget with `say --max-weekly P` only after that decision. |
| `stuck` | The same command failed with the same nonzero exit code and last 2 KB of output `repeat_limit` times. Other commands do not reset the count; a successful run of that command does. | Investigate the failure yourself or take it to the user; do not start another automatic fix cycle. |

After `budget_exhausted` or `stuck` the supervisor stops the exec process group and asks for a wrap-up
in the same thread, in the configured language. Time or weekly exhaustion asks for working changes to be
committed and pushed, a `partial` result and the remaining work; repeated failure asks for a `blocked`
result with numbers and attempts. The wrap-up has a 10-minute cap and is stopped when it reaches it.
`wait` prints the stop reason, then the wrap-up result. For a weekly stop it prints, for example,
`== turn stopped: weekly budget 10% exhausted (spent 10.4%)`. Saved-check behavior is described in
[checks.md](checks.md#when-the-saved-check-runs).

## What `wait` prints

- After a work turn: the result fields above, the branch's commits and uncommitted files.
- After a review: findings with IDs, severity, file and line, details and verification steps; with
  angles, a runtime and outcome line per angle before the combined review.
- After an automatic fix cycle: finding counts and decisions per round, rejected findings with reasons,
  code additions/deletions excluding guarded files, check results, the latest review in full, a
  finding-count history, and the stop reason. For `escalate`, the stop line names each P1/P2 finding
  targeting code from a fix round, that round's number, and its short commit SHA.
- Base merge rounds name the base and conflicting files. A base conflict without remaining fix
  rounds stops before automatic review with `auto_fix_stop: "base conflict"`; `wait` prints the base
  and conflicting files even when `--fix-rounds` is zero. Failed fetch or merge-tree checks print
  their recorded skip reason and allow review to proceed; see [review-cycle.md](review-cycle.md).
- `guarded files changed` and `== base tests`: see [checks.md](checks.md).
- `== sandbox: workspace` and publication results: see [sandbox.md](sandbox.md).
- CI outcome, checks, and failed-check log tails, when CI waiting is enabled.
- Account switches, `auto-continue at` and `needs login`: see [accounts.md](accounts.md).
