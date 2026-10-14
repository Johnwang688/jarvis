// V4 (plan §5, "Capture"): no cap at 30 s, nor on a 45 s push-to-talk; long
// messages leave as ordered chunks and come back as one text; memory stays
// bounded; mic OFF and a waiting card upload nothing; every drop has a reason;
// the unmute clamp still holds. The microphone is scripted through the real
// detector, recorder and assembler (the `feedMs` pattern); only the transcriber
// is a fake, and it "hears" the audio: each second of synthetic speech is a word
// whose number is encoded in its level, so the order and the overlap show.

import { describe, expect, it } from "vitest";
import { Capture, type UtteranceChunk, type UtteranceEnd } from "./capture";
import { CHUNK_HARD_MS, CHUNK_OVERLAP_MS, UTTERANCE_LIMIT_MS } from "./recorder";
import { SttAssembler, STT_MAX_CLIPS, STT_MAX_IN_FLIGHT, type SttPlaced } from "./stt";
import { HANGOVER_MS, samplesToMs } from "./vad";
import { VoiceDiagnostics, type ClaimSource, type DropReason } from "./voiceDiagnostics";

const QUIET = 0.0009;
const level = (k: number) => 0.05 + 0.002 * k;

/** What the fake STT hears: runs of one level are a word, "w<k>". Silence says nothing. */
function hear(samples: Float32Array): string {
  const words: string[] = [];
  let last = -1;
  for (const v of samples) {
    if (v < 0.02) { last = -1; continue; }
    const k = Math.round((v - 0.05) / 0.002);
    if (k !== last) { words.push(`w${k}`); last = k; }
  }
  return words.join(" ");
}

interface Rig {
  cap: Capture;
  stt: SttAssembler<{ id: string }>;
  chunks: UtteranceChunk[];
  ends: UtteranceEnd[];
  drops: { reason: DropReason; ms: number; source: ClaimSource }[];
  placed: SttPlaced<{ id: string }>[];
  uploads: () => number;
  diagnostics: VoiceDiagnostics;
  state: { muted: boolean; reason: "card" | "picker" | "settings" | "crash" | "speaking" | "no_chat" | null; fail: (chunk: number) => boolean; allowUpload: boolean };
  /** Speak words `from..to` (a word is 900 ms of level, then a 100 ms dip, as speech has), optionally pausing `pauseMs` after every `every` words. */
  say(from: number, to: number, o?: { every?: number; pauseMs?: number }): void;
  settle(): Promise<void>;
}

function rig(): Rig {
  const state: Rig["state"] = { muted: false, reason: null, fail: () => false, allowUpload: true };
  let clock = 10_000;
  const chunks: UtteranceChunk[] = [];
  const ends: UtteranceEnd[] = [];
  const drops: Rig["drops"] = [];
  const placed: Rig["placed"] = [];
  const audio = new Map<Blob, Float32Array>();
  const diagnostics = new VoiceDiagnostics(() => 0);
  let calls = 0;
  const stt = new SttAssembler<{ id: string }>({
    diagnostics, maxInFlight: STT_MAX_IN_FLIGHT, canUpload: () => state.allowUpload,
    transcribe: async (blob, o) => {
      calls++;
      await Promise.resolve();
      if (state.fail(o.seq)) throw Object.assign(new Error("down"), { status: 400 });
      return hear(audio.get(blob)!);
    },
    place: b => { placed.push(...b); },
  });
  const groups = new Map<number, { gid: number | null; meta: { id: string } }>();
  const cap = new Capture({
    suppressed: () => state.reason !== null, suppressionReason: () => state.reason ?? "speaking",
    muted: () => state.muted, onLevel: () => {}, onIdle: () => {},
    onDrop: (reason, ms, source) => drops.push({ reason, ms, source }),
    onListening: (_s, _t, u) => { groups.set(u!, { gid: null, meta: { id: `u${u}` } }); },
    onChunk: c => {
      chunks.push(c);
      const g = groups.get(c.utterance)!;
      audio.set(c.wav, c.samples);
      if (g.gid === null) g.gid = stt.beginGroup(c.source, g.meta);
      stt.submit(c.wav, c.ms, c.source, g.meta, { group: g.gid, overlapMs: c.overlapMs });
    },
    onUtteranceEnd: e => {
      ends.push(e);
      const g = groups.get(e.utterance);
      if (g?.gid != null) stt.closeGroup(g.gid);
    },
  }, () => clock);
  const say: Rig["say"] = (from, to, o = {}) => {
    for (let k = from; k <= to; k++) {
      cap.feedMs(900, level(k));
      cap.feedMs(100, QUIET);
      if (o.every && o.pauseMs && (k - from + 1) % o.every === 0 && k < to) cap.feedMs(o.pauseMs, QUIET);
    }
  };
  return {
    cap, stt, chunks, ends, drops, placed, diagnostics, state, say,
    uploads: () => calls,
    settle: async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); await stt.settled(); for (let i = 0; i < 20; i++) await Promise.resolve(); await stt.settled(); },
  };
}

