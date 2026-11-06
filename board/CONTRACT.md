# Board data contract (v1)

The board is a read-only, local web view of codex-worker tasks and Claude Code sessions from the Claude
desktop app. The server reads existing files and pushes changes over a WebSocket. It never starts,
stops or changes work, and never writes under either source's home.

## Sources

- Task home: the worker's data home, resolved by the worker's rules: `$TEAMSTER_HOME`; else the path in
  `$CODEX_TASK_HOME/teamster-home`, else `$CODEX_TASK_HOME` (legacy); else `~/.teamster` if it exists, else
  `~/.codex` (legacy) if it holds `claude-tasks/` or `codex-worker.json`, else `~/.teamster`. Live tasks are
  in `tasks/<slug>/` (legacy: `claude-tasks/<slug>/`), archived ones in `tasks/.archive/<slug-and-timestamp>/`
  (legacy: `claude-tasks/.archive/`). Each holds `state.json` and, when present, `brief.md`,
  `last.md`, `review.md`, `check.log`.
- Live activity: `events.jsonl` (Codex executor, `codex exec --json` lines) and `claude-events.jsonl` (Claude
  executor, `claude --output-format stream-json` lines; written by codex-worker 1.47+). Either may be missing.
- Accounts: `python3 -I <script> accounts --json` (never `--refresh`), at most once per
  `BOARD_ACCOUNTS_SECONDS` (default 300). The script path comes from `BOARD_CODEX_TASK` and defaults to this
  repository's `src/codex_worker/__main__.py`.
- Machines: `$BOARD_MACHINES_FILE` (default `board-machines.json` in the task home), with local or SSH
  probes every `BOARD_MACHINES_SECONDS` (default 30). The configuration and probe are described below.
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
- The server never reads `auth.json`, credentials or account homes.

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

interface Gpu {
  name: string;
  utilization_percent: number | null;
  memory_used_bytes: number | null; // null on Apple GPUs with shared memory
  memory_total_bytes: number | null;
  processes: { name: string; memory_bytes: number }[]; // ≤ 5 largest, each ≥ 100 MiB
}

interface Metrics {
  os: "linux" | "darwin" | string;
  cpu: {
    cores: number;
    load1: number;
    load5: number;
    percent: number | null; // busy share of all cores over about 1 s, 0–100; null when unknown
    average_percent: number | null; // mean of percent over the last 150 s; set by the server, null for hosts
  } | null;
  memory: { total_bytes: number; available_bytes: number } | null;
  gpus: Gpu[];
  disks: { mount: string; total_bytes: number; free_bytes: number }[];
}

type MachineStatus = "free" | "busy" | "overloaded" | "offline" | "unknown";
type MachineError = "timeout" | "unreachable" | "bad_output" | "probe_failed";

interface HostView {
  name: string;
  cpu: Metrics["cpu"];
  memory: Metrics["memory"];
  error: MachineError | null;
}

interface HistoryPoint {
  at: number;
  cpu_percent: number | null;    // cpu.percent, else load1 / cores × 100 capped at 100
  memory_percent: number | null; // (total_bytes - available_bytes) / total_bytes × 100
  gpu_percent: number | null;    // highest available GPU utilization
}

interface Machine {
  id: string;
  name: string;
  role: string | null;
  status: MachineStatus;
  reasons: string[];            // cpu, memory, gpu, gpu_memory or disk:<mount>
  checked_at: number | null;    // last attempt
  sampled_at: number | null;    // last success
  error: MachineError | null;   // last attempt's error
  metrics: Metrics | null;      // last successful metrics, retained after a failure
  host: HostView | null;        // optional physical host; never affects machine status
  history: HistoryPoint[];      // newest 120 points, held only in memory
}

interface Machines {
  config_error: string | null;
  items: Machine[];
}

