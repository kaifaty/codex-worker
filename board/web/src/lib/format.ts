// Russian, compact time formatting. Inputs are seconds.

const pad = (n: number) => String(n).padStart(2, "0");

/** A duration such as "45 с", "3 мин 05 с", "1 ч 05 мин", "2 д 3 ч". Negative values count as 0. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total} с`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} мин ${pad(total % 60)} с`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч ${pad(minutes % 60)} мин`;
  const days = Math.floor(hours / 24);
  return `${days} д ${hours % 24} ч`;
}

/** A coarse span for ages and countdowns: "12 с", "5 мин", "2 ч 10 мин", "3 д". */
function coarseSpan(seconds: number): string {
  const total = Math.floor(seconds);
  if (total < 60) return `${total} с`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} ч ${minutes % 60} мин` : `${hours} ч`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} д ${hours % 24} ч` : `${days} д`;
}

/** How long ago `at` was, seen at `now`: "только что", "12 с назад", "5 мин назад". */
export function formatAge(at: number, now: number): string {
  const age = now - at;
  if (age < 1) return "только что";
  return `${coarseSpan(age)} назад`;
}

/** How long until `at`, seen at `now`: "через 2 ч 10 мин", or "сейчас" once it has passed. */
export function formatUntil(at: number, now: number): string {
  const left = at - now;
  if (left < 1) return "сейчас";
  return `через ${coarseSpan(left)}`;
}

const timeFormat = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dayTimeFormat = new Intl.DateTimeFormat("ru-RU", {
  weekday: "short",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

/** Local clock time: "14:05" today, "пт, 7 нояб., 14:05" on another day. */
export function formatClock(at: number, now: number): string {
  const date = new Date(at * 1000);
  const today = new Date(now * 1000);
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  return sameDay ? timeFormat.format(date) : dayTimeFormat.format(date);
}

/** Full local date and time for tooltips. */
export function formatFullTime(at: number): string {
  return new Date(at * 1000).toLocaleString("ru-RU");
}
