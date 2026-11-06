import { describe, expect, it } from "vitest";
import type { Activity, TaskSummary } from "../types";
import { defaultRepo, islandTasks, placement, type Place } from "./placement";
import { MAX_BENCHES, attachOf, companionSpot, islandRadius, route, spotAt, type Point } from "./layout";

const NOW = 1_800_000_000;

function task(overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    slug: "t",
    kind: "work",
    title: null,
    link: null,
    repo: "repo",
    branch: null,
    task_type: null,
    executor: "codex",
    account: null,
    reviewer: "claude",
    model: null,
    effort: null,
    column: "running",
    phase: "turn",
    phase_started_at: NOW - 100,
    alive: true,
    turn: 1,
    fix_round: 0,
    outcome: "running",
    result_status: null,
    summary: null,
    pr: null,
    ci: null,
    sleeping_until: null,
    created: NOW - 1000,
    updated_at: NOW - 50,
    activity: { at: NOW - 5, kind: "command", text: "npm test" },
    ...overrides,
  };
}

const act = (kind: Activity["kind"], age = 5): Activity => ({ at: NOW - age, kind, text: kind });

describe("placement", () => {
  it.each([
    ["turn, command", task({ activity: act("command") }), "bench", "terminal", true],
    ["turn, edit", task({ activity: act("edit") }), "bench", "tool", true],
    ["turn, thinking", task({ activity: act("thinking") }), "bench", "think", true],
    ["turn, message", task({ activity: act("message") }), "bench", "stand", false],
    ["turn, tool", task({ activity: act("tool") }), "bench", "stand", false],
    ["check", task({ phase: "check" }), "stand", "wait", false],
    ["review", task({ phase: "review" }), "review", "sit", false],
    ["fix", task({ phase: "fix", activity: act("edit") }), "bench", "tool", true],
    ["ci", task({ phase: "ci", ci: "pending" }), "gate", "wait", false],
    ["sleeping", task({ column: "sleeping", phase: "sleeping", outcome: "sleeping", sleeping_until: NOW + 600 }), "tent", "sleep", false],
    ["review_me", task({ column: "review_me", phase: "idle", alive: false, result_status: "done", activity: null }), "dock", "carry", false],
    ["attention", task({ column: "attention", phase: "turn", alive: false, outcome: "failed", activity: null }), "bench", "stand", false],
  ] as const)("%s", (_, item, place, action, working) => {
    const result = placement(item, NOW)!;
    expect(result).toMatchObject({ place, action, working, slot: 0 });
    expect(result.beacon).toBe(item.column === "attention");
  });

  it("runs the shared place only while the task is alive, and seats the reviewer's family", () => {
    expect(placement(task({ phase: "check" }), NOW)!.placeWorking).toBe(true);
    expect(placement(task({ phase: "ci" }), NOW)!.placeWorking).toBe(true);
    expect(placement(task({ phase: "check", alive: false }), NOW)!.placeWorking).toBe(false);
    expect(placement(task({ phase: "turn" }), NOW)!.placeWorking).toBe(false);
    expect(placement(task({ phase: "review", reviewer: "codex" }), NOW)!.companion).toBe("codex");
    expect(placement(task({ phase: "review", reviewer: null }), NOW)!.companion).toBeNull();
  });

  it("does not animate stale or missing activity", () => {
    expect(placement(task({ activity: act("command", 60) }), NOW)).toMatchObject({ action: "terminal", working: true, stale: false });
    expect(placement(task({ activity: act("command", 61) }), NOW)).toMatchObject({ action: "stand", working: false, stale: true });
    expect(placement(task({ phase: "fix", activity: act("edit", 180) }), NOW)).toMatchObject({ action: "stand", working: false, stale: true });
    expect(placement(task({ activity: null }), NOW)).toMatchObject({ action: "stand", working: false, stale: true });
    expect(placement(task({ activity: { at: null, kind: "thinking", text: "?" } }), NOW)).toMatchObject({ action: "stand", working: false });
  });

  it("does not animate a task whose supervisor is gone", () => {
    for (const kind of ["command", "edit", "thinking"] as const) {
      expect(placement(task({ alive: false, activity: act(kind) }), NOW)).toMatchObject({ working: false, action: "stand" });
    }
    expect(placement(task({ alive: false, phase: "fix", activity: act("edit") }), NOW)!.working).toBe(false);
  });

  it("leaves closed tasks off the map", () => {
    expect(placement(task({ column: "closed", alive: false }), NOW)).toBeNull();
  });

  it("numbers benches by peer order and shares out slots by arrival", () => {
    const peers = [
      task({ slug: "a", phase: "check", phase_started_at: NOW - 30 }),
      task({ slug: "b" }),
      task({ slug: "c", phase: "check", phase_started_at: NOW - 90 }),
      task({ slug: "d", phase: "check", phase_started_at: NOW - 60 }),
      task({ slug: "e", phase: "review" }),
    ];
    const slots = Object.fromEntries(peers.map((peer) => [peer.slug, placement(peer, NOW, peers)!]));
    expect(slots.b).toMatchObject({ place: "bench", slot: 1 });
    expect([slots.c!.slot, slots.d!.slot, slots.a!.slot]).toEqual([0, 1, 2]);
    expect(slots.e).toMatchObject({ place: "review", slot: 0 });
    // The same arrival time falls back to the slug, so two tasks never share a slot.
    const tie = [task({ slug: "y", phase: "ci" }), task({ slug: "x", phase: "ci" })];
    expect(tie.map((peer) => placement(peer, NOW, tie)!.slot)).toEqual([1, 0]);
  });
});

