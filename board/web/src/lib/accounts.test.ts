import { describe, expect, it } from "vitest";
import { parseAccounts, type AccountRow } from "./accounts";

const data = (extra: Record<string, unknown> = {}) => ({
  codex: [{ name: "alpha", status: "available" }],
  claude: [{ name: "alpha" }, { name: "beta" }],
  ...extra,
});
const rows = (value: unknown): AccountRow[] =>
  (parseAccounts(value, 0).items ?? []).flatMap((item) => (item.kind === "row" ? [item.row] : []));
const duties = (value: unknown) => rows(value).map((row) => (row.duty ? `${row.executor}:${row.name}` : null)).filter(Boolean);

describe("duty account", () => {
  it("marks no account without a duty", () => {
    expect(duties(data())).toEqual([]);
    expect(duties(data({ duty: null }))).toEqual([]);
    expect(duties(data({ duty: { account: null, family: null, reason: "no account available" } }))).toEqual([]);
  });

  it("marks the account of the duty's family, with its reason and start", () => {
    const parsed = rows(data({ duty: { account: "alpha", family: "claude", since: 1_800_000_000, reason: "most room" } }));
    expect(parsed.filter((row) => row.duty).map((row) => `${row.executor}:${row.name}`)).toEqual(["claude:alpha"]);
    expect(parsed.find((row) => row.duty)?.duty).toEqual({ reason: "most room", since: 1_800_000_000 });
  });
});
