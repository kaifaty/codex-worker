---
name: codex-worker
description: Delegate implementation work to Codex (OpenAI's coding agent, using the user's ChatGPT plan) and stay in control of it - Claude plans, writes the brief, reviews the result and merges; Codex writes the code in its own git worktree. Use it to save the Claude subscription on any sizeable, well-specified coding task (a feature step, a fix with a clear "done when", engine/C++ work, benchmarks, mechanical changes), when the user says to give work to Codex, and to steer or check a Codex thread already open in the Codex desktop app - also in Russian (поручи кодексу, отдай codex, пусть кодекс сделает, делегируй, кодекс исполнитель, проверь кодекс, что там у кодекса).
---

# Codex as the worker, Claude as the lead

Codex runs on the user's ChatGPT plan, Claude on the Claude subscription. Codex writes the code; you decide
what to build, write the brief, check the result and merge. Hand over the implementation to preserve
Claude usage for planning, discussion, and review.

Reference detail lives in `references/`. Read a file only when its topic comes up:

| File | Read it when |
|---|---|
| [results.md](references/results.md) | reading `wait` output: result and review fields, outcomes, wrap-ups |
| [checks.md](references/checks.md) | `wait` prints `guarded files changed` or `base tests`, or you consider `--guard` or `--base-check` |
| [review-cycle.md](references/review-cycle.md) | you need to know how angles and fix rounds run inside the cycle |
| [settings.md](references/settings.md) | brief language, standing-rules override, default caps, `config.toml` trust |
| [sandbox.md](references/sandbox.md) | the user wants `--sandbox workspace`, or `wait` prints `== sandbox: workspace` |
| [accounts.md](references/accounts.md) | subscriptions and rotation, `auto-continue at`, `needs login`, reset credits |
| [notifications.md](references/notifications.md) | the user asks about task notifications |
| [telemetry.md](references/telemetry.md) | `report`, or deciding whether fix rounds or review angles pay off |
| [desktop-threads.md](references/desktop-threads.md) | the thread runs in the Codex desktop app, not under this skill |

## Delegate or do it yourself

Delegate when the task is code with a clear end: one feature step, a bug fix with a reproduction, engine or
C++ work, a benchmark or test harness, a refactor with tests, a batch of mechanical edits.

Keep it yourself when:

- it is a decision, a design question, a plan, or a talk with the user;
- it is small — a few edits or tool calls cost less than writing a brief and reviewing a PR;
- it needs the user's live systems you are trusted with and Codex is not (accounts, publishing, uploads);
- the spec is still fuzzy: settle it first (with the user if needed), then delegate.

## The brief

A task costs what its turns last, not its reasoning level: a sprawling multi-hour thread with
self-invented protocols can consume a weekly quota. The brief keeps the turn short and the goal clear.
Write it in the language set by `language` in `~/.codex/codex-worker.json` (`"en"` by default, `"ru"`
for Russian), translating this structure as needed:

```
<What to do — one sentence.>

Why: <one or two sentences of context, links to docs/ and files to start with>.

Done when:
- <a verifiable condition, preferably a number or test>
- <…>

Checks: <the test, benchmark, or end-to-end test that proves it is done>.
Do not touch: <what is outside the task>.
```

`start` appends the standing rules from [rules.md](references/rules.md) (own worktree, focused checks,
stop after two failed attempts, push and open a PR but never merge, a typed JSON result); don't repeat
them in the brief.

One task per brief; split independent parts into several tasks (stacked branches with `--base` when one
builds on another). Before starting, split any task that promises more than about 60 minutes of work.
Decisions that belong to the user are not Codex's to make: put them in the brief as settled facts, or ask
the user first.

## Commands

Set the script path for this skill:

```sh
T=${CLAUDE_SKILL_DIR}/scripts/codex_task.py
```

Run every command below as `python3 -I "$T" …`. The CLI is resolved from `CODEX_TASK_CODEX`, then the
ChatGPT.app bundle if present, then `codex` on `PATH`. Model and reasoning defaults come from the user's
Codex configuration; `--effort` overrides reasoning effort for a task.

