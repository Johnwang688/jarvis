import { describe, expect, it } from "vitest";
import { LOAD_DEADLINE_MS, MAX_BACKLOG_MS, SAMPLE_RATE, WakeStage, type WakeWorkerLike } from "./stage";
import { WAKE_FAIL_TEXT, type WakeFromWorker, type WakeToWorker } from "./protocol";
import type { PlaybackCommand } from "./keywords";

class FakeWorker implements WakeWorkerLike {
  sent: WakeToWorker[] = [];
  terminated = false;
  onmessage: ((e: MessageEvent<WakeFromWorker>) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onmessageerror: ((e: unknown) => void) | null = null;
  postMessage(m: WakeToWorker) { this.sent.push(m); }
  terminate() { this.terminated = true; }
  say(m: WakeFromWorker) { this.onmessage?.({ data: m } as MessageEvent<WakeFromWorker>); }
  audioSamples() { return this.sent.filter((m) => m.type === "audio").reduce((n, m) => n + (m as { samples: Float32Array }).samples.length, 0); }
}

function rig(opts: { active?: () => boolean; throwOnCreate?: boolean } = {}) {
  const workers: FakeWorker[] = [];
  const woke: number[] = [];
  const commands: PlaybackCommand[] = [];
  let t = 0;
  const stage = new WakeStage({
    onWake: () => woke.push(t),
    onCommand: (c) => commands.push(c),
    createWorker: () => { if (opts.throwOnCreate) throw new Error("no"); const w = new FakeWorker(); workers.push(w); return w; },
    now: () => t,
    active: opts.active,
  });
  const frame = () => new Float32Array(512).fill(0.1);
  return { stage, workers, woke, commands, frame, at: (ms: number) => { t = ms; }, w: () => workers[workers.length - 1] };
}
const ready = (r: ReturnType<typeof rig>) => { r.stage.start(); r.w().say({ type: "ready", loadMs: 900 }); };

describe("the wake stage", () => {
  it("loads, then listens: init, then config on ready, then audio", () => {
    const r = rig();
    r.stage.start();
    expect(r.stage.status().state).toBe("loading");
    expect(r.w().sent).toEqual([{ type: "init" }]);
    r.w().say({ type: "ready", loadMs: 900 });
    expect(r.stage.status()).toMatchObject({ state: "ready", loadMs: 900, set: "wake", sensitivity: "normal" });
    expect(r.w().sent[1]).toEqual({ type: "config", set: "wake", threshold: 0.25 });
    r.stage.push(r.frame());
    expect(r.w().sent[2]).toMatchObject({ type: "audio" });
    expect(r.stage.running()).toBe(true);
  });

  it("posts a copy, so the capture's own frame is never touched or detached", () => {
    const r = rig(); ready(r);
    const f = r.frame();
    r.stage.push(f);
    expect(f.length).toBe(512);
    expect(f[0]).toBeCloseTo(0.1);
  });

  it("a wake hit is one call to onWake and nothing else; a stale or early one is none", () => {
    const r = rig();
    r.stage.start();
    r.w().say({ type: "hit", set: "wake", phrase: "JARVIS" }); // not ready yet
    expect(r.woke.length).toBe(0);
    r.w().say({ type: "ready", loadMs: 1 });
    r.w().say({ type: "hit", set: "wake", phrase: "JARVIS" });
    expect(r.woke.length).toBe(1);
    expect(r.commands).toEqual([]);
    r.w().say({ type: "hit", set: "commands", phrase: "stop" }); // wrong set for now
    expect(r.commands).toEqual([]);
    expect(r.stage.status()).toMatchObject({ wakeHits: 1, commandHits: 0 });
  });

  it("the command set listens only while asked, and only the four playback commands come out", () => {
    const r = rig(); ready(r);
    r.stage.setCommands(true);
    expect(r.w().sent.at(-1)).toEqual({ type: "config", set: "commands", threshold: 0.25 });
    for (const c of ["pause", "resume", "skip", "stop"]) r.w().say({ type: "hit", set: "commands", phrase: c });
    r.w().say({ type: "hit", set: "commands", phrase: "approve" });
    r.w().say({ type: "hit", set: "commands", phrase: "" });
    r.w().say({ type: "hit", set: "wake", phrase: "JARVIS" }); // the wake set is not wanted any more
    expect(r.commands).toEqual(["pause", "resume", "skip", "stop"]);
    expect(r.woke.length).toBe(0);
    r.stage.setCommands(false);
    expect(r.w().sent.at(-1)).toEqual({ type: "config", set: "wake", threshold: 0.25 });
    r.w().say({ type: "hit", set: "commands", phrase: "stop" }); // a late one from the old set
    expect(r.commands.length).toBe(4);
  });

  it("sensitivity changes the worker's threshold (V0's grid) and nothing else", () => {
    const r = rig(); ready(r);
    r.stage.setSensitivity("high");
    expect(r.w().sent.at(-1)).toEqual({ type: "config", set: "wake", threshold: 0.15 });
    r.stage.setSensitivity("low");
    expect(r.w().sent.at(-1)).toEqual({ type: "config", set: "wake", threshold: 0.4 });
    const n = r.w().sent.length;
    r.stage.setSensitivity("low");
    r.stage.setSensitivity("bogus" as never);
    r.stage.setSensitivity("toString" as never);
    r.stage.setSensitivity("constructor" as never);
    expect(r.w().sent.length).toBe(n);
  });

  it("posts nothing while the microphone is off, and the worker forgets its audio when it comes back", () => {
    let on = true;
    const r = rig({ active: () => on }); ready(r);
    r.stage.push(r.frame());
    on = false;
    for (let i = 0; i < 50; i++) r.stage.push(r.frame());
    expect(r.w().audioSamples()).toBe(512);
    expect(r.stage.running()).toBe(false);
    on = true;
    r.stage.push(r.frame());
    const tail = r.w().sent.slice(-2);
    expect(tail.map((m) => m.type)).toEqual(["reset", "audio"]);
  });

  it("only ever sends the four kinds of message, and audio only as samples", () => {
    const r = rig(); ready(r);
    r.stage.push(r.frame()); r.stage.setSensitivity("high"); r.stage.setCommands(true);
    for (const m of r.w().sent) expect(["init", "config", "audio", "reset"]).toContain(m.type);
  });
});

describe("when the wake word cannot run", () => {
  it("says so with a code from the closed set, never an engine message, and stays failed", () => {
    for (const code of ["fetch", "hash", "runtime"] as const) {
      const r = rig();
      r.stage.start();
      r.w().say({ type: "failed", code });
      const s = r.stage.status();
      expect(s).toMatchObject({ state: "failed", code, note: WAKE_FAIL_TEXT[code] });
      expect(r.w().terminated).toBe(true);
      expect(r.stage.running()).toBe(false);
      r.stage.push(r.frame());
      expect(r.w().audioSamples()).toBe(0);
      r.at(5000); r.stage.push(r.frame()); // time passing does not undo it
      expect(r.stage.status().state).toBe("failed");
    }
  });

  it("a checksum failure reads as a checksum failure", () => {
    const r = rig(); r.stage.start();
    r.w().say({ type: "failed", code: "hash" });
    expect(r.stage.status().note).toMatch(/pinned checksum/);
    expect(r.stage.status().note).not.toMatch(/http|\/|\\/);
  });

  it("an unknown code from the worker becomes a runtime failure", () => {
    const r = rig(); r.stage.start();
    r.w().say({ type: "failed", code: "<script>" as never });
    expect(r.stage.status().code).toBe("runtime");
    const r2 = rig(); r2.stage.start();
    r2.w().say({ type: "failed", code: "toString" as never });
    expect(r2.stage.status().code).toBe("runtime");
  });

  it("no Worker, a Worker that will not construct, and a script error each fail visibly", () => {
    const a = rig({ throwOnCreate: true }); a.stage.start();
    expect(a.stage.status()).toMatchObject({ state: "failed", code: "worker" });
    const b = rig(); b.stage.start(); b.w().onerror?.({});
    expect(b.stage.status()).toMatchObject({ state: "failed", code: "worker" });
    const c = rig(); c.stage.start(); c.w().onmessageerror?.({});
    expect(c.stage.status()).toMatchObject({ state: "failed", code: "worker" });
    const saved = (globalThis as { Worker?: unknown }).Worker;
    delete (globalThis as { Worker?: unknown }).Worker;
    try {
      const d = new WakeStage({ onWake: () => {} });
      d.start();
      expect(d.status()).toMatchObject({ state: "failed", code: "no_worker" });
    } finally { if (saved) (globalThis as { Worker?: unknown }).Worker = saved; }
  });

  it("a model that takes too long to load is a timeout, noticed on the next frame", () => {
    const r = rig(); r.stage.start();
    r.at(LOAD_DEADLINE_MS - 1); r.stage.push(r.frame());
    expect(r.stage.status().state).toBe("loading");
    r.at(LOAD_DEADLINE_MS + 1); r.stage.push(r.frame());
    expect(r.stage.status()).toMatchObject({ state: "failed", code: "timeout" });
  });

  it("falls behind: more than MAX_BACKLOG_MS of audio unanswered is a backlog failure", () => {
    const r = rig(); ready(r);
    const frames = Math.ceil((MAX_BACKLOG_MS * SAMPLE_RATE) / 1000 / 512);
    for (let i = 0; i < frames - 1; i++) r.stage.push(r.frame());
    expect(r.stage.status().state).toBe("ready");
    r.stage.push(r.frame()); r.stage.push(r.frame());
    expect(r.stage.status()).toMatchObject({ state: "failed", code: "backlog" });
  });

  it("a worker that keeps reporting progress never trips the backlog bound", () => {
    const r = rig(); ready(r);
    let consumed = 0;
    for (let i = 0; i < 2000; i++) {
      r.stage.push(r.frame());
      consumed += 512;
      if (i % 8 === 7) r.w().say({ type: "progress", samples: consumed, busyMs: consumed / 16 / 40 });
    }
    expect(r.stage.status().state).toBe("ready");
    expect(r.stage.status().cpuPct).toBeCloseTo(2.5, 1);
  });

  it("retry loads a fresh worker and listening resumes; retry is a no-op unless failed", () => {
    const r = rig(); r.stage.start();
    r.stage.retry();
    expect(r.workers.length).toBe(1);
    r.w().say({ type: "failed", code: "fetch" });
    r.stage.retry();
    expect(r.workers.length).toBe(2);
    r.w().say({ type: "ready", loadMs: 5 });
    expect(r.stage.status()).toMatchObject({ state: "ready", code: null });
    r.w().say({ type: "hit", set: "wake", phrase: "JARVIS" });
    expect(r.woke.length).toBe(1);
  });

  it("stop releases the worker, and hits that arrive after it are nothing", () => {
    const r = rig(); ready(r);
    const w = r.w();
    r.stage.stop();
    expect(w.terminated).toBe(true);
    expect(r.stage.status().state).toBe("idle");
    expect(w.onmessage).toBeNull();
  });

  it("a throwing onWake cannot wedge the stage", () => {
    const stage = new WakeStage({ onWake: () => { throw new Error("capture"); }, createWorker: () => (w = new FakeWorker()) });
    let w!: FakeWorker;
    stage.start(); w.say({ type: "ready", loadMs: 1 });
    expect(() => w.say({ type: "hit", set: "wake", phrase: "JARVIS" })).not.toThrow();
    expect(stage.status().state).toBe("ready");
  });
});
