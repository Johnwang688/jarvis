// Acceptance tests for the Pink preset and the RGB button in Settings -> Appearance (owner 2026-11-13).
// Written by the orchestrator before the build; the implementer may not edit this file.
//
// Contract:
//  - the preset row ends with Pink (a static preset) and then an RGB button, `appearance-preset-rgb`, with
//    `aria-pressed` = RGB on. While RGB is on no static preset is pressed.
//  - RGB on keeps the stored accent/washes/base (they come back when RGB goes off); a static preset, Aurora
//    and Reset all turn RGB off. Reset is enabled while RGB is on.
//  - while RGB is on the five colour pickers are disabled and `appearance-rgb-note` explains why.
//  - `appearance-rgb-speed` is a range input, left = slow, right = fast: its value is 128 - seconds, so
//    min 8 (120 s per cycle) and max 120 (8 s per cycle). `appearance-rgb-speed-value` reads "<n> s per
//    cycle". It is disabled unless RGB is on and motion is not Still.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting } from "./AuroraSetting";
import { APPEARANCE_KEY } from "../lib/appearance";

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
const btn = (id: string) => q<HTMLButtonElement>(`appearance-preset-${id}`)!;
const stored = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}") as Record<string, unknown>;
const pressedIds = () => [...host.querySelectorAll<HTMLButtonElement>('[data-testid^="appearance-preset-"][aria-pressed="true"]')]
  .map((b) => b.dataset.testid!.slice("appearance-preset-".length));
const slider = () => q<HTMLInputElement>("appearance-rgb-speed")!;
async function setSlider(v: number) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(slider(), String(v));
    slider().dispatchEvent(new Event("input", { bubbles: true }));
  });
}
const PICKERS = ["accent", "wash-1", "wash-2", "wash-3", "base"];

it("the row ends with Pink then RGB; RGB starts unpressed", () => {
  const ids = [...host.querySelectorAll<HTMLButtonElement>('[data-testid^="appearance-preset-"]')]
    .map((b) => b.dataset.testid!.slice("appearance-preset-".length));
  expect(ids.slice(-2)).toEqual(["pink", "rgb"]);
  expect(btn("rgb").getAttribute("aria-pressed")).toBe("false");
  expect(btn("rgb").textContent).toContain("RGB");
});

it("Pink applies its colours like any preset", async () => {
  await act(async () => btn("pink").click());
  expect(stored()).toMatchObject({ accent: "#f9a8d4", washes: ["#9a2f6e", "#6a2f8a", "#7a2f7e"], base: "#140a12" });
  expect(pressedIds()).toEqual(["pink"]);
});

it("RGB on: stored, only RGB pressed, pickers disabled with a note, Reset enabled; stored colours kept", async () => {
  await act(async () => btn("nebula").click());
  await act(async () => btn("rgb").click());
  expect(stored().rgb).toBe(true);
  expect(stored().accent).toBe("#c4a8ff");
  expect(pressedIds()).toEqual(["rgb"]);
  for (const s of PICKERS) expect(q<HTMLInputElement>(`appearance-${s}`)!.disabled, s).toBe(true);
  expect(q("appearance-rgb-note")?.textContent ?? "").toMatch(/RGB/);
  expect(q<HTMLButtonElement>("appearance-reset")!.disabled).toBe(false);
});

it("clicking RGB again turns it off and the stored colours come back", async () => {
  await act(async () => btn("nebula").click());
  await act(async () => btn("rgb").click());
  await act(async () => btn("rgb").click());
  expect(stored().rgb).toBe(false);
  expect(pressedIds()).toEqual(["nebula"]);
  for (const s of PICKERS) expect(q<HTMLInputElement>(`appearance-${s}`)!.disabled, s).toBe(false);
  expect(q("appearance-rgb-note")).toBeNull();
});

it("a static preset, Aurora and Reset each turn RGB off", async () => {
  await act(async () => btn("rgb").click());
  await act(async () => btn("glacier").click());
  expect(stored().rgb).toBe(false);
  expect(pressedIds()).toEqual(["glacier"]);
  await act(async () => btn("rgb").click());
  await act(async () => btn("aurora").click());
  expect(stored().rgb).toBe(false);
  expect(stored().accent).toBeUndefined();
  expect(pressedIds()).toEqual(["aurora"]);
  await act(async () => btn("rgb").click());
  await act(async () => q<HTMLButtonElement>("appearance-reset")!.click());
  expect(stored().rgb).toBe(false);
  expect(pressedIds()).toEqual(["aurora"]);
});

it("the speed slider: disabled while RGB is off; left is slow, right is fast; the readout follows", async () => {
  expect(slider().disabled).toBe(true);
  await act(async () => btn("rgb").click());
  expect(slider().disabled).toBe(false);
  expect(slider().min).toBe("8");
  expect(slider().max).toBe("120");
  expect(slider().value).toBe(String(128 - 30));
  expect(q("appearance-rgb-speed-value")!.textContent).toBe("30 s per cycle");
  await setSlider(118);
  expect(stored().rgbSpeed).toBe(10);
  expect(q("appearance-rgb-speed-value")!.textContent).toBe("10 s per cycle");
  await setSlider(8);
  expect(stored().rgbSpeed).toBe(120);
});

it("the speed slider is disabled under Still motion even with RGB on", async () => {
  await act(async () => btn("rgb").click());
  const motion = q<HTMLSelectElement>("appearance-motion")!;
  await act(async () => {
    motion.value = "still";
    motion.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(slider().disabled).toBe(true);
});
