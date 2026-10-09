import { describe, expect, it } from "vitest";
import {
  DEFAULT_LAYOUT, LAYOUT_KEY, MAIN_MIN, PANE, RAIL, ZOOM_KEY, clampWidth, dragWidth, fitPanes, isTypingTarget,
  isZoom, keyWidth, loadLayout, loadZoom, maxWidth, parseLayout, parseZoom, saveLayout, saveZoom, shortcutFor,
  stepZoom, toCss,
} from "./layout";

/** A Storage stand-in: a Map, or one that throws on every call. */
const memory = (seed: Record<string, string> = {}) => {
  const m = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    map: m,
  };
};
const throwing = {
  getItem: () => {
    throw new Error("SecurityError: storage is blocked");
  },
  setItem: () => {
    throw new Error("QuotaExceededError");
  },
};

describe("zoom levels", () => {
  it("runs 70% to 160% in steps of ten", () => {
    expect(isZoom(70)).toBe(true);
    expect(isZoom(160)).toBe(true);
    expect(isZoom(100)).toBe(true);
    for (const bad of [60, 170, 105, 99.5, NaN, Infinity, "100", null]) expect(isZoom(bad)).toBe(false);
  });

  it("steps one level and stops at the ends rather than wrapping", () => {
    expect(stepZoom(100, 1)).toBe(110);
    expect(stepZoom(100, -1)).toBe(90);
    expect(stepZoom(160, 1)).toBe(160);
    expect(stepZoom(70, -1)).toBe(70);
    // Ten steps in from 70 is exactly 160, with no float drift on the way.
    let z = 70;
    for (let i = 0; i < 12; i++) z = stepZoom(z, 1);
    expect(z).toBe(160);
  });

  it("treats a non-level as 100% before stepping", () => {
    expect(stepZoom(NaN, 1)).toBe(110);
    expect(stepZoom(133, -1)).toBe(90);
  });

  it("parses only a stored level; any garbage is 100%", () => {
    expect(parseZoom("130")).toBe(130);
    expect(parseZoom(" 70 ")).toBe(70);
    for (const bad of [null, undefined, "", "abc", "1e2", "0x64", "-100", "105", "1000", "50", "120%", "NaN", 130]) {
      expect(parseZoom(bad)).toBe(100);
    }
  });

  it("round-trips through storage", () => {
    const s = memory();
    saveZoom(140, s);
    expect(s.map.get(ZOOM_KEY)).toBe("140");
    expect(loadZoom(s)).toBe(140);
  });

  it("never writes a bad level", () => {
    const s = memory();
    saveZoom(999, s);
    expect(s.map.get(ZOOM_KEY)).toBe("100");
  });

  it("falls back to 100% when storage is empty, garbage or throws", () => {
    expect(loadZoom(memory())).toBe(100);
    expect(loadZoom(memory({ [ZOOM_KEY]: "{not a number" }))).toBe(100);
    expect(loadZoom(throwing)).toBe(100);
    expect(() => saveZoom(120, throwing)).not.toThrow();
  });

  it("converts a screen length into the zoomed space", () => {
    expect(toCss(160, 160)).toBe(100);
    expect(toCss(70, 70)).toBe(100);
    expect(toCss(50, 100)).toBe(50);
    expect(toCss(50, 12345)).toBe(50); // a bad level is 100%, never a divide by garbage
  });
});

