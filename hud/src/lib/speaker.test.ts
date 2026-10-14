import { describe, expect, it, vi } from "vitest";
import {
  GUEST_LABEL, SpeakerStore, guestLabel, parseProposal, parseReadingResult, parseSpeakerStatus, speakerSummary,
  type SpeakerApi, type SpeakerStatus,
} from "./speaker";

const raw = (over: Record<string, unknown> = {}) => ({
  state: "not_enrolled", enrolled: false, readings: 0, readings_min: 3, readings_max: 6, prompts: ["one", "two", "three"],
  quality: { min_s: 6, max_s: 30, min_level_db: -40, max_level_db: -6, max_clip: 0.005, min_speech: 0.45 },
  guest: false, mode: "off", threshold: null, threshold_default: 0.6, threshold_min: 0.3, threshold_max: 0.9,
  short_clip_s: 1.2, short_relax: 0.08, installed: true, reason: "", loaded: false, model: "nemo_en_titanet_small",
  fetch: "python -m jarvis.speaker fetch", error: "", last: null, ...over,
});
const status = (over: Record<string, unknown> = {}) => parseSpeakerStatus(raw(over))!;

describe("parseSpeakerStatus", () => {
  it("reads the daemon's status", () => {
    const s = status({ state: "enrolled", enrolled: true, readings: 3, mode: "on", threshold: 0.55, last: { result: "matched", score: 0.81 } });
    expect(s).toMatchObject({ state: "enrolled", enrolled: true, readings: 3, mode: "on", threshold: 0.55, guest: false });
    expect(s.last).toEqual({ result: "matched", score: 0.81 });
    expect(s.quality.minS).toBe(6);
    expect(s.prompts).toHaveLength(3);
  });
  it("is null for anything that is not an object", () => {
    for (const v of [null, undefined, 3, "x", [], true]) expect(parseSpeakerStatus(v)).toBeNull();
  });
  it("Guest is on only for exactly true", () => {
    for (const v of ["true", 1, "yes", {}, [true], null, undefined]) expect(status({ guest: v }).guest).toBe(false);
    expect(status({ guest: true }).guest).toBe(true);
  });
  it("survives hostile fields", () => {
    const s = status({ state: "wat", mode: 7, readings: "many", threshold: "0.5", prompts: [1, "ok", null, "x".repeat(900)], quality: [], last: "no",
      threshold_min: NaN, error: "e".repeat(1000), model: 5 });
    expect(s.state).toBe("not_enrolled");
    expect(s.mode).toBe("off");
    expect(s.readings).toBe(0);
    expect(s.threshold).toBeNull();
    expect(s.prompts).toHaveLength(2);
    expect(s.prompts[1].length).toBe(400);
    expect(s.quality.minS).toBe(6);
    expect(s.thresholdMin).toBe(0.3);
    expect(s.error.length).toBe(400);
    expect(s.last).toBeNull();
  });
});

