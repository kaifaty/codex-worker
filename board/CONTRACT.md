# Board data contract (v1)

The board is a read-only, local web view of codex-worker tasks. The server reads the files the plugin
already writes and pushes changes over a WebSocket. It never starts, stops or changes a task, and never
writes under the codex-worker home.

## Sources

- Task home: `$CODEX_TASK_HOME` (default `~/.codex`); live tasks in `claude-tasks/<slug>/`, archived ones in
  `claude-tasks/.archive/<slug-and-timestamp>/`. Each holds `state.json` and, when present, `brief.md`,
  `last.md`, `review.md`, `check.log`.
- Live activity: `events.jsonl` (Codex executor, `codex exec --json` lines) and `claude-events.jsonl` (Claude
  executor, `claude --output-format stream-json` lines; written by codex-worker 1.47+). Either may be missing.
- Accounts: `python3 -I <codex_task.py> accounts --json` (never `--refresh`), at most once per
  `BOARD_ACCOUNTS_SECONDS` (default 300). The script path comes from `BOARD_CODEX_TASK` and defaults to this
  repository's `plugins/codex-worker/skills/codex-worker/scripts/codex_task.py`.
- The server reads no other files: never `auth.json`, credentials or account homes.

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
  kind: "work" | "review" | "scout";
  repo: string;               // basename of the repository directory
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
  alive: boolean;             // the task's supervisor process is running
  turn: number;               // turns so far
  fix_round: number;          // automatic fix rounds in the current cycle
  outcome: string | null;     // state.json "outcome" as written by the plugin
  result_status: string | null; // "done" | "partial" | "needs_decision" | "blocked" | null
  summary: string | null;     // first line of the last result summary, ≤ 200 chars
  pr: string | null;          // PR URL
  ci: string | null;          // "pending" | "passed" | "failed" | "skipped" | "timeout" | "none" | null
  sleeping_until: number | null;
  created: number | null;
  updated_at: number;         // mtime of state.json
  activity: Activity | null;  // latest activity, only while alive
}

interface TaskDetail extends TaskSummary {
  timeline: { at: number; text: string }[];   // turns, checks, reviews, rotations, CI, ≤ 200 entries
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

## REST

- `GET /api/snapshot` → `Snapshot`.
- `GET /api/tasks/{slug}` → `TaskDetail`; 404 for an unknown slug. Archived tasks are addressed by their
  archive folder name.
- `GET /api/tasks/{slug}/files/{name}` with `name` in `brief`, `last`, `review`, `check` → `text/plain;
  charset=utf-8`, at most the last 256 KiB; 404 when missing. No other names, no paths.

## WebSocket `/ws`

Server → client only; the server ignores client messages. Every message is one JSON object:

- `{"type": "snapshot", "snapshot": Snapshot}` — first message after connect.
- `{"type": "task", "task": TaskSummary}` — a task appeared or changed.
- `{"type": "task_removed", "slug": string}` — a live task folder disappeared (it is then sent again as
  `closed` if it was archived).
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
