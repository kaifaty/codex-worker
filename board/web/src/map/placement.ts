// What a task's agent does on the island, from the task alone. The scene only draws the result.

import type { TaskSummary } from "../types";
import { MAX_BENCHES } from "./layout";

/** "bench" is the task's own workbench; the others are shared by all tasks of the island. */
export type Place = "bench" | "stand" | "review" | "gate" | "tent" | "dock";

export type Action =
  | "terminal" // at the terminal of its bench: activity "command"
  | "tool" // with a tool at its bench: activity "edit", or a fix
  | "think" // sits at its bench with a lamp: activity "thinking"
  | "stand" // stands at its bench without work
  | "wait" // waits at the test stand or the gate
  | "sit" // sits at the review table
  | "sleep" // sleeps at the tent
  | "carry"; // stands at the dock with a ready crate

export interface Placement {
  place: Place;
  /** The bench number at "bench"; at a shared place, the agent's position there, distinct among peers. */
  slot: number;
  action: Action;
  /** The agent's own work animation may play: the task is alive and its activity is fresh. */
  working: boolean;
  /** The place works for this task: the test stand during a check, the gate light during CI. */
  placeWorking: boolean;
  /** The activity is missing or older than FRESH_SECONDS; the label then shows its age. */
  stale: boolean;
  /** A red beacon over the bench: the task needs attention. */
  beacon: boolean;
  /** Family of the second agent at the review table. */
  companion: "codex" | "claude" | null;
}

/** Activity older than this does not animate the agent. */
export const FRESH_SECONDS = 60;

/** Whether the task's latest activity is at most FRESH_SECONDS old at `now`. */
export function isFresh(task: TaskSummary, now: number): boolean {
  const at = task.activity?.at;
  return at !== null && at !== undefined && now - at <= FRESH_SECONDS;
}

/** The place of a task that is shown on the map; null for a closed task. */
export function placeOf(task: TaskSummary): Place | null {
  switch (task.column) {
    case "closed":
      return null;
    case "sleeping":
      return "tent";
    case "review_me":
      return "dock";
    case "attention":
      return "bench";
    case "running":
      switch (task.phase) {
        case "check":
          return "stand";
        case "review":
          return "review";
        case "ci":
          return "gate";
        case "sleeping":
          return "tent";
        default:
          return "bench"; // turn, fix, idle
      }
  }
}

/** When the agent came to its place: the order of the queue at a shared place. */
function arrivedAt(task: TaskSummary): number {
  return task.alive ? (task.phase_started_at ?? task.updated_at) : task.updated_at;
}

function byArrival(a: TaskSummary, b: TaskSummary): number {
  return arrivedAt(a) - arrivedAt(b) || a.slug.localeCompare(b.slug);
}

/**
 * Where `task`'s agent is and what it does at `now` (Unix seconds). `peers` are the tasks shown on the
 * island in bench order (see `islandTasks`); they number the benches and share out the slots of the
 * shared places, earliest arrival first. Returns null for a closed task, which is not on the map.
 */
export function placement(task: TaskSummary, now: number, peers: readonly TaskSummary[] = [task]): Placement | null {
  const place = placeOf(task);
  if (place === null) return null;
  const list = peers.some((peer) => peer.slug === task.slug) ? peers : [...peers, task];

  let slot: number;
  if (place === "bench") {
    slot = list.findIndex((peer) => peer.slug === task.slug);
  } else {
    const here = list.filter((peer) => placeOf(peer) === place).sort(byArrival);
    slot = here.findIndex((peer) => peer.slug === task.slug);
  }

  const fresh = task.alive && isFresh(task, now);
  const result: Placement = {
    place,
    slot,
    action: "stand",
    working: false,
    placeWorking: false,
    stale: !isFresh(task, now),
    beacon: task.column === "attention",
    companion: null,
  };

  if (task.column === "sleeping" || place === "tent") return { ...result, action: "sleep" };
  if (task.column === "review_me") return { ...result, action: "carry" };
  if (task.column === "attention") return result;

  switch (task.phase) {
    case "check":
      return { ...result, action: "wait", placeWorking: task.alive };
    case "ci":
      return { ...result, action: "wait", placeWorking: task.alive };
    case "review":
      return { ...result, action: "sit", companion: task.reviewer };
    case "fix":
      return fresh ? { ...result, action: "tool", working: true } : result;
    case "turn": {
      if (!fresh) return result;
      switch (task.activity!.kind) {
        case "command":
          return { ...result, action: "terminal", working: true };
        case "edit":
          return { ...result, action: "tool", working: true };
        case "thinking":
          return { ...result, action: "think", working: true };
        default:
          return result; // message, tool: stands at the bench
      }
    }
    default:
      return result;
  }
}

const COLUMN_RANK: Record<TaskSummary["column"], number> = { running: 0, sleeping: 1, attention: 2, review_me: 3, closed: 4 };

/**
 * The tasks of island `repo`, closed ones left out. At most MAX_BENCHES are shown, running ones first and
 * then the most recently updated; `shown` is in bench order (oldest first), `hidden` are the rest.
 */
export function islandTasks(tasks: readonly TaskSummary[], repo: string): { shown: TaskSummary[]; hidden: TaskSummary[] } {
  const live = tasks.filter((task) => task.repo === repo && task.column !== "closed");
  const ranked = [...live].sort(
    (a, b) => COLUMN_RANK[a.column] - COLUMN_RANK[b.column] || b.updated_at - a.updated_at || a.slug.localeCompare(b.slug),
  );
  const shown = ranked
    .slice(0, MAX_BENCHES)
    .sort((a, b) => (a.created ?? a.updated_at) - (b.created ?? b.updated_at) || a.slug.localeCompare(b.slug));
  return { shown, hidden: ranked.slice(MAX_BENCHES) };
}

/** The island to show first: the repository with the most tasks that are not closed, then the most alive. */
export function defaultRepo(tasks: readonly TaskSummary[]): string | null {
  const counts = new Map<string, { live: number; alive: number }>();
  for (const task of tasks) {
    const count = counts.get(task.repo) ?? { live: 0, alive: 0 };
    if (task.column !== "closed") count.live += 1;
    if (task.alive) count.alive += 1;
    counts.set(task.repo, count);
  }
  let best: string | null = null;
  let bestCount = { live: -1, alive: -1 };
  for (const [repo, count] of [...counts].sort(([a], [b]) => a.localeCompare(b))) {
    if (count.live > bestCount.live || (count.live === bestCount.live && count.alive > bestCount.alive)) {
      best = repo;
      bestCount = count;
    }
  }
  return best;
}
