import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, DEFAULT_COLOURS } from "../lib/appearance";
import { CUSTOM_PRESETS_KEY, MAX_CUSTOM_PRESETS, loadCustomPresets, type CustomPreset } from "../lib/presets";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root | undefined;
const entry = (id = "c-one", name = "One"): CustomPreset => ({ ...DEFAULT_COLOURS, washes: [...DEFAULT_COLOURS.washes], id, name });
const q = <T extends HTMLElement = HTMLElement>(id: string) => host.querySelector<T>(`[data-testid="appearance-${id}"]`);
const click = (id: string) => act(async () => q<HTMLButtonElement>(id)!.click());
async function type(id: string, value: string) {
  await act(async () => {
    const el = q<HTMLInputElement>(id)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const key = (id: string, key: string) => act(async () => {
  q(id)!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
});
async function external(list: unknown) {
  await act(async () => {
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(list));
    window.dispatchEvent(new StorageEvent("storage", { key: CUSTOM_PRESETS_KEY }));
  });
}
beforeEach(async () => {
  vi.useFakeTimers();
  localStorage.clear();
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify([entry(), entry("c-two", "Two")]));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root!.render(createElement(AuroraSetting)));
});
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  host.remove();
  localStorage.clear();
  vi.useRealTimers();
});

it.each(["preset-aurora", "preset-nebula", "preset-rgb", "reset"])("clears Undo on next preset action: %s", async id => {
  await click("preset-nebula"); // Reset must be enabled too.
  await click("custom-delete-c-one");
  expect(q("custom-undo")).not.toBeNull();
  await click(id);
  expect(q("custom-undo")).toBeNull();
  await act(async () => vi.advanceTimersByTime(0));
  expect(vi.getTimerCount()).toBe(0);
});

it.each(["save", "rename", "apply", "delete"])("clears or replaces the sole Undo on a custom %s", async action => {
  await click("custom-delete-c-one");
  if (action === "save") {
    await click("save-preset");
    await type("preset-name", "Three");
    await click("preset-name-save");
  } else if (action === "rename") {
    await click("custom-rename-c-two");
    await type("custom-name-c-two", "Renamed");
    await key("custom-name-c-two", "Enter");
  } else if (action === "apply") await click("custom-c-two");
  else await click("custom-delete-c-two");
  if (action === "delete") {
    await click("custom-undo");
    expect(loadCustomPresets().map(p => p.id)).toEqual(["c-two"]);
  } else expect(q("custom-undo")).toBeNull();
  await act(async () => vi.advanceTimersByTime(0));
  expect(vi.getTimerCount()).toBe(0);
});

it("expires Undo at exactly 10 seconds and clears its timer on unmount", async () => {
  await click("custom-delete-c-one");
  await act(async () => vi.advanceTimersByTime(9_999));
  expect(q("custom-undo")).not.toBeNull();
  await act(async () => vi.advanceTimersByTime(1));
  expect(q("custom-undo")).toBeNull();
  await click("custom-delete-c-two");
  await act(async () => vi.advanceTimersByTime(0));
  expect(vi.getTimerCount()).toBe(1);
  await act(async () => root!.unmount());
  root = undefined;
  expect(vi.getTimerCount()).toBe(0);
});

it.each(["new entries", "full list", "taken name", "duplicate id"])("Undo merges current storage or refuses safely after another window adds %s", async scenario => {
  await click("custom-delete-c-one");
  const next = scenario === "full list" ? Array.from({ length: MAX_CUSTOM_PRESETS }, (_, i) => entry(`c-x${i}`, `New ${i}`))
    : [entry("c-two", "Two"), entry(scenario === "duplicate id" ? "c-one" : "c-new", scenario === "taken name" ? "ONE" : "Elsewhere")];
  await external(next);
  await click("custom-undo");
  if (scenario === "new entries") expect(loadCustomPresets().map(p => p.name)).toEqual(["One", "Two", "Elsewhere"]);
  else {
    expect(loadCustomPresets()).toEqual(next);
    expect(q("custom-note")!.textContent).not.toBe("");
  }
  expect(q("custom-undo")).toBeNull();
});

it("Undo preserves another window's deletion", async () => {
  await click("custom-delete-c-one");
  await external([]);
  await click("custom-undo");
  expect(loadCustomPresets().map(p => p.id)).toEqual(["c-one"]);
});

it.each(["rename", "delete"])("a stale %s is a note and leaves the other window's data intact", async action => {
  if (action === "rename") {
    await click("custom-rename-c-one");
    await type("custom-name-c-one", "Renamed");
  }
  // Storage events are delivered asynchronously; the old control may still be visible.
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify([entry("c-two", "Two")]));
  if (action === "rename") await key("custom-name-c-one", "Enter");
  else await click("custom-delete-c-one");
  expect(q("custom-note")!.textContent).toMatch(/no longer exists/);
  expect(loadCustomPresets().map(p => p.id)).toEqual(["c-two"]);
  expect(q("custom-c-one")).toBeNull();
});

it("removes a vanished rename input and allows a new rename after a storage event", async () => {
  await click("custom-rename-c-one");
  await type("custom-name-c-one", "Draft");
  await external([entry("c-two", "Two")]);
  expect(q("custom-name-c-one")).toBeNull();
  await click("custom-rename-c-two");
  await type("custom-name-c-two", "New");
  await key("custom-name-c-two", "Enter");
  expect(loadCustomPresets().map(p => p.name)).toEqual(["New"]);
});

it("renders hostile names as cleaned text and gives the swatch only validated colours", async () => {
  const name = '<img src=x onerror="bad()">';
  await external([entry("c-one", name), { ...entry("c-bad", "Bad"), base: "url(https://invalid/)" }]);
  expect(q("custom-c-one")!.textContent).toBe(name);
  expect(host.querySelector("img")).toBeNull();
  expect(q("custom-c-bad")).toBeNull();
  expect(q("custom-c-one")!.querySelectorAll("[style]").length).toBe(5);
});

it("never presses a custom under RGB and disables an already-open save form", async () => {
  await click("save-preset");
  await click("preset-rgb");
  expect(q("custom-c-one")!.getAttribute("aria-pressed")).toBe("false");
  expect(q<HTMLButtonElement>("preset-name-save")!.disabled).toBe(true);
  await key("preset-name", "Enter");
  expect(loadCustomPresets()).toHaveLength(2);
  expect(q("custom-note")!.textContent).toMatch(/RGB/);
});

it.each(["accent", "codeAccent", "base", "wash-0", "wash-1", "wash-2"])("requires all six displayed colours to match: %s", async slot => {
  expect(q("custom-c-one")!.getAttribute("aria-pressed")).toBe("true");
  const appearance: Record<string, unknown> = { ...DEFAULT_COLOURS, washes: [...DEFAULT_COLOURS.washes] };
  if (slot.startsWith("wash-")) (appearance.washes as string[])[Number(slot.slice(5))] = "#334455";
  else appearance[slot] = slot === "base" ? "#05080a" : "#c0aef5";
  await act(async () => {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance));
    window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY }));
  });
  expect(q("custom-c-one")!.getAttribute("aria-pressed")).toBe("false");
});

it.each(["accent", "code-accent", "wash-1", "base"])("custom apply discards a pending %s pick", async slot => {
  await type(slot, slot === "base" ? "#05080a" : slot === "wash-1" ? "#334455" : "#c0aef5");
  await click("custom-c-one");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS * 2));
  const stored = JSON.parse(localStorage.getItem(APPEARANCE_KEY)!);
  expect(stored).toMatchObject(DEFAULT_COLOURS);
  expect(vi.getTimerCount()).toBe(0);
});
