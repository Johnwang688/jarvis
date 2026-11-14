// Acceptance tests for custom presets in Settings -> Appearance (owner 2026-11-13). Written by the
// orchestrator before the build; the implementer may not edit this file.
//
// Contract:
//  - `appearance-save-preset` opens an inline form: text input `appearance-preset-name` (prefilled
//    "Custom N", N = custom count + 1), `appearance-preset-name-save`, `appearance-preset-name-cancel`.
//    Enter saves, Escape cancels. Save stores the current colours + codeAccent through addCustomPreset; a
//    refusal shows in `appearance-custom-note` and keeps the form open. Disabled while RGB is on.
//  - Each custom preset is a button `appearance-custom-<id>` in the preset row AFTER the RGB button, with a
//    swatch and its name, `aria-pressed` when the current colours AND codeAccent match it (never under RGB).
//    Clicking applies its six colours, sets rgb false, drops a pending picker pick.
//  - Next to each: `appearance-custom-rename-<id>` (aria-label "Rename <name>") turns the name into an
//    input `appearance-custom-name-<id>`; Enter or blur commits through renameCustomPreset, Escape cancels;
//    a refusal shows in `appearance-custom-note` and keeps the input. `appearance-custom-delete-<id>`
//    (aria-label "Delete <name>") deletes and shows `appearance-custom-undo` ("Undo") until the next preset
//    action or 10 s; Undo restores it at the same place.
//  - Built-in presets have no rename or delete controls. Reset colours does not touch custom presets.
//  - Another window's change (storage event on the presets key) refreshes the row.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, AMBER, COLOUR_PRESETS } from "../lib/appearance";
import { CUSTOM_PRESETS_KEY, loadCustomPresets } from "../lib/presets";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root;
async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(AuroraSetting)));
}
beforeEach(async () => {
  vi.useFakeTimers();
  localStorage.clear();
  await mount();
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  localStorage.clear();
  vi.useRealTimers();
});

