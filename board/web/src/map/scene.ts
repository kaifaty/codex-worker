// The island in three.js. It draws what `placement` decided and moves the HTML labels of the overlay;
// it decides nothing about the tasks itself.

import * as THREE from "three";
import { MapControls } from "three/addons/controls/MapControls.js";
import type { TaskSummary } from "../types";
import type { Action, Placement } from "./placement";
import { Kit, buildBench, buildFigure, buildWorld, type BenchModel, type Figure, type PlaceModels } from "./models";
import { DAY, DUSK } from "./palette";
import {
  GATE,
  PIER,
  STAND,
  TABLES,
  TENTS,
  YARD,
  attachOf,
  benchAt,
  companionSpot,
  route,
  seated,
  spotAt,
  type Attach,
  type Point,
  type Spot,
} from "./layout";

export interface SceneEntry {
  task: TaskSummary;
  placement: Placement;
  /** The task's bench number on the island. */
  bench: number;
}

export type ZoomLevel = "far" | "mid" | "close";

export interface SceneOptions {
  /** Gets the canvas. */
  host: HTMLElement;
  /** Holds the labels: elements with data-anchor, data-align ("above" | "below") and data-priority. */
  overlay: HTMLElement;
  /** Pixels at the top of the host that labels keep clear of (the toolbar). */
  topInset(): number;
  onPick(slug: string): void;
  onZoom(level: ZoomLevel): void;
  /** The WebGL context is gone; the scene no longer draws. */
  onLost(): void;
}

export interface IslandScene {
  setEntries(entries: readonly SceneEntry[]): void;
  setSelected(slug: string | null): void;
  /** Draws only while active; the scene also stops by itself while the document is hidden. */
  setActive(active: boolean): void;
  /** The label elements or their text changed. */
  refreshLabels(): void;
  dispose(): void;
}

export class NoWebGLError extends Error {}

const WALK_SPEED = 3.2; // units per second
const AMBIENT_FRAME_MS = 1000 / 24; // work animations alone draw at 24 fps
const FOV = 34;
const POLAR = 0.72; // default tilt, radians from straight down
const TARGET = new THREE.Vector3(0, 0, 2.6);

interface Agent {
  slug: string;
  family: "codex" | "claude";
  figure: Figure;
  pos: Point;
  yaw: number;
  /** Where the agent rests now, or will once the path is walked. */
  spot: Spot;
  attach: Attach;
  place: Placement["place"];
  slot: number;
  /** Waypoints still to walk; the last one is `spot`. */
  path: Attach[];
  action: Action;
  working: boolean;
  stride: number;
  companion: Figure | null;
}

interface Bench {
  model: BenchModel;
  slot: number;
}

interface LabelBox {
  el: HTMLElement;
  anchor: string;
  above: boolean;
  priority: number;
  /** Bench labels are no wider than the distance between benches on screen. */
  fit: boolean;
  width: number;
  height: number;
  /** What was last written to the element; null before the first write. */
  shown: boolean | null;
  maxWidth: number;
  transform: string;
}

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

