import { describe, expect, it } from "vitest";
import { FRAME_DEADLINE_MS, LOAD_DEADLINE_MS, MAX_PENDING, VadStage, type DetectorStatus, type WorkerLike } from "./stage";
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

function rig(opts: { active?: () => boolean; denoise?: (f: Float32Array) => Float32Array; throwOnCreate?: boolean } = {}) {
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
    denoise: opts.denoise,
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

  it("a worker that falls behind: the queue is bounded, then energy takes over, in order", () => {
    const r = rig();
    ready(r);
    for (let i = 1; i <= MAX_PENDING; i++) r.stage.push(frame(i));
    expect(r.stage.status().state).toBe("ready");
    expect(r.out).toEqual([]);
    expect(r.w().frames()).toHaveLength(MAX_PENDING);
    r.stage.push(frame(MAX_PENDING + 1)); // one more than the bound
    expect(r.stage.status()).toMatchObject({ detector: "energy", state: "failed", code: "backlog" });
    expect(r.out.map((x) => x.v)).toEqual(Array.from({ length: MAX_PENDING + 1 }, (_, i) => i + 1));
    expect(r.out.every((x) => x.p === null)).toBe(true);
    expect(r.w().terminated).toBe(true);
    r.stage.push(frame(99)); // and from now on, straight through
    expect(r.out.at(-1)).toEqual({ v: 99, p: null });
  });

  it("a worker that answers late: the oldest unanswered frame has a deadline", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1));
    r.tick(FRAME_DEADLINE_MS + 1);
    r.stage.push(frame(2));
    expect(r.stage.status()).toMatchObject({ detector: "energy", code: "late" });
    expect(r.out.map((x) => x.p)).toEqual([null, null]);
  });

  it("a verdict that arrives past its deadline is used for that frame, then the stage falls back", () => {
    const r = rig();
    ready(r);
    r.stage.push(frame(1)); r.stage.push(frame(2));
    r.tick(FRAME_DEADLINE_MS + 1);
    r.w().say({ type: "prob", seq: 1, p: 0.9, ms: 1 });
    expect(r.out).toEqual([{ v: 1, p: 0.9 }, { v: 2, p: null }]);
    expect(r.stage.status().code).toBe("late");
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
