// @vitest-environment node
// The local wake word on the real model (vendored int8 zipformer2) in onnxruntime-web, in Node: the
// synthetic Kokoro clips in tests/face/fixtures/voice_v11, held to what sherpa-onnx's own spotter said
// about them (tests/voice_v11/native_reference.py, thresholds 0.15 / 0.25 / 0.4). Synthetic speech only.
import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { FailError } from "../listen/fail";
import { verifiedFiles } from "./engine";
import { KWS_FILES, KWS_PINS } from "./pins";
import { specsFor, WAKE_PIECES } from "./keywords";
import { MODEL_DIR, fixture, hitsFor, modelBytes, realEngine, tokenTable } from "./testkit";

// What the native spotter reported for these clips at 0.25 (audio time of the chunk it fired on).
const WAKE_NATIVE: Record<string, number> = {
  "kws_pos_00_am_adam.wav": 1.2, "kws_pos_00_bf_emma.wav": 1.2, "kws_pos_07_am_adam.wav": 0.8, "kws_pos_07_bf_emma.wav": 1.2,
  "kws_pos_08_bf_emma.wav": 1.2, "kws_pos_09_bf_emma.wav": 1.2, "kws_pos_10_am_adam.wav": 1.2, "kws_pos_10_bf_emma.wav": 1.2,
  "kws_pos_11_am_adam.wav": 1.2,
};
const COMMANDS_NATIVE: Record<string, [string, number]> = {
  "kws_pos_07_am_adam.wav": ["pause", 1.5], "kws_pos_07_bf_emma.wav": ["pause", 1.5], "kws_pos_08_bf_emma.wav": ["resume", 1.5],
  "kws_pos_09_bf_emma.wav": ["skip", 1.2], "kws_pos_10_am_adam.wav": ["stop", 1.5], "kws_pos_10_bf_emma.wav": ["stop", 1.5],
};
const NEGATIVES = ["kws_neg_00_am_adam.wav", "kws_neg_01_am_adam.wav", "kws_neg_09_am_adam.wav"];

describe("the local wake word on the real model", () => {
  it("hears Jarvis in every synthetic clip that has it, when the native spotter did", async () => {
    for (const [name, native] of Object.entries(WAKE_NATIVE)) {
      const hits = await hitsFor(fixture(name), "wake");
      expect(hits.map((h) => h.phrase), name).toEqual(["JARVIS"]);
      expect(Math.abs(hits[0].at - native), `${name} fired at ${hits[0].at}, native ${native}`).toBeLessThan(0.35);
    }
  }, 120_000);

  it("hears nothing in sentences that only sound like it (Travis, a jar, cancel)", async () => {
    for (const name of NEGATIVES) expect(await hitsFor(fixture(name), "wake"), name).toEqual([]);
  }, 60_000);

  it("silence and a pure tone are not a wake word", async () => {
    expect(await hitsFor(new Float32Array(16000 * 3), "wake")).toEqual([]);
    const tone = Float32Array.from({ length: 16000 * 3 }, (_, i) => 0.3 * Math.sin((2 * Math.PI * 440 * i) / 16000));
    expect(await hitsFor(tone, "wake")).toEqual([]);
  }, 60_000);

  it("the answer does not depend on how the audio is cut into pieces", async () => {
    const audio = fixture("kws_pos_11_am_adam.wav");
    const ref = await hitsFor(audio, "wake", 0.25, 1600);
    for (const chunk of [512, 777, 4000, 16000]) {
      const hits = await hitsFor(audio, "wake", 0.25, chunk);
      expect(hits.map((h) => h.phrase), `chunk ${chunk}`).toEqual(ref.map((h) => h.phrase));
    }
  }, 120_000);

  it("a stricter threshold hears less, a looser one at least as much (V0's grid: 0.15 / 0.25 / 0.4)", async () => {
    const audio = fixture("kws_pos_00_bf_emma.wav");
    expect((await hitsFor(audio, "wake", 0.15)).length).toBe(1);
    expect((await hitsFor(audio, "wake", 0.25)).length).toBe(1);
    expect((await hitsFor(audio, "wake", 0.4)).length).toBe(0); // the native spotter misses this clip at 0.4 too
    expect((await hitsFor(audio, "wake", 0.9)).length).toBe(0);
  }, 60_000);

  it("the command set hears the whole phrases, and only those", async () => {
    for (const [name, [command, native]] of Object.entries(COMMANDS_NATIVE)) {
      const hits = await hitsFor(fixture(name), "commands");
      expect(hits.map((h) => h.phrase), name).toEqual([command]);
      expect(Math.abs(hits[0].at - native), name).toBeLessThan(0.35);
    }
    // A bare word, "Jarvis" alone, and sentences that merely contain the words are no command.
    expect(await hitsFor(fixture("kws_pos_00_am_adam.wav"), "commands")).toEqual([]);
    expect(await hitsFor(fixture("kws_pos_11_am_adam.wav"), "commands")).toEqual([]);
    for (const name of NEGATIVES) expect(await hitsFor(fixture(name), "commands"), name).toEqual([]);
  }, 120_000);

  it("keeps listening after a hit: two Jarvis in one stretch are two hits", async () => {
    const one = fixture("kws_pos_00_am_adam.wav");
    const two = fixture("kws_pos_00_bf_emma.wav");
    const gap = new Float32Array(16000 * 2);
    const both = new Float32Array(gap.length * 3 + one.length + two.length);
    both.set(one, gap.length);
    both.set(two, gap.length * 2 + one.length);
    expect((await hitsFor(both, "wake")).map((h) => h.phrase)).toEqual(["JARVIS", "JARVIS"]);
  }, 60_000);

  it("a stream reset forgets the audio so far: half a word then the rest is not a word", async () => {
    const eng = await realEngine();
    const audio = fixture("kws_pos_00_am_adam.wav");
    const s = eng.newStream(specsFor(WAKE_PIECES, tokenTable(), 0.25));
    const pad = new Float32Array(16000);
    const first = await s.accept(audio.slice(0, 5000));
    s.reset();
    const second = await s.accept(audio.slice(5000));
    const tail = await s.accept(pad);
    expect(first).toBeNull();
    expect(second ?? tail).toBeNull();
  }, 60_000);

  it("costs a few percent of one core on this machine (V0 measured 3.3% under Node)", async () => {
    const eng = await realEngine();
    const audio = fixture("kws_pos_11_am_adam.wav");
    const before = eng.encoderMs;
    await hitsFor(audio, "wake");
    const seconds = audio.length / 16000 + 0.8;
    const pct = (100 * (eng.encoderMs - before)) / 1000 / seconds;
    expect(pct).toBeLessThan(15); // generous: the machine is shared; the real figure is in the notes
  }, 60_000);
});

