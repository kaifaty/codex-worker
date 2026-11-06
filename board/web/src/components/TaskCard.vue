<script setup lang="ts">
import { computed } from "vue";
import type { TaskSummary } from "../types";
import { formatAge, formatClock, formatDuration, formatFullTime, formatUntil } from "../lib/format";
import { ACTIVITY, EXECUTORS, KINDS, PHASES, ciChip, claudeLink, isFlashing, prLabel, problemLabel, taskTitle } from "../lib/labels";

const props = defineProps<{
  task: TaskSummary;
  now: number;
  changedAt: number | undefined;
  selected: boolean;
}>();

const emit = defineEmits<{ open: [slug: string] }>();

const t = computed(() => props.task);
const session = computed(() => t.value.kind === "session");
const title = computed(() => taskTitle(t.value));
const link = computed(() => claudeLink(t.value));
const ci = computed(() => ciChip(t.value));
// Checked once when the card is rendered; the key keeps a running animation from restarting.
const flashing = computed(() => isFlashing(props.changedAt, Date.now()));
const problem = computed(() => problemLabel(t.value));
const ticking = computed(() => t.value.alive && t.value.phase_started_at !== null && t.value.phase !== "sleeping");
const runLine = computed(() => [t.value.account, t.value.model, t.value.effort].filter(Boolean).join(" · "));
const where = computed(() => {
  const parts = [t.value.repo];
  if (t.value.task_type) parts.push(t.value.task_type);
  if (t.value.kind !== "work") parts.push(KINDS[t.value.kind]);
  return parts.join(" · ");
});
const activity = computed(() => (t.value.alive ? t.value.activity : null));
const showSummary = computed(() => !t.value.alive && !!t.value.summary);
</script>

<template>
  <article class="card" :class="[t.executor, { selected, dead: !t.alive, session }]" :data-slug="t.slug">
    <span v-if="flashing" :key="changedAt" class="flash" aria-hidden="true"></span>

    <div class="row head">
      <h3 class="slug">
        <button type="button" class="open" :title="session ? title : t.slug" @click="emit('open', t.slug)">{{ title }}</button>
      </h3>
      <span class="exec" :class="t.executor" :title="`Исполнитель: ${EXECUTORS[t.executor]}`">
        <span class="glyph" aria-hidden="true">{{ t.executor === "codex" ? "◆" : "✳" }}</span>{{ EXECUTORS[t.executor] }}
      </span>
    </div>

    <div class="row muted">
      <span class="ellipsis" :title="t.branch ? `${where}\n${t.branch}` : where">{{ where }}</span>
    </div>

    <div class="row">
      <span class="phase" :class="`p-${t.phase}`">
        <span class="dot" aria-hidden="true"></span>{{ PHASES[t.phase] }}
        <span v-if="ticking" class="num" :title="`с ${formatFullTime(t.phase_started_at!)}`">{{ formatDuration(now - t.phase_started_at!) }}</span>
      </span>
      <span class="counters num muted">
        <span title="Номер хода">ход {{ t.turn }}</span>
        <span v-if="t.fix_round > 0" title="Раунд исправления">· раунд {{ t.fix_round }}</span>
      </span>
    </div>

    <div v-if="runLine" class="row">
      <span class="mono muted ellipsis" :title="runLine">{{ runLine }}</span>
    </div>

    <div v-if="t.column === 'sleeping' && t.sleeping_until !== null" class="row wake">
      <span aria-hidden="true">⏾</span>
      <span class="ellipsis" :title="formatFullTime(t.sleeping_until)">
        проснётся {{ formatClock(t.sleeping_until, now) }} · {{ formatUntil(t.sleeping_until, now) }}
      </span>
    </div>

    <div v-if="ci || t.pr || problem || link" class="row chips">
      <span v-if="problem" class="chip bad">{{ problem }}</span>
      <span v-if="ci" class="chip" :class="ci.tone">{{ ci.label }}</span>
      <a v-if="t.pr" class="pr" :href="t.pr" target="_blank" rel="noopener" :title="t.pr">{{ prLabel(t.pr) }} ↗</a>
      <a v-if="link" class="pr app-link" :href="link" title="Открыть сессию в приложении Claude">Открыть в Claude ↗</a>
    </div>

    <div v-if="showSummary" class="row">
      <span class="ellipsis summary" :title="t.summary!">{{ t.summary }}</span>
    </div>

    <div v-if="t.alive && t.phase !== 'sleeping'" class="row activity">
      <template v-if="activity">
        <span class="kind" :title="ACTIVITY[activity.kind].label" aria-hidden="true">{{ ACTIVITY[activity.kind].icon }}</span>
        <span class="visually-hidden">{{ ACTIVITY[activity.kind].label }}:</span>
        <span class="ellipsis text" :title="activity.text">{{ activity.text }}</span>
        <span v-if="activity.at !== null" class="age num">{{ formatAge(activity.at, now) }}</span>
      </template>
      <span v-else class="faint">нет активности</span>
    </div>

    <div v-if="!t.alive" class="row foot">
      <span class="faint num" :title="formatFullTime(t.updated_at)">обновлено {{ formatAge(t.updated_at, now) }}</span>
    </div>
  </article>
