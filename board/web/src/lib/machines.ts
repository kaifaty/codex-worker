// Labels and figures of the machines strip. Pure functions; the components only lay them out.
import type { Gpu, HistoryPoint, Machine, MachineError, MachineStatus, Metrics } from "../types";
import { formatAge } from "./format";

export type Level = "ok" | "warn" | "bad";

export const STATUS: Record<MachineStatus, string> = {
  free: "свободна",
  busy: "занята",
  overloaded: "перегружена",
  offline: "нет связи",
  unknown: "нет данных",
};

export const ERRORS: Record<MachineError, string> = {
  timeout: "не отвечает",
  unreachable: "нет связи",
  bad_output: "странный ответ",
  probe_failed: "проба упала",
};

const REASONS: Record<string, string> = {
  cpu: "процессор",
  memory: "память",
  gpu: "GPU",
  gpu_memory: "видеопамять",
};

/** "cpu" → "процессор", "disk:/data" → "диск /data"; an unknown reason is shown as it is. */
export function reasonLabel(reason: string): string {
  if (reason.startsWith("disk:")) return `диск ${reason.slice(5)}`;
  return REASONS[reason] ?? reason;
}

export const errorLabel = (error: MachineError | string): string => ERRORS[error as MachineError] ?? error;

/** The status word with its cause: "занята: видеопамять", "нет связи: не отвечает", "свободна". */
export function statusText(machine: Pick<Machine, "status" | "reasons" | "error" | "metrics">): string {
  const word = STATUS[machine.status] ?? machine.status;
  if (machine.status === "busy" || machine.status === "overloaded") {
    return machine.reasons.length ? `${word}: ${machine.reasons.map(reasonLabel).join(", ")}` : word;
  }
  if (machine.status === "offline") {
    // Without any metrics there is nothing to explain: just "нет связи".
    if (!machine.metrics || !machine.error || machine.error === "unreachable") return word;
    return `${word}: ${errorLabel(machine.error)}`;
  }
  return word;
}

/** Colour of the status: green, yellow, red, or none for offline and unknown. */
export function statusLevel(status: MachineStatus): Level | null {
  return status === "free" ? "ok" : status === "busy" ? "warn" : status === "overloaded" ? "bad" : null;
}

// The server's thresholds, in percent of the resource in use.
const level = (percent: number, warn: number, bad = Infinity): Level =>
  percent >= bad ? "bad" : percent >= warn ? "warn" : "ok";
/** CPU busy percentage, the measure the status uses when the probe reports it. */
export const cpuLevel = (percent: number) => level(percent, 70, 90);
/** load1 / cores in percent; only used when the busy percentage is unknown. */
export const cpuLoadLevel = (percent: number) => level(percent, 70, 150);
export const memoryLevel = (percent: number) => level(percent, 75, 90);
export const gpuLevel = (percent: number) => level(percent, 70);
export const vramLevel = (percent: number) => level(percent, 60);
export const diskLevel = (percent: number) => level(percent, 85, 95);

/** "8", "8,4", "198": one decimal below 10, a comma, no trailing ",0". */
export function formatNumber(value: number): string {
  if (Math.abs(value) >= 9.95) return String(Math.round(value));
  const text = value.toFixed(1);
  return (text.endsWith(".0") ? text.slice(0, -2) : text).replace(".", ",");
}

const UNITS = ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"];

function splitBytes(bytes: number): { number: string; unit: string } {
  let value = Math.max(0, bytes);
  let index = 0;
  while (value >= 1024 && index < UNITS.length - 1) {
    value /= 1024;
    index += 1;
  }
  // 1023,7 ГБ would round to "1024 ГБ": show it as "1 ТБ" instead.
  if (Math.round(value) >= 1024 && index < UNITS.length - 1) {
    value /= 1024;
    index += 1;
  }
  return { number: index === 0 ? String(Math.round(value)) : formatNumber(value), unit: UNITS[index]! };
}

/** Binary units in Russian: "0 Б", "8,4 ГБ", "198 ГБ", "2,1 ТБ". */
export function formatBytes(bytes: number): string {
  const { number, unit } = splitBytes(bytes);
  return `${number} ${unit}`;
}

/** What is free out of the total: "19 ГБ из 31", or "500 ГБ из 2 ТБ" when the units differ. */
export function freeOf(free: number, total: number): string {
  const a = splitBytes(free);
  const b = splitBytes(total);
  return a.unit === b.unit ? `${a.number} ${a.unit} из ${b.number}` : `${a.number} ${a.unit} из ${b.number} ${b.unit}`;
}

const usedPercent = (total: number, free: number) => (total > 0 ? ((total - free) / total) * 100 : 0);