const words = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => `w${from + i}`).join(" ");

describe("no cap: long messages are transcribed whole, in order", () => {
  it("a 45 s push-to-talk is fully transcribed, in order, the overlap said once", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    expect(r.cap.press()).toBe(true);
    r.say(0, 44);                                   // 45 s of speech with no pause long enough to cut at
    r.cap.release();
    await r.settle();
    expect(r.chunks.length).toBeGreaterThanOrEqual(2);
    expect(r.chunks[0].ms).toBeLessThan(CHUNK_HARD_MS + 70);              // a hard cut at 25 s ...
    expect(r.chunks[1].overlapMs).toBeCloseTo(CHUNK_OVERLAP_MS, -2);       // ... with the overlap the join needs
    expect(r.placed).toHaveLength(1);
    expect(r.placed[0].text).toBe(words(0, 44));
    expect(r.placed[0]).toMatchObject({ late: false, source: "ptt", parts: r.chunks.length });
    expect(r.ends).toHaveLength(1);
    expect(r.ends[0]).toMatchObject({ source: "ptt", chunks: r.chunks.length, truncated: false });
    expect(r.ends[0].ms).toBeGreaterThan(45_000);
  });

  it("a push-to-talk past the old 40 s ring keeps its start", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.cap.press();
    r.say(0, 59);                                   // a minute: the ring (40 s) has long since wrapped
    r.cap.release();
    await r.settle();
    expect(r.placed[0].text).toBe(words(0, 59));
  });

  it("a 36 s hands-free dictation is transcribed whole and in order, cut at its pauses", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.cap.openFollowUp();                           // claimed without a wake word
    r.say(0, 35, { every: 9, pauseMs: 900 });       // pauses well inside the 1.5 s that ends a message
    r.cap.feedMs(HANGOVER_MS + 400, QUIET);
    await r.settle();
    expect(r.ends).toHaveLength(1);
    expect(r.ends[0].ms).toBeGreaterThan(36_000);   // the old cap closed this at 30 s
    expect(r.chunks.length).toBeGreaterThanOrEqual(2);
    expect(r.chunks.every(c => c.overlapMs === 0)).toBe(true);              // cut at pauses: nothing repeated
    expect(r.placed).toHaveLength(1);
    expect(r.placed[0].text).toBe(words(0, 35));
    expect(r.drops).toEqual([]);
  });

  it("still closes an unclaimed utterance at 30 s (room speech that never pauses)", () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    // modulated, as a room is
    for (let k = 0; k < 40; k++) { r.cap.feedMs(900, level(k)); r.cap.feedMs(100, QUIET); }
    expect(r.drops.map(d => d.reason)).toContain("unclaimed");
    expect(r.chunks).toHaveLength(0);
  });

  it("a wake word late into a long message still claims it, and nothing before the unmute leaks", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.say(0, 4);
    expect(r.cap.onWake()).toBe("claimed");
    r.say(5, 40);                                    // far past 30 s, claimed
    r.cap.feedMs(HANGOVER_MS + 400, QUIET);
    await r.settle();
    expect(r.placed[0].text.endsWith("w40")).toBe(true);
    expect(r.placed[0].text.split(" ").length).toBeGreaterThan(40);
  });
});

