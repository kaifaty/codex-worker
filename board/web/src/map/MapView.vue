<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, onUpdated, ref, shallowRef, watch } from "vue";
import type { TaskSummary } from "../types";
import { createBoard, useNow, type Board } from "../store/board";
import { formatAge, formatDuration, formatUntil } from "../lib/format";
import { ACTIVITY, EXECUTORS, PHASES, prLabel, problemLabel, taskTitle } from "../lib/labels";
import TaskPanel from "../components/TaskPanel.vue";
import { defaultRepo, islandTasks, placement, type Placement } from "./placement";
import { NoWebGLError, createIslandScene, type IslandScene, type SceneEntry, type ZoomLevel } from "./scene";

const props = defineProps<{ board: Board; active: boolean }>();
const emit = defineEmits<{ board: [] }>();

// The clock stops while the map is hidden, so a hidden map does not re-render every second.
const clock = useNow();
const now = ref(clock.value);
watch(clock, (value) => {
  if (props.active) now.value = value;
});

// In `dev:fixture` mode a scenario can stand in for the board's source.
const demo = import.meta.env.MODE === "fixture";
const sourceKind = ref<"board" | "scenario">("board");
const scenario = shallowRef<Board | null>(null);
watch(sourceKind, async (kind) => {
  chosen.value = null;
  selected.value = null;
  if (kind !== "scenario" || scenario.value) return;
  // Written out so that a production build drops the scenario.
  if (import.meta.env.MODE === "fixture") {
    const { scenarioSource } = await import("./scenario");
    const board = createBoard(scenarioSource());
    board.start();
    scenario.value = board;
  }
});
const source = computed(() => (sourceKind.value === "scenario" && scenario.value ? scenario.value : props.board));
const tasks = computed(() => source.value.tasks.value);
const loaded = computed(() => source.value.state.value.loaded);

// Islands: one per repository; by default the one with the most tasks that are not closed.
const chosen = ref<string | null>(null);
const repos = computed(() => {
  const counts = new Map<string, number>();
  for (const task of tasks.value) counts.set(task.repo, (counts.get(task.repo) ?? 0) + (task.column === "closed" ? 0 : 1));
  return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([name, live]) => ({ name, live }));
});
const repo = computed(() =>
  chosen.value && repos.value.some((item) => item.name === chosen.value) ? chosen.value : defaultRepo(tasks.value),
);
const repoModel = computed({
  get: () => repo.value ?? "",
  set: (value: string) => {
    chosen.value = value;
  },
});

const island = computed(() => (repo.value ? islandTasks(tasks.value, repo.value) : { shown: [], hidden: [] }));
const entries = computed<SceneEntry[]>(() =>
  island.value.shown.map((task, bench) => ({ task, bench, placement: placement(task, now.value, island.value.shown)! })),
);

// Labels ------------------------------------------------------------------------------------------

const zoom = ref<ZoomLevel>("mid");
const glyph = (task: TaskSummary) => (task.executor === "codex" ? "◆" : "✳");

function phaseText(task: TaskSummary): string {
  switch (task.column) {
    case "sleeping":
      return PHASES.sleeping;
    case "review_me":
      return "ждёт тебя";
    case "attention":
      return problemLabel(task) ?? "нужно внимание";
    default: {
      const ticking = task.alive && task.phase_started_at !== null;
      return ticking ? `${PHASES[task.phase]} · ${formatDuration(now.value - task.phase_started_at!)}` : PHASES[task.phase];
    }
  }
}

function phaseClass(task: TaskSummary): string {
  if (task.column === "attention") return "p-attention";
  if (task.column === "review_me") return "p-ready";
  if (task.column === "sleeping") return "p-sleeping";
  return `p-${task.phase}`;
}

/** The agent's line: its latest activity and how old it is, or why it no longer works. */
function activityLine(task: TaskSummary, where: Placement): { icon: string; text: string; age: string; stale: boolean } {
  if (task.column === "review_me") {
    return { icon: "▣", text: task.pr ? `готово · ${prLabel(task.pr)}` : "готово", age: formatAge(task.updated_at, now.value), stale: false };
  }
  if (!task.alive) {
    const text = task.summary ?? problemLabel(task) ?? "надзиратель не запущен";
    return { icon: "!", text, age: formatAge(task.updated_at, now.value), stale: true };
  }
  const activity = task.activity;
  if (!activity) return { icon: "·", text: "нет активности", age: "", stale: true };
  // The age matters once the activity is stale: the agent stands still, and the label says since when.
  return {
    icon: ACTIVITY[activity.kind].icon,
    text: activity.text,
    age: where.stale && activity.at !== null ? formatAge(activity.at, now.value) : "",
    stale: where.stale,
  };
}

