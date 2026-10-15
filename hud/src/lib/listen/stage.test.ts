import { describe, expect, it } from "vitest";
import {
  FRAME_DEADLINE_MS, HEALTHY_RESET_MS, HEAD_WAIT_MS, LATE_RUN, LOAD_DEADLINE_MS, MAX_AUTO_RETRIES, MAX_PENDING, RETRY_DELAYS_MS,
  SLEEP_GAP_MS, STRAIN_MS, VadStage, type DetectorStatus, type WorkerLike,
} from "./stage";
import type { FromWorker, ToWorker } from "./protocol";

class FakeWorker implements WorkerLike {
  onmessage: ((e: MessageEvent<FromWorker>) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onmessageerror: ((e: unknown) => void) | null = null;
  posted: ToWorker[] = [];
  terminated = false;
  postMessage(m: ToWorker) { this.posted.push(m); }
  terminate() { this.terminated = true; }
  say(m: FromWorker) { this.onmessage?.({ data: m } as MessageEvent<FromWorker>); }
  frames() { return this.posted.filter((m): m is Extract<ToWorker, { type: "frame" }> => m.type === "frame"); }
}

const frame = (v: number) => new Float32Array(512).fill(v);

function rig(opts: { active?: () => boolean; canRetry?: () => boolean; denoise?: (f: Float32Array) => Float32Array; throwOnCreate?: boolean; noise?: boolean } = {}) {
  let clock = 1000;
  const out: { v: number; p: number | null }[] = [];
  const statuses: DetectorStatus[] = [];
  const workers: FakeWorker[] = [];
  const stage = new VadStage({
    deliver: (f, p) => out.push({ v: f[0], p }),
    createWorker: () => {
      if (opts.throwOnCreate) throw new Error("blocked by CSP at http://x");
      const w = new FakeWorker(); workers.push(w); return w;
    },
    now: () => clock,
    active: opts.active,
    canRetry: opts.canRetry,
    denoise: opts.denoise,
    noiseSuppression: opts.noise,
    onStatus: (s) => statuses.push(s),
  });
  return { stage, out, statuses, workers, w: () => workers[workers.length - 1], tick: (ms: number) => (clock += ms) };
}

const ready = (r: ReturnType<typeof rig>) => { r.stage.start(); r.w().say({ type: "ready", loadMs: 400 }); };

describe("energy until the worker is ready", () => {
  it("passes frames on at once, judged by energy (p null), while loading", () => {
    const r = rig();
    r.stage.start();
    r.stage.push(frame(1)); r.stage.push(frame(2));
    expect(r.out).toEqual([{ v: 1, p: null }, { v: 2, p: null }]);
    expect(r.stage.status()).toMatchObject({ detector: "energy", state: "loading", code: null });
    expect(r.stage.status().note).toMatch(/loading/);
  });

  it("switches to Silero when the worker says ready, and says so", () => {
    const r = rig();
    ready(r);
    expect(r.stage.status()).toMatchObject({ detector: "silero", state: "ready", loadMs: 400 });
    expect(r.statuses.map((s) => s.detector)).toEqual(["energy", "silero"]);
  });

  it("never started: the capture still gets every frame", () => {
    const r = rig();
    r.stage.push(frame(1));
    expect(r.out).toEqual([{ v: 1, p: null }]);
    expect(r.stage.status().state).toBe("idle");
  });
});

describe("with the worker ready", () => {
  it("holds a frame until its probability returns, then delivers it with p, in order", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1)); r.stage.push(frame(2)); r.stage.push(frame(3));
    expect(r.out).toEqual([]); // nothing yet: the verdicts are out
    const [a, b, c] = r.w().frames();
    r.w().say({ type: "prob", seq: b.seq, p: 0.9, ms: 1 }); // out of order: the second answers first
    expect(r.out).toEqual([]); // the first is still in front of it
    r.w().say({ type: "prob", seq: a.seq, p: 0.1, ms: 1 });
    expect(r.out).toEqual([{ v: 1, p: 0.1 }, { v: 2, p: 0.9 }]);
    r.w().say({ type: "prob", seq: c.seq, p: 0.5, ms: 1 });
    expect(r.out.map((x) => x.v)).toEqual([1, 2, 3]);
  });

  it("posts a copy: the capture's frame is never detached or changed", () => {
    const r = rig();
    ready(r);
    const mine = frame(7);
    r.stage.push(mine);
    expect(mine.length).toBe(512); // still whole: only the copy was transferred
    const sent = r.w().frames()[0].samples;
    expect(sent).not.toBe(mine);
    expect(sent[0]).toBe(7);
    r.w().say({ type: "prob", seq: 1, p: 0.2, ms: 1 });
    expect(r.out[0].v).toBe(7);
  });

  it("the denoise seam changes only what the VAD scores, never what is recorded", () => {
    const r = rig({ denoise: (f) => f.map((x) => x * 0) });
    ready(r);
    r.stage.push(frame(5));
    expect(r.w().frames()[0].samples[0]).toBe(0);
    r.w().say({ type: "prob", seq: 1, p: 0, ms: 1 });
    expect(r.out[0].v).toBe(5);
  });

  it("reports the mean time per frame the worker took", () => {
    const r = rig();
    ready(r);
    for (let i = 1; i <= 4; i++) { r.stage.push(frame(i)); r.w().say({ type: "prob", seq: i, p: 0.1, ms: i }); }
    expect(r.stage.status().frameMs).toBeCloseTo(2.5);
    expect(r.stage.status().scored).toBe(4);
  });

  it("an unknown or repeated verdict is ignored", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1));
    r.w().say({ type: "prob", seq: 99, p: 1, ms: 1 });
    r.w().say({ type: "prob", seq: 1, p: 0.3, ms: 1 });
    r.w().say({ type: "prob", seq: 1, p: 0.9, ms: 1 });
    expect(r.out).toEqual([{ v: 1, p: 0.3 }]);
  });

  it("a non-finite probability is energy for that frame, not a guess", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1));
    r.w().say({ type: "prob", seq: 1, p: NaN, ms: 1 });
    expect(r.out).toEqual([{ v: 1, p: null }]);
  });
});

