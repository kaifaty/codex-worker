import { describe, expect, it } from "vitest";
import type { HistoryPoint, Machine } from "../types";
import {
  chart,
  chartPath,
  cpuLevel,
  cpuLoadLevel,
  cpuLoadText,
  cpuMeter,
  dataAge,
  diskLevel,
  formatBytes,
  freeOf,
  fullestDisk,
  gpuLevel,
  memoryLevel,
  reasonLabel,
  statusText,
  tileMeters,
  vramLevel,
} from "./machines";

const G = 1024 ** 3;
const machine = (extra: Partial<Machine> = {}): Machine => ({
  id: "m", name: "m", role: null, status: "free", reasons: [], checked_at: 1000, sampled_at: 1000, error: null,
  metrics: {
    os: "Linux",
    cpu: { cores: 10, load1: 3.8, load5: 3, percent: null },
    memory: { total_bytes: 31.4 * G, available_bytes: 19.2 * G },
    gpus: [],
    disks: [{ mount: "/", total_bytes: 228 * G, free_bytes: 68 * G }],
  },
  host: null, history: [], ...extra,
});

describe("status and reasons", () => {
  it.each([
    [{ status: "free" }, "свободна"],
    [{ status: "busy", reasons: ["gpu_memory"] }, "занята: видеопамять"],
    [{ status: "overloaded", reasons: ["cpu", "memory", "gpu", "disk:/data"] }, "перегружена: процессор, память, GPU, диск /data"],
    [{ status: "busy", reasons: [] }, "занята"],
    [{ status: "offline", error: "timeout" }, "нет связи: не отвечает"],
    [{ status: "offline", error: "bad_output" }, "нет связи: странный ответ"],
    [{ status: "offline", error: "probe_failed" }, "нет связи: проба упала"],
    [{ status: "offline", error: "unreachable" }, "нет связи"],
    [{ status: "offline", error: "timeout", metrics: null }, "нет связи"],
    [{ status: "unknown", metrics: null }, "нет данных"],
  ] as [Partial<Machine>, string][])("%o → %s", (extra, text) => {
    expect(statusText(machine(extra))).toBe(text);
  });

  it("shows an unknown reason as it is", () => {
    expect(reasonLabel("network")).toBe("network");
  });
});

describe("tile", () => {
  it("picks the fullest disk by share, not by size", () => {
    const disks = [
      { mount: "/", total_bytes: 1000 * G, free_bytes: 300 * G },
      { mount: "/small", total_bytes: 100 * G, free_bytes: 5 * G },
      { mount: "/tie", total_bytes: 200 * G, free_bytes: 10 * G },
    ];
    expect(fullestDisk(disks)?.mount).toBe("/small");
    expect(fullestDisk([])).toBeNull();
  });

  it("shows the busy share as a bar and the free amount as text, without rows for what is missing", () => {
    const meters = tileMeters(machine().metrics);
    expect(meters.map((m) => [m.label, m.value])).toEqual([
      ["CPU", "нагрузка 3,8 / 10"],
      ["Память", "19 ГБ из 31"],
      ["Диск", "68 ГБ из 228"],
    ]);
    expect(meters[0]?.percent).toBeCloseTo(38);
    const hot = tileMeters({ ...machine().metrics!, cpu: { cores: 10, load1: 38, load5: 30, percent: null } });
    expect(hot[0]).toMatchObject({ value: "нагрузка 38 / 10", percent: 100, level: "bad" });
    expect(tileMeters(null)).toEqual([]);
  });

  it("uses the busiest GPU and skips video memory on Apple", () => {
    const gpu = (utilization: number | null, used: number | null) => ({
      name: "g", utilization_percent: utilization, memory_used_bytes: used, memory_total_bytes: used === null ? null : 24 * G, processes: [],
    });
    const apple = tileMeters({ ...machine().metrics!, gpus: [gpu(86, null)] });
    expect(apple.map((m) => m.key)).toEqual(["cpu", "memory", "gpu", "disk:/"]);
    const nvidia = tileMeters({ ...machine().metrics!, gpus: [gpu(10, 20 * G), gpu(40, 2 * G)] });
    expect(nvidia.find((m) => m.key === "gpu")?.value).toBe("40 %");
    expect(nvidia.find((m) => m.key === "vram")?.value).toBe("4 ГБ из 24");
  });
});

