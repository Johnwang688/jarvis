import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, afterEach } from "vitest";
import { DevServerPrompts } from "./DevServerPrompt";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function setup() {
  let now = 0, x = 20, frame: FrameRequestCallback = () => {};
  vi.spyOn(performance, "now").mockImplementation(() => now);
  vi.stubGlobal("requestAnimationFrame", (fn: FrameRequestCallback) => { frame = fn; return 1; });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() =>
    ({ x, y: 50, left: x, top: 50, width: 300, height: 150, right: x + 300, bottom: 200, toJSON() {} }));
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host), placed = vi.fn(), dismissed = vi.fn();
  const render = (layoutKey = "stable") => act(() => root.render(createElement(DevServerPrompts, {
    rows: [{ id: "ds_12345678", url: "http://localhost:5173/", source: { kind: "terminal", id: "term1" },
      at: 0, phase: "prompt", token: 1, renewedAt: 0 }],
    hidden: false, layoutKey, zoom: 100, label: () => "Terminal",
    targets: [{ pane: 1, slot: 1, label: "Chat", disabled: false }],
    onOpen: vi.fn(), onPlace: placed, onDismiss: dismissed,
  })));
  render();
  return {
    placed, dismissed, render, advance: (at: number) => { now = at; },
    frame: (at: number) => { now = at; act(() => frame(at)); }, move: () => { x = 90; },
    click: () => act(() => host.querySelector<HTMLButtonElement>("[data-dev-slot]")!.click()),
    key: () => act(() => host.querySelector<HTMLElement>("section")!.dispatchEvent(new KeyboardEvent("keydown", { key: "1", bubbles: true }))),
    dismiss: () => act(() => host.querySelector<HTMLButtonElement>(".devserver-dismiss")!.click()),
    close: () => { act(() => root.unmount()); host.remove(); },
  };
}
describe("prompt action arming", () => {
  it("requires 500ms at a stable position", () => {
    const p = setup();
    try { p.frame(0); p.frame(499); p.click(); expect(p.placed).not.toHaveBeenCalled();
      p.frame(500); p.click(); expect(p.placed).toHaveBeenCalledOnce(); } finally { p.close(); }
  });
  it.each(["click", "key"] as const)("rechecks movement before a %s even before the next paint", kind => {
    const p = setup();
    try {
      p.frame(0); p.frame(600); p.move(); p[kind](); expect(p.placed).not.toHaveBeenCalled();
      // No RAF is delivered: the action itself starts a new full 500ms delay.
      p.advance(1099); p[kind](); expect(p.placed).not.toHaveBeenCalled();
      p.advance(1100); p[kind](); expect(p.placed).toHaveBeenCalledOnce();
    } finally { p.close(); }
  });
  it("also refuses dismissal during a movement's new delay", () => {
    const p = setup();
    try { p.frame(0); p.frame(600); p.move(); p.dismiss(); expect(p.dismissed).not.toHaveBeenCalled();
      p.advance(1100); p.dismiss(); expect(p.dismissed).toHaveBeenCalledOnce(); } finally { p.close(); }
  });
  it("rearms a layout change before another paint", () => {
    const p = setup();
    try { p.frame(0); p.frame(600); p.render("changed"); p.click(); expect(p.placed).not.toHaveBeenCalled();
      p.frame(600); p.advance(1100); p.click(); expect(p.placed).toHaveBeenCalledOnce(); } finally { p.close(); }
  });
});