export interface Meter {
  key: string;
  label: string;
  /** Share in use, 0–100, for the bar. */
  percent: number;
  level: Level;
  /** What sits next to the bar: mostly what is still free. */
  value: string;
  title: string;
}

const clamp = (percent: number) => Math.min(100, Math.max(0, percent));

/** "нагрузка 124 за 1 мин, 111 за 5 мин, ядер 10". */
export function cpuLoadText(cpu: NonNullable<Metrics["cpu"]>): string {
  return `нагрузка ${formatNumber(cpu.load1)} за 1 мин, ${formatNumber(cpu.load5)} за 5 мин, ядер ${cpu.cores}`;
}

/**
 * The busy percentage when the probe reports it: "36 %". Load average is no measure of use on macOS,
 * so it only stands in when the percentage is unknown: "нагрузка 1,2 / 32".
 */
export function cpuMeter(cpu: NonNullable<Metrics["cpu"]>): Meter {
  const busy = cpu.percent ?? null;
  if (busy !== null) {
    return {
      key: "cpu",
      label: "CPU",
      percent: clamp(busy),
      level: cpuLevel(busy),
      value: `${Math.round(busy)} %`,
      title: `CPU занят на ${Math.round(busy)} %; ${cpuLoadText(cpu)}`,
    };
  }
  const percent = cpu.cores > 0 ? (cpu.load1 / cpu.cores) * 100 : 0;
  return {
    key: "cpu",
    label: "CPU",
    percent: clamp(percent),
    level: cpuLoadLevel(percent),
    value: `нагрузка ${formatNumber(cpu.load1)} / ${cpu.cores}`,
    title: `Занятость CPU неизвестна; нагрузка за минуту ${formatNumber(cpu.load1)} на ${cpu.cores} ядер (${Math.round(percent)} %)`,
  };
}

export function memoryMeter(memory: NonNullable<Metrics["memory"]>): Meter {
  const percent = usedPercent(memory.total_bytes, memory.available_bytes);
  return {
    key: "memory",
    label: "Память",
    percent: clamp(percent),
    level: memoryLevel(percent),
    value: freeOf(memory.available_bytes, memory.total_bytes),
    title: `Память: свободно ${formatBytes(memory.available_bytes)} из ${formatBytes(memory.total_bytes)} (занято ${Math.round(percent)} %)`,
  };
}

export function gpuMeter(gpu: Gpu): Meter | null {
  if (gpu.utilization_percent === null) return null;
  const percent = gpu.utilization_percent;
  return {
    key: "gpu",
    label: "GPU",
    percent: clamp(percent),
    level: gpuLevel(percent),
    value: `${Math.round(percent)} %`,
    title: `${gpu.name}: загрузка ${Math.round(percent)} %`,
  };
}

/** Video memory; null on Apple, where the memory is shared. */
export function vramMeter(gpu: Gpu): Meter | null {
  if (gpu.memory_used_bytes === null || gpu.memory_total_bytes === null || gpu.memory_total_bytes <= 0) return null;
  const free = Math.max(0, gpu.memory_total_bytes - gpu.memory_used_bytes);
  const percent = (gpu.memory_used_bytes / gpu.memory_total_bytes) * 100;
  return {
    key: "vram",
    label: "VRAM",
    percent: clamp(percent),
    level: vramLevel(percent),
    value: freeOf(free, gpu.memory_total_bytes),
    title: `${gpu.name}: видеопамять свободна ${formatBytes(free)} из ${formatBytes(gpu.memory_total_bytes)} (занято ${Math.round(percent)} %)`,
  };
}

export function diskMeter(disk: Metrics["disks"][number]): Meter {
  const percent = usedPercent(disk.total_bytes, disk.free_bytes);
  return {
    key: `disk:${disk.mount}`,
    label: "Диск",
    percent: clamp(percent),
    level: diskLevel(percent),
    value: freeOf(disk.free_bytes, disk.total_bytes),
    title: `Диск ${disk.mount}: свободно ${formatBytes(disk.free_bytes)} из ${formatBytes(disk.total_bytes)} (занято ${Math.round(percent)} %)`,
  };
}

/** The disk with the largest share in use; the first one on a tie. */
export function fullestDisk(disks: Metrics["disks"]): Metrics["disks"][number] | null {
  let best: Metrics["disks"][number] | null = null;
  let bestPercent = -1;
  for (const disk of disks) {
    const percent = usedPercent(disk.total_bytes, disk.free_bytes);
    if (percent > bestPercent) {
      best = disk;
      bestPercent = percent;
    }
  }
  return best;
}

