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
});
