// Everything on the island, built from three.js primitives: no model files, textures or images.

import * as THREE from "three";
import type { Palette, Tone } from "./palette";
import {
  GATE,
  ISLAND,
  PIER,
  STAND,
  TABLES,
  TENTS,
  PATHS,
  islandOutline,
  type Point,
} from "./layout";

/** Shared geometries and palette materials; one dispose() frees them all. */
export class Kit {
  private palette: Palette;
  private readonly solid = new Map<Tone, THREE.MeshLambertMaterial>();
  private readonly glows = new Map<string, { tone: Tone; material: THREE.MeshBasicMaterial }>();
  private readonly geometries = new Map<string, THREE.BufferGeometry>();
  private readonly owned: THREE.BufferGeometry[] = [];

  constructor(palette: Palette) {
    this.palette = palette;
  }

  /** A flat-shaded, lit material of palette colour `tone`. */
  mat(tone: Tone): THREE.MeshLambertMaterial {
    let material = this.solid.get(tone);
    if (!material) {
      material = new THREE.MeshLambertMaterial({ color: this.palette[tone], flatShading: true });
      this.solid.set(tone, material);
    }
    return material;
  }

  /** An unlit material: screens, lamps and the beacon look lit in any light. */
  glow(tone: Tone, opacity = 1): THREE.MeshBasicMaterial {
    const key = `${tone}/${opacity}`;
    let entry = this.glows.get(key);
    if (!entry) {
      const material = new THREE.MeshBasicMaterial({ color: this.palette[tone], transparent: opacity < 1, opacity });
      if (opacity < 1) material.depthWrite = false;
      entry = { tone, material };
      this.glows.set(key, entry);
    }
    return entry.material;
  }

  /** A geometry made once and shared under `key`. */
  geo<T extends THREE.BufferGeometry>(key: string, make: () => T): T {
    let geometry = this.geometries.get(key);
    if (!geometry) {
      geometry = make();
      this.geometries.set(key, geometry);
    }
    return geometry as T;
  }

  /** A geometry used once; disposed with the kit. */
  own<T extends THREE.BufferGeometry>(geometry: T): T {
    this.owned.push(geometry);
    return geometry;
  }

  get colors(): Palette {
    return this.palette;
  }

  setPalette(palette: Palette): void {
    this.palette = palette;
    for (const [tone, material] of this.solid) material.color.set(palette[tone]);
    for (const { tone, material } of this.glows.values()) material.color.set(palette[tone]);
    this.shadow.color.set(palette.shadow);
    this.shadow.opacity = palette.shadowOpacity;
  }

  /** The material of the soft round shadows under agents and things. */
  readonly shadow = new THREE.MeshBasicMaterial({ color: "#000", transparent: true, opacity: 0.2, depthWrite: false });

  dispose(): void {
    for (const material of this.solid.values()) material.dispose();
    for (const { material } of this.glows.values()) material.dispose();
    for (const geometry of this.geometries.values()) geometry.dispose();
    for (const geometry of this.owned) geometry.dispose();
    this.shadow.dispose();
    this.solid.clear();
    this.glows.clear();
    this.geometries.clear();
    this.owned.length = 0;
  }
}

function mesh(geometry: THREE.BufferGeometry, material: THREE.Material, x = 0, y = 0, z = 0): THREE.Mesh {
  const result = new THREE.Mesh(geometry, material);
  result.position.set(x, y, z);
  return result;
}

const box = (kit: Kit, w: number, h: number, d: number) => kit.geo(`box ${w} ${h} ${d}`, () => new THREE.BoxGeometry(w, h, d));
const cylinder = (kit: Kit, top: number, bottom: number, h: number, segments: number) =>
  kit.geo(`cyl ${top} ${bottom} ${h} ${segments}`, () => new THREE.CylinderGeometry(top, bottom, h, segments));
const sphere = (kit: Kit, r: number, w = 10, h = 7) => kit.geo(`sph ${r} ${w} ${h}`, () => new THREE.SphereGeometry(r, w, h));

