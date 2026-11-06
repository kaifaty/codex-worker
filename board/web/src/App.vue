<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import type { Column, TaskSummary } from "./types";
import type { Board } from "./store/board";
import { useNow } from "./store/board";
import { COLUMNS, KIND_FILTERS, isKindFilter, matchesKind, type KindFilter } from "./lib/labels";
import { loadJson, saveJson } from "./lib/storage";
import AccountsPanel from "./components/AccountsPanel.vue";
import BoardColumn from "./components/BoardColumn.vue";
import MachinePanel from "./components/MachinePanel.vue";
import MachinesStrip from "./components/MachinesStrip.vue";
import TaskPanel from "./components/TaskPanel.vue";

const props = defineProps<{ board: Board }>();
const board = props.board;
const now = useNow();

type ExecutorFilter = "all" | "codex" | "claude";
interface Prefs {
  repo: string; // "" = all
  executor: ExecutorFilter;
  closedCollapsed: boolean;
  accountsCollapsed: boolean;
}
const PREFS_KEY = "codex-board.prefs.v1";
const defaults: Prefs = { repo: "", executor: "all", closedCollapsed: true, accountsCollapsed: false };
const isPrefs = (value: unknown): value is Prefs => {
  const v = value as Partial<Prefs> | null;
  return (
    !!v &&
    typeof v.repo === "string" &&
    (v.executor === "all" || v.executor === "codex" || v.executor === "claude") &&
    typeof v.closedCollapsed === "boolean" &&
    typeof v.accountsCollapsed === "boolean"
  );
};
const prefs = ref<Prefs>(loadJson(PREFS_KEY, defaults, isPrefs));
watch(prefs, (value) => saveJson(PREFS_KEY, value), { deep: true });

// Kept apart from Prefs so that saved prefs stay valid.
const MACHINES_KEY = "codex-board.machines-collapsed.v1";
const machinesCollapsed = ref(loadJson(MACHINES_KEY, false, (v): v is boolean => typeof v === "boolean"));
watch(machinesCollapsed, (value) => saveJson(MACHINES_KEY, value));

// Tasks, desktop sessions or both; kept apart from Prefs so that saved prefs stay valid.
const KIND_KEY = "codex-board.kind.v1";
const kindFilter = ref<KindFilter>(loadJson(KIND_KEY, "all", isKindFilter));
watch(kindFilter, (value) => saveJson(KIND_KEY, value));

type View = "board" | "map";
const VIEW_KEY = "codex-board.view.v1";
const view = ref<View>(loadJson(VIEW_KEY, "board", (v): v is View => v === "board" || v === "map"));
watch(view, (value) => saveJson(VIEW_KEY, value));
// The map and three.js load on first use, in their own chunk, and then stay mounted; the map stops
// drawing while the board is shown.
const MapView = shallowRef<(typeof import("./map/MapView.vue"))["default"] | null>(null);
const mapError = ref(false);
async function loadMap() {
  if (MapView.value) return;
  mapError.value = false;
  try {
    MapView.value = (await import("./map/MapView.vue")).default;
  } catch {
    mapError.value = true;
  }
}
watch(view, (value) => value === "map" && void loadMap(), { immediate: true });

const repos = computed(() => {
  const names = new Set(board.tasks.value.map((t) => t.repo));
  if (prefs.value.repo) names.add(prefs.value.repo);
  return [...names].sort((a, b) => a.localeCompare(b));
});

const visible = computed(() =>
  board.tasks.value.filter(
    (t) =>
      (!prefs.value.repo || t.repo === prefs.value.repo) &&
      matchesKind(t, kindFilter.value) &&
      (prefs.value.executor === "all" || t.executor === prefs.value.executor),
  ),
);

const order: Record<Column, (a: TaskSummary, b: TaskSummary) => number> = {
  // Stable orders, so that cards do not jump while the board is watched.
  running: (a, b) => (b.created ?? 0) - (a.created ?? 0) || a.slug.localeCompare(b.slug),
  sleeping: (a, b) => (a.sleeping_until ?? Infinity) - (b.sleeping_until ?? Infinity) || a.slug.localeCompare(b.slug),
  review_me: (a, b) => b.updated_at - a.updated_at,
  attention: (a, b) => b.updated_at - a.updated_at,
  closed: (a, b) => b.updated_at - a.updated_at,
};