describe("CPU", () => {
  it("shows the busy percentage instead of load average when the probe reports it", () => {
    const cpu = { cores: 10, load1: 124, load5: 111, percent: 36.4 };
    expect(cpuMeter(cpu)).toMatchObject({ label: "CPU", value: "36 %", percent: 36.4, level: "ok" });
    expect(cpuLoadText(cpu)).toBe("нагрузка 124 за 1 мин, 111 за 5 мин, ядер 10");
    expect(cpuMeter({ ...cpu, percent: 90 }).level).toBe("bad");
  });

  it("falls back to load average when the percentage is unknown", () => {
    expect(cpuMeter({ cores: 32, load1: 1.2, load5: 1, percent: null })).toMatchObject({
      value: "нагрузка 1,2 / 32",
      level: "ok",
    });
  });
});

describe("levels at the thresholds", () => {
  it.each([
    [cpuLevel, 69.9, "ok"], [cpuLevel, 70, "warn"], [cpuLevel, 89.9, "warn"], [cpuLevel, 90, "bad"],
    [cpuLoadLevel, 69.9, "ok"], [cpuLoadLevel, 70, "warn"], [cpuLoadLevel, 149.9, "warn"], [cpuLoadLevel, 150, "bad"],
    [memoryLevel, 74.9, "ok"], [memoryLevel, 75, "warn"], [memoryLevel, 90, "bad"],
    [gpuLevel, 69.9, "ok"], [gpuLevel, 70, "warn"], [gpuLevel, 100, "warn"],
    [vramLevel, 59.9, "ok"], [vramLevel, 60, "warn"],
    [diskLevel, 84.9, "ok"], [diskLevel, 85, "warn"], [diskLevel, 94.9, "warn"], [diskLevel, 95, "bad"],
  ] as const)("%#: %s → %s", (fn, percent, level) => {
    expect(fn(percent)).toBe(level);
  });
});

describe("formatBytes", () => {
  it.each([
    [0, "0 Б"],
    [1023, "1023 Б"],
    [1024, "1 КБ"],
    [1536, "1,5 КБ"],
    [10 * 1024 - 1, "10 КБ"],
    [8.4 * G, "8,4 ГБ"],
    [198 * G, "198 ГБ"],
    [1023.4 * G, "1023 ГБ"],
    [1023.7 * G, "1 ТБ"],
    [2.1 * 1024 * G, "2,1 ТБ"],
  ])("%s → %s", (bytes, text) => {
    expect(formatBytes(bytes)).toBe(text);
  });

  it("names the total's unit only when it differs", () => {
    expect(freeOf(19.2 * G, 31.4 * G)).toBe("19 ГБ из 31");
    expect(freeOf(500 * G, 2048 * G)).toBe("500 ГБ из 2 ТБ");
  });
});

describe("history chart", () => {
  const point = (at: number, load: number | null): HistoryPoint => ({ at, cpu_percent: load, memory_percent: 50, gpu_percent: null });

  it("breaks the line at a null and at a missing sample instead of drawing zero", () => {
    const data = chart([point(0, 10), point(30, 20), point(60, null), point(90, 30), point(120, 40), point(300, 50)], "cpu_percent");
    expect(data.segments.map((s) => s.map((p) => p.value))).toEqual([[10, 20], [30, 40], [50]]);
    expect(data.current).toBe(50);
    const path = chartPath(data, 300, 60);
    expect(path.match(/M/g)).toHaveLength(3);
    expect(path).not.toContain(",60.0"); // no point at the zero line
  });

  it("is still collecting with fewer than two points, and empty without known values", () => {
    expect(chart([point(0, 10)], "cpu_percent").collecting).toBe(true);
    expect(chart([], "cpu_percent").collecting).toBe(true);
    const gpu = chart([point(0, 10), point(30, 20)], "gpu_percent");
    expect(gpu).toMatchObject({ collecting: false, empty: true, current: null });
  });
});

describe("data age", () => {
  it("labels offline numbers with the age of the last success", () => {
    expect(dataAge(machine({ status: "offline", error: "timeout", sampled_at: 1000 - 640 }), 1000)).toBe("данные 10 мин назад");
    expect(dataAge(machine({ status: "offline", sampled_at: null, metrics: null }), 1000)).toBeNull();
  });

  it("stays quiet for fresh data and speaks up after a failed probe or a long silence", () => {
    expect(dataAge(machine({ sampled_at: 990 }), 1000)).toBeNull();
    expect(dataAge(machine({ sampled_at: 990, error: "timeout" }), 1000)).toBe("данные 10 с назад");
    expect(dataAge(machine({ sampled_at: 1000 - 120 }), 1000)).toBe("данные 2 мин назад");
  });
});
