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
| `pr` | PR URL, or an empty string |
| `commit_message` | repository-style message, subject on the first line; empty when the worker committed or nothing changed |
| `questions` | questions for Claude |
| `remaining` | unfinished work, including findings deferred as `out of scope` |
| `findings` | in a fix turn, `{id, decision, note}` per supplied finding, `decision` being `fixed` or `rejected` with a one-line reason; an empty array in ordinary turns |

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

A turn ends as `completed`, `failed`, `refused`, `stopped`, `budget_exhausted` or `stuck`.

- `budget_exhausted`: the turn used up its time budget, or the task used up its `--max-weekly` budget.
- `stuck`: the same command failed with the same nonzero exit code and the same last 2 KB of output
  `repeat_limit` times. Other commands do not reset the count; a successful run of that command does.
- `refused`: the model refused for possible cybersecurity risk; `wait` shows the first 200 characters
  of the refusal.

After `budget_exhausted` or `stuck` the supervisor stops the exec process group and asks for a wrap-up
in the same thread, in the configured language. Time or weekly exhaustion asks for working changes to be
committed and pushed, a `partial` result and the remaining work; repeated failure asks for a `blocked`
result with numbers and attempts. The wrap-up has a 10-minute cap and is stopped when it reaches it.
`wait` prints the stop reason, then the wrap-up result. For a weekly stop it prints, for example,
`== turn stopped: weekly budget 10% exhausted (spent 10.4%)`. The saved check runs; automatic review,
angles, and fix rounds stop. Before increasing the task budget, decide whether to split the work.

## What `wait` prints

- After a work turn: the result fields above, the branch's commits and uncommitted files.
- After a review: findings with IDs, severity, file and line, details and verification steps; with
  angles, a runtime and outcome line per angle before the combined review.
- After an automatic fix cycle: finding counts and decisions per round, rejected findings with reasons,
  code additions/deletions excluding guarded files, check results, the latest review in full, a
  finding-count history, and the stop reason. For `escalate`, the stop line names each P1/P2 finding
  targeting code from a fix round, that round's number, and its short commit SHA.
- `guarded files changed` and `== base tests`: see [checks.md](checks.md).
- `== sandbox: workspace` and publication results: see [sandbox.md](sandbox.md).
- Account switches, `auto-continue at` and `needs login`: see [accounts.md](accounts.md).
