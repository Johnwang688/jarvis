// Independent PR #357 probes. Unsafe catalog entries below are fault injection, not shipped presets.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import {
  AMBER, APPEARANCE_KEY, COLOUR_PRESETS, DEFAULT_APPEARANCE, DEFAULT_COLOURS,
  checkAccent, checkBase, checkPalette, checkPreset, checkWash, loadAppearance, resolveColours,
  type ColourPreset,
} from "../lib/appearance";
import { applyAppearance, installAppearance } from "../lib/appearanceApply";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root, uninstall: () => void;
beforeEach(async () => {
  vi.useFakeTimers();
  localStorage.clear();
  uninstall = installAppearance();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(AuroraSetting)));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  uninstall();
  localStorage.clear();
  applyAppearance(DEFAULT_APPEARANCE);
  vi.useRealTimers();
});

const button = (id: string) => host.querySelector<HTMLButtonElement>(`[data-testid="appearance-preset-${id}"]`)!;
const input = (slot: string) => host.querySelector<HTMLInputElement>(`[data-testid="appearance-${slot}"]`)!;
const pressed = () => [...host.querySelectorAll<HTMLButtonElement>('[aria-pressed="true"]')].map((b) => b.textContent);
const reset = () => host.querySelector<HTMLButtonElement>('[data-testid="appearance-reset"]')!;
const saved = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}");
async function drag(slot: string, value: string) {
  await act(async () => {
    const el = input(slot);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function commit(slot: string) {
  await act(async () => input(slot).dispatchEvent(new Event("change", { bubbles: true })));
}
async function external(record: object) {
  await act(async () => {
    const oldValue = localStorage.getItem(APPEARANCE_KEY);
    const newValue = JSON.stringify(record);
    localStorage.setItem(APPEARANCE_KEY, newValue);
    window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY, oldValue, newValue, storageArea: localStorage }));
  });
}

it("a native commit after a preset cannot flush the discarded drag", async () => {
  await drag("accent", "#a0c4ff");
  await act(async () => button("graphite").click());
  await commit("accent");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS * 4));
  expect(saved().accent).toBe("#e5e7eb");
  expect(pressed()).toEqual(["Graphite"]);
  expect(vi.getTimerCount()).toBe(0);
});

it("every shipped preset survives resolving exactly and applies all five colours", async () => {
  for (const p of COLOUR_PRESETS) {
    await act(async () => button(p.id).click());
    const expected = { accent: p.accent, washes: [...p.washes], base: p.base };
    expect(resolveColours(loadAppearance()), p.id).toEqual(expected);
    expect(pressed(), p.id).toEqual([p.name]);
    expect(reset().disabled, p.id).toBe(p.id === "aurora");
    for (const [slot, value] of Object.entries({ accent: p.accent, "wash-1": p.washes[0], "wash-2": p.washes[1], "wash-3": p.washes[2], base: p.base })) {
      expect(input(slot).value, `${p.id} ${slot}`).toBe(value);
      expect(document.documentElement.style.getPropertyValue(`--${slot}`), `${p.id} --${slot}`).toBe(p.id === "aurora" ? "" : value);
    }
  }
});

it("a preset clears refusal and darkening notes and each picker can fine tune it", async () => {
  await drag("accent", AMBER);
  await commit("accent");
  await drag("wash-1", "#ffffff");
  await commit("wash-1");
  expect(host.querySelectorAll('[role="status"]').length).toBe(2);
  await act(async () => button("moonlight").click());
  expect(host.querySelectorAll('[role="status"]').length).toBe(0);
  for (const [slot, value] of Object.entries({ accent: "#a0c4ff", "wash-1": "#334455", "wash-2": "#223344", "wash-3": "#445566", base: "#05080a" })) {
    await drag(slot, value);
    await commit(slot);
    expect(input(slot).value, slot).toBe(value);
    expect(pressed(), slot).toEqual([]);
    expect(reset().disabled).toBe(false);
  }
});

it("storage changes refresh all picks and pressed state, including partial custom records", async () => {
  const p = COLOUR_PRESETS.find((p) => p.id === "nebula")!;
  await external({ accent: p.accent.toUpperCase(), washes: p.washes, base: p.base });
  expect(pressed()).toEqual([p.name]);
  expect(input("accent").value).toBe(p.accent);
  expect(input("base").value).toBe(p.base);
  p.washes.forEach((w, i) => expect(input(`wash-${i + 1}`).value).toBe(w));
  await external({ accent: p.accent });
  expect(pressed()).toEqual([]);
  expect(input("wash-1").value).toBe(DEFAULT_COLOURS.washes[0]);
  expect(reset().disabled).toBe(false);
  await external({ accent: DEFAULT_COLOURS.accent });
  expect(pressed()).toEqual(["Aurora"]);
  expect(reset().disabled).toBe(false); // explicit default still has a stored copy to clear
  await act(async () => button("aurora").click());
  expect(reset().disabled).toBe(true);
  expect(saved()).not.toHaveProperty("accent");
});

it("presets and Aurora preserve unrelated and unknown stored fields", async () => {
  const unrelated = { motion: "still", stir: false, activity: false, drift: 0.3, future: { keep: true } };
  await external(unrelated);
  for (const id of ["orchid", "aurora"]) {
    await act(async () => button(id).click());
    expect(saved()).toMatchObject(unrelated);
  }
  expect(saved()).toEqual(unrelated);
});

const unsafe: [string, Partial<ColourPreset>][] = [
  ["meaning accent", { accent: AMBER }],
  ["malformed accent", { accent: "var(--red)" }],
  ["meaning wash", { washes: ["#ff7a00", "#3468c4", "#6450c4"] }],
  ["wash needing a clamp", { washes: ["#9fe8ff", "#3468c4", "#6450c4"] }],
  ["base needing a clamp", { base: "#405060" }],
  ["unsafe blend of individually safe colours", { washes: ["#590029", "#3468c4", "#6450c4"], base: "#000000" }],
  ["accent unreadable over its own washes", { accent: "#ababab", washes: ["#838283", "#838283", "#838283"] }],
];

it.each(unsafe)("a preset with %s is refused before storage", async (_label, patch) => {
  const p = COLOUR_PRESETS[1], original = { ...p };
  // Inject a broken catalog entry without changing production source, then exercise the actual click.
  Object.assign(p, patch);
  try {
    expect(checkPreset(p)).not.toBeNull();
    if (_label === "unsafe blend of individually safe colours") {
      p.washes.forEach((w) => expect(checkWash(w)).toMatchObject({ ok: true, clamped: false }));
      expect(checkBase(p.base)).toMatchObject({ ok: true, clamped: false });
      expect(checkPalette(p.washes, p.base)).not.toBeNull();
    }
    if (_label === "accent unreadable over its own washes") {
      expect(checkAccent(p.accent).ok).toBe(true);
      expect(checkAccent(p.accent, p.washes).ok).toBe(false);
    }
    await act(async () => button(p.id).click());
    expect(localStorage.getItem(APPEARANCE_KEY)).toBeNull();
    expect(pressed()).toEqual(["Aurora"]);
    expect(host.querySelector('[role="status"]')?.textContent).toContain("Not applied:");
  } finally {
    Object.assign(p, original);
  }
});
