import { describe, expect, it } from "vitest";
import { ContextGraph } from "./contextGraph";

const kw = (ids: number[], phrase: string, boost = 1, threshold = 0.25) => ({ ids, phrase, boost, threshold });

describe("the keyword trie", () => {
  it("follows a keyword's tokens, earning the boost per token, and matches at its last", () => {
    const g = new ContextGraph([kw([3, 4, 5], "ABC", 2)]);
    let state = g.root;
    const scores: number[] = [];
    for (const t of [3, 4, 5]) { const [s, next] = g.forwardOneStep(state, t); scores.push(s); state = next; }
    // The last token also carries the keyword's whole output score (2 + 2 + 2), as in the original's strict mode.
    expect(scores).toEqual([2, 2, 2 + 6]);
    expect(g.isMatched(state)?.phrase).toBe("ABC");
    expect(g.isMatched(state)?.level).toBe(3);
  });

  it("takes the boost back when the path falls off, so a wrong turn nets nothing", () => {
    const g = new ContextGraph([kw([1, 2, 3], "X", 1)]);
    let state = g.root;
    let total = 0;
    for (const t of [1, 2, 9]) { const [s, next] = g.forwardOneStep(state, t); total += s; state = next; }
    expect(total).toBe(0);
    expect(state.token).toBe(-1); // back at the root
    expect(g.isMatched(state)).toBeNull();
  });

  it("falls back along fail links to the longest suffix that is still a prefix of a keyword", () => {
    const g = new ContextGraph([kw([1, 2], "AB"), kw([2, 3], "BC")]);
    let state = g.root;
    const seen: (string | undefined)[] = [];
    for (const t of [1, 2, 3]) {
      const [, next] = g.forwardOneStep(state, t);
      state = next;
      seen.push(g.isMatched(state)?.phrase);
    }
    expect(seen).toEqual([undefined, "AB", "BC"]);
  });

  it("a keyword that ends inside a longer one is matched through the output link", () => {
    const g = new ContextGraph([kw([1, 2, 3], "ABC"), kw([2, 3], "BC")]);
    let state = g.root;
    for (const t of [1, 2, 3]) state = g.forwardOneStep(state, t)[1];
    expect(g.isMatched(state)?.phrase).toBe("ABC");
    // 9 then 2, 3 on its own: the suffix keyword
    state = g.root;
    for (const t of [9, 2, 3]) state = g.forwardOneStep(state, t)[1];
    expect(g.isMatched(state)?.phrase).toBe("BC");
  });

  it("keeps each keyword's own threshold", () => {
    const g = new ContextGraph([kw([1, 2], "AB", 1, 0.2), kw([1, 3], "AC", 1, 0.6)]);
    const a = g.forwardOneStep(g.forwardOneStep(g.root, 1)[1], 2)[1];
    const c = g.forwardOneStep(g.forwardOneStep(g.root, 1)[1], 3)[1];
    expect(g.isMatched(a)?.acThreshold).toBe(0.2);
    expect(g.isMatched(c)?.acThreshold).toBe(0.6);
  });
});
