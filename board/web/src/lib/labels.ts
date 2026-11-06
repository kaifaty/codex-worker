import type { Activity, Column, Phase, TaskFile, TaskSummary } from "../types";

export const COLUMNS: { id: Column; title: string }[] = [
  { id: "running", title: "Работает" },
  { id: "sleeping", title: "Спит до сброса" },
  { id: "review_me", title: "Ждёт тебя" },
  { id: "attention", title: "Нужно внимание" },
  { id: "closed", title: "Закрыто" },
];

export const PHASES: Record<Phase, string> = {
  turn: "ход",
  check: "проверка",
  review: "ревью",
  fix: "исправление",
  ci: "CI",
  sleeping: "сон",
  idle: "простой",
};

export const EXECUTORS: Record<TaskSummary["executor"], string> = { codex: "Codex", claude: "Claude" };

export const KINDS: Record<TaskSummary["kind"], string> = {
  work: "работа",
  review: "ревью",
  scout: "разведка",
  session: "сессия",
};

/** Heading of a card: a session's title, else the start of its uuid; the slug for a task. */
export function taskTitle(task: TaskSummary): string {
  if (task.kind !== "session") return task.slug;
  const title = task.title?.trim();
  return title ? title : task.slug.replace(/^local_/, "").slice(0, 8);
}

/** A session's link to the Claude app, only when it is a `claude://` link. */
export function claudeLink(task: TaskSummary): string | null {
  const link = task.link;
  return task.kind === "session" && typeof link === "string" && link.startsWith("claude://") ? link : null;
}

export type KindFilter = "all" | "tasks" | "sessions";

export const KIND_FILTERS: { id: KindFilter; label: string }[] = [
  { id: "all", label: "Всё" },
  { id: "tasks", label: "Задачи" },
  { id: "sessions", label: "Сессии" },
];

export const isKindFilter = (value: unknown): value is KindFilter =>
  value === "all" || value === "tasks" || value === "sessions";

export function matchesKind(task: TaskSummary, filter: KindFilter): boolean {
  if (filter === "all") return true;
  return (task.kind === "session") === (filter === "sessions");
}

export const ACTIVITY: Record<Activity["kind"], { icon: string; label: string }> = {
  command: { icon: "$", label: "команда" },
  edit: { icon: "✎", label: "правка" },
  message: { icon: "›", label: "сообщение" },
  tool: { icon: "⚙", label: "инструмент" },
  thinking: { icon: "…", label: "размышляет" },
};

export const CI: Record<string, { label: string; tone: "ok" | "bad" | "warn" | "muted" }> = {
  pending: { label: "CI идёт", tone: "warn" },
  passed: { label: "CI прошёл", tone: "ok" },
  failed: { label: "CI упал", tone: "bad" },
  skipped: { label: "CI пропущен", tone: "muted" },
  timeout: { label: "CI: таймаут", tone: "bad" },
  none: { label: "без CI", tone: "muted" },
};

export const RESULT: Record<string, string> = {
  done: "готово",
  partial: "частично",
  needs_decision: "нужно решение",
  blocked: "заблокирована",
};

export const OUTCOMES: Record<string, string> = {
  running: "надзиратель умер",
  sleeping: "надзиратель умер во сне",
  failed: "сбой",
  stopped: "остановлена",
  budget_exhausted: "бюджет исчерпан",
  conflict: "конфликт",
  interrupted: "прервана",
  stuck: "зависла",
  refused: "отказ",
  unavailable: "недоступна",
  exhausted: "лимиты исчерпаны",
  error: "ошибка",
};

/** Outcomes of a task that finished normally. */
const SUCCESS_OUTCOMES = new Set(["done", "completed"]);

export const FILES: Record<TaskFile, string> = { brief: "Бриф", last: "Ответ", review: "Ревью", check: "Проверка" };

/** "#118" from a GitHub or Forgejo PR URL, else "PR". */
export function prLabel(url: string): string {
  const match = /\/(?:pull|pulls|merge_requests)\/(\d+)/.exec(url);
  return match ? `PR #${match[1]}` : "PR";
}

/**
 * Why a finished task needs attention, in words, or null when nothing went wrong: a failure outcome first,
 * then a result status other than "done", then an unknown outcome when there is no result status.
 */
export function problemLabel(task: TaskSummary): string | null {
  if (task.column !== "attention" && task.column !== "closed") return null;
  const outcome = task.outcome && !SUCCESS_OUTCOMES.has(task.outcome) ? task.outcome : null;
  if (outcome && OUTCOMES[outcome]) return OUTCOMES[outcome]!;
  if (task.result_status) return task.result_status === "done" ? null : (RESULT[task.result_status] ?? task.result_status);
  return outcome;
}

/** CI chip of a card. A task whose supervisor is gone can no longer wait for a pending CI. */
export function ciChip(task: TaskSummary): { label: string; tone: "ok" | "bad" | "warn" | "muted" } | null {
  if (!task.ci) return null;
  if (task.ci === "pending" && !task.alive) return { label: "CI не дождались", tone: "muted" };
  return CI[task.ci] ?? { label: `CI: ${task.ci}`, tone: "muted" };
}

/** Length of the card highlight animation; `.flash` in TaskCard.vue uses the same value. */
export const FLASH_MS = 2400;

/** Whether a change at `changedAt` (ms) is still being highlighted at `nowMs`, so a card that comes back
 * through a filter later does not replay an old highlight. */
export function isFlashing(changedAt: number | undefined, nowMs: number): boolean {
  return changedAt !== undefined && nowMs - changedAt < FLASH_MS;
}
