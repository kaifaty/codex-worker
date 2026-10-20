# Multiple subscriptions, reset waits and reset credits

> OpenAI's [Terms of Use](https://openai.com/policies/terms-of-use/) prohibit “circumvent any rate limits
> or restrictions.” Rotation is off until the user explicitly lists `accounts` in the settings. Using it
> is at the user's own risk, for one person and only their own subscriptions.

## One login (the default)

Without `accounts` in `~/.codex/codex-worker.json`, tasks use the existing `main` login in `~/.codex`;
automatic limit monitoring and rotation are disabled. A language-only settings file does not enable
them.

## Adding a subscription

For another subscription, have the user run `python3 -I "$T" login second`. Its separate `CODEX_HOME`
is `~/.codex-accounts/second`, and login writes the new `~/.codex/codex-worker.json`. Never copy
`auth.json` between homes: each subscription needs its own sign-in. When the user has chosen multiple
subscriptions, the settings can contain:

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

## Claude reviewer account

`--reviewer claude` reviews through a separate Anthropic subscription. Install Claude Code CLI, then
have the user create a token while signed in to the reviewer subscription in an isolated config:

```sh
env -i HOME="$HOME" USER="$USER" PATH="$PATH" LANG=en_US.UTF-8 \
  CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/reviewer" claude setup-token
security add-generic-password -U -s codex-worker-claude-reviewer -a "$USER" -w
```

Paste the issued token at the Keychain password prompt. The supervisor reads it with
`security find-generic-password` immediately before each review and passes it only through the child
environment. Do not put it in settings, command arguments, or task files. The `claude.config_dir` and
`claude.token_keychain` settings replace these defaults; see [settings.md](settings.md#claude-reviewer).

`accounts` adds a `claude reviewer` line with utilization percentages and local reset times for the
five-hour and seven-day windows from the last Claude rate-limit event. Before the first event, it
prints `unknown`. The cache is `~/.codex/claude-tasks/claude-limits.json`. Claude review costs and
tokens appear separately in telemetry and `report`.

## Rotation

With `accounts` configured, `start` chooses an available account whose longest window resets first;
`say` keeps the current account while it is available. A detached supervisor watches limits and switches
accounts at the `switch_at` threshold or on a usage-limit error. The next account opens a new thread in
the same worktree, with the brief, follow-ups and current progress; commits and uncommitted changes are
the starting point. If no other account is available, it continues on credits only when `credits` is
true. Use `accounts` to inspect availability; two homes signed into the same ChatGPT account share
limits.

## Waiting for a reset

If all configured accounts are exhausted, the task waits and continues automatically after a reset (up
to 8 days); set `wait_for_reset` to false to pause and exit instead. When `wait` returns with
`auto-continue at`, tell the user that local time; run `wait` again later to follow, or use
`wait --through-sleep` to keep waiting.

## Reset credits

`use_reset_credits` defaults to `false`; enable it to spend one free reset before sleeping when the
natural reset is more than `reset_credit_min_hours` (default `12`) away.
`reset-credit <account> [--credit ID]` previews the earliest-expiring reset; add `--yes` to consume it
and show the RPC response and refreshed limits.

## Sign-in errors

Authentication errors get one retry, then rotation. When `wait` says `needs login`, have the user run
`login <account>`.

When a `turn.failed` or `error` event contains `at capacity` (case-insensitive), the supervisor waits
60 seconds and retries once on the same account. It resumes the existing thread with a short
continuation prompt, or starts a new exec with the original prompt if no thread was created.
This also works without configured accounts, and each review or fix turn gets its own retry.
`stop` and signals interrupt the pause. Active time and weekly budgets span both attempts; the pause
does not count as active exec time. A second capacity error fails the turn. `wait` shows
`== retried: model at capacity`; telemetry and `report` record `capacity_retries` (0 for older rows).
`CODEX_TASK_CAPACITY_RETRY_SECONDS` overrides the pause for tests.
