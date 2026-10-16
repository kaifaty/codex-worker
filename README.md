# codex-worker

Let Claude plan and review while Codex implements in its own git worktree. This Claude Code plugin
moves substantial coding work to your ChatGPT subscription, saving Claude subscription usage for the
parts where you want Claude's judgment.

Claude writes a bounded brief, starts Codex, and waits in the background. Codex commits and opens a PR.
A fresh Codex thread can provide the first review; Claude checks the diff, requests fixes, and handles
the final review and merge. Optional account monitoring can pause a task until a usage window resets.

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
python3 -I "$T" start csv-export --brief brief.md --review
python3 -I "$T" wait csv-export
python3 -I "$T" status csv-export
```

Use a brief with the change, context, measurable completion conditions, focused checks, and boundaries.
The worker creates `codex/csv-export` and an isolated worktree, implements the task, and runs the first
review in a fresh thread. In Claude Code, `wait` runs as a background Bash command so Claude can resume
when the turn ends.

Check the report and the actual diff. If there are issues, send one follow-up and wait again:

```sh
python3 -I "$T" say csv-export --file fixes.md --review
python3 -I "$T" wait csv-export
```

Codex does not merge. After the result has been reviewed and merged under your repository's rules,
remove the worktree and archive its threads:

```sh
python3 -I "$T" clean csv-export
```

## Commands

Run all commands as `python3 -I "$T" <command>`.

| Command | Purpose |
|---|---|
| `start <slug> --brief FILE [--repo DIR] [--base origin/BRANCH] [--effort E] [--review]` | Create a worktree and start implementation; `--brief -` reads stdin. |
| `wait <slug> [--timeout SEC] [--through-sleep]` | Wait for the turn, show its report and changes; optionally wait through limit resets. |
| `say <slug> "message"` or `say <slug> --file FILE` | Follow up while idle; add `--review` for a fresh review afterward. |
| `review <slug> [--effort E]` | Run the first review in a fresh Codex thread while the task is idle. |
| `status [<slug>]` | Show task state, account, thread, PR, commits, and last message. |
| `stats <slug>` | Show per-turn runtime, tool calls, tokens, and available usage estimates. |
| `accounts` | Show login, plan, limits, reset times, credits, and availability in English. |
| `login <name> [--browser]` | Have the human sign in to an account; writes `codex-worker.json`. |
| `stop <slug>` | Interrupt the task; send the next instructions with `say`. |
| `clean <slug> [--keep-branch]` | Remove the worktree and task logs and archive threads; optionally retain the local branch. |

Tasks use `~/.codex/worktrees/claude-<slug>/<repo>`, with state and logs under
`~/.codex/claude-tasks/<slug>/`. Turns continue independently of Claude's session. Only one task runs at
a time unless `start --parallel` is explicitly used for tasks whose builds and tests cannot interfere.
The default base is the remote's default branch.

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
| `language` | `"en"` | `"en"` or `"ru"`; selects standing rules and worker-generated handoff, review, and reset-continuation prompts. Claude writes the brief in the user's configured language. |
| `accounts` | Absent | Explicit account list; its presence enables automatic limit monitoring and rotation. Without it, tasks use the existing `~/.codex` login. |
| `switch_at` | `99` | Used-percentage threshold for configured accounts, greater than 0 and at most 100. |
| `credits` | `false` | Allow continuing on credits if no configured account has ordinary usage available. |
| `wait_for_reset` | `true` | When configured accounts are exhausted, wait for reset and continue; `false` stops instead. |

A settings file containing only language or other preferences does **not** enable automatic account
monitoring or rotation. The `accounts` command remains available to inspect the current login.

English standing rules are in `references/rules.md`; Russian rules are in `references/rules.ru.md`.
If `~/.codex/codex-worker-rules.md` exists, it replaces both. Use this file for other languages or your
own working rules. The worker substitutes `{branch}` and `{base}` in the selected rules.
Account status labels are always English.

`CODEX_TASK_HOME` can select another main Codex home. Settings, the rules override, task state, and
worktrees then resolve under that home.

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

## Optional desktop-app notifications

Tasks started by the worker do not need a notify hook. For threads created in the Codex desktop app,
the bundled `wait-turn.py` can wait for completion events written by `turn-log.py`.

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
- Concurrent processes sharing an account home can race during credential refresh. Reauthenticate
  with `login <name>` if needed.
- Automatic account monitoring and reset handling require explicit `accounts`; single-login use has
  no automatic rotation or reset supervisor.
- PR creation needs `gh` support for the remote. Otherwise Codex is instructed to push the branch and
  report its name.
- Cleanup removes the worktree and task logs. Review and merge before running `clean`.
- Windows is not supported; process supervision uses Unix process groups and signals.

## License

[MIT](LICENSE), copyright 2026 kaifaty.

