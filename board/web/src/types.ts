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

export interface Snapshot {
  generated_at: number;
  tasks: TaskSummary[]; // all non-closed tasks, plus at most 50 closed records with the newest updated_at
  accounts: Accounts;
}

// WebSocket /ws, server → client only.
export type ServerMessage =
  | { type: "snapshot"; snapshot: Snapshot }
  | { type: "task"; task: TaskSummary }
  | { type: "task_removed"; slug: string }
  | { type: "activity"; slug: string; activity: Activity }
  | { type: "accounts"; accounts: Accounts }
  | { type: "ping"; at: number };
