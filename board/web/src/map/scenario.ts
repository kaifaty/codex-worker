// Demo for `npm run dev:fixture`: three agents (Codex, Claude and a reviewer) go round every place of the
// island. One of them changes every SCENARIO_STEP_SECONDS, in turn, so every place and every walk shows up
// without real tasks.

import type { BoardSource } from "../lib/source";
import type { Activity, Column, Phase, TaskDetail, TaskSummary } from "../types";

export const SCENARIO_REPO = "demo-island";
export const SCENARIO_STEP_SECONDS = 4;

interface Stage {
  column: Column;
  phase: Phase;
  alive: boolean;
  /** The latest activity and its age when the stage starts; null when there is none. */
  activity: { kind: Activity["kind"]; text: string; age?: number } | null;
  note: string;
  turn: number;
  fix_round?: number;
  ci?: string;
  pr?: boolean;
  result_status?: string;
  outcome?: string;
  summary?: string;
  /** Seconds from the start of the stage to the wake-up. */
  sleep?: number;
}

interface Agent {
  slug: string;
  kind: TaskSummary["kind"];
  executor: TaskSummary["executor"];
  reviewer: TaskSummary["reviewer"];
  account: string;
  model: string;
  effort: string;
  task_type: string | null;
  /** The stage at step 0, so that the agents start at different places. */
  first: number;
  /** Words of this agent's activity lines. */
  words: { file: string; command: string; idea: string };
}

const AGENTS: readonly Agent[] = [
  {
    slug: "island-scene",
    kind: "work",
    executor: "codex",
    reviewer: "claude",
    account: "codex-team-2",
    model: "gpt-5.5-codex",
    effort: "high",
    task_type: "feature",
    first: 0,
    words: { file: "src/map/scene.ts", command: "npm run build", idea: "Где поставить стенд проверок" },
  },
  {
    slug: "label-overlap-fix",
    kind: "work",
    executor: "claude",
    reviewer: "codex",
    account: "claude-pool-1",
    model: "claude-opus-5-5",
    effort: "medium",
    task_type: "fix",
    first: 5,
    words: { file: "src/map/MapView.vue", command: "npm test -- --run", idea: "Почему подписи наезжают друг на друга" },
  },
  {
    slug: "review-pr-118",
    kind: "review",
    executor: "codex",
    reviewer: "claude",
    account: "codex-team-1",
    model: "gpt-5.5",
    effort: "high",
    task_type: null,
    first: 9,
    words: { file: "board/web/src/App.vue", command: "git diff origin/main...HEAD", idea: "Проверяю, что доска не изменилась" },
  },
];

/** The stages every agent goes through, in a circle: every row of the placement table, and a stale turn. */
function stages(words: Agent["words"]): Stage[] {
  const running = { column: "running", alive: true } as const;
  return [
    { ...running, phase: "turn", turn: 1, note: "ход 1", activity: { kind: "command", text: words.command } },
    { ...running, phase: "turn", turn: 1, note: "правка", activity: { kind: "edit", text: `Edit ${words.file}` } },
    { ...running, phase: "turn", turn: 1, note: "размышляет", activity: { kind: "thinking", text: words.idea } },
    { ...running, phase: "turn", turn: 1, note: "сообщение", activity: { kind: "message", text: "Готово, отдаю на проверку" } },
    { ...running, phase: "check", turn: 1, note: "проверка", activity: { kind: "message", text: "Готово, отдаю на проверку", age: 4 } },
    { ...running, phase: "review", turn: 1, note: "ревью", activity: { kind: "message", text: "Проверка прошла, жду ревью", age: 2 } },
    { ...running, phase: "fix", turn: 2, fix_round: 1, note: "исправление", activity: { kind: "edit", text: `Edit ${words.file}: замечания ревью` } },
    { ...running, phase: "check", turn: 2, fix_round: 1, note: "проверка", activity: { kind: "message", text: "Исправил замечания", age: 3 } },
    { ...running, phase: "ci", turn: 2, pr: true, ci: "pending", note: "CI", activity: { kind: "message", text: "Запушил ветку, жду CI", age: 2 } },
    {
      column: "review_me", phase: "idle", alive: false, turn: 2, pr: true, ci: "passed", note: "ждёт ревью",
      result_status: "done", outcome: "done", summary: "Готово, PR ждёт ревью", activity: null,
    },
    { ...running, phase: "turn", turn: 3, pr: true, ci: "passed", note: "ход 3", activity: { kind: "tool", text: "Read src/types.ts" } },
    {
      ...running, phase: "turn", turn: 3, pr: true, ci: "passed", note: "ход 3 молчит",
      activity: { kind: "message", text: "Жду ответа от сервера", age: 200 },
    },
    {
      column: "sleeping", phase: "sleeping", alive: true, turn: 3, pr: true, outcome: "sleeping", sleep: 25 * 60,
      note: "сон до сброса лимита", activity: { kind: "message", text: "Лимит аккаунта, сплю до сброса", age: 5 },
    },
    {
      column: "attention", phase: "idle", alive: false, turn: 3, pr: true, note: "нужно решение",
      result_status: "needs_decision", outcome: "done", summary: "Нужно решение: где показывать закрытые задачи", activity: null,
    },
  ];
}

