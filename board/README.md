# codex-worker board

A read-only local view of codex-worker tasks and Claude Code sessions from the Claude desktop app,
with phases, accounts, live activity and configured machine metrics. The server reads JSON and event logs from disk without
importing the plugin or changing source files. REST and WebSocket
payloads are defined in [CONTRACT.md](CONTRACT.md).

From the repository root, with Python 3.10+ and uv installed:

```sh
uv run --project board/server codex-worker-board
```

Open http://localhost:8787. The server listens on `127.0.0.1` by default; CLI options are
`--host` (default `127.0.0.1`) and `--port` (default `8787`). Requests must use a localhost Host header;
WebSocket Origins, when present, must also be localhost. The server reads task files and machine
metrics and listens on localhost; it has no task control endpoints.

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
| `TEAMSTER_HOME`, `CODEX_TASK_HOME` | `~/.teamster` | The worker's data home, found by the worker's rules (`codex-worker docs settings`). Read tasks from `tasks/` and archives from `tasks/.archive/` (`claude-tasks/` in a legacy home in `~/.codex`). Snapshots include at most 50 closed records together, ranked by state mtime. |
| `BOARD_POLL_SECONDS` | `1.0` | Interval between file mtime and size polls, in seconds. |
| `BOARD_ACCOUNTS_SECONDS` | `300` | Minimum interval between account subprocess calls, in seconds. |
| `BOARD_MACHINES_FILE` | `<data home>/board-machines.json` | Machine configuration; reread when mtime or size changes. Missing means no machines; invalid files preserve the last valid list and expose `config_error`. |
| `BOARD_MACHINES_SECONDS` | `30` | Interval between parallel machine and optional physical host probe cycles, in seconds. |
| `BOARD_STALE_DAYS` | `7` | Close non-alive tasks older than this many days by state mtime. Non-alive scouts are always closed; alive tasks are exempt from the age rule. |
| `BOARD_CLAUDE_DESKTOP_DIR` | `~/Library/Application Support/Claude` | Read desktop metadata from `claude-code-sessions/*/*/local_*.json`. Missing directories are skipped. |
| `BOARD_CLAUDE_HOME` | `~/.claude` | Read live state from `sessions/<pid>.json` and activity from `projects/*/<cliSessionId>.jsonl`. Key files are never opened. |
| `BOARD_SESSIONS_SECONDS` | `5` | Interval between desktop metadata scans, in seconds; unchanged files are not read again. Live state is read on every board poll. |
| `BOARD_SESSION_STALE_HOURS` | `48` | Close inactive sessions older than this many hours by `lastActivityAt`. Busy sessions and sessions awaiting a person take precedence. |
| `BOARD_CODEX_TASK` | Repository `src/codex_worker/__main__.py` | Script called as `python3 -I <script> accounts --json`, without `--refresh`, with a 60-second timeout. |

Account calls run outside the event loop. Errors preserve the previous account data and expose an
error string. A malformed or partially written state file preserves its last successfully read state.
The server never reads credentials or account homes directly. Desktop session files are internal
application data, not a public API. Only the metadata fields listed in [CONTRACT.md](CONTRACT.md) are
retained; malformed files preserve their previous valid state, and symbolic links are skipped.
Archived sessions are omitted. Sessions and tasks share the snapshot limit of 50 newest closed records
by `updated_at`. Live session transcripts use the same bounded incremental activity reader as tasks;
only events from the current busy period are shown. Transcript paths use ASCII letters and digits for
cwd encoding. Until a transcript is found, its direct path is checked on every poll and fallback searches
across project directories run at most once per 60 seconds; a found path is cached.

## Run as a service

On macOS, launchd can keep the board running: it starts at login and restarts after a crash.
`codex-worker-board plist` prints the job and installs nothing. It accepts `--host` and `--port` with
the defaults above. It sets `TEAMSTER_HOME` to a new-layout data home (also copying `CODEX_TASK_HOME`
when set) or `CODEX_TASK_HOME` to a legacy one, and copies every `BOARD_*` variable set in the current
environment into the job. Logs go to `<data home>/board-server.log`.

1. Clone the repository to a permanent place outside working directories, then build and sync:

   ```sh
   git clone https://github.com/kaifaty/codex-worker.git ~/codex-worker-board
   cd ~/codex-worker-board
   npm --prefix board/web ci && npm --prefix board/web run build
   uv sync --project board/server --no-dev
   ```

