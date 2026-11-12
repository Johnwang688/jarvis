import { describe, expect, it, vi } from "vitest";
import {
  APPEARANCE_EVENT, APPEARANCE_KEY, DEFAULT_APPEARANCE, loadAppearance, parseAppearance, saveAppearance,
} from "./appearance";

const store = (init: Record<string, string> = {}) => {
  const m = { ...init };
  return { getItem: (k: string) => (k in m ? m[k] : null), setItem: (k: string, v: string) => { m[k] = v; }, m };
};

describe("appearance preferences", () => {
  it("default to Fluid with both stirs on", () => {
    expect(loadAppearance(store())).toEqual({ motion: "fluid", stir: true, activity: true });
    expect(DEFAULT_APPEARANCE.motion).toBe("fluid");
  });

  it("fall back field by field on a corrupt or wrong-typed record", () => {
    for (const raw of ["{", "null", "[]", '"x"', "12", ""]) expect(parseAppearance(raw), raw).toEqual(DEFAULT_APPEARANCE);
    expect(parseAppearance('{"motion":"swirl","stir":"yes","activity":false}')).toEqual({ motion: "fluid", stir: true, activity: false });
    expect(parseAppearance('{"motion":"still","stir":false}')).toEqual({ motion: "still", stir: false, activity: true });
  });

  it("survive a storage that throws", () => {
    const bad = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(loadAppearance(bad)).toEqual(DEFAULT_APPEARANCE);
    expect(() => saveAppearance({ motion: "flow" }, bad)).not.toThrow();
  });

  it("save over what is there, keep AG-3's fields through a round trip, and tell the window", () => {
    const s = store({ [APPEARANCE_KEY]: JSON.stringify({ motion: "flow", accent: "#6fc3df", washes: ["#2a8fa0", "#3468c4", "#6450c4"] }) });
    const heard = vi.fn();
    window.addEventListener(APPEARANCE_EVENT, heard);
    const next = saveAppearance({ stir: false }, s);
    window.removeEventListener(APPEARANCE_EVENT, heard);
    expect(heard).toHaveBeenCalledTimes(1);
    expect(next).toMatchObject({ motion: "flow", stir: false, activity: true, accent: "#6fc3df" });
    expect(JSON.parse(s.m[APPEARANCE_KEY]!).washes).toEqual(["#2a8fa0", "#3468c4", "#6450c4"]);
  });

  it("ignore accent and washes of the wrong type", () => {
    expect(parseAppearance('{"accent":5,"washes":[1,2]}')).toEqual(DEFAULT_APPEARANCE);
  });

  it("saving keeps fields this HUD does not know (a later HUD's base, a malformed accent) without applying them", () => {
    const s = store({ [APPEARANCE_KEY]: JSON.stringify({ motion: "flow", base: "#101010", future: { a: 1 }, accent: 7 }) });
    const next = saveAppearance({ stir: false }, s);
    expect(next).toEqual({ motion: "flow", stir: false, activity: true });
    expect(JSON.parse(s.m[APPEARANCE_KEY]!)).toEqual({ motion: "flow", stir: false, activity: true, base: "#101010", future: { a: 1 }, accent: 7 });
  });
});