const q = <T extends Element = HTMLElement>(id: string) => host.querySelector<T>(`[data-testid="${id}"]`);
const click = async (id: string) => act(async () => q<HTMLButtonElement>(id)!.click());
const storedAppearance = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}") as Record<string, unknown>;
const customs = () => loadCustomPresets();
const customButtons = () => [...host.querySelectorAll<HTMLButtonElement>('[data-testid^="appearance-custom-c-"]')];
async function type(el: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function key(el: HTMLElement, k: string) {
  await act(async () => { el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
}
async function saveAs(name: string) {
  await click("appearance-save-preset");
  await type(q<HTMLInputElement>("appearance-preset-name")!, name);
  await click("appearance-preset-name-save");
}
async function pickCode(value: string) {
  const el = q<HTMLInputElement>("appearance-code-accent")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 5));
}

it("saving names the current colours and editor colour; the new preset joins the row after RGB, pressed", async () => {
  await click("appearance-preset-nebula");
  await pickCode("#c0aef5");
  await click("appearance-save-preset");
  const input = q<HTMLInputElement>("appearance-preset-name")!;
  expect(input.value).toBe("Custom 1");
  await type(input, "Night drive");
  await click("appearance-preset-name-save");
  const [p] = customs();
  const n = COLOUR_PRESETS.find((x) => x.id === "nebula")!;
  expect(p).toMatchObject({ name: "Night drive", accent: n.accent, washes: [...n.washes], base: n.base, codeAccent: "#c0aef5" });
  expect(q("appearance-preset-name")).toBeNull();
  const row = [...host.querySelectorAll<HTMLElement>('[data-testid="appearance-presets"] button')].map((b) => b.dataset.testid);
  expect(row.indexOf(`appearance-custom-${p!.id}`)).toBeGreaterThan(row.indexOf("appearance-preset-rgb"));
  expect(q(`appearance-custom-${p!.id}`)!.textContent).toContain("Night drive");
  expect(q(`appearance-custom-${p!.id}`)!.getAttribute("aria-pressed")).toBe("true");
});

it("Enter saves and Escape cancels the name form", async () => {
  await click("appearance-save-preset");
  await type(q<HTMLInputElement>("appearance-preset-name")!, "Keyed");
  await key(q("appearance-preset-name")!, "Enter");
  expect(customs().map((p) => p.name)).toEqual(["Keyed"]);
  await click("appearance-save-preset");
  expect(q<HTMLInputElement>("appearance-preset-name")!.value).toBe("Custom 2");
  await key(q("appearance-preset-name")!, "Escape");
  expect(q("appearance-preset-name")).toBeNull();
  expect(customs()).toHaveLength(1);
});

it("a taken name is refused in the note and the form stays open", async () => {
  await saveAs("Aurora");
  expect(customs()).toHaveLength(0);
  expect(q("appearance-custom-note")!.textContent ?? "").not.toBe("");
  expect(q("appearance-preset-name")).not.toBeNull();
});

it("Save is disabled while RGB is on", async () => {
  await click("appearance-preset-rgb");
  expect(q<HTMLButtonElement>("appearance-save-preset")!.disabled).toBe(true);
});

it("clicking a custom preset applies all six colours, turns RGB off and is pressed", async () => {
  await click("appearance-preset-orchid");
  await pickCode("#c0aef5");
  await saveAs("Plum");
  const id = customs()[0]!.id;
  await click("appearance-preset-graphite");
  await click("appearance-preset-rgb");
  await click(`appearance-custom-${id}`);
  const o = COLOUR_PRESETS.find((x) => x.id === "orchid")!;
  expect(storedAppearance()).toMatchObject({ rgb: false, accent: o.accent, washes: [...o.washes], base: o.base, codeAccent: "#c0aef5" });
  expect(q(`appearance-custom-${id}`)!.getAttribute("aria-pressed")).toBe("true");
});

it("a custom preset is not pressed when only the five colours match but the editor colour differs", async () => {
  await click("appearance-preset-orchid");
  await pickCode("#c0aef5");
  await saveAs("Plum");
  const id = customs()[0]!.id;
  await pickCode("#a5f3fc");
  expect(q(`appearance-custom-${id}`)!.getAttribute("aria-pressed")).toBe("false");
});

it("a custom preset click drops a pending picker pick", async () => {
  await saveAs("Base");
  const id = customs()[0]!.id;
  const el = q<HTMLInputElement>("appearance-accent")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, "#a0c4ff");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click(`appearance-custom-${id}`);
  await act(async () => vi.advanceTimersByTime(SETTLE_MS * 4));
  expect(storedAppearance().accent ?? "#6fc3df").toBe(customs()[0]!.accent);
  expect(vi.getTimerCount()).toBe(0);
});

it("rename: pencil opens an input, Enter commits, Escape cancels, a taken name is refused", async () => {
  await saveAs("First");
  await saveAs("Second");
  const [a, b] = customs();
  expect(q(`appearance-custom-rename-${a!.id}`)!.getAttribute("aria-label")).toBe("Rename First");
  await click(`appearance-custom-rename-${a!.id}`);
  const input = q<HTMLInputElement>(`appearance-custom-name-${a!.id}`)!;
  expect(input.value).toBe("First");
  await type(input, "Dusk");
  await key(input, "Enter");
  expect(customs()[0]!.name).toBe("Dusk");
  expect(q(`appearance-custom-name-${a!.id}`)).toBeNull();
  await click(`appearance-custom-rename-${b!.id}`);
  await type(q<HTMLInputElement>(`appearance-custom-name-${b!.id}`)!, "Nope");
  await key(q(`appearance-custom-name-${b!.id}`)!, "Escape");
  expect(customs()[1]!.name).toBe("Second");
  await click(`appearance-custom-rename-${b!.id}`);
  await type(q<HTMLInputElement>(`appearance-custom-name-${b!.id}`)!, "dusk");
  await key(q(`appearance-custom-name-${b!.id}`)!, "Enter");
  expect(customs()[1]!.name).toBe("Second");
  expect(q("appearance-custom-note")!.textContent ?? "").not.toBe("");
  expect(q(`appearance-custom-name-${b!.id}`)).not.toBeNull();
});

