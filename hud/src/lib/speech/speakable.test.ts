import { describe, expect, it } from "vitest";
import vectors from "./vectors.json";
import { speakable } from "./speakable";

describe("speakable() against voice.speakable's vectors", () => {
  it("answers exactly what the Python answers for every markdown construct", () => {
    expect(vectors.speakable.length).toBeGreaterThan(20);
    for (const v of vectors.speakable) expect(speakable(v.in), JSON.stringify(v.in)).toBe(v.out);
  });

  it("is idempotent, and a fence-only reply speaks nothing", () => {
    for (const v of vectors.speakable) expect(speakable(v.out)).toBe(v.out);
    expect(speakable("```\nonly code\n```")).toBe("");
    expect(speakable("~~~\nonly code\n~~~")).toBe("");
  });
});
