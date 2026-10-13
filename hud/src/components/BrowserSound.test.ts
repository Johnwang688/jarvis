// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const fixture = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const state = { current: {} as any };
  return { listeners, state, store: {
    start: vi.fn(), subscribe: (fn: () => void) => { listeners.add(fn); return () => listeners.delete(fn); },
    getSnapshot: () => state.current, change: vi.fn(async () => {}), refresh: vi.fn(async () => {}),
  } };
});
vi.mock("../state/browserSoundClient", () => ({ browserSound: fixture.store }));
import { BrowserSoundSetting, BrowserSpeaker } from "./BrowserSound";
let host: HTMLDivElement, root: Root;
const open = vi.fn();
function ready(desired = false) { return { status: "ready", busy: false, error: "", value: {
  desired, generation: 1, available: true, reason: null, instances: [],
} }; }
async function publish(next: any) { await act(async () => { fixture.state.current = next; fixture.listeners.forEach(fn => fn()); }); }
async function show(kind: "setting" | "speaker", props: Record<string, unknown> = {}) {
  await act(async () => root.render(kind === "setting"
    ? React.createElement(BrowserSoundSetting, { blocked: false, ...props })
    : React.createElement(BrowserSpeaker, { direct: false, blocked: false, onSettings: open, ...props })));
}
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  fixture.state.current = ready(); fixture.store.start.mockClear(); fixture.store.change.mockClear(); fixture.store.refresh.mockClear(); open.mockClear();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
describe("owner Browser sound controls", () => {
  it("switch uses daemon readback, explains Direct scope and sends one explicit preference gesture", async () => {
    await show("setting"); expect(host.textContent).toContain("Direct pages use this window's audio.");
    const input = host.querySelector("input")!;
    expect(input.checked).toBe(false);
    await act(async () => input.click()); expect(fixture.store.change).toHaveBeenCalledExactlyOnceWith(true);
    expect(input.checked, "no optimistic success before authoritative state").toBe(false);
    await publish(ready(true)); expect(input.checked).toBe(true);
  });
  it("approval blocking, unknown status and a pending mutation disable the switch", async () => {
    await show("setting", { blocked: true });
    expect(host.querySelector("input")!.disabled).toBe(true);
    await act(async () => host.querySelector("input")!.click()); expect(fixture.store.change).not.toHaveBeenCalled();
    await show("setting"); await publish({ ...ready(), status: "loading" });
    expect(host.querySelector("input")!.disabled).toBe(true);
    await publish({ ...ready(), busy: true }); expect(host.querySelector("input")!.disabled).toBe(true);
  });
  it("read failure offers only a status retry and blocked retry stays inert", async () => {
    await publish({ ...ready(), status: "error", error: "The sound setting could not be read." }); await show("setting");
    expect(host.querySelector('[role="status"]')?.textContent).toContain("could not be read");
    await act(async () => host.querySelector("button")!.click()); expect(fixture.store.refresh).toHaveBeenCalledTimes(1);
    await show("setting", { blocked: true }); await act(async () => host.querySelector("button")!.click());
    expect(fixture.store.refresh).toHaveBeenCalledTimes(1); expect(fixture.store.change).not.toHaveBeenCalled();
  });
  it("speaker opens Settings without toggling sound or granting Remote focus", async () => {
    await show("speaker"); const button = host.querySelector("button")!;
    expect(button.hasAttribute("data-browser-settings")).toBe(true);
    await act(async () => button.click()); expect(open).toHaveBeenCalledTimes(1);
    expect(fixture.store.change).not.toHaveBeenCalled(); expect(fixture.store.refresh).not.toHaveBeenCalled();
    expect(button.getAttribute("aria-label")).toContain("Open Browser settings");
  });
  it("Direct speaker wording never claims to mute its iframe", async () => {
    await publish(ready(true)); await show("speaker", { direct: true });
    expect(host.querySelector("button")?.getAttribute("aria-label")).toContain("Direct audio is unchanged");
  });
  it("blocked speaker is inert and Private pending explanation is visible", async () => {
    const state = ready(true); state.value.instances = [{ profile: "private", applied: false, phase: "pending", reason: "private", generation: 1 }] as any;
    await publish(state); await show("speaker", { blocked: true });
    await act(async () => host.querySelector("button")!.click()); expect(open).not.toHaveBeenCalled();
    expect(host.querySelector("button")?.title).toContain("Close all Private panes");
    expect(host.querySelector("button")?.classList.contains("warn")).toBe(true);
  });
});