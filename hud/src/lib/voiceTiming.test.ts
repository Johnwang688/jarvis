import { describe, expect, it } from "vitest";
import { END_PAUSE_CHOICES, clampEndPause, loadEndPause, saveEndPause } from "./voiceTiming";
import { END_PAUSE_MAX_MS, END_PAUSE_MIN_MS, END_PAUSE_MS } from "./vad";

const store = (init: Record<string, string> = {}) => {
  const m = { ...init };
  return { getItem: (k: string) => (k in m ? m[k] : null), setItem: (k: string, v: string) => { m[k] = v; }, m };
};

describe("the tunable message pause", () => {
  it("defaults to the plan's 2.5 s and offers it as a choice", () => {
    expect(loadEndPause(store())).toBe(2500);
    expect(END_PAUSE_CHOICES).toContain(END_PAUSE_MS);
  });
  it("round-trips, clamped to what the detector accepts", () => {
    const s = store();
    saveEndPause(3000, s);
    expect(loadEndPause(s)).toBe(3000);
    saveEndPause(1, s);
    expect(loadEndPause(s)).toBe(END_PAUSE_MIN_MS);
    saveEndPause(99_999, s);
    expect(loadEndPause(s)).toBe(END_PAUSE_MAX_MS);
  });
  it("garbage or blocked storage falls back to the default", () => {
    expect(loadEndPause(store({ "jarvis.hud.voice.endPauseMs": "soon" }))).toBe(END_PAUSE_MS);
    expect(loadEndPause({ getItem: () => { throw new Error("blocked"); } })).toBe(END_PAUSE_MS);
    expect(() => saveEndPause(2000, { setItem: () => { throw new Error("blocked"); } })).not.toThrow();
    expect(clampEndPause(NaN)).toBe(END_PAUSE_MS);
  });
  it("every offered choice is inside the bounds", () => {
    for (const ms of END_PAUSE_CHOICES) expect(clampEndPause(ms)).toBe(ms);
  });
});
