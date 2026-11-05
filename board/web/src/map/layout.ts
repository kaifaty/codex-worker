// Where things stand on the island, in world units. Pure numbers: the scene and the tests share it.
// x grows to the east (right on screen), z to the south (towards the camera), the ground is y = 0.

import type { Place } from "./placement";

export interface Point {
  x: number;
  z: number;
}

/** A place an agent stands at; it faces the direction (sin face, cos face). */
export interface Spot extends Point {
  face: number;
}

const SOUTH = 0;
/** The agent stands a little east of its bench's middle, beside the monitor. */
const BENCH_AGENT_X = 0.2;
const EAST = Math.PI / 2;
const WEST = -Math.PI / 2;

/** The island: an ellipse with a fixed, slightly uneven coast. */
export const ISLAND = { x: 0, z: 1.5, rx: 15.6, rz: 11.4 };

/** Coast points, counter-clockwise seen from above; the same every time. */
export function islandOutline(count = 30): Point[] {
  const points: Point[] = [];
  for (let i = 0; i < count; i++) {
    const angle = (i / count) * Math.PI * 2;
    const bump = 1 + 0.035 * Math.sin(angle * 3 + 0.6) + 0.025 * Math.sin(angle * 7 + 1.9);
    points.push({ x: ISLAND.x + Math.cos(angle) * ISLAND.rx * bump, z: ISLAND.z - Math.sin(angle) * ISLAND.rz * bump });
  }
  return points;
}

/** 0 at the centre, 1 on the smooth ellipse; the uneven coast stays within ±6 % of it. */
export function islandRadius(p: Point): number {
  return Math.hypot((p.x - ISLAND.x) / ISLAND.rx, (p.z - ISLAND.z) / ISLAND.rz);
}

// Workbenches: a yard of 3 rows by 4 columns. The agent works behind its bench, facing the camera, so
// that a label over its head covers the furniture of the row behind and never an agent.
export const MAX_BENCHES = 12;
export const BENCH_COLUMNS = [-5.1, -1.7, 1.7, 5.1] as const;
export const BENCH_ROWS = [-4.5, 0, 4.5] as const;
/** Benches fill from the middle, so that a few tasks stand near the centre. [row, column] */
const BENCH_ORDER: readonly (readonly [number, number])[] = [
  [1, 1], [1, 2], [0, 1], [0, 2], [2, 1], [2, 2],
  [1, 0], [1, 3], [0, 0], [0, 3], [2, 0], [2, 3],
];

function benchCell(slot: number): readonly [number, number] {
  return BENCH_ORDER[((slot % MAX_BENCHES) + MAX_BENCHES) % MAX_BENCHES]!;
}

/** Centre of bench `slot`. */
export function benchAt(slot: number): Point {
  const [row, column] = benchCell(slot);
  return { x: BENCH_COLUMNS[column]!, z: BENCH_ROWS[row]! };
}

/** The yard of benches and their agents. */
export const YARD = { x0: -6.6, x1: 6.6, z0: -6.7, z1: 5.3 };

// Shared places. Each has a fixed landmark and slots that never overlap, for up to 12 agents.
export const STAND = { x: -11.7, z: -3.2 }; // the test stand, facing east
export const TABLES: readonly Point[] = [
  { x: 10.0, z: -4.6 },
  { x: 10.0, z: -2.4 },
  { x: 10.0, z: -0.2 },
];
export const GATE = { x: 11.9, z: 4.7 }; // the gate, crossed from west to east
export const TENTS: readonly Point[] = [
  { x: -11.6, z: 1.6 },
  { x: -9.75, z: 1.6 },
  { x: -7.9, z: 1.6 },
  { x: -11.6, z: 3.9 },
  { x: -9.75, z: 3.9 },
  { x: -7.9, z: 3.9 },
];
export const PIER = { x: 0, z0: 10.8, z1: 14.8, width: 3.2 };

/** Distance between neighbouring slots at a shared place; agents are about 0.6 wide. */
export const SLOT_STEP = 0.95;

/** Where the agent of `slot` stands at `place`. For a bench the slot is the bench number. */
export function spotAt(place: Place, slot: number): Spot {
  switch (place) {
    case "bench": {
      const bench = benchAt(slot);
      return { x: bench.x + BENCH_AGENT_X, z: bench.z - 0.85, face: SOUTH };
    }
    case "stand": {
      // A queue in front of the stand, 4 deep, filling away from it.
      const column = Math.floor(slot / 4);
      return { x: -10.0 + column * SLOT_STEP, z: -4.6 + (slot % 4) * SLOT_STEP, face: WEST };
    }
    case "review": {
      // The first three sit at a table; the rest wait beside the tables.
      const table = TABLES[slot];
      if (table) return { x: table.x - 0.85, z: table.z, face: EAST };
      const wait = slot - TABLES.length;
      if (wait < 7) return { x: 8.15, z: -4.8 + wait * SLOT_STEP, face: EAST };
      return { x: 11.75, z: -2.9 + (wait - 7) * SLOT_STEP, face: WEST };
    }
    case "gate": {
      const column = Math.floor(slot / 4);
      return { x: 10.3 - column * SLOT_STEP, z: 3.6 + (slot % 4) * 0.9, face: EAST };
    }
    case "tent": {
      // In front of a tent; past six tents, on mats in a row.
      const tent = TENTS[slot];
      if (tent) return { x: tent.x, z: tent.z + 0.85, face: SOUTH };
      return { x: -11.6 + (slot - TENTS.length) * 0.9, z: 6.5, face: SOUTH };
    }
    case "dock": {
      // Rows of three, the first at the end of the pier.
      const row = Math.floor(slot / 3);
      const offsets = [0, -SLOT_STEP, SLOT_STEP] as const;
      return { x: PIER.x + offsets[slot % 3]!, z: PIER.z1 - 0.6 - row * SLOT_STEP, face: SOUTH };
    }
  }
}