const labels = computed(() =>
  entries.value.map(({ task, placement: where }) => {
    const phase = phaseText(task);
    const line = activityLine(task, where);
    const who = `${EXECUTORS[task.executor]}${task.account ? ` · ${task.account}` : ""}`;
    return {
      task,
      name: taskTitle(task),
      where,
      // At its bench the agent shares the bench label; elsewhere it has its own.
      home: where.place === "bench",
      phase,
      phaseClass: phaseClass(task),
      line,
      who,
      title: [taskTitle(task), phase, who, `${line.text}${line.age ? ` · ${line.age}` : ""}`].join("\n"),
      wake: task.sleeping_until !== null ? formatUntil(task.sleeping_until, now.value) : "до сброса",
    };
  }),
);

const CAPTIONS = [
  { id: "stand", text: "стенд проверок" },
  { id: "review", text: "стол ревью" },
  { id: "gate", text: "ворота CI" },
  { id: "tent", text: "палатки" },
  { id: "dock", text: "причал" },
] as const;

const hiddenTitle = computed(() => island.value.hidden.map(taskTitle).join("\n"));

// Panel -------------------------------------------------------------------------------------------

const selected = ref<string | null>(null);
const selectedTask = computed(() => (selected.value ? (source.value.state.value.tasks[selected.value] ?? null) : null));

function open(slug: string) {
  selected.value = slug;
}

function close() {
  const slug = selected.value;
  selected.value = null;
  void nextTick(() => {
    const label = slug ? overlay.value?.querySelector<HTMLElement>(`[data-anchor="agent:${CSS.escape(slug)}"]`) : null;
    (label && label.style.visibility !== "hidden" ? label : host.value?.querySelector<HTMLElement>("canvas"))?.focus();
  });
}

// Scene -------------------------------------------------------------------------------------------

const host = ref<HTMLElement | null>(null);
const overlay = ref<HTMLElement | null>(null);
const toolbar = ref<HTMLElement | null>(null);
const failure = ref<"webgl" | "lost" | null>(null);
let scene: IslandScene | null = null;

function mount() {
  if (!host.value || !overlay.value) return;
  try {
    scene = createIslandScene({
      host: host.value,
      overlay: overlay.value,
      topInset: () => (toolbar.value ? toolbar.value.offsetTop + toolbar.value.offsetHeight : 0),
      onPick: open,
      onZoom: (level) => {
        zoom.value = level;
      },
      onLost: () => {
        failure.value = "lost";
        scene?.dispose();
        scene = null;
      },
    });
  } catch (error) {
    if (!(error instanceof NoWebGLError)) console.error(error);
    failure.value = "webgl";
    return;
  }
  failure.value = null;
  scene.setEntries(entries.value);
  scene.setSelected(selected.value);
  scene.refreshLabels();
  scene.setActive(props.active);
}

function retry() {
  failure.value = null;
  void nextTick(mount);
}

watch(entries, (next) => scene?.setEntries(next));
watch(selected, (slug) => scene?.setSelected(slug));
watch(
  () => props.active,
  (active) => {
    if (active) now.value = Date.now() / 1000;
    scene?.setActive(active);
    if (!active) selected.value = null;
  },
);
onMounted(mount);
onUpdated(() => scene?.refreshLabels());
onBeforeUnmount(() => {
  scene?.dispose();
  scene = null;
  scenario.value?.stop();
});
</script>