describe("the sentence Settings shows", () => {
  it("says what each state means for whose voice is accepted", () => {
    expect(speakerSummary(status())).toBe("Not enrolled: anyone's voice is accepted.");
    expect(speakerSummary(status({ state: "enrolling", readings: 2 }))).toMatch(/2 of 3 readings.*Anyone's voice is accepted/);
    expect(speakerSummary(status({ state: "enrolled", enrolled: true, mode: "on", threshold: 0.5 }))).toBe("Enrolled: only your voice is accepted (threshold 0.50).");
    expect(speakerSummary(status({ guest: true, mode: "guest" }))).toBe("Guest voice (no voice enrolled): anyone's voice is accepted.");
    expect(speakerSummary(status({ guest: true, mode: "guest", enrolled: true, state: "enrolled" }))).toMatch(/^Guest voice is ON/);
    expect(speakerSummary(status({ state: "enrolled", enrolled: true, mode: "blocked", reason: "the speaker model is not downloaded" }))).toMatch(/cannot run.*not downloaded.*Nothing is transcribed/);
    expect(speakerSummary(status({ state: "stale", mode: "blocked" }))).toMatch(/another speaker model/);
    expect(speakerSummary(status({ state: "damaged", mode: "blocked", error: "the saved voiceprint is damaged" }))).toMatch(/Delete it and enroll again/);
    expect(speakerSummary(status({ state: "unsafe", mode: "blocked" }))).toMatch(/not safe/);
  });
});

describe("the label", () => {
  it("is GUEST VOICE exactly while Guest voice is on", () => {
    expect(guestLabel(true)).toBe(GUEST_LABEL);
    expect(GUEST_LABEL).toBe("GUEST VOICE");
    expect(guestLabel(false)).toBe("");
  });
});

function store(initial: SpeakerStatus | null = status(), overrides: Partial<SpeakerApi> = {}) {
  let current = initial;
  const api: SpeakerApi = {
    status: vi.fn(async () => current),
    guest: vi.fn(async (on: boolean) => { current = current && { ...current, guest: on, mode: on ? "guest" : current.enrolled ? "on" : "off" }; return current; }),
    ...overrides,
  };
  return { s: new SpeakerStore(api), api, set: (v: SpeakerStatus | null) => { current = v; } };
}

describe("SpeakerStore", () => {
  it("starts with no label and takes the daemon's status", async () => {
    const { s } = store(status({ guest: true, mode: "guest" }));
    expect(s.snapshot()).toMatchObject({ guest: false, status: null });
    await s.load();
    expect(s.snapshot()).toMatchObject({ guest: true, mode: "guest" });
  });
  it("an older or absent daemon leaves the label off, without an error", async () => {
    const { s } = store(null, { status: async () => { throw new Error("404"); } });
    await s.load();
    expect(s.snapshot()).toMatchObject({ guest: false, error: "" });
  });
  it("another window's switch arrives on the bus at once", () => {
    const { s } = store();
    const seen: boolean[] = [];
    s.subscribe(() => seen.push(s.snapshot().guest));
    s.apply({ guest: true, enrolled: true, mode: "guest" });
    expect(s.snapshot()).toMatchObject({ guest: true, enrolled: true, mode: "guest" });
    s.apply({ guest: false, enrolled: true, mode: "on" });
    expect(s.snapshot().guest).toBe(false);
    expect(seen.slice(0, 2)).toEqual([true, false]);
  });
  it("ignores an event that is not a switch", () => {
    const { s } = store();
    for (const bad of [null, "x", [], { guest: "true" }, { enrolled: true }]) s.apply(bad);
    expect(s.snapshot().guest).toBe(false);
  });
  it("the owner's switch takes the daemon's answer, not the click", async () => {
    const { s, api } = store(status({ enrolled: true, state: "enrolled", mode: "on" }));
    await s.load();
    expect(await s.setGuest(true)).toBe(true);
    expect(api.guest).toHaveBeenCalledWith(true);
    expect(s.snapshot()).toMatchObject({ guest: true, mode: "guest", error: "" });
    await s.setGuest(false);
    expect(s.snapshot()).toMatchObject({ guest: false, mode: "on" });
  });
  it("a refused switch leaves the label as it was and says why", async () => {
    const { s } = store(status({ guest: true, mode: "guest" }), { guest: async () => { throw new Error("the speaker folder is not safe"); } });
    await s.load();
    expect(await s.setGuest(false)).toBe(false);
    expect(s.snapshot().guest).toBe(true);
    expect(s.snapshot().error).toBe("the speaker folder is not safe");
  });
  it("a reload that was already in flight does not undo a switch just made", async () => {
    let release!: () => void;
    const gate = new Promise<void>(r => { release = r; });
    const { s } = store(status(), { guest: async () => { await gate; return status({ guest: true, mode: "guest" }); } });
    const switching = s.setGuest(true);
    await s.load();                                   // the daemon still says Guest is off
    expect(s.snapshot().guest).toBe(false);           // not applied while a change is pending
    release();
    await switching;
    expect(s.snapshot().guest).toBe(true);
  });
});

describe("the wire", () => {
  it("parses a proposal and refuses a broken one", () => {
    const p = parseProposal({
      threshold: 0.48, bins: { lo: -0.2, width: 0.05, n: 24 }, genuine: { n: 9, min: 0.8, p10: 0.82, median: 0.9, hist: new Array(24).fill(1) },
      impostors: { n: 3, max: 0.3, median: 0.1, hist: [1, 2], closest: [{ voice: "af_x", score: 0.3 }, { voice: 1, score: 2 }] },
      false_reject: 0, false_accept: 0, overlap: false, warning: "", readings: 3,
    })!;
    expect(p.threshold).toBe(0.48);
    expect(p.impostors.closest).toEqual([{ voice: "af_x", score: 0.3 }]);
    expect(p.genuine.hist).toHaveLength(24);
    for (const bad of [null, {}, { threshold: "x" }, { threshold: NaN, genuine: {}, impostors: {}, bins: {} }]) expect(parseProposal(bad)).toBeNull();
  });
  it("parses a reading's result", () => {
    expect(parseReadingResult({ accepted: false, problems: ["too short", 4], readings: 1 })).toEqual({ accepted: false, problems: ["too short"], readings: 1 });
    expect(parseReadingResult({ readings: 1 })).toBeNull();
    expect(parseReadingResult("x")).toBeNull();
  });
});
