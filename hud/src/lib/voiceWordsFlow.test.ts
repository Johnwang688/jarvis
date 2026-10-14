// V10 end to end below the React layer: the real Capture (its tail of quiet),
// the real SttAssembler (chunks, a reply still in flight) and `planWords`, the
// way App.placeTranscripts drives them. Only the transcriber is scripted.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Capture, type UtteranceEnd } from "./capture";
import { SttAssembler, type SttCapture, type SttPlaced } from "./stt";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { planWords, type WordStep, type Words } from "./voiceWords";

const W: Words = { enabled: true, send: ["over"], cancel: ["cancel"] };
const QUIET = 0.0009;

interface Reply { seq: number; resolve: (t: string) => void }

function rig() {
  const replies: Reply[] = [];
  const planned: WordStep<SttPlaced<SttCapture>>[][] = [];
  const ends: UtteranceEnd[] = [];
  const stt = new SttAssembler<SttCapture>({
    diagnostics: new VoiceDiagnostics(() => 0),
    transcribe: (_blob, o) => new Promise<string>((resolve) => { replies.push({ seq: o.seq, resolve }); }),
    place: (batch) => { planned.push(planWords(batch, { words: W, card: false })); },
    now: () => Date.now(),
  });
  const groups = new Map<number, { gid: number | null; meta: SttCapture }>();
  const cap = new Capture({
    suppressed: () => false, muted: () => false, onLevel: () => {}, onIdle: () => {},
    onListening: (_s, _t, u) => { groups.set(u!, { gid: null, meta: { mode: "review", chat: "t1" } }); },
    onChunk: (c) => {
      const g = groups.get(c.utterance)!;
      if (g.gid === null) g.gid = stt.beginGroup(c.source, g.meta);
      stt.submit(c.wav, c.ms, c.source, g.meta, { group: g.gid, overlapMs: c.overlapMs });
    },
    onUtteranceEnd: (e) => {
      ends.push(e);
      const g = groups.get(e.utterance);
      if (g?.gid != null) { g.meta.tailMs = e.tailMs; stt.closeGroup(g.gid); }
    },
  }, () => 10_000);
  /** One push-to-talk dictation: `speechMs` of talking with a pause in the middle (so it leaves as two chunks), then `quietMs`, key up. */
  const dictate = (speechMs: number, quietMs: number) => {
    cap.feedMs(2000, QUIET);
    cap.press();
    const half = Math.floor(speechMs / 2);
    cap.feedMs(half, 0.06);
    cap.feedMs(800, QUIET);        // a place to cut
    cap.feedMs(speechMs - half, 0.06);
    if (quietMs) cap.feedMs(quietMs, QUIET);
    cap.release();
  };
  const tick = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
  return { cap, stt, replies, planned, ends, dictate, tick };
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe("a cancel word discards the whole message, in-flight chunks included", () => {
  it("the last chunk is still being transcribed when the key comes up: nothing is placed until it answers, and then it is all cancelled", async () => {
    const r = rig();
    r.dictate(20_000, 700);
    await r.tick();
    expect(r.replies.length).toBeGreaterThanOrEqual(2);          // two chunks went out
    r.replies[0].resolve("write the quarterly report");           // the first has answered ...
    await r.tick();
    expect(r.planned).toHaveLength(0);                            // ... and the second is in flight: nothing placed
    r.replies[1].resolve("and email it to Sam cancel");
    await r.tick();
    expect(r.planned).toHaveLength(1);
    expect(r.planned[0].map((s) => s.kind)).toEqual(["cancel"]);   // no `run`: not one word survives
    expect(r.planned[0][0].items.map((i) => i.text).join(" ")).toBe("write the quarterly report and email it to Sam");
    expect(r.ends[0].tailMs).toBeGreaterThanOrEqual(700);
  });

  it("the same words released in the same breath stay prose: no quiet, no cancel", async () => {
    const r = rig();
    r.dictate(20_000, 0);
    await r.tick();
    r.replies.forEach((x, i) => x.resolve(i === 0 ? "I want to" : "cancel"));
    await r.tick();
    expect(r.planned[0].map((s) => s.kind)).toEqual(["run"]);
    expect(r.planned[0][0].items[0].text).toBe("I want to cancel");
  });
});

describe("a send word needs its quiet", () => {
  it("send it over + 600 ms of quiet: send, with over stripped", async () => {
    const r = rig();
    r.dictate(3000, 600);
    await r.tick();
    r.replies.forEach((x, i) => x.resolve(r.replies.length === 1 ? "send it over" : i === 0 ? "send" : "it over"));
    await r.tick();
    expect(r.planned[0].map((s) => s.kind)).toEqual(["send"]);
    expect(r.planned[0][0].items.map((i) => i.text).join(" ")).toBe("send it");
  });

  it("the game is over with the key released at once: a sentence", async () => {
    const r = rig();
    r.dictate(3000, 0);
    await r.tick();
    r.replies.forEach((x, i) => x.resolve(r.replies.length === 1 ? "the game is over" : i === 0 ? "the game" : "is over"));
    await r.tick();
    expect(r.planned[0].map((s) => s.kind)).toEqual(["run"]);
    expect(r.planned[0][0].items[0].text).toBe("the game is over");
  });

  it("a chunk that failed makes the message late: its trailing over is not read", async () => {
    const r = rig();
    r.dictate(20_000, 700);
    await r.tick();
    r.replies[0].resolve("first part");
    // The second chunk fails for good (no reply, then the deadline and its one retry).
    await vi.advanceTimersByTimeAsync(40_000);
    await vi.advanceTimersByTimeAsync(40_000);
    await r.tick();
    expect(r.planned).toHaveLength(1);
    expect(r.planned[0][0].kind).toBe("run");
    expect((r.planned[0][0].items[0] as SttPlaced<SttCapture>).late).toBe(true);
  });
});
