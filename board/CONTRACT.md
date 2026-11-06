# Board data contract (v1)

The board is a read-only, local web view of codex-worker tasks and Claude Code sessions from the Claude
desktop app. The server reads existing files and pushes changes over a WebSocket. It never starts,
stops or changes work, and never writes under either source's home.

## Sources

- Task home: `$CODEX_TASK_HOME` (default `~/.codex`); live tasks in `claude-tasks/<slug>/`, archived ones in
  `claude-tasks/.archive/<slug-and-timestamp>/`. Each holds `state.json` and, when present, `brief.md`,
  `last.md`, `review.md`, `check.log`.
- Live activity: `events.jsonl` (Codex executor, `codex exec --json` lines) and `claude-events.jsonl` (Claude
  executor, `claude --output-format stream-json` lines; written by codex-worker 1.47+). Either may be missing.
- Accounts: `python3 -I <codex_task.py> accounts --json` (never `--refresh`), at most once per
  `BOARD_ACCOUNTS_SECONDS` (default 300). The script path comes from `BOARD_CODEX_TASK` and defaults to this
  repository's `plugins/codex-worker/skills/codex-worker/scripts/codex_task.py`.
- Desktop sessions: `$BOARD_CLAUDE_DESKTOP_DIR/claude-code-sessions/*/*/local_*.json` (default desktop
  directory `~/Library/Application Support/Claude`). These are internal application files, not a public
  API. Only `sessionId`, `cliSessionId`, `title`, `cwd`, `originCwd`, `model`, `effort`, `isArchived`,
  `createdAt`, `lastActivityAt`, `completedTurns`, `interruptedAwaitingPerson`, and `prs` are retained.
  For each PR, only `url`, `state`, `branch`, and `own` are retained. Other fields are never exposed.
