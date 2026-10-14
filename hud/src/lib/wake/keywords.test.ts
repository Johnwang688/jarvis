// @vitest-environment node
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { COMMAND_OF, COMMAND_PIECES, SENSITIVITY, WAKE_PIECES, parseTokens, specsFor } from "./keywords";
import { MODEL_DIR } from "./testkit";

const tokens = parseTokens(readFileSync(resolve(MODEL_DIR, "tokens.txt"), "utf8"));

// The ids sentencepiece's bpe.model gave for each phrase when V11 was written (tests/voice_v11/native_reference.py
// encodes with the same model): the pieces in keywords.ts must be exactly these tokens, or the browser listens for
// something other than what V0 measured.
const SENTENCEPIECE_IDS: Record<string, number[]> = {
  JARVIS: [424, 24, 168, 3],
  "JARVIS PAUSE": [424, 24, 168, 3, 134, 28, 95],
  "JARVIS RESUME": [424, 24, 168, 3, 39, 3, 28, 145],
  "JARVIS SKIP": [424, 24, 168, 3, 31, 54, 36, 26],
  "JARVIS STOP": [424, 24, 168, 3, 93, 22, 26],
};

describe("the keyword lists", () => {
  it("the wake list is one phrase, \"Jarvis\" (Q16)", () => {
    expect(Object.keys(WAKE_PIECES)).toEqual(["JARVIS"]);
  });

  it("every piece is a token of the model, and the ids are sentencepiece's", () => {
    for (const set of [WAKE_PIECES, COMMAND_PIECES]) {
      for (const [phrase, pieces] of Object.entries(set)) {
        expect(pieces.map((p) => tokens.get(p)), phrase).toEqual(SENTENCEPIECE_IDS[phrase]);
      }
    }
  });

  it("every command starts with Jarvis (V-6) and maps to a playback command, no other", () => {
    for (const phrase of Object.keys(COMMAND_PIECES)) {
      expect(phrase.startsWith("JARVIS ")).toBe(true);
      expect(["pause", "resume", "skip", "stop"]).toContain(COMMAND_OF[phrase]);
    }
    expect(Object.values(COMMAND_OF).sort()).toEqual(["pause", "resume", "skip", "stop"]);
  });

  it("specs carry V0's operating points", () => {
    expect(specsFor(WAKE_PIECES, tokens, SENSITIVITY.normal.threshold)).toEqual([{ ids: SENTENCEPIECE_IDS.JARVIS, phrase: "JARVIS", boost: 1, threshold: 0.25 }]);
    expect([SENSITIVITY.high.threshold, SENSITIVITY.normal.threshold, SENSITIVITY.low.threshold]).toEqual([0.15, 0.25, 0.4]);
    expect(() => specsFor({ X: ["▁NOPE"] }, tokens, 0.25)).toThrow();
  });

  it("parses tokens.txt", () => {
    expect(parseTokens("<blk> 0\n▁A 6\nbad line\nS 3\n")).toEqual(new Map([["<blk>", 0], ["▁A", 6], ["S", 3]]));
  });
});
