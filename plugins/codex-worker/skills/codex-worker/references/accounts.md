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
  "soft_switch_at": 80,
  "credits": false,
  "wait_for_reset": true,
  "accounts": [
    {"name": "main", "home": "~/.codex"},
    {"name": "second", "home": "~/.codex-accounts/second"}
  ]
}
```

## Claude account pool

`claude.accounts` is a pool of isolated Claude Code CLI accounts without assigned roles.
`start --executor claude` runs work, follow-ups, review fixes, CI repairs and wrap-up through this
pool. `--executor codex` uses Codex; the default comes from `executor` in `codex-worker.json`,
otherwise `codex`. The executor is saved and cannot change during a task. Without an explicit
`--reviewer`, Claude tasks use Codex reviews and Codex tasks keep the configured reviewer default.
With `--reviewer claude`, reviews prefer an available account other than the Claude worker account.
Install Claude Code CLI, then have the human add each Anthropic subscription:

```sh
python3 -I "$T" claude-login first
python3 -I "$T" claude-login second
```

`claude-login <name> [--config-dir DIR] [--keychain SERVICE]` runs `claude setup-token` in a cleaned
environment and an isolated `CLAUDE_CONFIG_DIR`, defaulting to `~/.claude-accounts/<name>`. The human
completes sign-in, then pastes the issued token at the Keychain password prompt. The command invokes
`security add-generic-password -U -s <service> -a "$USER" -w`; the service defaults to
`codex-worker-claude-<name>`. On success it appends the account to `claude.accounts` in
`~/.codex/codex-worker.json`. Names contain only lowercase letters, digits and hyphens and must be
unique. Re-running the command for an existing name refreshes its login instead of duplicating it;
explicit path or service overrides update that entry after successful setup. Adding to legacy settings
preserves their account as `default`, including when its paths come entirely from defaults.
Concurrent logins merge their account entries into the latest settings after sign-in finishes.
Both sign-in and the Keychain prompt use the human's terminal, opened by its device path from
stdin, stdout or stderr (`/dev/tty` only as a fallback: macOS kqueue rejects it, and the Claude CLI
watches its input with kqueue);
the plugin does not copy the issued token into its own output, arguments, settings, state, or logs.

After saving, `claude-login` runs the same check as `claude-probe <name>`. A failed probe prints its
reason and exits nonzero; the account remains saved. Signing in to an existing account updates its
login fields and preserves the other account settings, including `weight`.

`claude-probe <name>` sends one request, `Reply with the single word OK.`, with `claude.model`,
one maximum turn, no tools and `--permission-mode dontAsk`. It uses the same cleaned environment
and Keychain token lookup as reviews, with stdin and stderr sent to `/dev/null` and a fresh temporary
working directory removed afterward. Rate-limit events update the account's cached windows. It prints
one line, `Claude account <name>: token ok; 5h …; 7d …`, using the same window format as `accounts`,
or one of `token unavailable`, `authentication failed`, `usage limit`, or `request failed`. Only a
successful response exits zero. Tokens never enter arguments, plugin output, settings, state or logs.

`claude-rename <old> <new>` changes the name in `claude.accounts` and the cached limits key, keeping
`config_dir` and `token_keychain` unchanged. The old name must exist; the new name must be unused
and contain only lowercase letters, digits and hyphens. Legacy settings are first materialized as the
`default` pool entry, as with `claude-login`.

`claude-remove <name>` removes the pool entry and cached limits. It leaves the config directory and
Keychain entry intact and prints their locations for manual deletion. The last pool account cannot
be removed; switch `reviewer` to `codex` instead.

For example:

```json
{
  "claude": {
    "limits_max_age_minutes": 30,
    "accounts": [
      {"name": "first", "config_dir": "~/.claude-accounts/first", "token_keychain": "codex-worker-claude-first", "weight": 5},
      {"name": "second", "config_dir": "~/.claude-accounts/second", "token_keychain": "codex-worker-claude-second", "weight": 20}
    ]
  }
}
```

The legacy `claude.config_dir` and `claude.token_keychain` settings still form a one-account pool
named `default`. With neither configured, that account retains `~/.claude-accounts/reviewer` and
`codex-worker-claude-reviewer`. Model and effort are shared settings under `claude`;
see [settings.md](settings.md#claude-accounts). `claude.max_turns` limits reviews only,
while task work has no tool-round cap. For work, task `--model` and `--effort` take
precedence over `claude.model` and `claude.effort`; supported work efforts are `low`, `medium`,
`high`, `xhigh` and `max`. Claude work supports only `--sandbox off`; `workspace` is rejected
before launch. A task effort of `max` leaves Codex reviews at their configured effort, since
`max` is specific to Claude. The selected account and session are kept across work turns while
that account is available. If it is unavailable for work when a turn starts, the turn moves to
another pool account with the handoff below (`wait` prints the reason `unavailable for work`), or
sleeps when the whole pool is unavailable.

When a work attempt ends with `usage limit`, `authentication failed` or `token unavailable`, the
account is marked unavailable and the attempt is repeated on another pool account not yet tried in
this turn. That account starts a new session with the same handoff Codex gets on rotation: the
brief, the current message, the branch's `git log` and `git status`, and the summaries and
`remaining` items of the last three Claude results. The task's session id is replaced by the new
one, and `wait` prints `== rotated <a> → <b>: <reason>, new thread <session>`.

When no other account is available, the task sleeps until the earliest of the pool's limit ends
(`limited_until`, or the rejected reset) and, for accounts without an active limit, their window
resets, plus the reset margin. Sleep uses the same `sleeping` state, `all accounts at limit;
auto-continue at ...` line, `wait --through-sleep`, `stop` and `say` behavior and 8-day cap as
Codex, and `wait_for_reset: false` pauses with `exhausted` instead. After waking it prefers the
task's account and resumes its session with a short note followed by the turn's message; otherwise it
switches to another available account with the handoff. Claude rotation uses no credits or reset
credits. Reset waits and the limit probes made while choosing an account are excluded from
`--max-minutes`, which counts active time across attempts.

Claude work reserves the top of each account's week for reviews: `claude.work_switch_at` (a finite
number from `0` to `100`, default `85`) makes an account eligible for work only while both windows
are below `min(work_switch_at, soft_switch_at)` on the first pass and below `work_switch_at` on the
second. Reviews keep the `soft_switch_at` and `switch_at` thresholds. Work journal rows
record the provider, account, session, model, effort, tokens, cost and the seven-day utilization
increase for the turn. `--max-weekly` limits the accumulated utilization increase.

Each account's optional `weight` is its relative subscription size, a positive finite number
defaulting to `1`. For example, weights `5` and `20` represent subscriptions with five and twenty
times the base quota. Utilization percentages are always measured against each account's own limit.

Each saved limit snapshot has an `updated_at` timestamp. Every probe attempt, successful or not, also
records `probe_attempted_at`. Before choosing an account for work, a review or an angle, the supervisor probes
every unblocked account whose snapshot has no timestamp or is older than
`claude.limits_max_age_minutes` (a finite nonnegative number, default `30`), unless its last probe
attempt is younger than that interval. This prevents repeated failed probes while the old snapshot
is still stale. Set the interval to `0` to probe before every selection. Refreshes use the same
request as `claude-probe`; an account lock prevents simultaneous probes, and both timestamps are
checked again after acquiring the lock. The shared limits-file lock is released during the request.
The supervisor checks for stop before each probe and terminates a running refresh's process group
when stopped. A failed or stopped probe leaves the previous snapshot available for selection;
usage-limit and missing-token markers retain their usual behavior. Manual `claude-probe` always
attempts a request regardless of snapshot or attempt age.

The supervisor first considers accounts below `soft_switch_at` in both the five-hour and seven-day
windows, then accounts below `switch_at` if none qualify (for work, the `work_switch_at` thresholds above). Angles in the same review prefer unused
available accounts. Within that group it chooses the largest
`(100 - seven_day_used_percent) * weight / max(hours_until_seven_day_reset, 1)`:
the account whose remaining quota needs to be spent fastest before its reset. Unknown utilization
counts as zero, an unknown or passed weekly reset as 168 hours away, and utilization is zero after a reset.
Ties prefer lower five-hour utilization, then configuration order. With equal weights and usage,
an earlier weekly reset still wins.

A rejected rate limit or 401/403 marks that account unavailable until its reported reset and retries
the same review on another available account. A late allowance snapshot does not clear an active
limit marker. The reset is shown as `unknown` when none was reported. `wait` prints one line per switch:
`== claude account <a> limited until <time>; review by <b>`. Once no Claude account is available,
the review falls back to Codex. See [review-cycle.md](review-cycle.md#review-provider) for other
fallback causes and cancellation behavior.

`accounts` prints one Claude line per account, with its name, weight, five-hour and seven-day
utilization percentages, local reset times, snapshot time (`as of ...`), `stale` when the snapshot
needs refreshing, and any limit marker; a missing Keychain token adds
`(token unavailable)` for the hour it excludes the account. Without recorded windows it prints
`no requests yet; run claude-probe <name>`. `accounts --json` includes each Claude account's numeric
`weight`, UTC ISO `updated_at` timestamp (or `null`) and boolean `stale`, alongside its existing
window and availability fields. `accounts --refresh` first refreshes only stale, unblocked Claude
accounts using the same locking and failure rules as selection, then prints; it can be combined with
`--json`. For example:

```text
claude second: weight 20; 5h 4% (reset ...); 7d 13% (reset ...); as of 2026-11-03 10:54
```

The cache
`~/.codex/claude-tasks/claude-limits.json` stores a dictionary by account name;
an older unnamed cache is read as `default`. Every Claude review record and journal row carries its
account name; `report --json` includes it in each task's `reviews` list. Claude review costs and tokens remain separate from Codex.

## Rotation

With `accounts` configured, `start` prefers accounts below `soft_switch_at` in both the `primary` and
`secondary` windows, choosing the one whose longest window resets first. If none are below the soft
threshold, it uses the same reset ordering among accounts still below `switch_at`.

`soft_switch_at` must be a number in `(0, switch_at]`. It defaults to `switch_at` (whose default is
`99`), preserving the original behavior. For example, `soft_switch_at: 80` and `switch_at: 99` keep
a reserve on each account: reaching 80% used in either window triggers a switch only if another
available account is below 80% in both windows. When all accounts have reached the soft threshold,
the current account continues until `switch_at` or a usage-limit error; the soft threshold causes
no further switches. `say` also keeps the current account while it is below the soft threshold or
there is no other available account below it.

A detached supervisor watches limits and applies these thresholds during the task. `wait` and
rotation notifications name `soft_switch_at` when it caused the switch; `accounts` displays it when
it differs from `switch_at`. The next account opens a new thread in the same worktree, with the brief,
follow-ups and current progress; commits and uncommitted changes are the starting point. At the hard
threshold, if no other account is available, it continues on credits only when `credits` is true.
Use `accounts` to inspect availability; two homes signed into the same ChatGPT account share limits.

## Credits

With `credits: true`, a task whose accounts have reached `switch_at` may keep running on an account's
ChatGPT credits. `credits_floor` (default `0`) keeps a reserve: an account runs on credits only while
its own known balance is strictly above the floor; `unlimited` credits always pass, and an unknown
balance never does. `credits_floor_until` (optional `YYYY-MM-DD`) limits the reserve to that local
date: the floor applies through the end of the day, and from the next day it is `0`, so the rest of
the balance can be spent before it expires.
If credits are blocked only by this reserve, a waiting task wakes at the start of the next local
day after `credits_floor_until`, or at the next window reset if it comes sooner. After waking it
prefers an available subscription, then the current account's spendable credits, then another
account's spendable credits.

```json
{"credits": true, "credits_floor": 50, "credits_floor_until": "2030-06-15"}
```

The supervisor re-reads every account's balance at each limit sample while an exec runs on credits.
When the balance reaches the floor, that account counts as exhausted without credits: the task
rotates to an available account, or to another account with credits above the floor, and otherwise
waits for a reset (or pauses with `wait_for_reset: false`). The same rule chooses the account at
the start of a turn. `wait` and notifications give the reason, for example
`credits of main at the floor 50`. `accounts` prints the active floor and its end date once and marks
balances at or below it with `(at floor)`; `accounts --json` adds the active `credits_floor`,
`credits_floor_until` and each account's `credits.spendable`: the known balance above the active
floor (at least `0`), `0` when `hasCredits` is false, or `null` for unlimited or unknown credits.
The text output shows `unlimited` for unlimited credits and `0` when `hasCredits` is false.

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
