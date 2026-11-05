<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { BoardSource } from "../lib/source";
import type { TaskDetail, TaskFile, TaskSummary } from "../types";
import { formatAge, formatClock, formatDuration, formatFullTime } from "../lib/format";
import { renderMarkdown } from "../lib/markdown";
import { COLUMNS, EXECUTORS, FILES, KINDS, PHASES, RESULT, CI, prLabel } from "../lib/labels";

const props = defineProps<{
  slug: string;
  /** The live summary from the board; null once the task is gone. */
  task: TaskSummary | null;
  source: BoardSource;
  now: number;
}>();
const emit = defineEmits<{ close: [] }>();

const detail = ref<TaskDetail | null>(null);
const detailError = ref<string | null>(null);
const notFound = ref(false);
const tab = ref<TaskFile | null>(null);
const fileText = ref<string | null>(null);
const fileState = ref<"idle" | "loading" | "missing" | "error">("idle");
const closeButton = ref<HTMLButtonElement | null>(null);
const cache = new Map<string, string | null>();

const shown = computed<TaskSummary | null>(() => props.task ?? detail.value);
const files = computed(() => (["brief", "last", "review", "check"] as const).filter((name) => detail.value?.files.includes(name)));

let detailRequest = 0;
async function loadDetail() {
  const request = ++detailRequest;
  try {
    const result = await props.source.fetchDetail(props.slug);
    if (request !== detailRequest) return;
    detailError.value = null;
    notFound.value = result === null;
    if (result) detail.value = result;
  } catch (error) {
    if (request !== detailRequest) return;
    detailError.value = error instanceof Error ? error.message : String(error);
  }
}

let fileRequest = 0;
async function loadFile() {
  const name = tab.value;
  const request = ++fileRequest;
  if (!name) {
    fileText.value = null;
    fileState.value = "idle";
    return;
  }
  const key = `${props.slug}\n${name}\n${props.task?.updated_at ?? detail.value?.updated_at ?? ""}`;
  if (cache.has(key)) {
    fileText.value = cache.get(key)!;
    fileState.value = fileText.value === null ? "missing" : "idle";
    return;
  }
  fileState.value = "loading";
  try {
    const text = await props.source.fetchFile(props.slug, name);
    if (request !== fileRequest) return;
    cache.set(key, text);
    fileText.value = text;
    fileState.value = text === null ? "missing" : "idle";
  } catch {
    if (request !== fileRequest) return;
    fileState.value = "error";
  }
}

const html = computed(() => (fileText.value !== null && tab.value !== "check" ? renderMarkdown(fileText.value) : ""));

watch(
  () => props.slug,
  () => {
    detail.value = null;
    notFound.value = false;
    tab.value = null;
    void loadDetail();
  },
  { immediate: true },
);
// Refresh the detail when the task changes on the board.
watch(
  () => props.task?.updated_at,
  (next, previous) => {
    if (next !== undefined && previous !== undefined && next !== previous) void loadDetail();
  },
);
watch(files, (list) => {
  if (!tab.value || !list.includes(tab.value)) tab.value = list[0] ?? null;
});
watch([tab, () => props.task?.updated_at], () => void loadFile());

function onKey(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  }
}

function onTabKey(event: KeyboardEvent) {
  const list = files.value;
  if (!tab.value || list.length < 2) return;
  const index = list.indexOf(tab.value);
  let next: number | null = null;
  if (event.key === "ArrowRight") next = (index + 1) % list.length;
  if (event.key === "ArrowLeft") next = (index - 1 + list.length) % list.length;
  if (next === null) return;
  event.preventDefault();
  tab.value = list[next]!;
  void nextTick(() => document.getElementById(`tab-${tab.value}`)?.focus());
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  void nextTick(() => closeButton.value?.focus());
});
onBeforeUnmount(() => window.removeEventListener("keydown", onKey));

const columnTitle = (id: string) => COLUMNS.find((c) => c.id === id)?.title ?? id;
</script>

