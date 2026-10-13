// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Annotate } from "./Annotate";
import type { CaptureStill } from "../lib/pageCapture";
vi.mock("../lib/pageCapture", () => ({
  loadCaptureImage: async () => document.createElement("img"),
}));
const still: CaptureStill = { capture_id: "a".repeat(32),
  image: { mime: "image/png", data_b64: "aGk=", width: 800, height: 600 },
  url: "http://localhost:23456/", title: "Synthetic page", text_chars: 4, whole: false, notes: [] };
let host: HTMLDivElement, root: Root;
const stage = vi.fn(async (_ops: readonly { t: string }[]) => false), close = vi.fn(), release = vi.fn();
function show(blocked = false, active = true) {
  return act(async () => { root.render(React.createElement(Annotate, { still, zoom: 100, blocked, active, onClose: close, onStage: stage })); });
}
function button(label: string) {
  return Array.from(host.querySelectorAll("button")).find(b => b.textContent === label)!;
}
async function click(label: string) { await act(async () => button(label).click()); }
function pointer(type: string, x = 20, y = 30) {
  const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperty(event, "pointerId", { value: 7 });
  host.querySelector('[data-testid="annotation-canvas"]')!.dispatchEvent(event);
}
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  const context = Object.fromEntries(["clearRect", "drawImage", "save", "restore", "scale", "beginPath", "arc", "fill",
    "moveTo", "lineTo", "stroke", "closePath", "strokeRect", "strokeText", "fillText"].map(k => [k, vi.fn()]));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { value: vi.fn(), configurable: true });
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { value: () => true, configurable: true });
  Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", { value: release, configurable: true });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  stage.mockReset().mockResolvedValue(false); close.mockReset(); release.mockReset();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("owner annotation event boundary", () => {
  it("commits all four tools, supports undo/clear, and stages marks without sending", async () => {
    await show();
    await act(async () => { pointer("pointerdown"); pointer("pointermove", 70, 90); pointer("pointerup", 100, 110); });
    for (const tool of ["Arrow", "Box"]) {
      await click(tool);
      await act(async () => { pointer("pointerdown"); pointer("pointerup", 100, 110); });
    }
    await click("Text");
    await act(async () => pointer("pointerdown"));
    const input = host.querySelector("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Look here");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click("Add");
    await click("Send to chat");
    expect(stage).toHaveBeenCalledTimes(1);
    expect(stage.mock.calls[0][0].map((op: { t: string }) => op.t)).toEqual(["s", "a", "r", "t"]);
    expect(close).not.toHaveBeenCalled();
    await click("Undo");
    expect(host.querySelector("[data-marks]")?.getAttribute("data-marks")).toBe("3");
    await click("Clear");
    expect(host.querySelector("[data-marks]")?.getAttribute("data-marks")).toBe("0");
  });
  it("card/Settings coverage cancels a held stroke and refuses drawing, staging and local Escape", async () => {
    await show();
    await act(async () => pointer("pointerdown"));
    await show(true);
    expect(release).toHaveBeenCalledWith(7);
    await act(async () => {
      pointer("pointerup", 100, 110); pointer("pointerdown"); pointer("pointerup");
      host.querySelector("section")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    await click("Send to chat");
    expect(stage).not.toHaveBeenCalled(); expect(close).not.toHaveBeenCalled();
    expect(host.querySelector("[data-marks]")?.getAttribute("data-marks")).toBe("0");
    await show(false, false);
    await click("Send to chat"); expect(stage).not.toHaveBeenCalled();
  });
  it("only one export runs in flight; successful staging closes the still", async () => {
    let finish!: (value: boolean) => void;
    stage.mockImplementation(() => new Promise<boolean>(r => { finish = r; }));
    await show(); await click("Send to chat"); await click("Attaching…");
    expect(stage).toHaveBeenCalledTimes(1);
    await act(async () => finish(true)); expect(close).toHaveBeenCalledTimes(1);
  });
});
