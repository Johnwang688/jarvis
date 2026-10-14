// The four rules the capture design turns on, each of which was a bug first.

import { describe, expect, it, vi } from "vitest";
import { Capture } from "./capture";
import { CONTINUE_CHAIN_MS, CONTINUE_MAX_CLAIMS, CONTINUE_MS, HANGOVER_MS, PREROLL_MS, WAKE_GRACE_MS, samplesToMs } from "./vad";
import type { ClaimSource } from "./voiceDiagnostics";

function mk(opts: { muted?: () => boolean; suppressed?: () => boolean } = {}) {
  const sent: { ms: number; source?: ClaimSource }[] = [];
  const notes: string[] = [];
  const listens: { source?: ClaimSource; tag?: number | null }[] = [];
  const tokens: (number | null | undefined)[] = [];
  let clock = 10_000;
  const cap = new Capture(
    {
      suppressed: opts.suppressed || (() => false),
      muted: opts.muted || (() => false),
      onLevel: () => {},
      onListening: (source, tag) => { notes.push("listening"); listens.push({ source, tag }); },
      onIdle: (n) => notes.push(n || "idle"),
      onUtterance: (_wav, ms, source, token) => { sent.push({ ms, source }); tokens.push(token); },
    },
    () => clock,
  );
  return { cap, sent, notes, listens, tokens, tick: (ms: number) => (clock += ms) };
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
    // V3 changed this deliberately: speech straight afterwards is now the
    // message's continuation (CONTINUE_MS, plan §3.2) -- that is the feature.
    // The hit itself still claims one utterance: once the window has lapsed,
    // an aside to someone else is not a second turn.
    cap.feedMs(CONTINUE_MS + 1000, QUIET);
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

describe("a window that stopped drawing sends nothing in hand (review of PR #32)", () => {
  it("abandon() drops a held push-to-talk: releasing it sends nothing", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    expect(cap.press()).toBe(true);
    cap.feedMs(1500, SPEECH);
    cap.abandon();
    cap.release();
    expect(sent).toHaveLength(0);
  });

  it("abandon() drops an utterance already claimed, and the follow-up window", () => {
    const { cap, sent } = mk();
    cap.feedMs(2000, QUIET);
    cap.openFollowUp();
    cap.feedMs(600, SPEECH); // open and claimed by the follow-up window
    cap.abandon();
    cap.feedMs(800, SPEECH);
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    cap.feedMs(1200, SPEECH); // the window is closed too: not claimed
    cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(sent).toHaveLength(0);
  });
});

