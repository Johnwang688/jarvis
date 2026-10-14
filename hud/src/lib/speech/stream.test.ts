import { describe, expect, it } from "vitest";
import { MORE_ON_SCREEN, ProseStream, SPOKEN_CAP_CHARS } from "./stream";

const texts = (cs: { text: string }[]) => cs.map((c) => c.text);

/** Stream `text` in deltas of `size`, then settle it with the same text. */
function run(text: string, size: number, settle: string | null = text): string[] {
  const s = new ProseStream();
  const out: string[] = [];
  for (let i = 0; i < text.length; i += size) out.push(...texts(s.feed(text.slice(i, i + size))));
  out.push(...texts(settle === null ? s.close() : s.settle(settle)));
  return out;
}

const REPLY =
  "Right, here is what I found. The approval gate had seven separate ways past it, and every one of them was a second, " +
  "older copy of an idea that the rules module already had right. I have fixed all seven.";

describe("ProseStream", () => {
  it("speaks the same chunks whatever the delta size, and each exactly once", () => {
    const whole = run(REPLY, REPLY.length);
    expect(whole.length).toBeGreaterThan(1);
    for (const size of [1, 3, 7, 20]) expect(run(REPLY, size), `size ${size}`).toEqual(whole);
    expect(whole.join(" ")).toContain("I have fixed all seven.");
  });

  it("starts speaking before the message ends", () => {
    const s = new ProseStream();
    let early: string[] = [];
    for (let i = 0; i < REPLY.length - 10; i += 5) early = early.concat(texts(s.feed(REPLY.slice(i, i + 5))));
    expect(early.length).toBeGreaterThan(0);
  });

  it("never speaks code that streams in the middle of a message", () => {
    const text = "Let me show you. Here is the change.\n```ts\nconst secret = 1;\n```\nThat is the whole change, it works.";
    for (const size of [1, 4, 11]) {
      const out = run(text, size).join(" ");
      expect(out, `size ${size}`).not.toContain("secret");
      expect(out, `size ${size}`).toContain("it works.");
    }
  });

  it("speaks only the unspoken remainder of a settled text", () => {
    // The fast path joins continuations: the settled text is longer than what streamed.
    const s = new ProseStream();
    const streamed = "The first part is long enough to speak on its own. ";
    const got = texts(s.feed(streamed));
    const rest = texts(s.settle(streamed + "The continuation arrived only in the settled text."));
    expect([...got, ...rest].join(" ")).toBe("The first part is long enough to speak on its own. The continuation arrived only in the settled text.");
    // A settled text that differs from the stream adds nothing: nothing is said twice.
    const t = new ProseStream();
    t.feed("One sentence that already streamed out loud, fully. ");
    expect(texts(t.settle("A completely different rewrite of the reply."))).toEqual(["One sentence that already streamed out loud, fully."]);
  });

  it("speaks a settled text that never streamed (no deltas)", () => {
    expect(texts(new ProseStream().settle("Done. The tests pass."))).toEqual(["Done. The tests pass."]);
  });

  it("drops the fast path's cut-off marker, and a diagnostic that arrived as text", () => {
    const out = texts(new ProseStream().settle("This is the end of what fit [cut off at the token limit]"));
    expect(out.join(" ")).toBe("This is the end of what fit");
    expect(texts(new ProseStream().settle("[session not saved: disk full]"))).toEqual([]);
  });

  it("starts each message afresh after close", () => {
    const s = new ProseStream();
    s.feed("Half a sentence with no end");
    expect(texts(s.close())).toEqual(["Half a sentence with no end"]);
    expect(s.open).toBe(false);
    expect(texts(s.settle("Next message."))).toEqual(["Next message."]);
  });

  it("a silenced message says nothing more, until it closes", () => {
    const s = new ProseStream();
    s.feed("Spoken once. ");
    s.silence();
    expect(s.feed("More that must not be spoken at all, ever. And again.")).toEqual([]);
    expect(s.close()).toEqual([]);
    expect(texts(s.settle("Fresh."))).toEqual(["Fresh."]);
  });

  it("caps a very long reply with 'and more on screen'", () => {
    const long = Array.from({ length: 60 }, (_, i) => `Sentence number ${i} is here and reasonably long.`).join(" ");
    const out = run(long, 50);
    expect(out[out.length - 1]).toBe(MORE_ON_SCREEN);
    expect(out.slice(0, -1).join(" ").length).toBeLessThanOrEqual(SPOKEN_CAP_CHARS + 60);
    expect(out.filter((t) => t === MORE_ON_SCREEN)).toHaveLength(1);
  });

  describe("a gap in the event stream", () => {
    const FIRST = "The first sentence was fully spoken before the stream dropped. ";
    const MISSED = "The second sentence arrived only while the stream was down. ";
    const LAST = "And the third came after it returned.";

    it("a settled text after the gap speaks what the gap swallowed, and nothing already spoken", () => {
      const s = new ProseStream();
      const out = texts(s.feed(FIRST + "The sentence in progr"));
      s.gap();
      const rest = texts(s.settle(FIRST + "The sentence in progress ended here. " + MISSED + LAST));
      const all = [...out, ...rest].join(" ");
      expect(all.split("The first sentence was fully spoken").length - 1).toBe(1);
      expect(all).toContain("The sentence in progress ended here.");
      expect(all).toContain("only while the stream was down.");
      expect(all).toContain("And the third came after it returned.");
    });

    it("with no settled text, the unfinished sentence is dropped, not spoken broken", () => {
      const s = new ProseStream();
      const out = texts(s.feed(FIRST + "Half a sentence that never finishe"));
      s.gap();
      expect([...out, ...texts(s.close())].join(" ")).not.toContain("never finishe");
    });

    it("a delta after the gap starts clean, and a later settled text adds nothing twice", () => {
      const s = new ProseStream();
      const a = texts(s.feed(FIRST + "Half a sentence that never finishe"));
      s.gap();
      const b = texts(s.feed("d. A sentence after the gap, long enough to be heard. "));
      const c = texts(s.settle("Not the same text at all."));
      const all = [...a, ...b, ...c].join(" ");
      expect(all).not.toContain("never finishe");
      expect(all.split("A sentence after the gap").length - 1).toBe(1);
    });

    it("an unclosed fence stays closed across the gap", () => {
      const s = new ProseStream();
      const a = texts(s.feed("Here it is.\n```py\nsecret_code = 1\n"));
      s.gap();
      const b = texts(s.feed("more_secret = 2\n```\nAfter the code, spoken. And more."));
      const all = [...a, ...b, ...texts(s.close())].join(" ");
      expect(all).not.toMatch(/secret/);
      expect(all).toContain("After the code");
    });
  });
});
