// Acceptance tests: in Settings -> Appearance a warm pick (amber, red, orange) is applied, not refused
// (owner 2026-11-14, scope "Everything"). Written by the orchestrator before the build; the implementer may
// not edit this file.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, AMBER, RED } from "../lib/appearance";

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
const stored = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}") as Record<string, unknown>;
async function pick(slot: string, value: string) {
  const el = q<HTMLInputElement>(`appearance-${slot}`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 5));
}

it.each([["accent", AMBER], ["accent", RED], ["code-accent", "#ff8800"], ["wash-1", "#8a2a10"], ["base", "#140806"]])(
  "a warm %s pick (%s) is applied with no refusal note", async (slot, value) => {
    await pick(slot, value);
    const key = slot === "code-accent" ? "codeAccent" : slot.startsWith("wash") ? "washes" : slot;
    const v = stored()[key];
    expect(Array.isArray(v) ? (v as string[])[0] : v).toBe(value);
    expect(q(`appearance-${slot}-note`)?.textContent ?? "").not.toMatch(/amber or red/i);
    expect(host.textContent ?? "").not.toMatch(/Too close to amber or red/);
  });

it("the help text no longer promises that colours near amber/red are refused", () => {
  expect(host.textContent ?? "").not.toMatch(/a colour close to them is not applied/);
});