<template>
  <aside class="panel" role="dialog" aria-modal="false" aria-labelledby="panel-title">
    <header class="p-head">
      <h2 id="panel-title" class="ellipsis" :title="slug">{{ slug }}</h2>
      <button ref="closeButton" type="button" class="btn close" title="Закрыть (Esc)" @click="emit('close')">
        <span aria-hidden="true">✕</span><span class="visually-hidden">Закрыть</span>
      </button>
    </header>

    <div class="p-body">
      <p v-if="!task" class="note">Задачи больше нет на доске; показаны последние данные.</p>
      <p v-if="detailError" class="note bad">Не удалось загрузить задачу: {{ detailError }}</p>
      <p v-else-if="notFound && !shown" class="note">Задача не найдена.</p>

      <dl v-if="shown" class="fields">
        <dt>Колонка</dt>
        <dd>{{ columnTitle(shown.column) }}</dd>
        <dt>Исполнитель</dt>
        <dd>
          <span class="exec" :class="shown.executor"><span class="glyph" aria-hidden="true">{{ shown.executor === "codex" ? "◆" : "✳" }}</span>{{ EXECUTORS[shown.executor] }}</span>
          <span v-if="shown.reviewer" class="muted"> · ревьюер {{ EXECUTORS[shown.reviewer] }}</span>
        </dd>
        <dt>Аккаунт</dt>
        <dd class="mono ellipsis" :title="shown.account ?? ''">{{ shown.account ?? "—" }}</dd>
        <dt>Модель</dt>
        <dd class="mono ellipsis">{{ [shown.model, shown.effort].filter(Boolean).join(" · ") || "—" }}</dd>
        <dt>Репозиторий</dt>
        <dd class="ellipsis" :title="shown.repo">{{ shown.repo }}</dd>
        <dt>Ветка</dt>
        <dd class="mono ellipsis" :title="shown.branch ?? ''">{{ shown.branch ?? "—" }}</dd>
        <dt>Тип</dt>
        <dd>{{ KINDS[shown.kind] }}<template v-if="shown.task_type"> · {{ shown.task_type }}</template></dd>
        <dt>Фаза</dt>
        <dd class="num">
          {{ PHASES[shown.phase] }}
          <template v-if="shown.alive && shown.phase_started_at !== null"> · {{ formatDuration(now - shown.phase_started_at) }}</template>
        </dd>
        <dt>Ходы</dt>
        <dd class="num">{{ shown.turn }}<template v-if="shown.fix_round > 0"> · раунд исправления {{ shown.fix_round }}</template></dd>
        <dt>Надзиратель</dt>
        <dd>{{ shown.alive ? "работает" : "не запущен" }}<span v-if="shown.outcome" class="muted mono"> · {{ shown.outcome }}</span></dd>
        <template v-if="shown.result_status">
          <dt>Итог</dt>
          <dd>{{ RESULT[shown.result_status] ?? shown.result_status }}</dd>
        </template>
        <template v-if="shown.summary">
          <dt>Сводка</dt>
          <dd class="wrap">{{ shown.summary }}</dd>
        </template>
        <template v-if="shown.pr">
          <dt>PR</dt>
          <dd class="ellipsis"><a :href="shown.pr" target="_blank" rel="noopener" :title="shown.pr">{{ prLabel(shown.pr) }} ↗</a></dd>
        </template>
        <template v-if="shown.ci">
          <dt>CI</dt>
          <dd>{{ CI[shown.ci]?.label ?? shown.ci }}</dd>
        </template>
        <template v-if="shown.sleeping_until !== null">
          <dt>Проснётся</dt>
          <dd class="num" :title="formatFullTime(shown.sleeping_until)">{{ formatClock(shown.sleeping_until, now) }}</dd>
        </template>
        <template v-if="shown.activity">
          <dt>Сейчас</dt>
          <dd class="wrap mono">{{ shown.activity.text }}<span v-if="shown.activity.at !== null" class="muted num"> · {{ formatAge(shown.activity.at, now) }}</span></dd>
        </template>
        <template v-if="shown.created !== null">
          <dt>Создана</dt>
          <dd class="num" :title="formatFullTime(shown.created)">{{ formatClock(shown.created, now) }} · {{ formatAge(shown.created, now) }}</dd>
        </template>
        <dt>Обновлена</dt>
        <dd class="num" :title="formatFullTime(shown.updated_at)">{{ formatAge(shown.updated_at, now) }}</dd>
      </dl>

      <section v-if="detail && detail.timeline.length" class="timeline" aria-labelledby="timeline-title">
        <h3 id="timeline-title">Хронология</h3>
        <ol>
          <li v-for="(entry, index) in detail.timeline" :key="index">
            <time class="num" :title="formatFullTime(entry.at)">{{ formatClock(entry.at, now) }}</time>
            <span class="wrap">{{ entry.text }}</span>
          </li>
        </ol>
      </section>

      <section v-if="files.length" class="files">
        <div class="tabs" role="tablist" aria-label="Файлы задачи" @keydown="onTabKey">
          <button
            v-for="name in files"
            :id="`tab-${name}`"
            :key="name"
            type="button"
            role="tab"
            class="btn"
            :aria-selected="tab === name"
            :aria-controls="`tabpanel-${name}`"
            :tabindex="tab === name ? 0 : -1"
            @click="tab = name"
          >
            {{ FILES[name] }}
          </button>
        </div>
        <div v-if="tab" :id="`tabpanel-${tab}`" class="tabpanel" role="tabpanel" :aria-labelledby="`tab-${tab}`" tabindex="0">
          <p v-if="fileState === 'loading' && fileText === null" class="note">Загрузка…</p>
          <p v-else-if="fileState === 'missing'" class="note">Файла нет.</p>
          <p v-else-if="fileState === 'error'" class="note bad">Не удалось загрузить файл.</p>
          <pre v-else-if="tab === 'check' && fileText !== null" class="log mono">{{ fileText }}</pre>
          <!-- markdown-it runs with html: false, so task text cannot inject markup. -->
          <div v-else-if="fileText !== null" class="md" v-html="html"></div>
        </div>
      </section>
      <p v-else-if="detail" class="note">Файлов задачи нет.</p>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  width: min(600px, 100vw);
  border-left: 1px solid var(--border);
  background: var(--surface);
  box-shadow: var(--shadow-panel);
}