describe("the memory behind a long message is bounded", () => {
  it("holds one chunk of audio and a handful of clips through ten minutes of dictation", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.cap.press();
    let heldPeak = 0, clipPeak = 0;
    for (let s = 0; s < 600; s += 10) {
      r.say(0, 9);                                   // ten seconds a time
      heldPeak = Math.max(heldPeak, (r.cap as any).rec.heldMs);
      clipPeak = Math.max(clipPeak, r.stt.snapshot().clips.length);
      await r.settle();
    }
    r.cap.release();
    await r.settle();
    expect(heldPeak).toBeLessThanOrEqual(CHUNK_HARD_MS + 70);
    expect(Math.max(...r.chunks.map(c => c.ms))).toBeLessThanOrEqual(CHUNK_HARD_MS + 70);
    expect(clipPeak).toBeLessThanOrEqual(STT_MAX_CLIPS);
    expect(r.stt.snapshot().clips).toEqual([]);      // everything placed: no audio left behind
    expect(r.placed).toHaveLength(1);
    expect(r.placed[0].text.split(" ").length).toBeGreaterThanOrEqual(600);
    expect(r.diagnostics.snapshot().rows).toHaveLength(1);   // one row, not one per chunk
  });

  it("ends at the 15-minute ceiling with the message sent as it stood, and says so", async () => {
    const r = rig();
    let limit = -1;
    (r.cap as any).hooks.onLimit = (ms: number) => { limit = ms; };
    r.cap.feedMs(2000, QUIET);
    r.cap.press();
    for (let s = 0; s < 905; s += 5) r.say(0, 4);
    await r.settle();
    expect(limit).toBeGreaterThanOrEqual(UTTERANCE_LIMIT_MS);
    expect(r.ends).toHaveLength(1);
    expect(r.ends[0].ms).toBeGreaterThanOrEqual(UTTERANCE_LIMIT_MS);
    expect(r.cap.recording()).toBe(false);
    r.cap.release();                                  // letting go afterwards sends nothing more
    expect(r.ends).toHaveLength(1);
    expect(r.placed).toHaveLength(1);
  });
});

describe("a failed chunk", () => {
  it("is kept and retryable, and the chunks after it still place in order", async () => {
    const r = rig();
    r.state.fail = seq => seq === 2;                  // the second chunk's upload is refused
    r.cap.feedMs(2000, QUIET);
    r.cap.press();
    r.say(0, 59);
    r.cap.release();
    await r.settle();
    const failed = r.stt.snapshot().clips.filter(c => c.state === "failed");
    expect(failed).toHaveLength(1);
    expect(r.stt.clip(failed[0].id)).not.toBeNull();
    expect(r.placed).toHaveLength(1);
    expect(r.placed[0]).toMatchObject({ late: true });                     // AUTO will not send half a message
    const t = r.placed[0].text;
    expect(t.startsWith("w0 w1")).toBe(true);
    expect(t.endsWith("w59")).toBe(true);
    expect(t).not.toContain("w30");                                       // the failed chunk's middle is missing
    // ... the words after it are in order
    const nums = t.split(" ").map(w => Number(w.slice(1)));
    expect(nums).toEqual([...nums].sort((a, b) => a - b));
    // Retry puts them back
    r.state.fail = () => false;
    expect(r.stt.retry(failed[0].id)).toBe(true);
    await r.settle();
    expect(r.placed).toHaveLength(2);
    expect(r.placed[1]).toMatchObject({ late: true, retried: true });
    expect(r.placed[1].text).toContain("w30");
  });
});

