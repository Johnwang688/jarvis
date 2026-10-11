// Where the model popover goes (review of PR #29): upward from its button as
// designed, downward only when there is not enough room above and more
// below, never taller than the room it opens into — a fixed 160px floor used
// to push it past the window's top — and every screen rect divided by the zoom
// through lib/layout's `toCss`, like every other menu here.

import { describe, expect, it } from "vitest";
import { placePopover } from "./ModelChip";

const WIN = { w: 1280, h: 800 };
const at = (top: number, left = 300, height = 18) => ({ left, top, bottom: top + height });

describe("placePopover", () => {
  it("opens upward from a button low in the window", () => {
    const p = placePopover(at(700), 100, WIN);
    expect(p.up).toBe(true);
    expect(p.bottom).toBe(800 - 700 + 6);
    expect(p.maxHeight).toBe(440);
    expect(p.left).toBe(300);
  });

  it("opens downward when there is not enough room above and more below", () => {
    const p = placePopover(at(150), 100, WIN);
    expect(p.up).toBe(false);
    expect(p.top).toBe(150 + 18 + 6);
    // Never taller than the room below, never past the bottom edge.
    expect(p.top! + p.maxHeight).toBeLessThanOrEqual(800 - 8);
  });

  it("stays upward when above is short but below is shorter, and never runs off the top", () => {
    const p = placePopover(at(180, 300, 18), 100, { w: 1280, h: 360 });
    expect(p.up).toBe(true);
    // The old floor (160px) would have put its top at 360 - (360-180+6) - 160 < 8.
    const top = 360 - p.bottom! - p.maxHeight;
    expect(top).toBeGreaterThanOrEqual(8);
  });

  it("divides every screen rect by the zoom (toCss)", () => {
    const p = placePopover(at(1120, 480), 160, { w: 2048, h: 1280 });
    expect(p.up).toBe(true);
    expect(p.left).toBeCloseTo(480 / 1.6);
    expect(p.bottom).toBeCloseTo(1280 / 1.6 - 1120 / 1.6 + 6);
  });

  it("keeps inside a narrow window, narrowing itself if it must", () => {
    const p = placePopover(at(700, 250), 100, { w: 280, h: 800 });
    expect(p.width).toBe(280 - 16);
    expect(p.left).toBe(8);
    const q = placePopover(at(700, 1200), 100, WIN);
    expect(q.left + q.width).toBeLessThanOrEqual(1280 - 8);
  });
});
