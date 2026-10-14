import { describe, expect, it } from "vitest";
import {
  CHUNK_HARD_MS, CHUNK_OVERLAP_MS, CHUNK_SOFT_MS, CHUNK_SOFT_OVERLAP_MS, UTTERANCE_LIMIT_MS, UtteranceRecorder, type RecordedChunk,
} from "./recorder";
import { msToSamples, samplesToMs } from "./vad";

const FRAME = 512;
function mk() {
  const chunks: RecordedChunk[] = [];
  const rec = new UtteranceRecorder(7, c => chunks.push(c));
  /** Feed `ms` of audio at a constant value; `loud` says whether the detector heard speech. Returns true once the ceiling is hit. */
  const feed = (ms: number, value = 0.1, loud = true) => {
    let left = msToSamples(ms), hit = false;
    while (left > 0) {
      const n = Math.min(FRAME, left);
      hit = rec.push(new Float32Array(n).fill(value), loud) || hit;
      left -= n;
    }
    return hit;
  };
  return { rec, chunks, feed };
}

describe("UtteranceRecorder", () => {
  it("holds a short message whole and sends it as one chunk at the end", () => {
    const { rec, chunks, feed } = mk();
    feed(3000);
    expect(chunks).toHaveLength(0);
    const s = rec.end(0);
    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toMatchObject({ utterance: 7, index: 0, overlapMs: 0, startMs: 0 });
    expect(chunks[0].ms).toBeCloseTo(3000, -1);
    expect(s).toMatchObject({ chunks: 1 });
  });

  it("takes the pre-roll first, as speech", () => {
    const { rec, chunks } = mk();
    rec.start(new Float32Array(msToSamples(800)).fill(0.2));
    rec.end(0);
    expect(chunks[0].ms).toBeCloseTo(800, -1);
    expect(chunks[0].samples[0]).toBeCloseTo(0.2);
  });

  it("cuts at the first 700 ms pause once the chunk is 8 s long, repeating only 200 ms of quiet", () => {
    const { rec, chunks, feed } = mk();
    feed(CHUNK_SOFT_MS - 500);
    feed(600, 0, false);             // a pause, but the chunk is not 8 s yet
    expect(chunks).toHaveLength(0);
    feed(2000);                      // speech again
    feed(500, 0, false);
    expect(chunks).toHaveLength(0);  // 500 ms is not a long enough pause
    feed(300, 0, false);             // now 800 ms of quiet
    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toMatchObject({ overlapMs: 0, repeats: false });
    feed(2000);
    rec.end(0);
    expect(chunks).toHaveLength(2);
    expect(chunks[1]).toMatchObject({ index: 1, repeats: false });
    expect(chunks[1].overlapMs).toBeCloseTo(CHUNK_SOFT_OVERLAP_MS, -2);   // a sliver of the quiet, so no first phoneme sits on an edge
    expect(chunks[1].samples[0]).toBe(0);                                  // ... and it is quiet
    // together they are everything that was spoken, once
    expect(chunks[0].ms + chunks[1].ms - chunks[1].overlapMs).toBeCloseTo(rec.ms, -1);
  });

  it("pads a cut at a pause with digital silence, never a repeat of the faint audio before it", () => {
    const { rec, chunks, feed } = mk();
    feed(CHUNK_SOFT_MS);
    feed(800, 0.004, false);             // quiet to the detector, but not silent: a trailing word's tail
    expect(chunks).toHaveLength(1);
    feed(1000);
    rec.end(0);
    const pad = chunks[1].samples.subarray(0, msToSamples(CHUNK_SOFT_OVERLAP_MS));
    expect(Array.from(pad).every(v => v === 0)).toBe(true);
    expect(chunks[1].repeats).toBe(false);
  });

  it("takes a shorter pause after 18 s", () => {
    const { chunks, feed } = mk();
    feed(19000);
    expect(chunks).toHaveLength(0);
    feed(350, 0, false);
    expect(chunks).toHaveLength(1);
  });

  it("cuts hard at 25 s in the middle of speech, and the next chunk repeats the last half second", () => {
    const { rec, chunks, feed } = mk();
    // a ramp, so each sample says where it came from
    let n = 0;
    const total = msToSamples(30000);
    while (n < total) {
      const len = Math.min(FRAME, total - n);
      const f = new Float32Array(len);
      for (let i = 0; i < len; i++) f[i] = (n + i) / total;
      rec.push(f, true);
      n += len;
    }
    rec.end(0);
    expect(chunks).toHaveLength(2);
    expect(chunks[0].ms).toBeGreaterThanOrEqual(CHUNK_HARD_MS);
    expect(chunks[0].ms).toBeLessThan(CHUNK_HARD_MS + 40);   // the frame that crossed the line
    expect(chunks[1]).toMatchObject({ repeats: true });
    expect(chunks[1].overlapMs).toBeCloseTo(CHUNK_OVERLAP_MS, -1);
    const a = chunks[0].samples, b = chunks[1].samples, ov = msToSamples(CHUNK_OVERLAP_MS);
    expect(Array.from(b.subarray(0, ov))).toEqual(Array.from(a.subarray(a.length - ov)));
    expect(chunks[1].startMs).toBeCloseTo(CHUNK_HARD_MS - CHUNK_OVERLAP_MS, -2);
    // nothing missing: the unique audio is the whole 30 s
    expect(a.length + b.length - ov).toBe(total);
    expect(void feed).toBeUndefined();
  });

  it("holds at most one chunk of audio, however long the message runs", () => {
    const { rec, chunks, feed } = mk();
    let peak = 0;
    for (let s = 0; s < 600; s++) { feed(1000); peak = Math.max(peak, rec.heldMs); }   // ten minutes, no pause
    rec.end(0);
    expect(peak).toBeLessThanOrEqual(CHUNK_HARD_MS + 40);
    expect(Math.max(...chunks.map(c => c.ms))).toBeLessThanOrEqual(CHUNK_HARD_MS + 40);
    expect(chunks.length).toBeGreaterThanOrEqual(24);
    // every chunk starts where the unique audio so far ends, less its overlap
    let end = 0;
    for (const c of chunks) { expect(c.startMs).toBeCloseTo(end - c.overlapMs, -1); end += c.ms - c.overlapMs; }
  });

  it("trims the closing silence but never into the overlap, and says nothing of a silent tail", () => {
    const a = mk();
    a.feed(2000); a.feed(600, 0, false);
    a.rec.end(msToSamples(400));
    expect(a.chunks[0].ms).toBeCloseTo(2200, -1);

    // a cut at a pause leaves a silent remainder: not sent as a chunk of its own
    const b = mk();
    b.feed(9000); b.feed(800, 0, false);            // cut here
    expect(b.chunks).toHaveLength(1);
    b.feed(1500, 0, false);                          // the message then trails off
    const s = b.rec.end(msToSamples(1300));
    expect(b.chunks).toHaveLength(1);
    expect(s.chunks).toBe(1);

    // ...but a first chunk is always sent: a held talk key is the owner's say-so
    const c = mk();
    c.feed(900, 0, false);
    c.rec.end(0);
    expect(c.chunks).toHaveLength(1);
  });

  describe("the first chunk is cut at the first gap (V5)", () => {
    it("at the first 700 ms pause once 350 ms of speech has been heard, however short the chunk", () => {
      const { rec, chunks, feed } = mk();
      feed(1500); feed(600, 0, false);
      expect(chunks).toHaveLength(0);
      feed(200, 0, false);
      expect(chunks).toHaveLength(1);
      expect(chunks[0]).toMatchObject({ index: 0, overlapMs: 0, startMs: 0 });
      expect(chunks[0].ms).toBeCloseTo(2200, -2);
      expect(rec.chunks).toBe(1);
    });

    it("not before 350 ms of speech: the pre-roll does not count, nor does a click", () => {
      const a = mk();
      a.rec.start(new Float32Array(msToSamples(800)).fill(0.2));
      a.feed(900, 0, false);
      expect(a.chunks).toHaveLength(0);
      const b = mk();
      b.feed(300); b.feed(900, 0, false);
      expect(b.chunks).toHaveLength(0);
      b.feed(100); b.feed(900, 0, false); // 400 ms heard in all: the next long pause cuts
      expect(b.chunks).toHaveLength(1);
    });

    it("once only: a second early pause waits for the 8 s rule", () => {
      const { chunks, feed } = mk();
      feed(1500); feed(800, 0, false);
      feed(2000); feed(800, 0, false);
      expect(chunks).toHaveLength(1);
      feed(6000); feed(800, 0, false);
      expect(chunks).toHaveLength(2);
      expect(chunks[1]).toMatchObject({ index: 1, repeats: false }); // a cut at a pause repeats no speech (main: 200 ms of digital silence leads the next chunk)
    });

    it("what follows an early cut is not sent when it is only silence", () => {
      const { rec, chunks, feed } = mk();
      feed(1500); feed(800, 0, false);
      feed(3000, 0, false);
      const s = rec.end(msToSamples(2000));
      expect(chunks).toHaveLength(1);
      expect(s.chunks).toBe(1);
    });
  });

  it("abort discards what is held, keeps what left, and reports the loss", () => {
    const { rec, chunks, feed } = mk();
    feed(10000); feed(800, 0, false);          // one chunk leaves
    feed(3000);
    const r = rec.abort();
    expect(chunks).toHaveLength(1);
    expect(r.chunks).toBe(1);
    expect(r.droppedMs).toBeGreaterThanOrEqual(3000);          // the speech, and the little quiet after the cut
    expect(r.droppedMs).toBeLessThan(3200);
    expect(rec.push(new Float32Array(512), true)).toBe(false);   // nothing more is taken
    expect(chunks).toHaveLength(1);
  });

  it("reports the 15-minute ceiling", () => {
    const { rec, feed } = mk();
    let hit = false;
    for (let s = 0; s < 899; s++) hit = feed(1000) || hit;
    expect(hit).toBe(false);
    expect(feed(1100)).toBe(true);
    expect(samplesToMs(msToSamples(UTTERANCE_LIMIT_MS))).toBe(UTTERANCE_LIMIT_MS);
    rec.end(0);
  });
});