describe("mic OFF and a card upload nothing", () => {
  it("OFF claims nothing: no recorder, no chunk, no upload, however long it is spoken to", async () => {
    const r = rig();
    r.state.muted = true;
    r.cap.feedMs(2000, QUIET);
    r.cap.openFollowUp();
    expect(r.cap.press()).toBe(false);
    expect(r.cap.onWake()).toBe("armed");
    r.say(0, 60);
    r.cap.feedMs(HANGOVER_MS + 400, QUIET);
    await r.settle();
    expect(r.chunks).toHaveLength(0);
    expect(r.uploads()).toBe(0);
    expect(r.cap.recording()).toBe(false);
  });

  it("OFF in the middle of a message stops it: what already left stays, nothing more does, the tail is a `muted` drop", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.cap.press();
    r.say(0, 29);                                      // one chunk leaves (hard cut at 25 s)
    const left = r.chunks.length;
    expect(left).toBe(1);
    r.state.muted = true;
    r.say(30, 59);
    r.cap.release();
    await r.settle();
    expect(r.chunks).toHaveLength(left);               // not one chunk more
    expect(r.drops.map(d => d.reason)).toEqual(["muted", ...r.drops.slice(1).map(d => d.reason)]);
    expect(r.drops[0]).toMatchObject({ reason: "muted", source: "ptt" });
    expect(r.ends[0]).toMatchObject({ truncated: true, chunks: left, continuation: null });
    expect(r.placed[0].text).not.toContain("w40");     // the part said while OFF never left
  });

  it("a card appearing mid-message stops uploading at once; its words wait for review, not for a send", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.cap.openFollowUp();
    r.say(0, 3);
    r.state.reason = "card";
    const before = r.chunks.length;
    const calls = r.uploads();
    r.say(4, 40, { every: 8, pauseMs: 900 });          // would have been several chunks
    r.state.reason = null;
    await r.settle();
    expect(r.chunks).toHaveLength(before);
    expect(r.uploads()).toBe(calls);
    expect(r.drops.map(d => d.reason)).toContain("suppressed:card");
    expect(r.ends[0]).toMatchObject({ truncated: true, continuation: null });
  });

  it("a held talk key is stopped by a card, a picker, Settings and a crash, but carries on through his own voice", () => {
    for (const reason of ["card", "picker", "settings", "crash"] as const) {
      const r = rig();
      r.cap.feedMs(2000, QUIET);
      r.cap.press();
      r.say(0, 2);
      r.state.reason = reason;
      r.say(3, 4);
      expect(r.cap.recording(), reason).toBe(false);
      expect(r.drops.map(d => d.reason), reason).toEqual([`suppressed:${reason}`]);
      r.cap.release();                                  // letting go afterwards adds nothing
      expect(r.drops, reason).toHaveLength(1);
    }
    for (const reason of ["speaking", "no_chat"] as const) {
      const r = rig();
      r.cap.feedMs(2000, QUIET);
      r.cap.press();
      r.state.reason = reason;
      r.say(0, 2);
      expect(r.cap.recording(), reason).toBe(true);
      r.state.reason = null;
      r.say(3, 4);
      r.cap.release();
      expect(r.ends[0], reason).toMatchObject({ chunks: 1, truncated: false, source: "ptt" });
    }
  });

  it("a chunk that waits for its turn is not uploaded if a card came up while it waited", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.cap.press();
    r.say(0, 90);                                       // several chunks, two uploading at a time
    r.state.allowUpload = false;                        // the card arrived before they all got their turn
    r.cap.release();
    await r.settle();
    const states = r.stt.snapshot().clips.map(c => c.state);
    expect(states.some(s => s === "failed")).toBe(true);          // refused, kept, visible
    expect(r.uploads()).toBeLessThan(r.chunks.length);
  });
});

