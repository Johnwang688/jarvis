import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import {
  APPEARANCE_KEY, COLOUR_PRESETS, DEFAULT_APPEARANCE, DEFAULT_CODE_ACCENT, DEFAULT_COLOURS,
  checkAccent, checkPalette, loadAppearance, resolveColours, rgbWashes,
} from "../lib/appearance";
import { applyAppearance, installAppearance } from "../lib/appearanceApply";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const GRAY = ["#838283", "#838283", "#838283"];
const RGB_ONLY = "#ababab";
let host: HTMLDivElement, root: Root, uninstall: () => void;
beforeEach(async () => {
  vi.useFakeTimers();
  localStorage.clear();
  uninstall = installAppearance();
  host = document.createElement("div");
  document.body.append(host);
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
const q = <T extends HTMLElement = HTMLElement>(id: string) => host.querySelector<T>(`[data-testid="appearance-${id}"]`)!;
const stored = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}");
const click = async (id: string) => act(async () => q<HTMLButtonElement>(id).click());
const settle = async () => act(async () => vi.advanceTimersByTime(SETTLE_MS + 1));
async function external(record: object) {
  await act(async () => {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(record));
    window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY }));
  });
}
async function input(slot: string, value: string, event = "input") {
  await act(async () => {
    const el = q<HTMLInputElement>(slot);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(event, { bubbles: true }));
  });
}
async function commit() {
  await act(async () => q("code-accent").dispatchEvent(new Event("change", { bubbles: true })));
}
function shownAndApplied(expected: string) {
  expect(q<HTMLInputElement>("code-accent").value, "picker").toBe(expected);
  expect(q("code-accent-hex").textContent, "readout").toBe(expected);
  expect(resolveColours(loadAppearance()).codeAccent, "resolver").toBe(expected);
  expect(document.documentElement.style.getPropertyValue("--code-accent") || DEFAULT_CODE_ACCENT, "CSS").toBe(expected);
}

it("fixture is safe statically but its code accent is valid only with RGB on", () => {
  expect(checkPalette(GRAY, DEFAULT_COLOURS.base)).toBeNull();
  expect(checkAccent(RGB_ONLY, GRAY).ok).toBe(false);
  expect(checkAccent(RGB_ONLY, rgbWashes(0)).ok).toBe(true);
});

it.each(["settle", "pending native commit", "native change only"])("RGB code pick uses displayed washes via %s", async path => {
  await external({ rgb: true, washes: GRAY });
  if (path === "native change only") await input("code-accent", RGB_ONLY, "change");
  else {
    await input("code-accent", RGB_ONLY);
    if (path === "settle") await settle();
    else await commit();
  }
  expect(stored().codeAccent).toBe(RGB_ONLY);
  shownAndApplied(RGB_ONLY);
  expect(q("code-accent-note")).toBeNull();
});

it("RGB off displays and applies a safe fallback while preserving the dormant stored code colour", async () => {
  await external({ rgb: true, washes: GRAY, accent: "#e8edf3", codeAccent: RGB_ONLY, future: 7 });
  shownAndApplied(RGB_ONLY);
  await click("preset-rgb");
  expect(stored()).toMatchObject({ rgb: false, codeAccent: RGB_ONLY, washes: GRAY, future: 7 });
  shownAndApplied(DEFAULT_CODE_ACCENT);
  await commit(); // Committing the displayed fallback must not erase the dormant preference.
  expect(stored().codeAccent).toBe(RGB_ONLY);
  shownAndApplied(DEFAULT_CODE_ACCENT);
  await click("preset-rgb");
  shownAndApplied(RGB_ONLY);
});

it("the five static pickers keep their stored display while only the code display follows RGB", async () => {
  await external({ rgb: false, washes: GRAY, accent: "#e8edf3", base: "#05080a", codeAccent: RGB_ONLY });
  const slots = ["accent", "wash-1", "wash-2", "wash-3", "base"];
  const before = slots.map(s => [q<HTMLInputElement>(s).value, q(`${s}-hex`).textContent]);
  shownAndApplied(DEFAULT_CODE_ACCENT);
  await click("preset-rgb");
  expect(slots.map(s => [q<HTMLInputElement>(s).value, q(`${s}-hex`).textContent])).toEqual(before);
  slots.forEach(s => expect(q<HTMLInputElement>(s).disabled).toBe(true));
  expect(q<HTMLInputElement>("code-accent").disabled).toBe(false);
  shownAndApplied(RGB_ONLY);
  await click("preset-rgb");
  expect(slots.map(s => [q<HTMLInputElement>(s).value, q(`${s}-hex`).textContent])).toEqual(before);
  slots.forEach(s => expect(q<HTMLInputElement>(s).disabled).toBe(false));
  shownAndApplied(DEFAULT_CODE_ACCENT);
});

