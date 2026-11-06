<script setup lang="ts">
import { computed } from "vue";
import type { Machine, Machines } from "../types";
import { formatFullTime } from "../lib/format";
import { STATUS, dataAge, errorLabel, freeSummary, statusLevel, statusText, tileMeters } from "../lib/machines";

const props = defineProps<{ machines: Machines | null; now: number; collapsed: boolean; selected: string | null }>();
const emit = defineEmits<{ toggle: []; open: [id: string, opener: HTMLElement | null] }>();
// The button that opened the card gets the focus back when it closes, tile or folded chip alike.
const open = (id: string, event: Event) => emit("open", id, event.currentTarget as HTMLElement | null);

const items = computed(() => props.machines?.items ?? []);
const shown = computed(() => items.value.length > 0 || !!props.machines?.config_error);

const tiles = computed(() =>
  items.value.map((machine: Machine) => ({
    machine,
    level: statusLevel(machine.status),
    status: statusText(machine),
    meters: tileMeters(machine.metrics),
    age: dataAge(machine, props.now),
    // A failed probe that has not yet made the machine offline: a small mark, the numbers stay.
    mark: machine.error && machine.status !== "offline" ? errorLabel(machine.error) : null,
  })),
);
</script>

<template>
  <section v-if="shown" class="machines" aria-labelledby="machines-title">
    <div class="m-head">
      <h2 id="machines-title" class="m-title">
        <button type="button" class="m-toggle" :aria-expanded="!collapsed" aria-controls="machines-body" @click="emit('toggle')">
          <span class="chevron" aria-hidden="true">{{ collapsed ? "▶" : "▼" }}</span>
          Машины
          <span v-if="items.length" class="count num">{{ items.length }}</span>
        </button>
      </h2>
      <span v-if="items.length" class="faint num">{{ freeSummary(items) }}</span>
      <!-- Collapsed: still one line that tells which machine is free. -->
      <ul v-if="collapsed && items.length" class="mini">
        <li v-for="tile in tiles" :key="tile.machine.id" :data-machine="tile.machine.id">
          <button type="button" class="mini-btn" :class="tile.machine.status" :title="tile.status" @click="open(tile.machine.id, $event)">
            <span class="t-status" :class="tile.level"><span class="dot" aria-hidden="true"></span></span>
            <span class="ellipsis">{{ tile.machine.name }}</span>
            <span class="mini-word" :class="tile.level">{{ STATUS[tile.machine.status] }}</span>
          </button>
        </li>
      </ul>
      <p v-if="machines?.config_error" class="config faint" :title="machines.config_error">
        Конфиг машин не прочитан: {{ machines.config_error }}
      </p>
    </div>

    <ul v-show="!collapsed && items.length" id="machines-body" class="tiles">
      <li v-for="tile in tiles" :key="tile.machine.id" :data-machine="tile.machine.id">
        <button
          type="button"
          class="tile"
          :class="[tile.machine.status, { selected: selected === tile.machine.id }]"
          :aria-pressed="selected === tile.machine.id"
          @click="open(tile.machine.id, $event)"
        >
          <span class="t-line">
            <span class="t-name ellipsis" :title="tile.machine.name">{{ tile.machine.name }}</span>
            <span class="t-status" :class="tile.level">
              <span class="dot" aria-hidden="true"></span>{{ tile.status }}
            </span>
          </span>
          <span v-for="meter in tile.meters" :key="meter.key" class="meter" :title="meter.title">
            <span class="m-label">{{ meter.label }}</span>
            <span class="bar" aria-hidden="true"><span :class="meter.level" :style="{ width: `${meter.percent}%` }"></span></span>
            <span class="m-value num">{{ meter.value }}</span>
          </span>
          <span v-if="tile.mark || tile.age" class="t-foot">
            <span v-if="tile.mark" class="mark"><span aria-hidden="true">⚠</span> {{ tile.mark }}</span>
            <span v-if="tile.age" class="age num" :title="tile.machine.sampled_at ? formatFullTime(tile.machine.sampled_at) : ''">{{ tile.age }}</span>
          </span>
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.machines {
  padding: 6px 16px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-3);
}

.m-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  min-height: 24px;
}

.m-title {
  margin: 0;
  flex: none;
  font-size: 12px;
  font-weight: 650;
}

.m-toggle {
  all: unset;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  border-radius: 4px;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--muted);
}

