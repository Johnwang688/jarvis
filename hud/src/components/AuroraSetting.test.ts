// AuroraSetting's picker timing (AG-3): a dragged colour is judged once the input is still for SETTLE_MS, at once
// on the native `change`, and Reset drops a pick still waiting so it cannot land after the defaults.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AuroraSetting, SETTLE_MS } from "./AuroraSetting";
import { APPEARANCE_KEY, DEFAULT_ACCENT } from "../lib/appearance";

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

const accentInput = () => host.querySelector<HTMLInputElement>('[data-testid="appearance-accent"]')!;
const stored = () => JSON.parse(localStorage.getItem(APPEARANCE_KEY) ?? "{}") as { accent?: string };
/** What a picker drag sends: the value, then an `input` event (React's onChange). */
async function drag(value: string) {
  const el = accentInput();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("judges a dragged colour once the input settles, not before", async () => {
  await drag("#a0c4ff");
  expect(stored().accent).toBeUndefined();
  await act(async () => vi.advanceTimersByTime(SETTLE_MS + 5));
  expect(stored().accent).toBe("#a0c4ff");
  expect(accentInput().value).toBe("#a0c4ff");
});

it("judges at once on the picker's change", async () => {
  await drag("#a0c4ff");
  await act(async () => accentInput().dispatchEvent(new Event("change", { bubbles: true })));
  expect(stored().accent).toBe("#a0c4ff");
});

it("Reset drops a pick still waiting, so the defaults stay (no replay after the reset)", async () => {
  await drag("#a0c4ff");
  const reset = host.querySelector<HTMLButtonElement>('[data-testid="appearance-reset"]')!;
  // Reset is enabled only with something custom; make it so with a committed pick first, then a pending one.
  await act(async () => accentInput().dispatchEvent(new Event("change", { bubbles: true })));
  await drag("#b0d4ff");
  expect(reset.disabled).toBe(false);
  await act(async () => reset.click());
  expect(stored().accent).toBeUndefined();
  await act(async () => vi.advanceTimersByTime(SETTLE_MS * 4));
  expect(stored().accent).toBeUndefined();
  expect(accentInput().value).toBe(DEFAULT_ACCENT);
  expect(vi.getTimerCount()).toBe(0);
});
