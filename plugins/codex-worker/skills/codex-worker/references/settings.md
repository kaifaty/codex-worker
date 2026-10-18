# Settings

Settings live in `~/.codex/codex-worker.json`. The old `~/.codex/claude-accounts.json` is read only when
that file is absent.

| Key | Default | Effect |
|---|---|---|
| `language` | `"en"` | brief language (`"ru"` for Russian) and the language of the scout, handoff, review, fix, wrap-up and reset-continuation prompts the script writes; account status output is always English |
| `max_turn_minutes` | none | `--max-minutes` default for implementation turns; with neither set, a turn has no time cap |
| `max_review_minutes` | `30` | cap for reviews and scouts, separate from implementation time |
| `max_task_weekly_percent` | none | task weekly budget in percentage points (0 < P ≤ 100); `start --max-weekly P` overrides it, and `say` and `review` inherit or replace the saved task budget |
| `repeat_limit` | `3` | identical failures before a `stuck` stop ([results.md](results.md)) |
| `review_angles_min_lines` | `400` | changed lines from which `--review-angles auto` runs angles |
| `ci` | `false` | wait for GitHub PR checks after the automatic cycle; `start/say --ci` enables it for the task |
| `ci_timeout_minutes` | `30` | maximum wait for PR checks per CI attempt; `--ci-rounds N` defaults to one repair turn |
| `sandbox`, `writable_dirs` | `"off"`, `[]` | [sandbox.md](sandbox.md) |
| `accounts`, `switch_at`, `credits`, `wait_for_reset`, `use_reset_credits`, `reset_credit_min_hours` | | [accounts.md](accounts.md) |
| `notify`, `notify_events` | | [notifications.md](notifications.md) |

## Standing rules

`start` appends `references/rules.md` or `references/rules.ru.md` to the brief, following `language`.
`~/.codex/codex-worker-rules.md`, if present, replaces either file: use it for another language or your
own rules. `{branch}` and `{base}` in it are replaced with the task's branch and PR target. Scouts use
their own read-only instructions instead of task rules.

## Repository trust

`codex exec` trusts the repository's main checkout in `config.toml` (`[projects."<repo>"] trust_level`),
not each worktree. A first task in a new repository adds one entry.
