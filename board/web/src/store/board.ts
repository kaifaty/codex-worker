import { computed, onBeforeUnmount, ref, shallowRef } from "vue";
import type { BoardSource } from "../lib/source";
import type { ConnectionState } from "../lib/socket";
import { emptyState, reduce, type BoardState } from "./reduce";

export function createBoard(source: BoardSource) {
  const state = shallowRef<BoardState>(emptyState);
  const connection = ref<ConnectionState>("connecting");
  const retryInMs = ref<number | null>(null);
  let stop: (() => void) | null = null;

  const start = () => {
    stop?.();
    stop = source.connect(
      (message) => {
        state.value = reduce(state.value, message, Date.now());
      },
      (next, retry) => {
        connection.value = next;
        retryInMs.value = retry;
      },
    );
  };

  return {
    source,
    state,
    connection,
    retryInMs,
    tasks: computed(() => Object.values(state.value.tasks)),
    start,
    stop: () => {
      stop?.();
      stop = null;
    },
  };
}

export type Board = ReturnType<typeof createBoard>;

/** Current time in Unix seconds, ticking once per second while the component lives. */
export function useNow() {
  const now = ref(Date.now() / 1000);
  const timer = setInterval(() => {
    now.value = Date.now() / 1000;
  }, 1000);
  onBeforeUnmount(() => clearInterval(timer));
  return now;
}