/** A soft round shadow on the ground. */
export function blob(kit: Kit, radius: number, x = 0, z = 0): THREE.Mesh {
  const geometry = kit.geo(`blob ${radius}`, () => new THREE.CircleGeometry(radius, 16).rotateX(-Math.PI / 2));
  const result = mesh(geometry, kit.shadow, x, 0.03, z);
  result.renderOrder = 1;
  return result;
}

// Agents ---------------------------------------------------------------------------------------------

export interface Figure {
  /** At the feet; rotation.y turns the agent, its front is local +z. */
  root: THREE.Group;
  /** Moves up and down for walking and sitting. */
  body: THREE.Group;
  armLeft: THREE.Group;
  armRight: THREE.Group;
  legLeft: THREE.Group;
  legRight: THREE.Group;
  tool: THREE.Object3D;
  crate: THREE.Object3D;
  bulb: THREE.Object3D;
  shadow: THREE.Mesh;
  /** Meshes a click can hit. */
  parts: THREE.Mesh[];
}

/**
 * An agent: Codex is angular (square body and head, a diamond on top), Claude is round (capsules and a
 * ball head). A reviewer carries a clipboard.
 */
export function buildFigure(kit: Kit, family: "codex" | "claude", reviewer = false): Figure {
  const root = new THREE.Group();
  const body = new THREE.Group();
  root.add(body);
  const parts: THREE.Mesh[] = [];
  const add = <T extends THREE.Mesh>(parent: THREE.Object3D, item: T): T => {
    parent.add(item);
    parts.push(item);
    return item;
  };
  const main = kit.mat(family);
  const soft = kit.mat(family === "codex" ? "codexSoft" : "claudeSoft");
  const dark = kit.mat(family === "codex" ? "codexDark" : "claudeDark");

  const limb = (x: number, y: number) => {
    const pivot = new THREE.Group();
    pivot.position.set(x, y, 0);
    body.add(pivot);
    return pivot;
  };
  const legLeft = limb(-0.12, 0.44);
  const legRight = limb(0.12, 0.44);
  const armLeft = limb(-0.33, 0.9);
  const armRight = limb(0.33, 0.9);

  if (family === "codex") {
    for (const leg of [legLeft, legRight]) add(leg, mesh(box(kit, 0.15, 0.44, 0.17), dark, 0, -0.22, 0));
    // A tapered square torso, wider at the shoulders.
    const torso = add(body, mesh(cylinder(kit, 0.33, 0.24, 0.52, 4), main, 0, 0.7, 0));
    torso.rotation.y = Math.PI / 4;
    for (const arm of [armLeft, armRight]) add(arm, mesh(box(kit, 0.12, 0.42, 0.13), main, 0, -0.2, 0));
    add(body, mesh(box(kit, 0.4, 0.36, 0.36), soft, 0, 1.16, 0));
    add(body, mesh(box(kit, 0.3, 0.08, 0.03), dark, 0, 1.18, 0.185));
    add(body, mesh(kit.geo("diamond", () => new THREE.OctahedronGeometry(0.12)), main, 0, 1.5, 0));
  } else {
    for (const leg of [legLeft, legRight]) {
      add(leg, mesh(kit.geo("leg claude", () => new THREE.CapsuleGeometry(0.08, 0.28, 3, 8)), dark, 0, -0.22, 0));
    }
    add(body, mesh(kit.geo("torso claude", () => new THREE.CapsuleGeometry(0.26, 0.16, 5, 12)), main, 0, 0.72, 0));
    for (const arm of [armLeft, armRight]) {
      add(arm, mesh(kit.geo("arm claude", () => new THREE.CapsuleGeometry(0.07, 0.28, 3, 8)), main, 0, -0.2, 0));
    }
    add(body, mesh(sphere(kit, 0.23, 14, 10), soft, 0, 1.2, 0));
    add(body, mesh(sphere(kit, 0.1, 10, 7), main, 0, 1.47, 0));
  }

  // Props, shown by the scene for the action.
  const tool = new THREE.Group();
  tool.add(mesh(cylinder(kit, 0.025, 0.025, 0.36, 5), kit.mat("woodDark"), 0, -0.16, 0));
  tool.add(mesh(box(kit, 0.2, 0.08, 0.08), kit.mat("metalDark"), 0, -0.34, 0));
  tool.position.set(0, -0.36, 0.06);
  tool.rotation.x = Math.PI / 2;
  armRight.add(tool);

  const crate = new THREE.Group();
  crate.add(mesh(box(kit, 0.48, 0.36, 0.38), kit.mat("crate")));
  crate.add(mesh(box(kit, 0.5, 0.08, 0.4), kit.mat("ready"), 0, 0.06, 0));
  crate.position.set(0, 0.72, 0.36);
  body.add(crate);

  const bulb = new THREE.Group();
  bulb.add(mesh(sphere(kit, 0.11, 10, 7), kit.glow("lamp")));
  bulb.add(mesh(sphere(kit, 0.2, 10, 7), kit.glow("lamp", 0.28)));
  bulb.add(mesh(cylinder(kit, 0.05, 0.05, 0.07, 6), kit.mat("metalDark"), 0, -0.12, 0));
  bulb.position.set(0.34, 1.62, 0.05);
  body.add(bulb);

  if (reviewer) {
    const board = mesh(box(kit, 0.26, 0.34, 0.03), kit.mat("paper"), 0, -0.38, 0.1);
    board.rotation.x = -0.6;
    armLeft.add(board);
  }

  const shadow = blob(kit, 0.42);
  root.add(shadow);
  root.scale.setScalar(1.2);
  for (const part of parts) part.userData.figure = true;
  return { root, body, armLeft, armRight, legLeft, legRight, tool, crate, bulb, shadow, parts };
}

