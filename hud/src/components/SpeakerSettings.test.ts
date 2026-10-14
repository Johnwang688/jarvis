import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseSpeakerStatus, SpeakerStore, type SpeakerStatus } from "../lib/speaker";

const mocks = vi.hoisted(() => ({
  speaker: vi.fn(), speakerReading: vi.fn(), speakerPropose: vi.fn(), speakerAccept: vi.fn(), speakerThreshold: vi.fn(),
  speakerGuest: vi.fn(), speakerDelete: vi.fn(),
}));
vi.mock("../api", () => ({ api: mocks }));
import { SpeakerSettings } from "./SpeakerSettings";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); Object.values(mocks).forEach(m => m.mockReset()); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

const raw = (over: Record<string, unknown> = {}) => ({
  state: "not_enrolled", enrolled: false, readings: 0, readings_min: 3, readings_max: 6, prompts: ["Read this first.", "Then this.", "And this."],
  quality: { min_s: 6, max_s: 30, min_level_db: -40, max_level_db: -6, max_clip: 0.005, min_speech: 0.45 },
  guest: false, mode: "off", threshold: null, threshold_default: 0.6, threshold_min: 0.3, threshold_max: 0.9, short_clip_s: 1.2, short_relax: 0.08,
  installed: true, reason: "", loaded: false, model: "m", fetch: "python -m jarvis.speaker fetch", error: "", last: null, ...over,
});
const status = (over: Record<string, unknown> = {}): SpeakerStatus => parseSpeakerStatus(raw(over))!;

function setup(initial: SpeakerStatus, listenImpl?: (fn: (f: Float32Array) => void) => (() => void) | null) {
  let current = initial;
  mocks.speaker.mockImplementation(async () => current);
  const store = new SpeakerStore({ status: async () => current, guest: async (on) => (mocks.speakerGuest(on), (current = { ...current, guest: on, mode: on ? "guest" : "off" })) });
  let feed: ((f: Float32Array) => void) | null = null;
  const unlisten = vi.fn();
  const listen = vi.fn(listenImpl ?? ((fn) => { feed = fn; return unlisten; }));
  return { store, listen, unlisten, feed: (f: Float32Array) => feed?.(f), set: (s: SpeakerStatus) => { current = s; } };
}
const show = async (h: ReturnType<typeof setup>, props: Record<string, unknown> = {}) => {
  await act(async () => root.render(createElement(SpeakerSettings, { store: h.store, listen: h.listen, ...props })));
  await act(async () => { await new Promise(r => setTimeout(r, 0)); });
};
const q = (id: string) => host.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;
const click = (id: string) => act(async () => { q(id)!.click(); await new Promise(r => setTimeout(r, 0)); });
const speak = (feed: (f: Float32Array) => void, seconds: number, amp: number) => {
  const frames = Math.round((seconds * 16000) / 512);
  let n = 0;
  for (let k = 0; k < frames; k++) {
    const f = new Float32Array(512);
    for (let i = 0; i < 512; i++, n++) f[i] = amp * Math.sin(2 * Math.PI * 220 * n / 16000);
    feed(f);
  }
};

