import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { followViewport, KEYBOARD_PX, type ViewportWindow } from "./viewport";

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
