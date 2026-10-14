// V10: how long it was quiet after the last word (`UtteranceEnd.tailMs`), which
// is what a trailing "over" or "cancel" needs 500 ms of; and the `endNow` seam
// local keyword spotting (V11) will use to end a message at once.

import { describe, expect, it } from "vitest";
import { Capture, type UtteranceEnd } from "./capture";
import { END_PAUSE_MS } from "./vad";
import { SEND_WORD_SILENCE_MS } from "./voiceWords";

function mk(state: { reason?: "card" | null; muted?: boolean } = {}) {
  const ends: UtteranceEnd[] = [];
  let clock = 10_000;
  const cap = new Capture({
    suppressed: () => !!state.reason, suppressionReason: () => state.reason ?? "speaking", muted: () => !!state.muted,
    onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onChunk: () => {},
    onUtteranceEnd: (e) => ends.push(e),
  }, () => clock);
  const F = 512;
  const talk = (ms: number) => cap.feedMs(ms, 0.06, F);
  const quiet = (ms: number) => cap.feedMs(ms, 0.0009, F);
  const claimed = (ms = 1500) => { quiet(1500); cap.openFollowUp(); talk(ms); };
  return { cap, ends, talk, quiet, claimed };
}

describe("tailMs: the quiet behind the last word", () => {
  it("a hands-free message that ended on the pause carries at least the pause", () => {
    const m = mk();
    m.claimed();
    m.quiet(END_PAUSE_MS + 300);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].tailMs).toBeGreaterThanOrEqual(END_PAUSE_MS);
    expect(m.ends[0].tailMs).toBeGreaterThanOrEqual(SEND_WORD_SILENCE_MS);
    expect(m.ends[0].truncated).toBe(false);
  });

  it("a tuned-down pause (the 1 s minimum) still clears the send-word silence", () => {
    const m = mk();
    m.cap.setEndPause(1000);
    m.claimed();
    m.quiet(1300);
    expect(m.ends[0].tailMs).toBeGreaterThanOrEqual(1000);
  });

  it("push-to-talk: a word, 600 ms of quiet, then the key up has 600 ms", () => {
    const m = mk();
    m.quiet(2000);
    m.cap.press();
    m.talk(1200);
    m.quiet(600);
    m.cap.release();
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].tailMs).toBeGreaterThanOrEqual(600);
    expect(m.ends[0].tailMs).toBeLessThan(700);
  });

  it("push-to-talk released in the same breath as the word has none", () => {
    const m = mk();
    m.quiet(2000);
    m.cap.press();
    m.talk(1200);
    m.cap.release();
    expect(m.ends[0].tailMs).toBeLessThan(SEND_WORD_SILENCE_MS);
  });

  it("push-to-talk released 300 ms after the word is still short of the 500 ms", () => {
    const m = mk();
    m.quiet(2000);
    m.cap.press();
    m.talk(1200);
    m.quiet(300);
    m.cap.release();
    expect(m.ends[0].tailMs).toBeLessThan(SEND_WORD_SILENCE_MS);
    expect(m.ends[0].tailMs).toBeGreaterThanOrEqual(300);
  });

  it("a message cut short (a card came up, the mic went OFF) has none, whatever the quiet", () => {
    const card: { reason: "card" | null } = { reason: null };
    const m = mk(card);
    m.claimed();
    m.quiet(800);
    card.reason = "card";
    m.quiet(200);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0]).toMatchObject({ truncated: true, tailMs: 0 });
  });
});

describe("endNow: end the message before the pause runs out (the V11 seam)", () => {
  it("ends a claimed hands-free message at once when it has been quiet 500 ms", () => {
    const m = mk();
    m.claimed();
    m.quiet(SEND_WORD_SILENCE_MS + 60);
    expect(m.ends).toHaveLength(0);
    expect(m.cap.endNow()).toBe(true);
    expect(m.ends).toHaveLength(1);
    expect(m.ends[0].tailMs).toBeGreaterThanOrEqual(SEND_WORD_SILENCE_MS);
    expect(m.ends[0].chunks).toBeGreaterThan(0);
    // The natural pause does not end it a second time.
    m.quiet(END_PAUSE_MS + 500);
    expect(m.ends).toHaveLength(1);
  });

  it("does nothing before the quiet is long enough", () => {
    const m = mk();
    m.claimed();
    m.quiet(300);
    expect(m.cap.endNow()).toBe(false);
    expect(m.ends).toHaveLength(0);
    m.talk(800);                       // speech resumed: the message is still one message
    m.quiet(END_PAUSE_MS + 300);
    expect(m.ends).toHaveLength(1);
  });

  it("does nothing with no claimed message, or for the talk key, which owns its own recording", () => {
    const m = mk();
    m.quiet(1500);
    expect(m.cap.endNow()).toBe(false);
    m.talk(1500);                      // unclaimed room speech
    m.quiet(600);
    expect(m.cap.endNow()).toBe(false);
    m.cap.feedMs(END_PAUSE_MS + 300, 0.0009, 512);
    const n = mk();
    n.quiet(2000);
    n.cap.press();
    n.talk(1200);
    n.quiet(800);
    expect(n.cap.endNow()).toBe(false);
    expect(n.ends).toHaveLength(0);
    n.cap.release();
    expect(n.ends).toHaveLength(1);
  });

  it("arms the continuation window like any message end", () => {
    const m = mk();
    m.claimed();
    m.quiet(600);
    expect(m.cap.endNow()).toBe(true);
    expect(m.ends[0].continuation).not.toBeNull();
  });
});
