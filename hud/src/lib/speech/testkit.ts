// Fakes for the speech tests: a clock that only moves when told to, a player
// whose "audio" lasts as long as the text is long, and a synthesizer with a
// settable latency. Nothing here makes a sound or touches the network.

import type { Clock, PlayHandle, Player, SpeechItem } from "./queue";

export class FakeClock implements Clock {
  t = 0;
  private timers: { at: number; fn: () => void; id: number }[] = [];
  private next = 1;

  now() { return this.t; }

  setTimeout(fn: () => void, ms: number) {
    const id = this.next++;
    this.timers.push({ at: this.t + ms, fn, id });
    return id;
  }

  clearTimeout(handle: unknown) {
    this.timers = this.timers.filter((x) => x.id !== handle);
  }

  /** Let settled promises run their callbacks. */
  async flush() {
    for (let i = 0; i < 12; i++) await Promise.resolve();
  }

  /** Move time forward, running every timer that falls due, in order, with promises settled between. */
  async advance(ms: number) {
    const end = this.t + ms;
    await this.flush();
    for (;;) {
      const due = this.timers.filter((x) => x.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      this.timers = this.timers.filter((x) => x !== due);
      this.t = due.at;
      due.fn();
      await this.flush();
    }
    this.t = end;
    await this.flush();
  }

  pendingTimers() { return this.timers.length; }
}

export interface FakeAudio { text: string; ms: number }

/** Audio lasts `MS_PER_CHAR` per character: silence proportional to text. */
export const MS_PER_CHAR = 50;

export class FakePlayer implements Player {
  /** Everything started, in order. */
  started: string[] = [];
  /** How many sources are sounding right now: never more than one. */
  active = 0;
  maxActive = 0;
  isBlocked = false;
  private lastLevel = 0.5;

  constructor(private clock: FakeClock) {}

  /** The fake `/say` answers the text itself as bytes; "silence proportional to text". */
  async decode(bytes: ArrayBuffer) {
    const text = new TextDecoder().decode(bytes);
    return { text, ms: Math.max(50, text.length * MS_PER_CHAR) } as FakeAudio as unknown;
  }

  play(audio: unknown): PlayHandle {
    const a = audio as FakeAudio;
    this.started.push(a.text);
    this.active += 1;
    this.maxActive = Math.max(this.maxActive, this.active);
    let finish!: (how: "ended" | "stopped") => void;
    let over = false;
    const done = new Promise<"ended" | "stopped">((resolve) => { finish = resolve; });
    const end = (how: "ended" | "stopped") => {
      if (over) return;
      over = true;
      this.active -= 1;
      this.clock.clearTimeout(timer);
      finish(how);
    };
    const timer = this.clock.setTimeout(() => end("ended"), this.stuck ? 1e12 : a.ms);
    return { done, stop: () => end("stopped") };
  }

  level() { return this.lastLevel; }
  blocked() { return this.isBlocked; }
  /** `stuck`: a source that never ends by itself (a wedged audio device). */
  stuck = false;
  duration(audio: unknown) { return (audio as FakeAudio).ms; }
}

export class FakeSynth {
  calls: string[] = [];
  latency = 100;
  failing: (item: SpeechItem) => boolean = () => false;
  hang: (item: SpeechItem) => boolean = () => false;
  aborted: string[] = [];

  constructor(private clock: FakeClock) {}

  fn = (item: SpeechItem, signal: AbortSignal): Promise<FakeAudio> => {
    this.calls.push(item.text);
    return new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => { this.aborted.push(item.text); reject(new Error("aborted")); });
      if (this.hang(item)) return;
      this.clock.setTimeout(() => {
        if (this.failing(item)) reject(new Error("tts down"));
        else resolve({ text: item.text, ms: Math.max(50, item.text.length * MS_PER_CHAR) });
      }, this.latency);
    });
  };
}

/** `/say` as bytes: the text itself, after `latency` fake ms; `fail` rejects. */
export class FakeSay {
  calls: { text: string; voice: string | null }[] = [];
  latency = 100;
  fail: (text: string) => boolean = () => false;

  constructor(private clock: FakeClock) {}

  fn = (text: string, opts: { voice: string | null; signal: AbortSignal }): Promise<ArrayBuffer> => {
    this.calls.push({ text, voice: opts.voice });
    return new Promise((resolve, reject) => {
      opts.signal.addEventListener("abort", () => reject(new Error("aborted")));
      this.clock.setTimeout(() => {
        if (this.fail(text)) reject(new Error("503"));
        else resolve(new TextEncoder().encode(text).buffer as ArrayBuffer);
      }, this.latency);
    });
  };
}

export class FakeGate {
  speaker = true;
  private cbs = new Set<() => void>();
  isSpeaker() { return this.speaker; }
  subscribe(cb: () => void) { this.cbs.add(cb); return () => { this.cbs.delete(cb); }; }
  set(speaker: boolean) { this.speaker = speaker; this.cbs.forEach((f) => f()); }
}