<template>
  <section class="map" aria-label="Карта острова">
    <div ref="toolbar" class="toolbar">
      <label class="island">
        <span>Остров</span>
        <select v-model="repoModel" class="btn" :disabled="!repos.length">
          <option v-if="!repos.length" value="">нет задач</option>
          <option v-for="item in repos" :key="item.name" :value="item.name">{{ item.name }} · {{ item.live }}</option>
        </select>
      </label>
      <div v-if="demo" class="seg" role="group" aria-label="Источник карты">
        <button type="button" class="btn" :aria-pressed="sourceKind === 'board'" @click="sourceKind = 'board'">Доска</button>
        <button type="button" class="btn" :aria-pressed="sourceKind === 'scenario'" @click="sourceKind = 'scenario'">Сценарий</button>
      </div>
      <span class="legend" aria-hidden="true">
        <span class="codex">◆ Codex</span>
        <span class="claude">✳ Claude</span>
      </span>
      <span class="hint">Перетаскивание — сдвиг, правая кнопка или два пальца — поворот, колесо — масштаб</span>
    </div>

    <div v-if="failure" class="fallback" role="alert">
      <p v-if="failure === 'webgl'">Карте нужен WebGL, а этот браузер его не даёт. Задачи видны на доске.</p>
      <p v-else>Видеокарта сбросила карту. Можно попробовать ещё раз или перейти на доску.</p>
      <p>
        <a href="#" @click.prevent="emit('board')">Открыть доску</a>
        <template v-if="failure === 'lost'"> · <a href="#" @click.prevent="retry">Повторить</a></template>
      </p>
    </div>

    <div v-show="!failure" ref="host" class="stage">
      <div ref="overlay" class="overlay" :class="`z-${zoom}`">
        <span v-for="caption in CAPTIONS" :key="caption.id" class="lbl caption" :data-anchor="`place:${caption.id}`" data-priority="7">
          {{ caption.text }}
        </span>
        <template v-for="label in labels" :key="label.task.slug">
          <button
            type="button"
            class="lbl bench"
            :class="[label.phaseClass, label.task.executor, { selected: selected === label.task.slug, home: label.home }]"
            :data-anchor="`bench:${label.task.slug}`"
            data-fit="bench"
            :data-priority="label.where.beacon ? 0 : selected === label.task.slug ? 1 : 2"
            :title="label.title"
            @click="open(label.task.slug)"
          >
            <span class="b-slug"><span v-if="label.where.beacon" class="alarm" aria-label="нужно внимание">!</span>{{ label.name }}</span>
            <span class="b-phase"><span class="dot" aria-hidden="true"></span>{{ label.phase }}</span>
            <template v-if="label.home">
              <span class="b-who"><span class="glyph" aria-hidden="true">{{ glyph(label.task) }}</span>{{ label.who }}</span>
              <span class="b-act" :class="{ stale: label.line.stale }">
                <span class="icon" aria-hidden="true">{{ label.line.icon }}</span>
                <span class="text">{{ label.line.text }}</span>
                <span v-if="label.line.age" class="age num">{{ label.line.age }}</span>
              </span>
            </template>
          </button>
          <button
            v-if="label.where.action === 'sleep'"
            type="button"
            class="lbl tent"
            :data-anchor="`tent:${label.task.slug}`"
            data-priority="3"
            :title="`${label.name}: спит, проснётся ${label.wake}`"
            @click="open(label.task.slug)"
          >
            ⏾ {{ label.wake }}
          </button>
          <button
            v-else-if="!label.home"
            type="button"
            class="lbl agent"
            :class="[label.task.executor, { stale: label.line.stale, selected: selected === label.task.slug }]"
            :data-anchor="`agent:${label.task.slug}`"
            data-align="below"
            :data-priority="selected === label.task.slug ? 1 : 4"
            :title="label.title"
            @click="open(label.task.slug)"
          >
            <span class="b-who"><span class="glyph" aria-hidden="true">{{ glyph(label.task) }}</span>{{ label.who }}</span>
            <span class="b-act" :class="{ stale: label.line.stale }">
              <span class="icon" aria-hidden="true">{{ label.line.icon }}</span>
              <span class="text">{{ label.line.text }}</span>
              <span v-if="label.line.age" class="age num">{{ label.line.age }}</span>
            </span>
          </button>
        </template>
        <span v-if="island.hidden.length" class="lbl more" data-anchor="more" data-align="below" data-priority="2" :title="hiddenTitle">
          ещё {{ island.hidden.length }}
        </span>
      </div>
      <p v-if="!loaded" class="status">Жду первый снимок доски…</p>
      <p v-else-if="!entries.length" class="status">На этом острове сейчас никого: все задачи закрыты.</p>
    </div>

    <TaskPanel
      v-if="selected && active"
      :slug="selected"
      :task="selectedTask"
      :source="source.source"
      :now="now"
      @close="close"
    />
  </section>
</template>

<style scoped>
.map {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.stage {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--surface-2);
}

.stage :deep(.map-canvas) {
  position: absolute;
  inset: 0;
  display: block;
  width: 100%;
  height: 100%;
  touch-action: none;
  outline: none;
}

.stage :deep(.map-canvas:focus-visible) {
  outline: 2px solid var(--focus);
  outline-offset: -2px;
}

.toolbar {
  position: absolute;
  top: 8px;
  left: 10px;
  right: 10px;
  z-index: 3;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  pointer-events: none;
}

.toolbar > * {
  pointer-events: auto;
}

.island {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 3px 2px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  font-weight: 600;
  min-width: 0;
}

.island select {
  max-width: 260px;
  padding: 1px 6px;
  text-overflow: ellipsis;
  font-weight: 400;
}

.seg {
  display: inline-flex;
}

.seg .btn {
  border-radius: 0;
  margin-left: -1px;
}

.seg .btn:first-child {
  border-radius: var(--radius) 0 0 var(--radius);
  margin-left: 0;
}

