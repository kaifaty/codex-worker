import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoffDelay, connectSocket, type ConnectionState, type SocketLike } from "./socket";
import type { ServerMessage } from "../types";

class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.({});
  }
  send(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  fail() {
    this.onclose?.({});
  }
}

const last = () => FakeSocket.all[FakeSocket.all.length - 1]!;

describe("connectSocket", () => {
  let states: [ConnectionState, number | null][];
  let messages: ServerMessage[];
  let stop: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    states = [];
    messages = [];
    stop = connectSocket({
      url: "ws://localhost/ws",
      onMessage: (m) => messages.push(m),
      onState: (s, retry) => states.push([s, retry]),
      createSocket: (url) => new FakeSocket(url),
      silenceMs: 60000,
    });
  });

  afterEach(() => {
    stop();
    vi.useRealTimers();
  });

  it("delays grow from 1 s and stop at 30 s", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 20].map(backoffDelay)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  });

  it("reconnects with exponential delay and resets it after a successful open", () => {
    expect(states).toEqual([["connecting", null]]);
    last().fail();
    expect(states.at(-1)).toEqual(["reconnecting", 1000]);
    vi.advanceTimersByTime(999);
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(2);

    last().fail();
    expect(states.at(-1)).toEqual(["reconnecting", 2000]);
    vi.advanceTimersByTime(2000);
    last().fail();
    expect(states.at(-1)).toEqual(["reconnecting", 4000]);
    for (const delay of [4000, 8000, 16000, 30000]) {
      vi.advanceTimersByTime(delay);
      last().fail();
    }
    expect(states.at(-1)).toEqual(["reconnecting", 30000]);
    vi.advanceTimersByTime(30000);

    last().open();
    expect(states.at(-1)).toEqual(["open", null]);
    last().fail();
    expect(states.at(-1)).toEqual(["reconnecting", 1000]);
  });

  it("passes messages through and takes a fresh snapshot after a reconnect", () => {
    const first = last();
    first.open();
    first.send({ type: "ping", at: 1 });
    first.onmessage?.({ data: "not json" });
    first.fail();
    expect(first.closed).toBe(true);
    vi.advanceTimersByTime(1000);
    const second = last();
    expect(second).not.toBe(first);
    second.open();
    const snapshot = { type: "snapshot", snapshot: { generated_at: 2, tasks: [], accounts: { fetched_at: null, error: null, data: null } } };
    second.send(snapshot);
    first.send({ type: "ping", at: 99 }); // a stale socket is ignored
    expect(messages).toEqual([{ type: "ping", at: 1 }, snapshot]);
  });

  it("drops a silent connection and stops for good when asked", () => {
    last().open();
    vi.advanceTimersByTime(59999);
    expect(states.at(-1)?.[0]).toBe("open");
    vi.advanceTimersByTime(1);
    expect(states.at(-1)).toEqual(["reconnecting", 1000]);
    stop();
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.all).toHaveLength(1);
  });
});