- Live sessions: `$BOARD_CLAUDE_HOME/sessions/<pid>.json` (default home `~/.claude`), with numeric filenames
  only. The fields used are `pid`, `sessionId` (the metadata's `cliSessionId`), `cwd`, `status`,
  `statusUpdatedAt`, and `entrypoint`. A state is valid only while its PID is alive. Files such as
  `<pid>.<hash>.key` are never opened.
- Session activity: `$BOARD_CLAUDE_HOME/projects/<encoded-cwd>/<cliSessionId>.jsonl`, where every character
  outside ASCII letters and digits in the live cwd is replaced by `-`. Only busy, live sessions are
  tailed through the same bounded incremental reader as task activity. Until a transcript is found,
  check the direct path on each poll and search `projects/*/` at most once per 60 seconds. Cache a found
  path. Assistant events may carry ISO `timestamp` values.
- The server reads no other files: never `auth.json`, credentials or account homes.

Missing directories are skipped. Malformed metadata or live-state files preserve their last valid
record. Symbolic links are never followed. Metadata scans run once per `BOARD_SESSIONS_SECONDS`
(default 5); a file is read only when its mtime or size changes. Archived metadata retains only the
information needed to detect a change. Live session state is read on each board poll.

## Types

All times are Unix seconds (float) or `null`. Strings that come from task text are cut to the stated length.

```ts
type Column = "running" | "sleeping" | "review_me" | "attention" | "closed";
type Phase = "turn" | "check" | "review" | "fix" | "ci" | "sleeping" | "idle";

interface Activity {
  at: number | null;
  kind: "command" | "edit" | "message" | "tool" | "thinking";
  text: string;               // one line, ≤ 200 chars
}

interface TaskSummary {
  slug: string;
  kind: "work" | "review" | "scout" | "session";
  title: string | null;       // desktop session title; null for tasks
  link: string | null;        // claude://claude.ai/epitaxy/<sessionId>; null for tasks
  repo: string;               // repository basename; see session mapping below
  branch: string | null;
  task_type: string | null;   // e.g. "feature", "fix", "layout"
  executor: "codex" | "claude";
  account: string | null;     // Codex account, or the Claude pool account for a Claude executor
  reviewer: "codex" | "claude" | null;
  model: string | null;
  effort: string | null;
  column: Column;
  phase: Phase;
  phase_started_at: number | null;
  alive: boolean;             // task supervisor running, or session PID running with status busy
  turn: number;               // turns so far
  fix_round: number;          // automatic fix rounds in the current cycle
  outcome: string | null;     // state.json "outcome" as written by the plugin
  result_status: string | null; // "done" | "partial" | "needs_decision" | "blocked" | null
  summary: string | null;     // first line of the last result summary, ≤ 200 chars
  pr: string | null;          // PR URL
  ci: string | null;          // "pending" | "passed" | "failed" | "skipped" | "timeout" | "none" | null
  sleeping_until: number | null;
  created: number | null;
  updated_at: number;         // task state mtime, or session lastActivityAt / 1000
  activity: Activity | null;  // latest activity, only while alive
}

interface TaskDetail extends TaskSummary {
  timeline: { at: number; text: string }[];   // task events (≤ 200), or session creation and last activity
  files: ("brief" | "last" | "review" | "check")[];   // which files exist
}

interface Accounts {
  fetched_at: number | null;
  error: string | null;
  data: unknown | null;       // `accounts --json` output, passed through unchanged
}

interface Snapshot {
  generated_at: number;
  tasks: TaskSummary[];       // all non-closed tasks, plus at most 50 closed records with the newest updated_at
  accounts: Accounts;
}
```

### Columns

- `running`: `alive` and not sleeping.
- `sleeping`: `outcome == "sleeping"` and `alive`.
- `review_me`: not `alive`, `result_status == "done"` and the outcome is not a failure — waiting for the lead.
- `attention`: not `alive` and anything else (`needs_decision`, `blocked`, `partial`, `failed`, `stopped`,
  `budget_exhausted`, `conflict`, a supervisor that died with `outcome == "running"` or `"sleeping"`).
- `closed`: archived records, non-alive scouts regardless of outcome, and non-alive tasks whose `updated_at`
  is older than `BOARD_STALE_DAYS` (default 7 days). Age never closes an alive task. `Snapshot.tasks` includes
  at most 50 closed records across archive and live task folders together, ranked by newest `updated_at`.
  Closed classification takes precedence over the other columns.

### Desktop session mapping

Sessions use the same `TaskSummary`, with these values:

- `slug` is `sessionId` (`local_<uuid>`); `kind` is `session`, `executor` is `claude`.
- `title` comes from metadata; `link` is `claude://claude.ai/epitaxy/<sessionId>`.
- `reviewer`, `task_type`, `account`, `ci`, `outcome`, `result_status`, `summary`, and `sleeping_until`
  are `null`; `fix_round` is 0. `model` and `effort` come from metadata; `turn` is `completedTurns`.
- `repo` comes from `originCwd`, falling back to `cwd`: for a path containing `/.claude/worktrees/`,
  use the directory name immediately before that segment; otherwise use the last directory name.
  An application service directory inside `BOARD_CLAUDE_DESKTOP_DIR` gives `"(no folder)"`.
- `pr` is the last PR URL with `own == true`, otherwise the last PR URL. `branch` is the last own PR's
  branch, or `null` if there is no own PR.
- `alive` requires live state matching `cliSessionId`, a running PID, and `status == "busy"`.
  `phase` is `turn` when alive, otherwise `idle`; `phase_started_at` is the live `statusUpdatedAt` when
  alive, otherwise metadata `lastActivityAt`. `activity` is shown and emitted only when alive and its
  `at` is at least the current busy state's `statusUpdatedAt` (in seconds). Leaving busy clears it.
- `created` and `updated_at` are metadata `createdAt` and `lastActivityAt`. Metadata and live-state
  millisecond timestamps are converted to Unix seconds.

Session columns use the first matching rule:

1. `isArchived`: omit the session entirely, including from `closed`.
2. `alive`: `running`.
3. `interruptedAwaitingPerson` is not `null`: `attention`.
4. At least one PR exists and all PR states are `MERGED` or `CLOSED`: `closed`.
5. `lastActivityAt` is older than `BOARD_SESSION_STALE_HOURS` (default 48 hours): `closed`.
6. Otherwise: `review_me`, waiting for a person after a completed turn.

Closed sessions and tasks share the limit of 50 records, ranked by `updated_at`.
The additional fields are additive; existing clients may ignore `title` and `link`.

## REST

- `GET /api/snapshot` → `Snapshot`.
- `GET /api/tasks/{slug}` → `TaskDetail`; 404 for an unknown slug. Archived tasks are addressed by their
  archive folder name. Sessions have an empty `files` array and creation/last-activity timeline entries.
  Session paths come only from scanned records, never from a requested slug; path-like slugs return 404.
- `GET /api/tasks/{slug}/files/{name}` with `name` in `brief`, `last`, `review`, `check` → `text/plain;
  charset=utf-8`, at most the last 256 KiB; 404 when missing or for a session. No other names, no paths.

## WebSocket `/ws`

Server → client only; the server ignores client messages. Every message is one JSON object:

- `{"type": "snapshot", "snapshot": Snapshot}` — first message after connect.
- `{"type": "task", "task": TaskSummary}` — a task appeared or changed.
- `{"type": "task_removed", "slug": string}` — a live task folder disappeared (it is then sent again as
  `closed` if it was archived), or a session's metadata disappeared or became archived.
- `{"type": "activity", "slug": string, "activity": Activity}` — new latest activity of a live task.
- `{"type": "accounts", "accounts": Accounts}` — new account data.
- `{"type": "ping", "at": number}` — every 20 s.

The server polls file mtimes and sizes once per `BOARD_POLL_SECONDS` (default 1.0) and sends only changes.
A client that reconnects gets a fresh `snapshot`.

## Serving and safety

- `codex-worker-board [--host 127.0.0.1] [--port 8787]` binds to `127.0.0.1` by default.
- Requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]` (with any port) get 403, and so do
  WebSocket upgrades whose `Origin` is present and not one of those hosts — DNS-rebinding protection.
- When `board/web/dist/` exists, `/` serves it (SPA fallback to `index.html`); otherwise `/` returns a short
  text that says to build the web app.
