// localStorage can be missing, full or blocked (private mode, file://); the board works without it.

export function loadJson<T>(key: string, fallback: T, valid: (value: unknown) => value is T): T {
  try {
    const text = localStorage.getItem(key);
    if (text === null) return fallback;
    const value: unknown = JSON.parse(text);
    return valid(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

export function saveJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Not saved; the choice still applies until reload.
  }
}