| Step | Command arguments |
|---|---|
| Start a task | `start <slug> --brief brief.md [--repo DIR] [--base origin/<branch>] [--effort E] [--sandbox off\|workspace] --max-minutes M [--max-weekly P] --review [--review-angles LIST] --fix-rounds 2 --check "<command from Checks>" [--check-timeout SEC] [--guard GLOB] [--base-check]` (base defaults to the remote's default branch; `--guard` is repeatable) |
| Scout code | `scout <slug> "question"` or `scout <slug> --file Q.md`, with `[--repo DIR] [--effort E]`; read-only in the existing directory (default `.`), no worktree or branch |
| Wait for the cycle | `wait <slug> [--through-sleep]` as a **background** Bash command: it re-invokes you when the whole review/fix cycle ends and prints its result, the branch's commits and uncommitted files |
| Follow-up / fix request | `say <slug> "…"` or `say <slug> --file FILE`, with `--max-minutes M [--max-weekly P]` and optionally `--review [--review-angles LIST] --fix-rounds 2` (only when idle), then `wait` again; the saved check is inherited unless replaced with `--check CMD` or removed with `--no-check`; `--no-base-check` drops inherited base tests |
| First review | `review <slug> [--effort E] [--review-angles LIST] [--max-weekly P]` (only when idle), then `wait` |
| Look | `status [<slug>]` — account, state, PR, commits, last message, and the last turn's `result` |
| Cost | `stats <slug>` — per-turn time, tool calls, tokens, weekly-limit % across all task threads |
| Compare costs and outcomes | `report [--since 7d\|30d\|YYYY-MM-DD] [--repo NAME] [--slug SLUG] [--json]` |
| Accounts | `accounts` — logins, plans, limits, reset times and credits; `reset-credit <account> [--credit ID] [--yes]` |
| Sign in | `login <name> [--browser]` — the human signs in with a device code, or in the browser |
| Interrupt | `stop <slug>` (there is no mid-turn steer for exec; stop, then `say`) |
| Finish | `clean <slug>` after the PR is merged: removes the worktree and logs, deletes the local branch, archives every thread |

The worktree is `~/.codex/worktrees/claude-<slug>/<repo>` on branch `codex/<slug>`; state and logs are in
`~/.codex/claude-tasks/<slug>/`. Only one task runs at a time unless you pass `--parallel`; use that only
when the tasks' builds and tests cannot interfere. A task's turn survives the end of your session; in a
new session `status` and `wait` pick it up again.

Scouts need no `--parallel`: run several at once, one question each, alongside tasks. `say` continues a
scout's read-only thread; `--check`, `--review`, `--review-angles`, `--fix-rounds` and `review` don't
apply to scouts. `clean` on a scout removes only its state and logs, never the inspected directory.

## Budgets and checks

Pass `--max-minutes M` on every implementation `start` and `say`, sized to the task. M counts active
exec time across account rotations, not reset sleeps or account waits. Reviews and scouts have their own cap (`max_review_minutes`, 30 by default). The
supervisor also stops a turn that keeps repeating the same failing command, then asks for a wrap-up in
the same thread.

Use `--max-weekly P` (greater than 0 and at most 100) to cap the task in percentage points of the weekly
limit. The task accumulates usage across follow-ups, work, reviews, angles, fix rounds, wrap-ups, and
accounts. Each account keeps a weekly high-water mark: repeated or lower samples add zero; a reset
timestamp advancing by more than an hour starts a new mark without charging the reset sample.
The default is `max_task_weekly_percent`, or no weekly cap when
unset. An exhausted task rejects `say` and `review` until `--max-weekly P` raises its budget above the
measured spend. Exhaustion wraps up, runs the saved check, and skips further automatic phases. `status`
and `wait` warn when the remaining budget exceeds account headroom up to `switch_at`.

The saved check runs in the worktree after each completed implementation turn, before review. A failure
or timeout skips the review, and `wait` shows the last 40 lines of `check.log`.

## The loop

Before briefing unfamiliar code, ask scouts instead of reading files yourself, then `wait` for their
reports. Verify key facts by reading only the cited `file:line` locations; use `say` for follow-ups.

1. Write the brief to a file outside the repository (the session scratchpad or a temporary directory),
   so it cannot enter a checkout commit. Run
   `start --max-minutes M --max-weekly 10 --review --fix-rounds 2 --check "<command from Checks>"`, then
   one background `wait` for the whole cycle. Add `--review-angles auto` for large or risky PRs (processes, concurrency,
   security), `--base-check` when behavior must stay the same (refactoring, optimization), and `--guard`
   for test files the check reaches only through `sh -c`. Tell the user in a line what went to Codex and
   that you will report back; don't sit and poll.
2. When `wait` returns, read the turn outcome and `result` first:
   - `budget_exhausted`: use the check, wrap-up and remaining work to decide whether to split the task
     before raising an exhausted weekly budget with `say --max-weekly P`. For a time stop, choose a
     budgeted `say` or split the task.
   - `stuck`: investigate the repeated failure yourself or take it to the user; do not start another
     automatic fix cycle.
   - `refused` (a model refusal for possible cybersecurity risk): it is not retried or rotated.
   - `needs_decision`: decide yourself if the decision is within the task, otherwise ask the user; send
     the decision with `say`. `blocked`: resolve the blocker or take it to the user. `partial`: `say`
     the next instructions.
   - `done`: read the check result and the latest review; if there was no `--review`, run `review` and
     `wait` again. If the check failed and the cycle stopped, send a fix request before a review.
   - `result` unknown: read the original final message and continue the same way.

   The supervisor's check result is objective evidence for that command; the summary, finding decisions
   and rejection reasons are worker claims to verify.
3. Read the actual diff (`gh pr diff <n>` or `git -C <worktree> diff <base>`), the latest review and
   every rejection reason. Check each finding and rejection against the code, then review the parts the
   findings did not cover for real failures. On `guarded files changed`, read every named file in full
   (a deleted file's base version too) and make sure no assertion was removed, no test skipped, no
   tolerance widened, and no mock replaced the code under test. Examine any base-test failure before
   accepting the work, even when the check and review pass. Follow the repo's review rules; use
   `/code-review` for big or risky PRs if available. Check the numbers against "Done when". A passing
   check does not replace reading the diff.
4. Problems found, or findings left after the round limit → one `say` with all of them, each with what's
   wrong, what to do and how it is checked, with `--max-minutes M --review --fix-rounds 2` for another
   bounded cycle. Decide yourself whether P3 findings and work rejected as `out of scope` belong in a
   separate task, and record deferred work in issues when appropriate.
5. Good → merge per the repo's rules, then `clean`. Report to the user: what Codex did, what you
   checked, what is still open, the PR link, and any account switch `wait` reported.

## Review angles and fix rounds

`--review-angles auto` runs `correctness,concurrency,tests` when the
diff from base (with uncommitted changes) has at least `review_angles_min_lines` changed lines (400 by
default), otherwise one ordinary review. For a specific risk, pass a comma-separated list instead:

- `correctness`: behavior and the brief's "Done when" conditions;
- `concurrency`: races, processes, signals, shared files and state, retries, and recovery;
- `security`: secrets, shell and path injection, untrusted input, permissions, and sandbox boundaries;
- `tests`: weakened or ineffective tests, uncovered brief conditions, and mocks replacing tested code.

On `start` and `say`, `--review-angles` implies `--review`. `--fix-rounds N` (0–5, default 0) needs
`--review`; each round sends the findings back to the implementation thread to be fixed or rejected
with a reason, then checks and reviews again.

## Stop rules

- The automatic cycle stops on a `clean` review, only P3 findings (`auto_fix_stop: "only P3"`), the round
  limit, an exhausted task weekly budget (`auto_fix_stop: "budget"`), a review where every angle refused
  (`auto_fix_stop: "review refused"`), `partial`, `blocked`, `needs_decision`, `budget_exhausted`, `stuck`,
  `interrupted`, or `stop`. You decide what happens to remaining P3 findings.
- After two rounds on the same point, take it to the user.
- After `interrupted`, check the preserved worktree with `git status` and continue through `say`.
  If interruptions repeat, inspect the signal line in `stderr.log`.
- A Codex PR you started through this skill is your work for the repo's merge rules; still ask the user
  before merging anything they asked to see first, or anything that breaks what is in use.

## Pitfalls

- Tasks run local Codex tools with the user's Codex configuration. Make the task boundaries explicit in
  the brief and follow the repository's rules.
- Do not add accounts or enable rotation unless the user tells you to
  ([accounts.md](references/accounts.md)).
- `clean` removes the task worktree and logs; run it only after reviewing and merging the result.