it("rename commits on blur", async () => {
  await saveAs("First");
  const id = customs()[0]!.id;
  await click(`appearance-custom-rename-${id}`);
  const input = q<HTMLInputElement>(`appearance-custom-name-${id}`)!;
  await type(input, "Blurred");
  await act(async () => { input.dispatchEvent(new FocusEvent("blur")); input.dispatchEvent(new FocusEvent("focusout", { bubbles: true })); });
  expect(customs()[0]!.name).toBe("Blurred");
});

it("delete removes it and offers Undo, which restores it in place; Undo goes away after 10 s", async () => {
  await saveAs("One");
  await saveAs("Two");
  await saveAs("Three");
  const ids = customs().map((p) => p.id);
  expect(q(`appearance-custom-delete-${ids[1]}`)!.getAttribute("aria-label")).toBe("Delete Two");
  await click(`appearance-custom-delete-${ids[1]}`);
  expect(customs().map((p) => p.name)).toEqual(["One", "Three"]);
  expect(q(`appearance-custom-${ids[1]}`)).toBeNull();
  await click("appearance-custom-undo");
  expect(customs().map((p) => p.name)).toEqual(["One", "Two", "Three"]);
  expect(q("appearance-custom-undo")).toBeNull();
  await click(`appearance-custom-delete-${ids[0]}`);
  expect(q("appearance-custom-undo")).not.toBeNull();
  await act(async () => vi.advanceTimersByTime(10_500));
  expect(q("appearance-custom-undo")).toBeNull();
  expect(customs().map((p) => p.name)).toEqual(["Two", "Three"]);
});

it("built-ins have no rename or delete; Reset leaves custom presets alone", async () => {
  await saveAs("Keep me");
  expect(host.querySelectorAll('[data-testid^="appearance-custom-rename-"]').length).toBe(1);
  expect(host.querySelectorAll('[data-testid^="appearance-custom-delete-"]').length).toBe(1);
  for (const p of COLOUR_PRESETS) {
    expect(q(`appearance-custom-rename-${p.id}`)).toBeNull();
    expect(q(`appearance-custom-delete-${p.id}`)).toBeNull();
  }
  await click("appearance-preset-nebula");
  await click("appearance-reset");
  expect(customs().map((p) => p.name)).toEqual(["Keep me"]);
});

it("custom presets survive a remount, and another window's change refreshes the row", async () => {
  await saveAs("Persisted");
  await act(async () => root.unmount());
  host.remove();
  await mount();
  expect(customButtons().map((b) => b.textContent)).toEqual([expect.stringContaining("Persisted")]);
  const list = JSON.parse(localStorage.getItem(CUSTOM_PRESETS_KEY)!);
  list.push({ ...list[0], id: "c-other", name: "From elsewhere" });
  await act(async () => {
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(list));
    window.dispatchEvent(new StorageEvent("storage", { key: CUSTOM_PRESETS_KEY }));
  });
  expect(customButtons()).toHaveLength(2);
});

it("a hostile stored entry never shows up", async () => {
  await act(async () => {
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify([
      { id: "c-bad", name: "Hot", accent: AMBER, washes: ["#2a8fa0", "#3468c4", "#6450c4"], base: "#0a1116", codeAccent: "#6fc3df" },
    ]));
    window.dispatchEvent(new StorageEvent("storage", { key: CUSTOM_PRESETS_KEY }));
  });
  expect(customButtons()).toHaveLength(0);
});
