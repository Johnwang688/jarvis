// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const fixture = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const state = { current: {} as any };
  return { listeners, state, store: {
    start: vi.fn(), subscribe: (fn: () => void) => { listeners.add(fn); return () => listeners.delete(fn); },
    getSnapshot: () => state.current, change: vi.fn(async () => {}), refresh: vi.fn(async () => {}), toggle: vi.fn(async () => {}),
  } };
});
vi.mock("../state/browserSoundClient", () => ({ browserSound: fixture.store }));
import { BrowserSoundSetting, BrowserSpeaker, speakerGesture } from "./BrowserSound";
let host: HTMLDivElement, root: Root;
const open = vi.fn();
function ready(desired = false) { return { status: "ready", busy: false, error: "", value: {
  desired, generation: 1, available: true, reason: null, instances: [{profile:"p-12345678", requested:false, applied:false,generation:1,phase:"ready",reason:null}],
} }; }
async function publish(next: any) { await act(async () => { fixture.state.current = next; fixture.listeners.forEach(fn => fn()); }); }
async function show(kind: "setting" | "speaker", props: Record<string, unknown> = {}) {
  await act(async () => root.render(kind === "setting"
    ? React.createElement(BrowserSoundSetting, { blocked: false, ...props })
    : React.createElement(BrowserSpeaker, { direct: false, blocked: false, tabId:"1234abcd",profile:"p-12345678",profileLabel:"Main", ...props })));
}
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  fixture.state.current = ready(); fixture.store.start.mockClear(); fixture.store.change.mockClear(); fixture.store.refresh.mockClear(); fixture.store.toggle.mockClear(); open.mockClear();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
describe("owner Browser sound controls", () => {
  it("switch uses daemon readback, explains Direct scope and sends one explicit preference gesture", async () => {
    await show("setting"); expect(host.textContent).toContain("Direct pages use this window's audio after a page click.");
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
  it("speaker is a direct profile control and ignores synthetic activation", async () => {
    await publish(ready(true));await show("speaker");const button=host.querySelector("button")!;
    expect(button.hasAttribute("data-browser-sound")).toBe(true);
    expect(button.getAttribute("aria-label")).toContain("Turn on sound for Main");
    expect(button.title).toContain("New tabs inherit it");
    expect(button.title).toContain("changing sites does not mute");
    await act(async()=>button.click());
    expect(fixture.store.toggle).not.toHaveBeenCalled();expect(open).not.toHaveBeenCalled();
    expect(speakerGesture(true,false)).toBe(true);
    expect(speakerGesture(false,false)).toBe(false);expect(speakerGesture(true,true)).toBe(false);
  });
  it("master off hides Remote speaker and Direct explains its independent browser policy", async () => {
    await show("speaker");expect(host.querySelector("button")).toBeNull();
    await show("speaker",{direct:true});
    expect(host.textContent).toContain("Sound after a page click");expect(host.querySelector("button")).toBeNull();
  });
  it("blocked, unknown, pending write and Private speakers remain inert", async () => {
    await publish(ready(true));await show("speaker",{blocked:true});
    expect(host.querySelector("button")!.disabled).toBe(true);
    await show("speaker");await publish({...ready(true),status:"loading"});
    expect(host.querySelector("button")!.disabled).toBe(true);
    await publish({...ready(true),busy:true});expect(host.querySelector("button")!.disabled).toBe(true);
    const state=ready(true);state.value.instances=[{profile:"private",requested:false,applied:false,generation:1,phase:"ready",reason:null}];
    await publish(state);await show("speaker",{profile:"private"});
    expect(host.querySelector("button")!.disabled).toBe(true);
    expect(host.textContent).toContain("Close Private panes to allow sound");
    expect(fixture.store.toggle).not.toHaveBeenCalled();
  });
  it("renders the audible profile as text without autofocus", async () => {
    const state=ready(true);state.value.instances[0].requested=true;state.value.instances[0].applied=true;
    await publish(state);await show("speaker");
    expect(host.textContent).toContain("Sound on for Main");
    expect(host.querySelector("button")!.getAttribute("aria-pressed")).toBe("true");
    expect(document.activeElement).not.toBe(host.querySelector("button"));
  });
});
