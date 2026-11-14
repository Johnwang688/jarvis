import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, DEFAULT_COLOURS } from "../lib/appearance";
import { CUSTOM_PRESETS_KEY, PRESETS_EVENT, loadCustomPresets } from "../lib/presets";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root;
const q = <T extends HTMLElement = HTMLElement>(id: string) => host.querySelector<T>(`[data-testid="appearance-${id}"]`)!;
const click = (id: string) => act(async () => q<HTMLButtonElement>(id).click());
async function input(id: string, value: string, kind = "input") {
  await act(async () => {
    const el = q<HTMLInputElement>(id);
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(kind, { bubbles: true }));
  });
}
beforeEach(async () => {
  vi.useFakeTimers();
  localStorage.clear();
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify([{...DEFAULT_COLOURS, id:"c-one", name:"One"}]));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(AuroraSetting)));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  localStorage.clear();
  vi.useRealTimers();
});

it.each(["accent", "code-accent", "wash-1", "base", "refused accent", "motion", "drift", "stir", "activity", "rgb-speed", "appearance event", "presets event", "unrelated storage"])("keeps Undo and its original deadline through %s", async action => {
  await click("custom-delete-c-one");
  await act(async () => vi.advanceTimersByTime(4000));
  if (action === "motion") await input("motion", "flow", "change");
  else if (["drift", "rgb-speed"].includes(action)) await input(action, action === "drift" ? "150" : "90");
  else if (["stir", "activity"].includes(action)) await click(action);
  else if (action === "appearance event" || action === "unrelated storage") await act(async () => window.dispatchEvent(new StorageEvent("storage", {key: action === "appearance event" ? APPEARANCE_KEY : "elsewhere"})));
  else if (action === "presets event") await act(async () => window.dispatchEvent(new Event(PRESETS_EVENT)));
  else await input(action === "refused accent" ? "accent" : action, action === "refused accent" ? "#ffb547" : action === "base" ? "#05080a" : action === "wash-1" ? "#334455" : "#c0aef5");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS * 2));
  expect(q("custom-undo")).not.toBeNull();
  await act(async () => vi.advanceTimersByTime(9999 - 4000 - SETTLE_MS * 2));
  expect(q("custom-undo")).not.toBeNull();
  await act(async () => vi.advanceTimersByTime(1));
  expect(q("custom-undo")).toBeNull();
});

it.each(["aurora", "moonlight", "graphite", "deep-blue", "northern-lights", "emerald", "glacier", "nebula", "orchid", "pink", "rgb"])("clears Undo and its timeout immediately when applying %s", async id => {
  await click("custom-delete-c-one");
  await click("preset-" + id);
  expect(q("custom-undo")).toBeNull();
  await act(async () => vi.advanceTimersByTime(0));
  expect(vi.getTimerCount()).toBe(0);
  expect(loadCustomPresets()).toEqual([]);
});

it("Undo after an individual colour edit restores the preset without reverting that edit", async () => {
  await click("custom-delete-c-one");
  await input("accent", "#c0aef5");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS));
  await click("custom-undo");
  expect(loadCustomPresets().map(p => p.id)).toEqual(["c-one"]);
  expect(JSON.parse(localStorage.getItem(APPEARANCE_KEY)!).accent).toBe("#c0aef5");
});