describe("Settings -> Voice -> Your voice", () => {
  it("says whose voice is accepted, shows the first prompt, and does not claim anything it has not read", async () => {
    const h = setup(status());
    await show(h);
    expect(q("speaker-summary")!.textContent).toBe("Not enrolled: anyone's voice is accepted.");
    expect(q("speaker-prompt")!.textContent).toBe("Read this first.");
    expect(q("speaker-delete")).toBeNull();                 // nothing to delete
    expect((q("guest-switch") as HTMLInputElement).checked).toBe(false);
    expect(host.textContent).toContain("Guest voice (no voice enrolled)");
  });

  it("a card up disables every action", async () => {
    const h = setup(status());
    await show(h, { blocked: true });
    expect((q("speaker-record") as HTMLButtonElement).disabled).toBe(true);
    expect((q("guest-switch") as HTMLInputElement).disabled).toBe(true);
  });

  it("a take that is too short is never uploaded", async () => {
    const h = setup(status());
    await show(h);
    await click("speaker-record");
    expect(h.listen).toHaveBeenCalledTimes(1);
    await act(async () => speak(h.feed, 2, 0.2));
    await click("speaker-stop");
    expect(mocks.speakerReading).not.toHaveBeenCalled();
    expect(q("speaker-problems")!.textContent).toMatch(/too short/);
    expect(h.unlisten).toHaveBeenCalled();                  // the microphone is let go
    expect(q("speaker-record")).not.toBeNull();             // and the owner can read it again
  });

  it("a good take is sent once, as WAV, and the status is read back", async () => {
    const h = setup(status());
    mocks.speakerReading.mockImplementation(async () => { h.set(status({ state: "enrolling", readings: 1 })); return { accepted: true, problems: [], readings: 1 }; });
    await show(h);
    await click("speaker-record");
    await act(async () => speak(h.feed, 10, 0.2));
    await click("speaker-stop");
    expect(mocks.speakerReading).toHaveBeenCalledTimes(1);
    const blob = mocks.speakerReading.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("audio/wav");
    expect(blob.size).toBe(44 + 313 * 512 * 2);          // 313 frames of 512 samples, 16-bit mono
    expect(q("speaker-note")!.textContent).toBe("Reading 1 kept.");
    expect(q("speaker-readings")!.textContent).toBe("1 of 3 readings kept.");
    expect(q("speaker-prompt")!.textContent).toBe("Then this.");
  });

  it("the daemon's refusal of a take is shown, not hidden", async () => {
    const h = setup(status());
    mocks.speakerReading.mockResolvedValue({ accepted: false, problems: ["too quiet: speak closer to the microphone"], readings: 0 });
    await show(h);
    await click("speaker-record");
    await act(async () => speak(h.feed, 10, 0.2));
    await click("speaker-stop");
    expect(q("speaker-problems")!.textContent).toMatch(/too quiet/);
    expect(q("speaker-note")).toBeNull();
  });

  it("with the microphone closed it says so and records nothing", async () => {
    const h = setup(status(), () => null);
    await show(h);
    await click("speaker-record");
    expect(q("speaker-error")!.textContent).toMatch(/microphone is not open/);
    expect(q("speaker-stop")).toBeNull();
  });

  it("leaving Settings mid-take lets the microphone go and uploads nothing", async () => {
    const h = setup(status());
    await show(h);
    await click("speaker-record");
    await act(async () => speak(h.feed, 4, 0.2));
    await act(async () => root.unmount());
    expect(h.unlisten).toHaveBeenCalled();
    expect(mocks.speakerReading).not.toHaveBeenCalled();
    root = createRoot(host);
  });

  it("three readings: check them, see the proposal, and accept the number shown", async () => {
    const h = setup(status({ state: "enrolling", readings: 3 }));
    mocks.speakerPropose.mockResolvedValue({
      threshold: 0.47, bins: { lo: -0.2, width: 0.05, n: 24 }, genuine: { n: 9, min: 0.71, p10: 0.73, median: 0.8, hist: new Array(24).fill(0) },
      impostors: { n: 12, max: 0.31, median: 0.1, hist: new Array(24).fill(0), closest: [] }, falseReject: 0, falseAccept: 0, overlap: false, warning: "", readings: 3,
    });
    mocks.speakerAccept.mockImplementation(async () => status({ state: "enrolled", enrolled: true, mode: "on", readings: 3, threshold: 0.47 }));
    await show(h);
    expect(q("speaker-accept")).toBeNull();                  // nothing to accept before the analysis
    await click("speaker-analyse");
    expect(q("speaker-proposed")!.textContent).toBe("0.47");
    expect(q("speaker-proposal")!.textContent).toContain("closest of 12 built-in voices scores 0.31");
    expect((q("speaker-threshold") as HTMLInputElement).value).toBe("0.47");
    await click("speaker-accept");
    expect(mocks.speakerAccept).toHaveBeenCalledWith(0.47);
  });

  it("a threshold outside the daemon's bounds cannot be accepted", async () => {
    const h = setup(status({ state: "enrolling", readings: 3 }));
    mocks.speakerPropose.mockResolvedValue({
      threshold: 0.47, bins: { lo: -0.2, width: 0.05, n: 24 }, genuine: { n: 9, min: 0.7, p10: 0.7, median: 0.8, hist: [] },
      impostors: { n: 0, max: null, median: null, hist: [], closest: [] }, falseReject: 0, falseAccept: null, overlap: false, warning: "no built-in voice could be scored", readings: 3,
    });
    await show(h);
    await click("speaker-analyse");
    expect(q("speaker-warning")!.textContent).toMatch(/no built-in voice/);
    const input = q("speaker-threshold") as HTMLInputElement;
    for (const v of ["0.95", "0.1", "", "abc"]) {
      await act(async () => {
        const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
        set.call(input, v); input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect((q("speaker-accept") as HTMLButtonElement).disabled, v).toBe(true);
    }
  });

  it("the analysis needs the speaker model: without it, it says so", async () => {
    const h = setup(status({ state: "enrolling", readings: 3, installed: false, reason: "the speaker model is not downloaded" }));
    await show(h);
    expect(q("speaker-analyse")).toBeNull();
    expect(host.textContent).toContain("python -m jarvis.speaker fetch");
  });

  it("enrolled: the threshold is tunable and the last check is shown, with Guest and Delete beside it", async () => {
    const h = setup(status({ state: "enrolled", enrolled: true, mode: "on", readings: 3, threshold: 0.5, last: { result: "rejected", score: 0.21 } }));
    mocks.speakerThreshold.mockImplementation(async (t: number) => status({ state: "enrolled", enrolled: true, mode: "on", readings: 3, threshold: t }));
    await show(h);
    expect((q("speaker-threshold-tune") as HTMLInputElement).value).toBe("0.5");
    expect(host.textContent).toContain("Last check: rejected (score 0.21)");
    expect(host.textContent).toContain("Guest voice");
    expect(host.textContent).not.toContain("no voice enrolled");
    await act(async () => {
      const input = q("speaker-threshold-tune") as HTMLInputElement;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "0.62");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click("speaker-save");
    expect(mocks.speakerThreshold).toHaveBeenCalledWith(0.62);
  });

  it("a stale voiceprint offers a rebuild, and nothing else that would trust it", async () => {
    const h = setup(status({ state: "stale", enrolled: false, mode: "blocked", readings: 3, threshold: 0.5 }));
    mocks.speakerPropose.mockResolvedValue({ threshold: 0.44, bins: { lo: -0.2, width: 0.05, n: 24 }, genuine: { n: 1, min: 0, p10: 0, median: 0, hist: [] },
      impostors: { n: 0, max: null, median: null, hist: [], closest: [] }, falseReject: 0, falseAccept: null, overlap: false, warning: "", readings: 3 });
    mocks.speakerAccept.mockResolvedValue(status({ state: "enrolled", enrolled: true, mode: "on", readings: 3, threshold: 0.44 }));
    await show(h);
    expect(q("speaker-threshold-tune")).toBeNull();
    await click("speaker-rebuild");
    expect(mocks.speakerAccept).toHaveBeenCalledWith(0.44);
  });

  it("the Guest switch asks the daemon and shows its answer", async () => {
    const h = setup(status({ state: "enrolled", enrolled: true, mode: "on", threshold: 0.5, readings: 3 }));
    await show(h);
    await act(async () => { (q("guest-switch") as HTMLInputElement).click(); await new Promise(r => setTimeout(r, 0)); });
    expect(mocks.speakerGuest).toHaveBeenCalledWith(true);
    expect((q("guest-switch") as HTMLInputElement).checked).toBe(true);
    expect(q("speaker-summary")!.textContent).toMatch(/^Guest voice is ON/);
  });

  it("Delete asks twice, then removes everything", async () => {
    const h = setup(status({ state: "enrolled", enrolled: true, mode: "on", threshold: 0.5, readings: 3 }));
    mocks.speakerDelete.mockImplementation(async () => status());
    await show(h);
    await click("speaker-delete");
    expect(mocks.speakerDelete).not.toHaveBeenCalled();
    await click("speaker-delete-cancel");
    expect(q("speaker-delete")).not.toBeNull();
    await click("speaker-delete");
    await click("speaker-delete-confirm");
    expect(mocks.speakerDelete).toHaveBeenCalledTimes(1);
    expect(q("speaker-summary")!.textContent).toBe("Not enrolled: anyone's voice is accepted.");
    expect(q("speaker-note")!.textContent).toMatch(/Everything was deleted/);
  });
});