describe("falls back to energy, visibly, and never drops a frame", () => {
  it("no Worker in this window", () => {
    const r = rig({ throwOnCreate: true });
    r.stage.start();
    expect(r.stage.status()).toMatchObject({ detector: "energy", state: "failed", code: "worker" });
    expect(r.stage.status().note).not.toMatch(/http|CSP/); // the error text never reaches the line
    r.stage.push(frame(1));
    expect(r.out).toEqual([{ v: 1, p: null }]);
  });

  it("the worker could not load (hash, fetch, runtime): the code is the reason", () => {
    for (const code of ["hash", "fetch", "runtime"] as const) {
      const r = rig();
      r.stage.start();
      r.w().say({ type: "failed", code });
      expect(r.stage.status()).toMatchObject({ detector: "energy", state: "failed", code });
      expect(r.w().terminated).toBe(true);
      expect(r.statuses.at(-1)!.note.length).toBeGreaterThan(10);
    }
    const r = rig();
    ready(r);
    r.w().say({ type: "failed", code: "hash" });
    expect(r.stage.status().note).toMatch(/checksum/);
  });

  it("an unknown failure code is shown as a runtime failure", () => {
    const r = rig();
    r.stage.start();
    r.w().say({ type: "failed", code: "<script>" as never });
    expect(r.stage.status().code).toBe("runtime");
  });

  it("the worker script failing to load (an error event)", () => {
    const r = rig();
    r.stage.start();
    r.w().onerror?.({});
    expect(r.stage.status()).toMatchObject({ detector: "energy", code: "worker" });
  });

  it("loading takes too long", () => {
    const r = rig();
    r.stage.start();
    r.tick(LOAD_DEADLINE_MS + 1);
    r.stage.push(frame(1));
    expect(r.stage.status()).toMatchObject({ state: "failed", code: "timeout" });
    expect(r.out).toEqual([{ v: 1, p: null }]);
  });

  it("a slow but timely worker does not fall back", () => {
    const r = rig();
    ready(r);
    for (let i = 1; i <= 200; i++) {
      r.stage.push(frame(i)); r.tick(32);
      r.w().say({ type: "prob", seq: i, p: 0.5, ms: 20 });
    }
    expect(r.stage.status().state).toBe("ready");
    expect(r.out).toHaveLength(200);
  });

  it("a failure is sticky until retry(), and retry starts a fresh worker", () => {
    const r = rig();
    ready(r);
    r.w().say({ type: "failed", code: "runtime" });
    r.stage.push(frame(1));
    expect(r.workers).toHaveLength(1);
    r.stage.retry();
    expect(r.workers).toHaveLength(2);
    expect(r.stage.status().state).toBe("loading");
    r.w().say({ type: "ready", loadMs: 1 });
    expect(r.stage.status().detector).toBe("silero");
    r.stage.retry(); // ready: nothing to do
    expect(r.workers).toHaveLength(2);
  });

  it("every frame in comes out exactly once and in order, whatever the worker does", () => {
    let seed = 7;
    const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    for (let trial = 0; trial < 30; trial++) {
      const r = rig();
      r.stage.start();
      let n = 0;
      for (let step = 0; step < 120; step++) {
        const x = rnd();
        if (x < 0.55) r.stage.push(frame(++n));
        else if (x < 0.8) {
          const w = r.w();
          const fs = w.frames().filter((f) => f.seq > 0);
          if (fs.length) w.say({ type: "prob", seq: fs[Math.floor(rnd() * fs.length)].seq, p: rnd(), ms: 1 });
        } else if (x < 0.85) r.w().say({ type: "ready", loadMs: 1 });
        else if (x < 0.87) r.w().say({ type: "failed", code: "runtime" });
        else if (x < 0.9) r.tick(FRAME_DEADLINE_MS + 5);
        else if (x < 0.92) r.stage.retry();
      }
      // Whatever is still held is released only by a failure: make one, then check.
      r.w().say({ type: "failed", code: "backlog" });
      expect(r.out.map((o) => o.v)).toEqual(Array.from({ length: n }, (_, i) => i + 1));
    }
  });
});

