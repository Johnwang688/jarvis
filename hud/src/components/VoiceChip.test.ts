import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { VoiceChip } from "./VoiceChip";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

const base = {
  pane: 2 as const, info: { voice: "bm_george", muted: false }, pool: ["af_nicole", "bm_george"],
  speaking: false, active: true, onChoose: vi.fn(), onSkip: vi.fn(), onMute: vi.fn(),
};
const render = (extra: Partial<Parameters<typeof VoiceChip>[0]> = {}) =>
  act(async () => root.render(createElement(VoiceChip, { ...base, ...extra })));
const q = (id: string) => host.querySelector<HTMLElement>(`[data-testid="pane-2-voice${id}"]`)!;

it("shows the chat's voice, pulses while it speaks, and opens inline", async () => {
  await render({ speaking: true });
  expect(q("").getAttribute("data-speaking")).toBe("true");
  expect(q("-button").textContent).toBe("George");
  expect(q("-select")).toBeNull();
  await act(async () => q("-button").click());
  expect(q("-select")).not.toBeNull();
});

it("choosing a voice, skip and mute call back; muted reads Unmute", async () => {
  const onChoose = vi.fn(), onSkip = vi.fn(), onMute = vi.fn();
  await render({ onChoose, onSkip, onMute });
  await act(async () => q("-button").click());
  const select = q("-select") as unknown as HTMLSelectElement;
  await act(async () => { select.value = "af_nicole"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(onChoose).toHaveBeenCalledWith("af_nicole");
  await act(async () => q("-skip").click());
  expect(onSkip).toHaveBeenCalledTimes(1);
  await act(async () => q("-mute").click());
  expect(onMute).toHaveBeenCalledWith(true);
  await render({ onMute, info: { voice: "bm_george", muted: true } });
  expect(q("-mute").textContent).toBe("Unmute");
  expect(q("").getAttribute("data-muted")).toBe("true");
});

it("skip has nothing to skip when the chat has no speech, and every control is dead under a card", async () => {
  await render({ active: false });
  await act(async () => q("-button").click());
  expect((q("-skip") as HTMLButtonElement).disabled).toBe(true);
  await render({ blocked: true });
  expect((q("-button") as HTMLButtonElement).disabled).toBe(true);
  expect((q("-select") as unknown as HTMLSelectElement).disabled).toBe(true);
  expect((q("-mute") as HTMLButtonElement).disabled).toBe(true);
});

it("a voice outside the pool (one since uninstalled) is still listed, so the select shows what is in force", async () => {
  await render({ info: { voice: "bf_old", muted: false } });
  await act(async () => q("-button").click());
  const values = [...host.querySelectorAll("option")].map((o) => o.getAttribute("value"));
  expect(values).toEqual(["", "bf_old", "af_nicole", "bm_george"]);
});
