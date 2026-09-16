// The capture pipeline's arithmetic, ported from jarvis.html unchanged in
// behaviour (CLAUDE.md, "Voice round 2"). Everything is measured in samples
// rather than wall-clock milliseconds: the audio thread is the only clock that
// cannot drift against the buffer actually being sent.
//
// Kept as plain classes fed one frame at a time so the tests can drive them
// with a synthetic level sequence — no microphone, no timing luck, and the
// thing under test is the same code the mic runs.

export const CAP_RATE = 16000; // parakeet's own rate — resampling here costs nothing
export const RING_SEC = 40;
export const PREROLL_MS = 800; // rewound from before speech was detected
export const ONSET_MS = 60; // sustained speech this long opens an utterance
export const HANGOVER_MS = 1500; // silence this long closes one
export const MIN_UTTER_MS = 350; // shorter is a cough, not a turn
export const MAX_UTTER_MS = 30000; // a hard stop, never a normal ending
export const FOLLOWUP_MS = 9000; // speak again after he answers, no wake word
export const WAKE_GRACE_MS = 1200; // recognizer lag: a wake hit claims a slightly older utterance

// How fast the floor chases the room, in seconds rather than in frames. Frames
// are whatever the audio thread hands over, so a per-frame coefficient silently
// means a different amount of time on a different buffer size — and a test
// feeding one large frame would measure something the microphone never does.
export const FLOOR_FALL_TC = 0.2; // a room going quiet is tracked within a breath
export const FLOOR_RISE_IDLE_TC = 8; // sustained background noise has to be sustained
export const FLOOR_RISE_OPEN_TC = 25;

export const msToSamples = (ms: number) => Math.round((ms * CAP_RATE) / 1000);
export const samplesToMs = (n: number) => (n * 1000) / CAP_RATE;

/** The always-open mic's ring buffer. `written` is the clock. */
export class Ring {
  buf: Float32Array;
  written = 0;

  constructor(seconds = RING_SEC) {
    this.buf = new Float32Array(CAP_RATE * seconds);
  }

  push(frame: Float32Array) {
    const len = this.buf.length;
    for (let i = 0; i < frame.length; i++) this.buf[(this.written + i) % len] = frame[i];
    this.written += frame.length;
  }

  // Anything older than the ring has been overwritten; asking for it yields
  // whatever is still there rather than silence or an error.
  slice(from: number, to: number): Float32Array {
    const first = Math.max(from, this.written - this.buf.length, 0);
    const last = Math.min(to, this.written);
    const out = new Float32Array(Math.max(0, last - first));
    for (let i = 0; i < out.length; i++) out[i] = this.buf[(first + i) % this.buf.length];
    return out;
  }
}

export type VadEvent = "open" | "close" | "cap" | null;

export class Vad {
  floor = 0.02;
  thresh = 0.02;
  rms = 0;
  run = 0;
  quiet = 0;
  voiced = 0;
  open = false;
  openedAt = 0;
  peak = 0;

  reset() {
    this.floor = 0.02;
    this.thresh = 0.02;
    this.rms = 0;
    this.run = 0;
    this.quiet = 0;
    this.voiced = 0;
    this.open = false;
    this.openedAt = 0;
    this.peak = 0;
  }

  /**
   * @param rms      this frame's level
   * @param n        the frame length in samples — what turns the ratios into times
   * @param written  the ring's sample clock
   */
  feed(rms: number, n: number, written: number): VadEvent {
    this.rms = rms;
    // The floor falls fast and rises slowly, so a room that goes quiet is
    // tracked within a breath while a passing conversation has to be sustained
    // to raise it. While an utterance is open it rises slower still: the
    // owner's own voice must not walk the threshold up underneath itself and
    // cut the sentence off, which is exactly the "recording gets cut" report.
    const tc = rms < this.floor ? FLOOR_FALL_TC : this.open ? FLOOR_RISE_OPEN_TC : FLOOR_RISE_IDLE_TC;
    this.floor += (rms - this.floor) * (1 - Math.exp(-(n / CAP_RATE) / tc));
    this.floor = Math.max(this.floor, 0.0008);
    this.thresh = Math.max(this.floor * 3.0, 0.006);

    const loud = rms > this.thresh;
    if (loud) {
      this.run += n;
      this.quiet = 0;
    } else {
      this.quiet += n;
      this.run = 0;
    }
    // How much of this utterance was actually speech. The segment that gets
    // sent is padded at both ends, so measuring *it* against a minimum length
    // would let a 90ms cough clear the bar on padding alone.
    if (this.open && loud) this.voiced += n;

    if (!this.open) {
      if (this.run >= msToSamples(ONSET_MS)) {
        this.open = true;
        this.peak = rms;
        this.voiced = this.run;
        // Back-date the start: the onset was confirmed only after ONSET_MS of
        // speech had already happened, and the pre-roll reaches further back
        // still, to whatever was said before the detector agreed it was speech.
        this.openedAt =
          written - Math.min(this.run, msToSamples(ONSET_MS * 4)) - msToSamples(PREROLL_MS);
        return "open";
      }
      return null;
    }

    this.peak = Math.max(this.peak, rms);
    if (written - this.openedAt >= msToSamples(MAX_UTTER_MS)) {
      this.open = false;
      return "cap";
    }
    if (this.quiet >= msToSamples(HANGOVER_MS)) {
      this.open = false;
      return "close";
    }
    return null;
  }
}

/**
 * Where a closing utterance ends. The utterance ended a full hangover ago;
 * everything since is the silence that proved it ended, so it is trimmed back
 * to a natural tail rather than shipped as a second of nothing.
 */
export function closeBounds(vad: Vad, written: number, event: "close" | "cap") {
  const from = vad.openedAt;
  const to = event === "cap" ? written : Math.max(from, written - vad.quiet + msToSamples(200));
  return { from, to };
}

/**
 * A claimed utterance that never rose meaningfully clear of the room is
 * background noise that happened to land in the follow-up window, not something
 * said to him.
 */
export function longEnough(vad: Vad): boolean {
  return samplesToMs(vad.voiced) >= MIN_UTTER_MS && vad.peak >= vad.thresh * 1.6;
}

/** RMS of one frame — the same number the orb's level meter reads. */
export function frameRms(frame: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  return Math.sqrt(sum / Math.max(1, frame.length));
}

/** A 16-bit PCM WAV the HUD builds itself, rather than a webm MediaRecorder owns. */
export function wavBytes(samples: Float32Array, rate = CAP_RATE): ArrayBuffer {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  const ascii = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return bytes;
}

export function wavBlob(samples: Float32Array, rate = CAP_RATE): Blob {
  return new Blob([wavBytes(samples, rate)], { type: "audio/wav" });
}
