---
name: codex-worker
description: Delegate implementation work to Codex (OpenAI's coding agent, using the user's ChatGPT plan) and stay in control of it - Claude plans, writes the brief, reviews the result and merges; Codex writes the code in its own git worktree. Use it to save the Claude subscription on any sizeable, well-specified coding task (a feature step, a fix with a clear "done when", engine/C++ work, benchmarks, mechanical changes), when the user says to give work to Codex, and to steer or check a Codex thread already open in the Codex desktop app - also in Russian (поручи кодексу, отдай codex, пусть кодекс сделает, делегируй, кодекс исполнитель, проверь кодекс, что там у кодекса).
---

# Codex as the worker, Claude as the lead

Codex runs on the user's ChatGPT plan, Claude on the Claude subscription. Codex writes the code; you decide
what to build, write the brief, check the result and merge. Hand over the implementation to preserve
Claude usage for planning, discussion, and review.

## Delegate or do it yourself

Delegate when the task is code with a clear end: one feature step, a bug fix with a reproduction, engine or
C++ work, a benchmark or test harness, a refactor with tests, a batch of mechanical edits.

Keep it yourself when:

- it is a decision, a design question, a plan, or a talk with the user;
- it is small — a few edits or tool calls cost less than writing a brief and reviewing a PR;
- it needs the user's live systems you are trusted with and Codex is not (accounts, publishing, uploads);
- the spec is still fuzzy: settle it first (with the user if needed), then delegate.

## The brief

Task cost is determined by the duration of a turn rather than its reasoning level. A sprawling
multi-hour thread with self-invented protocols may consume an entire weekly quota. The brief keeps
the turn short and the goal clear. Write the actual brief in the user's language, following the
`language` setting in `~/.codex/codex-worker.json` (`"en"` by default, `"ru"` for Russian). Use this
structure, translated as needed:

```
<What to do — one sentence.>

Why: <one or two sentences of context, links to docs/ and files to start with>.

Done when:
- <a verifiable condition, preferably a number or test>
- <…>

Checks: <the test, benchmark, or end-to-end test that proves it is done>.
Do not touch: <what is outside the task>.
```

`start` appends the standing rules: own worktree only, no bureaucracy, checks for this task only, stop
after two failed attempts with numbers, push and open a PR but never merge, no Co-Authored-By, and a
short plain-language report. The settings choose `references/rules.md` or `references/rules.ru.md`;
`~/.codex/codex-worker-rules.md`, if present, replaces either file. Use that override for another
language or your own rules; `{branch}` and `{base}` are replaced with the task's branch and PR target.

The same language setting selects the handoff, review, and reset-continuation prompts written by the
script. Account status output is always English. The old `~/.codex/claude-accounts.json` is read only
when `codex-worker.json` is absent.