/** Push `n` frames 32 ms apart (the microphone's own rhythm), answering each at once unless told not to. */
function stream(r: ReturnType<typeof rig>, n: number, answer = true, start = 1000) {
  for (let i = 0; i < n; i++) {
    r.tick(32);
    r.stage.push(frame(start + i));
    if (answer) { const f = r.w().frames().at(-1)!; r.w().say({ type: "prob", seq: f.seq, p: 0.5, ms: 1 + (i % 5) * 0.2, total: 1 }); }
  }
}

/** Let `ms` go by the way real time does: frames keep arriving (a second apart is plenty), so no gap reads as a sleep. */
function wait(r: ReturnType<typeof rig>, ms: number) {
  for (let left = ms; left > 0; left -= 1000) { r.tick(Math.min(1000, left)); r.stage.push(frame(7777)); }
}

describe("a worker that is genuinely too slow", () => {
  it("the queue is bounded: past MAX_PENDING frames are delivered as energy, in order, without failing", () => {
    const r = rig();
    ready(r);
    for (let i = 1; i <= MAX_PENDING + 3; i++) r.stage.push(frame(i));
    expect(r.stage.status().state).toBe("ready");
    expect(r.w().frames()).toHaveLength(MAX_PENDING); // the 3 beyond the bound were not posted
    expect(r.out).toEqual([]);                        // and wait behind the unanswered head
    for (const f of r.w().frames()) r.w().say({ type: "prob", seq: f.seq, p: 0.7, ms: 1, total: 1 });
    expect(r.out.map((o) => o.v)).toEqual(Array.from({ length: MAX_PENDING + 3 }, (_, i) => i + 1));
    expect(r.out.slice(0, MAX_PENDING).every((o) => o.p === 0.7)).toBe(true);
    expect(r.out.slice(MAX_PENDING).every((o) => o.p === null)).toBe(true);
    r.stage.push(frame(99)); // the worker missed audio: it starts clean at the next frame it is given
    expect(r.w().posted.map((m) => m.type).slice(-2)).toEqual(["reset", "frame"]);
  });

  it("a verdict that never comes: the oldest frame's wait, counted over responsive time, fails it", () => {
    const r = rig();
    ready(r);
    let n = 0;
    while (r.stage.status().state === "ready" && n < 100) { r.tick(32); r.stage.push(frame(++n)); }
    expect(r.stage.status()).toMatchObject({ detector: "energy", code: "late" });
    expect(n * 32).toBeGreaterThan(HEAD_WAIT_MS);
    expect(n * 32).toBeLessThan(HEAD_WAIT_MS + 200);
    expect(r.out.map((o) => o.v)).toEqual(Array.from({ length: n }, (_, i) => i + 1));
    expect(r.out.every((o) => o.p === null)).toBe(true);
    expect(r.w().terminated).toBe(true);
  });

  it("verdicts that keep taking too long in the worker itself: LATE_RUN in a row fail it, fewer do not", () => {
    const r = rig();
    ready(r);
    for (let i = 1; i < LATE_RUN; i++) { r.tick(32); r.stage.push(frame(i)); r.w().say({ type: "prob", seq: i, p: 0.5, ms: 5, total: FRAME_DEADLINE_MS + 1 }); }
    expect(r.stage.status().state).toBe("ready");
    r.tick(32); r.stage.push(frame(50)); r.w().say({ type: "prob", seq: LATE_RUN, p: 0.5, ms: 5, total: 3 }); // one good one resets the run
    for (let i = 1; i <= LATE_RUN; i++) { r.tick(32); r.stage.push(frame(60 + i)); r.w().say({ type: "prob", seq: LATE_RUN + i, p: 0.5, ms: 5, total: FRAME_DEADLINE_MS + 1 }); }
    expect(r.stage.status()).toMatchObject({ state: "failed", code: "late" });
  });

  it("answering on time but never catching up: a full queue for STRAIN_MS fails it as a backlog", () => {
    const r = rig();
    ready(r);
    // The worker answers the oldest posted frame every 20th push: the head keeps changing
    // (never 750 ms old) but the queue stays full.
    let n = 0, answered = 0;
    while (r.stage.status().state === "ready" && n < 400) {
      r.tick(32); r.stage.push(frame(++n));
      if (n % 20 === 0) {
        answered = r.w().frames().map((x) => x.seq).find((q) => q > answered) ?? answered;
        r.w().say({ type: "prob", seq: answered, p: 0.5, ms: 1, total: 1 });
      }
    }
    expect(r.stage.status()).toMatchObject({ state: "failed", code: "backlog" });
    expect(n * 32).toBeGreaterThan(STRAIN_MS);
  });
});

