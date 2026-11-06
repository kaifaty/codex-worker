// `npm run dev:fixture`: plays the fixtures instead of talking to the server. The fixture times are
// shifted so that the snapshot looks fresh and stream message i happens i seconds after it.
import type { BoardSource } from "../lib/source";
import { emptyState, reduce, type BoardState } from "../store/reduce";
import type { ServerMessage, Snapshot, TaskDetail, TaskFile } from "../types";
import snapshotJson from "./snapshot.json";
import streamJson from "./stream.json";
import detailsJson from "./details.json";

interface FixtureDetail {
  timeline: { at: number; text: string }[];
  files: Partial<Record<TaskFile, string>>;
}

const snapshot = snapshotJson as unknown as Snapshot;
const stream = streamJson as unknown as ServerMessage[];
const details = detailsJson as unknown as Record<string, FixtureDetail>;
const BASE = snapshot.generated_at;
const STEP_MS = 1000;

const TIME_KEYS = new Set([
  "generated_at", "phase_started_at", "sleeping_until", "created", "updated_at", "at", "fetched_at",
  "resets_at", "limited_until", "expires_at", "since", "checked_at", "sampled_at",
]);

function shiftValue(value: unknown, delta: number): unknown {
  if (typeof value === "number") return value + delta;
  if (typeof value === "string") {
    const ms = Date.parse(value);
    return Number.isNaN(ms) ? value : new Date(ms + delta * 1000).toISOString();
  }
  if (Array.isArray(value)) return value.map((item) => shiftValue(item, delta));
  return value;
}

/** Copies `value` with every known time field moved by `delta` seconds. */
export function shiftTimes<T>(value: T, delta: number): T {
  if (Array.isArray(value)) return value.map((item) => shiftTimes(item, delta)) as T;
  if (typeof value !== "object" || value === null) return value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    out[key] = TIME_KEYS.has(key) && item !== null ? shiftValue(item, delta) : shiftTimes(item, delta);
  }
  return out as T;
}

export function fixtureSource(): BoardSource {
  let state: BoardState = emptyState;
  let delta = 0;
  return {
    label: "фикстуры",
    connect(onMessage, onState) {
      let index = -1; // -1 = the snapshot, then the stream; the cycle repeats like a reconnect.
      const emit = (message: ServerMessage) => {
        state = reduce(state, message, Date.now());
        onMessage(message);
      };
      const step = () => {
        const now = Date.now() / 1000;
        if (index < 0) {
          delta = now - BASE;
          emit(shiftTimes<ServerMessage>({ type: "snapshot", snapshot }, delta));
        } else {
          const message = stream[index];
          if (message) emit(shiftTimes(message, now - (BASE + index + 1)));
        }
        index = index + 1 >= stream.length ? -1 : index + 1;
      };
      onState("connecting", null);
      onState("open", null);
      step();
      const timer = setInterval(step, STEP_MS);
      return () => clearInterval(timer);
    },
    async fetchDetail(slug) {
      const task = state.tasks[slug];
      if (!task) return null;
      const extra = details[slug];
      const timeline = extra
        ? shiftTimes(extra.timeline, delta)
        : task.created !== null
          ? [{ at: task.created, text: "задача создана" }]
          : [];
      const files = (["brief", "last", "review", "check"] as const).filter((name) => extra?.files[name] !== undefined);
      const detail: TaskDetail = { ...task, timeline, files };
      return detail;
    },
    async fetchFile(slug, name) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      return details[slug]?.files[name] ?? null;
    },
  };
}
