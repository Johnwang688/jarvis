import { describe, expect, it } from "vitest";
import { loadNoiseSuppression, saveNoiseSuppression } from "./pref";

const store = (init: Record<string, string> = {}) => {
  const m = { ...init };
  return { getItem: (k: string) => (k in m ? m[k] : null), setItem: (k: string, v: string) => { m[k] = v; }, m };
};

describe("the Noise suppression switch's memory", () => {
  it("is off by default: nothing stored, nothing to load", () => {
    expect(loadNoiseSuppression(store())).toBe(false);
  });
  it("round-trips on and off", () => {
    const s = store();
    saveNoiseSuppression(true, s);
    expect(loadNoiseSuppression(s)).toBe(true);
    saveNoiseSuppression(false, s);
    expect(loadNoiseSuppression(s)).toBe(false);
  });
  it("only an explicit on turns it on: garbage and blocked storage mean off", () => {
    for (const v of ["", "true", "yes", "2", "on"]) expect(loadNoiseSuppression(store({ "jarvis.hud.voice.noiseSuppression": v }))).toBe(false);
    expect(loadNoiseSuppression({ getItem: () => { throw new Error("blocked"); } })).toBe(false);
    expect(() => saveNoiseSuppression(true, { setItem: () => { throw new Error("blocked"); } })).not.toThrow();
  });
});
