import { describe, expect, it } from "vitest";
import type { TaskSummary } from "../types";
import { FLASH_MS, ciChip, isFlashing, problemLabel } from "./labels";

const task = (extra: Partial<TaskSummary>): TaskSummary => ({
  slug: "t", kind: "work", repo: "r", branch: null, task_type: null, executor: "codex", account: null,
  reviewer: null, model: null, effort: null, column: "closed", phase: "idle", phase_started_at: null,
  alive: false, turn: 1, fix_round: 0, outcome: null, result_status: null, summary: null, pr: null,
  ci: null, sleeping_until: null, created: null, updated_at: 0, activity: null, ...extra,
});

describe("problemLabel", () => {
  it.each(["attention", "closed"] as const)("shows no chip for a successful task in %s", (column) => {
    expect(problemLabel(task({ column, outcome: "done", result_status: "done" }))).toBeNull();
    expect(problemLabel(task({ column, outcome: "completed", result_status: "done" }))).toBeNull();
    expect(problemLabel(task({ column, outcome: "completed" }))).toBeNull();
    expect(problemLabel(task({ column, outcome: "done" }))).toBeNull();
  });

  it("names real problems", () => {
    expect(problemLabel(task({ column: "attention", outcome: "completed", result_status: "needs_decision" }))).toBe("нужно решение");
    expect(problemLabel(task({ column: "closed", outcome: "conflict", result_status: "done" }))).toBe("конфликт");
    expect(problemLabel(task({ column: "attention", outcome: "interrupted" }))).toBe("прервана");
    expect(problemLabel(task({ column: "attention", outcome: "running" }))).toBe("надзиратель умер");
    expect(problemLabel(task({ column: "attention", outcome: "mystery" }))).toBe("mystery");
  });

  it("shows nothing outside attention and closed", () => {
    expect(problemLabel(task({ column: "review_me", outcome: "failed", result_status: "blocked" }))).toBeNull();
  });
});

describe("ciChip", () => {
  it("says CI was not waited for when nobody is waiting any more", () => {
    expect(ciChip(task({ ci: "pending", alive: false }))).toEqual({ label: "CI не дождались", tone: "muted" });
    expect(ciChip(task({ ci: "pending", alive: true }))).toEqual({ label: "CI идёт", tone: "warn" });
    expect(ciChip(task({ ci: "passed", alive: false }))?.label).toBe("CI прошёл");
    expect(ciChip(task({ ci: null }))).toBeNull();
  });
});

describe("isFlashing", () => {
  it("highlights only while the animation runs", () => {
    expect(isFlashing(undefined, 10_000)).toBe(false);
    expect(isFlashing(10_000, 10_000)).toBe(true);
    expect(isFlashing(10_000, 10_000 + FLASH_MS - 1)).toBe(true);
    expect(isFlashing(10_000, 10_000 + FLASH_MS)).toBe(false);
  });
});