const columns = computed(() =>
  COLUMNS.map((column) => ({
    ...column,
    tasks: visible.value.filter((t) => t.column === column.id).sort(order[column.id]),
  })),
);

const selected = ref<string | null>(null);
const selectedTask = computed(() => (selected.value ? (board.state.value.tasks[selected.value] ?? null) : null));

function open(slug: string) {
  selected.value = slug;
}

// The machine card works on both tabs; it and the task card replace each other on the board.
const machineId = ref<string | null>(null);
const selectedMachine = computed(
  () => board.state.value.machines?.items.find((machine) => machine.id === machineId.value) ?? null,
);
let machineOpener: HTMLElement | null = null;
function openMachine(id: string, opener: HTMLElement | null) {
  machineId.value = id;
  machineOpener = opener;
  selected.value = null;
}
watch(selected, (slug) => {
  if (slug) machineId.value = null;
});
function closeMachine() {
  const id = machineId.value;
  machineId.value = null;
  if (!id) return;
  const opener = machineOpener;
  machineOpener = null;
  void nextTick(() => {
    // The button that opened the card, else the machine's visible button if the strip changed meanwhile.
    const buttons = document.querySelectorAll<HTMLElement>(`[data-machine="${CSS.escape(id)}"] button`);
    const visible = (button: HTMLElement | null) => !!button?.isConnected && button.offsetParent !== null;
    const target = visible(opener) ? opener : [...buttons].find(visible);
    target?.focus();
  });
}

function close() {
  const slug = selected.value;
  selected.value = null;
  if (!slug) return;
  // Return focus to the card that opened the panel.
  void nextTick(() => {
    const card = document.querySelector<HTMLElement>(`[data-slug="${CSS.escape(slug)}"] .open`);
    card?.focus();
  });
}

const connectionText = computed(() => {
  switch (board.connection.value) {
    case "open":
      return "в эфире";
    case "reconnecting":
      return "переподключение…";
    default:
      return "подключение…";
  }
});
const retryText = computed(() =>
  board.connection.value === "reconnecting" && board.retryInMs.value !== null
    ? `повтор через ${Math.round(board.retryInMs.value / 1000)} с`
    : "",
);

const live = computed(() => board.tasks.value.filter((t) => t.column !== "closed").length);

onMounted(() => board.start());
onBeforeUnmount(() => board.stop());
</script>