One task per brief; split independent parts into several tasks (stacked branches with `--base` when one
builds on another). Decisions that belong to the user are not Codex's to make: put them in the brief as
settled facts, or ask the user first.

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
| Start a task | `start <slug> --brief brief.md [--repo DIR] [--base origin/<branch>] [--effort E] [--review]` (base defaults to the remote's default branch) |
| Wait for the turn | `wait <slug>` as a **background** Bash command: it re-invokes you when Codex's turn ends and prints its final message, the branch's commits and uncommitted files |
| Follow-up / fix request | `say <slug> "…"` or `say <slug> --file FILE`, optionally `--review` (only when idle), then `wait` again |
| First review | `review <slug> [--effort E]` (only when idle), then `wait`; `--review` on `start`/`say` runs it after a successful turn |
| Look | `status [<slug>]` — account, state, PR, commits, last message |
| Cost | `stats <slug>` — per-turn time, tool calls, tokens, weekly-limit % across all task threads |
| Accounts | `accounts` — logins, plans, limits, reset times and credits |
| Sign in | `login <name> [--browser]` — human signs in with a device code, or in the browser |
| Interrupt | `stop <slug>` (there is no mid-turn steer for exec; stop, then `say`) |
| Finish | `clean <slug>` after the PR is merged: removes the worktree, deletes the local branch, archives every thread under its account |

The worktree is `~/.codex/worktrees/claude-<slug>/<repo>` on branch `codex/<slug>`; state and logs are in
`~/.codex/claude-tasks/<slug>/`. Only one task runs at a time unless you pass `--parallel`; use that only
when the tasks' builds and tests cannot interfere. A task's turn survives the end of your session; in a
new session `status` and `wait` pick it up again.

## Multiple subscriptions

> OpenAI's [Terms of Use](https://openai.com/policies/terms-of-use/) prohibit “circumvent any rate limits
> or restrictions.” Rotation is off until the user explicitly lists `accounts` in the settings.
> Using it is at the user's own risk, for one person and only their own subscriptions. Do not add
> accounts or enable rotation without the user's instruction.

Without `accounts` in the settings, tasks use the existing `main` login in `~/.codex`; automatic limit
monitoring and rotation are disabled. A language-only settings file does not enable them. For another
subscription, have the user run `python3 -I "$T" login second`; its separate `CODEX_HOME` is
`~/.codex-accounts/second`. Login writes the new `~/.codex/codex-worker.json` settings file.

When the user has chosen to configure multiple subscriptions, the settings can contain:

```json
{
  "language": "en",
  "switch_at": 99,
  "credits": false,
  "wait_for_reset": true,
  "accounts": [
    {"name": "main", "home": "~/.codex"},
    {"name": "second", "home": "~/.codex-accounts/second"}
  ]
}
```

With `accounts` configured, `start` chooses an available account whose longest window resets first;
`say` keeps the current account while it is available. A detached supervisor watches limits and
switches accounts at the threshold or a usage-limit error. The next account opens a new thread in the
same worktree, with the brief, follow-ups and current progress; commits and uncommitted changes are the
starting point. If no other account is available, it continues on credits only when `credits` is true.

If all configured accounts are exhausted, the task waits and continues automatically after a reset
(up to 8 days); set `wait_for_reset` to false to pause and exit instead. When `wait` returns with
`auto-continue at`, tell the user that local time; run `wait` again later to follow, or use
`wait --through-sleep` to keep waiting. Use `accounts` to inspect availability; two homes signed into
the same ChatGPT account share limits.

`use_reset_credits` defaults to `false`; enable it to spend one free reset before sleeping when the natural reset is more than `reset_credit_min_hours` (default `12`) away.
`reset-credit <account> [--credit ID]` previews the earliest-expiring reset; add `--yes` to consume it and show the RPC response and refreshed limits.
Authentication errors get one retry, then rotation; when `wait` says `needs login`, have the user run `login <account>`.

## The loop

1. Write the brief to a temporary file outside the repository, such as the session scratchpad or a
   temporary directory, so it cannot enter a checkout commit. Run `start --review`, then `wait` in the
   background. Tell the user in a line what went to Codex and that you will report back; don't sit and
   poll.
2. When `wait` returns: read Codex's report and the first review it ran in a fresh thread (for a turn
   started without `--review`, run `review` and `wait` again). The report and the findings are claims,
   not proof.
3. Check every finding against the actual diff (`gh pr diff <n>` or
   `git -C <worktree> diff <base>`), then review the parts the findings did not cover for real failures.
   Follow the repo's review rules; use `/code-review` for big or risky PRs if available. Check its
   numbers against the "Done when" list.
4. Found problems → one `say` with all of them, each with what's wrong, what to do and how it is checked.
   Same stop rule: after two rounds on the same point, take it to the user.
5. Good → merge per the repo's rules, then `clean`. Report to the user: what Codex did, what you checked,
   what is still open, the PR link. If `wait` reported an account switch, mention it.

A Codex PR started by you through this skill is your work for the repo's merge rules; still ask the user
before merging anything they asked to see first, or anything that breaks what is in use.

## Codex threads in the desktop app

For a thread the user runs in the Codex app (not started by this skill), the app owns it, so `say` and
`exec resume` can't touch it ("already has an active writer"). Instead:

- Read it: `~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread-id>.jsonl`; messages have
  `payload.type == "message"` and role user/assistant. Use
  `python3 -I "${CLAUDE_SKILL_DIR}/scripts/codex-stats.py" <thread-id>` for cost.
- Send to it: `codex queue --thread <thread-id> --message "…"` with a CLI that supports `queue`.
  On macOS the bundled CLI is at
  `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
  A queued message starts a turn when the thread is open in the app and idle; during a turn it waits
  for the turn's end. A thread not opened since the app started waits until the user opens it.
  Exit 0 means stored, not delivered — check the rollout. Effort can't be set per message.
- Hear back: `python3 -I "${CLAUDE_SKILL_DIR}/scripts/wait-turn.py" <thread-id>` in the background exits
  when that thread's next turn ends. It reads `~/.codex/turn-events.jsonl`, which the optional
  `${CLAUDE_SKILL_DIR}/scripts/turn-log.py` notify hook fills. See the repository README for setup.
  The hook chains to Codex Computer Use only when its client exists.

Steering a desktop turn mid-way requires the app's Steer control.

## Pitfalls

- Tasks run local Codex tools with the user's Codex configuration. Make the task boundaries explicit
  in the brief and follow the repository's rules.
- Restarting the desktop app kills the turns of the app's threads; exec tasks of this skill are
  separate processes and keep running.
- Never copy `auth.json` between homes; each subscription needs its own sign-in.
- `codex exec` trusts the repository's main checkout in `config.toml`
  (`[projects."<repo>"] trust_level`), not each worktree. A first task in a new repository adds one entry.
- `clean` removes the task worktree and logs; use it after reviewing and merging the result.

