// Acceptance tests: the "Editor & terminal" colour picker in Settings -> Appearance (owner 2026-11-13).
// Written by the orchestrator before the build; the implementer may not edit this file.
//
// Contract: a picker `appearance-code-accent` with readout `appearance-code-accent-hex` and note
// `appearance-code-accent-note`, judged like the accent picker (settle, commit, checkAccent against the
// washes). Presets and RGB never change it; RGB leaves it enabled; Reset clears it.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, DEFAULT_CODE_ACCENT } from "../lib/appearance";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root;
beforeEach(async () => {
  vi.useFakeTimers();
  localStorage.clear();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(AuroraSetting)));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  localStorage.clear();
  vi.useRealTimers();
});

const q = <T extends Element = HTMLElement>(id: string) => host.querySelector<T>(`[data-testid="${id}"]`);
const code = () => q<HTMLInputElement>("appearance-code-accent")!;
const stored = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}") as Record<string, unknown>;
async function pick(value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(code(), value);
    code().dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 5));
}
const click = async (id: string) => act(async () => q<HTMLButtonElement>(id)!.click());

it("shows the picker with today's cyan by default", () => {
  expect(code()).not.toBeNull();
  expect(code().value).toBe(DEFAULT_CODE_ACCENT);
  expect(q("appearance-code-accent-hex")!.textContent).toBe(DEFAULT_CODE_ACCENT);
});

it("a pick is stored and shown, and does not touch the accent", async () => {
  await pick("#c0aef5");
  expect(stored().codeAccent).toBe("#c0aef5");
  expect(stored().accent).toBeUndefined();
  expect(q("appearance-code-accent-hex")!.textContent).toBe("#c0aef5");
});

it("an amber pick is refused with a reason and nothing is stored", async () => {
  await pick("#fbbf24");
  expect(stored().codeAccent).toBeUndefined();
  expect(q("appearance-code-accent-note")!.textContent).toMatch(/^Not applied/);
});

it("presets and RGB leave it alone; it stays enabled under RGB; Reset clears it", async () => {
  await pick("#c0aef5");
  await click("appearance-preset-moonlight");
  expect(stored().codeAccent).toBe("#c0aef5");
  expect(stored().accent).toBe("#e8edf3");
  await click("appearance-preset-rgb");
  expect(stored().codeAccent).toBe("#c0aef5");
  expect(code().disabled).toBe(false);
  await click("appearance-preset-aurora");
  expect(stored().codeAccent).toBe("#c0aef5");
  await click("appearance-reset");
  expect(stored().codeAccent).toBeUndefined();
  expect(code().value).toBe(DEFAULT_CODE_ACCENT);
});
