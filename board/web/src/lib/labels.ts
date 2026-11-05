import type { Activity, Column, Phase, TaskFile, TaskSummary } from "../types";

export const COLUMNS: { id: Column; title: string }[] = [
  { id: "running", title: "Работает" },
  { id: "sleeping", title: "Спит до сброса" },
  { id: "review_me", title: "Ждёт ревью" },
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

export const KINDS: Record<TaskSummary["kind"], string> = { work: "работа", review: "ревью", scout: "разведка" };

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
};

export const FILES: Record<TaskFile, string> = { brief: "Бриф", last: "Ответ", review: "Ревью", check: "Проверка" };

/** "#118" from a GitHub or Forgejo PR URL, else "PR". */
export function prLabel(url: string): string {
  const match = /\/(?:pull|pulls|merge_requests)\/(\d+)/.exec(url);
  return match ? `PR #${match[1]}` : "PR";
}

/** Why a task needs attention, in words: the result status, or the outcome when it is a failure. */
export function problemLabel(task: TaskSummary): string | null {
  if (task.column !== "attention" && task.column !== "closed") return null;
  if (task.outcome && task.outcome !== "done" && OUTCOMES[task.outcome]) return OUTCOMES[task.outcome]!;
  if (task.result_status) return RESULT[task.result_status] ?? task.result_status;
  return task.outcome;
}
