<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import type { Machine } from "../types";
import { formatAge, formatFullTime } from "../lib/format";
import {
  cpuAverageText,
  cpuLoadText,
  cpuMeter,
  dataAge,
  diskMeter,
  errorLabel,
  formatBytes,
  gpuMeter,
  memoryMeter,
  statusLevel,
  statusText,
  vramMeter,
  type Meter,
} from "../lib/machines";
import MachineChart from "./MachineChart.vue";

const props = defineProps<{
  id: string;
  /** The live machine; null once it is gone from the data. */
  machine: Machine | null;
  now: number;
}>();
const emit = defineEmits<{ close: [] }>();

// The last known data stays on screen when the machine leaves the list.
const last = shallowRef<Machine | null>(props.machine);
watch(
  () => props.machine,
  (machine) => {
    if (machine) last.value = machine;
  },
);
watch(
  () => props.id,
  () => {
    last.value = props.machine;
  },
);
const shown = computed(() => props.machine ?? (last.value?.id === props.id ? last.value : null));
const metrics = computed(() => shown.value?.metrics ?? null);
const level = computed(() => (shown.value ? statusLevel(shown.value.status) : null));
const age = computed(() => (shown.value ? dataAge(shown.value, props.now) : null));

const gpus = computed(() =>
  (metrics.value?.gpus ?? []).map((gpu, index) => ({
    key: `${index}:${gpu.name}`,
    name: gpu.name,
    meters: [gpuMeter(gpu), vramMeter(gpu)].filter((meter): meter is Meter => meter !== null),
    shared: gpu.memory_total_bytes === null,
    processes: [...gpu.processes].sort((a, b) => b.memory_bytes - a.memory_bytes),
  })),
);
const hasPercent = (cpu: { percent?: number | null }) => cpu.percent !== null && cpu.percent !== undefined;
const disks = computed(() =>
  (metrics.value?.disks ?? []).map((disk) => ({ ...diskMeter(disk), label: disk.mount })),
);
const hasGpu = computed(
  () => gpus.value.length > 0 || (shown.value?.history ?? []).some((point) => point.gpu_percent !== null),
);