</template>

<style scoped>
.card {
  position: relative;
  display: grid;
  gap: 3px;
  padding: 7px 9px 8px 10px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--codex);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
  min-width: 0;
}

.card.claude {
  border-left-color: var(--claude);
}

.card:hover {
  border-color: var(--border-strong);
}

.card.claude:hover {
  border-left-color: var(--claude);
}

.card.codex:hover {
  border-left-color: var(--codex);
}

.card.selected {
  border-color: var(--focus);
  box-shadow: 0 0 0 1px var(--focus);
}

.card:has(.open:focus-visible) {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}

.flash {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: var(--flash);
  pointer-events: none;
  animation: flash 2.4s ease-out forwards; /* FLASH_MS in lib/labels.ts */
}

@keyframes flash {
  from {
    opacity: 1;
  }
  to {
    opacity: 0;
  }
}

.row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.head {
  justify-content: space-between;
}

.slug {
  min-width: 0;
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  flex: 1;
}

.open {
  all: unset;
  display: block;
  box-sizing: border-box;
  width: 100%; /* a block button still shrinks to its text */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

/* The whole card opens the task; links inside stay clickable above it. */
.open::after {
  content: "";
  position: absolute;
  inset: 0;
}

.open:focus-visible {
  outline: none;
}

.pr {
  position: relative;
  z-index: 1;
  font-size: 12px;
  text-decoration: none;
  white-space: nowrap;
}

.pr:hover {
  text-decoration: underline;
}

.phase {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-weight: 550;
  white-space: nowrap;
  --c: var(--phase-idle);
}

.phase .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--c);
}

.p-turn { --c: var(--phase-turn); }
.p-check { --c: var(--phase-check); }
.p-review { --c: var(--phase-review); }
.p-fix { --c: var(--phase-fix); }
.p-ci { --c: var(--phase-ci); }
.p-sleeping { --c: var(--phase-sleeping); }
.p-idle { --c: var(--phase-idle); }

.counters {
  margin-left: auto;
  white-space: nowrap;
  font-size: 12px;
}

.chips {
  flex-wrap: wrap;
  row-gap: 3px;
}

.wake {
  color: var(--muted);
}

.summary {
  color: var(--text);
}

.activity {
  margin-top: 2px;
  padding-top: 4px;
  border-top: 1px dashed var(--border);
  font-size: 12px;
}

.activity .kind {
  flex: none;
  width: 14px;
  text-align: center;
  font-family: var(--mono);
  color: var(--muted);
}

.activity .text {
  flex: 1;
  font-family: var(--mono);
  font-size: 11.5px;
}

.activity .age,
.faint {
  flex: none;
  color: var(--faint);
  font-size: 11.5px;
  white-space: nowrap;
}

.dead .slug {
  font-weight: 550;
}

/* A desktop session: a quieter, flat card with a dashed outline, so it never reads as a task. */
.card.session {
  border-style: dashed;
  border-left-style: solid;
  border-left-color: color-mix(in srgb, var(--claude) 45%, var(--border));
  background: var(--surface-3);
  box-shadow: none;
}

.card.session:hover {
  border-left-color: color-mix(in srgb, var(--claude) 45%, var(--border));
}

.card.session.selected {
  border-color: var(--focus);
}

.app-link {
  margin-left: auto;
}
</style>
