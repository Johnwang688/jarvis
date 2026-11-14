// Orchestrator acceptance test (2026-11-14, after Sol round 2): the whole-palette proof moved off the load
// path, so the safety intent "a preset whose colours blend into amber/red is never applied" is pinned here,
// at the click. The implementer may not edit this file.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting } from "./AuroraSetting";
import { APPEARANCE_KEY } from "../lib/appearance";
import { CUSTOM_PRESETS_KEY, checkCustomPreset, loadCustomPresets } from "../lib/presets";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root;
// Every per-colour guard passes; only the whole-palette proof refuses (Sol round 2's minimized record).
const BLEND = { id: "c-blend", name: "Blend", accent: "#e8edf3", codeAccent: "#e8edf3",
  washes: ["#0090a1", "#606d42", "#7e7155"], base: "#00181b" };

beforeEach(async () => {
  localStorage.clear();
  localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify([BLEND]));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(createElement(AuroraSetting)));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  localStorage.clear();
});

it("a stored preset that fails only the palette proof is refused at the click: nothing applied, a note shown", async () => {
  expect(checkCustomPreset(BLEND as never)).not.toBeNull();
  const before = localStorage.getItem(APPEARANCE_KEY);
  // Load keeps it: it passes every quick guard, and the button must refuse it at the click.
  const btn = host.querySelector<HTMLButtonElement>('[data-testid="appearance-custom-c-blend"]');
  expect(btn).not.toBeNull();
  if (btn) {
    await act(async () => btn.click());
    expect(localStorage.getItem(APPEARANCE_KEY)).toBe(before);
    expect(document.documentElement.style.getPropertyValue("--wash-1")).not.toBe("#0090a1");
    expect(host.querySelector('[data-testid="appearance-custom-note"]')?.textContent ?? "").not.toBe("");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
  }
  expect(loadCustomPresets().map((p) => p.id)).toEqual(["c-blend"]);
});
