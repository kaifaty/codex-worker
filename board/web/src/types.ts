// Board data contract v1 (board/CONTRACT.md). Keep this file in step with the contract.
// All times are Unix seconds (float) or null.

export type Column = "running" | "sleeping" | "review_me" | "attention" | "closed";
export type Phase = "turn" | "check" | "review" | "fix" | "ci" | "sleeping" | "idle";

export interface Activity {
  at: number | null;
  kind: "command" | "edit" | "message" | "tool" | "thinking";
  text: string; // one line, ≤ 200 chars
}

export interface TaskSummary {
  slug: string;
  kind: "work" | "review" | "scout" | "session";
  title: string | null; // desktop session title; null for tasks
  link: string | null; // claude://claude.ai/epitaxy/<sessionId>; null for tasks
  repo: string; // repository basename; see the session mapping in the contract
  branch: string | null;
  task_type: string | null; // e.g. "feature", "fix", "layout"
  executor: "codex" | "claude";
  account: string | null; // Codex account, or the Claude pool account for a Claude executor
  reviewer: "codex" | "claude" | null;
  model: string | null;
  effort: string | null;
  column: Column;
  phase: Phase;
  phase_started_at: number | null;
  alive: boolean; // task supervisor running, or session PID running with status busy
  turn: number; // turns so far
  fix_round: number; // automatic fix rounds in the current cycle
  outcome: string | null; // state.json "outcome" as written by the plugin
  result_status: string | null; // "done" | "partial" | "needs_decision" | "blocked" | null
  summary: string | null; // first line of the last result summary, ≤ 200 chars
  pr: string | null; // PR URL
  ci: string | null; // "pending" | "passed" | "failed" | "skipped" | "timeout" | "none" | null
  sleeping_until: number | null;
  created: number | null;
  updated_at: number; // task state mtime, or session lastActivityAt / 1000
  activity: Activity | null; // latest activity, only while alive
}

export type TaskFile = "brief" | "last" | "review" | "check";

export interface TaskDetail extends TaskSummary {
  timeline: { at: number; text: string }[]; // task events (≤ 200), or session creation and last activity
  files: TaskFile[]; // which files exist
}

export interface Accounts {
  fetched_at: number | null;
  error: string | null;
  data: unknown | null; // `accounts --json` output, passed through unchanged
}

export interface Gpu {
  name: string;
  utilization_percent: number | null;
  memory_used_bytes: number | null; // null on Apple: the memory is shared, there is no video memory line
  memory_total_bytes: number | null;
  processes: { name: string; memory_bytes: number }[];
}

export interface Metrics {
  os: string;
  cpu: { cores: number; load1: number; load5: number } | null;
  memory: { total_bytes: number; available_bytes: number } | null;
  gpus: Gpu[];
  disks: { mount: string; total_bytes: number; free_bytes: number }[];
}

export type MachineStatus = "free" | "busy" | "overloaded" | "offline" | "unknown";
export type MachineError = "timeout" | "unreachable" | "bad_output" | "probe_failed";

/** The hypervisor or host a machine runs on; shown for context, never part of the status. */
export interface HostView {
  name: string;
  cpu: Metrics["cpu"];
  memory: Metrics["memory"];
  error: MachineError | null;
}

export interface HistoryPoint {
  at: number;
  load_percent: number | null;
  memory_percent: number | null;
  gpu_percent: number | null;
}

export interface Machine {
  id: string;
  name: string;
  role: string | null;
  status: MachineStatus;
  reasons: string[]; // "cpu", "memory", "gpu", "gpu_memory", "disk:<mount>"
  checked_at: number | null; // last attempt
  sampled_at: number | null; // last success; the metrics stay after a failure
  error: MachineError | null;
  metrics: Metrics | null;
  host: HostView | null;
  history: HistoryPoint[]; // up to 120 points, one per 30 s
}

export interface Machines {
  config_error: string | null;
  items: Machine[];
}

export interface Snapshot {
  generated_at: number;
  tasks: TaskSummary[]; // all non-closed tasks, plus at most 50 closed records with the newest updated_at
  accounts: Accounts;
  machines?: Machines; // missing from an older server
}

// WebSocket /ws, server → client only.
export type ServerMessage =
  | { type: "snapshot"; snapshot: Snapshot }
  | { type: "task"; task: TaskSummary }
  | { type: "task_removed"; slug: string }
  | { type: "activity"; slug: string; activity: Activity }
  | { type: "accounts"; accounts: Accounts }
  | { type: "machines"; machines: Machines }
  | { type: "ping"; at: number };
