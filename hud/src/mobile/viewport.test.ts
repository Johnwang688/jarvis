import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { followViewport, KEYBOARD_PX, ZOOM_MAX, type ViewportWindow } from "./viewport";

describe("the keyboard never covers the message box (owner, 2026-10-28)", () => {
  it("sizes the app to the visible viewport and says when the keyboard is up", () => {
    const fns: Record<string, () => void> = {};
    const vv = { height: 844, offsetTop: 0, addEventListener: (t: "resize" | "scroll", fn: () => void) => { fns[t] = fn; } };
    const win: ViewportWindow = { innerHeight: 844, visualViewport: vv };
    const root = document.createElement("html");
    followViewport(win, root);
    expect([root.style.getPropertyValue("--vv-h"), root.style.getPropertyValue("--vv-top"), root.dataset.keyboard])
      .toEqual(["844px", "0px", "closed"]);
    // The keyboard opens: the visible part shrinks, and iOS may pan it down.
    vv.height = 480;
    vv.offsetTop = 12;
    fns.resize();
    expect([root.style.getPropertyValue("--vv-h"), root.style.getPropertyValue("--vv-top"), root.dataset.keyboard])
      .toEqual(["480px", "12px", "open"]);
    fns.scroll();
    expect(root.style.getPropertyValue("--vv-top")).toBe("12px");
    // A small change (the address bar) is not the keyboard.
    vv.height = 844 - KEYBOARD_PX + 1;
    fns.resize();
    expect(root.dataset.keyboard).toBe("closed");
  });

  it("never reads a pinch-zoom as the keyboard (review of #331, LOW 3)", () => {
    const fns: Record<string, () => void> = {};
    const vv = { height: 844, offsetTop: 0, scale: 1, addEventListener: (t: "resize" | "scroll", fn: () => void) => { fns[t] = fn; } };
    const root = document.createElement("html");
    followViewport({ innerHeight: 844, visualViewport: vv }, root);
    // Zoomed in 2x: the visible part is half as tall, and panned.
    vv.scale = 2;
    vv.height = 422;
    vv.offsetTop = 200;
    fns.resize();
    expect(root.dataset.keyboard).toBe("closed");
    expect(root.style.getPropertyValue("--vv-h")).toBe("");
    expect(root.style.getPropertyValue("--vv-top")).toBe("");
    // At the edge of ZOOM_MAX it still counts as unzoomed; back at 1 the keyboard reads again.
    vv.scale = ZOOM_MAX;
    vv.height = 480;
    vv.offsetTop = 0;
    fns.resize();
    expect([root.dataset.keyboard, root.style.getPropertyValue("--vv-h")]).toEqual(["open", "480px"]);
  });

  it("reads it again on a window resize or a rotation, so 'open' cannot stick (review of #331, LOW 4)", () => {
    const vfns: Record<string, () => void> = {};
    const wfns: Record<string, () => void> = {};
    const vv = { height: 480, offsetTop: 0, addEventListener: (t: "resize" | "scroll", fn: () => void) => { vfns[t] = fn; } };
    const win: ViewportWindow = { innerHeight: 844, visualViewport: vv,
      addEventListener: (t, fn) => { wfns[t] = fn; } };
    const root = document.createElement("html");
    followViewport(win, root);
    expect(root.dataset.keyboard).toBe("open");
    expect(Object.keys(wfns).sort()).toEqual(["orientationchange", "resize"]);
    // Rotated to landscape: the window is now as short as the visible part; no viewport event came.
    win.innerHeight = 390;
    vv.height = 390;
    wfns.orientationchange();
    expect([root.dataset.keyboard, root.style.getPropertyValue("--vv-h")]).toEqual(["closed", "390px"]);
    // And back to portrait, read on the window's resize.
    win.innerHeight = 844;
    vv.height = 844;
    wfns.resize();
    expect([root.dataset.keyboard, root.style.getPropertyValue("--vv-h")]).toEqual(["closed", "844px"]);
  });

  it("sets nothing where there is no visualViewport (the plain layout)", () => {
    const root = document.createElement("html");
    followViewport({ innerHeight: 844, visualViewport: null }, root);
    expect(root.getAttribute("style")).toBeNull();
    expect(root.dataset.keyboard).toBeUndefined();
  });

  it("mobile.css reads it: the app follows the viewport, the tab bar steps aside for the keyboard", () => {
    const css = readFileSync(join(__dirname, "mobile.css"), "utf8");
    const app = css.slice(css.indexOf(".app {"), css.indexOf("}", css.indexOf(".app {")));
    expect(app).toContain("top: var(--vv-top, 0px)");
    expect(app).toContain("height: var(--vv-h, 100%)");
    expect(css).toContain(':root[data-keyboard="open"] .tabs { display: none; }');
    // The tab bar keeps the iPhone's bottom safe area; the bar above it needs none of its own.
    expect(css).toMatch(/\.tabs \{[^}]*padding-bottom: env\(safe-area-inset-bottom\)/);
  });
});