.seg .btn:last-child {
  border-radius: 0 var(--radius) var(--radius) 0;
}

.legend {
  display: inline-flex;
  gap: 8px;
  padding: 2px 8px;
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--surface) 85%, transparent);
  font-size: 12px;
  font-weight: 650;
}

.legend .codex {
  color: var(--codex);
}

.legend .claude {
  color: var(--claude);
}

.hint {
  margin-left: auto;
  padding: 2px 8px;
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--surface) 80%, transparent);
  color: var(--muted);
  font-size: 11.5px;
}

.status,
.fallback {
  max-width: min(440px, calc(100% - 32px));
  margin: 0;
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--muted);
  text-align: center;
  box-shadow: var(--shadow);
}

.status {
  position: absolute;
  left: 50%;
  top: 40%;
  z-index: 2;
  transform: translate(-50%, -50%);
}

.fallback {
  margin: 72px auto 0;
  color: var(--text);
}

.fallback p {
  margin: 4px 0;
}

/* Labels: positioned by the scene with transform; hidden until it places them. */
.overlay {
  position: absolute;
  inset: 0;
  z-index: 1;
  overflow: hidden;
  pointer-events: none;
}

.lbl {
  position: absolute;
  left: 0;
  top: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
  max-width: 190px;
  margin: 0;
  padding: 1px 6px 2px;
  border: 1px solid color-mix(in srgb, var(--border-strong) 70%, transparent);
  border-radius: 5px;
  background: color-mix(in srgb, var(--surface) 90%, transparent);
  color: var(--text);
  font: inherit;
  font-size: 11px;
  line-height: 15px;
  text-align: left;
  white-space: nowrap;
  visibility: hidden;
  pointer-events: auto;
  cursor: pointer;
  will-change: transform;
}

.lbl > span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.lbl:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}

.lbl.selected {
  border-color: var(--focus);
  box-shadow: 0 0 0 1px var(--focus);
}

.b-slug {
  font-weight: 650;
}

.alarm {
  display: inline-block;
  margin-right: 4px;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--bad);
  color: var(--surface);
  font-weight: 800;
}

.b-phase {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.b-phase .dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 4px;
  border-radius: 50%;
  background: var(--phase-idle);
  vertical-align: 0;
}

.p-turn .dot { background: var(--phase-turn); }
.p-check .dot { background: var(--phase-check); }
.p-review .dot { background: var(--phase-review); }
.p-fix .dot { background: var(--phase-fix); }
.p-ci .dot { background: var(--phase-ci); }
.p-sleeping .dot { background: var(--phase-sleeping); }
.p-ready .dot { background: var(--ok); }
.p-attention .dot { background: var(--bad); }

.bench.p-attention {
  border-color: var(--bad);
}

.bench.p-attention .b-phase {
  color: var(--bad);
}

.agent,
.bench.home {
  border-left: 3px solid var(--codex);
}

.agent.claude,
.bench.home.claude {
  border-left-color: var(--claude);
}

.agent {
  background: color-mix(in srgb, var(--surface) 84%, transparent);
}

.b-who {
  color: var(--muted);
  font-size: 10.5px;
  line-height: 14px;
}

.codex .glyph {
  color: var(--codex);
}

.claude .glyph {
  color: var(--claude);
}

.b-who .glyph {
  margin-right: 3px;
}

.lbl > .b-act {
  display: flex;
  gap: 4px;
  min-width: 0;
}

.b-act .icon {
  flex: none;
  color: var(--muted);
  font-family: var(--mono);
}

.b-act .text {
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.b-act .age {
  flex: none;
  color: var(--faint);
}

.b-act.stale .text {
  color: var(--muted);
}

.tent {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.caption {
  padding: 0 5px;
  border-color: transparent;
  background: color-mix(in srgb, var(--surface) 55%, transparent);
  color: var(--muted);
  font-size: 10.5px;
  line-height: 14px;
  letter-spacing: 0.02em;
  pointer-events: none;
}

.more {
  background: var(--surface);
  color: var(--muted);
  font-weight: 650;
  cursor: default;
}

/* What the labels show at each zoom: far — the slugs; middle — phases and activity; close — accounts too. */
.z-far .b-phase,
.z-far .b-act,
.z-far .b-who,
.z-far .agent,
.z-mid .bench .b-who {
  display: none;
}

.z-far .bench.home {
  border-left-width: 1px;
}

.z-far .lbl {
  padding: 0 4px;
}

@media (max-width: 760px) {
  .hint {
    display: none;
  }

  .island select {
    max-width: 52vw;
  }
}

@media (prefers-color-scheme: dark) {
  .stage {
    background: #262d40;
  }
}
</style>
