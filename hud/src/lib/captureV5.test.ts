// V5: the ML speech probability and the three timers, through the real
// Capture (plan §3.2, §5 "Capture"). Probabilities are scripted per frame the
// way the VAD stage delivers them; amplitudes are chosen to DISAGREE with them
// where that is the point (a quiet talker the energy test would miss, a loud
// noise or a TV it would accept).

import { describe, expect, it } from "vitest";
import { Capture } from "./capture";
import { CHUNK_GAP_MS } from "./recorder";
import {
  CONTINUE_CHAIN_MS, CONTINUE_MAX_CLAIMS, CONTINUE_MS, END_PAUSE_MAX_MS, END_PAUSE_MIN_MS, END_PAUSE_MS, HANGOVER_MS,
  SEGMENT_GAP_MS, SPEECH_OFF_P, SPEECH_ON_P, Vad,
} from "./vad";
import type { ClaimSource, DropReason } from "./voiceDiagnostics";

function mk(state: { suppressed?: boolean; muted?: boolean } = {}) {
  const ends: { ms: number; chunks: number; source: ClaimSource; truncated: boolean }[] = [];
  const listens: (ClaimSource | undefined)[] = [];
  const chunks: number[] = [];
  const drops: DropReason[] = [];
  let clock = 10_000;
  const cap = new Capture({
    suppressed: () => !!state.suppressed, muted: () => !!state.muted, onLevel: () => {},
    onListening: (s) => listens.push(s), onIdle: () => {},
    onChunk: (c) => chunks.push(c.index),
    onDrop: (r) => drops.push(r),
    onUtteranceEnd: (e) => ends.push({ ms: e.ms, chunks: e.chunks, source: e.source, truncated: e.truncated }),
  }, () => clock);
  const F = 512;
  // The stage hands the capture real 512-sample frames.
  const talk = (ms: number, p = 0.9, amp = 0.004) => cap.feedMs(ms, amp, F, p);
  const quiet = (ms: number, p = 0.02, amp = 0.0009) => cap.feedMs(ms, amp, F, p);
  /** A claimed first message: the follow-up window is open, `ms` of speech. */
  const claimed = (ms = 1500) => { quiet(1500); cap.openFollowUp(); talk(ms); };
  return { cap, ends, listens, chunks, drops, talk, quiet, claimed, tick: (ms: number) => (clock += ms) };
}

describe("the three timers (plan §3.2)", () => {
  it("segment gap 700 ms, message pause 2.5 s, continuation window 6 s", () => {
    expect(SEGMENT_GAP_MS).toBe(700);
    expect(END_PAUSE_MS).toBe(2500);
    expect(CONTINUE_MS).toBe(6000);
    expect(HANGOVER_MS).toBe(END_PAUSE_MS);
    expect(CHUNK_GAP_MS).toBe(SEGMENT_GAP_MS); // the recorder's pause cut reads the shared constant
    expect(CONTINUE_MAX_CLAIMS).toBe(2); // V3's chain cap is unchanged
    expect(CONTINUE_CHAIN_MS).toBe(20_000);
    expect(new Vad().endPauseMs).toBe(2500);
    expect(new Capture({} as never).endPauseMs).toBe(2500);
  });
});