describe("the pinned files", () => {
  it("the vendored bytes are the pinned bytes, and the archive they came from is V0's", () => {
    for (const f of KWS_FILES) {
      const bytes = readFileSync(resolve(MODEL_DIR, f === "tokens" ? "tokens.txt" : `${f}.int8.onnx`));
      expect(bytes.length, f).toBe(KWS_PINS[f].bytes);
      expect(createHash("sha256").update(bytes).digest("hex"), f).toBe(KWS_PINS[f].sha256);
    }
    const manifest = JSON.parse(readFileSync(resolve(MODEL_DIR, "manifest.json"), "utf8"));
    for (const f of manifest.files) {
      const key = f.file === "tokens.txt" ? "tokens" : f.file.split(".")[0];
      expect(f.sha256, f.file).toBe(KWS_PINS[key as keyof typeof KWS_PINS].sha256);
      expect(f.bytes, f.file).toBe(KWS_PINS[key as keyof typeof KWS_PINS].bytes);
    }
    const v0 = JSON.parse(readFileSync(resolve(MODEL_DIR, "../../../tests/voice_v0/models.json"), "utf8")).models["kws-zipformer-gigaspeech-3.3M"];
    expect(manifest.archive_sha256).toBe(v0.sha256);
    expect(readFileSync(resolve(MODEL_DIR, "LICENSE.txt"), "utf8")).toContain("Apache License");
  });

  it("a changed byte, a short file or a failed fetch never reaches the runtime", async () => {
    const good = modelBytes();
    const serve = (tamper?: (file: string, b: Uint8Array) => Uint8Array | Error) => async (file: keyof typeof good) => {
      const r = tamper?.(file, good[file]);
      if (r instanceof Error) throw r;
      return r ?? good[file];
    };
    // (Outcomes are compared as short strings: a resolved 5 MB file in a failed assertion would be printed whole.)
    const outcome = (t?: (file: string, b: Uint8Array) => Uint8Array | Error) =>
      verifiedFiles(serve(t)).then(() => "accepted", (e: unknown) => (e instanceof FailError ? e.code : "other"));
    expect(await outcome()).toBe("accepted");
    expect(await outcome((file, b) => { const c = b.slice(); c[c.length >> 1] ^= 1; return file === "joiner" ? c : b; })).toBe("hash");
    expect(await outcome((f, b) => (f === "encoder" ? b.slice(0, b.length - 1) : b))).toBe("hash");
    expect(await outcome((f, b) => (f === "tokens" ? new TextEncoder().encode(new TextDecoder().decode(b) + " ") : b))).toBe("hash");
    expect(await outcome((f, b) => (f === "decoder" ? new Error("404") : b))).toBe("fetch");
  });
});
