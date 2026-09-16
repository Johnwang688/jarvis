// The segmenter math, ported from tests/face/hud_capture_check.py's
// expectations. Driving it needs no audio: a frame of N samples at a constant
// amplitude has exactly that RMS, so a synthetic level sequence exercises the
// same code the microphone runs.

import { describe, expect, it } from "vitest";
import {
  CAP_RATE, HANGOVER_MS, MIN_UTTER_MS, PREROLL_MS, Ring, Vad,
  closeBounds, frameRms, longEnough, msToSamples, samplesToMs, wavBlob, wavBytes,
} from "./vad";

/** Feed `ms` of constant-amplitude audio, collecting the detector's events. */
function feed(vad: Vad, ring: Ring, ms: number, amp: number, frame = 1024) {
  const events: string[] = [];
  let done = 0;
  const total = msToSamples(ms);
  while (done < total) {
    const n = Math.min(frame, total - done);
    const buf = new Float32Array(n).fill(amp);
    ring.push(buf);
    const e = vad.feed(frameRms(buf), n, ring.written);
    if (e) events.push(e);
    done += n;
  }
  return events;
}

describe("ring buffer", () => {
  it("is the clock, and a slice past its end yields what is still there", () => {
    const r = new Ring(1); // one second
    r.push(new Float32Array(CAP_RATE).fill(0.5));
    expect(r.written).toBe(CAP_RATE);
    r.push(new Float32Array(CAP_RATE).fill(0.25));
    // The first second has been overwritten; asking for it yields the newest
    // audio rather than silence or an error.
    const s = r.slice(0, r.written);
    expect(s.length).toBe(CAP_RATE);
    expect(s[0]).toBeCloseTo(0.25, 5);
  });
});

describe("the adaptive threshold", () => {
  it("opens an utterance at 0.02, which the old fixed 0.045 never could", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 3000, 0.0009); // a quiet room
    const events = feed(vad, ring, 500, 0.02);
    expect(events).toContain("open");
  });

  it("does not end an utterance on a 1.1s pause mid-sentence", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    expect(feed(vad, ring, 600, 0.05)).toContain("open");
    // 1100ms is less than the 1500ms hangover: still one utterance.
    expect(feed(vad, ring, 1100, 0.0005)).not.toContain("close");
    expect(vad.open).toBe(true);
    expect(feed(vad, ring, 600, 0.05)).toEqual([]);
    expect(feed(vad, ring, HANGOVER_MS + 100, 0.0005)).toContain("close");
  });

  it("lifts the threshold on sustained background talk instead of wedging open", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    const before = vad.thresh;
    feed(vad, ring, 20000, 0.05); // 20s of background conversation
    expect(vad.thresh).toBeGreaterThan(before * 3);
    // Whatever opened has been closed by the cap or the hangover, not left
    // running to the recording limit.
    feed(vad, ring, HANGOVER_MS + 200, 0.0005);
    expect(vad.open).toBe(false);
  });

  it("rises slower while an utterance is open than while idle", () => {
    // Compared one frame at a time from the same starting floor, because in a
    // whole-utterance comparison both detectors open and there is nothing left
    // to contrast. The owner's own voice must not walk the threshold up
    // underneath itself and cut the sentence off.
    const step = (open: boolean) => {
      const v = new Vad();
      v.floor = 0.01;
      v.open = open;
      v.openedAt = 0;
      v.feed(0.08, 1600, 1_000_000);
      return v.floor;
    };
    expect(step(true)).toBeLessThan(step(false));
    expect(step(true)).toBeGreaterThan(0.01); // it still rises, just slower
  });

  it("measures the time constants in seconds, not in frames", () => {
    // The same wall-clock audio at two frame sizes must land in the same place.
    const run = (frame: number) => {
      const vad = new Vad();
      const ring = new Ring();
      feed(vad, ring, 4000, 0.04, frame);
      return vad.thresh;
    };
    expect(run(256)).toBeCloseTo(run(4096), 3);
  });
});

describe("what gets sent", () => {
  it("back-dates the start by the pre-roll", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    const at = ring.written;
    feed(vad, ring, 200, 0.05);
    expect(vad.open).toBe(true);
    // The utterance starts before the detector agreed it was speech.
    expect(vad.openedAt).toBeLessThanOrEqual(at - msToSamples(PREROLL_MS) + msToSamples(200));
    expect(at - vad.openedAt).toBeGreaterThanOrEqual(msToSamples(PREROLL_MS));
  });

  it("trims the hangover off the end", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    feed(vad, ring, 800, 0.05);
    feed(vad, ring, HANGOVER_MS + 200, 0.0005);
    const { to } = closeBounds(vad, ring.written, "close");
    // The silence that proved the utterance ended is not shipped: what is sent
    // stops ~200ms past the last speech, not a full hangover later.
    expect(ring.written - to).toBeGreaterThan(msToSamples(1000));
  });

  it("measures the minimum length on the speech, not on the padded segment", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    feed(vad, ring, 90, 0.05); // a cough
    feed(vad, ring, HANGOVER_MS + 200, 0.0005);
    // The segment is padded at both ends and would clear 350ms on padding
    // alone; `voiced` counts only frames above the threshold.
    expect(samplesToMs(vad.voiced)).toBeLessThan(MIN_UTTER_MS);
    expect(longEnough(vad)).toBe(false);
  });

  it("accepts a real sentence", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    feed(vad, ring, 1400, 0.06);
    feed(vad, ring, HANGOVER_MS + 200, 0.0005);
    expect(longEnough(vad)).toBe(true);
  });

  it("caps a runaway utterance rather than recording forever", () => {
    const vad = new Vad();
    const ring = new Ring();
    feed(vad, ring, 2000, 0.001);
    feed(vad, ring, 300, 0.05);
    expect(vad.open).toBe(true);
    // The cap is a hard stop, never a normal ending: in a real room the
    // rising floor closes an over-long utterance first, so it is exercised by
    // putting the open mark 30s back rather than by 30s of synthetic speech.
    vad.openedAt = ring.written - msToSamples(30_100);
    // One frame, because the detector legitimately re-opens on the very next
    // one: the cap ends *this* utterance, it does not stop listening.
    const buf = new Float32Array(1024).fill(0.05);
    ring.push(buf);
    expect(vad.feed(frameRms(buf), buf.length, ring.written)).toBe("cap");
    expect(vad.open).toBe(false);
  });
});

describe("wav payload", () => {
  it("is a RIFF WAV the transcriber can take as-is", () => {
    const bytes = new Uint8Array(wavBytes(new Float32Array(1600).fill(0.5)));
    // Consumers sniff `RIFF` rather than trusting a label — v1's rule, kept.
    expect(String.fromCharCode(...bytes.slice(0, 4))).toBe("RIFF");
    expect(String.fromCharCode(...bytes.slice(8, 12))).toBe("WAVE");
    expect(bytes.length).toBe(44 + 1600 * 2);
    const view = new DataView(bytes.buffer);
    expect(view.getUint32(24, true)).toBe(CAP_RATE); // parakeet's own rate
    expect(view.getUint16(34, true)).toBe(16); // 16-bit PCM
  });

  it("wraps the same bytes in a blob", () => {
    expect(wavBlob(new Float32Array(160)).size).toBe(44 + 160 * 2);
  });
});
