# codex-worker board

A read-only local view of codex-worker tasks and Claude Code sessions from the Claude desktop app,
with phases, accounts and live activity. The server reads JSON and event logs from disk without
importing the plugin or changing source files. REST and WebSocket
payloads are defined in [CONTRACT.md](CONTRACT.md).

From the repository root, with Python 3.10+ and uv installed:

```sh
uv run --project board/server codex-worker-board
```

Open http://localhost:8787. The server listens on `127.0.0.1` by default; CLI options are
`--host` (default `127.0.0.1`) and `--port` (default `8787`). Requests must use a localhost Host header;
WebSocket Origins, when present, must also be localhost. The server only reads task files and listens
on localhost; it has no task control endpoints.

The frontend builds into `board/web/dist`, which the server serves with an SPA fallback:

```sh
npm --prefix board/web ci && npm --prefix board/web run build
```

Until that directory exists, `/` returns a short build instruction. The API is available independently.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `CODEX_TASK_HOME` | `~/.codex` | Read tasks from `claude-tasks/` and archives from `claude-tasks/.archive/`. Snapshots include at most 50 closed records together, ranked by state mtime. |
| `BOARD_POLL_SECONDS` | `1.0` | Interval between file mtime and size polls, in seconds. |
| `BOARD_ACCOUNTS_SECONDS` | `300` | Minimum interval between account subprocess calls, in seconds. |
| `BOARD_STALE_DAYS` | `7` | Close non-alive tasks older than this many days by state mtime. Non-alive scouts are always closed; alive tasks are exempt from the age rule. |
| `BOARD_CLAUDE_DESKTOP_DIR` | `~/Library/Application Support/Claude` | Read desktop metadata from `claude-code-sessions/*/*/local_*.json`. Missing directories are skipped. |
| `BOARD_CLAUDE_HOME` | `~/.claude` | Read live state from `sessions/<pid>.json` and activity from `projects/*/<cliSessionId>.jsonl`. Key files are never opened. |
| `BOARD_SESSIONS_SECONDS` | `5` | Interval between desktop metadata scans, in seconds; unchanged files are not read again. Live state is read on every board poll. |
| `BOARD_SESSION_STALE_HOURS` | `48` | Close inactive sessions older than this many hours by `lastActivityAt`. Busy sessions and sessions awaiting a person take precedence. |
| `BOARD_CODEX_TASK` | Repository `plugins/codex-worker/skills/codex-worker/scripts/codex_task.py` | Script called as `python3 -I <script> accounts --json`, without `--refresh`, with a 60-second timeout. |

Account calls run outside the event loop. Errors preserve the previous account data and expose an
error string. A malformed or partially written state file preserves its last successfully read state.
The server never reads credentials or account homes directly. Desktop session files are internal
application data, not a public API. Only the metadata fields listed in [CONTRACT.md](CONTRACT.md) are
retained; malformed files preserve their previous valid state, and symbolic links are skipped.
Archived sessions are omitted. Sessions and tasks share the snapshot limit of 50 newest closed records
by `updated_at`. Live session transcripts use the same bounded incremental activity reader as tasks;
if a transcript has moved to another project directory, its fallback lookup is cached for the session.

Run the server tests with temporary task and Claude directories and fake account scripts:

```sh
uv run --project board/server pytest board/server/tests -q
```
