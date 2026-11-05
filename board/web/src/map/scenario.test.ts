import { describe, expect, it } from "vitest";
import { placement } from "./placement";
import { SCENARIO_CYCLE, SCENARIO_STEP_SECONDS, scenarioTasks } from "./scenario";

const START = 1_800_000_000;

describe("scenario", () => {
  const frames = Array.from({ length: SCENARIO_CYCLE + 1 }, (_, step) => ({
    now: START + step * SCENARIO_STEP_SECONDS + 1,
    tasks: scenarioTasks(step, START),
  }));

  it("takes every agent through every place and action of the map", () => {
    const seen = new Map<string, Set<string>>();
    for (const { now, tasks } of frames) {
      for (const task of tasks) {
        const where = placement(task, now, tasks)!;
        const marks = seen.get(task.slug) ?? new Set<string>();
        marks.add(`${where.place}/${where.action}${where.beacon ? "/beacon" : ""}`);
        if (task.phase === "turn" && task.activity && task.alive) marks.add(`turn/${task.activity.kind}${where.stale ? "/stale" : ""}`);
        marks.add(`column/${task.column}`);
        marks.add(`phase/${task.phase}`);
        seen.set(task.slug, marks);
      }
    }
    expect([...seen.keys()]).toHaveLength(3);
    for (const marks of seen.values()) {
      for (const mark of [
        "turn/command", "turn/edit", "turn/thinking", "turn/message", "turn/tool", "turn/message/stale",
        "bench/terminal", "bench/tool", "bench/think", "bench/stand",
        "stand/wait", "review/sit", "gate/wait", "tent/sleep", "dock/carry", "bench/stand/beacon",
        "phase/check", "phase/review", "phase/fix", "phase/ci",
        "column/running", "column/sleeping", "column/review_me", "column/attention",
      ]) {
        expect(marks, mark).toContain(mark);
      }
    }
    const executors = new Set(frames[0]!.tasks.map((task) => `${task.kind}/${task.executor}`));
    expect(executors).toEqual(new Set(["work/codex", "work/claude", "review/codex"]));
  });

  it("changes one agent per step and comes back round", () => {
    for (let step = 1; step < frames.length; step++) {
      const before = frames[step - 1]!.tasks;
      const after = frames[step]!.tasks;
      const changed = after.filter((task, index) => task.updated_at !== before[index]!.updated_at);
      expect(changed, `step ${step}`).toHaveLength(1);
      expect(changed[0]!.updated_at).toBe(START + step * SCENARIO_STEP_SECONDS);
    }
    const state = (step: number) => frames[step]!.tasks.map((task) => [task.column, task.phase, task.activity?.kind]);
    expect(state(SCENARIO_CYCLE)).toEqual(state(0));
    // Benches keep their order: the creation times never move.
    expect(frames.every(({ tasks }) => tasks.every((task, index) => task.created === frames[0]!.tasks[index]!.created))).toBe(true);
  });
});