describe.each([
  ["ML (Silero) probabilities", true],
  ["energy fallback (no probability)", false],
])("the message pause, %s", (_name, ml) => {
  // In the ML run the audio is quiet but the probability says speech; in the energy run it is loud audio.
  const talk = (m: ReturnType<typeof mk>, ms: number) => (ml ? m.talk(ms, 0.9, 0.004) : m.cap.feedMs(ms, 0.06, 512));
  const quiet = (m: ReturnType<typeof mk>, ms: number) => (ml ? m.quiet(ms, 0.02, 0.06) : m.cap.feedMs(ms, 0.0009, 512));
  const first = (m: ReturnType<typeof mk>) => { m.cap.feedMs(1500, 0.0009, 512, ml ? 0.02 : undefined); m.cap.openFollowUp(); talk(m, 1500); };

  it("a 2.4 s pause continues the same message", () => {
    const m = mk();
    first(m);
    quiet(m, 2400);
    expect(m.ends).toHaveLength(0); // still open
    talk(m, 1500);
    quiet(m, END_PAUSE_MS + 300);
    expect(m.listens).toEqual(["follow_up"]); // one claim, no second
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].ms).toBeGreaterThan(1500 + 2400 + 1400); // both parts and the pause between
  });

  it("a 2.6 s pause ends it; speech after that is a new message (a continuation)", () => {
    const m = mk();
    first(m);
    quiet(m, 2600);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].source).toBe("follow_up");
    expect(m.ends[0].ms).toBeLessThan(1500 + 2600); // the silence that proved it ended is trimmed
    talk(m, 1500);
    quiet(m, END_PAUSE_MS + 300);
    expect(m.ends.map((e) => e.source)).toEqual(["follow_up", "continuation"]);
  });

  it("continuation is claimed at 5 s after a message ended, and not at 7 s", () => {
    for (const [gap, claimed] of [[5000, 1], [7000, 0]] as const) {
      const m = mk();
      first(m);
      quiet(m, END_PAUSE_MS + 300); // the message ends
      expect(m.ends).toHaveLength(1);
      quiet(m, gap);
      talk(m, 1500);
      quiet(m, END_PAUSE_MS + 300);
      expect(m.ends.filter((e) => e.source === "continuation")).toHaveLength(claimed);
    }
  });

  it("the pause is tunable, and held within bounds", () => {
    const m = mk();
    m.cap.setEndPause(1500);
    first(m);
    quiet(m, 1400);
    expect(m.ends).toHaveLength(0);
    quiet(m, 300);
    expect(m.ends).toHaveLength(1);
    const c = new Capture({} as never);
    c.setEndPause(10); expect(c.endPauseMs).toBe(END_PAUSE_MIN_MS);
    c.setEndPause(1e9); expect(c.endPauseMs).toBe(END_PAUSE_MAX_MS);
    c.setEndPause(NaN); expect(c.endPauseMs).toBe(END_PAUSE_MS);
  });
});

describe("the probability, not the loudness, decides", () => {
  it("a quiet talker the energy test would miss opens an utterance", () => {
    const m = mk();
    m.quiet(2000);
    m.cap.openFollowUp();
    m.talk(1500, 0.9, 0.0012); // barely above the floor
    expect(m.listens).toEqual(["follow_up"]);
    m.quiet(END_PAUSE_MS + 300);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].chunks).toBe(1);
  });

  it("loud noise or a slammed door the model calls non-speech opens nothing", () => {
    const m = mk();
    m.quiet(1500);
    m.cap.openFollowUp();
    m.cap.feedMs(4000, 0.2, 512, 0.04);
    m.quiet(END_PAUSE_MS + 300);
    expect(m.listens).toEqual([]);
    expect(m.ends).toEqual([]);
    expect(m.cap.vad.open).toBe(false);
  });

  it("a noise-only recording opens no utterance (here: loud steady noise scored near zero)", () => {
    const m = mk();
    m.quiet(1000);
    let seed = 3;
    for (let i = 0; i < 300; i++) {
      const frame = new Float32Array(512);
      for (let k = 0; k < 512; k++) { seed = (seed * 1664525 + 1013904223) >>> 0; frame[k] = ((seed / 2 ** 32) - 0.5) * 0.2; }
      m.cap.onFrame(frame, 0.01 + (i % 7) * 0.01);
    }
    expect(m.cap.vad.open).toBe(false);
    expect(m.listens).toEqual([]);
    expect(m.drops).toEqual([]);
  });

  it("speech opens only after ONSET (60 ms): one stray frame does not", () => {
    const m = mk();
    m.quiet(1500);
    m.cap.openFollowUp();
    m.talk(32, 0.99);
    m.quiet(500);
    expect(m.listens).toEqual([]);
  });

  it("hysteresis: a probability between 0.35 and 0.5 holds speech open; below 0.35 closes it", () => {
    expect([SPEECH_ON_P, SPEECH_OFF_P]).toEqual([0.5, 0.35]);
    const v = new Vad();
    const n = 512;
    const f = (p: number) => { v.feed(0.01, n, 0, p); return v.loud; };
    expect(f(0.49)).toBe(false); // not enough to start
    expect(f(0.5)).toBe(true);
    expect(f(0.4)).toBe(true);   // a dip inside a word
    expect(f(0.35)).toBe(true);  // at the floor, still in
    expect(f(0.34)).toBe(false);
    expect(f(0.45)).toBe(false); // and back above the off level is not enough to restart
    expect(f(0.6)).toBe(true);
  });

  it("a non-finite probability is judged by energy for that frame", () => {
    const v = new Vad();
    v.feed(0.0009, 512, 0, 0.9);
    expect(v.ml).toBe(true);
    v.feed(0.2, 512, 512, NaN);
    expect(v.ml).toBe(false);
    expect(v.loud).toBe(true);
  });

  it("the noise floor still tracks the room while the ML VAD judges", () => {
    const v = new Vad();
    for (let i = 0; i < 400; i++) v.feed(0.03, 512, i * 512, 0.01);
    expect(v.floor).toBeGreaterThan(0.01);
  });
});

