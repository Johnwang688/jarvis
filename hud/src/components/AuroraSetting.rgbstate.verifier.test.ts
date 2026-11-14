import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, DEFAULT_APPEARANCE, COLOUR_PRESETS, resolveColours, loadAppearance } from "../lib/appearance";
import { applyAppearance, installAppearance } from "../lib/appearanceApply";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root, uninstall: () => void;
beforeEach(async () => {
  vi.useFakeTimers(); localStorage.clear(); uninstall = installAppearance();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  await act(async () => root.render(createElement(AuroraSetting)));
});
afterEach(async () => {
  await act(async () => root.unmount()); host.remove(); uninstall(); localStorage.clear();
  applyAppearance(DEFAULT_APPEARANCE); vi.useRealTimers();
});
const q = <T extends HTMLElement = HTMLElement>(id: string) => host.querySelector<T>('[data-testid="appearance-' + id + '"]')!;
const click = async (id: string) => act(async () => q<HTMLButtonElement>(id).click());
const stored = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}");
async function external(record: object) {
  await act(async () => {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(record));
    window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY }));
  });
}
async function drag(slot: string, value: string) {
  await act(async () => {
    const el = q<HTMLInputElement>(slot);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
it("RGB code picker shows the accepted colour against the displayed RGB washes", async () => {
  await external({ rgb: true, washes: ["#838283", "#838283", "#838283"] });
  await drag("code-accent", "#ababab");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 1));
  expect(stored().codeAccent).toBe("#ababab");
  expect(document.documentElement.style.getPropertyValue("--code-accent")).toBe("#ababab");
  expect(q("code-accent-hex").textContent).toBe("#ababab");
  expect(q<HTMLInputElement>("code-accent").value).toBe("#ababab");
});
it.each(["preset-rgb", "preset-pink", "reset"])("drops a pending code pick on %s", async id => {
  await external({ codeAccent: "#c4a8ff", future: { keep: [1, 2] } });
  await drag("code-accent", "#c0aef5");
  await click(id);
  await act(async () => vi.advanceTimersByTime(SETTLE_MS * 4));
  expect(stored().codeAccent).toBe(id === "reset" ? undefined : "#c4a8ff");
  expect(stored().future).toEqual({ keep: [1, 2] });
  expect(vi.getTimerCount()).toBe(0);
});
it("storage records matching a preset never press it while RGB is on", async () => {
  for (const p of COLOUR_PRESETS) {
    await external({ ...p, rgb: true });
    expect([...host.querySelectorAll('[aria-pressed="true"]')].map(e => (e as HTMLElement).dataset.testid)).toEqual(["appearance-preset-rgb"]);
  }
  await external({ rgb: "true", accent: "#c4a8ff" });
  expect(host.querySelectorAll('[aria-pressed="true"]').length).toBe(0);
});
it("storage event rechecks hostile code accents before applying them", async () => {
  await external({ rgb: "true", accent: "url(x)", codeAccent: "#fbbf24", washes: ["#ff0000", "#ffaa00", "#ffff00"] });
  expect(resolveColours(loadAppearance()).codeAccent).toBe("#6fc3df");
  expect(document.documentElement.style.getPropertyValue("--code-accent") || "#6fc3df").toBe("#6fc3df");
});

it("wash picks recheck the code accent and refuse a newly unreadable combination", async () => {
  await drag("code-accent", "#ababab");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 1));
  expect(stored().codeAccent).toBe("#ababab");
  await drag("wash-1", "#838283");
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 1));
  expect(stored().washes).toBeUndefined();
  expect(q("wash-1-note").textContent).toMatch(/Not applied.*editor and terminal/);
});
it("speed slider reverses both endpoints, clamps stored speeds, and preserves unknown fields", async () => {
  for (const [seconds, expected] of [[1, 8], [999, 120], [30, 30]]) {
    await external({ rgb: true, rgbSpeed: seconds, future: { keep: true } });
    expect(q<HTMLInputElement>("rgb-speed").value).toBe(String(128 - expected));
    expect(q("rgb-speed-value").textContent).toBe(expected + " s per cycle");
  }
  for (const [position, seconds] of [[120, 8], [8, 120]]) {
    await drag("rgb-speed", String(position));
    expect(stored().rgbSpeed).toBe(seconds);
    expect(q("rgb-speed-value").textContent).toBe(seconds + " s per cycle");
    expect(stored().future).toEqual({ keep: true });
  }
});
