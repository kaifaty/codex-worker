<script setup lang="ts">
import { computed } from "vue";
import type { Accounts } from "../types";
import { parseAccounts } from "../lib/accounts";
import { formatAge, formatClock, formatFullTime, formatUntil } from "../lib/format";

const props = defineProps<{ accounts: Accounts | null; now: number; collapsed: boolean }>();
const emit = defineEmits<{ toggle: [] }>();

const parsed = computed(() => parseAccounts(props.accounts?.data ?? null, props.now));
const count = computed(() => parsed.value.items?.length ?? 0);
const level = (percent: number) => (percent >= 90 ? "bad" : percent >= 70 ? "warn" : "ok");
</script>

<template>
  <section class="accounts" aria-labelledby="accounts-title">
    <div class="acc-head">
      <h2 id="accounts-title" class="acc-title">
        <button type="button" class="acc-toggle" :aria-expanded="!collapsed" aria-controls="accounts-body" @click="emit('toggle')">
          <span class="chevron" aria-hidden="true">{{ collapsed ? "▶" : "▼" }}</span>
          Аккаунты
          <span v-if="count" class="count num">{{ count }}</span>
        </button>
      </h2>
      <span v-if="accounts?.fetched_at" class="faint num" :title="formatFullTime(accounts.fetched_at)">
        данные {{ formatAge(accounts.fetched_at, now) }}
      </span>
      <span v-else-if="accounts" class="faint">данных ещё нет</span>
      <p v-if="accounts?.error" class="error" role="status">
        <span aria-hidden="true">⚠</span>
        <span class="error-text" :title="accounts.error">Ошибка: {{ accounts.error }}</span>
      </p>
    </div>

    <div v-show="!collapsed" id="accounts-body">
      <pre v-if="parsed.raw !== null" class="raw mono" :title="parsed.raw">{{ parsed.raw }}</pre>
      <ul v-else-if="parsed.items && parsed.items.length" class="grid">
        <li v-for="item in parsed.items" :key="item.kind === 'row' ? item.row.key : item.key" class="acc" :class="item.kind === 'row' ? [item.row.executor, { bad: item.row.bad }] : 'unknown'">
          <template v-if="item.kind === 'row'">
            <div class="line">
              <span class="exec" :class="item.row.executor" :title="item.row.executor === 'codex' ? 'Codex' : 'Claude'">
                <span class="glyph" aria-hidden="true">{{ item.row.executor === "codex" ? "◆" : "✳" }}</span>
                <span class="visually-hidden">{{ item.row.executor === "codex" ? "Codex" : "Claude" }}</span>
              </span>
              <span class="name ellipsis" :title="item.row.name">{{ item.row.name }}</span>
              <span v-if="item.row.weeklyPercent !== null" class="pct num" :class="level(item.row.weeklyPercent)">{{ Math.round(item.row.weeklyPercent) }} %</span>
            </div>
            <div v-if="item.row.weeklyPercent !== null" class="bar" role="img" :aria-label="`Неделя: ${Math.round(item.row.weeklyPercent)} %`">
              <span :class="level(item.row.weeklyPercent)" :style="{ width: `${Math.min(100, Math.max(0, item.row.weeklyPercent))}%` }"></span>
            </div>
            <div class="line sub">
              <span v-if="item.row.status" class="status" :class="{ bad: item.row.bad }">
                {{ item.row.status }}<template v-if="item.row.limitedUntil !== null"> · {{ formatUntil(item.row.limitedUntil, now) }}</template>
              </span>
              <span v-if="item.row.weeklyResetsAt !== null" class="ellipsis num" :title="formatFullTime(item.row.weeklyResetsAt)">
                сброс {{ formatClock(item.row.weeklyResetsAt, now) }}
              </span>
              <span v-else-if="!item.row.status" class="faint">нет недельного окна</span>
            </div>
          </template>
          <code v-else class="mono ellipsis" :title="item.json">{{ item.json }}</code>
        </li>
      </ul>
      <p v-else-if="accounts && !accounts.error" class="faint empty">аккаунтов нет</p>
    </div>
  </section>
</template>

<style scoped>
.accounts {
  padding: 6px 16px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-3);
}

.acc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  min-height: 24px;
}

.acc-title {
  margin: 0;
  flex: none;
  font-size: 12px;
  font-weight: 650;
}

.acc-toggle {
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

.acc-toggle:focus-visible {
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

.error {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
  margin: 0;
  padding: 1px 8px;
  border-radius: var(--radius);
  background: var(--bad-soft);
  color: var(--bad);
  font-size: 12px;
}

.error-text {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
  min-width: 0;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 6px;
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}

.acc {
  display: grid;
  gap: 3px;
  min-width: 0;
  padding: 5px 8px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.acc.bad {
  border-color: color-mix(in srgb, var(--bad) 45%, var(--border));
}

.acc.unknown {
  display: flex;
  align-items: center;
  color: var(--muted);
}

.line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.exec {
  padding: 0 4px;
}

.name {
  flex: 1;
  font-weight: 600;
}

.pct {
  flex: none;
  font-weight: 650;
}

.pct.ok { color: var(--ok); }
.pct.warn { color: var(--warn); }
.pct.bad { color: var(--bad); }

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

.sub {
  justify-content: space-between;
  color: var(--muted);
  font-size: 11.5px;
}

.status {
  flex: none;
  white-space: nowrap;
}

.sub .num {
  text-align: right;
}

.status.bad {
  color: var(--bad);
  font-weight: 550;
}

.raw {
  margin: 6px 0 0;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 6.5em;
  overflow: hidden;
}

.empty {
  margin: 4px 0 0;
}

@media (max-width: 760px) {
  .accounts {
    padding: 6px 10px 8px;
  }

  .acc-head {
    flex-wrap: wrap;
  }

  .error {
    flex-basis: 100%;
  }

  .grid {
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  }
}
</style>
