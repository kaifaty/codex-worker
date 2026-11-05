import type { ServerMessage, TaskDetail, TaskFile } from "../types";
import { connectSocket, type ConnectionState } from "./socket";

/** Where the board gets its data: the real server, or the fixtures in `dev:fixture` mode. */
export interface BoardSource {
  /** Shown in the header next to the connection state, e.g. "фикстуры". */
  label: string | null;
  connect(onMessage: (message: ServerMessage) => void, onState: (state: ConnectionState, retryInMs: number | null) => void): () => void;
  /** null for an unknown task (404). */
  fetchDetail(slug: string): Promise<TaskDetail | null>;
  /** null when the file does not exist (404). */
  fetchFile(slug: string, name: TaskFile): Promise<string | null>;
}

async function get(path: string): Promise<Response | null> {
  const response = await fetch(path, { headers: { Accept: "application/json, text/plain" } });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`.trim());
  return response;
}

export function serverSource(): BoardSource {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return {
    label: null,
    connect: (onMessage, onState) => connectSocket({ url: `${scheme}://${location.host}/ws`, onMessage, onState }),
    async fetchDetail(slug) {
      const response = await get(`/api/tasks/${encodeURIComponent(slug)}`);
      return response ? ((await response.json()) as TaskDetail) : null;
    },
    async fetchFile(slug, name) {
      const response = await get(`/api/tasks/${encodeURIComponent(slug)}/files/${name}`);
      return response ? response.text() : null;
    },
  };
}
