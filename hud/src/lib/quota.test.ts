import { describe, expect, it } from "vitest";
import { allowanceBar, barsFor, clampPct, level, resetsIn } from "./quota";
import type { ProviderUsage } from "../types";

const T0 = Date.parse("2026-09-15T00:00:00Z");

const provider = (over: Partial<ProviderUsage>): ProviderUsage => ({
  state: "available",
  reason: "",
  today: { work_tokens: 0, spend_usd: 0, equivalent_usd: 0 },
  allowance: null,
  quota: null,
  ...over,
});

describe("quota bar math", () => {
  it("steps colour at 70 and 90, on the boundary", () => {
    expect(level(0)).toBe("ok");
    expect(level(69.9)).toBe("ok");
    expect(level(70)).toBe("warn");
    expect(level(89.9)).toBe("warn");
    expect(level(90)).toBe("high");
    expect(level(100)).toBe("high");
  });

  it("clamps rather than drawing a bar wider than its track", () => {
    expect(clampPct(140)).toBe(100);
    expect(clampPct(-5)).toBe(0);
    expect(clampPct(NaN)).toBe(0);
    // A provider reporting over 100% is over quota, not "ok".
    expect(level(140)).toBe("high");
  });

  it("says how long until the window resets", () => {
    expect(resetsIn("2026-09-15T03:12:00Z", T0)).toBe("resets in 3h 12m");
    expect(resetsIn("2026-09-15T00:12:00Z", T0)).toBe("resets in 12m");
    expect(resetsIn("2026-09-19T04:00:00Z", T0)).toBe("resets in 4d 4h");
    expect(resetsIn("2026-09-15T00:00:20Z", T0)).toBe("resets in under a minute");
  });

  it("a window already past reads as rolled over, not as a negative duration", () => {
    expect(resetsIn("2026-09-14T23:00:00Z", T0)).toBe("resets now");
  });

  it("an unparseable reset time says it was not reported", () => {
    expect(resetsIn("", T0)).toMatch(/not reported/);
    expect(resetsIn("soon", T0)).toMatch(/not reported/);
  });
});

describe("what the panel is allowed to draw", () => {
  it("draws a bar per reported window, in the reported order", () => {
    const bars = barsFor(
      provider({
        quota: {
          windows: [
            { name: "5h", used_percent: 41, resets_at: "2026-09-15T04:00:00Z" },
            { name: "weekly", used_percent: 82, resets_at: "2026-09-19T00:00:00Z" },
          ],
        },
      }),
      T0,
    );
    expect(bars.map((b) => b.name)).toEqual(["5h", "weekly"]);
    expect(bars[0]).toMatchObject({ percent: 41, level: "ok", note: "resets in 4h 0m" });
    expect(bars[1]).toMatchObject({ percent: 82, level: "warn" });
  });

  it("draws nothing at all for a provider that reported no quota", () => {
    // Never computed: the whole point of the contract's `quota: null`.
    expect(barsFor(provider({ quota: null }), T0)).toEqual([]);
    expect(barsFor(provider({ quota: { windows: [] } }), T0)).toEqual([]);
  });

  it("the local allowance is its own fraction, of our ledger and not theirs", () => {
    const bar = allowanceBar(
      provider({
        today: { work_tokens: 412000, spend_usd: 0, equivalent_usd: 9.42 },
        allowance: { work_tokens: 2000000 },
      }),
    );
    expect(bar!.percent).toBeCloseTo(20.6, 6);
    expect(bar).toMatchObject({ level: "ok", name: "local allowance" });
    expect(bar!.note).toContain("412,000");
    expect(bar!.note).toContain("2,000,000");
  });

  it("no allowance means no second bar, rather than a bar of zero", () => {
    expect(allowanceBar(provider({ allowance: null }))).toBeNull();
    expect(allowanceBar(provider({ allowance: { work_tokens: 0 } }))).toBeNull();
  });

  it("an allowance already spent is full and high, not 340% wide", () => {
    const bar = allowanceBar(
      provider({
        today: { work_tokens: 3_400_000, spend_usd: 0, equivalent_usd: 0 },
        allowance: { work_tokens: 1_000_000 },
      }),
    );
    expect(bar).toMatchObject({ percent: 100, level: "high" });
  });
});
