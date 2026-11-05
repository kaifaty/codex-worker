import type { ServerMessage } from "../types";

export type ConnectionState = "connecting" | "open" | "reconnecting";

/** The part of the browser WebSocket this client uses; tests pass a fake. */
export interface SocketLike {
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(): void;
}

export interface SocketOptions {
  url: string;
  onMessage: (message: ServerMessage) => void;
  onState: (state: ConnectionState, retryInMs: number | null) => void;
  createSocket?: (url: string) => SocketLike;
  /** Close and reconnect when nothing arrives for this long; the server pings every 20 s. */
  silenceMs?: number;
}

export const MIN_DELAY_MS = 1000;
export const MAX_DELAY_MS = 30000;

/** Delay before reconnect attempt `attempt` (0-based): 1, 2, 4, 8, 16, 30, 30… seconds. */
export function backoffDelay(attempt: number): number {
  return Math.min(MAX_DELAY_MS, MIN_DELAY_MS * 2 ** attempt);
}

function parse(data: unknown): ServerMessage | null {
  if (typeof data !== "string") return null;
  try {
    const value: unknown = JSON.parse(data);
    if (value && typeof value === "object" && typeof (value as { type?: unknown }).type === "string") {
      return value as ServerMessage;
    }
  } catch {
    // A broken frame is dropped; the next snapshot after a reconnect heals the board.
  }
  return null;
}

/**
 * Keeps one WebSocket open, reconnecting with exponential delay. Returns a function that stops it.
 * The server sends a fresh snapshot after each connect, so no state needs replaying here.
 */
export function connectSocket(options: SocketOptions): () => void {
  const createSocket = options.createSocket ?? ((url: string) => new WebSocket(url) as unknown as SocketLike);
  const silenceMs = options.silenceMs ?? 60000;
  let socket: SocketLike | null = null;
  let attempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let silenceTimer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;

  const clearSilence = () => {
    if (silenceTimer !== null) clearTimeout(silenceTimer);
    silenceTimer = null;
  };

  const armSilence = (current: SocketLike) => {
    clearSilence();
    silenceTimer = setTimeout(() => {
      if (socket === current) drop(current);
    }, silenceMs);
  };

  const drop = (current: SocketLike) => {
    if (socket !== current || stopped) return;
    socket = null;
    clearSilence();
    current.onopen = current.onmessage = current.onclose = current.onerror = null;
    try {
      current.close();
    } catch {
      // Already closed.
    }
    const delay = backoffDelay(attempt);
    attempt += 1;
    options.onState("reconnecting", delay);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      open();
    }, delay);
  };

  const open = () => {
    if (stopped) return;
    let current: SocketLike;
    try {
      current = createSocket(options.url);
    } catch {
      // An invalid URL or a blocked socket: count it as a failed attempt.
      const placeholder: SocketLike = {
        onopen: null, onmessage: null, onclose: null, onerror: null, close() {},
      };
      socket = placeholder;
      drop(placeholder);
      return;
    }
    socket = current;
    current.onopen = () => {
      if (socket !== current) return;
      attempt = 0;
      options.onState("open", null);
      armSilence(current);
    };
    current.onmessage = (event) => {
      if (socket !== current) return;
      armSilence(current);
      const message = parse(event.data);
      if (message) options.onMessage(message);
    };
    current.onclose = () => drop(current);
    current.onerror = () => drop(current);
  };

  options.onState("connecting", null);
  open();

  return () => {
    stopped = true;
    clearSilence();
    if (retryTimer !== null) clearTimeout(retryTimer);
    const current = socket;
    socket = null;
    if (current) {
      current.onopen = current.onmessage = current.onclose = current.onerror = null;
      current.close();
    }
  };
}