// Workbenches ---------------------------------------------------------------------------------------

export interface BenchModel {
  root: THREE.Group;
  /** The ground around the bench; red while the task needs attention. */
  patch: THREE.Mesh;
  /** Lit while the agent types: the keyboard and the light on the back of the monitor. */
  keys: THREE.Mesh;
  led: THREE.Mesh;
  stool: THREE.Object3D;
  beacon: THREE.Group;
  /** The lamp of the beacon; the scene scales it with the camera distance. */
  lamp: THREE.Object3D;
  parts: THREE.Mesh[];
}

/**
 * A workbench. Its agent works behind it (north), facing the camera; the monitor faces the agent, and
 * its back light and the keyboard show the camera when the terminal is in use.
 */
export function buildBench(kit: Kit): BenchModel {
  const root = new THREE.Group();
  const parts: THREE.Mesh[] = [];
  const add = (item: THREE.Mesh) => {
    root.add(item);
    parts.push(item);
    return item;
  };
  const wood = kit.mat("wood");
  const dark = kit.mat("woodDark");
  // Trodden ground around the bench and its agent.
  const patch = mesh(cylinder(kit, 1.3, 1.3, 0.03, 8), kit.mat("yard"), 0, 0.02, -0.4);
  patch.scale.z = 0.9;
  patch.rotation.y = Math.PI / 8;
  root.add(patch);
  add(mesh(box(kit, 1.56, 0.09, 0.7), wood, 0, 0.76, 0));
  add(mesh(box(kit, 0.09, 0.72, 0.62), dark, -0.68, 0.36, 0));
  add(mesh(box(kit, 0.09, 0.72, 0.62), dark, 0.68, 0.36, 0));
  add(mesh(box(kit, 0.1, 0.2, 0.1), kit.mat("metalDark"), -0.42, 0.9, 0.18));
  add(mesh(box(kit, 0.62, 0.44, 0.08), kit.mat("metalDark"), -0.42, 1.2, 0.18));
  const led = add(mesh(box(kit, 0.5, 0.07, 0.02), kit.glow("screenOff"), -0.42, 1.34, 0.225));
  const keys = add(mesh(box(kit, 0.5, 0.03, 0.2), kit.mat("metalDark"), 0.2, 0.82, -0.14));
  root.add(blob(kit, 0.95));

  const stool = mesh(cylinder(kit, 0.2, 0.17, 0.46, 6), dark, 0.2, 0.23, -0.85);
  stool.visible = false;
  root.add(stool);

  // The beacon stands at the front corner, below the label over the agent.
  const beacon = new THREE.Group();
  beacon.add(mesh(cylinder(kit, 0.04, 0.05, 1.3, 5), kit.mat("metalDark"), 0, 0.65, 0));
  const lamp = new THREE.Group();
  lamp.position.y = 1.42;
  lamp.add(mesh(sphere(kit, 0.17, 10, 7), kit.glow("beacon")));
  lamp.add(mesh(sphere(kit, 0.34, 12, 8), kit.glow("beacon", 0.3)));
  beacon.add(lamp);
  beacon.position.set(0.74, 0, 0.3);
  beacon.visible = false;
  root.add(beacon);
  return { root, patch, keys, led, stool, beacon, lamp, parts };
}

