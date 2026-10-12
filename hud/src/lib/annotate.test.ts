import { describe, expect, it } from "vitest";
import {
  EXPORT_EDGE, EXPORT_MAX_BYTES, INK, MAX_OPS, MAX_POINTS, TEXT_MAX, addPoint, arrowHead, clear, clientToImage, commit,
  exportSize, exportType, fitStill, inkWidth, isReal, render, shapeOp, startStroke, textOp, undo,
  type InkContext, type Op,
} from "./annotate";

/** A 2D context that records what was asked of it. */
function recorder() {
  const calls: string[] = [];
  const state = { lineCap: "", lineJoin: "", lineWidth: 0, strokeStyle: "" as unknown, fillStyle: "" as unknown, font: "", textBaseline: "" };
  const log = (name: string) => (...args: unknown[]) => {
    calls.push(`${name}(${args.map((a) => (typeof a === "number" ? Math.round(a * 100) / 100 : a)).join(",")})`);
  };
  const ctx: InkContext = {
    ...state,
    save: log("save"), restore: log("restore"), scale: log("scale"), beginPath: log("beginPath"),
    closePath: log("closePath"), moveTo: log("moveTo"), lineTo: log("lineTo"), arc: log("arc"), stroke: log("stroke"),
    fill: log("fill"), strokeRect: log("strokeRect"), strokeText: log("strokeText"), fillText: log("fillText"),
  };
  return { ctx, calls };
}

const RED = INK[0].value;

describe("fitting the still into its view", () => {
  it("is object-fit: contain, centred", () => {
    expect(fitStill({ width: 800, height: 600 }, { width: 1600, height: 1200 })).toEqual({ x: 0, y: 0, scale: 0.5 });
    expect(fitStill({ width: 1000, height: 600 }, { width: 1600, height: 1200 })).toEqual({ x: 100, y: 0, scale: 0.5 });
    expect(fitStill({ width: 800, height: 800 }, { width: 1600, height: 1200 })).toEqual({ x: 0, y: 100, scale: 0.5 });
  });

  // The plan's zoom rule, as in Remote mode: a screen position is divided by the zoom before it is fitted.
  it("a pointer lands on the still where it was drawn, at every zoom", () => {
    const image = { width: 1600, height: 1200 };
    const fit = fitStill({ width: 1000, height: 600 }, image);     // still at x 100..900, half size
    const box = { left: 40, top: 30 };
    // Still point (400, 300) is view CSS point (300, 150), on screen at (300, 150) × zoom.
    for (const zoom of [70, 100, 160]) {
      const z = zoom / 100;
      expect(clientToImage({ x: 40 + 300 * z, y: 30 + 150 * z }, box, zoom, fit, image)).toEqual([400, 300]);
    }
  });

  it("holds a point off the still to its edge", () => {
    const image = { width: 1600, height: 1200 };
    const fit = fitStill({ width: 1000, height: 600 }, image);
    expect(clientToImage({ x: 0, y: 0 }, { left: 0, top: 0 }, 100, fit, image)).toEqual([0, 0]);
    expect(clientToImage({ x: 5000, y: 5000 }, { left: 0, top: 0 }, 100, fit, image)).toEqual([1600, 1200]);
  });

  it("a width the owner sees is the same on a sharp still and a plain one", () => {
    expect(inkWidth(4, { x: 0, y: 0, scale: 0.5 })).toBe(8);      // a 2x still drawn at half size
    expect(inkWidth(4, { x: 0, y: 0, scale: 1 })).toBe(4);
  });
});

describe("making marks", () => {
  it("a stroke gathers points that moved, and stops at its cap", () => {
    let s = startStroke([1, 1], RED, 4);
    s = addPoint(s, [1.2, 1.2]);
    expect(s.t === "s" && s.pts.length).toBe(1);
    s = addPoint(s, [5, 5]);
    expect(s.t === "s" && s.pts).toEqual([[1, 1], [5, 5]]);
    let long = startStroke([0, 0], RED, 4);
    for (let i = 1; i < MAX_POINTS + 50; i++) long = addPoint(long, [i * 2, 0]);
    expect(long.t === "s" && long.pts.length).toBe(MAX_POINTS);
  });

  it("a box is normalised; Shift makes it a square", () => {
    expect(shapeOp("box", [50, 60], [10, 20], { colour: RED, width: 4 })).toEqual({ t: "r", x: 10, y: 20, w: 40, h: 40, c: RED, lw: 4 });
    expect(shapeOp("box", [0, 0], [30, 10], { colour: RED, width: 4, shift: true })).toMatchObject({ w: 30, h: 30 });
    expect(shapeOp("box", [0, 0], [-30, 10], { colour: RED, width: 4, shift: true })).toMatchObject({ x: -30, y: 0, w: 30, h: 30 });
  });

  it("an arrow snaps to 45° with Shift", () => {
    const a = shapeOp("arrow", [0, 0], [100, 10], { colour: RED, width: 4, shift: true });
    expect(a.t === "a" && [Math.round(a.bx), Math.round(a.by)]).toEqual([100, 0]);
    const d = shapeOp("arrow", [0, 0], [100, 90], { colour: RED, width: 4, shift: true });
    expect(d.t === "a" && Math.round(d.bx) === Math.round(d.by)).toBe(true);
  });

  it("text is one line, capped; nothing typed is no mark", () => {
    expect(textOp([3, 4], "look\nhere\u2028now", RED, 18)).toEqual({ t: "t", x: 3, y: 4, text: "look here now", c: RED, s: 18 });
    expect(textOp([0, 0], "  \n ", RED, 18)).toBeNull();
    const long = textOp([0, 0], "y".repeat(TEXT_MAX + 50), RED, 18);
    expect(long?.t === "t" && long.text.length).toBe(TEXT_MAX);
  });

  it("a slip is not a mark; undo and clear", () => {
    expect(isReal(shapeOp("box", [0, 0], [2, 2], { colour: RED, width: 4 }))).toBe(false);
    expect(isReal(shapeOp("arrow", [0, 0], [2, 2], { colour: RED, width: 4 }))).toBe(false);
    expect(isReal(startStroke([1, 1], RED, 4))).toBe(true);           // a dot
    let ops: Op[] = [];
    ops = commit(ops, shapeOp("box", [0, 0], [2, 2], { colour: RED, width: 4 }));
    ops = commit(ops, null);
    expect(ops).toEqual([]);
    ops = commit(ops, shapeOp("box", [0, 0], [20, 20], { colour: RED, width: 4 }));
    ops = commit(ops, textOp([1, 1], "hi", RED, 18));
    expect(ops.map((o) => o.t)).toEqual(["r", "t"]);
    expect(undo(ops).map((o) => o.t)).toEqual(["r"]);
    expect(undo([])).toEqual([]);
    expect(clear()).toEqual([]);
    let full: Op[] = [];
    for (let i = 0; i < MAX_OPS + 5; i++) full = commit(full, startStroke([i, i], RED, 4));
    expect(full.length).toBe(MAX_OPS);
  });
});

