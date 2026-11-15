import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { RowScroll } from "./rowScroll";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("font-size");
});

function harness() {
  const main = document.createElement("main");
  main.className = "main";
  const strip = document.createElement("div");
  strip.className = "m-strip";
  const row = document.createElement("article");
  main.append(strip, row);
  document.body.replaceChildren(main);
  const rect = (top: number, height: number) => ({ top, bottom: top + height, height } as DOMRect);
  vi.spyOn(main, "getBoundingClientRect").mockReturnValue(rect(100, 600));
  const stripRect = vi.spyOn(strip, "getBoundingClientRect").mockReturnValue(rect(100, 120));
  const rowRect = vi.spyOn(row, "getBoundingClientRect").mockReturnValue(rect(250, 100));
  const scroll = vi.fn();
  row.scrollIntoView = scroll;
  const callbacks: (() => void)[] = [];
  const observe = vi.fn(), disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { callbacks.push(callback); }
    observe = observe;
    disconnect = disconnect;
  });
  document.documentElement.style.fontSize = "20px";
  const inset = () => main.style.getPropertyValue("--m-row-scroll-top");
  return { main, strip, row, rect, stripRect, rowRect, scroll, callbacks, observe, disconnect, inset,
    rows: new RowScroll(main) };
}

describe("row jumps clear the real sticky strip", () => {
  it("reserves its measured height and rem gap in the native scrollport for all descendants", () => {
    const h = harness();
    expect(h.rows.sync()).toBe(132.5);
    expect(h.inset()).toBe("6.625rem");
    // This connects the measured value to native scrollIntoView/focus/hash scrolling.
    const css = readFileSync(join(__dirname, "threads.css"), "utf8");
    expect(css).toMatch(/\.main\s*\{\s*scroll-padding-top:\s*var\(--m-row-scroll-top, 0rem\)/);
    expect(h.observe).toHaveBeenCalledWith(h.strip);
    h.rows.stop();
  });

  it("remeasures immediately before a jump, even before the resize callback runs", () => {
    const h = harness();
    h.rows.sync();
    h.stripRect.mockReturnValue(h.rect(100, 180));
    h.rows.jump(h.row);
    expect(h.inset()).toBe("9.625rem");
    expect(h.scroll).toHaveBeenCalledWith({ block: "start" });
    h.rows.stop();
  });

  it("includes the scrollport's top padding so the entire row clears the strip", () => {
    const h = harness();
    h.main.style.paddingTop = "20px";
    expect(h.rows.sync()).toBe(152.5);
    expect(h.inset()).toBe("7.625rem");
    // Clear of main.top + strip.height, but still inside its padded sticky edge.
    h.rowRect.mockReturnValue(h.rect(240, 100));
    h.rows.returnTo(h.row);
    expect(h.scroll).toHaveBeenCalledWith({ block: "start" });
    h.rows.stop();
  });

  it("follows wrapped strips and text-size changes without moving an ordinary scroll", () => {
    const h = harness();
    h.rows.sync();
    h.main.scrollTop = 321;
    document.documentElement.style.fontSize = "24px";
    h.stripRect.mockReturnValue(h.rect(100, 240));
    h.callbacks[0]();
    expect(h.inset()).toBe("10.625rem");
    expect(h.main.scrollTop).toBe(321);
    expect(h.scroll).not.toHaveBeenCalled();
    h.rows.stop();
  });

  it("rebinds after redraw and removes the inset and observer outside the Inbox or on stop", () => {
    const h = harness();
    h.rows.sync();
    const replacement = h.strip.cloneNode() as HTMLElement;
    vi.spyOn(replacement, "getBoundingClientRect").mockReturnValue(h.rect(100, 60));
    h.strip.replaceWith(replacement);
    h.rows.sync();
    expect(h.disconnect).toHaveBeenCalledTimes(1);
    expect(h.observe).toHaveBeenLastCalledWith(replacement);
    expect(h.inset()).toBe("3.625rem");
    replacement.remove();
    expect(h.rows.sync()).toBe(0);
    expect(h.inset()).toBe("");
    h.rows.stop();
    expect(h.disconnect).toHaveBeenCalledTimes(3);
  });

  it.each([
    [150, 100, "start"], // Inside the scrollport, but under the strip.
    [250, 100, null], // Already visible: keep the exact restored scroll.
    [650, 100, "nearest"], // Fits, but below the viewport.
    [250, 800, "start"], // Taller than the usable area: expose its start.
  ] as const)("returns to a row at %s, height %s, using %s", (top, height, block) => {
    const h = harness();
    h.main.scrollTop = 321;
    h.rowRect.mockReturnValue(h.rect(top, height));
    h.rows.returnTo(h.row);
    if (block) expect(h.scroll).toHaveBeenCalledWith({ block });
    else expect(h.scroll).not.toHaveBeenCalled();
    expect(h.main.scrollTop).toBe(321);
    h.rows.stop();
  });

  it("measures jumps when ResizeObserver is unavailable", () => {
    const h = harness();
    vi.stubGlobal("ResizeObserver", undefined);
    h.rows.jump(h.row);
    expect(h.inset()).toBe("6.625rem");
    expect(h.scroll).toHaveBeenCalledWith({ block: "start" });
    h.rows.stop();
  });

  it("a hidden strip reserves no space and does not move restored scroll", () => {
    const h = harness();
    h.rows.sync();
    h.stripRect.mockReturnValue(h.rect(100, 0));
    h.rowRect.mockReturnValue(h.rect(105, 100));
    h.rows.returnTo(h.row);
    expect(h.inset()).toBe("");
    expect(h.scroll).not.toHaveBeenCalled();
    h.rows.stop();
  });
});