describe("a busy main thread is not a slow worker (MED-1)", () => {
  for (const stall of [700, 1500, 5000]) {
    it(`a ${stall} ms stall: the frames and the verdicts arrive in a burst, Silero stays`, () => {
      const r = rig();
      ready(r);
      stream(r, 20);                                  // healthy
      r.tick(32); r.stage.push(frame(4000));          // a frame goes out, its verdict is on the way...
      r.tick(32); r.stage.push(frame(4001));
      r.tick(stall);                                  // ...and the main thread is blocked
      const burst = Math.floor(stall / 32) + 1;       // the microphone's frames arrive together
      for (let i = 0; i < burst; i++) r.stage.push(frame(5000 + i));
      for (const f of r.w().frames()) if (f.seq >= 21) r.w().say({ type: "prob", seq: f.seq, p: 0.8, ms: 1, total: 1 });
      expect(r.stage.status().state).toBe("ready");
      expect(r.out.map((o) => o.v).slice(-burst)).toEqual(Array.from({ length: burst }, (_, i) => 5000 + i));
      stream(r, 10, true, 9000);                      // and carries on scoring afterwards
      expect(r.out.slice(-10).every((o) => o.p === 0.5)).toBe(true);
    });
  }

  it("a stall does not age the oldest frame: the worker answered at once, the main thread read it late", () => {
    const r = rig();
    ready(r);
    r.tick(32); r.stage.push(frame(1));
    r.tick(32); r.stage.push(frame(2));               // the oldest frame is being watched
    r.tick(3000);                                     // blocked before the verdict could be read
    r.stage.push(frame(3));
    r.w().say({ type: "prob", seq: 1, p: 0.6, ms: 1, total: 1 });
    r.w().say({ type: "prob", seq: 2, p: 0.6, ms: 1, total: 1 });
    r.w().say({ type: "prob", seq: 3, p: 0.6, ms: 1, total: 1 });
    expect(r.stage.status().state).toBe("ready");
    expect(r.out.map((o) => o.p)).toEqual([0.6, 0.6, 0.6]);
  });

  it("repeated stalls with a worker that answers each time do not add up to a failure", () => {
    const r = rig();
    ready(r);
    for (let k = 0; k < 10; k++) { stream(r, 5); r.tick(900); stream(r, 1); }
    expect(r.stage.status().state).toBe("ready");
  });
});