/** The GPU with the highest utilization, or the first one when none reports it. */
export function busiestGpu(gpus: Gpu[]): Gpu | null {
  let best: Gpu | null = null;
  for (const gpu of gpus) {
    if (!best || (gpu.utilization_percent ?? -1) > (best.utilization_percent ?? -1)) best = gpu;
  }
  return best;
}

/** The GPU whose video memory is the most used, among those that report it. */
function fullestVram(gpus: Gpu[]): Gpu | null {
  let best: Gpu | null = null;
  let bestPercent = -1;
  for (const gpu of gpus) {
    const meter = vramMeter(gpu);
    if (meter && meter.percent > bestPercent) {
      best = gpu;
      bestPercent = meter.percent;
    }
  }
  return best;
}

/** The short bars of a tile: CPU, memory, the busiest GPU and its video memory, the fullest disk. */
export function tileMeters(metrics: Metrics | null): Meter[] {
  if (!metrics) return [];
  const meters: (Meter | null)[] = [];
  if (metrics.cpu) meters.push(cpuMeter(metrics.cpu));
  if (metrics.memory) meters.push(memoryMeter(metrics.memory));
  const gpu = busiestGpu(metrics.gpus);
  if (gpu) meters.push(gpuMeter(gpu));
  const vram = fullestVram(metrics.gpus);
  if (vram) meters.push(vramMeter(vram));
  const disk = fullestDisk(metrics.disks);
  if (disk) meters.push(diskMeter(disk));
  return meters.filter((meter): meter is Meter => meter !== null);
}

/** Older than this, the data says how old it is even when nothing failed. */
export const STALE_AFTER = 90;

/**
 * "данные 10 мин назад" when the numbers are not fresh: the machine is offline, the last probe failed,
 * or the last success is older than STALE_AFTER. null when they are fresh or there are none.
 */
export function dataAge(machine: Pick<Machine, "status" | "error" | "sampled_at" | "metrics">, now: number): string | null {
  if (machine.sampled_at === null || !machine.metrics) return null;
  const stale = machine.status === "offline" || machine.error !== null || now - machine.sampled_at > STALE_AFTER;
  return stale ? `данные ${formatAge(machine.sampled_at, now)}` : null;
}

/** "3 свободны из 5", "свободных нет из 4". */
export function freeSummary(machines: Machine[]): string {
  const free = machines.filter((machine) => machine.status === "free").length;
  if (!free) return `свободных нет из ${machines.length}`;
  const word = free % 10 === 1 && free % 100 !== 11 ? "свободна" : "свободны";
  return `${free} ${word} из ${machines.length}`;
}

export type HistoryKey = "cpu_percent" | "memory_percent" | "gpu_percent";

export interface ChartPoint {
  /** 0 = an hour before the last point, 1 = the last point. */
  x: number;
  value: number;
}

export interface Chart {
  /** Runs of consecutive known values; a null or a missing sample starts a new run. */
  segments: ChartPoint[][];
  /** The value of the last point, null when it is unknown. */
  current: number | null;
  /** Fewer than two points: the history is still being collected. */
  collecting: boolean;
  /** No known value at all. */
  empty: boolean;
}

export const CHART_WINDOW = 3600;
/** Points come every 30 s; a longer silence is a gap, not a straight line. */
const CHART_GAP = 90;

export function chart(history: HistoryPoint[], key: HistoryKey): Chart {
  const points = [...history].sort((a, b) => a.at - b.at);
  const last = points[points.length - 1];
  const end = last?.at ?? 0;
  const segments: ChartPoint[][] = [];
  let run: ChartPoint[] = [];
  let previousAt: number | null = null;
  for (const point of points) {
    const value = point[key];
    const gap = previousAt !== null && point.at - previousAt > CHART_GAP;
    if (value === null || gap) {
      if (run.length) segments.push(run);
      run = [];
    }
    if (value !== null && end - point.at <= CHART_WINDOW) {
      run.push({ x: 1 - (end - point.at) / CHART_WINDOW, value });
    }
    previousAt = point.at;
  }
  if (run.length) segments.push(run);
  return {
    segments,
    current: last ? last[key] : null,
    collecting: points.length < 2,
    empty: segments.length === 0,
  };
}

/** SVG path data of the segments in a `width` × `height` box; a lone point becomes a dot-sized dash. */
export function chartPath(data: Chart, width: number, height: number): string {
  // Every history value is a share from 0 to 100.
  const y = (value: number) => height - (Math.min(Math.max(value, 0), 100) / 100) * height;
  return data.segments
    .map((segment) => {
      const parts = segment.map((point, index) => `${index ? "L" : "M"}${(point.x * width).toFixed(1)},${y(point.value).toFixed(1)}`);
      if (segment.length === 1) parts.push(`h0.1`);
      return parts.join("");
    })
    .join("");
}
