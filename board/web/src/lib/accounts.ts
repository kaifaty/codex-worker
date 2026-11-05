// Reads `codex_task.py accounts --json` output defensively. Any part that does not have the
// expected shape is shown as compact JSON instead.

export interface AccountRow {
  key: string;
  executor: "codex" | "claude";
  name: string;
  /** Weekly usage in percent, when the account reports it. */
  weeklyPercent: number | null;
  /** Unix seconds of the weekly reset. */
  weeklyResetsAt: number | null;
  /** Short state such as "исчерпан" or "не вошёл"; null for a healthy account. */
  status: string | null;
  /** The status is a problem (exhausted, logged out, limited). */
  bad: boolean;
  /** Unix seconds until which the account is limited. */
  limitedUntil: number | null;
}

export type AccountItem = { kind: "row"; row: AccountRow } | { kind: "raw"; key: string; json: string };

export interface ParsedAccounts {
  /** Recognised accounts, or null when `data` has an unknown form. */
  items: AccountItem[] | null;
  /** Compact JSON of the whole value when `items` is null. */
  raw: string | null;
}

type Obj = Record<string, unknown>;

const isObj = (value: unknown): value is Obj =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** ISO string or Unix seconds → Unix seconds. */
export function toSeconds(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const ms = Date.parse(value);
    return Number.isNaN(ms) ? null : ms / 1000;
  }
  return null;
}

export function compactJson(value: unknown, limit = 400): string {
  let text: string;
  try {
    text = JSON.stringify(value) ?? String(value);
  } catch {
    text = String(value);
  }
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

const WEEK_MINUTES = 7 * 24 * 60;

/** The weekly window of a Codex account: the longest of `primary` and `secondary`. */
function codexWeekly(entry: Obj): Obj | null {
  const windows = [entry.secondary, entry.primary].filter(isObj);
  if (windows.length === 0) return null;
  const withMinutes = windows.filter((w) => num(w.window_minutes) !== null);
  if (withMinutes.length > 0) {
    const longest = withMinutes.reduce((a, b) => (num(b.window_minutes)! > num(a.window_minutes)! ? b : a));
    return num(longest.window_minutes)! >= WEEK_MINUTES ? longest : null;
  }
  return isObj(entry.secondary) ? entry.secondary : null;
}

const CODEX_STATUS: Record<string, string> = {
  exhausted: "исчерпан",
  not_logged_in: "не вошёл",
  duplicate: "дубликат",
};

function codexRow(entry: Obj, index: number): AccountRow | null {
  if (typeof entry.name !== "string") return null;
  const weekly = codexWeekly(entry);
  const status = typeof entry.status === "string" ? entry.status : null;
  return {
    key: `codex:${entry.name}:${index}`,
    executor: "codex",
    name: entry.name,
    weeklyPercent: weekly ? num(weekly.used_percent) : null,
    weeklyResetsAt: weekly ? toSeconds(weekly.resets_at) : null,
    status: status === null ? "нет данных" : status === "available" ? null : (CODEX_STATUS[status] ?? status),
    bad: status !== null && status !== "available",
    limitedUntil: null,
  };
}

function claudeRow(entry: Obj, index: number, now: number): AccountRow | null {
  if (typeof entry.name !== "string") return null;
  const weekly = isObj(entry.seven_day) ? entry.seven_day : null;
  const limitedUntil = toSeconds(entry.limited_until);
  let status: string | null = null;
  let bad = false;
  if (entry.token_unavailable === true) {
    status = "нет токена";
    bad = true;
  } else if (limitedUntil !== null && limitedUntil > now) {
    status = "упёрся в лимит";
    bad = true;
  } else if (entry.stale === true) {
    status = "данные устарели";
  }
  return {
    key: `claude:${entry.name}:${index}`,
    executor: "claude",
    name: entry.name,
    weeklyPercent: weekly ? num(weekly.used_percent) : null,
    weeklyResetsAt: weekly ? toSeconds(weekly.resets_at) : null,
    status,
    bad,
    limitedUntil: bad && limitedUntil !== null && limitedUntil > now ? limitedUntil : null,
  };
}

/** `now` is Unix seconds. */
export function parseAccounts(data: unknown, now: number): ParsedAccounts {
  if (data === null || data === undefined) return { items: [], raw: null };
  if (!isObj(data) || (!Array.isArray(data.codex) && !Array.isArray(data.claude))) {
    return { items: null, raw: compactJson(data) };
  }
  const items: AccountItem[] = [];
  const add = (list: unknown, executor: "codex" | "claude") => {
    if (!Array.isArray(list)) return;
    list.forEach((entry: unknown, index) => {
      const row = isObj(entry)
        ? executor === "codex"
          ? codexRow(entry, index)
          : claudeRow(entry, index, now)
        : null;
      items.push(row ? { kind: "row", row } : { kind: "raw", key: `${executor}:raw:${index}`, json: compactJson(entry, 160) });
    });
  };
  add(data.codex, "codex");
  add(data.claude, "claude");
  return { items, raw: null };
}