/** Seat of the second, reviewing agent at table `slot`, or null when the agent waits without a table. */
export function companionSpot(slot: number): Spot | null {
  const table = TABLES[slot];
  return table ? { x: table.x + 0.85, z: table.z, face: WEST } : null;
}

/** Whether the agent at `place` / `slot` has a seat (a stool or a table chair). */
export function seated(place: Place, slot: number): boolean {
  return place === "review" && slot < TABLES.length;
}

// Paths. Agents walk along lanes between the bench rows, the two side roads and short spurs to the
// shared places, so that they never cut through benches.
const LANES = BENCH_ROWS.map((z) => z - 1.9);
const SIDE = 7.2;
const NODES: readonly Point[] = [
  { x: -SIDE, z: LANES[0]! }, // 0 L0
  { x: -SIDE, z: LANES[1]! }, // 1 L1
  { x: -SIDE, z: LANES[2]! }, // 2 L2
  { x: SIDE, z: LANES[0]! }, // 3 R0
  { x: SIDE, z: LANES[1]! }, // 4 R1
  { x: SIDE, z: LANES[2]! }, // 5 R2
  { x: 0, z: LANES[2]! }, // 6 M2, where the dock path leaves lane 2
  { x: -8.4, z: -0.9 }, // 7 stand
  { x: 7.9, z: 1.0 }, // 8 review
  { x: 8.2, z: 4.6 }, // 9 gate
  { x: -7.2, z: 5.6 }, // 10 tents
  { x: 0, z: 10.6 }, // 11 dock
];
const EDGES: readonly (readonly [number, number])[] = [
  [0, 3], [1, 4], [2, 6], [6, 5], // lanes
  [0, 1], [1, 2], [3, 4], [4, 5], // side roads
  [6, 11], // dock path
  [7, 1], [7, 2], [8, 4], [8, 5], [9, 5], [10, 2],
];
/** The paths agents walk, as segments to draw on the ground. */
export const PATHS: readonly (readonly [Point, Point])[] = EDGES.map(([a, b]) => [NODES[a]!, NODES[b]!] as const);
const DOOR: Record<Exclude<Place, "bench">, number> = { stand: 7, review: 8, gate: 9, tent: 10, dock: 11 };

/** Where a point joins the path network: a point on edge [a, b] (a == b for a node). */
export interface Attach {
  at: Point;
  edge: readonly [number, number];
}

/** How the agent resting at `place` / `slot` joins the path network. */
export function attachOf(place: Place, slot: number): Attach {
  if (place !== "bench") {
    const node = DOOR[place];
    return { at: NODES[node]!, edge: [node, node] };
  }
  const [row, column] = benchCell(slot);
  const at = { x: BENCH_COLUMNS[column]!, z: LANES[row]! };
  const edge: readonly [number, number] = row === 0 ? [0, 3] : row === 1 ? [1, 4] : at.x < 0 ? [2, 6] : [6, 5];
  return { at, edge };
}

const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.z - b.z);
const sameEdge = (a: readonly [number, number], b: readonly [number, number]) =>
  (a[0] === b[0] && a[1] === b[1]) || (a[0] === b[1] && a[1] === b[0]);

/** Waypoints along the network from `from.at` to `to.at`, both included, each with its own attachment. */
export function route(from: Attach, to: Attach): Attach[] {
  // Dijkstra over the nodes plus two virtual ones: FROM = n, TO = n + 1.
  const n = NODES.length;
  const point = (i: number) => (i === n ? from.at : i === n + 1 ? to.at : NODES[i]!);
  const links: [number, number][][] = Array.from({ length: n + 2 }, () => []);
  const link = (a: number, b: number) => {
    const d = distance(point(a), point(b));
    links[a]!.push([b, d]);
    links[b]!.push([a, d]);
  };
  for (const [a, b] of EDGES) link(a, b);
  for (const node of new Set(from.edge)) link(n, node);
  for (const node of new Set(to.edge)) link(n + 1, node);
  if (sameEdge(from.edge, to.edge)) link(n, n + 1);

  const best = new Array<number>(n + 2).fill(Infinity);
  const previous = new Array<number>(n + 2).fill(-1);
  const done = new Array<boolean>(n + 2).fill(false);
  best[n] = 0;
  for (;;) {
    let current = -1;
    for (let i = 0; i < n + 2; i++) if (!done[i] && best[i]! < Infinity && (current < 0 || best[i]! < best[current]!)) current = i;
    if (current < 0 || current === n + 1) break;
    done[current] = true;
    for (const [next, d] of links[current]!) {
      if (best[current]! + d < best[next]!) {
        best[next] = best[current]! + d;
        previous[next] = current;
      }
    }
  }
  if (best[n + 1] === Infinity) return [from, to];
  const path: Attach[] = [];
  for (let i = n + 1; i >= 0; i = previous[i]!) {
    const step: Attach = i === n ? from : i === n + 1 ? to : { at: NODES[i]!, edge: [i, i] };
    const last = path[path.length - 1];
    if (!last || distance(last.at, step.at) > 1e-6) path.push(step);
    if (i === n) break;
  }
  return path.reverse();
}
