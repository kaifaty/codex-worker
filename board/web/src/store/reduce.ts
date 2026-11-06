import type { Accounts, Machines, ServerMessage, TaskSummary } from "../types";

export interface BoardState {
  /** False until the first snapshot arrives. */
  loaded: boolean;
  generatedAt: number | null;
  /** Tasks by slug. */
  tasks: Readonly<Record<string, TaskSummary>>;
  accounts: Accounts | null;
  /** null until a server that reports machines sends them. */
  machines: Machines | null;
  /** Server time of the last ping. */
  lastPing: number | null;
  /** Client time (ms) of the last live change of each task; drives the change highlight. */
  changedAt: Readonly<Record<string, number>>;
}

export const emptyState: BoardState = {
  loaded: false,
  generatedAt: null,
  tasks: {},
  accounts: null,
  machines: null,
  lastPing: null,
  changedAt: {},
};

function without<T>(record: Readonly<Record<string, T>>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * Applies one server message. Pure: never mutates `state`, returns `state` itself when nothing changes.
 * `now` is the client time in milliseconds and only feeds `changedAt`.
 */
export function reduce(state: BoardState, message: ServerMessage, now: number): BoardState {
  switch (message.type) {
    case "snapshot": {
      const tasks: Record<string, TaskSummary> = {};
      for (const task of message.snapshot.tasks) tasks[task.slug] = task;
      return {
        loaded: true,
        generatedAt: message.snapshot.generated_at,
        tasks,
        accounts: message.snapshot.accounts,
        machines: message.snapshot.machines ?? null,
        lastPing: state.lastPing,
        changedAt: {},
      };
    }
    case "task": {
      const task = message.task;
      return {
        ...state,
        tasks: { ...state.tasks, [task.slug]: task },
        changedAt: { ...state.changedAt, [task.slug]: now },
      };
    }
    case "task_removed": {
      if (!(message.slug in state.tasks)) return state;
      return {
        ...state,
        tasks: without(state.tasks, message.slug),
        changedAt: without(state.changedAt, message.slug),
      };
    }
    case "activity": {
      const task = state.tasks[message.slug];
      if (!task) return state;
      return {
        ...state,
        tasks: { ...state.tasks, [task.slug]: { ...task, activity: message.activity } },
        changedAt: { ...state.changedAt, [task.slug]: now },
      };
    }
    case "accounts":
      return { ...state, accounts: message.accounts };
    case "machines":
      return { ...state, machines: message.machines };
    case "ping":
      return { ...state, lastPing: message.at };
    default:
      // A newer server may send message types this client does not know yet.
      return state;
  }
}