// Shared places -------------------------------------------------------------------------------------

export interface PlaceModels {
  /** Lights of the test stand, lit in turn while a check runs, and its roof lamp. */
  standLights: THREE.Mesh[];
  standLamp: THREE.Mesh;
  /** Turns while a check runs. */
  standFan: THREE.Object3D;
  /** The CI light over the gate and its segments. */
  gateLamp: THREE.Mesh;
  gateLights: THREE.Mesh[];
  tents: THREE.Object3D[];
  tables: THREE.Object3D[];
}

function standModel(kit: Kit, group: THREE.Group): Pick<PlaceModels, "standLights" | "standLamp" | "standFan"> {
  const metal = kit.mat("metal");
  const dark = kit.mat("metalDark");
  const at = (x: number, y: number, z: number) => [STAND.x + x, y, STAND.z + z] as const;
  group.add(mesh(box(kit, 1.5, 1.2, 2.7), metal, ...at(0, 0.6, 0)));
  group.add(mesh(box(kit, 1.2, 0.26, 2.3), dark, ...at(0, 1.33, 0)));
  group.add(mesh(box(kit, 0.06, 0.5, 2.1), dark, ...at(0.77, 0.72, 0)));
  group.add(blob(kit, 1.7, STAND.x, STAND.z));
  const standLights: THREE.Mesh[] = [];
  for (let i = 0; i < 5; i++) {
    const light = mesh(box(kit, 0.05, 0.16, 0.26), kit.glow("screenOff"), ...at(0.81, 0.74, -0.84 + i * 0.42));
    standLights.push(light);
    group.add(light);
  }
  // A small turbine on the roof.
  const fan = new THREE.Group();
  fan.position.set(...at(0, 1.62, 0.55));
  fan.add(mesh(cylinder(kit, 0.07, 0.07, 0.36, 6), dark, 0, 0, 0));
  for (let i = 0; i < 3; i++) {
    const blade = mesh(box(kit, 0.62, 0.04, 0.14), metal, 0, 0.16, 0);
    blade.rotation.y = (i * Math.PI) / 3;
    fan.add(blade);
  }
  group.add(fan);
  group.add(mesh(cylinder(kit, 0.12, 0.14, 0.7, 6), dark, ...at(-0.25, 1.75, -0.65)));
  group.add(mesh(cylinder(kit, 0.05, 0.05, 0.4, 5), dark, ...at(0.3, 1.66, -0.6)));
  const standLamp = mesh(sphere(kit, 0.22, 12, 8), kit.glow("lampOff"), ...at(0.3, 1.95, -0.6));
  group.add(standLamp);
  return { standLights, standLamp, standFan: fan };
}