export function createIslandScene(options: SceneOptions): IslandScene {
  if (!webglAvailable()) throw new NoWebGLError("WebGL is not available");
  let renderer: THREE.WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "low-power" });
  } catch (error) {
    throw new NoWebGLError(error instanceof Error ? error.message : String(error));
  }
  const { host, overlay } = options;
  const canvas = renderer.domElement;
  canvas.className = "map-canvas";
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-label", "Карта острова: перетаскивание сдвигает, правая кнопка или два пальца поворачивают");
  host.prepend(canvas);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  let reduced = motionQuery.matches;
  const kit = new Kit(darkQuery.matches ? DUSK : DAY);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color();
  scene.fog = new THREE.Fog(0xffffff, 70, 190);
  const hemi = new THREE.HemisphereLight();
  const sun = new THREE.DirectionalLight();
  sun.position.set(-9, 20, 11);
  scene.add(hemi, sun);

  const world = new THREE.Group();
  scene.add(world);
  const places: PlaceModels = buildWorld(kit, world);
  const dynamic = new THREE.Group();
  scene.add(dynamic);

  const ring = new THREE.Mesh(
    kit.geo("ring", () => new THREE.RingGeometry(0.52, 0.66, 28).rotateX(-Math.PI / 2)),
    kit.glow("select"),
  );
  ring.position.y = 0.05;
  ring.visible = false;
  scene.add(ring);

  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.5, 400);
  const controls = new MapControls(camera, canvas);
  controls.enableDamping = !reduced;
  controls.dampingFactor = 0.14;
  controls.minAzimuthAngle = -Math.PI / 4;
  controls.maxAzimuthAngle = Math.PI / 4;
  controls.minPolarAngle = 0.38;
  controls.maxPolarAngle = 1.1;
  controls.minDistance = 7;
  controls.maxDistance = 120;
  controls.zoomToCursor = true;
  // The target stays near the middle of the island, so a pan never pushes the island off the screen.
  controls.cursor.copy(TARGET);
  controls.maxTargetRadius = 9;
  controls.target.copy(TARGET);
  controls.listenToKeyEvents(canvas);
  let fitted = true; // the camera still shows the default view

  function applyTheme() {
    const palette = darkQuery.matches ? DUSK : DAY;
    kit.setPalette(palette);
    (scene.background as THREE.Color).set(palette.sky);
    scene.fog!.color.set(palette.sky);
    hemi.color.set(palette.hemiSky);
    hemi.groundColor.set(palette.hemiGround);
    hemi.intensity = palette.hemi;
    sun.color.set(palette.sun);
    sun.intensity = palette.sunIntensity;
    invalidate();
  }

  // Agents and benches ----------------------------------------------------------------------------

  const agents = new Map<string, Agent>();
  const benches = new Map<string, Bench>();
  let entries: readonly SceneEntry[] = [];
  let selected: string | null = null;
  const pickables: THREE.Object3D[] = [];

  const tag = (objects: THREE.Mesh[], slug: string) => {
    for (const object of objects) object.userData.slug = slug;
  };
  function rebuildPickables() {
    pickables.length = 0;
    for (const bench of benches.values()) pickables.push(...bench.model.parts);
    for (const agent of agents.values()) {
      pickables.push(...agent.figure.parts);
      if (agent.companion) pickables.push(...agent.companion.parts);
    }
  }

  function targetOf(entry: SceneEntry): { spot: Spot; attach: Attach; slot: number } {
    const { place } = entry.placement;
    const slot = place === "bench" ? entry.bench : entry.placement.slot;
    return { spot: spotAt(place, slot), attach: attachOf(place, slot), slot };
  }

  function walkTo(agent: Agent, spot: Spot, attach: Attach) {
    const target: Attach = { at: spot, edge: attach.edge };
    if (reduced) {
      agent.path = [];
      agent.pos = { x: spot.x, z: spot.z };
      agent.yaw = spot.face;
      return;
    }
    // Finish the leg in progress unless it leads to the old spot; then turn back at its entrance.
    let start: Attach;
    const prefix: Attach[] = [];
    if (agent.path.length > 1) {
      prefix.push(agent.path[0]!);
      start = agent.path[0]!;
    } else {
      start = agent.attach;
    }
    const steps = [...prefix, ...route(start, attach), target];
    agent.path = steps.filter((step, index) => index === 0 || Math.hypot(step.at.x - steps[index - 1]!.at.x, step.at.z - steps[index - 1]!.at.z) > 1e-6);
  }

  /** What of the entries the scene shows; equal signatures draw the same picture. */
  const signature = (list: readonly SceneEntry[]) =>
    list
      .map(({ task, placement: p, bench }) =>
        [task.slug, task.executor, task.ci, bench, p.place, p.slot, p.action, p.working, p.placeWorking, p.beacon, p.companion].join(" "),
      )
      .join("\n");
  let shown = "";

  function setEntries(next: readonly SceneEntry[]) {
    // The clock re-sends the same entries every second; only a visible change reaches the scene.
    const key = signature(next);
    entries = next;
    if (key === shown) return;
    shown = key;
    const seen = new Set<string>();
    let changed = false;
    for (const entry of next) {
      const { task, placement } = entry;
      seen.add(task.slug);
      let bench = benches.get(task.slug);
      if (!bench) {
        const model = buildBench(kit);
        tag(model.parts, task.slug);
        dynamic.add(model.root);
        bench = { model, slot: -1 };
        benches.set(task.slug, bench);
        changed = true;
      }
      if (bench.slot !== entry.bench) {
        const at = benchAt(entry.bench);
        bench.model.root.position.set(at.x, 0, at.z);
        bench.slot = entry.bench;
      }
      bench.model.beacon.visible = placement.beacon;
      bench.model.patch.material = kit.mat(placement.beacon ? "alarmGround" : "yard");

      const target = targetOf(entry);
      let agent = agents.get(task.slug);
      if (!agent || agent.family !== task.executor) {
        if (agent) dynamic.remove(agent.figure.root);
        const figure = buildFigure(kit, task.executor);
        tag(figure.parts, task.slug);
        dynamic.add(figure.root);
        agent = {
          slug: task.slug,
          family: task.executor,
          figure,
          pos: { x: target.spot.x, z: target.spot.z },
          yaw: target.spot.face,
          spot: target.spot,
          attach: target.attach,
          place: placement.place,
          slot: target.slot,
          path: [],
          action: placement.action,
          working: placement.working,
          stride: 0,
          companion: agent?.companion ?? null,
        };
        agents.set(task.slug, agent);
        changed = true;
      } else if (agent.place !== placement.place || agent.slot !== target.slot) {
        walkTo(agent, target.spot, target.attach);
      }
      // A resting agent joins the paths where its new place does; a walking one keeps its waypoint's.
      if (!agent.path.length) agent.attach = target.attach;
      agent.spot = target.spot;
      agent.place = placement.place;
      agent.slot = target.slot;
      agent.action = placement.action;
      agent.working = placement.working;

      // The reviewer sits across the table while the review runs.
      const companionAt = placement.place === "review" && placement.companion ? companionSpot(target.slot) : null;
      if (companionAt && placement.companion) {
        if (!agent.companion || agent.companion.root.userData.family !== placement.companion) {
          if (agent.companion) dynamic.remove(agent.companion.root);
          agent.companion = buildFigure(kit, placement.companion, true);
          agent.companion.root.userData.family = placement.companion;
          tag(agent.companion.parts, task.slug);
          dynamic.add(agent.companion.root);
          changed = true;
        }
        agent.companion.root.position.set(companionAt.x, 0, companionAt.z);
        agent.companion.root.rotation.y = companionAt.face;
        pose(agent.companion, "sit", 0, false, true);
      } else if (agent.companion) {
        dynamic.remove(agent.companion.root);
        agent.companion = null;
        changed = true;
      }
    }
    for (const [slug, agent] of agents) {
      if (seen.has(slug)) continue;
      dynamic.remove(agent.figure.root);
      if (agent.companion) dynamic.remove(agent.companion.root);
      agents.delete(slug);
      changed = true;
    }
    for (const [slug, bench] of benches) {
      if (seen.has(slug)) continue;
      dynamic.remove(bench.model.root);
      benches.delete(slug);
      changed = true;
    }
    if (changed) rebuildPickables();
    updateFixtures();
    invalidate();
  }

  /** Tents and tables beyond the first ones show up only when someone needs them. */
  function updateFixtures() {
    let sleepers = 0;
    let reviews = 0;
    for (const entry of entries) {
      if (entry.placement.place === "tent") sleepers = Math.max(sleepers, entry.placement.slot + 1);
      if (entry.placement.place === "review") reviews = Math.max(reviews, entry.placement.slot + 1);
    }
    places.tents.forEach((tent, index) => (tent.visible = index < Math.max(2, sleepers)));
    places.tables.forEach((table, index) => (table.visible = index < Math.max(2, reviews)));
  }

  // Poses -----------------------------------------------------------------------------------------

  type Pose = Action | "walk";

  /** Sets the figure for `action`. `t` is seconds; work animations move only when `working`. */
  function pose(figure: Figure, action: Pose, t: number, working: boolean, reviewer = false) {
    const { body, armLeft, armRight, legLeft, legRight } = figure;
    body.position.set(0, 0, 0);
    body.rotation.set(0, 0, 0);
    armLeft.rotation.set(0, 0, 0);
    armRight.rotation.set(0, 0, 0);
    legLeft.rotation.set(0, 0, 0);
    legRight.rotation.set(0, 0, 0);
    figure.tool.visible = action === "tool";
    figure.crate.visible = action === "carry";
    figure.bulb.visible = action === "think";
    figure.bulb.scale.setScalar(1);
    figure.shadow.visible = true;
    const animate = working && !reduced;
    switch (action) {
      case "walk": {
        const swing = Math.sin(t);
        legLeft.rotation.x = 0.55 * swing;
        legRight.rotation.x = -0.55 * swing;
        armLeft.rotation.x = -0.45 * swing;
        armRight.rotation.x = 0.45 * swing;
        body.position.y = 0.035 * Math.abs(Math.cos(t));
        break;
      }
      case "terminal": {
        const tap = animate ? 0.13 * Math.sin(t * 15) : 0;
        armLeft.rotation.x = -1.2 + tap;
        armRight.rotation.x = -1.2 - tap;
        break;
      }
      case "tool":
        armLeft.rotation.x = -0.55;
        armRight.rotation.x = -1.45 + (animate ? 0.5 * Math.sin(t * 7.5) : 0);
        break;
      case "think":
        legLeft.rotation.x = -1.25;
        legRight.rotation.x = -1.25;
        armRight.rotation.x = -2.0;
        armRight.rotation.z = 0.35;
        if (animate) figure.bulb.scale.setScalar(1 + 0.14 * Math.sin(t * 3.2));
        break;
      case "sit":
        legLeft.rotation.x = -1.3;
        legRight.rotation.x = -1.3;
        armLeft.rotation.x = reviewer ? -1.0 : -0.85;
        armRight.rotation.x = -0.85;
        break;
      case "carry":
        armLeft.rotation.x = -0.95;
        armRight.rotation.x = -0.95;
        break;
      case "sleep":
        body.rotation.x = -Math.PI / 2;
        body.position.set(0, 0.22, 0.72);
        figure.shadow.visible = false;
        break;
      case "wait":
        armLeft.rotation.x = 0.18;
        armRight.rotation.x = 0.18;
        break;
      case "stand":
        break;
    }
  }

  // Animation -------------------------------------------------------------------------------------

  let active = false;
  let raf = 0;
  let lastFrame = 0;
  let lastDraw = 0;
  let sceneDirty = true;
  let labelsDirty = true;
  let sizesDirty = true;

  function running() {
    return active && !document.hidden;
  }

  function request() {
    if (!raf && running()) raf = requestAnimationFrame(frame);
  }

  /** Something changed: draw a frame. */
  function invalidate() {
    sceneDirty = true;
    labelsDirty = true;
    request();
  }

  function turnTowards(current: number, goal: number, step: number): number {
    let delta = ((goal - current + Math.PI) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2) - Math.PI;
    if (Math.abs(delta) <= step) return goal;
    delta = Math.sign(delta) * step;
    return current + delta;
  }

  /** Moves the agents; true while one of them walks or turns. */
  function step(dt: number): boolean {
    let moving = false;
    for (const agent of agents.values()) {
      const next = agent.path[0];
      if (next) {
        moving = true;
        const dx = next.at.x - agent.pos.x;
        const dz = next.at.z - agent.pos.z;
        const length = Math.hypot(dx, dz);
        const travel = WALK_SPEED * dt;
        if (length <= travel) {
          agent.pos = { x: next.at.x, z: next.at.z };
          agent.attach = next;
          agent.path.shift();
          if (!agent.path.length) agent.attach = attachOf(agent.place, agent.slot);
        } else {
          agent.pos = { x: agent.pos.x + (dx / length) * travel, z: agent.pos.z + (dz / length) * travel };
        }
        if (length > 1e-4) agent.yaw = turnTowards(agent.yaw, Math.atan2(dx, dz), dt * 10);
        agent.stride += travel * 3.4;
      } else if (agent.yaw !== agent.spot.face) {
        moving = true;
        agent.yaw = reduced ? agent.spot.face : turnTowards(agent.yaw, agent.spot.face, dt * 6);
      }
    }
    return moving;
  }

  function hasAmbient(): boolean {
    if (reduced) return false;
    return entries.some((entry) => entry.placement.working || entry.placement.placeWorking);
  }

  function updateVisuals(t: number) {
    for (const agent of agents.values()) {
      const root = agent.figure.root;
      root.position.set(agent.pos.x, 0, agent.pos.z);
      root.rotation.y = agent.yaw;
      const walking = agent.path.length > 0;
      // A review without a table seat, or a seat not reached yet, is waiting on foot.
      const action: Pose = walking
        ? "walk"
        : agent.action === "sit" && !seated(agent.place, agent.slot)
          ? "wait"
          : agent.action;
      pose(agent.figure, action, walking ? agent.stride : t, agent.working);
      const bench = benches.get(agent.slug);
      if (bench) {
        const home = !walking && agent.place === "bench";
        bench.model.stool.visible = home && agent.action === "think";
        const typing = home && agent.action === "terminal" && agent.working;
        bench.model.led.material = kit.glow(typing ? "screen" : "screenOff");
        bench.model.keys.material = typing ? kit.glow("screen") : kit.mat("metalDark");
      }
    }
    const pick = selected ? agents.get(selected) : undefined;
    ring.visible = !!pick;
    if (pick) ring.position.set(pick.pos.x, 0.05, pick.pos.z);

    // The beacon lamp keeps its size on screen when the camera moves away.
    const scale = THREE.MathUtils.clamp(camera.position.distanceTo(controls.target) / 24, 1, 3.5);
    for (const bench of benches.values()) bench.model.lamp.scale.setScalar(scale);

    // The test stand runs while a check runs; the gate light shows the CI state of who waits there.
    const checking = entries.some((entry) => entry.placement.place === "stand" && entry.placement.placeWorking);
    const lit = reduced ? -1 : Math.floor(t * 5) % places.standLights.length;
    places.standLights.forEach((light, index) => {
      light.material = kit.glow(checking && (lit < 0 || lit === index) ? "standLight" : "screenOff");
    });
    places.standLamp.material = kit.glow(checking ? "standLight" : "lampOff");
    if (checking && !reduced) places.standFan.rotation.y = t * 2.6;

    const atGate = entries.filter((entry) => entry.placement.place === "gate" && entry.placement.placeWorking);
    const pending = atGate.some((entry) => entry.task.ci === "pending" || entry.task.ci === null);
    const failed = atGate.some((entry) => entry.task.ci === "failed" || entry.task.ci === "timeout");
    const gateTone = !atGate.length ? "lampOff" : pending ? "gateLight" : failed ? "beacon" : "screen";
    places.gateLamp.material = kit.glow(gateTone);
    const scan = reduced || !pending ? -1 : Math.floor(t * 4) % places.gateLights.length;
    places.gateLights.forEach((light, index) => {
      light.material = kit.glow(atGate.length && (scan < 0 || scan === index) ? gateTone : "screenOff");
    });
  }

  function frame(time: number) {
    raf = 0;
    if (!running()) return;
    // A slow frame still moves the agents by the time that passed, up to a quarter of a second.
    const dt = lastFrame ? Math.min(0.25, (time - lastFrame) / 1000) : 0;
    lastFrame = time;
    const moved = controls.update(dt);
    const walking = step(dt);
    const ambient = hasAmbient();
    const draw = sceneDirty || moved || walking || (ambient && time - lastDraw >= AMBIENT_FRAME_MS - 2);
    if (draw) {
      updateVisuals(time / 1000);
      renderer.render(scene, camera);
      lastDraw = time;
      sceneDirty = false;
    }
    if (draw || labelsDirty) layoutLabels();
    if (moved || walking || ambient || (controls.enableDamping && controlsBusy)) request();
    else lastFrame = 0;
  }

  // Labels ----------------------------------------------------------------------------------------

  let boxes: LabelBox[] = [];
  let width = 1;
  let height = 1;
  let zoom: ZoomLevel | null = null;
  const projected = new THREE.Vector3();

  function refreshLabels() {
    const previous = new Map(boxes.map((box) => [box.el, box]));
    boxes = [...overlay.querySelectorAll<HTMLElement>("[data-anchor]")].map((el) => {
      const old = previous.get(el);
      return {
        el,
        anchor: el.dataset.anchor ?? "",
        above: el.dataset.align !== "below",
        priority: Number(el.dataset.priority ?? 5),
        fit: el.dataset.fit === "bench",
        width: 0,
        height: 0,
        shown: old?.shown ?? null,
        maxWidth: old?.maxWidth ?? 0,
        transform: old?.transform ?? "",
      };
    });
    sizesDirty = true;
    labelsDirty = true;
    request();
  }

  /** World point of a label anchor, or null when its thing is not on the island. */
  function anchorPoint(anchor: string, out: THREE.Vector3): THREE.Vector3 | null {
    const split = anchor.indexOf(":");
    const kind = split < 0 ? anchor : anchor.slice(0, split);
    const key = split < 0 ? "" : anchor.slice(split + 1);
    switch (kind) {
      case "bench": {
        const bench = benches.get(key);
        if (!bench) return null;
        const at = bench.model.root.position;
        // Over the head of the agent working there, or just over the bench while the agent is away.
        return agents.get(key)?.place === "bench" ? out.set(at.x + 0.2, 2.05, at.z - 0.85) : out.set(at.x, 1.5, at.z + 0.1);
      }
      case "agent": {
        const agent = agents.get(key);
        return agent ? out.set(agent.pos.x, 0, agent.pos.z + 0.45) : null;
      }
      case "tent": {
        const agent = agents.get(key);
        if (!agent) return null;
        const tent = TENTS[agent.slot];
        return tent ? out.set(tent.x, 1.3, tent.z) : out.set(agent.spot.x, 0.6, agent.spot.z);
      }
      case "place":
        switch (key) {
          case "stand":
            return out.set(STAND.x, 2.75, STAND.z);
          case "review":
            return out.set(TABLES[0]!.x, 1.1, TABLES[0]!.z - 0.7);
          case "gate":
            return out.set(GATE.x, 3.7, GATE.z);
          case "tent":
            return out.set(TENTS[0]!.x - 0.2, 0, TENTS[0]!.z - 1.0);
          case "dock":
            return out.set(PIER.x + PIER.width / 2 + 0.9, 0, PIER.z0 + 0.5);
        }
        return null;
      case "more":
        return out.set(YARD.x1 - 0.6, 0, YARD.z1 + 0.1);
    }
    return null;
  }

  function toScreen(point: THREE.Vector3): { x: number; y: number } | null {
    projected.copy(point).project(camera);
    if (projected.z > 1 || projected.z < -1) return null;
    return { x: ((projected.x + 1) / 2) * width, y: ((1 - projected.y) / 2) * height };
  }

  const side = new THREE.Vector3();
  /** As wide as the gap to the nearest bench in the same row allows. */
  function benchWidth(slug: string, point: THREE.Vector3): number {
    const own = benches.get(slug)?.model.root.position;
    let gap = Infinity;
    for (const bench of benches.values()) {
      const at = bench.model.root.position;
      if (own && at !== own && Math.abs(at.z - own.z) < 0.1) gap = Math.min(gap, Math.abs(at.x - own.x));
    }
    if (gap === Infinity) return 200;
    side.copy(point).setX(point.x - gap / 2);
    const a = toScreen(side);
    side.setX(point.x + gap / 2);
    const b = toScreen(side);
    if (!a || !b) return 120;
    return THREE.MathUtils.clamp(Math.hypot(b.x - a.x, b.y - a.y) - 8, 48, 200);
  }

  function updateZoom() {
    const distance = camera.position.distanceTo(controls.target);
    const perUnit = height / (2 * distance * Math.tan(THREE.MathUtils.degToRad(FOV / 2)));
    // The thresholds keep a bench label (1, 3 or 4 lines) clear of the agent of the row behind.
    const level: ZoomLevel = perUnit < 22 ? "far" : perUnit < 44 ? "mid" : "close";
    if (level !== zoom) {
      zoom = level;
      sizesDirty = true;
      options.onZoom(level);
    }
  }

  const point = new THREE.Vector3();
  function layoutLabels() {
    labelsDirty = false;
    updateZoom();
    const inset = options.topInset() + 4;
    const candidates: { box: LabelBox; x: number; y: number }[] = [];
    for (const box of boxes) {
      const anchor = anchorPoint(box.anchor, point);
      const screen = anchor ? toScreen(anchor) : null;
      if (!screen || screen.x < -60 || screen.x > width + 60 || screen.y < -60 || screen.y > height + 60) continue;
      if (box.fit) {
        const max = Math.round(benchWidth(box.anchor.slice("bench:".length), anchor!));
        if (Math.abs(max - box.maxWidth) >= 2) {
          box.maxWidth = max;
          box.el.style.maxWidth = `${max}px`;
          sizesDirty = true;
        }
      }
      candidates.push({ box, x: screen.x, y: screen.y });
    }
    if (sizesDirty) {
      // One layout read for all labels, after all the writes above.
      sizesDirty = false;
      for (const box of boxes) {
        box.width = box.el.offsetWidth;
        box.height = box.el.offsetHeight;
      }
    }
    // Most important first; among equals, the nearer one (lower on screen) wins.
    candidates.sort((a, b) => a.box.priority - b.box.priority || b.y - a.y);
    const placed: { left: number; top: number; right: number; bottom: number }[] = [];
    const overlaps = (r: { left: number; top: number; right: number; bottom: number }) =>
      placed.find((p) => r.left < p.right + 2 && r.right > p.left - 2 && r.top < p.bottom + 1 && r.bottom > p.top - 1);
    const positions = new Map<LabelBox, string>();
    for (const { box, x, y } of candidates) {
      const w = box.width;
      const h = box.height;
      if (!w || !h) continue; // hidden at this zoom
      const left = THREE.MathUtils.clamp(x - w / 2, 4, Math.max(4, width - w - 4));
      const top = THREE.MathUtils.clamp(box.above ? y - h - 3 : y + 3, inset, Math.max(inset, height - h - 4));
      let rect = { left, top, right: left + w, bottom: top + h };
      // One nudge away from a label that is already there; failing that, hide rather than overlap.
      const hit = overlaps(rect);
      if (hit) {
        const moved = box.above ? hit.top - h - 2 : hit.bottom + 2;
        if (Math.abs(moved - top) > h * 1.3 || moved < inset || moved > height - h - 4) continue;
        rect = { left, top: moved, right: left + w, bottom: moved + h };
        if (overlaps(rect)) continue;
      }
      placed.push(rect);
      positions.set(box, `translate3d(${Math.round(rect.left)}px, ${Math.round(rect.top)}px, 0)`);
    }
    for (const box of boxes) {
      const transform = positions.get(box);
      const visible = transform !== undefined;
      if (visible !== box.shown) {
        box.shown = visible;
        box.el.style.visibility = visible ? "visible" : "hidden";
      }
      if (transform !== undefined && transform !== box.transform) {
        box.transform = transform;
        box.el.style.transform = transform;
      }
    }
  }

  // Camera ----------------------------------------------------------------------------------------

  const offset = new THREE.Vector3();
  /** Default tilt: steeper on tall screens, where the island is narrow and its rows need height. */
  let polar = POLAR;
  /** Default target: chosen so that the island sits in the middle below the toolbar. */
  const home = TARGET.clone();
  function placeCamera(distance: number) {
    offset.setFromSphericalCoords(distance, polar, 0);
    camera.position.copy(controls.target).add(offset);
    camera.lookAt(controls.target);
    camera.updateMatrixWorld();
  }

  // What the default view must show: the shared places on both sides, the trees in the north, the pier.
  const CONTENT = [
    new THREE.Vector3(-13.4, 0, 1.5),
    new THREE.Vector3(13.4, 0, 1.5),
    new THREE.Vector3(-12.5, 0, -4.5),
    new THREE.Vector3(12.5, 0, -4.5),
    new THREE.Vector3(-12, 0, 7.5),
    new THREE.Vector3(12, 0, 7.5),
    new THREE.Vector3(0, 2.2, -9.4),
    new THREE.Vector3(0, 0, PIER.z1 + 0.3),
  ];

  /** The nearest distance at which CONTENT fits below the toolbar, looking at (0, 0, z). */
  function fitDistance(z: number): number {
    controls.target.set(0, 0, z);
    const top = 1 - (2 * (options.topInset() + 24)) / height;
    let low = 8;
    let high = 240;
    for (let i = 0; i < 22; i++) {
      const middle = (low + high) / 2;
      placeCamera(middle);
      const fits = CONTENT.every((p) => {
        projected.copy(p).project(camera);
        return Math.abs(projected.x) <= 0.96 && projected.y <= top && projected.y >= -0.97;
      });
      if (fits) high = middle;
      else low = middle;
    }
    return high;
  }

  /** Picks the default target and returns the default distance. */
  function fit(): number {
    polar = width / height < 0.8 ? 0.55 : POLAR;
    const saved = controls.target.clone();
    const position = camera.position.clone();
    let best = { distance: Infinity, z: TARGET.z };
    for (let z = 0; z <= 6.01; z += 0.5) {
      const distance = fitDistance(z);
      if (distance < best.distance - 1e-3) best = { distance, z };
    }
    home.set(0, 0, best.z);
    controls.target.copy(saved);
    camera.position.copy(position);
    camera.lookAt(saved);
    return best.distance;
  }

  function resize() {
    width = Math.max(1, host.clientWidth);
    height = Math.max(1, host.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    const distance = fit();
    controls.maxDistance = Math.max(distance * 1.2, controls.minDistance + 1);
    if (fitted) {
      controls.target.copy(home);
      placeCamera(distance);
    } else {
      camera.lookAt(controls.target);
    }
    controls.update();
    sizesDirty = true;
    invalidate();
  }

  // Input -----------------------------------------------------------------------------------------

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let down: { x: number; y: number; at: number } | null = null;
  let controlsBusy = false;

  function hit(event: PointerEvent): string | null {
    const rect = canvas.getBoundingClientRect();
    pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const found = raycaster.intersectObjects(pickables, false)[0];
    return (found?.object.userData.slug as string | undefined) ?? null;
  }
  const onPointerDown = (event: PointerEvent) => {
    down = { x: event.clientX, y: event.clientY, at: performance.now() };
  };
  const onPointerUp = (event: PointerEvent) => {
    const start = down;
    down = null;
    if (!start || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 6 || performance.now() - start.at > 700) return;
    const slug = hit(event);
    if (slug) options.onPick(slug);
  };
  // Labels sit over the canvas; the wheel over a label still zooms the map.
  const onOverlayWheel = (event: WheelEvent) => {
    if (event.target === canvas) return;
    event.preventDefault();
    canvas.dispatchEvent(new WheelEvent("wheel", event));
  };
  const onPointerMove = (event: PointerEvent) => {
    if (event.buttons || event.pointerType !== "mouse") return;
    canvas.style.cursor = hit(event) ? "pointer" : "";
  };
  const onStart = () => {
    controlsBusy = true;
    fitted = false;
    request();
  };
  const onEnd = () => {
    controlsBusy = false;
    request();
  };
  const onChange = () => {
    sceneDirty = true;
    labelsDirty = true;
    request();
  };
  const onLost = (event: Event) => {
    event.preventDefault();
    setActive(false);
    options.onLost();
  };
  const onVisibility = () => {
    if (running()) invalidate();
    else if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
      lastFrame = 0;
    }
  };
  const onMotion = () => {
    reduced = motionQuery.matches;
    controls.enableDamping = !reduced;
    if (reduced) {
      for (const agent of agents.values()) {
        agent.path = [];
        agent.pos = { x: agent.spot.x, z: agent.spot.z };
        agent.yaw = agent.spot.face;
      }
    }
    invalidate();
  };

  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("webglcontextlost", onLost);
  overlay.addEventListener("wheel", onOverlayWheel, { passive: false });
  controls.addEventListener("start", onStart);
  controls.addEventListener("end", onEnd);
  controls.addEventListener("change", onChange);
  document.addEventListener("visibilitychange", onVisibility);
  darkQuery.addEventListener("change", applyTheme);
  motionQuery.addEventListener("change", onMotion);
  const observer = new ResizeObserver(() => resize());
  observer.observe(host);

  applyTheme();
  resize();

  function setActive(next: boolean) {
    active = next;
    if (running()) {
      sizesDirty = true;
      invalidate();
    } else if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
      lastFrame = 0;
    }
  }

  return {
    setEntries,
    setSelected(slug) {
      selected = slug;
      invalidate();
    },
    setActive,
    refreshLabels,
    dispose() {
      active = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      observer.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("webglcontextlost", onLost);
      overlay.removeEventListener("wheel", onOverlayWheel);
      controls.removeEventListener("start", onStart);
      controls.removeEventListener("end", onEnd);
      controls.removeEventListener("change", onChange);
      document.removeEventListener("visibilitychange", onVisibility);
      darkQuery.removeEventListener("change", applyTheme);
      motionQuery.removeEventListener("change", onMotion);
      controls.stopListenToKeyEvents();
      controls.dispose();
      scene.clear();
      kit.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
      agents.clear();
      benches.clear();
      pickables.length = 0;
      boxes = [];
    },
  };
}