.m-toggle:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}

.chevron {
  width: 10px;
  font-size: 8px;
}

.count {
  padding: 0 5px;
  border-radius: 999px;
  background: var(--surface-2);
  font-size: 11px;
  line-height: 17px;
}

.faint {
  color: var(--faint);
  font-size: 12px;
  white-space: nowrap;
}

.config {
  flex: 1;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mini {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.mini-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 220px;
  padding: 0 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: inherit;
  font: inherit;
  font-size: 12px;
  line-height: 18px;
  cursor: pointer;
}

.mini-btn.offline,
.mini-btn.unknown {
  border-style: dashed;
  color: var(--muted);
}

.mini-word {
  flex: none;
  color: var(--muted);
}

.mini-word.ok { color: var(--ok); }
.mini-word.warn { color: var(--warn); }
.mini-word.bad { color: var(--bad); }

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(205px, 1fr));
  gap: 6px;
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}

.tiles li {
  display: flex;
  min-width: 0;
}

.tile {
  display: grid;
  align-content: start;
  gap: 1px;
  width: 100%;
  min-width: 0;
  padding: 4px 8px 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.tile:hover {
  border-color: var(--border-strong);
}

.tile.selected {
  border-color: var(--focus);
  box-shadow: 0 0 0 1px var(--focus);
}

.tile.overloaded {
  border-color: color-mix(in srgb, var(--bad) 45%, var(--border));
}

/* Offline: the last known numbers, visibly not live. */
.tile.offline,
.tile.unknown {
  background: var(--surface-3);
  border-style: dashed;
}

.tile.offline .meter,
.tile.offline .t-name {
  opacity: 0.6;
}

/* A long status ("перегружена: процессор, память, GPU") moves under the name instead of being cut. */
.t-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 6px;
  min-width: 0;
  margin-bottom: 1px;
}

.t-name {
  flex: 0 1 auto;
  font-weight: 600;
}

.t-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  margin-left: auto;
  color: var(--muted);
  font-size: 11.5px;
}

.t-line .t-status {
  display: block;
  flex: 1 1 auto;
  white-space: normal;
  text-align: right;
}

.t-line .t-status .dot {
  display: inline-block;
  margin-right: 4px;
  vertical-align: 0;
}

.t-status .dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  border: 1.5px solid var(--faint);
}

.t-status.ok { color: var(--ok); font-weight: 600; }
.t-status.warn { color: var(--warn); font-weight: 600; }
.t-status.bad { color: var(--bad); font-weight: 600; }
.t-status.ok .dot { background: var(--ok); border-color: var(--ok); }
.t-status.warn .dot { background: var(--warn); border-color: var(--warn); }
.t-status.bad .dot { background: var(--bad); border-color: var(--bad); }

.meter {
  display: grid;
  grid-template-columns: 42px minmax(24px, 1fr) max-content;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 11.5px;
  line-height: 14px;
}

.m-label {
  color: var(--muted);
}

.m-value {
  text-align: right;
}

.bar {
  height: 4px;
  border-radius: 2px;
  background: var(--surface-2);
  overflow: hidden;
}

.bar span {
  display: block;
  height: 100%;
  border-radius: 2px;
}

.bar .ok { background: var(--ok); }
.bar .warn { background: var(--warn); }
.bar .bad { background: var(--bad); }

.t-foot {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 2px 8px;
  font-size: 11px;
  color: var(--muted);
}

.mark {
  padding: 0 5px;
  border-radius: 999px;
  background: var(--warn-soft);
  color: var(--warn);
  white-space: nowrap;
}

.age {
  margin-left: auto;
  white-space: nowrap;
  font-weight: 550;
}

.tile.offline .age {
  color: var(--text);
}

/* Narrow screens: the tiles scroll sideways inside the strip, the page does not. */
@media (max-width: 760px) {
  .machines {
    padding: 6px 10px 8px;
  }

  .m-head {
    flex-wrap: wrap;
  }

  .config {
    flex-basis: 100%;
    white-space: normal;
  }

  .tiles {
    grid-template-columns: none;
    grid-auto-flow: column;
    grid-auto-columns: minmax(220px, 78%);
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scroll-snap-type: x proximity;
    padding-bottom: 4px;
  }

  .tiles li {
    scroll-snap-align: start;
  }
}
</style>