function gateModel(kit: Kit, group: THREE.Group): Pick<PlaceModels, "gateLamp" | "gateLights"> {
  const wood = kit.mat("woodDark");
  for (const dz of [-1.35, 1.35]) {
    group.add(mesh(box(kit, 0.32, 2.4, 0.32), wood, GATE.x, 1.2, GATE.z + dz));
    group.add(blob(kit, 0.4, GATE.x, GATE.z + dz));
  }
  group.add(mesh(box(kit, 0.46, 0.34, 3.3), kit.mat("wood"), GATE.x, 2.5, GATE.z));
  const gateLights: THREE.Mesh[] = [];
  for (let i = 0; i < 5; i++) {
    const light = mesh(box(kit, 0.05, 0.16, 0.4), kit.glow("screenOff"), GATE.x - 0.25, 2.5, GATE.z - 1.0 + i * 0.5);
    gateLights.push(light);
    group.add(light);
  }
  group.add(mesh(cylinder(kit, 0.05, 0.05, 0.3, 5), kit.mat("metalDark"), GATE.x, 2.82, GATE.z));
  const gateLamp = mesh(sphere(kit, 0.24, 12, 8), kit.glow("lampOff"), GATE.x, 3.1, GATE.z);
  group.add(gateLamp);
  return { gateLamp, gateLights };
}

function tentModel(kit: Kit, at: Point, index: number): THREE.Object3D {
  const tent = new THREE.Group();
  const cover = mesh(kit.geo("tent", () => new THREE.ConeGeometry(0.85, 1.15, 4)), kit.mat(index % 2 ? "canvasAlt" : "canvas"), 0, 0.575, 0);
  cover.rotation.y = Math.PI / 4;
  tent.add(cover);
  const door = mesh(kit.geo("tent door", () => new THREE.CircleGeometry(0.3, 3)), kit.mat("woodDark"), 0, 0.26, 0.475);
  door.rotation.z = Math.PI / 2;
  door.rotation.x = -0.45;
  tent.add(door);
  tent.add(blob(kit, 0.95));
  tent.position.set(at.x, 0, at.z);
  return tent;
}

function tableModel(kit: Kit, at: Point): THREE.Object3D {
  const table = new THREE.Group();
  table.add(mesh(cylinder(kit, 0.56, 0.56, 0.07, 8), kit.mat("wood"), 0, 0.68, 0));
  table.add(mesh(cylinder(kit, 0.08, 0.14, 0.66, 6), kit.mat("woodDark"), 0, 0.33, 0));
  const paper = mesh(box(kit, 0.3, 0.01, 0.4), kit.mat("paper"), 0.05, 0.72, 0.04);
  paper.rotation.y = 0.3;
  table.add(paper);
  for (const dx of [-0.85, 0.85]) table.add(mesh(cylinder(kit, 0.2, 0.17, 0.42, 6), kit.mat("woodDark"), dx, 0.21, 0));
  table.add(blob(kit, 0.75));
  table.position.set(at.x, 0, at.z);
  return table;
}

function dockModel(kit: Kit, group: THREE.Group): void {
  const length = PIER.z1 - PIER.z0;
  const middle = (PIER.z0 + PIER.z1) / 2;
  group.add(mesh(box(kit, PIER.width, 0.14, length), kit.mat("wood"), PIER.x, 0.05, middle));
  for (let i = 0; i < 6; i++) {
    const plank = mesh(box(kit, PIER.width + 0.04, 0.02, 0.05), kit.mat("woodDark"), PIER.x, 0.125, PIER.z0 + 0.4 + i * 0.7);
    group.add(plank);
  }
  for (const dx of [-PIER.width / 2 + 0.12, PIER.width / 2 - 0.12]) {
    for (const z of [PIER.z0 + 1.6, PIER.z1 - 0.15]) {
      group.add(mesh(cylinder(kit, 0.1, 0.1, 1.1, 6), kit.mat("woodDark"), PIER.x + dx, -0.3, z));
    }
  }
}

function tree(kit: Kit, at: Point, size: number, dark: boolean): THREE.Object3D {
  const item = new THREE.Group();
  item.add(mesh(cylinder(kit, 0.09, 0.13, 0.6, 5), kit.mat("trunk"), 0, 0.3, 0));
  item.add(mesh(kit.geo("crown", () => new THREE.ConeGeometry(0.62, 1.3, 6)), kit.mat(dark ? "leafDark" : "leaf"), 0, 1.15, 0));
  item.add(mesh(kit.geo("crown top", () => new THREE.ConeGeometry(0.45, 0.9, 6)), kit.mat(dark ? "leaf" : "leafDark"), 0, 1.7, 0));
  item.add(blob(kit, 0.6));
  item.position.set(at.x, 0, at.z);
  item.scale.setScalar(size);
  return item;
}