2. Write the job:

   ```sh
   board/server/.venv/bin/codex-worker-board plist > ~/Library/LaunchAgents/codex-worker.board.plist
   ```

3. Load it:

   ```sh
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/codex-worker.board.plist
   ```

4. Update: pull, build and sync as in step 1, then restart the job:

   ```sh
   git pull
   npm --prefix board/web ci && npm --prefix board/web run build
   uv sync --project board/server --no-dev
   launchctl kickstart -k gui/$(id -u)/codex-worker.board
   ```

5. Disable:

   ```sh
   launchctl bootout gui/$(id -u)/codex-worker.board
   ```

The port must be free, so stop a manually started server before loading the job.

## Machine metrics

Create the machine configuration outside the public repository, using
[machines.example.json](machines.example.json) as a fictitious example. The file contains a `machines`
array. Each machine requires a unique `id` matching `^[a-z0-9-]+$`, a `name`, and exactly one of
`local: true` or `ssh`. Optional `role` describes the machine; `disks` contains absolute paths and
defaults to `["/"]`. A VM may include `host: {"name": "Hypervisor", "ssh": "root@hypervisor.example"}`
to collect its physical host's CPU and memory separately. Every `ssh` must match
`^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$`; targets beginning with `-` are rejected. Any invalid entry rejects
the file. The server keeps the last valid configuration and publishes a short `config_error` until
a corrected file is loaded. Removing the file clears the list without an error.

Every 30 seconds by default, the server runs a standalone Python 3.9-compatible probe locally with
`python3 -I - <disks…>` or remotely with
`ssh -o BatchMode=yes -o ConnectTimeout=5 -- <target> python3 -I - <disks…>`, sending the probe source
through stdin and quoting remote paths. All probes run concurrently with individual 15-second
timeouts and a 1 MiB output limit. A timed-out process is killed and awaited. The probe only reads
state: no file writes, service or container control, or sudo. Its external commands use 5-second
timeouts and disconnected stdin. No SSH connection is attempted unless a target is configured.

Metrics contain OS, CPU core count and load averages, total and available memory, GPU utilization and
memory, up to five GPU processes using at least 100 MiB each, and total/free bytes for requested disks.
Linux memory comes from `/proc/meminfo`; macOS memory comes from `sysctl` and `memory_pressure`.
NVIDIA data comes from `nvidia-smi`, with UUID matching processes to GPUs; Apple GPU utilization comes
from `ioreg` and uses shared memory. Missing sections are `null` or empty lists. Missing disk paths
are skipped. Only process basenames or their virtual environment project names are exposed, never
full executable paths. Untrusted output is validated and strings are limited to 200 characters.

`GET /api/snapshot` includes `machines: {config_error, items}`. Every machine contains its identity,
`status`, `reasons`, attempt/success timestamps, last error and successful metrics, optional host
CPU/memory, and up to 120 history points in memory. Successful probes append history with CPU load
percentage (which can exceed 100%), used-memory percentage and highest GPU utilization; failures
preserve the last successful metrics and history. Error values are `timeout`, `unreachable` (SSH exit
255), `bad_output` or `probe_failed`. SSH targets never appear in API or WebSocket data, including
errors; stderr goes only to server logs. Physical host metrics do not affect machine status.

Status is `unknown` before any attempt, `offline` after a failure without successful data or two
consecutive failures, otherwise `free`, `busy` or `overloaded` based on the last successful metrics.
One failure with prior data preserves the computed status and records the error. Thresholds are:

| Metric | `busy` | `overloaded` |
| --- | --- | --- |
| CPU load1 / cores | ≥ 0.7 | ≥ 1.5 |
| Available memory / total | < 25% | < 10% |
| GPU utilization | ≥ 70% | — |
| GPU used memory / total | ≥ 60% | — |
| Disk free space / total | < 15% | < 5% |

The highest severity wins. Reasons use `cpu`, `memory`, `gpu`, `gpu_memory` and `disk:<mount>` for
matching conditions at that severity or above; `free`, `offline` and `unknown` have no reasons.
The server sends `{"type": "machines", "machines": Machines}` after each cycle whose data changed,
ignoring `checked_at` in comparison. The existing frontend ignores the added snapshot field and
unknown message type. Machine selection and the machine interface are separate work.
The complete `Metrics`, `Gpu`, `Machine`, `Machines`, host and history types are in
[CONTRACT.md](CONTRACT.md#types).

Run the server tests with temporary task and Claude directories, fake account scripts and fake SSH:

```sh
uv run --project board/server pytest board/server/tests -q
```
