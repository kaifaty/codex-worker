# codex-worker

Let Claude plan and review while Codex implements in its own git worktree. This Claude Code plugin
moves substantial coding work to your ChatGPT subscription, saving Claude subscription usage for the
parts where you want Claude's judgment.

Claude writes a bounded brief, starts Codex, and waits in the background. Codex commits and opens a PR,
or the supervisor publishes its changes when the optional workspace sandbox is enabled.
Optional Codex reviews run in fresh threads, and the supervisor can send findings back for bounded
fix rounds. Claude checks the final diff and review and handles merging. Optional account monitoring
can pause a task until a usage window resets.

This repository is both the `kaifaty` Claude Code marketplace and the `codex-worker` plugin.

## Requirements

- Claude Code with plugin support.
- Codex CLI installed and signed in to a ChatGPT account with Codex access.
- Python 3.9 or newer, using only the standard library.
- Git, a repository with an `origin` remote, and permission to push task branches.
- macOS or Linux.
- Optional: GitHub CLI (`gh`), authenticated for the repository, to open and inspect PRs.

Install [Codex CLI](https://github.com/openai/codex) if needed (`npm install -g @openai/codex`), then run
`codex login`. The worker uses your existing Codex model, reasoning, and permission settings. There is
no required model or reasoning level.

The worker finds Codex in this order:

1. `CODEX_TASK_CODEX`, if set to the executable to use.
2. On macOS, the bundled executable at
   `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`, if present.
3. `codex` on `PATH`.

## Install

In Claude Code:

```text
/plugin marketplace add kaifaty/codex-worker
/plugin install codex-worker@kaifaty
```

Invoke the skill with `/codex-worker:codex-worker`, followed by the task. For example:

```text
/codex-worker:codex-worker Add CSV export for the current search results.
Use the existing filters, add a focused test, and open a PR for review.
```

For a manual installation, clone the repository and copy the skill into your personal Claude skills:

```sh
git clone https://github.com/kaifaty/codex-worker.git
mkdir -p ~/.claude/skills
cp -R codex-worker/plugins/codex-worker/skills/codex-worker ~/.claude/skills/
```

The manually installed skill is invoked as `/codex-worker`. Keep its `scripts/` and `references/`
directories beside `SKILL.md`.

## A short task cycle

The skill handles these commands for Claude. To run them yourself, set `T` to the installed
`scripts/codex_task.py`. With the manual installation:

```sh
T="$HOME/.claude/skills/codex-worker/scripts/codex_task.py"
python3 -I "$T" start csv-export --brief brief.md --max-minutes 45 --review --fix-rounds 2 --check "python3 -m unittest tests.test_csv_export"
python3 -I "$T" wait csv-export
python3 -I "$T" status csv-export
```

Use a brief with the change, context, measurable completion conditions, focused checks, and boundaries.
The worker creates `codex/csv-export` and an isolated worktree, implements the task, runs the check,
and starts a review in a fresh thread if the check passes and the result is `done`. With
`--fix-rounds 2`, it can send findings to the worker, check the fixes, and review again twice without
Claude relaying messages. One `wait` covers the whole cycle; in Claude Code it runs as a background
Bash command.

Work and review turns use strict JSON schemas through `--output-schema`. `wait` renders the work's
status (`done`, `partial`, `blocked`, or `needs_decision`), summary, checks, PR, questions, and remaining
work, plus round summaries, rejected findings with reasons, the latest review, and the stop reason.
Implementation results include `findings` decisions (`fixed` or `rejected`), empty on ordinary turns.
Review findings include IDs, severity, locations, and verification steps. `status <slug>`
includes the latest `result`. Invalid or non-JSON finals remain readable as `result: unknown`, and
automatic review keeps its previous behavior for them. Scouts return plain text.

Check the actual diff, latest review, and rejection reasons: the worker's claims need verification.
Send remaining findings through `say` or record issues after the round limit:

```sh
python3 -I "$T" say csv-export --file fixes.md --max-minutes 30 --review --fix-rounds 2
python3 -I "$T" wait csv-export
```

After `interrupted`, check the preserved worktree with `git status` and continue through `say`.
If interruptions repeat, inspect the signal line in `stderr.log`.

Codex does not merge. After the result has been reviewed and merged under your repository's rules,
remove the worktree and archive its threads:

```sh
python3 -I "$T" clean csv-export
```

## Read code with a scout

Before writing a brief about unfamiliar code, ask a scout a focused question. It reads the current
directory on your ChatGPT subscription and returns a concise report with `file:line` references,
verified facts, assumptions, relevant tests, and what it did not inspect. Check key facts by reading
only the cited lines.

```sh
python3 -I "$T" scout csv-path "Where are export filters applied, and which tests cover them?" --repo .
python3 -I "$T" wait csv-path
python3 -I "$T" say csv-path "Does the same path handle empty results?"
python3 -I "$T" wait csv-path
python3 -I "$T" clean csv-path
```

Use `--file Q.md` instead of a quoted question, and `--effort E` to override reasoning effort.
Scouts enforce a read-only sandbox in the existing checkout or worktree; they create no branch or
worktree. Run scouts alongside implementation tasks or in parallel, one question per scout, without
`--parallel`. Follow up with `say`; checks and reviews are not available for scouts. `clean` removes
the scout's state and logs and archives its threads, leaving the inspected directory untouched.

## Commands

Run all commands as `python3 -I "$T" <command>`.

| Command | Purpose |
|---|---|
| `start <slug> --brief FILE [--repo DIR] [--base origin/BRANCH] [--effort E] [--sandbox off\|workspace] [--max-minutes M] [--review [--review-angles LIST] [--fix-rounds N]] [--check CMD] [--guard GLOB] [--base-check]` | Create a worktree and start implementation; `--brief -` reads stdin; repeat `--guard` for extra protected paths. |
| `scout <slug> "question"` or `scout <slug> --file FILE [--repo DIR] [--effort E]` | Read the existing directory and return a concise report with source references. |
| `wait <slug> [--timeout SEC] [--through-sleep]` | Wait for the turn, show its report and changes; optionally wait through limit resets. |
| `say <slug> "message"` or `say <slug> --file FILE` | Follow up while idle; set `--max-minutes M`, add `--review [--review-angles LIST] --fix-rounds N`, replace the check with `--check CMD`, remove it with `--no-check`, or disable base tests with `--no-base-check`. |
| `review <slug> [--effort E] [--review-angles LIST]` | Run the first review in a fresh Codex thread while the task is idle. |
| `status [<slug>]` | Show task state, account, thread, PR, commits, last message, and latest result. |
| `stats <slug>` | Show per-turn runtime, tool calls, tokens, and available usage estimates. |
| `report [--since 7d\|30d\|YYYY-MM-DD] [--repo NAME] [--slug SLUG] [--json]` | Summarize persistent costs and outcomes by task, account, and period (default: seven days). |
| `accounts` | Show login, plan, limits, reset times, credits, and availability in English. |
| `login <name> [--browser]` | Have the human sign in to an account; writes `codex-worker.json`. |
| `stop <slug>` | Interrupt the task; send the next instructions with `say`. |
| `clean <slug> [--keep-branch]` | Remove the worktree and task logs and archive threads; optionally retain the local branch. |

Tasks use `~/.codex/worktrees/claude-<slug>/<repo>`, with state and logs under
`~/.codex/claude-tasks/<slug>/`. Turns continue independently of Claude's session. Only one task runs at
a time unless `start --parallel` is explicitly used for tasks whose builds and tests cannot interfere.
The default base is the remote's default branch.

The supervisor keeps numeric stage telemetry in `<CODEX_TASK_HOME>/codex-worker-log.jsonl`
(default `~/.codex/`); `clean` preserves it. Use `report` to compare review findings, check success,
fix rounds to clean review, tokens, and weekly-window usage before changing rounds or angle defaults.
Quota sleeps are excluded from active time; weekly usage omits reset crossings and can include other
activity on a shared account. `--json` exposes the same metrics for external reports; dates use UTC.

Ordinary PRs use one review. For large or risky PRs involving processes, concurrency, or security,
add `--review-angles auto` to `start`, `say`, or `review`. At 400 changed lines across `base..HEAD` and
uncommitted changes (configurable with `review_angles_min_lines`), `auto` runs `correctness,concurrency,tests`;
smaller diffs keep one review. To choose the focus explicitly, pass a comma-separated list from
`correctness,concurrency,security,tests`. Each angle runs sequentially in a fresh thread; findings are
merged and duplicates combined, with their angles shown by `wait`. Later fix rounds repeat only
angles with findings, plus `correctness`; a single review stays single. The flag implies `--review`
on `start` and `say`. Scouts do not support review angles.
Model cybersecurity refusals end the turn as `refused` without retrying or rotating; other review
angles continue, and a review with only refusals stops fixes with `auto_fix_stop: "review refused"`.

`--fix-rounds` accepts 0–5 (default 0), requires `--review`, and is unavailable for scouts. The loop
stops on a clean review, only P3 findings (`auto_fix_stop: "only P3"`), the round limit, a worker result
requiring intervention, or `stop`. Claude decides what to do with P3 findings. From the second review,
reviewers focus on regressions from fixes and unmet completion conditions; new edge cases outside the
brief are P3. Fix turns reject disproportionate work outside those conditions as `out of scope` and
put it in `remaining`. A failed check after a fix uses the next available round to send the last 40
lines of `check.log` to the worker;
without a remaining round, it stops without another review. Reviews are told the command and result
of a passing saved check and may run only focused checks for a specific finding, not repeat the suite.

`--max-minutes M` on `start` and `say` caps active implementation exec time, including continuations after
account rotation; reset sleeps and account waits do not count. The default is `max_turn_minutes`, or
no cap if unset. Reviews and scouts use a separate `max_review_minutes` budget (30 by default).
Repeating a command's same nonzero exit code and last 2 KB of output `repeat_limit` times (default 3)
also stops a turn; other commands do not reset its count, but success for that command does.
Either stop kills the exec process group and starts a wrap-up in the same thread, capped at 10 minutes:
time exhaustion asks for commits, a push, and a `partial` result; repeated failure asks for a `blocked`
result with evidence. The typed turn outcome is `budget_exhausted` or `stuck`. `wait` prints
`== turn stopped: time budget 90 min exhausted` or `== turn stopped: repeated failure ×3: <command>`,
then the wrap-up. The saved check runs; automatic review and fix rounds do not.

## Checks after each turn

Pass the brief's Checks command as `start --check "CMD"`. The supervisor runs `/bin/sh -c CMD` in the
task worktree after every completed implementation turn, including follow-ups. `say --check "CMD"`
replaces the saved command; `say --no-check` removes it. `--check-timeout SEC` defaults to 1800 seconds.
In workspace sandbox mode, checks run through `codex sandbox` with the same write permissions as the
worker and reviews.
Failed, stopped, interrupted, and paused turns do not run checks. Turns ending with `budget_exhausted`
or `stuck` still run the saved check and skip automatic review and fix rounds.

Output goes to the task's `check.log`. `wait` prints the exit code and duration; on failure or timeout,
it also prints the last 40 output lines. A failed check skips a requested automatic review. Results
`partial`, `blocked`, and `needs_decision` skip automatic review with
`review_skipped: "result <status>"`; the saved check still runs. The supervisor's result is objective
evidence for that command; Claude must still read the actual diff and verify the completion conditions.

`wait` names changed guarded files with added/deleted line counts, including uncommitted changes, and
separately flags deleted files and added test-skip markers. Guards cover standard test and CI paths,
existing relative repository paths in top-level check tokens (including `NAME=path` and `--opt=path`),
and saved, repeatable `start --guard "GLOB"` patterns. Discovered paths stay guarded after deletion.
Review prompts list those files and ask whether checks were weakened; read them in full.
Add paths inside `sh -c '…'` through `--guard`, since nested shell commands are not inspected.

For refactoring or optimization that should preserve behavior, add `start --base-check`. After a
passing check, the supervisor reruns the command in a temporary detached worktree at the task's
`HEAD`, with guarded files restored from the base (new files removed, deleted files restored), then
removes that worktree. `wait` shows `== base tests: passed/FAILED` and a log tail. Failure is advisory:
review still runs. The setting persists until `say --no-base-check` disables it.
Use relative paths with `--base-check`: a command containing the task worktree's absolute path
produces `== base tests: skipped (…)` and is not rerun.

## Settings and language

Settings live in `~/.codex/codex-worker.json`. If it does not exist, the worker reads the legacy
`~/.codex/claude-accounts.json`; the new file always takes precedence. `login` writes only the new file.

A minimal configuration is:

```json
{
  "language": "en",
  "switch_at": 99,
  "credits": false,
  "wait_for_reset": true
}
```

| Key | Default | Meaning |
|---|---|---|
| `language` | `"en"` | `"en"` or `"ru"`; selects standing rules and worker-generated scout, handoff, review, fix, and reset-continuation prompts. Claude writes the brief in the user's configured language. |
| `sandbox` | `"off"` | `"off"` or `"workspace"`; limit worker, review, and check writes to the worktree and `writable_dirs`. `start --sandbox` overrides it for the task; `say` inherits it. |
| `writable_dirs` | `[]` | Additional writable directories, such as build caches; `~` is expanded and paths are made absolute. |
| `accounts` | Absent | Explicit account list; its presence enables automatic limit monitoring and rotation. Without it, tasks use the existing `~/.codex` login. |
| `switch_at` | `99` | Used-percentage threshold for configured accounts, greater than 0 and at most 100. |
| `credits` | `false` | Allow continuing on credits if no configured account has ordinary usage available. |
| `wait_for_reset` | `true` | When configured accounts are exhausted, wait for reset and continue; `false` stops instead. |
| `max_turn_minutes` | Absent | Default active implementation exec budget for `start` and `say`; `--max-minutes M` overrides it. Unset means no time cap. |
| `max_review_minutes` | `30` | Separate active exec budget for each review or scout turn. |
| `review_angles_min_lines` | `400` | Changed-line threshold for `--review-angles auto`; counts `base..HEAD` and uncommitted changes. |
| `repeat_limit` | `3` | Matching failures of the same command that trigger `stuck`; success for that command resets its count. |
| `notify` | Absent | `"macos"` for desktop notifications, or an argv list that receives the event JSON as its final argument. |
| `notify_events` | All events | Optional list of event types to send. |

A settings file containing only language or other preferences does **not** enable automatic account
monitoring or rotation. The `accounts` command remains available to inspect the current login.

English standing rules are in `references/rules.md`; Russian rules are in `references/rules.ru.md`.
If `~/.codex/codex-worker-rules.md` exists, it replaces both. Use this file for other languages or your
own working rules. The worker substitutes `{branch}` and `{base}` in the selected rules.
Scouts use their own read-only instructions instead of these task rules. Account status labels are
always English.

`CODEX_TASK_HOME` can select another main Codex home. Settings, the rules override, task state, and
worktrees then resolve under that home.

## Optional workspace sandbox

The sandbox is off by default. To enable it, add these settings to `~/.codex/codex-worker.json`:

```json
{
  "sandbox": "workspace",
  "writable_dirs": ["~/.gradle", "~/.npm"]
}
```

`start --sandbox off|workspace` overrides the setting for one task, and `say` inherits the saved mode.
Workers, fresh and resumed reviews, and saved checks use `workspace-write` with network access.
Scouts remain read-only. Writes outside the worktree are blocked, protecting task state and saved
checks, other repositories, `~/.ssh`, and `~/.codex`. Temporary directories remain writable, and each
entry in `writable_dirs` explicitly grants additional write access. Builds using caches outside the
worktree fail until those cache directories are added. Reading the entire disk, including tokens,
and network access are **not** restricted.

The worker leaves changes in the worktree and returns a `commit_message` whose first line is the
subject. After each completed work turn, including fix rounds and any result status, the supervisor
commits changes before checking them, with Git hooks and fsmonitor disabled. It pushes without force
and creates a PR if `gh` is available and the branch has none. Failed, stopped, or interrupted turns
are not committed. Empty messages use `<slug>: turn <N>`. Push or PR failures do not fail the turn; `wait` shows the publication
result or error and `== sandbox: workspace`. `status` also shows the mode.

Only filters whose `clean`, `smudge`, or `process` commands start with `git-lfs ` or `git lfs ` allow
supervisor publication; otherwise, commit the turn manually. Workspace mode is unsuitable for a
repository whose config references worktree files through `include.path` or `core.attributesFile`.

## Task notifications

Notifications work even when Claude's session has ended. For macOS, add to `codex-worker.json`:

```json
{
  "notify": "macos"
}
```

An argv hook receives one final JSON argument with `event`, `kind`, `slug`, `repo`, `branch`, `account`, `at`,
and a short `message`. Event types are `turn_finished` (`outcome`, `result`, also named in `message`,
and `guarded_changed`, the number of changed guarded files),
`needs_input` (`result` and the first question for `blocked` or `needs_decision`), `paused` (`until`),
`woke`, `rotated` (`from` and `to`), `needs_login`, `reset_credit_used`, `check_failed`, and `review_done`
(`verdict`, finding count, `round`, and `angles`), plus `budget_exhausted`, `stuck`, and `interrupted`.
`turn_finished` is sent when the entire cycle ends, with `outcome` including `budget_exhausted`,
`stuck`, or `interrupted`. If a signal arrives while final notifications are being launched, a
correcting `turn_finished` follows; its outcome supersedes the earlier event. Separate guard and
interruption events also obey `notify_events`. Once final notifications are stable, the outcome is
frozen for the idle-state write; later signals are logged without revising the finished turn.
Use `notify_events` to select the events you want. For example, send the event JSON to an ntfy topic
with `curl`; the appended argument supplies the value for `--data-binary`:

```json
{
  "notify": ["curl", "--fail", "--silent", "--show-error", "https://ntfy.sh/YOUR_PRIVATE_TOPIC", "--data-binary"],
  "notify_events": ["turn_finished", "paused", "check_failed"]
}
```

Hooks run asynchronously in a separate process group with no stdin. Their output and launch errors
go to the task's `notify.log`; a notification failure does not fail the task.

## Multiple subscriptions

> **OpenAI's [Terms of Use](https://openai.com/policies/terms-of-use/) prohibit “circumvent any rate
> limits or restrictions.” Rotation is disabled until you explicitly list `accounts` in the settings.
> Use this feature at your own risk, for one person and only your own subscriptions.**

If you choose to configure it, sign in to each subscription separately:

```sh
python3 -I "$T" login second
```

The human completes the device-code sign-in, or uses `--browser`. Additional account homes default to
`~/.codex-accounts/<name>`. Never copy `auth.json` between homes.

An explicit multi-account configuration looks like this:

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

`start` chooses an available account whose longest usage window resets first; follow-ups keep the
current account while it is available. At the threshold or a usage-limit error, the supervisor opens a
new thread on another configured account in the same worktree. It supplies the brief, follow-ups, and
progress so implementation can continue from existing commits and uncommitted changes.

If every configured account is exhausted, the task can sleep until a reset and continue automatically
(up to eight days of reset waiting). `wait` normally returns the scheduled local time;
`wait --through-sleep` keeps waiting. Set `wait_for_reset` to `false` to stop instead. Two homes signed
into the same ChatGPT account share limits and are not separate subscriptions.

`use_reset_credits` defaults to `false`; enable it to spend one free reset before sleeping when the natural reset is more than `reset_credit_min_hours` (default `12`) away.
`reset-credit <account> [--credit ID]` previews the earliest-expiring reset; add `--yes` to consume it and show the RPC response and refreshed limits.
Authentication errors get one retry, then rotation; `needs login` means run `login <account>` to restore that account.

## Optional desktop-app notifications

For threads created in the Codex desktop app, the bundled `wait-turn.py` can wait for completion
events written by `turn-log.py`. This optional hook is separate from the task notifications above.

To enable that optional hook, set `notify` in your `~/.codex/config.toml` to the installed script's
absolute path. For a manual skill installation, replace `<home>` below with your home directory;
TOML does not expand `~` or shell variables here:

```toml
notify = ["python3", "-I", "<home>/.claude/skills/codex-worker/scripts/turn-log.py"]
```

For a plugin installation, use the absolute path to `scripts/turn-log.py` inside the installed skill.
Claude can resolve it as `${CLAUDE_SKILL_DIR}/scripts/turn-log.py`; the config needs the expanded path.
Preserve any existing notify configuration so you can restore it when removing the hook.

Codex supplies the JSON event argument. The hook appends events to `~/.codex/turn-events.jsonl` and
chains to Codex Computer Use only if its client exists. It does not automatically preserve arbitrary
other notify hooks.

```sh
SCRIPTS="$HOME/.claude/skills/codex-worker/scripts"
python3 -I "$SCRIPTS/wait-turn.py" <thread-id>
python3 -I "$SCRIPTS/codex-stats.py" <thread-id>
```

Run `wait-turn.py` in the background from Claude Code. Desktop threads belong to the app; the worker's
`say` and `exec resume` cannot take over its active writer. A CLI that supports `codex queue` can queue
a message for an open app thread; successful queueing means stored, so check the rollout for delivery.
On macOS, that command may be available from the ChatGPT.app bundle listed above.

## Limitations

- Claude must review the diff and verify completion conditions. A Codex report or first review is not
  proof that the change is correct.
- The worker relies on Codex CLI commands, event formats, and account RPCs; compatibility can change
  with CLI versions. Desktop queueing and Computer Use integration depend on installed app support.
- Codex uses local tools and your existing permissions. Keep task boundaries explicit, and delegate
  only access and actions you intend to authorize.
- Automatic account monitoring and reset handling require explicit `accounts`; single-login use has
  no automatic rotation or reset supervisor.
- PR creation needs `gh` support for the remote. Otherwise Codex is instructed to push the branch and
  report its name.
- Cleanup removes the worktree and task logs. Review and merge before running `clean`.
- Windows is not supported; process supervision uses Unix process groups and signals.

## Tests

Run the CLI integration tests with Python 3.9 or newer, using only the standard library:

```sh
python3 -I tests/test_rotation.py
```

The full suite runs in parallel processes by default, distributing tests dynamically according to
the available CPU count. Set `CODEX_TEST_JOBS` to choose the number of processes;
`CODEX_TEST_JOBS=1 python3 -I tests/test_rotation.py` runs sequentially in the current process.
Passing test names keeps the usual unittest behavior, for example:

```sh
python3 -I tests/test_rotation.py RotationTests.test_mid_turn_rotation_preserves_work_and_followups
```

## License

[MIT](LICENSE), copyright 2026 kaifaty.

