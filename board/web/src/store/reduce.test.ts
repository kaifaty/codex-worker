import { describe, expect, it } from "vitest";
import type { Accounts, Machines, Snapshot, TaskSummary } from "../types";
import { emptyState, reduce, type BoardState } from "./reduce";

const task = (slug: string, extra: Partial<TaskSummary> = {}): TaskSummary => ({
  slug, kind: "work", title: null, link: null, repo: "repo", branch: null, task_type: "fix", executor: "codex", account: "a",
  reviewer: null, model: null, effort: null, column: "running", phase: "turn", phase_started_at: 100,
  alive: true, turn: 1, fix_round: 0, outcome: "running", result_status: null, summary: null, pr: null,
  ci: null, sleeping_until: null, created: 90, updated_at: 100, activity: null, ...extra,
});
const accounts: Accounts = { fetched_at: 50, error: null, data: { codex: [] } };
const snapshot = (tasks: TaskSummary[]): Snapshot => ({ generated_at: 200, tasks, accounts });

const loaded = (): BoardState =>
  reduce(emptyState, { type: "snapshot", snapshot: snapshot([task("a"), task("b", { column: "attention", alive: false })]) }, 1);

describe("reduce", () => {
  it("snapshot replaces the whole board and clears highlights", () => {
    const before = reduce(loaded(), { type: "task", task: task("c") }, 5);
    const after = reduce(before, { type: "snapshot", snapshot: snapshot([task("z")]) }, 6);
    expect(Object.keys(after.tasks)).toEqual(["z"]);
    expect(after.loaded).toBe(true);
    expect(after.generatedAt).toBe(200);
    expect(after.accounts).toBe(accounts);
    expect(after.changedAt).toEqual({});
  });

  it("task adds a new task and marks it changed", () => {
    const state = loaded();
    const next = reduce(state, { type: "task", task: task("c") }, 7);
    expect(Object.keys(next.tasks).sort()).toEqual(["a", "b", "c"]);
    expect(next.changedAt).toEqual({ c: 7 });
    expect(Object.keys(state.tasks)).toEqual(["a", "b"]);
  });

  it("task replaces a changed task, including a column change", () => {
    const state = loaded();
    const moved = task("a", { column: "review_me", alive: false, result_status: "done", turn: 3 });
    const next = reduce(state, { type: "task", task: moved }, 8);
    expect(next.tasks.a).toBe(moved);
    expect(next.tasks.a?.column).toBe("review_me");
    expect(state.tasks.a?.column).toBe("running");
    expect(next.tasks.b).toBe(state.tasks.b);
  });

  it("task_removed drops the task and ignores an unknown slug", () => {
    const state = reduce(loaded(), { type: "task", task: task("c") }, 3);
    const next = reduce(state, { type: "task_removed", slug: "c" }, 4);
    expect(Object.keys(next.tasks).sort()).toEqual(["a", "b"]);
    expect(next.changedAt).toEqual({});
    expect(reduce(next, { type: "task_removed", slug: "nope" }, 5)).toBe(next);
  });

  it("activity updates the latest activity of a known task only", () => {
    const state = loaded();
    const activity = { at: 210, kind: "command" as const, text: "npm test" };
    const next = reduce(state, { type: "activity", slug: "a", activity }, 9);
    expect(next.tasks.a?.activity).toEqual(activity);
    expect(next.changedAt.a).toBe(9);
    expect(state.tasks.a?.activity).toBeNull();
    expect(reduce(state, { type: "activity", slug: "nope", activity }, 9)).toBe(state);
  });

  it("accounts replaces the account data", () => {
    const fresh: Accounts = { fetched_at: 300, error: "timeout", data: null };
    const next = reduce(loaded(), { type: "accounts", accounts: fresh }, 1);
    expect(next.accounts).toBe(fresh);
  });

  it("machines come from the snapshot, are null without the field and are replaced by a message", () => {
    expect(loaded().machines).toBeNull();
    const first: Machines = { config_error: null, items: [] };
    const withMachines = reduce(emptyState, { type: "snapshot", snapshot: { ...snapshot([]), machines: first } }, 1);
    expect(withMachines.machines).toBe(first);
    const next: Machines = { config_error: "bad toml", items: [] };
    const after = reduce(withMachines, { type: "machines", machines: next }, 2);
    expect(after.machines).toBe(next);
    expect(after.tasks).toBe(withMachines.tasks);
    expect(reduce(after, { type: "snapshot", snapshot: snapshot([]) }, 3).machines).toBeNull();
  });

  it("ping records the server time and keeps the tasks", () => {
    const state = loaded();
    const next = reduce(state, { type: "ping", at: 400 }, 1);
    expect(next.lastPing).toBe(400);
    expect(next.tasks).toBe(state.tasks);
  });
});