const TREES: readonly [number, number, number][] = [
  [-9.2, -6.0, 1.0], [-5.6, -7.7, 0.9], [-2.2, -8.3, 1.15], [1.5, -8.2, 0.95], [4.8, -7.8, 1.1], [13.8, 1.9, 0.9],
  [-3.6, 7.0, 0.85], [3.6, 7.2, 0.95], [-4.6, 9.6, 1.0], [-7.8, 8.9, 0.9], [4.6, 9.7, 1.1], [8.6, 9.0, 0.95],
];
const ROCKS: readonly [number, number, number][] = [
  [-12.4, -6.0, 0.5], [13.0, -3.8, 0.4], [-3.2, 11.9, 0.35], [11.2, 8.8, 0.45], [-14.6, 2.4, 0.4], [2.8, 11.8, 0.3],
];

/** Builds the island and the shared places into `group`. */
export function buildWorld(kit: Kit, group: THREE.Group): PlaceModels {
  const outline = islandOutline();
  const shape = (scale: number) =>
    new THREE.Shape(outline.map((p) => new THREE.Vector2(p.x * scale, -(ISLAND.z + (p.z - ISLAND.z) * scale))));

  const water = mesh(kit.own(new THREE.CircleGeometry(220, 48).rotateX(-Math.PI / 2)), kit.mat("water"), 0, -0.42, 0);
  group.add(water);
  const shallow = mesh(kit.own(new THREE.ShapeGeometry(shape(1.13)).rotateX(-Math.PI / 2)), kit.mat("shallow"), 0, -0.4, 0);
  group.add(shallow);
  const ground = kit.own(new THREE.ExtrudeGeometry(shape(1), { depth: 1.3, bevelEnabled: false }));
  ground.rotateX(-Math.PI / 2).translate(0, -1.3, 0);
  group.add(mesh(ground, kit.mat("sand")));
  const grass = mesh(kit.own(new THREE.ShapeGeometry(shape(0.9)).rotateX(-Math.PI / 2)), kit.mat("grass"), 0, 0.012, 0);
  group.add(grass);

  // The paths agents walk: lanes between the bench rows, side roads and spurs to the shared places.
  const path = (a: Point, b: Point, width: number) => {
    const length = Math.hypot(b.x - a.x, b.z - a.z);
    const strip = mesh(box(kit, width, 0.02, 1), kit.mat("path"), (a.x + b.x) / 2, 0.016, (a.z + b.z) / 2);
    strip.scale.z = length + width;
    strip.rotation.y = Math.atan2(b.x - a.x, b.z - a.z);
    group.add(strip);
  };
  for (const [a, b] of PATHS) path(a, b, 0.7);
  path({ x: GATE.x, z: GATE.z }, { x: GATE.x + 2.4, z: GATE.z }, 0.7);

  const places = new THREE.Group();
  group.add(places);
  const stand = standModel(kit, places);
  const gate = gateModel(kit, places);
  const tents = TENTS.map((at, index) => tentModel(kit, at, index));
  const tables = TABLES.map((at) => tableModel(kit, at));
  for (const item of [...tents, ...tables]) places.add(item);
  dockModel(kit, places);

  for (const [x, z, size] of TREES) group.add(tree(kit, { x, z }, size, (x * 7 + z) % 2 > 0));
  const rockGeometry = kit.geo("rock", () => new THREE.DodecahedronGeometry(1, 0));
  for (const [x, z, size] of ROCKS) {
    const rock = mesh(rockGeometry, kit.mat("rock"), x, size * 0.3, z);
    rock.scale.set(size * 1.2, size * 0.75, size);
    rock.rotation.y = x;
    group.add(rock);
  }
  return { ...stand, ...gate, tents, tables };
}