describe("the recorder's pause cut reads the ML flag", () => {
  it("a long message is cut at the 700 ms segment gap the probability says is quiet, even if the room is loud", () => {
    const m = mk();
    m.claimed(1500);
    m.talk(8000); // past CHUNK_SOFT_MS
    expect(m.chunks).toEqual([]);
    m.cap.feedMs(SEGMENT_GAP_MS + 100, 0.2, 512, 0.03); // loud audio, quiet by the model
    expect(m.chunks).toEqual([0]);
    expect(m.ends).toEqual([]); // the message itself stays open
  });
});

describe("what V2-V4 guarantee still holds with the ML VAD", () => {
  it("his own voice never opens an utterance, however sure the model is", () => {
    const state = { suppressed: true };
    const m = mk(state);
    m.quiet(1500);
    m.cap.openFollowUp();
    m.talk(5000, 0.99, 0.1);
    m.quiet(END_PAUSE_MS + 300, 0.02);
    expect(m.listens).toEqual([]);
    expect(m.ends).toEqual([]);
    expect(m.cap.vad.open).toBe(false);
    expect(m.chunks).toEqual([]);
  });

  it("OFF claims nothing and uploads nothing", () => {
    const state = { muted: true };
    const m = mk(state);
    m.cap.openFollowUp();
    m.talk(3000, 0.99, 0.1);
    m.quiet(END_PAUSE_MS + 300);
    expect(m.listens).toEqual([]);
    expect(m.chunks).toEqual([]);
  });

  it("a card appearing mid-message stops the recording at once", () => {
    const state = { suppressed: false };
    const m = mk(state);
    m.claimed(1500);
    state.suppressed = true;
    m.talk(500, 0.99);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].truncated).toBe(true);
    expect(m.chunks).toEqual([]);
  });

  it("falling back from the ML VAD to energy mid-message keeps the same message", () => {
    const m = mk();
    m.claimed(1200);
    m.cap.feedMs(1200, 0.06, 512); // the stage failed: no probability, loud audio
    m.cap.feedMs(END_PAUSE_MS + 300, 0.0009, 512);
    expect(m.listens).toEqual(["follow_up"]);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].ms).toBeGreaterThan(2400);
  });

  it("the pre-roll reaches back before the confirmed onset", () => {
    const m = mk();
    m.quiet(1500);
    m.cap.openFollowUp();
    m.talk(1000);
    m.quiet(END_PAUSE_MS + 300);
    expect(m.ends[0].ms).toBeGreaterThan(1000); // pre-roll included
  });
});
