import { HeldBack, joined, nextNonce, pendingAfter, type GiveBack } from "./giveback";

const file = (name: string) => ({ name, mime: "text/plain", data_b64: "" });

describe("give-back", () => {
  it("mints a fresh nonce every time, even within one millisecond", () => {
    const a = nextNonce();
    const b = nextNonce();
    expect(b).toBeGreaterThan(a);
  });

  it("keeps only what the box has not taken, oldest first when joined", () => {
    const list: GiveBack[] = [
      { text: "one", files: [], nonce: 1 },
      { text: "", files: [file("a.txt")], nonce: 2 },
      { text: "three", files: [], nonce: 3 },
    ];
    expect(pendingAfter(list, 1).map((g) => g.nonce)).toEqual([2, 3]);
    expect(pendingAfter(list, 3)).toEqual([]);
    expect(joined(pendingAfter(list, 0))).toEqual({ text: "one\nthree", files: [file("a.txt")] });
  });

  it("holds words per thread until that thread is opened, once", () => {
    const held = new HeldBack();
    held.hold("A", "first", []);
    held.hold("A", "second", [file("b.txt")]);
    held.hold("B", "other", []);
    held.hold("C", "", []);
    expect(held.has("C")).toBe(false);
    expect(held.take("A")).toEqual({ text: "first\nsecond", files: [file("b.txt")] });
    expect(held.take("A")).toBeNull();
    expect(held.take("B")).toEqual({ text: "other", files: [] });
  });
});