describe("pane widths", () => {
  it("clamps into each pane's own range and rounds", () => {
    expect(clampWidth("left", 100)).toBe(PANE.left.min);
    expect(clampWidth("left", 9999)).toBe(PANE.left.max);
    expect(clampWidth("right", 100)).toBe(PANE.right.min);
    expect(clampWidth("right", 9999)).toBe(PANE.right.max);
    expect(clampWidth("left", 300.6)).toBe(301);
  });

  it("gives a non-number the default", () => {
    for (const bad of [NaN, Infinity, -Infinity, "300", null, undefined, {}]) {
      expect(clampWidth("left", bad)).toBe(PANE.left.def);
      expect(clampWidth("right", bad)).toBe(PANE.right.def);
    }
  });

  it("parses each field on its own", () => {
    expect(parseLayout(JSON.stringify({ left: 300, right: 400, leftCollapsed: true, rightCollapsed: false })))
      .toEqual({ left: 300, right: 400, leftCollapsed: true, rightCollapsed: false });
    // A bad width does not reset the other pane.
    expect(parseLayout(JSON.stringify({ left: "wide", right: 400 })))
      .toEqual({ ...DEFAULT_LAYOUT, right: 400 });
    // Out of range is clamped, not refused.
    expect(parseLayout(JSON.stringify({ left: 5, right: 5000 })).left).toBe(PANE.left.min);
    expect(parseLayout(JSON.stringify({ left: 5, right: 5000 })).right).toBe(PANE.right.max);
  });

  it("only a literal true folds a pane", () => {
    for (const v of ["true", 1, "yes", null, {}]) {
      expect(parseLayout(JSON.stringify({ leftCollapsed: v, rightCollapsed: v })).leftCollapsed).toBe(false);
    }
  });

  it("gives garbage the defaults", () => {
    for (const bad of [null, undefined, "", "{", "[]", "[1,2]", "null", "42", '"x"', 7]) {
      expect(parseLayout(bad)).toEqual(DEFAULT_LAYOUT);
    }
  });

  it("round-trips through storage, cleaned", () => {
    const s = memory();
    saveLayout({ left: 9999, right: 300, leftCollapsed: true, rightCollapsed: false }, s);
    expect(JSON.parse(s.map.get(LAYOUT_KEY)!)).toEqual(
      { left: PANE.left.max, right: 300, leftCollapsed: true, rightCollapsed: false });
    expect(loadLayout(s)).toEqual({ left: PANE.left.max, right: 300, leftCollapsed: true, rightCollapsed: false });
  });

  it("falls back to the defaults when storage throws", () => {
    expect(loadLayout(throwing)).toEqual(DEFAULT_LAYOUT);
    expect(() => saveLayout(DEFAULT_LAYOUT, throwing)).not.toThrow();
    // And a fresh object, so a caller cannot mutate the default.
    expect(loadLayout(throwing)).not.toBe(DEFAULT_LAYOUT);
  });
});

describe("fitting the panes to the window", () => {
  const roomy = 1280;
  it("draws what was chosen when it fits", () => {
    expect(fitPanes(DEFAULT_LAYOUT, roomy)).toEqual({ left: 236, right: 316 });
  });

  it("draws a folded pane as its rail", () => {
    expect(fitPanes({ ...DEFAULT_LAYOUT, leftCollapsed: true }, roomy)).toEqual({ left: RAIL, right: 316 });
    expect(fitPanes({ ...DEFAULT_LAYOUT, rightCollapsed: true }, roomy)).toEqual({ left: 236, right: RAIL });
  });

  it("shrinks both panes by their slack when the centre would go under its minimum", () => {
    const layout = { ...DEFAULT_LAYOUT, left: 480, right: 560 };
    const available = 1280 / 1.2; // 120% on a 1280px window
    const { left, right } = fitPanes(layout, available);
    expect(left + right + MAIN_MIN).toBeLessThanOrEqual(available);
    expect(left).toBeGreaterThanOrEqual(PANE.left.min);
    expect(right).toBeGreaterThanOrEqual(PANE.right.min);
    expect(left).toBeLessThan(480);
    expect(right).toBeLessThan(560);
  });

  it("stops at the minimums when even they do not fit", () => {
    expect(fitPanes({ ...DEFAULT_LAYOUT, left: 400, right: 400 }, 500))
      .toEqual({ left: PANE.left.min, right: PANE.right.min });
  });

  it("takes nothing from a folded pane", () => {
    const { left, right } = fitPanes({ ...DEFAULT_LAYOUT, leftCollapsed: true, right: 560 }, 800);
    expect(left).toBe(RAIL);
    expect(right).toBe(800 - MAIN_MIN - RAIL);
  });

  it("does not touch what is stored", () => {
    const layout = { ...DEFAULT_LAYOUT, left: 480 };
    fitPanes(layout, 600);
    expect(layout.left).toBe(480);
  });
});

