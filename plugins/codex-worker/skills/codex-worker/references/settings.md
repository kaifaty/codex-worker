# Settings

Settings live in `~/.codex/codex-worker.json`. The old `~/.codex/claude-accounts.json` is read only when
that file is absent.

| Key | Default | Effect |
|---|---|---|
| `language` | `"en"` | brief language (`"ru"` for Russian) and the language of the scout, handoff, review, fix, wrap-up and reset-continuation prompts the script writes; account status output is always English |
| `max_turn_minutes` | none | `--max-minutes` default for implementation turns; with neither set, a turn has no time cap |
| `max_review_minutes` | `30` | cap for reviews and scouts, separate from implementation time |
| `reviewer` | `"codex"` | default review provider: `"codex"` or `"claude"`; saved per task and overridden by `--reviewer` |
| `max_task_weekly_percent` | none | task weekly budget in percentage points (0 < P ≤ 100); `start --max-weekly P` overrides it, and `say` and `review` inherit or replace the saved task budget |
| `repeat_limit` | `3` | identical failures before a `stuck` stop ([results.md](results.md)) |
| `review_angles_min_lines` | `400` | changed lines from which `--review-angles auto` runs angles |
| `ci` | `false` | initial task CI setting; see [Task CI options](#task-ci-options) |
| `ci_timeout_minutes` | `30` | maximum wait for CI checks or a CI command per attempt |
| `sandbox`, `writable_dirs` | `"off"`, `[]` | [sandbox.md](sandbox.md) |
| `accounts`, `switch_at`, `credits`, `wait_for_reset`, `use_reset_credits`, `reset_credit_min_hours` | | [accounts.md](accounts.md) |
| `soft_switch_at` | value of `switch_at` | number in `(0, switch_at]`; prefer accounts below this usage percentage in both windows, then use the reserve up to `switch_at`; see [accounts.md](accounts.md#rotation) |
| `notify`, `notify_events` | | [notifications.md](notifications.md) |

## Claude reviewer

`--reviewer claude` uses an isolated Claude Code CLI account for reviews. Work and fix rounds still
run in Codex. Settings belong in the `claude` object:

```json
{
  "reviewer": "claude",
  "claude": {
    "config_dir": "~/.claude-accounts/reviewer",
    "token_keychain": "codex-worker-claude-reviewer",
    "max_turns": 40
  }
}
```

| `claude` key | Default | Effect |
|---|---|---|
| `config_dir` | `"~/.claude-accounts/reviewer"` | isolated `CLAUDE_CONFIG_DIR` |
| `token_keychain` | `"codex-worker-claude-reviewer"` | macOS Keychain service; account is `$USER` |
| `model` | omitted | optional Claude `--model` |
| `effort` | omitted | optional Claude `--effort`; independent of Codex `--effort` |
| `max_turns` | `40` | positive integer passed as Claude `--max-turns` |

The executable is `CODEX_TASK_CLAUDE`, or `claude` on `PATH`. `start` and `review` fail before
launching when Claude is selected but its CLI is missing. See [accounts.md](accounts.md#claude-reviewer-account)
for token setup and [review-cycle.md](review-cycle.md#review-provider) for fallback behavior.

## Task CI options

`start --ci` or `"ci": true` in the settings enables GitHub PR check waiting for the task. The saved
setting persists across follow-ups: `say` without CI flags preserves it, `say --ci` enables it, and
`say --no-ci` disables it. `--ci` and `--no-ci` are mutually exclusive on `say`.

`start --ci-command CMD` or `say --ci-command CMD` saves a shell command and enables the same CI cycle
for other hosts. It replaces GitHub check waiting; `say` inherits it unless replaced, and
`say --no-ci-command` removes it without disabling CI. The two command flags are mutually exclusive;
`--no-ci` disables the cycle while retaining the saved command.

`start --ci-rounds N` sets the maximum repair turns per CI cycle (a nonnegative integer, default 1).
`say --ci-rounds N` replaces the saved limit; omitting it preserves the limit. Changing the round limit
does not enable CI. Waiting and repair mechanics are in [ci.md](ci.md).

## Standing rules

`start` appends `references/rules.md` or `references/rules.ru.md` to the brief, following `language`.
`~/.codex/codex-worker-rules.md`, if present, replaces either file: use it for another language or your
own rules. `{branch}` and `{base}` in it are replaced with the task's branch and PR target. Scouts use
their own read-only instructions instead of task rules.

## Repository trust

`codex exec` trusts the repository's main checkout in `config.toml` (`[projects."<repo>"] trust_level`),
not each worktree. A first task in a new repository adds one entry.
