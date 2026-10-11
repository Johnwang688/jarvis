import { describe, expect, it } from "vitest";
import { effectiveZoom, keepOnScreen } from "./zoomfix";

describe("Monaco's popups under the HUD's CSS zoom", () => {
  it("a box already on screen does not move", () => {
    expect(keepOnScreen({ left: 100, top: 100, right: 400, bottom: 300 }, 1600, 900, 1.6)).toEqual({ dx: 0, dy: 0 });
  });

  it("a box past the right edge comes back by the overflow, in its own (zoomed) pixels", () => {
    // 1576..2083 in a 1600-wide window, at 160%: 483 viewport px back, 301.875 CSS px.
    const d = keepOnScreen({ left: 1576, top: 264, right: 2083, bottom: 503 }, 1600, 900, 1.6);
    expect(d.dx).toBeCloseTo(-483 / 1.6);
    expect(d.dy).toBe(0);
  });

  it("a box past the bottom comes up; one taller than the window keeps its top on screen", () => {
    expect(keepOnScreen({ left: 0, top: 800, right: 100, bottom: 1000 }, 1600, 900, 2).dy).toBe(-50);
    expect(keepOnScreen({ left: 0, top: 50, right: 100, bottom: 1200 }, 1600, 900, 1).dy).toBe(-50);
  });

  it("a box wider than the window keeps its start on screen", () => {
    expect(keepOnScreen({ left: 300, top: 0, right: 2300, bottom: 10 }, 1600, 900, 1).dx).toBe(-300);
  });

  it("a nonsense zoom is read as 1", () => {
    expect(keepOnScreen({ left: 200, top: 0, right: 1700, bottom: 10 }, 1600, 900, 0).dx).toBe(-100);
  });

  it("the zoom is the product of every zoom up the tree, across a shadow root's host", () => {
    const root = document.createElement("div");
    root.style.zoom = "1.6";
    const inner = document.createElement("div");
    inner.style.zoom = "0.5";
    const host = document.createElement("div");
    root.appendChild(inner);
    inner.appendChild(host);
    document.body.appendChild(root);
    const shadow = host.attachShadow({ mode: "open" });
    const view = document.createElement("div");
    shadow.appendChild(view);
    // jsdom computes no `zoom`, so the walk reads the inline zooms: every one
    // on the way up, through the shadow root to its host and beyond.
    const inline = (e: Element) => (e as HTMLElement).style.zoom;
    expect(effectiveZoom(view, inline)).toBeCloseTo(0.8);
    view.style.zoom = "2";
    expect(effectiveZoom(view, inline)).toBeCloseTo(1.6);
    expect(effectiveZoom(null, inline)).toBe(1);
    root.remove();
  });
});
