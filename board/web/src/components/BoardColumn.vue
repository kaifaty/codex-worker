<script setup lang="ts">
import type { Column, TaskSummary } from "../types";
import TaskCard from "./TaskCard.vue";

defineProps<{
  id: Column;
  title: string;
  tasks: TaskSummary[];
  now: number;
  changedAt: Readonly<Record<string, number>>;
  selected: string | null;
  collapsible?: boolean;
  collapsed?: boolean;
}>();

const emit = defineEmits<{ open: [slug: string]; toggle: [] }>();
</script>

<template>
  <section class="column" :class="[`c-${id}`, { collapsed }]" :aria-labelledby="`col-${id}`">
    <header class="col-head">
      <h2 :id="`col-${id}`" class="col-title">
        <button
          v-if="collapsible"
          type="button"
          class="col-toggle"
          :aria-expanded="!collapsed"
          :aria-controls="`col-body-${id}`"
          @click="emit('toggle')"
        >
          <span class="chevron" aria-hidden="true">{{ collapsed ? "▶" : "▼" }}</span>
          <span class="title-text">{{ title }}</span>
          <span class="count num">{{ tasks.length }}</span>
        </button>
        <template v-else>
          <span class="title-text">{{ title }}</span>
          <span class="count num">{{ tasks.length }}</span>
        </template>
      </h2>
    </header>
    <div v-show="!collapsed" :id="`col-body-${id}`" class="col-body">
      <TaskCard
        v-for="task in tasks"
        :key="task.slug"
        :task="task"
        :now="now"
        :changed-at="changedAt[task.slug]"
        :selected="selected === task.slug"
        @open="emit('open', $event)"
      />
      <p v-if="tasks.length === 0" class="empty">пусто</p>
    </div>
  </section>
</template>

<style scoped>
.column {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  border-radius: 8px;
  background: var(--surface-2);
}

.col-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px 6px;
  min-width: 0;
}

.col-title {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
  margin: 0;
  font-size: 12px;
  font-weight: 650;
}

.title-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--muted);
}

.c-attention .title-text {
  color: var(--bad);
}

.c-review_me .title-text {
  color: var(--ok);
}

.count {
  flex: none;
  min-width: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  font-size: 11px;
  line-height: 17px;
  text-align: center;
}

.col-toggle {
  all: unset;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  width: 100%;
  cursor: pointer;
  border-radius: 4px;
}

.col-toggle:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}

.chevron {
  flex: none;
  width: 10px;
  color: var(--muted);
  font-size: 8px;
}

.col-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px 6px 8px;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.empty {
  margin: 4px 4px 2px;
  color: var(--faint);
  font-size: 12px;
}

/* Wide screens: the collapsed column is a narrow vertical strip. */
@media (min-width: 761px) {
  .collapsed .col-head {
    padding: 10px 0;
    height: 100%;
    align-items: flex-start;
    justify-content: center;
  }

  .collapsed .col-title {
    width: auto;
  }

  .collapsed .col-toggle {
    writing-mode: vertical-rl;
    width: auto;
    padding: 2px 0;
  }

  .collapsed .chevron {
    width: auto;
  }

  .collapsed .count {
    writing-mode: horizontal-tb;
  }
}
</style>
