// Was the orchestrator's test (2026-11-14, after Sol round 2) that a preset whose colours blend into amber/red
// is refused at the click. Owner 2026-11-14 removed the amber/red guard and its blend proof, so the same
// record (every per-colour check passes) is now applied at the click.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting } from "./AuroraSetting";
import { APPEARANCE_KEY } from "../lib/appearance";
import { CUSTOM_PRESETS_KEY, checkCustomPreset, loadCustomPresets } from "../lib/presets";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root;
// Every per-colour guard passes; it used to be refused by the whole-palette proof (Sol round 2's minimized record).
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

it("a stored preset whose colours blend into warm tones is applied at the click, with no note", async () => {
  expect(checkCustomPreset(BLEND as never)).toBeNull();
  const btn = host.querySelector<HTMLButtonElement>('[data-testid="appearance-custom-c-blend"]');
  expect(btn).not.toBeNull();
  if (btn) {
    await act(async () => btn.click());
    expect(JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}").washes).toEqual(BLEND.washes);
    expect(host.querySelector('[data-testid="appearance-custom-note"]')?.textContent ?? "").toBe("");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
  }
  expect(loadCustomPresets().map((p) => p.id)).toEqual(["c-blend"]);
});