.p-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px 10px 16px;
  border-bottom: 1px solid var(--border);
}

.p-head h2 {
  flex: 1;
  margin: 0;
  font-size: 15px;
  font-weight: 650;
}

.close {
  flex: none;
  padding: 3px 8px;
}

.p-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 16px 24px;
}

.note {
  margin: 0 0 8px;
  padding: 4px 8px;
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--muted);
}

.note.bad {
  background: var(--bad-soft);
  color: var(--bad);
}

.fields {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 3px 14px;
  margin: 0 0 14px;
}

.fields dt {
  color: var(--muted);
}

.fields dd {
  margin: 0;
  min-width: 0;
}

.wrap {
  overflow-wrap: anywhere;
}

.timeline h3 {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 650;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--muted);
}

.timeline ol {
  margin: 0 0 14px;
  padding: 0;
  list-style: none;
  border-left: 2px solid var(--border);
}

.timeline li {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 10px;
  padding: 2px 0 2px 10px;
}

.timeline time {
  color: var(--faint);
  white-space: nowrap;
}

.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}

.tabpanel {
  min-width: 0;
  border-radius: var(--radius);
}

.log {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-3);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11.5px;
  line-height: 1.45;
}

.md {
  overflow-wrap: anywhere;
  line-height: 1.5;
}

.md :deep(h1),
.md :deep(h2),
.md :deep(h3) {
  margin: 14px 0 6px;
  font-size: 14px;
}

.md :deep(h1) {
  font-size: 16px;
}

.md :deep(p),
.md :deep(ul),
.md :deep(ol) {
  margin: 6px 0;
}

.md :deep(ul),
.md :deep(ol) {
  padding-left: 20px;
}

.md :deep(code) {
  padding: 0 3px;
  border-radius: 3px;
  background: var(--surface-2);
  font-family: var(--mono);
  font-size: 12px;
}

.md :deep(pre) {
  padding: 8px 10px;
  border-radius: var(--radius);
  background: var(--surface-3);
  border: 1px solid var(--border);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.md :deep(pre code) {
  padding: 0;
  background: none;
}

.md :deep(table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
}

.md :deep(th),
.md :deep(td) {
  padding: 3px 8px;
  border: 1px solid var(--border);
}

.md :deep(blockquote) {
  margin: 6px 0;
  padding-left: 10px;
  border-left: 3px solid var(--border);
  color: var(--muted);
}
</style>
