// Capture -> chunk -> SttAssembler, wired the way App wires them (`chunkSubmitOptions`): a word
// said on both sides of a pause cut is two words, not one echo (review of the V5 merge).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { Capture } from "./capture";
import { SttAssembler, chunkSubmitOptions } from "./stt";
import { END_PAUSE_MS } from "./vad";
import { VoiceDiagnostics } from "./voiceDiagnostics";

async function run(optionsFor: typeof chunkSubmitOptions, answers: string[]) {
  const placed: string[] = [];
  let gid = 0;
  const stt = new SttAssembler<string>({
    transcribe: async (_blob, o) => answers[o.seq - o.utteranceId],
    place: (b) => { placed.push(...b.map((p) => p.text)); },
    diagnostics: new VoiceDiagnostics(() => 0),
  });
  const chunks: { overlapMs: number; repeats: boolean }[] = [];
  const cap = new Capture({
    suppressed: () => false, muted: () => false, onLevel: () => {}, onIdle: () => {},
    onListening: (source) => { gid = stt.beginGroup(source ?? "unclaimed", "m"); },
    onChunk: (c) => {
      chunks.push({ overlapMs: c.overlapMs, repeats: c.repeats });
      stt.submit(c.wav, c.ms, c.source, "m", optionsFor(c, gid));
    },
    onUtteranceEnd: () => { stt.closeGroup(gid); },
  });
  cap.feedMs(1500, 0.0009, 512, 0.02);
  cap.openFollowUp();
  cap.feedMs(1500, 0.03, 512, 0.95);          // "it was very"
  cap.feedMs(900, 0.0009, 512, 0.02);          // a breath: the first chunk is cut here, at a pause
  cap.feedMs(1500, 0.03, 512, 0.95);          // "very good indeed"
  cap.feedMs(END_PAUSE_MS + 300, 0.0009, 512, 0.02);
  await stt.whenGroupSettled(gid);
  return { placed, chunks };
}

describe("a word said on both sides of a pause cut", () => {
  it("is kept twice: a cut at a pause is not an echo", async () => {
    const { placed, chunks } = await run(chunkSubmitOptions, ["it was very", "very good indeed"]);
    expect(chunks).toHaveLength(2);
    expect(chunks[1]).toMatchObject({ overlapMs: 200, repeats: false });
    expect(placed).toEqual(["it was very very good indeed"]);
  });

  it("while a hard cut in speech still joins on the words it repeats", async () => {
    const placed: string[] = [];
    const stt = new SttAssembler<string>({
      transcribe: async (_b, o) => ["we should rename the branch", "rename the branch before tonight"][o.seq - o.utteranceId],
      place: (b) => { placed.push(...b.map((p) => p.text)); },
      diagnostics: new VoiceDiagnostics(() => 0),
    });
    const g = stt.beginGroup("ptt", "m");
    const wav = new Blob([new Uint8Array(100)]);
    stt.submit(wav, 8000, "ptt", "m", chunkSubmitOptions({ overlapMs: 0, repeats: false }, g));
    stt.submit(wav, 8500, "ptt", "m", chunkSubmitOptions({ overlapMs: 500, repeats: true }, g));
    stt.closeGroup(g);
    await stt.whenGroupSettled(g);
    expect(placed).toEqual(["we should rename the branch before tonight"]);
  });

  it("the wiring passes the chunk's own flag (it is not dropped on the way)", () => {
    expect(chunkSubmitOptions({ overlapMs: 200, repeats: false, detector: "silero" }, 3)).toEqual({ group: 3, overlapMs: 200, repeats: false, detector: "silero" });
  });

  it("App submits every chunk through it (and through nothing that could leave the flag out)", () => {
    const app = readFileSync(resolve(__dirname, "../App.tsx"), "utf8");
    expect(app.match(/stt\.submit\(/g)).toHaveLength(1);
    expect(app).toContain("stt.submit(chunk.wav, chunk.ms, chunk.source, u.meta, chunkSubmitOptions(chunk, u.gid))");
  });
});