describe("drawing", () => {
  it("an arrow's head points at its end, and the shaft stops at the head", () => {
    const op = shapeOp("arrow", [0, 0], [100, 0], { colour: RED, width: 4 });
    if (op.t !== "a") throw new Error("not an arrow");
    const h = arrowHead(op)!;
    expect(h.tip).toEqual([100, 0]);
    expect(h.left[0]).toBeLessThan(100);
    expect(h.right[0]).toBeLessThan(100);
    expect(Math.sign(h.left[1])).toBe(-Math.sign(h.right[1]));
    expect(h.shaftEnd[0]).toBeGreaterThan(80);
    expect(h.shaftEnd[0]).toBeLessThan(100);
    expect(h.shaftEnd[1]).toBeCloseTo(0, 9);
    expect(arrowHead({ ...op, bx: 0, by: 0 })).toBeNull();
  });

  it("renders every mark at the scale asked, and leaves the context as it was", () => {
    const { ctx, calls } = recorder();
    const ops: Op[] = [
      { t: "s", c: RED, w: 4, pts: [[0, 0], [10, 10]] },
      { t: "s", c: RED, w: 4, pts: [[5, 5]] },
      shapeOp("box", [0, 0], [20, 10], { colour: RED, width: 4 }),
      shapeOp("arrow", [0, 0], [100, 0], { colour: RED, width: 4 }),
      textOp([7, 8], "here", RED, 18)!,
    ];
    render(ctx, ops, 0.5);
    expect(calls[0]).toBe("save()");
    expect(calls[1]).toBe("scale(0.5,0.5)");
    expect(calls[calls.length - 1]).toBe("restore()");
    expect(calls).toContain("moveTo(0,0)");
    expect(calls).toContain("lineTo(10,10)");
    expect(calls).toContain("arc(5,5,2,0,6.28)");
    expect(calls).toContain("strokeRect(0,0,20,10)");
    expect(calls).toContain("closePath()");
    expect(calls).toContain("strokeText(here,7,8)");
    expect(calls).toContain("fillText(here,7,8)");
    expect(ctx.textBaseline).toBe("top");
  });

  it("restores the context even if a mark throws", () => {
    const { ctx, calls } = recorder();
    ctx.strokeRect = () => {
      throw new Error("lost context");
    };
    expect(() => render(ctx, [shapeOp("box", [0, 0], [20, 10], { colour: RED, width: 4 })], 1)).toThrow();
    expect(calls[calls.length - 1]).toBe("restore()");
  });
});

describe("the picture Send to chat stages", () => {
  it("is at most 1920 px on its longest edge, never enlarged", () => {
    expect(exportSize({ width: 3840, height: 2160 })).toEqual({ width: EXPORT_EDGE, height: 1080, scale: 0.5 });
    expect(exportSize({ width: 1200, height: 2400 })).toEqual({ width: 960, height: EXPORT_EDGE, scale: 0.8 });
    expect(exportSize({ width: 800, height: 600 })).toEqual({ width: 800, height: 600, scale: 1 });
  });

  it("is a PNG, or past 3.5 MB a JPEG at quality 0.9", () => {
    expect(exportType(1000)).toEqual({ mime: "image/png", name: "page.png" });
    expect(exportType(EXPORT_MAX_BYTES)).toEqual({ mime: "image/png", name: "page.png" });
    expect(exportType(EXPORT_MAX_BYTES + 1)).toEqual({ mime: "image/jpeg", name: "page.jpg", quality: 0.9 });
    expect(exportType(NaN).mime).toBe("image/jpeg");
  });
});
