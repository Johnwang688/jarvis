// The four rules the capture design turns on, each of which was a bug first.

import { describe, expect, it, vi } from "vitest";
import { Capture } from "./capture";
import { HANGOVER_MS, PREROLL_MS, WAKE_GRACE_MS, samplesToMs } from "./vad";

function mk(opts: { muted?: () => boolean; suppressed?: () => boolean } = {}) {
  const sent: { ms: number }[] = [];
  const notes: string[] = [];
  let clock = 10_000;
  const cap = new Capture(
    {
      suppressed: opts.suppressed || (() => false),
      muted: opts.muted || (() => false),
      onLevel: () => {},
      onListening: () => notes.push("listening"),
      onIdle: (n) => notes.push(n || "idle"),
      onUtterance: (_wav, ms) => sent.push({ ms }),
    },
    () => clock,
  );
  return { cap, sent, notes, tick: (ms: number) => (clock += ms) };
}

const QUIET = 0.0009;
const SPEECH = 0.06;

describe("an utterance is only sent if it was addressed to him", () => {
  it("captures and discards speech nobody claimed", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    cap.feedMs(1200, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    // Continuous capture without that gate is a hot mic.
    expect(sent).toHaveLength(0);
  });

  it("sends one that landed in the follow-up window", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    cap.openFollowUp();
    cap.feedMs(1200, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(1);
  });

  it("lets the follow-up window lapse", () => {
    const { cap, sent, tick } = mk();
    cap.feedMs(2000, QUIET);
    cap.openFollowUp();
    tick(10_000); // past FOLLOWUP_MS
    cap.feedMs(1200, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(0);
  });
});

describe("a wake phrase claims exactly one utterance", () => {
  it("claims the utterance already open and back-dates past the lag", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    cap.feedMs(400, SPEECH); // the owner is already mid-sentence
    const openedBefore = cap.vad.openedAt;
    expect(cap.onWake()).toBe("claimed");
    expect(cap.vad.openedAt).toBeLessThan(openedBefore);
    expect(cap.ring.written - cap.vad.openedAt).toBeGreaterThanOrEqual(
      // far enough back to cover the recognizer's lag plus the pre-roll
      (WAKE_GRACE_MS + PREROLL_MS) * 16 - 16,
    );
    cap.feedMs(1000, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(1);
    // 1.4s spoken uploads as more than that — the difference is what used to
    // be lost off the front of every wake-word turn.
    expect(sent[0].ms).toBeGreaterThan(1400);
  });

  it("does not let one hit claim the next thing said as well", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    cap.feedMs(400, SPEECH);
    cap.onWake();
    cap.feedMs(900, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(1);
    // An aside to someone else, straight afterwards, is not a second turn.
    cap.feedMs(1200, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(1);
  });

  it("arms rather than claiming when the phrase arrived before any speech", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    expect(cap.onWake()).toBe("armed");
    cap.feedMs(1000, SPEECH); // WAKE_GRACE_MS lets this one claim itself
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(1);
  });
});

describe("he does not answer himself", () => {
  it("tracks the room while suppressed but never opens an utterance", () => {
    let speaking = true;
    const { cap, sent } = mk({ suppressed: () => speaking });
    cap.feedMs(2000, QUIET);
    const before = cap.vad.thresh;
    cap.feedMs(6000, SPEECH); // his own voice out of the speakers
    expect(sent).toHaveLength(0);
    expect(cap.vad.thresh).toBeGreaterThan(before); // the room was still tracked
    speaking = false;
    cap.openFollowUp();
    cap.feedMs(1400, SPEECH * 3);
    cap.feedMs(HANGOVER_MS + 400, QUIET);
    expect(sent).toHaveLength(1);
  });
});

describe("the OFF mode is the mic mute", () => {
  it("claims nothing and uploads nothing while muted", () => {
    let muted = true;
    const { cap, sent } = mk({ muted: () => muted });
    cap.feedMs(2000, QUIET);
    cap.openFollowUp(); // a muted mic must not promise "just speak"
    cap.feedMs(1400, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(0);
    expect(cap.onWake()).toBe("armed");
    expect(cap.press()).toBe(false);
    muted = false;
  });

  it("keeps tracking the room locally so the threshold is current at unmute", () => {
    const { cap } = mk({ muted: () => true });
    cap.feedMs(2000, QUIET);
    const before = cap.vad.thresh;
    cap.feedMs(15000, SPEECH);
    expect(cap.vad.thresh).toBeGreaterThan(before);
  });

  it("never uploads audio captured while muted, not even as pre-roll", () => {
    let muted = true;
    const { cap, sent } = mk({ muted: () => muted });
    // Three seconds recorded while the owner believed the mic was off. The
    // room's floor rises through it, so the unmuted voice afterwards is louder
    // — which is what a real unmute-and-speak looks like anyway.
    cap.feedMs(3000, SPEECH);
    muted = false;
    cap.markUnmute();
    const floor = cap.micFloorSample;
    expect(floor).toBeGreaterThan(0);
    cap.openFollowUp();
    cap.feedMs(1500, SPEECH * 6);
    cap.feedMs(HANGOVER_MS + 400, QUIET);
    expect(sent).toHaveLength(1);
    // Everything sent starts at or after the unmute mark: the pre-roll and the
    // wake path's back-dating cannot reach across it.
    expect(sent[0].ms).toBeLessThanOrEqual(samplesToMs(cap.ring.written - floor) + 1);
  });
});

describe("push to talk", () => {
  it("keeps its pre-roll, so the first syllable survives", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    expect(cap.press()).toBe(true);
    cap.feedMs(1200, SPEECH);
    cap.release();
    expect(sent).toHaveLength(1);
    expect(sent[0].ms).toBeGreaterThan(1200 + PREROLL_MS - 50);
  });

  it("refuses a tap", () => {
    const { cap, sent, notes } = mk();
    cap.feedMs(2000, QUIET);
    cap.press();
    cap.feedMs(80, SPEECH);
    cap.release();
    expect(sent).toHaveLength(0);
    expect(notes).toContain("TOO SHORT");
  });

  it("does not let the detector re-open on the tail it just sent", () => {
    const { cap } = mk();
    cap.feedMs(2000, QUIET);
    cap.press();
    cap.feedMs(1500, SPEECH);
    cap.release();
    expect(cap.vad.open).toBe(false);
    expect(cap.segClaimed).toBe(false);
  });
});

describe("a noise that is not a turn", () => {
  it("does not consume the follow-up window", () => {
    vi.useFakeTimers();
    const { cap, sent, notes } = mk();
    cap.feedMs(2000, QUIET);
    cap.openFollowUp();
    cap.feedMs(90, SPEECH); // a cough
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(0);
    expect(notes).toContain("DIDN'T CATCH THAT");
    // He was addressed; the owner gets the window back.
    cap.feedMs(1400, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(1);
    vi.useRealTimers();
  });
});