describe("a long gap between frames is a reset, not a failure (sleep and resume)", () => {
  it("working: forgets the timing history and starts the worker clean", () => {
    const r = rig();
    ready(r);
    stream(r, 5);
    r.tick(SLEEP_GAP_MS + 10);
    r.stage.push(frame(1));
    expect(r.stage.status().state).toBe("ready");
    expect(r.w().posted.map((m) => m.type).slice(-2)).toEqual(["reset", "frame"]);
  });

  it("failed for a timing reason: tries again at once, without spending an automatic retry", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(0));
    r.w().say({ type: "failed", code: "worker" as never });
    expect(r.stage.status().state).toBe("failed");
    r.tick(SLEEP_GAP_MS + 10);
    r.stage.push(frame(1));
    expect(r.workers).toHaveLength(2);
    expect(r.stage.status().state).toBe("loading");
    r.w().say({ type: "failed", code: "worker" as never });
    wait(r, RETRY_DELAYS_MS[0] + 1); // the first automatic retry is still whole
    expect(r.workers).toHaveLength(3);
  });

  it("rearm() (the page is visible again) does the same, and never revives a permanent failure", () => {
    const r = rig();
    r.stage.start(); r.w().say({ type: "failed", code: "hash" });
    r.stage.rearm();
    expect(r.workers).toHaveLength(1);
    const q = rig();
    ready(q);
    q.w().say({ type: "failed", code: "backlog" });
    q.stage.rearm();
    expect(q.workers).toHaveLength(2);
  });
});