describe("dragging and keys", () => {
  it("bounds a pane by the centre's minimum and the other pane", () => {
    expect(maxWidth("left", 316, 1280)).toBe(PANE.left.max);
    expect(maxWidth("left", 316, 1000)).toBe(1000 - MAIN_MIN - 316);
    // Never below the pane's own minimum, however little room there is.
    expect(maxWidth("left", 316, 400)).toBe(PANE.left.min);
    expect(maxWidth("right", 236, NaN)).toBe(PANE.right.max);
  });

  it("follows the pointer: right widens the left pane and narrows the right one", () => {
    expect(dragWidth("left", 236, 50, 480)).toBe(286);
    expect(dragWidth("right", 316, 50, 560)).toBe(266);
    expect(dragWidth("right", 316, -50, 560)).toBe(366);
  });

  it("clamps a drag at both ends", () => {
    expect(dragWidth("left", 236, -500, 480)).toBe(PANE.left.min);
    expect(dragWidth("left", 236, 500, 480)).toBe(480);
    expect(dragWidth("left", 236, 500, 300)).toBe(300);
    expect(dragWidth("right", 316, 900, 560)).toBe(PANE.right.min);
  });

  it("moves the edge with the arrows, a bigger step with Shift", () => {
    expect(keyWidth("left", 236, "ArrowRight", false, 480)).toBe(252);
    expect(keyWidth("left", 236, "ArrowLeft", true, 480)).toBe(PANE.left.min);
    expect(keyWidth("left", 236, "ArrowRight", true, 480)).toBe(300);
    // On the right pane the edge is on its left: ArrowLeft widens it.
    expect(keyWidth("right", 316, "ArrowLeft", false, 560)).toBe(332);
    expect(keyWidth("right", 316, "ArrowRight", false, 560)).toBe(300);
  });

  it("goes to the ends with Home and End, and ignores other keys", () => {
    expect(keyWidth("left", 236, "Home", false, 480)).toBe(PANE.left.min);
    expect(keyWidth("left", 236, "End", false, 400)).toBe(400);
    expect(keyWidth("left", 236, "Enter", false, 480)).toBeNull();
    expect(keyWidth("left", 236, " ", false, 480)).toBeNull();
  });
});

describe("shortcuts", () => {
  const k = (key: string, over: Partial<{ ctrlKey: boolean; metaKey: boolean; altKey: boolean; shiftKey: boolean }> = {}) =>
    shortcutFor({ key, ctrlKey: true, metaKey: false, altKey: false, shiftKey: false, ...over });

  it("zooms with Ctrl+= / Ctrl+- / Ctrl+0 in every spelling", () => {
    expect(k("=")).toBe("zoomIn");
    expect(k("+", { shiftKey: true })).toBe("zoomIn");
    expect(k("-")).toBe("zoomOut");
    expect(k("_", { shiftKey: true })).toBe("zoomOut");
    expect(k("0")).toBe("zoomReset");
    expect(k("=", { ctrlKey: false, metaKey: true })).toBe("zoomIn");
  });

  it("folds the left pane with Ctrl+B and the right with Ctrl+Alt+B", () => {
    expect(k("b")).toBe("toggleLeft");
    expect(k("B", { shiftKey: true })).toBe("toggleLeft");
    expect(k("b", { altKey: true })).toBe("toggleRight");
  });

  it("ignores keys without Ctrl, and AltGr's characters", () => {
    expect(shortcutFor({ key: "=", ctrlKey: false, altKey: false })).toBeNull();
    expect(shortcutFor({ key: "b", ctrlKey: false, altKey: false })).toBeNull();
    // AltGr arrives as Ctrl+Alt with the character it types.
    expect(k("ń", { altKey: true })).toBeNull();
    expect(k("=", { altKey: true })).toBeNull();
    expect(k("a")).toBeNull();
  });

  it("knows when the owner is typing", () => {
    const make = (html: string) => {
      const host = document.createElement("div");
      host.innerHTML = html;
      document.body.appendChild(host);
      return host.firstElementChild as HTMLElement;
    };
    expect(isTypingTarget(make("<textarea></textarea>"))).toBe(true);
    expect(isTypingTarget(make('<input type="text">'))).toBe(true);
    expect(isTypingTarget(make("<input>"))).toBe(true);
    expect(isTypingTarget(make('<input type="search">'))).toBe(true);
    expect(isTypingTarget(make("<select></select>"))).toBe(true);
    const monaco = make('<div class="monaco-editor"><div><span id="m">x</span></div></div>');
    expect(isTypingTarget(monaco.querySelector("#m"))).toBe(true);
    expect(isTypingTarget(make('<input type="checkbox">'))).toBe(false);
    expect(isTypingTarget(make("<button>x</button>"))).toBe(false);
    expect(isTypingTarget(document.body)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(window)).toBe(false);
  });
});
