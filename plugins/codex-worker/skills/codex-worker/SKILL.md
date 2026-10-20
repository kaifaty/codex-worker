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
| [results.md](references/results.md) | reading `wait` output: result and review fields, CI, outcomes, wrap-ups |
| [budgets.md](references/budgets.md) | time and weekly caps, exhaustion, or headroom warnings |
| [ci.md](references/ci.md) | GitHub check waiting and CI repair rounds |
| [checks.md](references/checks.md) | `wait` prints `guarded files changed` or `base tests`, or you consider `--guard` or `--base-check` |
| [review-cycle.md](references/review-cycle.md) | angle choices, fix-round mechanics and automatic stops |
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

Visual and 3D work stays with Claude Opus 5.5: textures, image-generation prompts, models, materials,
animations, scenes, rendering and shader code, and any review of them. Do it yourself, or through an Opus
subagent when you run on another model. Codex gets no task, scout or review of it; split a mixed task and
delegate only the rest. The one exception is image generation: a scout in an empty directory may run
Codex's `image_gen` tool with the prompt you wrote. Ask it only for the file paths, not for an
assessment, and judge each image yourself.

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

`start` appends the [standing rules](references/settings.md#standing-rules); don't repeat them in the
brief. PR fields and publication receipts are described in [results.md](references/results.md).

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
Use `--reviewer claude` on `start`, `say`, or `review` for reviews on an isolated Anthropic subscription; Codex still implements and fixes ([setup](references/accounts.md#claude-reviewer-account)).

| Step | Command arguments |
|---|---|
| Start a task | `start <slug> --brief brief.md [--repo DIR] [--base origin/<branch>] [--effort E] [--sandbox off\|workspace] --max-minutes M [--max-weekly P] --review [--review-angles LIST] --fix-rounds 2 [--ci] [--ci-rounds N] --check "<command from Checks>" [--check-timeout SEC] [--guard GLOB] [--base-check]` (base defaults to the remote's default branch; `--guard` is repeatable) |
| Scout code | `scout <slug> "question"` or `scout <slug> --file Q.md`, with `[--repo DIR] [--effort E]`; read-only in the existing directory (default `.`), no worktree or branch |
| Wait for the cycle | `wait <slug> [--through-sleep]` as a **background** Bash command: it re-invokes you when the whole review/fix cycle ends; read its [output](references/results.md#what-wait-prints) |
| Follow-up / fix request | `say <slug> "…"` or `say <slug> --file FILE`, with `--max-minutes M [--max-weekly P] [--effort E] [--review] [--review-angles LIST] [--fix-rounds 2] [--ci\|--no-ci] [--ci-rounds N]` (only when idle), then `wait` again; the saved check is inherited unless replaced with `--check CMD` or removed with `--no-check`; `--no-base-check` drops inherited base tests |
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

Pass `--max-minutes M` on every implementation `start` and `say`, sized to the task. Use
`--max-weekly P` to bound weekly usage; allow extra budget for parallel work on one account.
See [budgets.md](references/budgets.md) for cap mechanics and [telemetry.md](references/telemetry.md)
for usage accounting. Saved checks and their output are described in [checks.md](references/checks.md).

## The loop

Before briefing unfamiliar code, ask scouts instead of reading files yourself, then `wait` for their
reports. Verify key facts by reading only the cited `file:line` locations; use `say` for follow-ups.

1. Write the brief to a file outside the repository (the session scratchpad or a temporary directory),
   so it cannot enter a checkout commit. Run
   `start --max-minutes M --max-weekly 10 --review --fix-rounds 2 --ci --check "<command from Checks>"`
   for GitHub repositories (omit `--ci` elsewhere), then
   one background `wait` for the whole cycle. Add `--review-angles auto` for large or risky PRs (processes, concurrency,
   security), `--base-check` when behavior must stay the same (refactoring, optimization), and `--guard`
   for test files the check reaches only through `sh -c`. Tell the user in a line what went to Codex and
   that you will report back; don't sit and poll.
2. When `wait` returns, follow the [turn outcome table](references/results.md#turn-outcomes), then read
   `result`:
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
   wrong, what to do and how it is checked, with `--max-minutes M --review --fix-rounds 2 --ci` for another
   bounded cycle. Decide yourself whether P3 findings and work rejected as `out of scope` belong in a
   separate task, and record deferred work in issues when appropriate.
5. For a GitHub PR, merge only when `wait` reports `CI … passed`; `failed`, `timeout`, `none`, and
   `skipped` need resolution first. Immediately before merging, run `gh pr checks <url>` again and
   verify all checks pass or skip. Merge per the repo's rules, then `clean`. Report to the user: what Codex did, what you
   checked, what is still open, the PR link, and any account switch `wait` reported.

## Stop rules

- Read the [automatic stop reason](references/review-cycle.md#cycle-stops) and decide what happens
  to remaining P3 findings.
- On `escalate`, read the finding and the named fix round's diff. Choose one: simplify with a narrow
  `say` instruction to remove the extra mechanism; accept the finding as P3 or record an issue; or
  allow one focused round with `say <slug> "…" --max-minutes M --review --fix-rounds 1`.
- After two rounds on the same point, take it to the user.
- A Codex PR you started through this skill is your work for the repo's merge rules; still ask the user
  before merging anything they asked to see first, or anything that breaks what is in use.

## Pitfalls

- Tasks run local Codex tools with the user's Codex configuration. Make the task boundaries explicit in
  the brief and follow the repository's rules.
- Do not add accounts or enable rotation unless the user tells you to
  ([accounts.md](references/accounts.md)).
- `clean` removes the task worktree and logs; run it only after reviewing and merging the result.
