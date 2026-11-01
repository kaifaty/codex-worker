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
| `credits_floor` | `0` | finite number ≥ 0; with `credits: true`, an account runs on credits only while its balance is above it; see [accounts.md](accounts.md#credits) |
| `credits_floor_until` | none | `YYYY-MM-DD`; the floor applies through the end of this local date and is `0` from the next day |
| `soft_switch_at` | value of `switch_at` | number in `(0, switch_at]`; prefer accounts below this usage percentage in both windows, then use the reserve up to `switch_at`; see [accounts.md](accounts.md#rotation) |
| `notify`, `notify_events` | | [notifications.md](notifications.md) |

## Task models

`start --model M` saves a Codex model for the task's implementation, fixes, and wrap-up turns.
`say` inherits it; `say --model M` replaces it, and `say --model default` clears it so Codex uses
its configured model again. `status` and `wait` show a saved task model beside the latest observed
reasoning effort, falling back to the task's effort override or `default` when not yet known.
`--effort E` continues to control reasoning separately.

`start`, `say`, and `review` accept `--review-model M` for Codex reviews, including each review angle.
It is saved separately from the work model; follow-ups and later reviews inherit it. Passing
`--review-model default` clears it. Without a review override, Codex uses its existing review defaults.
Claude reviews continue to use `claude.model`.

Model names must match `[A-Za-z0-9._-]{1,64}`. The worker checks the name's format, not whether the
model exists or is available to an account. `default` is reserved for clearing an override.

## Account JSON

`accounts --json` emits one JSON object. Plain `accounts` retains its text table and Claude lines.
All timestamps below are UTC ISO 8601 strings; unknown values are `null`.

| Field | Contents |
|---|---|
| `codex` | List of account objects with `name`, `plan`, `primary`, `secondary`, `credits`, `reset_credits`, and `status` |
| `codex[].primary`, `codex[].secondary` | Window object with `used_percent`, `window_minutes`, and `resets_at`, or `null` when unknown |
| `codex[].credits` | Object with `balance`, `has_credits`, `unlimited`, and `spendable` (balance minus the active floor, at least 0); unknown fields are `null` |
| `codex[].reset_credits` | Object with `available_count` and `expires_at` (list of expiry timestamps); unknown fields are `null` |
| `codex[].status` | `available`, `exhausted`, `duplicate`, `not_logged_in`, or `null` when unknown |
| `claude` | List of pool accounts with `name`, `five_hour`, `seven_day`, `limited_until`, and `token_unavailable` |
| `claude[].five_hour`, `claude[].seven_day` | Window object with `used_percent` and `resets_at`, or `null` when unknown |
| `claude[].limited_until` | Active account cooldown timestamp, or `null` |
| `claude[].token_unavailable` | `true` while a missing Keychain token excludes the account (one hour from the failure), otherwise `null` |
| `thresholds` | Object with configured `soft_switch_at` and `switch_at` used-percentage thresholds |
| `credits_floor` | Active credit floor: the configured value, or `0` after `credits_floor_until` |
| `credits_floor_until` | Configured end date of the floor (`YYYY-MM-DD`), or `null` |

The JSON includes no tokens, credential paths, or raw account errors.

## Claude accounts

`claude.accounts` configures a pool of isolated Claude Code CLI accounts without assigned roles.
`--reviewer claude` selects from it before each review or angle; work and fix rounds still run in
Codex. Settings belong in the `claude` object:

```json
{
  "reviewer": "claude",
  "claude": {
    "accounts": [
      {"name": "first", "config_dir": "~/.claude-accounts/first", "token_keychain": "codex-worker-claude-first"},
      {"name": "second", "config_dir": "~/.claude-accounts/second", "token_keychain": "codex-worker-claude-second"}
    ],
    "max_turns": 40
  }
}
```

| `claude` key | Default | Effect |
|---|---|---|
| `accounts` | legacy account named `default` | nonempty list of accounts; each has a unique `name` using only `[a-z0-9-]`, an isolated `config_dir` and a `token_keychain` service for `$USER` |
| `config_dir` | `"~/.claude-accounts/reviewer"` | legacy isolated `CLAUDE_CONFIG_DIR`, used when `accounts` is absent |
| `token_keychain` | `"codex-worker-claude-reviewer"` | legacy macOS Keychain service, used when `accounts` is absent |
| `model` | omitted | optional Claude `--model`, shared by all accounts |
| `effort` | omitted | optional Claude `--effort`, shared by all accounts and independent of Codex `--effort` |
| `max_turns` | `40` | positive integer passed as Claude `--max-turns`, shared by all accounts |

The top-level `soft_switch_at` and `switch_at` thresholds also apply to Claude's five-hour and
seven-day windows. Below-soft accounts are preferred, then below-hard accounts, with the earliest
seven-day reset first in each group. Unknown limits count as available. Legacy settings, including
a missing `claude` object, remain a one-account pool named `default`.

The executable is `CODEX_TASK_CLAUDE`, or `claude` on `PATH`. `start` and `review` fail before
launching when Claude is selected but its CLI is missing. See [accounts.md](accounts.md#claude-account-pool)
for `claude-login` and token setup, and [review-cycle.md](review-cycle.md#review-provider) for retries
and fallback behavior.

## Task CI options

`start --no-push` keeps commits local through `say`, reviews, fixes and wrap-up; the supervisor skips branch and PR publication (`no-push`), and `wait`/`status` show push disabled.
It disables the default `ci` setting; explicit `--ci` or `--ci-command CMD` on `start` or `say` is an error for this task. Local `--check` still runs.

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
With `--check`, `start` appends a rule to run the saved check in full before finishing with status `done`, including when a custom rules file is used.

## Repository trust

`codex exec` trusts the repository's main checkout in `config.toml` (`[projects."<repo>"] trust_level`),
not each worktree. A first task in a new repository adds one entry.
