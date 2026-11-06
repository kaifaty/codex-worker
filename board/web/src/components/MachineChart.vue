<script setup lang="ts">
import { computed, ref } from "vue";
import type { HistoryPoint } from "../types";
import { formatAge } from "../lib/format";
import { CHART_WINDOW, chart, chartPath, formatNumber, type HistoryKey } from "../lib/machines";

const props = defineProps<{ history: HistoryPoint[]; field: HistoryKey; title: string; now: number; staleAge?: string | null }>();

const W = 300;
const H = 60;
const data = computed(() => chart(props.history, props.field));
const path = computed(() => chartPath(data.value, W, H));
const percent = (value: number | null) => (value === null ? "нет данных" : `${formatNumber(value)} %`);
const lastAt = computed(() => props.history.reduce((latest, point) => Math.max(latest, point.at), -Infinity));

// Hover: the sample nearest to the pointer.
const hover = ref<HistoryPoint | null>(null);
function onMove(event: PointerEvent) {
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
  if (box.width <= 0) return;
  const at = lastAt.value - (1 - (event.clientX - box.left) / box.width) * CHART_WINDOW;
  let best: HistoryPoint | null = null;
  for (const point of props.history) if (!best || Math.abs(point.at - at) < Math.abs(best.at - at)) best = point;
  hover.value = best && Math.abs(best.at - at) <= 60 ? best : null;
}
const hoverX = computed(() => (hover.value ? (1 - (lastAt.value - hover.value.at) / CHART_WINDOW) * 100 : 0));
const hoverValue = computed(() => (hover.value ? hover.value[props.field] : null));
const hoverY = computed(() =>
  hoverValue.value === null ? null : 100 - Math.min(Math.max(hoverValue.value, 0), 100),
);
</script>

<template>
  <figure class="chart">
    <figcaption class="c-head">
      <span class="c-title">{{ title }}</span>
      <span v-if="!data.collecting && !data.empty" class="c-scale num">шкала 0–100 %</span>
    </figcaption>
    <p v-if="data.collecting" class="c-note">история собирается</p>
    <p v-else-if="data.empty" class="c-note">за час данных нет</p>
    <template v-else>
      <div class="plot" @pointermove="onMove" @pointerleave="hover = null">
        <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" role="img" :aria-label="`${title} за час, ${staleAge ?? 'сейчас'} ${percent(data.current)}`">
          <line class="grid" x1="0" :x2="W" y1="0" y2="0" vector-effect="non-scaling-stroke" />
          <line class="grid base" x1="0" :x2="W" :y1="H" :y2="H" vector-effect="non-scaling-stroke" />
          <path class="line" :d="path" vector-effect="non-scaling-stroke" />
        </svg>
        <template v-if="hover">
          <span class="cross" :style="{ left: `${hoverX}%` }" aria-hidden="true"></span>
          <span v-if="hoverY !== null" class="dot" :style="{ left: `${hoverX}%`, top: `${hoverY}%` }" aria-hidden="true"></span>
          <span class="tip num" :class="{ left: hoverX > 60 }" :style="{ left: `${hoverX}%` }">
            {{ percent(hoverValue) }} · {{ formatAge(hover.at, now) }}
          </span>
        </template>
      </div>
      <div class="c-axis">
        <span>час назад</span>
        <span class="num c-now">{{ staleAge ?? "сейчас" }} {{ percent(data.current) }}</span>
      </div>
    </template>
  </figure>
</template>

<style scoped>
.chart {
  margin: 0 0 10px;
}

.c-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 2px;
  font-size: 12px;
}

.c-title {
  color: var(--muted);
}

.c-now {
  color: var(--text);
  font-weight: 600;
}

.c-note {
  margin: 0;
  padding: 10px 8px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--faint);
  font-size: 12px;
  text-align: center;
}

.plot {
  position: relative;
  height: 64px;
  border-radius: 4px;
  background: var(--surface-3);
}

svg {
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.grid {
  stroke: var(--border);
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

.grid.base {
  stroke-dasharray: none;
}

.line {
  fill: none;
  stroke: var(--link);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.c-scale {
  color: var(--faint);
  font-size: 10.5px;
}

.cross {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--border-strong);
  pointer-events: none;
}

.dot {
  position: absolute;
  width: 8px;
  height: 8px;
  margin: -4px 0 0 -4px;
  border-radius: 50%;
  background: var(--link);
  box-shadow: 0 0 0 2px var(--surface);
  pointer-events: none;
}

.tip {
  position: absolute;
  top: -22px;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  box-shadow: var(--shadow);
  font-size: 11px;
  white-space: nowrap;
  pointer-events: none;
}

.tip.left {
  transform: translateX(-100%);
}

.c-axis {
  display: flex;
  justify-content: space-between;
  margin-top: 1px;
  color: var(--faint);
  font-size: 10.5px;
}
</style>
