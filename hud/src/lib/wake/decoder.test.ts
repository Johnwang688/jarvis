import { describe, expect, it } from "vitest";
import { ContextGraph } from "./contextGraph";
import { KeywordStream, type Joint } from "./decoder";

// A scripted network: frame t's encoder vector is [t], and `plan[t]` is the token it "hears" with
// probability `p` (everything else shares the rest). Blank is token 0.
const VOCAB = 12;
const scripted = (plan: number[], p = 0.99): Joint => async (contexts, frame) => {
  const t = Math.round(frame[0]);
  const hear = plan[t] ?? 0;
  const rest = (1 - p) / (VOCAB - 1);
  const row = Float32Array.from({ length: VOCAB }, (_, k) => Math.log(k === hear ? p : rest));
  return contexts.map(() => row.slice());
};
const frames = (n: number, from = 0) => Array.from({ length: n }, (_, i) => Float32Array.of(from + i));
const kw = (ids: number[], phrase: string, threshold = 0.25) => ({ ids, phrase, boost: 1, threshold });

describe("the keyword decoder", () => {
  it("fires once the keyword is spelled and followed by more than one blank frame", async () => {
    const s = new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC")]), scripted([0, 3, 4, 5, 0, 0, 0]));
    const hit = await s.decode(frames(7));
    expect(hit?.phrase).toBe("ABC");
  });

  it("waits for the trailing blanks: one is not enough, and it fires on the second", async () => {
    const plan = [0, 3, 4, 5, 0, 0];
    const s = new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC")]), scripted(plan));
    expect(await s.decode(frames(5))).toBeNull(); // the word, then one blank
    const hit = await s.decode(frames(1, 5)); // the second blank
    expect(hit?.phrase).toBe("ABC");
  });

  it("does not fire on part of a keyword, or on a different order", async () => {
    const g = new ContextGraph([kw([3, 4, 5], "ABC")]);
    expect(await new KeywordStream(g, scripted([0, 3, 4, 0, 0, 0, 0])).decode(frames(7))).toBeNull();
    expect(await new KeywordStream(g, scripted([0, 3, 5, 4, 0, 0, 0])).decode(frames(7))).toBeNull();
    expect(await new KeywordStream(g, scripted([0, 6, 7, 8, 0, 0, 0])).decode(frames(7))).toBeNull();
  });

  it("holds each keyword to its threshold: the mean probability of its tokens", async () => {
    // Each token heard with p = 0.3: a 0.25 keyword fires, a 0.4 one does not.
    const plan = [0, 3, 4, 5, 0, 0, 0];
    expect((await new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC", 0.25)]), scripted(plan, 0.3)).decode(frames(7)))?.phrase).toBe("ABC");
    expect(await new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC", 0.4)]), scripted(plan, 0.3)).decode(frames(7))).toBeNull();
  });

  it("starts over after a hit and hears the next one", async () => {
    const plan = [0, 3, 4, 5, 0, 0, 0, 0, 3, 4, 5, 0, 0, 0];
    const s = new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC")]), scripted(plan));
    expect((await s.decode(frames(7)))?.phrase).toBe("ABC");
    expect((await s.decode(frames(7, 7)))?.phrase).toBe("ABC");
  });

  it("tells two keywords that share a start apart", async () => {
    const g = new ContextGraph([kw([3, 4, 5], "ABC"), kw([3, 4, 6], "ABD")]);
    expect((await new KeywordStream(g, scripted([0, 3, 4, 6, 0, 0, 0])).decode(frames(7)))?.phrase).toBe("ABD");
    expect((await new KeywordStream(g, scripted([0, 3, 4, 5, 0, 0, 0])).decode(frames(7)))?.phrase).toBe("ABC");
  });

  it("counts the blank frames after the last word, and a reset forgets them", async () => {
    const s = new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC")]), scripted([0, 0, 0, 0, 0]));
    await s.decode(frames(5));
    expect(s.trailingBlanks).toBe(5);
    s.reset();
    expect(s.trailingBlanks).toBe(0);
  });

  it("treats <unk> like blank", async () => {
    const s = new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC")]), scripted([2, 2, 2, 2, 2]));
    await s.decode(frames(5));
    expect(s.trailingBlanks).toBe(5);
  });

  it("scores every live path in one call (the network is batched, not called per path)", async () => {
    let calls = 0; let widest = 0;
    const joint = scripted([0, 3, 4, 5, 0, 0, 0], 0.6);
    const s = new KeywordStream(new ContextGraph([kw([3, 4, 5], "ABC")]), async (c, f) => { calls++; widest = Math.max(widest, c.length); return joint(c, f); });
    await s.decode(frames(7));
    expect(calls).toBe(7);
    expect(widest).toBeGreaterThan(1);
    expect(widest).toBeLessThanOrEqual(4);
  });
});