describe("automatic retries", () => {
  const failWith = (r: ReturnType<typeof rig>, code: string) => { r.w().say({ type: "failed", code: code as never }); };

  it("transient failures retry after 30 s, 2 min, 10 min, then stop; the owner's button still works", () => {
    expect([...RETRY_DELAYS_MS]).toEqual([30_000, 120_000, 600_000]);
    expect(MAX_AUTO_RETRIES).toBe(3);
    const r = rig();
    ready(r);
    failWith(r, "late");
    expect(r.stage.status().retrying).toBe(true);
    expect(r.stage.status().note).toMatch(/trying again/);
    wait(r, RETRY_DELAYS_MS[0] - 1000);
    expect(r.workers).toHaveLength(1);                 // too early
    wait(r, 1100);
    expect(r.workers).toHaveLength(2);                 // 30 s
    failWith(r, "backlog");
    wait(r, RETRY_DELAYS_MS[1] - 1000);
    expect(r.workers).toHaveLength(2);
    wait(r, 1100);
    expect(r.workers).toHaveLength(3);                 // 2 min
    failWith(r, "timeout");
    wait(r, RETRY_DELAYS_MS[2] + 1);
    expect(r.workers).toHaveLength(4);                 // 10 min
    failWith(r, "worker");
    expect(r.stage.status().retrying).toBe(false);
    wait(r, 3_600_000);
    expect(r.workers).toHaveLength(4);                 // no fourth
    r.stage.retry();
    expect(r.workers).toHaveLength(5);                 // the button always tries
  });

  it("only at a quiet moment: nothing open or recording, the mic not OFF, Settings closed", () => {
    let quiet = false, on = true;
    const r = rig({ canRetry: () => quiet, active: () => on });
    ready(r);
    failWith(r, "late");
    wait(r, RETRY_DELAYS_MS[0] + 1);
    expect(r.workers).toHaveLength(1);                 // an utterance is open / Settings is up
    quiet = true; on = false;
    r.stage.push(frame(2));
    expect(r.workers).toHaveLength(1);                 // OFF
    on = true;
    r.stage.push(frame(3));
    expect(r.workers).toHaveLength(2);
  });

  it("never for hash, no_worker or runtime; fetch gets one more try when the mic is next opened", () => {
    for (const code of ["hash", "runtime"]) {
      const r = rig();
      r.stage.start(); failWith(r, code);
      wait(r, 3_600_000);
      expect(r.workers, code).toHaveLength(1);
      expect(r.stage.status().retrying).toBe(false);
      r.stage.stop();
      r.stage.start();                                   // the next mic open: still no
      expect(r.workers, code).toHaveLength(1);
      expect(r.stage.status()).toMatchObject({ state: "failed", code });
      r.stage.retry();                                   // the button
      expect(r.workers, code).toHaveLength(2);
    }
    const n = rig({ throwOnCreate: true });
    n.stage.start();
    wait(n, 3_600_000);
    expect(n.stage.status().code).toBe("worker");        // (a throwing constructor is "worker"; "no_worker" below)
    const f = rig();
    f.stage.start(); failWith(f, "fetch");
    wait(f, 3_600_000);
    expect(f.workers).toHaveLength(1);                   // not on its own
    f.stage.stop(); f.stage.start();
    expect(f.workers).toHaveLength(2);                   // at the next mic open
  });

  it("the count resets after HEALTHY_RESET_MS of Silero working", () => {
    const r = rig();
    ready(r);
    failWith(r, "late");
    wait(r, RETRY_DELAYS_MS[0] + 1);   // retry 1
    r.w().say({ type: "ready", loadMs: 1 });
    wait(r, HEALTHY_RESET_MS + 1000); stream(r, 2);             // five healthy minutes
    failWith(r, "late");
    wait(r, RETRY_DELAYS_MS[0] + 1);
    expect(r.workers).toHaveLength(3);                          // the first delay again, not the 2 min one
  });

  it("every switch is reported to the observer (diagnostics log them)", () => {
    const r = rig();
    ready(r);
    failWith(r, "late");
    wait(r, RETRY_DELAYS_MS[0] + 1);
    r.w().say({ type: "ready", loadMs: 1 });
    expect(r.statuses.map((s) => `${s.detector}:${s.state}`)).toEqual([
      "energy:loading", "silero:ready", "energy:failed", "energy:loading", "silero:ready",
    ]);
  });
});

describe("the other details", () => {
  it("statistics are republished every so often, not at every verdict", () => {
    const r = rig();
    ready(r);
    const before = r.statuses.length;
    stream(r, 200);
    const after = r.statuses.length - before;
    expect(after).toBeGreaterThanOrEqual(3);
    expect(after).toBeLessThanOrEqual(5);
    expect(r.statuses.at(-1)!.frameMs).toBeCloseTo(1.4, 0);
  });

  it("an object-prototype name is not a failure code", () => {
    for (const code of ["toString", "constructor", "__proto__"]) {
      const r = rig();
      r.stage.start();
      r.w().say({ type: "failed", code: code as never });
      expect(r.stage.status().code).toBe("runtime");
    }
  });

  it("a stale run's token cannot stop the live stage", () => {
    const r = rig();
    const old = r.stage.start();
    const live = r.stage.start();
    r.stage.stop(old);
    expect(r.stage.status().state).toBe("loading");
    r.stage.stop(live);
    expect(r.stage.status().state).toBe("idle");
  });
});

