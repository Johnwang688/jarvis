// V9: the self-voice verdict through the real Capture (plan §3.4 step 2, §5
// "self-voice frames never open an utterance"). The gate itself is tested in
// selfvoice.test.ts; here its verdicts are scripted per frame, the way the
// window hands them over, and the model's probability and the level are chosen
// to say "speech" so that only the verdict can be what stops a frame.

import { describe, expect, it } from "vitest";
import { Capture } from "./capture";
import type { SelfFrame } from "./selfvoice";
import type { ClaimSource, DropReason } from "./voiceDiagnostics";

const HIS: SelfFrame = { active: true, playing: true, self: true, reason: "echo" };
const OWNER: SelfFrame = { active: true, playing: true, self: false, reason: "unexplained" };
const TAIL: SelfFrame = { active: true, playing: false, self: true, reason: "echo" };

function mk(state: { suppressed?: boolean; muted?: boolean } = {}) {
  const listens: (ClaimSource | undefined)[] = [];
  const drops: { reason: DropReason; source: ClaimSource }[] = [];
  const ends: { truncated: boolean; chunks: number }[] = [];
  let clock = 10_000;
  const cap = new Capture({
    suppressed: () => !!state.suppressed, muted: () => !!state.muted, onLevel: () => {},
    onListening: (s) => listens.push(s), onIdle: () => {},
    onChunk: () => {},
    onDrop: (reason, _ms, source) => drops.push({ reason, source }),
    onUtteranceEnd: (e) => ends.push({ truncated: e.truncated, chunks: e.chunks }),
  }, () => clock);
  const F = 512;
  const talk = (ms: number, self: SelfFrame | null, p = 0.95, amp = 0.05) => cap.feedMs(ms, amp, F, p, self);
  const quiet = (ms: number, self: SelfFrame | null = null) => cap.feedMs(ms, 0.0009, F, 0.02, self);
  return { cap, listens, drops, ends, talk, quiet, tick: (ms: number) => (clock += ms) };
}

describe("self-voice frames never open an utterance", () => {
  it("loud, speech-shaped frames judged his own voice open nothing, however long", () => {
    const m = mk();
    m.quiet(1500);
    m.cap.openFollowUp(); // a window that would claim anything that opened
    m.talk(6000, HIS);
    expect(m.cap.vad.open).toBe(false);
    expect(m.listens).toEqual([]);
    expect(m.cap.segClaimed).toBe(false);
  });

  it("the same frames, judged the owner's, do open one (the verdict is what decided)", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(1000, OWNER);
    expect(m.cap.vad.open).toBe(true);
  });

  it("with the gate absent the capture is exactly as before", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(1000, null);
    expect(m.cap.vad.open).toBe(true);
  });

  it("an energy-detector frame (no probability) as loud as speech is vetoed too", () => {
    const m = mk();
    m.cap.feedMs(1500, 0.0009, 512, undefined, null);
    m.cap.feedMs(2000, 0.08, 512, undefined, HIS);
    expect(m.cap.vad.open).toBe(false);
  });

  it("vetoed frames do not move the noise floor", () => {
    const m = mk();
    m.quiet(2000);
    const before = m.cap.vad.floor;
    m.cap.feedMs(8000, 0.08, 512, undefined, HIS);
    expect(m.cap.vad.floor).toBe(before);
  });

  it("an utterance that opened on the owner's frames is not held open by his voice after it", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(1000, OWNER);
    expect(m.cap.vad.open).toBe(true);
    m.talk(3000, HIS); // only his voice from here: a 2.5 s pause as far as the detector can tell
    expect(m.cap.vad.open).toBe(false);
  });

  it("is reported as self_voice when speech-shaped sound was held back", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(1500, HIS);
    m.quiet(600); // the region ends: one row, not one per frame
    expect(m.drops.filter((d) => d.reason === "self_voice")).toHaveLength(1);
    expect(m.drops.find((d) => d.reason === "self_voice")!.source).toBe("unclaimed");
  });
});

describe("while his voice is audible, only the owner's own act claims", () => {
  it("the follow-up window closes when a reply begins to sound", () => {
    const m = mk();
    m.quiet(1500);
    m.cap.openFollowUp();
    expect(m.cap.conversing()).toBe(true);
    m.quiet(300, TAIL); // the tail of the last reply: not a new one
    expect(m.cap.conversing()).toBe(true);
    m.quiet(300, HIS); // a new reply sounds
    expect(m.cap.conversing()).toBe(false);
  });

  it("the continuation window is closed while he is audible", () => {
    const m = mk();
    m.quiet(1500);
    m.cap.openFollowUp();
    m.talk(1500, null);
    m.quiet(3000); // message sent: a continuation window is open
    expect(m.cap.continueUntil).toBeGreaterThan(0);
    m.quiet(100, HIS);
    expect(m.cap.continueUntil).toBe(-1);
  });

  it("the owner's speech over him is unclaimed: it is dropped, with the reason", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(1000, OWNER);
    m.quiet(3000, OWNER);
    expect(m.listens).toEqual([]);
    expect(m.drops.map((d) => d.reason)).toContain("unclaimed");
  });

  it("a wake hit with nothing open while he is audible is ignored, reported, and not kept for the next utterance", () => {
    const m = mk();
    m.quiet(1500);
    m.quiet(100, HIS);
    expect(m.cap.onWake()).toBe("ignored");
    expect(m.drops.some((d) => d.reason === "self_voice" && d.source === "wake")).toBe(true);
    // The owner speaks within the grace: nothing was held in reserve, so it is not claimed.
    m.talk(1000, OWNER);
    m.quiet(3000, OWNER);
    expect(m.listens).toEqual([]);
  });

  it("a wake hit during an utterance the owner opened over him claims it", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(800, OWNER);
    expect(m.cap.vad.open).toBe(true);
    expect(m.cap.onWake()).toBe("claimed");
    expect(m.listens).toEqual(["wake"]);
    m.talk(800, OWNER);
    m.quiet(3000, OWNER);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].truncated).toBe(false);
  });

  it("a wake hit with his voice not audible is exactly as before (armed, then claims the next utterance)", () => {
    const m = mk();
    m.quiet(1500);
    expect(m.cap.onWake()).toBe("armed");
    m.talk(800, null);
    expect(m.listens).toEqual(["wake"]);
  });

  it("a claimed message keeps recording through his voice (no truncation)", () => {
    const m = mk();
    m.quiet(1500);
    m.talk(800, OWNER);
    m.cap.onWake();
    m.talk(1500, HIS);
    expect(m.ends).toHaveLength(0);
    expect(m.cap.recording()).toBe(true);
  });

  it("the talk key still records, through his voice", () => {
    const m = mk();
    m.quiet(1500);
    m.quiet(500, HIS);
    expect(m.cap.press()).toBe(true);
    m.talk(2000, HIS);
    m.cap.release();
    expect(m.listens).toEqual(["ptt"]);
    expect(m.ends).toHaveLength(1);
  });
});

describe("what the gate cannot excuse", () => {
  it("a card, OFF and the rest still suppress whatever the verdict says", () => {
    const state = { suppressed: true };
    const m = mk(state);
    m.quiet(1500);
    m.cap.openFollowUp();
    m.talk(2000, OWNER);
    m.quiet(3000, OWNER);
    expect(m.listens).toEqual([]);
    expect(m.cap.recording()).toBe(false);
    expect(m.drops.some((d) => d.reason === "suppressed:speaking")).toBe(true);
  });
});
