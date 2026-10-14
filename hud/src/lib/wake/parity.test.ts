// @vitest-environment node
// The port against the original: every clip of V0's synthetic KWS set (and every third dictation clip) through the
// browser engine and through sherpa-onnx's own spotter (tests/voice_v11/native_reference.py wrote reference.json),
// at thresholds 0.15 / 0.25 / 0.4, for the wake set and the command set. Needs V0's fixtures and the reference run,
// so it is skipped unless JARVIS_VOICE_V0=1 and the file exists:
//   JARVIS_VOICE_V0=1 ~/.cache/jarvis-voice-v0/venv/bin/python -I tests/voice_v11/native_reference.py
//   JARVIS_VOICE_V0=1 npx vitest run src/lib/wake/parity.test.ts
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { hitsFor, wav } from "./testkit";

const HOME = process.env.HOME ?? "";
const REF = `${HOME}/.cache/jarvis-voice-v0/v11/reference.json`;
const FIX = `${HOME}/.cache/jarvis-voice-v0/fixtures/`;
const on = process.env.JARVIS_VOICE_V0 === "1" && existsSync(REF);

describe.skipIf(!on)("the browser engine against sherpa-onnx's own spotter", () => {
  const ref = on ? JSON.parse(readFileSync(REF, "utf8")) : null;
  for (const set of ["jarvis", "commands"] as const) {
    for (const thr of [0.15, 0.25, 0.4]) {
      it(`${set} at ${thr}: same clips fire, same keyword, within one chunk`, async () => {
        const run: Record<string, [string, number][]> = ref.runs[`${set}@${thr}`];
        let same = 0;
        const differ: string[] = [];
        for (const c of ref.clips as { file: string; text: string }[]) {
          const got = await hitsFor(wav(FIX + c.file), set === "jarvis" ? "wake" : "commands", thr, 1600, 800);
          const want = run[c.file];
          const phrase = (p: string) => (set === "jarvis" ? p : p.split(" ")[1].toLowerCase());
          const ok = got.length === want.length && got.every((g, i) => g.phrase === phrase(want[i][0]) && Math.abs(g.at - want[i][1]) < 0.35);
          if (ok) same++; else differ.push(`${c.file} "${c.text}": ts=${JSON.stringify(got)} native=${JSON.stringify(want)}`);
        }
        const n = ref.clips.length;
        console.log(`${set}@${thr}: ${same}/${n} identical`, differ.length ? "\n" + differ.join("\n") : "");
        // Float arithmetic around a threshold can tip one borderline clip; more than that is a different spotter.
        expect(same).toBeGreaterThanOrEqual(n - 2);
      }, 600_000);
    }
  }
});