describe("the mic OFF scores nothing", () => {
  it("posts no frames while inactive, resets the worker's state when listening resumes", () => {
    let on = true;
    const r = rig({ active: () => on });
    ready(r);
    r.stage.push(frame(1));
    r.w().say({ type: "prob", seq: 1, p: 0.8, ms: 1 });
    on = false;
    r.stage.push(frame(2)); r.stage.push(frame(3));
    expect(r.w().frames()).toHaveLength(1); // nothing posted while OFF
    expect(r.out.map((o) => o.v)).toEqual([1, 2, 3]); // but the capture still gets its frames
    expect(r.out[1].p).toBeNull();
    on = true;
    r.stage.push(frame(4));
    expect(r.w().posted.map((m) => m.type).slice(-2)).toEqual(["reset", "frame"]);
  });

  it("an OFF frame behind a scored one waits its turn", () => {
    let on = true;
    const r = rig({ active: () => on });
    ready(r);
    r.stage.push(frame(1));
    on = false;
    r.stage.push(frame(2));
    expect(r.out).toEqual([]); // frame 2 does not overtake frame 1
    r.w().say({ type: "prob", seq: 1, p: 0.6, ms: 1 });
    expect(r.out.map((o) => o.v)).toEqual([1, 2]);
  });
});

describe("stop", () => {
  it("ends the worker and forgets what was held", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1));
    r.stage.stop();
    expect(r.w().terminated).toBe(true);
    expect(r.stage.status().state).toBe("idle");
    r.w().say({ type: "prob", seq: 1, p: 0.9, ms: 1 }); // a late verdict from a dead worker
    expect(r.out).toEqual([]);
  });
});

const denoiseMsgs = (w: FakeWorker) => w.posted.filter((m) => m.type === "denoise");

describe("noise suppression (V14): off by default, and nothing loads", () => {
  it("is off, and the worker is never told about it, unless the switch is on", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1));
    expect(r.stage.status().noise).toMatchObject({ wanted: false, state: "off", code: null, frameMs: 0, loadMs: 0 });
    expect(denoiseMsgs(r.w())).toEqual([]);
    expect(r.w().posted.map((m) => m.type)).toEqual(["init", "frame"]);
  });

  it("switching on after the worker is up tells it, and the state follows the worker's reports", () => {
    const r = rig();
    ready(r);
    r.stage.setNoiseSuppression(true);
    expect(denoiseMsgs(r.w())).toEqual([{ type: "denoise", on: true }]);
    expect(r.stage.status().noise).toMatchObject({ wanted: true, state: "loading" });
    r.w().say({ type: "denoise", state: "ready", loadMs: 120 });
    expect(r.stage.status().noise).toMatchObject({ wanted: true, state: "ready", loadMs: 120, code: null });
    r.stage.push(frame(1));
    r.w().say({ type: "prob", seq: 1, p: 0.3, ms: 0.5, dms: 0.4 });
    expect(r.stage.status().noise.frameMs).toBeCloseTo(0.4, 5);
    expect(r.stage.status().frameMs).toBeCloseTo(0.5, 5); // the VAD's own time stays apart
  });

  it("switching off tells the worker and says off at once", () => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "ready", loadMs: 100 });
    r.stage.setNoiseSuppression(false);
    expect(denoiseMsgs(r.w()).at(-1)).toEqual({ type: "denoise", on: false });
    expect(r.stage.status().noise).toMatchObject({ wanted: false, state: "off" });
    r.w().say({ type: "denoise", state: "ready", loadMs: 100 }); // a report from before the switch went off
    expect(r.stage.status().noise.state).toBe("off");
  });

  it("switched on before the worker exists: it reads closed (the mic is not open), then asks as soon as the worker starts", () => {
    const r = rig();
    r.stage.setNoiseSuppression(true);
    expect(r.stage.status().noise).toMatchObject({ wanted: true, state: "closed" });
    r.stage.start();
    expect(r.w().posted.map((m) => m.type)).toEqual(["init", "denoise"]);
    expect(r.stage.status().noise.state).toBe("loading");
  });

  it("an initial switch position of on behaves the same", () => {
    const r = rig({ noise: true });
    expect(r.stage.status().noise).toMatchObject({ wanted: true, state: "closed" });
    r.stage.start();
    expect(denoiseMsgs(r.w())).toEqual([{ type: "denoise", on: true }]);
  });
});