describe("islands", () => {
  it("shows at most 12 benches and counts the rest", () => {
    const many = Array.from({ length: 15 }, (_, i) =>
      task({ slug: `t${String(i).padStart(2, "0")}`, created: NOW - 1000 + i, updated_at: NOW - i, column: i < 3 ? "review_me" : "running" }),
    );
    const { shown, hidden } = islandTasks([...many, task({ slug: "old", column: "closed" }), task({ slug: "other", repo: "x" })], "repo");
    expect(shown).toHaveLength(MAX_BENCHES);
    expect(hidden.map((t) => t.slug).sort()).toEqual(["t00", "t01", "t02"]);
    expect(shown.map((t) => t.slug)).toEqual([...shown].sort((a, b) => a.created! - b.created!).map((t) => t.slug));
  });

  it("keeps attention tasks on the island ahead of sleeping ones", () => {
    const sleeping = Array.from({ length: MAX_BENCHES }, (_, i) =>
      task({ slug: `s${i}`, column: "sleeping", alive: false, updated_at: 2000 + i }),
    );
    const { shown, hidden } = islandTasks([...sleeping, task({ slug: "stuck", column: "attention", alive: false, updated_at: 1000 })], "repo");
    expect(shown.map((t) => t.slug)).toContain("stuck");
    expect(hidden).toHaveLength(1);
    expect(hidden[0]?.column).toBe("sleeping");
  });

  it("starts on the repository with the most tasks that are not closed", () => {
    const tasks = [
      task({ slug: "1", repo: "b" }),
      task({ slug: "2", repo: "b", column: "attention", alive: false }),
      task({ slug: "3", repo: "a" }),
      task({ slug: "4", repo: "a", column: "closed", alive: false }),
      task({ slug: "5", repo: "a", column: "closed", alive: false }),
    ];
    expect(defaultRepo(tasks)).toBe("b");
    expect(defaultRepo([])).toBeNull();
  });
});

describe("layout", () => {
  const SHARED: Place[] = ["stand", "review", "gate", "tent", "dock"];
  const all: { key: string; at: Point }[] = [];
  for (let slot = 0; slot < MAX_BENCHES; slot++) all.push({ key: `bench ${slot}`, at: spotAt("bench", slot) });
  for (const place of SHARED) for (let slot = 0; slot < MAX_BENCHES; slot++) all.push({ key: `${place} ${slot}`, at: spotAt(place, slot) });
  for (let slot = 0; slot < MAX_BENCHES; slot++) {
    const seat = companionSpot(slot);
    if (seat) all.push({ key: `reviewer ${slot}`, at: seat });
  }

  it("never puts two agents in one spot, at any place, up to 12 each", () => {
    for (let i = 0; i < all.length; i++) {
      for (let j = i + 1; j < all.length; j++) {
        const d = Math.hypot(all[i]!.at.x - all[j]!.at.x, all[i]!.at.z - all[j]!.at.z);
        expect(d, `${all[i]!.key} and ${all[j]!.key}`).toBeGreaterThanOrEqual(0.8);
      }
    }
  });

  it("keeps every spot on the island, and links every place by a path", () => {
    for (const { key, at } of all) {
      // The pier reaches into the water; everything else is on land.
      if (!key.startsWith("dock")) expect(islandRadius(at), key).toBeLessThan(0.9);
    }
    for (const place of ["bench", ...SHARED] as Place[]) {
      const path = route(attachOf("bench", 0), attachOf(place, 3));
      expect(path.length).toBeGreaterThanOrEqual(1);
      expect(path[0]!.at).toEqual(attachOf("bench", 0).at);
      expect(path[path.length - 1]!.at).toEqual(attachOf(place, 3).at);
    }
  });
});