const STAGES = AGENTS.map((agent) => stages(agent.words));
export const SCENARIO_STAGES = STAGES[0]!.length;
/** Steps until every agent is back where it started. */
export const SCENARIO_CYCLE = SCENARIO_STAGES * AGENTS.length;

/** How many times agent `index` has changed by step `step`: step k ≥ 1 moves agent (k - 1) mod 3. */
function moves(index: number, step: number): number {
  return step > index ? Math.floor((step - 1 - index) / AGENTS.length) + 1 : 0;
}

/** The step at which agent `index` last changed, 0 for its first stage. */
function lastChange(index: number, step: number): number {
  const count = moves(index, step);
  return count === 0 ? 0 : index + 1 + (count - 1) * AGENTS.length;
}

function build(index: number, stageIndex: number, changedAt: number, start: number): TaskSummary {
  const agent = AGENTS[index]!;
  const stage = STAGES[index]![stageIndex]!;
  return {
    slug: agent.slug,
    kind: agent.kind,
    title: null,
    link: null,
    repo: SCENARIO_REPO,
    branch: agent.kind === "work" ? `codex/${agent.slug}` : null,
    task_type: agent.task_type,
    executor: agent.executor,
    account: agent.account,
    reviewer: agent.reviewer,
    model: agent.model,
    effort: agent.effort,
    column: stage.column,
    phase: stage.phase,
    phase_started_at: changedAt,
    alive: stage.alive,
    turn: stage.turn,
    fix_round: stage.fix_round ?? 0,
    outcome: stage.outcome ?? (stage.alive ? "running" : null),
    result_status: stage.result_status ?? null,
    summary: stage.summary ?? null,
    pr: stage.pr ? `https://github.com/example/demo-island/pull/${118 + index}` : null,
    ci: stage.ci ?? null,
    sleeping_until: stage.sleep !== undefined ? changedAt + stage.sleep : null,
    created: start - 3600 + index * 60,
    updated_at: changedAt,
    activity:
      stage.alive && stage.activity
        ? { at: changedAt - (stage.activity.age ?? 0), kind: stage.activity.kind, text: stage.activity.text }
        : null,
  };
}

/** The three tasks at step `step` of a scenario that started at `start` (Unix seconds). */
export function scenarioTasks(step: number, start: number): TaskSummary[] {
  return AGENTS.map((agent, index) => {
    const changed = lastChange(index, step);
    // The first stages started a little before the scenario, so their clocks do not all read zero.
    const changedAt = changed === 0 ? start - 20 - index * 35 : start + changed * SCENARIO_STEP_SECONDS;
    return build(index, (agent.first + moves(index, step)) % SCENARIO_STAGES, changedAt, start);
  });
}

function timeline(index: number, step: number, start: number): { at: number; text: string }[] {
  const agent = AGENTS[index]!;
  const entries = [{ at: start - 3600 + index * 60, text: "демо-задача создана" }];
  const count = moves(index, step);
  for (let move = Math.max(0, count - SCENARIO_STAGES + 1); move <= count; move++) {
    const at = move === 0 ? start - 20 - index * 35 : start + (index + 1 + (move - 1) * AGENTS.length) * SCENARIO_STEP_SECONDS;
    entries.push({ at, text: STAGES[index]![(agent.first + move) % SCENARIO_STAGES]!.note });
  }
  return entries;
}

const BRIEF = `# Демо-сценарий карты

Три агента — Codex, Claude и ревьюер — по кругу проходят все места острова: рабочее место, стенд
проверок, стол ревью, ворота CI, причал, палатку и красный маяк. Каждые ${SCENARIO_STEP_SECONDS} с меняется
один из них. Это не настоящие задачи: сценарий есть только в режиме \`npm run dev:fixture\`.
`;

/** A source that plays the scenario, for the map's demo mode. */
export function scenarioSource(): BoardSource {
  let start = Date.now() / 1000;
  let step = 0;
  let tasks: TaskSummary[] = [];
  return {
    label: "сценарий",
    connect(onMessage, onState) {
      start = Date.now() / 1000;
      step = 0;
      tasks = scenarioTasks(step, start);
      onState("connecting", null);
      onState("open", null);
      onMessage({
        type: "snapshot",
        snapshot: { generated_at: start, tasks, accounts: { fetched_at: null, error: null, data: null } },
      });
      const timer = setInterval(() => {
        step += 1;
        const next = scenarioTasks(step, start);
        for (const task of next) {
          const previous = tasks.find((item) => item.slug === task.slug);
          if (!previous || previous.updated_at !== task.updated_at) onMessage({ type: "task", task });
        }
        tasks = next;
      }, SCENARIO_STEP_SECONDS * 1000);
      return () => clearInterval(timer);
    },
    async fetchDetail(slug) {
      const index = tasks.findIndex((task) => task.slug === slug);
      if (index < 0) return null;
      const detail: TaskDetail = { ...tasks[index]!, timeline: timeline(index, step, start), files: ["brief"] };
      return detail;
    },
    async fetchFile(slug, name) {
      return name === "brief" && tasks.some((task) => task.slug === slug) ? BRIEF : null;
    },
  };
}