it.each(["settle", "commit"])("a pending code pick is rejudged after a storage RGB-off event via %s", async path => {
  await external({ rgb: true, washes: GRAY });
  await input("code-accent", RGB_ONLY);
  await external({ rgb: false, washes: GRAY, codeAccent: "#c4a8ff" });
  if (path === "settle") await settle(); else await commit();
  expect(stored().codeAccent).toBe("#c4a8ff");
  shownAndApplied("#c4a8ff");
  expect(q("code-accent-note").textContent).toMatch(/Not applied.*Too dark/);
});

it.each(["settle", "commit"])("a pending code pick uses newly displayed RGB washes after storage RGB-on via %s", async path => {
  await external({ rgb: false, washes: GRAY });
  await input("code-accent", RGB_ONLY);
  await external({ rgb: true, washes: GRAY });
  if (path === "settle") await settle(); else await commit();
  expect(stored().codeAccent).toBe(RGB_ONLY);
  shownAndApplied(RGB_ONLY);
});

it("storage events recompute the code display without rewriting a dormant preference", async () => {
  for (const rgb of [true, false, true, false]) {
    await external({ rgb, washes: GRAY, codeAccent: RGB_ONLY });
    expect(stored().codeAccent).toBe(RGB_ONLY);
    shownAndApplied(rgb ? RGB_ONLY : DEFAULT_CODE_ACCENT);
  }
  await external({ rgb: true, washes: GRAY, codeAccent: "#fbbf24" });
  shownAndApplied(DEFAULT_CODE_ACCENT);
});

it("wash commits check the applied fallback and re-resolve the preserved code colour", async () => {
  await external({ rgb: true, washes: GRAY, codeAccent: RGB_ONLY });
  await click("preset-rgb");
  shownAndApplied(DEFAULT_CODE_ACCENT);
  for (const slot of ["wash-1", "wash-2", "wash-3"]) {
    await input(slot, "#334455", "change");
    expect(q(`${slot}-note`)).toBeNull();
    expect(stored().codeAccent).toBe(RGB_ONLY);
  }
  shownAndApplied(RGB_ONLY);
  await input("wash-1", GRAY[0], "change");
  expect(q("wash-1-note").textContent).toMatch(/Not applied.*editor and terminal/);
  expect(stored().washes).toEqual(["#334455", "#334455", "#334455"]);
  shownAndApplied(RGB_ONLY);
});

it.each(["preset-rgb", "preset-aurora", "preset-pink", "reset"])("%s discards a pending RGB-only code pick even after native commit", async id => {
  await external({ rgb: true, washes: GRAY, codeAccent: "#c4a8ff" });
  await input("code-accent", RGB_ONLY);
  await click(id);
  await commit();
  await settle();
  expect(stored().codeAccent).toBe(id === "reset" ? undefined : "#c4a8ff");
  shownAndApplied(id === "reset" ? DEFAULT_CODE_ACCENT : "#c4a8ff");
  expect(vi.getTimerCount()).toBe(0);
});

it("every static preset and Reset resynchronizes the code picker after an RGB-only choice", async () => {
  for (const p of COLOUR_PRESETS) {
    await external({ rgb: true, washes: GRAY, codeAccent: RGB_ONLY });
    await click(`preset-${p.id}`);
    expect(stored().rgb, p.id).toBe(false);
    expect(stored().codeAccent, p.id).toBe(RGB_ONLY);
    const expected = checkAccent(RGB_ONLY, p.washes).ok ? RGB_ONLY : DEFAULT_CODE_ACCENT;
    shownAndApplied(expected);
  }
  await click("reset");
  expect(stored().codeAccent).toBeUndefined();
  shownAndApplied(DEFAULT_CODE_ACCENT);
  expect(q<HTMLButtonElement>("reset").disabled).toBe(true);
});
