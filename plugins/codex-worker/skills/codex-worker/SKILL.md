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
JSON result with a plain-language summary, checks, PR, questions, remaining work, finding decisions,
and a `commit_message` string (empty after worker commits or when there are no changes).
Ordinary turns return an empty `findings` array; fix turns use `{id, decision: fixed|rejected, note}`
for the supplied findings. The settings
choose `references/rules.md` or `references/rules.ru.md`; `~/.codex/codex-worker-rules.md`, if present,
replaces either file. Use that override for another
language or your own rules; `{branch}` and `{base}` are replaced with the task's branch and PR target.

The same language setting selects the scout, handoff, review, fix, wrap-up, and reset-continuation prompts written by
the script. Scouts use their own read-only instructions instead of task rules. Account status output
is always English. The old `~/.codex/claude-accounts.json` is read only
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
| Start a task | `start <slug> --brief brief.md [--repo DIR] [--base origin/<branch>] [--effort E] [--sandbox off\|workspace] --max-minutes M --review --fix-rounds 2 --check "<command from Checks>" [--guard GLOB] [--base-check]` (base defaults to the remote's default branch; `--guard` is repeatable) |
| Scout code | `scout <slug> "question"` or `scout <slug> --file Q.md`, with `[--repo DIR] [--effort E]`; reads the existing directory (default `.`) with a read-only sandbox and creates no worktree or branch; `wait` prints its concise report |
| Wait for the cycle | `wait <slug>` as a **background** Bash command: it re-invokes you when the full review/fix cycle ends and prints its result, the branch's commits and uncommitted files |
| Follow-up / fix request | `say <slug> "…"` or `say <slug> --file FILE`, with `--max-minutes M` and optionally `--review --fix-rounds 2` (only when idle), then `wait` again; the saved check is inherited unless replaced with `--check CMD` or removed with `--no-check`; `--no-base-check` disables inherited base tests |
| First review | `review <slug> [--effort E]` (only when idle), then `wait`; `--review` on `start`/`say` runs it after a `done` result and a passing check, or an unstructured result for compatibility |
| Look | `status [<slug>]` — account, state, PR, commits, last message, and the last turn's `result` |
| Cost | `stats <slug>` — per-turn time, tool calls, tokens, weekly-limit % across all task threads |
| Accounts | `accounts` — logins, plans, limits, reset times and credits |
| Sign in | `login <name> [--browser]` — human signs in with a device code, or in the browser |
| Interrupt | `stop <slug>` (there is no mid-turn steer for exec; stop, then `say`) |
| Finish | `clean <slug>` after the PR is merged: removes the worktree, deletes the local branch, archives every thread under its account |

The worktree is `~/.codex/worktrees/claude-<slug>/<repo>` on branch `codex/<slug>`; state and logs are in
`~/.codex/claude-tasks/<slug>/`. Only one task runs at a time unless you pass `--parallel`; use that only
when the tasks' builds and tests cannot interfere. A task's turn survives the end of your session; in a
new session `status` and `wait` pick it up again.

## Optional workspace sandbox

The `sandbox` setting in `~/.codex/codex-worker.json` accepts `"off"` (default) or `"workspace"`.
`start --sandbox off|workspace` overrides it for one task; `say` inherits the saved mode. For example:

```json
{
  "sandbox": "workspace",
  "writable_dirs": ["~/.gradle", "~/.npm"]
}
```

`writable_dirs` expands `~` and makes paths absolute. In workspace mode, worker and review execs,
including resumes and account rotations, use `workspace-write` with network access. Saved checks run
through `codex sandbox` with the same permissions; scouts remain read-only.

The sandbox blocks writes outside the worktree, protecting task state and saved checks, other
repositories, `~/.ssh`, and `~/.codex`. Temporary directories stay writable, and `writable_dirs`
explicitly grants write access to its entries. Builds with caches outside the worktree fail until the
cache directories are added. The sandbox does **not** restrict disk reads, including tokens, or
network access.

Workspace instructions override the worker's commit and publication rules: leave changes in the
worktree and return a repository-appropriate `commit_message`, with the subject on its first line.
After each completed implementation turn, including fix rounds and any result status, the supervisor
commits before checking, disables Git hooks, pushes without force, and creates a PR if `gh` is
available and the branch has none. An empty message uses `<slug>: turn <N>`. Failed and stopped turns
are not committed. Publication errors do not fail the turn; `wait` prints the publication result or
error and `== sandbox: workspace`. `status` shows the saved mode.

## Turn checks and limits

Choose `--max-minutes M` for every implementation `start` and `say` according to the task's size;
split a task whose safe budget would be very long. Without the flag, `max_turn_minutes` in
`~/.codex/codex-worker.json` is the default; if both are absent, there is no time cap. The budget counts
active exec time across account rotations, excluding reset sleeps and account waits. Reviews and
scouts have their own `max_review_minutes` budget (default 30), which does not consume implementation
time.

The supervisor also detects repeated command failures: the same command, nonzero exit code, and
last 2 KB of output must match `repeat_limit` times (default 3). Other commands do not reset the count;
a successful run of that command does. Time exhaustion or repeated failure stops the exec process
group and requests a wrap-up in the same thread, in the configured language. Time exhaustion asks
for working changes to be committed and pushed, a `partial` result, and remaining work; repeated
failure asks for a `blocked` result with numbers and attempts. The wrap-up has a 10-minute cap and
is stopped if it reaches that cap. Outcomes are `budget_exhausted` and `stuck`, alongside
`completed`, `failed`, and `stopped`. `wait` prints the stop reason followed by the wrap-up result.

Scouts can run alongside tasks and in parallel without `--parallel`, one question per scout. `say`
continues the same read-only thread; `--check`, `--review`, `--fix-rounds`, and `review` are unavailable
for scouts.
`clean` archives scout threads and removes only its task state and logs, preserving the inspected
directory.

The supervisor runs the saved check in the worktree after each completed implementation turn, before
automatic review. `--check-timeout SEC` defaults to 1800; output goes to the task's `check.log`.
Failure or timeout skips the requested review, and `wait` shows the result and the last 40 output
lines. Results of `partial`, `blocked`, or `needs_decision` also skip automatic review, with
`review_skipped: "result <status>"`; the saved check still runs. Failed, stopped, and paused turns do
not run checks. A `budget_exhausted` or `stuck` outcome still runs the saved check as objective evidence,
but skips automatic review and fix rounds. Inside an automatic fix cycle, other incomplete results
stop immediately before checks.

The supervisor guards test directories (`tests/`, `test/`, `__tests__/`, `spec/`), test filenames
(`test_*`, `*_test.*`, `*.test.*`, `*.spec.*`), and CI paths (`.github/workflows/`,
`.gitlab-ci.yml`, `.forgejo/workflows/`). It also guards existing relative repository paths in top-level
check tokens (including `NAME=path` and `--opt=path`) and patterns passed with repeatable
`start --guard "GLOB"`; discovered paths and patterns persist in task state, even after deletion.
Add paths inside `sh -c '…'` through `--guard`, since nested shell commands are not inspected.
After a work turn, `wait` prints `guarded files changed` with each changed file's added and
deleted line counts across the base-to-HEAD diff and uncommitted changes. It separately flags deleted
guarded files and added `skip`/`xfail`/`only` markers. Review prompts name the files and ask the reviewer
to look for removed assertions, skips, wider tolerances, and mocks replacing the code under test.

Use `start --base-check` for tasks that should preserve behavior, such as refactoring and optimization.
After a passing ordinary check, the supervisor creates a temporary detached worktree at the task's
`HEAD`, restores guarded files from the base (removing new files and restoring deleted files), runs
the same command, then removes the worktree. The check record includes the base run's outcome, exit
code, and duration. `wait` prints `== base tests: passed/FAILED` with a log tail. Failure is a signal
to examine: a legitimate behavior change can explain it, and review still runs. This option persists
across turns; `say --no-base-check` disables it.
Use relative paths with `--base-check`: a command containing the task worktree's absolute path
produces `== base tests: skipped (…)` and is not rerun.

Use `start --max-minutes M --review --fix-rounds 2 --check "<command from Checks>"` by default.
`--fix-rounds N` on
`start` or `say` accepts 0–5, defaults to 0, and requires `--review`. Each round sends the review's
findings directly to the implementation thread, with the same account rotation and reset waiting as
`say`. Codex must verify each finding against the code, fix real defects with tests where useful,
reject incorrect findings with a one-line reason, then commit and push. A finding requiring
disproportionate complexity for a case outside "Done when" must be rejected as `out of scope`, with
the reason in its finding decision and the deferred work in `remaining`. The saved check runs before
another review in a fresh thread. That review receives rejected findings and their reasons, with an
instruction to explain any disagreement before raising them again. From the second review onward,
reviewers look for regressions introduced by fixes and unmet "Done when" conditions. New edge cases
outside the brief are P3 only.

The cycle stops on `clean`, only P3 findings (`auto_fix_stop: "only P3"`), the round limit, `partial`,
`blocked`, `needs_decision`, `budget_exhausted`, `stuck`, or `stop`. Claude decides how to handle the
remaining P3 findings. A check failure after a fix spends the next available round on the last 40
lines of `check.log` instead of
review findings. If no round remains, it stops without another review. A single `wait` follows the
cycle and prints round finding counts and decisions, rejected findings with reasons, check results,
the latest review in full, a finding-count history, and the stop reason. If the task sleeps at a limit,
the usual `wait --through-sleep` option applies.

Every review prompt names the command and result of a passing saved check and tells the reviewer not
to rerun the full suite. Only focused checks needed to confirm a specific finding should run again.

Implementation and review turns use `--output-schema` with strict schemas saved in the task directory.
`wait` renders the implementation's `status`, `summary`, `checks`, `pr`, `questions`, `remaining`, and
`findings` decisions, plus review findings with IDs, severity, file and line, details, and verification
steps. Reviews have a
`clean` or `defects` verdict. The supervisor saves valid implementation JSON in state as `result` and
valid review JSON in the review record. An invalid or non-JSON final message has `result: null`;
`wait` labels it `unknown` and prints the original text. Scouts keep their plain-text reports.

The user can configure task notifications with `notify` in `~/.codex/codex-worker.json`: `"macos"` or
an argv list receiving the event JSON as its final argument. `notify_events` optionally filters event
types. Hooks run asynchronously and write output or launch errors to the task's `notify.log`; see the
README for examples.
`review_done` includes the review's `round`; `turn_finished` fires once at the end of the whole cycle
and includes `guarded_changed`, the number of changed guarded files, and the typed `outcome`, including
`budget_exhausted` or `stuck`. Separate `budget_exhausted` and `stuck` events obey `notify_events` too.

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

Before briefing unfamiliar code, ask a scout instead of reading files yourself. Run scouts in
parallel, one question each, and `wait` for their reports. Verify key facts by reading only the cited
`file:line` locations; use `say` for follow-up questions.

1. Write the brief to a temporary file outside the repository, such as the session scratchpad or a
   temporary directory, so it cannot enter a checkout commit. Choose `--max-minutes M` by task size
   and run `start --max-minutes M --review --fix-rounds 2
   --check "<command from Checks>"`, then one `wait` in the background for the whole cycle. Tell the
   user in a line what went to Codex and that you will report back; don't sit and poll.
2. When `wait` returns, first read the turn outcome and `result`. After `budget_exhausted`, use the
   check, wrap-up, and remaining work to decide whether to continue with a budgeted `say` or split
   the task. After `stuck`, investigate the repeated failure yourself or take it to the user; do not
   start another automatic fix cycle. For `needs_decision`, decide yourself if the decision is
   within the task; otherwise ask the user, then send the decision with `say`. For `blocked`, resolve
   the blocker or take it to the user. For `partial`, send `say` with the next instructions. For
   `done`, read the check result and latest review from its fresh thread; if there was no
   `--review`, run `review` and `wait` again. If `result` is unknown, read the original final message
   and follow the same check and review process as before. The supervisor's check result is objective
   evidence for that command; the summary, finding decisions, and rejection reasons are worker claims
   to verify. If the check failed and the cycle stopped, send a fix request before requesting a review.
3. Read the actual diff, latest review, and every rejection reason. Check each finding and rejection
   against the code (`gh pr diff <n>` or
   `git -C <worktree> diff <base>`), then review the parts the findings did not cover for real failures.
   If `wait` prints `guarded files changed`, read every named file in full and check that tests and
   checks were not weakened through removed assertions, skips, wider tolerances, or mocks replacing
   the code under test. Read a deleted file's base version. Examine any base-test failure before
   accepting the work, even when the ordinary check and review pass.
   Follow the repo's review rules; use `/code-review` for big or risky PRs if available. Check its
   numbers against the "Done when" list. A passing check does not replace reading the diff.
4. Found problems or findings left after the round limit → one `say` with all of them, each with what's
   wrong, what to do and how it is checked; choose `--max-minutes M` and use `--review --fix-rounds 2`
   for another bounded cycle. Decide yourself whether P3 findings and work rejected as `out of scope`
   belong in a separate task.
   Record deferred work in issues when appropriate. Same stop rule: after two rounds on the same
   point, take it to the user.
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

