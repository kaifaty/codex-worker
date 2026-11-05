# codex-worker board

A read-only local view of codex-worker tasks, phases, accounts and live activity. The server reads task
JSON and event logs from disk without importing the plugin or changing task files. REST and WebSocket
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

The header switches between two views of the same data: «Доска», the columns, and «Карта», a prototype
that draws one repository as a low-poly island (three.js, loaded only when the tab opens). Each task has a
workbench there, and its agent walks to the place of its phase: the test stand, the review table, the CI
gate, a tent while it sleeps, the pier when it waits for review. `board/web/src/map/placement.ts` decides
the place and the action. `npm --prefix board/web run dev:fixture` adds a «Сценарий» source to the map
that takes three demo agents through every place.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `CODEX_TASK_HOME` | `~/.codex` | Read tasks from `claude-tasks/` and archives from `claude-tasks/.archive/`. Snapshots include at most 50 closed records together, ranked by state mtime. |
| `BOARD_POLL_SECONDS` | `1.0` | Interval between file mtime and size polls, in seconds. |
| `BOARD_ACCOUNTS_SECONDS` | `300` | Minimum interval between account subprocess calls, in seconds. |
| `BOARD_STALE_DAYS` | `7` | Close non-alive tasks older than this many days by state mtime. Non-alive scouts are always closed; alive tasks are exempt from the age rule. |
| `BOARD_CODEX_TASK` | Repository `plugins/codex-worker/skills/codex-worker/scripts/codex_task.py` | Script called as `python3 -I <script> accounts --json`, without `--refresh`, with a 60-second timeout. |

Account calls run outside the event loop. Errors preserve the previous account data and expose an
error string. A malformed or partially written state file preserves its last successfully read state.
The server never reads credentials or account homes directly.

Run the server tests with temporary task homes and fake account scripts:

```sh
uv run --project board/server pytest board/server/tests -q
```
