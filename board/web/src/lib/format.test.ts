import { describe, expect, it } from "vitest";
import { formatAge, formatDuration, formatUntil } from "./format";

describe("formatDuration", () => {
  it.each([
    [-5, "0 с"],
    [0, "0 с"],
    [59.9, "59 с"],
    [60, "1 мин 00 с"],
    [754, "12 мин 34 с"],
    [3600, "1 ч 00 мин"],
    [3600 + 5 * 60 + 9, "1 ч 05 мин"],
    [86400 * 2 + 3600 * 3, "2 д 3 ч"],
  ])("%s s → %s", (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text);
  });
});

describe("formatAge", () => {
  it.each([
    [1000, "только что"],
    [999.5, "только что"],
    [988, "12 с назад"],
    [1000 - 5 * 60 - 30, "5 мин назад"],
    [1000 - 7200, "2 ч назад"],
    [1000 - 7800, "2 ч 10 мин назад"],
    [1000 - 3 * 86400, "3 д назад"],
    [1005, "только что"],
  ])("at %s → %s", (at, text) => {
    expect(formatAge(at, 1000)).toBe(text);
  });
});

describe("formatUntil", () => {
  it("counts down and stops at zero", () => {
    expect(formatUntil(1000 + 7800, 1000)).toBe("через 2 ч 10 мин");
    expect(formatUntil(1030, 1000)).toBe("через 30 с");
    expect(formatUntil(990, 1000)).toBe("сейчас");
  });
});