interface Snapshot {
  generated_at: number;
  tasks: TaskSummary[];       // all non-closed tasks, plus at most 50 closed records with the newest updated_at
  accounts: Accounts;
  machines: Machines;
}
```

### Machine configuration and probes

See [machines.example.json](machines.example.json) for a configuration containing only fictitious
machines. The file is an object with a `machines` array. Each entry requires a unique `id` matching
`^[a-z0-9-]+$` and a `name`. Exactly one of `local: true` or `ssh` selects its probe transport. Optional
`role` describes its purpose; optional `disks` is an array of absolute paths, defaulting to `["/"]`.
Optional `host: {"name": "Hypervisor", "ssh": "root@hypervisor.example"}` describes a VM's physical
host. Only its CPU and memory are published.

Every SSH target, including a host's target, must match `^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$` and cannot
start with `-`. An invalid entry rejects the entire file. The server rereads it when its mtime or size
changes. A missing file means an empty machine list with no error. A malformed or invalid file keeps
the last valid machines and sets `config_error` to a short reason without revealing an SSH target.
Targets never appear in REST or WebSocket data, including errors.

The server sends the standalone, Python 3.9-compatible `probe.py` as stdin to
`python3 -I - <disks…>` locally, or to
`ssh -o BatchMode=yes -o ConnectTimeout=5 -- <target> python3 -I - <disks…>` remotely. Remote paths are
quoted with `shlex.quote`. Probes run in parallel; each has a 15-second limit and at most 1 MiB of
stdout is read. A timed-out process is killed and awaited. Output is untrusted: fields are converted
and validated and strings are limited to 200 characters. Malformed output gives `bad_output`.
SSH exit code 255 gives `unreachable`, other nonzero exits give `probe_failed`, and expiry gives
`timeout`. Stderr is retained only in server logs.
Physical host probes use the same transport with an internal `--host` argument, collecting CPU and
memory only and skipping GPU commands.

The probe only reads state; it does not write files, control services or containers, or use sudo.
Each external command has a 5-second timeout and `stdin=DEVNULL`. It prints one JSON object and exits
successfully even when some sections are unavailable: those sections are `null` or empty arrays.
CPU uses `os.cpu_count` and `os.getloadavg`. `percent` is the busy share of all cores over one
second: on Linux, two reads of the aggregate `cpu` line of `/proc/stat` one second apart, with
`idle + iowait` as idle time; on macOS, the last `CPU usage: … % idle` line of `top -l 2 -n 0 -s 1`
(the first sample counts since boot), as `100 − idle`. A failed or unparsable sample gives `null`.
Host probes collect `percent` too. Load average is reported but is no measure of use on macOS: it has been
seen at twelve times the core count while the CPU was 64% idle.
The server accepts `percent` from 0 to 100; a probe without the field gives `null`. Linux memory uses `/proc/meminfo` (`MemTotal` and
`MemAvailable`), while macOS uses `sysctl -n hw.memsize` and the final system-wide free percentage
from `memory_pressure`. Available macOS memory is that fraction of total memory. Disks use
`os.statvfs`: total is `f_blocks × f_frsize`, free is `f_bavail × f_frsize`; missing paths are skipped.

NVIDIA metrics use `nvidia-smi` GPU queries for index, UUID, name, utilization, used memory and total
memory, and compute-app queries for GPU UUID, process name and used memory. UUID associates processes
with their GPU, `[N/A]` becomes `null`, and a missing or failed command yields an empty GPU list.
Apple GPU utilization comes from `ioreg -r -d 1 -c IOAccelerator` (`Device Utilization %`), with the
name from `sysctl -n machdep.cpu.brand_string`; GPU memory is `null` because memory is shared.
Process names are the last path component. For `python…`, `node` or `java` under `/.venv/` or `/venv/`,
the directory immediately before that segment is used instead. For example,
`/x/decider/.venv/bin/python` becomes `decider`, and `/x/build/bin/llama-server` becomes `llama-server`.
Full process paths are never published.

### Machine status and history

Status uses the machine's last successful metrics and consecutive failure count. Before any attempt,
status is `unknown`; a failure without prior successful metrics or two consecutive failures gives
`offline`. Both statuses, and `free`, have empty `reasons`. After one failed attempt with prior data,
the previous metrics still determine status and `error` describes the failed attempt. Host metrics
never affect status.

The pure `machine_status(metrics, failures)` function uses these thresholds, defined as named
constants in `machines.py`. The highest matching severity wins; reasons include every triggered
condition at that severity or above, including all applicable disk mounts.

| Condition | `busy` threshold | `overloaded` threshold | Reason |
| --- | --- | --- | --- |
| CPU `average_percent` (or `percent` before it exists) | ≥ 70% | ≥ 90% | `cpu` |
| CPU load1 / cores, only when `percent` is `null` | ≥ 0.7 | ≥ 1.5 | `cpu` |
| Available memory / total | < 25% | < 10% | `memory` |
| GPU utilization | ≥ 70% | — | `gpu` |
| GPU used memory / total | ≥ 60% | — | `gpu_memory` |
| Disk free space / total | < 15% | < 5% | `disk:<mount>` |

Otherwise status is `free`. One-second CPU samples jump, so the status follows their mean over the last
150 seconds, which is about six samples at the default interval. History retains at most 120 samples in memory and is reset on server
restart. A successful machine probe appends one point; failed probes retain history without adding
a point. Unknown percentages are `null`. `cpu_percent` is the busy percentage, or load1 / cores × 100
capped at 100 when the percentage is unknown, so every history value is between 0 and 100.

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
- `{"type": "machines", "machines": Machines}` — at the end of a machine polling cycle when data
  changed, comparing without `checked_at`. Metric and history changes count as changes.
- `{"type": "ping", "at": number}` — every 20 s.

The server polls file mtimes and sizes once per `BOARD_POLL_SECONDS` (default 1.0) and sends only changes.
A client that reconnects gets a fresh `snapshot`.
The `machines` snapshot field and WebSocket message are additive. Existing clients can ignore them;
the current frontend selects known snapshot fields and ignores unknown WebSocket message types.

## Serving and safety

- `codex-worker-board [--host 127.0.0.1] [--port 8787]` binds to `127.0.0.1` by default.
- Requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]` (with any port) get 403, and so do
  WebSocket upgrades whose `Origin` is present and not one of those hosts — DNS-rebinding protection.
- When `board/web/dist/` exists, `/` serves it (SPA fallback to `index.html`); otherwise `/` returns a short
  text that says to build the web app.
