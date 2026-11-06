# Settings

Settings live in `config.json` in the [data home](#data-home) (`~/.teamster`). A legacy home keeps them in
`~/.codex/codex-worker.json` and reads the old `~/.codex/claude-accounts.json` only when that file is absent.

| Key | Default | Effect |
|---|---|---|
| `language` | `"en"` | brief language (`"ru"` for Russian) and the language of the scout, handoff, review, fix, wrap-up and reset-continuation prompts the script writes; account status output is always English |
| `max_turn_minutes` | none | `--max-minutes` default for implementation turns; with neither set, a turn has no time cap |
| `max_review_minutes` | `30` | cap for reviews and scouts, separate from implementation time |
| `reviewer` | `"codex"` | default review provider: `"codex"` or `"claude"`; saved per task and overridden by `--reviewer` |
| `max_task_weekly_percent` | none | task weekly budget in percentage points (0 < P ≤ 100); `start --max-weekly P` overrides it, and `say` and `review` inherit or replace the saved task budget |
| `repeat_limit` | `3` | identical failures before a `stuck` stop ([results.md](results.md)) |
| `capacity_retry_minutes` | `[1, 5, 15, 30]` | pauses in minutes (positive numbers) before each continuation after a model `at capacity` error in one turn; an error after the last pause fails the turn ([accounts.md](accounts.md#sign-in-errors)) |
| `review_angles_min_lines` | `400` | changed lines from which `--review-angles auto` runs angles |
| `ci` | `false` | initial task CI setting; see [Task CI options](#task-ci-options) |
| `ci_timeout_minutes` | `30` | maximum wait for CI checks or a CI command per attempt |
| `sandbox`, `writable_dirs` | `"off"`, `[]` | [sandbox.md](sandbox.md) |
| `accounts`, `switch_at`, `credits`, `wait_for_reset`, `use_reset_credits`, `reset_credit_min_hours` | | [accounts.md](accounts.md) |
| `credits_floor` | `0` | finite number ≥ 0; with `credits: true`, an account runs on credits only while its balance is above it; see [accounts.md](accounts.md#credits) |
| `credits_floor_until` | none | `YYYY-MM-DD`; the floor applies through the end of this local date and is `0` from the next day |
| `soft_switch_at` | value of `switch_at` | number in `(0, switch_at]`; prefer accounts below this usage percentage in both windows, then use the reserve up to `switch_at`; see [accounts.md](accounts.md#rotation) |
| `notify`, `notify_events` | | [notifications.md](notifications.md) |
| `duty.relative_margin` | `25` | finite number ≥ 0; the duty account moves within a family only to a room at least this many percent larger ([accounts.md](accounts.md#duty-account)) |
| `duty.absolute_margin` | `100` | finite number ≥ 0; the same move also needs a room at least this many units larger |
| `duty.claude_only_task_types` | `["visual", "layout"]` | task types (`--task-type`) that a Claude duty account still takes when it is the only Claude account left |

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
| `duty` | Contents of `~/.teamster/duty.json` (`account`, `family`, `since`, `checked_at`, `reason`, `rooms` by family), or `null` without a readable duty record ([accounts.md](accounts.md#duty-account)) |

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
| `max_turns` | `40` | positive integer passed as Claude review `--max-turns`, shared by all accounts; task work is uncapped |
| `work_switch_at` | `85` | finite number in `[0, 100]`; Claude work uses an account only while both windows stay below it, keeping the rest of each week for reviews; see [accounts.md](accounts.md#claude-account-pool) |

The top-level `soft_switch_at` and `switch_at` thresholds also apply to Claude's five-hour and
seven-day windows; Claude work uses `min(work_switch_at, soft_switch_at)` and then `work_switch_at`. Below-soft accounts are preferred, then below-hard accounts, with the earliest
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

`start` appends `src/codex_worker/rules.md` or `src/codex_worker/rules.ru.md` to the brief, following `language`.
`~/.teamster/rules.md`, if present, replaces either file: use it for another language or your
own rules. `{branch}` and `{base}` in it are replaced with the task's branch and PR target. Scouts use
their own read-only instructions instead of task rules.
With `--check`, `start` appends a rule to run the saved check in full before finishing with status `done`, including when a custom rules file is used.

## Data home

The tool keeps its own files in a data home, apart from Codex's home. Codex's home is `~/.codex`, or
`CODEX_TASK_HOME` when set; it keeps `config.toml`, sessions and the default `main` login, with
`~/.codex-accounts` next to it. The data home is the first match:

1. `TEAMSTER_HOME` is set: that directory, new layout. Codex's home is `CODEX_TASK_HOME` if set, else `~/.codex`.
2. `CODEX_TASK_HOME` is set: if it contains the file `teamster-home`, the data home is the path written
   in that file, new layout; otherwise it is `CODEX_TASK_HOME` itself, legacy layout.
3. Neither is set: `~/.teamster` if it exists, new layout. Otherwise `~/.codex` in the legacy layout if
   it holds `claude-tasks/` or `codex-worker.json`, or while an unfinished `~/.teamster.migrating`
   exists. Otherwise `~/.teamster`, new layout.

| Purpose | Legacy layout | New layout |
|---|---|---|
| Task records | `claude-tasks/` | `tasks/` |
| Worktrees | `worktrees/` | `worktrees/` |
| Settings | `codex-worker.json`, read fallback `claude-accounts.json` | `config.json` |
| Settings lock | `codex-worker.lock` | `config.lock` |
| Telemetry journal | `codex-worker-log.jsonl` | `log.jsonl` |
| Reset journal | `codex-worker-resets.jsonl` | `resets.jsonl` |
| Rules override | `codex-worker-rules.md` | `rules.md` |
| Duty | `duty.json`, `duty-log.jsonl`, `duty.lock`, `duty-launchd.log` | same names |
| Board | `board-machines.json`, `board-server.log` | same names |

The documents name paths in the new layout under `~/.teamster`; a legacy home has the legacy names in
`~/.codex`. `duty plist` and `codex-worker-board plist` write `TEAMSTER_HOME` for a new-layout home
(and `CODEX_TASK_HOME` when Codex's home is not `~/.codex`), and `CODEX_TASK_HOME` for a legacy one.

### migrate-home

`codex-worker migrate-home` prints the plan and changes nothing; `codex-worker migrate-home --apply`
moves the legacy data from `~/.codex` to `~/.teamster`. It refuses, before it changes anything and
with a nonzero exit, when `TEAMSTER_HOME` or `CODEX_TASK_HOME` is set, when the data is already in the
new layout, when a task or scout supervisor is alive (sleeping and waiting tasks included; it names
them), when `~/.teamster` exists and is not empty, when `~/.teamster` exists and is empty, and when
`~/.teamster` and `~/.codex` are on different devices. An empty `~/.teamster` is refused with a request
to `rmdir` it: while the directory exists, commands already treat it as the data home (rule 3) and
lock it with its own `config.lock`, which the migration does not hold.

The move uses only `os.rename`, never a copy. It builds `~/.teamster.migrating`, writes
`migration.json` there with every planned pair, renames each present item into it under its new
name, renames the directory to `~/.teamster`, and finally writes `~/.codex/teamster-home` with the
new path, so launchd jobs that still set `CODEX_TASK_HOME=~/.codex` follow the move. Until the
directory rename the tool keeps resolving to the legacy home. Running `--apply` again after a crash
at any step resumes from the saved plan and finishes.

Worktrees never move: git worktree metadata and task state hold absolute paths through
`~/.codex/worktrees`. `~/.teamster/worktrees` becomes a symlink to the resolved real path of
`~/.codex/worktrees` (which may itself be a symlink to another disk), and the old entry stays. Also
left in place: `.bak-*` files, `~/.codex/checks`, `~/.codex/briefs`, `~/.codex-accounts`, every Codex
file, and the lock files `codex-worker.lock` and `duty.lock`.

Commands that write task records, journals or cached limits (`start`, `scout`, `say`, `review`,
`clean`, `verdict`, `accounts`, `reset-credit`, `claude-probe`) hold the settings lock shared for their
whole run; `login` and the `claude-*` account
commands hold it exclusively. `migrate-home --apply` holds the legacy settings lock and `duty.lock`
exclusively from its final checks until the marker is written. The legacy lock files are never moved,
so a command that starts meanwhile waits for the same lock. Once granted, a command resolves its data
home again and stops if the home moved or a migration is unfinished; run it again.

After the move, restart the launchd jobs with
`launchctl kickstart -k gui/$(id -u)/codex-worker.board` and
`launchctl kickstart -k gui/$(id -u)/codex-worker.duty`, and regenerate the plists you printed. The
tool never runs `launchctl` itself.

### Rolling back

With no task running and the launchd jobs stopped. Clean tasks started after the migration first:
their worktree paths go through `~/.teamster/worktrees`, which the rollback removes.

1. `rm ~/.codex/teamster-home`
2. Move every recorded pair back:
   `python3 -c 'import json, os; [os.rename(p["to"], p["from"]) for p in json.load(open(os.path.expanduser("~/.teamster/migration.json")))["moved"]]'`
3. `rm ~/.teamster/worktrees ~/.teamster/migration.json` (the symlink only; the worktrees are still in
   `~/.codex/worktrees`), then `rm -f ~/.teamster/*.lock` and `rmdir ~/.teamster`. If `rmdir` fails,
   the remaining files were created after the migration; move each to its legacy name from the table
   above first.
4. Restart the launchd jobs and regenerate their plists; they set `CODEX_TASK_HOME` again.

## Repository trust

`codex exec` trusts the repository's main checkout in `config.toml` (`[projects."<repo>"] trust_level`),
not each worktree. A first task in a new repository adds one entry.