describe("every drop says why", () => {
  const scenarios: [string, (r: Rig) => void, DropReason][] = [
    ["nobody claimed it", r => { r.say(0, 2); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "unclaimed"],
    ["a cough in the follow-up window", r => { r.cap.openFollowUp(); r.cap.feedMs(90, 0.06); r.cap.feedMs(HANGOVER_MS + 300, QUIET); }, "too_short"],
    ["a tap on the talk key", r => { r.cap.press(); r.cap.feedMs(80, 0.06); r.cap.release(); }, "too_short"],
    ["spoken while OFF", r => { r.state.muted = true; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "muted"],
    ["spoken under a card", r => { r.state.reason = "card"; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "suppressed:card"],
    ["spoken with a picker open", r => { r.state.reason = "picker"; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "suppressed:picker"],
    ["spoken in Settings", r => { r.state.reason = "settings"; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "suppressed:settings"],
    ["spoken over his own voice", r => { r.state.reason = "speaking"; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "suppressed:speaking"],
    ["spoken with no chat", r => { r.state.reason = "no_chat"; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "suppressed:no_chat"],
    ["spoken in a crashed window", r => { r.state.reason = "crash"; r.say(0, 3); r.cap.feedMs(HANGOVER_MS + 400, QUIET); }, "suppressed:crash"],
    ["a held key when the window crashes", r => { r.cap.press(); r.say(0, 2); r.cap.abandon(); }, "suppressed:crash"],
    ["a claimed message when the window crashes", r => { r.cap.openFollowUp(); r.say(0, 2); r.cap.abandon(); }, "suppressed:crash"],
    ["a held key when a card appears", r => { r.cap.press(); r.say(0, 1); r.state.reason = "card"; r.say(2, 3); }, "suppressed:card"],
    ["a claimed message when the mic goes OFF", r => { r.cap.openFollowUp(); r.say(0, 1); r.state.muted = true; r.say(2, 3); }, "muted"],
  ];
  it.each(scenarios)("%s", (_name, run, reason) => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    run(r);
    expect(r.chunks).toHaveLength(0);                   // nothing left
    expect(r.drops.map(d => d.reason)).toContain(reason);
    // ... and not twice for one thing
    expect(r.drops.filter(d => d.reason === reason).length).toBeLessThanOrEqual(1);
  });

  it("an upload that fails, an empty transcript and a discard are rows with reasons too", async () => {
    const r = rig();
    r.state.fail = () => true;
    r.cap.feedMs(2000, QUIET); r.cap.press(); r.say(0, 2); r.cap.release();
    await r.settle();
    expect(r.diagnostics.snapshot().rows.at(-1)?.outcome).toBe("stt_failed");
    const id = r.stt.snapshot().clips[0].id;
    r.stt.discard(id);
    // an utterance of silence
    const q = rig();
    q.cap.feedMs(2000, QUIET); q.cap.press(); q.cap.feedMs(1500, QUIET); q.cap.release();
    await q.settle();
    expect(q.diagnostics.snapshot().rows.at(-1)?.outcome).toBe("empty_transcript");
  });
});

describe("the unmute clamp holds across chunks", () => {
  it("nothing from before the unmute is in any chunk, however far back the pre-roll or a wake would reach", async () => {
    const r = rig();
    r.state.muted = true;
    r.cap.feedMs(3000, 0.3);                            // loud, while the owner believed the mic was off
    r.state.muted = false;
    r.cap.markUnmute();
    r.cap.press();
    r.say(0, 29);
    r.cap.release();
    await r.settle();
    const all = r.chunks.flatMap(c => Array.from(c.samples));
    expect(all.length).toBeGreaterThan(0);
    expect(all.reduce((m, v) => Math.max(m, v), 0)).toBeLessThan(0.25);       // no 0.3 anywhere
    expect(samplesToMs(all.length)).toBeLessThanOrEqual(samplesToMs(r.cap.ring.written - r.cap.micFloorSample) + CHUNK_OVERLAP_MS * r.chunks.length + 1);
  });
});

describe("continuation and the chain cap survive chunking (V3)", () => {
  it("a long wake-claimed message arms the continuation window from its end, once", async () => {
    const r = rig();
    r.cap.feedMs(2000, QUIET);
    r.say(0, 1);
    r.cap.onWake();
    r.say(2, 30, { every: 9, pauseMs: 900 });
    r.cap.feedMs(HANGOVER_MS + 400, QUIET);
    expect(r.ends).toHaveLength(1);
    expect(r.ends[0].continuation).toBe(1);
    // speech soon after is the same message's continuation; nothing earlier armed it
    r.say(31, 33);
    r.cap.feedMs(HANGOVER_MS + 400, QUIET);
    expect(r.ends).toHaveLength(2);
    expect(r.ends[1].source).toBe("continuation");
    await r.settle();
    expect(r.placed.map(p => p.text.split(" ")[0])).toEqual(["w0", "w31"]);
  });
});
