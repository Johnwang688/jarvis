import { describe, expect, it } from "vitest";
import { FailError } from "./fail";
import { SILERO_BYTES, SILERO_SHA256 } from "./pins";
import { SileroRunner, verifiedModel, type OrtLike, type SessionLike } from "./runner";
import { sha256HexSync } from "./sha256";

const bytesOf = (n: number, seed = 1) => Uint8Array.from({ length: n }, (_, i) => (i * 31 + seed) & 255);

describe("the model is checked before anything parses it", () => {
  const good = bytesOf(1000);
  const pin = sha256HexSync(good);

  it("accepts the pinned bytes", async () => {
    expect(await verifiedModel(async () => good, pin, 1000)).toBe(good);
  });

  it("refuses one changed byte, with the hash code", async () => {
    const bad = good.slice(); bad[500] ^= 1;
    await expect(verifiedModel(async () => bad, pin, 1000)).rejects.toMatchObject({ name: "FailError", code: "hash" });
  });

  it("refuses a truncated or padded file", async () => {
    await expect(verifiedModel(async () => good.slice(0, 999), pin, 1000)).rejects.toMatchObject({ code: "hash" });
    await expect(verifiedModel(async () => new Uint8Array([...good, 0]), pin, 1000)).rejects.toMatchObject({ code: "hash" });
  });

  it("says fetch when the model cannot be read at all", async () => {
    await expect(verifiedModel(async () => { throw new Error("404 https://secret.example/x"); }, pin, 1000))
      .rejects.toMatchObject({ code: "fetch", message: "fetch" });
  });

  it("defaults to the real pin", async () => {
    // (not `.rejects`: were it accepted, a failing assertion would try to print 2.3 MB)
    expect(await verifiedModel(async () => bytesOf(SILERO_BYTES)).then(() => "accepted", (e: FailError) => e.code)).toBe("hash");
    expect(SILERO_SHA256).toMatch(/^[0-9a-f]{64}$/);
  });
});

function fakeOrt(probFor: (n: number) => number) {
  const calls: { input: Float32Array; state: Float32Array; sr: unknown }[] = [];
  let n = 0;
  const ort: OrtLike = {
    Tensor: class { constructor(public type: string, public data: Float32Array | BigInt64Array, public dims: readonly number[]) {} } as unknown as OrtLike["Tensor"],
    InferenceSession: {
      create: async () => {
        const session: SessionLike = {
          run: async (feeds) => {
            const f = feeds as Record<string, { data: Float32Array; dims: number[] }>;
            calls.push({ input: f.input.data.slice(), state: f.state.data.slice(), sr: f.sr });
            n++;
            return { output: { data: new Float32Array([probFor(n)]) }, stateN: { data: new Float32Array(256).fill(n) } };
          },
        };
        return session;
      },
    },
  };
  return { ort, calls };
}

describe("the Silero loop", () => {
  it("feeds context + frame, carries the state, resets cleanly", async () => {
    const { ort, calls } = fakeOrt((n) => n / 100);
    const r = await SileroRunner.create(ort, new Uint8Array(1));
    expect(calls).toHaveLength(1); // the warm-up frame
    calls.length = 0;
    const a = Float32Array.from({ length: 512 }, (_, i) => i / 1000);
    const b = Float32Array.from({ length: 512 }, (_, i) => -i / 1000);
    expect(await r.run(a)).toBeCloseTo(0.02); // call number 2 (warm-up was 1)
    expect(await r.run(b)).toBeCloseTo(0.03);
    expect(calls[0].input.length).toBe(576);
    expect(Array.from(calls[0].input.subarray(0, 64))).toEqual(new Array(64).fill(0)); // fresh context after the warm-up reset
    expect(Array.from(calls[0].input.subarray(64))).toEqual(Array.from(a));
    expect(Array.from(calls[1].input.subarray(0, 64))).toEqual(Array.from(a.subarray(448))); // last 64 of the previous frame
    expect(calls[0].state.every((x) => x === 0)).toBe(true);
    expect(calls[1].state.every((x) => x === 2)).toBe(true); // state out of call 2 is state in of call 3
    r.reset();
    await r.run(a);
    expect(calls[2].state.every((x) => x === 0)).toBe(true);
    expect(Array.from(calls[2].input.subarray(0, 64))).toEqual(new Array(64).fill(0));
  });

  it("sends the 16 kHz int64 rate", async () => {
    const { ort, calls } = fakeOrt(() => 0.5);
    await SileroRunner.create(ort, new Uint8Array(1));
    const sr = calls[0].sr as { type: string; data: BigInt64Array };
    expect(sr.type).toBe("int64");
    expect(sr.data[0]).toBe(16000n);
  });

  it("refuses a frame of the wrong length and clamps odd probabilities", async () => {
    const { ort } = fakeOrt(() => 1.5);
    const r = await SileroRunner.create(ort, new Uint8Array(1));
    await expect(r.run(new Float32Array(480))).rejects.toThrow();
    expect(await r.run(new Float32Array(512))).toBe(1);
  });

  it("a session that will not build is a runtime failure with no engine text", async () => {
    const ort = { Tensor: class {}, InferenceSession: { create: async () => { throw new Error("wasm at /x/y failed"); } } } as unknown as OrtLike;
    const e = await SileroRunner.create(ort, new Uint8Array(1)).catch((x) => x);
    expect(e).toBeInstanceOf(FailError);
    expect(e.code).toBe("runtime");
    expect(e.message).toBe("runtime");
  });

  it("non-finite output is an error, not a probability", async () => {
    const { ort } = fakeOrt((n) => (n > 1 ? NaN : 0.5));
    const r = await SileroRunner.create(ort, new Uint8Array(1));
    await expect(r.run(new Float32Array(512))).rejects.toThrow();
  });
});