describe("noise suppression falls back to raw, visibly, and the VAD keeps working", () => {
  it.each([["fetch"], ["hash"], ["runtime"], ["slow"]] as const)("a '%s' failure is named, Silero stays the detector, frames still flow", (code) => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "failed", code });
    const st = r.stage.status();
    expect(st.noise).toMatchObject({ wanted: true, state: "failed", code });
    expect(st.noise.note).toMatch(/noise-suppression|noise suppression/);
    expect(st.detector).toBe("silero");
    r.stage.push(frame(5));
    r.w().say({ type: "prob", seq: 1, p: 0.8, ms: 1, dms: 0 });
    expect(r.out).toEqual([{ v: 5, p: 0.8 }]);
    expect(st.noise.note).not.toMatch(/http|\.wasm|rnnoise_/);
  });

  it("an unknown failure code shows as a runtime failure", () => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "failed", code: "weird" as never });
    expect(r.stage.status().noise.code).toBe("runtime");
  });

  it("retryNoise asks the worker again; the switch off and on does too", () => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "failed", code: "fetch" });
    r.stage.retryNoise();
    expect(denoiseMsgs(r.w()).length).toBe(2);
    expect(r.stage.status().noise.state).toBe("loading");
    r.w().say({ type: "denoise", state: "failed", code: "fetch" });
    r.stage.setNoiseSuppression(false);
    r.stage.setNoiseSuppression(true);
    expect(denoiseMsgs(r.w()).map((m) => (m as { on: boolean }).on)).toEqual([true, true, false, true]);
  });

  it("when Silero itself fails, noise suppression is waiting, not running; a retry starts both", () => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "ready", loadMs: 50 });
    r.w().say({ type: "failed", code: "runtime" });
    expect(r.stage.status().noise).toMatchObject({ wanted: true, state: "waiting" });
    r.stage.retry();
    expect(denoiseMsgs(r.w()).at(-1)).toEqual({ type: "denoise", on: true });
    expect(r.stage.status().noise.state).toBe("loading");
  });

  it("whatever it does, the capture gets the raw frame (recorded audio is not denoised)", () => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "ready", loadMs: 50 });
    const mine = frame(0.25);
    const copy = mine.slice();
    r.stage.push(mine);
    const sent = r.w().frames()[0].samples;
    sent.fill(0); // the worker rewrites what it was given
    r.w().say({ type: "prob", seq: 1, p: 0.6, ms: 1, dms: 0.3 });
    expect(r.out).toHaveLength(1);
    expect(r.out[0].v).toBe(0.25);
    expect(Array.from(mine)).toEqual(Array.from(copy));
  });

  it("a stopped stage (mic closed) forgets the noise stage but keeps the switch, and says the mic is closed", () => {
    const r = rig({ noise: true });
    ready(r);
    r.w().say({ type: "denoise", state: "ready", loadMs: 50 });
    r.stage.stop();
    expect(r.stage.status().noise).toMatchObject({ wanted: true, state: "closed", note: "the microphone is closed" });
  });
});

describe("what the self-voice gate and the capture are handed (V9 x V14)", () => {
  it("is the very frame the microphone pushed, bit for bit, with noise suppression ready: the gate compares raw mic frames, as before V14", () => {
    const got: Float32Array[] = [];
    const workers: FakeWorker[] = [];
    const stage = new VadStage({
      deliver: (f) => got.push(f),
      createWorker: () => { const w = new FakeWorker(); workers.push(w); return w; },
      noiseSuppression: true,
    });
    stage.start();
    const w = workers[0];
    w.say({ type: "ready", loadMs: 1 });
    w.say({ type: "denoise", state: "ready", loadMs: 1 });
    const mine = Float32Array.from({ length: 512 }, (_, i) => Math.sin(i / 5) * 0.3);
    const copy = Array.from(mine);
    stage.push(mine);
    w.frames()[0].samples.fill(0); // the worker's denoised result lands in ITS copy
    w.say({ type: "prob", seq: 1, p: 0.7, ms: 1, dms: 0.4 });
    expect(got).toHaveLength(1);
    expect(got[0]).toBe(mine);
    expect(Array.from(got[0])).toEqual(copy);
  });
});