const closeButton = ref<HTMLButtonElement | null>(null);
function onKey(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  }
}
onMounted(() => {
  window.addEventListener("keydown", onKey);
  void nextTick(() => closeButton.value?.focus());
});
onBeforeUnmount(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <aside class="panel" role="dialog" aria-modal="false" aria-labelledby="machine-title">
    <header class="p-head">
      <div class="p-titles">
        <h2 id="machine-title" class="ellipsis" :title="shown?.name ?? id">{{ shown?.name ?? id }}</h2>
        <span v-if="shown?.role" class="role ellipsis" :title="shown.role">{{ shown.role }}</span>
      </div>
      <button ref="closeButton" type="button" class="btn close" title="Закрыть (Esc)" @click="emit('close')">
        <span aria-hidden="true">✕</span><span class="visually-hidden">Закрыть</span>
      </button>
    </header>

    <div class="p-body">
      <p v-if="!machine" class="note">Машины больше нет в списке; показаны последние данные.</p>
      <p v-if="!shown" class="note">Машина не найдена.</p>
      <template v-else>
        <div class="status-line" :class="shown.status">
          <span class="chip" :class="level ?? ''"><span class="dot" aria-hidden="true"></span>{{ statusText(shown) }}</span>
          <span v-if="shown.checked_at !== null" class="muted num" :title="formatFullTime(shown.checked_at)">
            проверено {{ formatAge(shown.checked_at, now) }}
          </span>
          <span v-else class="muted">ещё не проверялась</span>
        </div>
        <p v-if="shown.error && shown.status !== 'offline'" class="note warn">
          <span aria-hidden="true">⚠</span> Последняя проба: {{ errorLabel(shown.error) }}; цифры с прошлого успешного опроса.
        </p>
        <p v-if="age" class="note" :class="{ stale: shown.status === 'offline' }">
          Цифры не живые: {{ age }}<template v-if="shown.sampled_at !== null"> ({{ formatFullTime(shown.sampled_at) }})</template>.
        </p>
        <p v-if="!metrics" class="note">Метрик ещё не было.</p>

        <div v-if="metrics" class="sections" :class="{ dim: shown.status === 'offline' }">
          <section v-if="metrics.cpu || metrics.memory" class="sec">
            <h3>Процессор и память</h3>
            <template v-if="metrics.cpu">
              <div class="meter labelled cpu" :title="cpuMeter(metrics.cpu).title">
                <span class="m-label">CPU</span>
                <span class="bar" aria-hidden="true"><span :class="cpuMeter(metrics.cpu).level" :style="{ width: `${cpuMeter(metrics.cpu).percent}%` }"></span></span>
                <span class="num" :class="{ big: hasPercent(metrics.cpu) }">{{ cpuMeter(metrics.cpu).value }}</span>
              </div>
              <p v-if="cpuAverageText(metrics.cpu)" class="sub num indent">{{ cpuAverageText(metrics.cpu) }}</p>
              <p class="sub num indent">{{ cpuLoadText(metrics.cpu) }}</p>
            </template>
            <div v-if="metrics.memory" class="meter labelled" :title="memoryMeter(metrics.memory).title">
              <span class="m-label">Память</span>
              <span class="bar" aria-hidden="true"><span :class="memoryMeter(metrics.memory).level" :style="{ width: `${memoryMeter(metrics.memory).percent}%` }"></span></span>
              <span class="num">свободно {{ memoryMeter(metrics.memory).value }}</span>
            </div>
          </section>

          <section v-if="gpus.length" class="sec">
            <h3>GPU</h3>
            <div v-for="gpu in gpus" :key="gpu.key" class="gpu">
              <p class="gpu-name ellipsis" :title="gpu.name">{{ gpu.name }}</p>
              <div v-for="meter in gpu.meters" :key="meter.key" class="meter labelled" :title="meter.title">
                <span class="m-label">{{ meter.key === "gpu" ? "загрузка" : "видеопамять" }}</span>
                <span class="bar" aria-hidden="true"><span :class="meter.level" :style="{ width: `${meter.percent}%` }"></span></span>
                <span class="num">{{ meter.key === "gpu" ? meter.value : `свободно ${meter.value}` }}</span>
              </div>
              <p v-if="gpu.shared" class="sub indent">видеопамять общая с системной</p>
              <table v-if="gpu.processes.length" class="procs">
                <caption class="visually-hidden">Процессы на {{ gpu.name }}</caption>
                <tbody>
                  <tr v-for="(process, index) in gpu.processes" :key="index">
                    <td class="mono ellipsis" :title="process.name">{{ process.name }}</td>
                    <td class="num">{{ formatBytes(process.memory_bytes) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section v-if="disks.length" class="sec">
            <h3>Диски</h3>
            <div v-for="disk in disks" :key="disk.key" class="meter labelled" :title="disk.title">
              <span class="m-label mono ellipsis" :title="disk.label">{{ disk.label }}</span>
              <span class="bar" aria-hidden="true"><span :class="disk.level" :style="{ width: `${disk.percent}%` }"></span></span>
              <span class="num">свободно {{ disk.value }}</span>
            </div>
          </section>
          <p v-if="metrics.os" class="sub os">ОС: {{ metrics.os }}</p>
        </div>

        <section class="sec">
          <h3>За час</h3>
          <MachineChart :history="shown.history" field="cpu_percent" title="CPU занят" :now="now" :stale-age="age" />
          <MachineChart :history="shown.history" field="memory_percent" title="Память занята" :now="now" :stale-age="age" />
          <MachineChart v-if="hasGpu" :history="shown.history" field="gpu_percent" title="GPU" :now="now" :stale-age="age" />
        </section>

        <section v-if="shown.host" class="host" aria-labelledby="host-title">
          <h3 id="host-title">Хост <span class="host-name">{{ shown.host.name }}</span></h3>
          <p class="sub">Машина, на которой работает эта; на статус не влияет.</p>
          <p v-if="shown.host.error" class="sub warn-text"><span aria-hidden="true">⚠</span> {{ errorLabel(shown.host.error) }}</p>
          <template v-if="shown.host.cpu">
            <div class="meter labelled cpu" :title="cpuMeter(shown.host.cpu).title">
              <span class="m-label">CPU</span>
              <span class="bar" aria-hidden="true"><span :class="cpuMeter(shown.host.cpu).level" :style="{ width: `${cpuMeter(shown.host.cpu).percent}%` }"></span></span>
              <span class="num" :class="{ big: hasPercent(shown.host.cpu) }">{{ cpuMeter(shown.host.cpu).value }}</span>
            </div>
            <p class="sub num indent">{{ cpuLoadText(shown.host.cpu) }}</p>
          </template>
          <div v-if="shown.host.memory" class="meter labelled" :title="memoryMeter(shown.host.memory).title">
            <span class="m-label">Память</span>
            <span class="bar" aria-hidden="true"><span :class="memoryMeter(shown.host.memory).level" :style="{ width: `${memoryMeter(shown.host.memory).percent}%` }"></span></span>
            <span class="num">свободно {{ memoryMeter(shown.host.memory).value }}</span>
          </div>
          <p v-if="!shown.host.cpu && !shown.host.memory && !shown.host.error" class="sub">данных хоста нет</p>
        </section>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 21;
  display: flex;
  flex-direction: column;
  width: min(480px, 100vw);
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

.p-titles {
  flex: 1;
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.p-titles h2 {
  flex: 0 1 auto;
  margin: 0;
  font-size: 15px;
  font-weight: 650;
}

.role {
  flex: 0 1 auto;
  color: var(--muted);
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

.status-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  margin-bottom: 10px;
}

.status-line .chip {
  font-size: 12px;
  line-height: 20px;
  white-space: normal;
}

.chip .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  border: 1.5px solid currentColor;
}

.chip.ok .dot,
.chip.warn .dot,
.chip.bad .dot {
  background: currentColor;
}

.note {
  margin: 0 0 8px;
  padding: 4px 8px;
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--muted);
}

.note.warn {
  background: var(--warn-soft);
  color: var(--warn);
}

.note.stale {
  color: var(--text);
  font-weight: 550;
}

.sections.dim .sec {
  opacity: 0.7;
}

.sec,
.host {
  margin: 0 0 14px;
}

h3 {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 650;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--muted);
}

/* Wide enough for mounts such as /srv/assistant; a longer one is cut and keeps its full path in a tooltip. */
.panel {
  --label: 150px;
}

.meter {
  display: grid;
  grid-template-columns: var(--label) minmax(60px, 1fr) max-content;
  align-items: center;
  gap: 10px;
  min-height: 20px;
}

.meter .big {
  font-size: 17px;
  font-weight: 650;
  line-height: 22px;
}

.sub.indent {
  margin: 0 0 4px calc(var(--label) + 10px);
}

@media (max-width: 480px) {
  .panel {
    --label: 104px;
  }
}

.sub.os {
  margin: 0 0 14px;
}

.host-name {
  text-transform: none;
  letter-spacing: 0;
  color: var(--text);
}

.m-label {
  color: var(--muted);
}

.bar {
  height: 6px;
  border-radius: 3px;
  background: var(--surface-2);
  overflow: hidden;
}

.bar span {
  display: block;
  height: 100%;
  border-radius: 3px;
}

.bar .ok { background: var(--ok); }
.bar .warn { background: var(--warn); }
.bar .bad { background: var(--bad); }

.sub {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.warn-text {
  color: var(--warn);
}

.gpu + .gpu {
  margin-top: 8px;
}

.gpu-name {
  margin: 0 0 2px;
  font-weight: 600;
}

.procs {
  width: calc(100% - var(--label) - 10px);
  margin: 4px 0 0 calc(var(--label) + 10px);
  border-collapse: collapse;
  table-layout: fixed;
}

.procs td {
  padding: 1px 0;
  border-top: 1px solid var(--border);
}

.procs td.num {
  width: 90px;
  text-align: right;
}

.host {
  padding: 8px 10px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  background: var(--surface-3);
}

.host .sub {
  margin: 0 0 6px;
}
</style>