// V3 (plan §3.2, answered Q3): speech resuming soon after a message ended is
// claimed as its continuation, with no wake word. The window is counted on the
// ring's sample clock, so these tests need no timers at all.
describe("the continuation window (V3)", () => {
  /** A wake-claimed message, fed until it has gone out. The window opens there. */
  function firstMessage(m: ReturnType<typeof mk>, via: "wake" | "follow_up" = "wake", speechMs = 1000) {
    m.cap.feedMs(2000, QUIET);
    if (via === "follow_up") m.cap.openFollowUp();
    m.cap.feedMs(400, SPEECH);
    if (via === "wake") m.cap.onWake();
    // Long speech in breaths: a constant level for many seconds would walk the
    // detector's floor up under itself, and these tests are about the timers.
    for (let left = speechMs; left > 0; left -= 1500) {
      m.cap.feedMs(Math.min(1500, left), SPEECH);
      if (left > 1500) m.cap.feedMs(150, QUIET);
    }
    while (m.sent.length === 0) m.cap.feedMs(64, QUIET, 1024);
  }
  /** `gap` ms of quiet after the message went out, then a second thing said. */
  function resume(m: ReturnType<typeof mk>, gap: number) {
    const before = m.sent.length;
    m.cap.feedMs(gap, QUIET);
    m.cap.feedMs(1200, SPEECH);
    m.cap.feedMs(HANGOVER_MS + 300, QUIET);
    return m.sent.length - before;
  }

  it("claims speech resuming at 5 s but not at 7 s", () => {
    const near = mk();
    firstMessage(near);
    expect(resume(near, 5000)).toBe(1);
    expect(near.sent[1].source).toBe("continuation");
    const far = mk();
    firstMessage(far);
    expect(resume(far, 7000)).toBe(0);
  });

  it("closes where the plan says: claimed just inside CONTINUE_MS, not just outside", () => {
    expect(CONTINUE_MS).toBe(6000);
    const inside = mk();
    firstMessage(inside);
    expect(resume(inside, CONTINUE_MS - 400)).toBe(1);
    const outside = mk();
    firstMessage(outside);
    expect(resume(outside, CONTINUE_MS + 400)).toBe(0);
  });

  it("is also opened by a message the follow-up window claimed", () => {
    const m = mk();
    firstMessage(m, "follow_up");
    expect(m.sent[0].source).toBe("follow_up");
    expect(resume(m, 4000)).toBe(1);
    expect(m.sent[1].source).toBe("continuation");
  });

  it("chains: a continuation is a message too and opens the next window, up to the cap", () => {
    expect(CONTINUE_MAX_CLAIMS).toBe(2);
    const m = mk();
    firstMessage(m);
    expect(resume(m, 3000)).toBe(1);
    expect(resume(m, 3000)).toBe(1);
    expect(m.sent.map((x) => x.source)).toEqual(["wake", "continuation", "continuation"]);
  });

  it("stopgap: a third continuation in one chain is not claimed, and the window is shut", () => {
    const m = mk();
    firstMessage(m);
    resume(m, 3000);
    resume(m, 3000);
    expect(m.cap.continuing()).toBe(false);
    expect(resume(m, 1000)).toBe(0);
    expect(m.sent).toHaveLength(3);
    // A new wake claim starts a fresh chain.
    m.cap.feedMs(CONTINUE_MS + 1000, QUIET);
    m.cap.feedMs(400, SPEECH);
    m.cap.onWake();
    m.cap.feedMs(1000, SPEECH);
    m.cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(m.sent).toHaveLength(4);
    expect(resume(m, 3000)).toBe(1);
  });

  it("stopgap: the chain ends CONTINUE_CHAIN_MS after the claim that began it", () => {
    expect(CONTINUE_CHAIN_MS).toBe(20000);
    const longer = mk();
    firstMessage(longer, "wake", 18000); // the message itself runs 18 s
    expect(resume(longer, 3000)).toBe(0); // 3 s later is within 6 s of its end but past 20 s of its start
    const control = mk();
    firstMessage(control, "wake", 1000);
    expect(resume(control, 3000)).toBe(1);
  });

  it("stopgap: TV-like speech with 2 s gaps after a wake steers at most twice", () => {
    const m = mk();
    firstMessage(m);
    let claimed = 0;
    for (let i = 0; i < 12; i++) claimed += resume(m, 2000);
    expect(claimed).toBeLessThanOrEqual(CONTINUE_MAX_CLAIMS);
    expect(claimed).toBe(2);
  });

  it("only a wake or follow-up claim starts a chain: a push-to-talk or unclaimed message never does", () => {
    const m = mk();
    m.cap.feedMs(2000, QUIET);
    m.cap.press();
    m.cap.feedMs(1200, SPEECH);
    m.cap.release();
    expect(m.tokens).toEqual([null]);
    const u = mk();
    u.cap.feedMs(2000, QUIET);
    u.cap.feedMs(1200, SPEECH);
    u.cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(u.cap.continuing()).toBe(false);
  });

  it("a caller can take a window back when its message did not go out, and only that window", () => {
    const m = mk();
    firstMessage(m);
    const token = m.tokens[0];
    expect(token).toBeTruthy();
    m.cap.cancelContinuation(999);
    expect(m.cap.continuing()).toBe(true);
    m.cap.cancelContinuation(token);
    expect(m.cap.continuing()).toBe(false);
    expect(resume(m, 500)).toBe(0);
    // A later message's window is not closed by a stale token.
    const n = mk();
    firstMessage(n);
    const stale = n.tokens[0];
    resume(n, 2000);
    n.cap.cancelContinuation(stale);
    expect(n.cap.continueUntil).toBeGreaterThanOrEqual(0);
  });

  it("is not opened by speech nobody claimed", () => {
    const m = mk();
    m.cap.feedMs(2000, QUIET);
    m.cap.feedMs(1200, SPEECH);
    m.cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(resume(m, 1000)).toBe(0);
    expect(m.sent).toHaveLength(0);
  });

  it("is not opened by push-to-talk: letting go of the key is the full stop", () => {
    const m = mk();
    m.cap.feedMs(2000, QUIET);
    m.cap.press();
    m.cap.feedMs(1200, SPEECH);
    m.cap.release();
    expect(m.sent).toHaveLength(1);
    expect(resume(m, 1000)).toBe(0);
  });

  it("does not outlive the mic going OFF", () => {
    let muted = false;
    const m = mk({ muted: () => muted });
    firstMessage(m);
    muted = true;
    m.cap.feedMs(500, QUIET);
    muted = false;
    m.cap.markUnmute();
    expect(resume(m, 500)).toBe(0);
  });

  it("does not outlive a card, a picker or his own voice (anything that suppresses the detector)", () => {
    let blocked = false;
    const m = mk({ suppressed: () => blocked });
    firstMessage(m);
    blocked = true;
    m.cap.feedMs(200, QUIET);
    blocked = false;
    expect(resume(m, 500)).toBe(0);
  });

  it("ends when the owner chooses another chat (closeContinuation)", () => {
    const m = mk();
    firstMessage(m);
    m.cap.closeContinuation();
    expect(resume(m, 500)).toBe(0);
  });

  it("ends when the window stopped drawing", () => {
    const m = mk();
    firstMessage(m);
    m.cap.abandon();
    expect(resume(m, 500)).toBe(0);
  });

  it("a wake phrase or the key still outranks it, and push-to-talk closes it", () => {
    const m = mk();
    firstMessage(m);
    m.cap.feedMs(1000, QUIET);
    m.cap.press();
    expect(m.cap.continuing()).toBe(false);
    m.cap.release();
  });
});

describe("what a claim hands back to the window (V3, Q5)", () => {
  it("reports the source, and the tag a follow-up window was opened for", () => {
    const m = mk();
    m.cap.feedMs(2000, QUIET);
    m.cap.openFollowUp(3);
    m.cap.feedMs(1200, SPEECH);
    m.cap.feedMs(HANGOVER_MS + 300, QUIET);
    expect(m.listens).toEqual([{ source: "follow_up", tag: 3 }]);
  });

  it("does not leak a stale tag into a later claim", () => {
    const m = mk();
    m.cap.feedMs(2000, QUIET);
    m.cap.openFollowUp(3);
    m.cap.feedMs(1200, SPEECH);
    m.cap.feedMs(HANGOVER_MS + 300 + CONTINUE_MS + 1000, QUIET);
    m.cap.feedMs(400, SPEECH);
    m.cap.onWake();
    expect(m.listens.at(-1)).toEqual({ source: "wake", tag: null });
  });

  it("a plain follow-up has no tag: the selected chat", () => {
    const m = mk();
    m.cap.feedMs(2000, QUIET);
    m.cap.openFollowUp();
    m.cap.feedMs(1200, SPEECH);
    expect(m.listens).toEqual([{ source: "follow_up", tag: null }]);
  });
});
