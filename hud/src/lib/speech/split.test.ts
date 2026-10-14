import { describe, expect, it } from "vitest";
import vectors from "./vectors.json";
import { sentences, stableChunks, wordCut } from "./split";
import { speakable } from "./speakable";

// The vectors are what v1's Python answers (tests/face/speech_vectors_check.py
// regenerates them and fails when they drift from the Python).

describe("sentence splitter against v1's vectors", () => {
  it("splits every corpus text exactly as _sentences does", () => {
    expect(vectors.sentences.length).toBeGreaterThan(20);
    for (const v of vectors.sentences) expect(sentences(v.in), JSON.stringify(v.in)).toEqual(v.out);
  });

  it("chooses the same settled chunks as _stable_chunks, fed a character at a time", () => {
    for (const v of vectors.stable) {
      const declared: string[] = [];
      for (let i = 1; i <= v.text.length; i++) {
        for (const chunk of stableChunks(speakable(v.text.slice(0, i)))) if (!declared.includes(chunk)) declared.push(chunk);
      }
      expect(declared, v.text.slice(0, 40)).toEqual(v.declared);
      expect(sentences(speakable(v.text))).toEqual(v.final);
      for (const [i, chunks] of v.samples as [number, string[]][]) {
        expect(stableChunks(speakable(v.text.slice(0, i))), `${v.text.slice(0, 30)}@${i}`).toEqual(chunks);
      }
    }
  });

  it("never changes a chunk it declared settled (v1's stability argument)", () => {
    for (const v of vectors.stable) {
      expect(v.final.slice(0, v.declared.length)).toEqual(v.declared);
      for (const chunk of v.declared) expect(v.final).toContain(chunk);
    }
  });

  it("keeps the short opener and does not strand a binding word", () => {
    const text = "Sure, I can do that for you right now, and then I will check the remaining three files in the directory carefully.";
    const [first, ...rest] = sentences(text);
    expect(first.length).toBeLessThanOrEqual(90);
    expect(rest.length).toBeGreaterThan(0);
    const cut = wordCut("one two three four five six seven eight nine and the", 56);
    expect("one two three four five six seven eight nine and the".slice(0, cut).endsWith(" the")).toBe(false);
  });
});