<template>
  <div class="app" :class="{ 'closed-open': !prefs.closedCollapsed, 'map-view': view === 'map' }">
    <header class="topbar">
      <h1 class="title">Доска задач <span class="muted">codex-worker</span></h1>
      <div class="seg views" role="group" aria-label="Вид">
        <button type="button" class="btn" :aria-pressed="view === 'board'" @click="view = 'board'">Доска</button>
        <button type="button" class="btn" :aria-pressed="view === 'map'" @click="view = 'map'">Карта</button>
      </div>
      <span class="conn" :class="board.connection.value" role="status" :title="retryText">
        <span class="dot" aria-hidden="true"></span>{{ connectionText }}
        <span v-if="retryText" class="faint">{{ retryText }}</span>
      </span>
      <span v-if="board.source.label" class="chip warn">{{ board.source.label }}</span>
      <span v-if="board.state.value.loaded" class="faint num">{{ live }} в работе и ожидании</span>

      <div v-show="view === 'board'" class="filters" role="group" aria-label="Фильтры">
        <div class="seg kinds" role="group" aria-label="Задачи и сессии">
          <button
            v-for="option in KIND_FILTERS"
            :key="option.id"
            type="button"
            class="btn"
            :aria-pressed="kindFilter === option.id"
            @click="kindFilter = option.id"
          >
            {{ option.label }}
          </button>
        </div>
        <label class="repo">
          <span class="visually-hidden">Репозиторий</span>
          <select v-model="prefs.repo" class="btn" aria-label="Репозиторий">
            <option value="">Все репозитории</option>
            <option v-for="repo in repos" :key="repo" :value="repo">{{ repo }}</option>
          </select>
        </label>
        <div class="seg" role="group" aria-label="Исполнитель">
          <button
            v-for="option in [
              { id: 'all', label: 'Все' },
              { id: 'codex', label: '◆ Codex' },
              { id: 'claude', label: '✳ Claude' },
            ] as const"
            :key="option.id"
            type="button"
            class="btn"
            :class="option.id"
            :aria-pressed="prefs.executor === option.id"
            @click="prefs.executor = option.id"
          >
            {{ option.label }}
          </button>
        </div>
      </div>
    </header>

    <MachinesStrip
      :machines="board.state.value.machines"
      :now="now"
      :collapsed="machinesCollapsed"
      :selected="machineId"
      @toggle="machinesCollapsed = !machinesCollapsed"
      @open="openMachine"
    />

    <AccountsPanel
      v-show="view === 'board'"
      :accounts="board.state.value.accounts"
      :now="now"
      :collapsed="prefs.accountsCollapsed"
      @toggle="prefs.accountsCollapsed = !prefs.accountsCollapsed"
    />

    <main v-show="view === 'board'" class="board" :aria-busy="!board.state.value.loaded">
      <p v-if="!board.state.value.loaded" class="loading">Жду первый снимок доски…</p>
      <template v-else>
        <BoardColumn
          v-for="column in columns"
          :id="column.id"
          :key="column.id"
          :title="column.title"
          :tasks="column.tasks"
          :now="now"
          :changed-at="board.state.value.changedAt"
          :selected="selected"
          :collapsible="column.id === 'closed'"
          :collapsed="column.id === 'closed' && prefs.closedCollapsed"
          @open="open"
          @toggle="prefs.closedCollapsed = !prefs.closedCollapsed"
        />
      </template>
    </main>

    <TaskPanel
      v-if="selected && view === 'board'"
      :slug="selected"
      :task="selectedTask"
      :source="board.source"
      :now="now"
      @close="close"
    />

    <template v-if="view === 'map' || MapView">
      <component :is="MapView" v-if="MapView" v-show="view === 'map'" :board="board" :active="view === 'map'" @board="view = 'board'" />
      <p v-else-if="mapError" class="map-note">
        Не удалось загрузить карту. <a href="#" @click.prevent="loadMap">Повторить</a> ·
        <a href="#" @click.prevent="view = 'board'">Открыть доску</a>
      </p>
      <p v-else class="map-note">Загружаю карту…</p>
    </template>

    <MachinePanel v-if="machineId" :id="machineId" :machine="selectedMachine" :now="now" @close="closeMachine" />
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
}

.topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  white-space: nowrap;
}

.title .muted {
  font-weight: 500;
}

.views .btn {
  padding: 1px 10px;
}

.map-note {
  margin: 24px auto;
  color: var(--muted);
}

.conn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 9px 1px 7px;
  border-radius: 999px;
  background: var(--surface-2);
  font-size: 12px;
  white-space: nowrap;
}

.conn .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--faint);
}

.conn.open {
  background: var(--ok-soft);
  color: var(--ok);
}

.conn.open .dot {
  background: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent);
}

.conn.reconnecting {
  background: var(--warn-soft);
  color: var(--warn);
}

.conn.reconnecting .dot {
  background: var(--warn);
}

.faint {
  color: var(--faint);
  font-size: 12px;
  white-space: nowrap;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  min-width: 0;
}

.repo select {
  max-width: 240px;
  padding-right: 6px;
  text-overflow: ellipsis;
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

.seg .btn:focus-visible {
  position: relative;
  z-index: 1;
}

.seg .btn.codex:not([aria-pressed="true"]) {
  color: var(--codex);
}

.seg .btn.claude:not([aria-pressed="true"]) {
  color: var(--claude);
}

.board {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) 40px;
  gap: 8px;
  padding: 8px 12px 10px;
}

.closed-open .board {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}

.loading {
  grid-column: 1 / -1;
  margin: 24px auto;
  color: var(--muted);
}

/* Narrow screens: columns become vertical sections and the page scrolls. */
@media (max-width: 760px) {
  .app {
    height: auto;
    min-height: 100vh;
  }

  /* The map fills the screen instead. */
  .app.map-view {
    height: 100vh;
    height: 100dvh;
    min-height: 0;
  }

  .topbar {
    padding: 8px 10px;
  }

  .filters {
    margin-left: 0;
    width: 100%;
  }

  .repo {
    flex: 1;
    min-width: 0;
  }

  .repo select {
    width: 100%;
    max-width: none;
  }

  .board,
  .closed-open .board {
    display: flex;
    flex-direction: column;
    padding: 8px 8px 16px;
  }
}
</style>
