import { describe, expect, it } from "vitest";
import { WebAudioPlayer } from "./player";

/** Just enough of an AudioContext to see what the player asks of it. */
class FakeSource {
  buffer: unknown = null;
  onended: (() => void) | null = null;
  started = 0;
  stopped = 0;
  connected: unknown = null;
  connect(n: unknown) { this.connected = n; }
  disconnect() {}
  start() { this.started++; }
  stop() { this.stopped++; queueMicrotask(() => this.onended?.()); }
}

class FakeContext {
  state: "running" | "suspended" = "suspended";
  destination = { dest: true };
  sources: FakeSource[] = [];
  resumed = 0;
  analyserNode = {
    fftSize: 0, connect(_n: unknown) {},
    getFloatTimeDomainData(buf: Float32Array) { buf.fill(0.1); },
  };
  private listeners: (() => void)[] = [];
  createAnalyser() { return this.analyserNode as unknown as AnalyserNode; }
  createBufferSource() { const s = new FakeSource(); this.sources.push(s); return s as unknown as AudioBufferSourceNode; }
  async decodeAudioData(b: ArrayBuffer) { return { decoded: b.byteLength }; }
  addEventListener(_t: string, f: () => void) { this.listeners.push(f); }
  removeEventListener(_t: string, f: () => void) { this.listeners = this.listeners.filter((x) => x !== f); }
  listenerCount() { return this.listeners.length; }
  async resume() { this.resumed++; this.state = "running"; this.listeners.forEach((f) => f()); }
  async close() {}
}

function target() {
  const handlers = new Map<string, () => void>();
  return {
    handlers,
    add: (n: string, f: () => void) => { handlers.set(n, f); },
    remove: (n: string) => { handlers.delete(n); },
  };
}

function make() {
  const ctx = new FakeContext();
  const t = target();
  const player = new WebAudioPlayer(() => ctx as unknown as AudioContext, {
    addEventListener: t.add, removeEventListener: t.remove,
  } as unknown as Pick<Window, "addEventListener" | "removeEventListener">);
  return { ctx, t, player };
}

describe("WebAudioPlayer", () => {
  it("decodes bytes and plays them through the analyser", async () => {
    const { ctx, player } = make();
    const audio = await player.decode(new ArrayBuffer(8));
    expect(audio).toEqual({ decoded: 8 });
    player.play(audio);
    expect(ctx.sources).toHaveLength(1);
    expect(ctx.sources[0].started).toBe(1);
    expect(ctx.sources[0].connected).toBe(ctx.analyserNode);
    expect(player.level()).toBeGreaterThan(0);
  });

  it("ends with 'ended' at the natural end and 'stopped' after stop()", async () => {
    const { ctx, player } = make();
    const a = player.play({});
    ctx.sources[0].onended?.();
    expect(await a.done).toBe("ended");
    const b = player.play({});
    b.stop();
    expect(await b.done).toBe("stopped");
    expect(ctx.sources[1].stopped).toBe(1);
    b.stop();                                   // idempotent
    expect(ctx.sources[1].stopped).toBe(1);
  });

  it("is blocked while the context is suspended, and the first gesture frees it", async () => {
    const { ctx, t, player } = make();
    await player.decode(new ArrayBuffer(1));
    expect(player.blocked()).toBe(true);
    let told = 0;
    player.subscribeBlocked(() => { told++; });
    t.handlers.get("pointerdown")!();
    await Promise.resolve();
    expect(ctx.resumed).toBeGreaterThan(0);
    expect(player.blocked()).toBe(false);
    expect(told).toBeGreaterThan(0);
    expect(t.handlers.has("pointerdown")).toBe(false);   // the listener goes once it worked
  });

  it("reports a buffer's length in ms for the watchdog", () => {
    const { player } = make();
    expect(player.duration({ duration: 1.5 })).toBe(1500);
    expect(Number.isFinite(player.duration({}))).toBe(false);
  });

  it("dispose removes the gesture and statechange listeners", async () => {
    const { ctx, t, player } = make();
    await player.decode(new ArrayBuffer(1));
    expect(t.handlers.has("pointerdown")).toBe(true);
    expect(ctx.listenerCount()).toBe(1);
    player.dispose();
    expect(t.handlers.has("pointerdown")).toBe(false);
    expect(ctx.listenerCount()).toBe(0);
  });

  describe("the self-voice reference (V9)", () => {
    const buf = (n = 100) => ({ sampleRate: 24000, getChannelData: () => new Float32Array(n).fill(0.2) });
    const sink = () => {
      const log: string[] = [];
      return { log, start: (id: number, samples: Float32Array, rate: number) => log.push(`start ${id} ${samples.length} ${rate}`), stop: (id: number) => log.push(`stop ${id}`) };
    };

    it("reports each sound as it starts and as it stops", async () => {
      const { ctx, player } = make();
      const s = sink();
      player.setReference(s);
      await ctx.resume();
      const h = player.play(buf());
      expect(s.log).toEqual(["start 1 100 24000"]);
      h.stop();
      expect(s.log).toEqual(["start 1 100 24000", "stop 1"]);
      ctx.sources[0].onended?.();                     // a late natural end after the stop changes nothing more than a repeat
      expect(s.log.filter((l) => l.startsWith("start"))).toHaveLength(1);
    });

    it("a natural end is reported as a stop", async () => {
      const { ctx, player } = make();
      const s = sink();
      player.setReference(s);
      await ctx.resume();
      const h = player.play(buf());
      ctx.sources[0].onended?.();
      await h.done;
      expect(s.log).toEqual(["start 1 100 24000", "stop 1"]);
    });

    // A real context resumes later, not inside the call: the page waits for a gesture.
    const slow = (ctx: FakeContext) => {
      const real = ctx.resume.bind(ctx);
      ctx.resume = async () => {};
      return () => real();
    };

    it("a sound started while the context is suspended is not a reference until it runs", async () => {
      const { ctx, player } = make();
      const run = slow(ctx);
      const s = sink();
      player.setReference(s);
      await player.decode(new ArrayBuffer(1));       // the context exists, suspended
      player.play(buf());
      expect(s.log).toEqual([]);                      // suspended: nothing is audible yet
      await run();                                    // statechange
      expect(s.log).toEqual(["start 1 100 24000"]);
    });

    it("a sound stopped before the context ran is never a reference", async () => {
      const { ctx, player } = make();
      const run = slow(ctx);
      const s = sink();
      player.setReference(s);
      await player.decode(new ArrayBuffer(1));
      player.play(buf()).stop();
      await run();
      expect(s.log.filter((l) => l.startsWith("start"))).toEqual([]);
    });

    it("a buffer that cannot be read, or a sink that throws, never breaks playback", async () => {
      const { ctx, player } = make();
      player.setReference({ start: () => { throw new Error("no"); }, stop: () => { throw new Error("no"); } });
      await ctx.resume();
      const h = player.play(buf());
      h.stop();
      expect(await h.done).toBe("stopped");
      const h2 = player.play({});                     // no getChannelData
      expect(ctx.sources[1].started).toBe(1);
      h2.stop();
    });

    it("with no reference nothing is asked", async () => {
      const { ctx, player } = make();
      await ctx.resume();
      expect(() => player.play(buf()).stop()).not.toThrow();
    });
  });
});
