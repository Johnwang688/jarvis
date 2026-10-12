import { describe, expect, it } from "vitest";
import { quotedName, shortThreadId, threadLabel, UNTITLED_NEW, UNTITLED_OLD } from "./threadTitle";

describe("quotedName", () => {
  it("is the title, quoted; an untitled chat's label carries its id", () => {
    expect(quotedName({ id: "a1b2c3d4", title: "Robot Plans", turns: 2 })).toBe("“Robot Plans”");
    expect(quotedName({ id: "a1b2c3d4", title: "", turns: 2 })).toBe("“Untitled” (a1b2c3d4)");
  });
});

describe("threadLabel", () => {
  it("is the title when there is one, whitespace collapsed", () => {
    expect(threadLabel({ title: "Fixing the  drivetrain\nbuild", turns: 3 }))
      .toEqual({ text: "Fixing the drivetrain build", untitled: false });
  });

  it("is never the id: New thread before a turn, Untitled after", () => {
    expect(threadLabel({ title: "", turns: 0 })).toEqual({ text: UNTITLED_NEW, untitled: true });
    expect(threadLabel({ title: "   ", turns: 0 })).toEqual({ text: UNTITLED_NEW, untitled: true });
    expect(threadLabel({ title: "", turns: 4 })).toEqual({ text: UNTITLED_OLD, untitled: true });
    expect(threadLabel({ title: undefined as unknown as string, turns: undefined as unknown as number }))
      .toEqual({ text: UNTITLED_NEW, untitled: true });
  });

  it("leaves markup as text", () => {
    expect(threadLabel({ title: "<b>bold</b> & co", turns: 1 }).text).toBe("<b>bold</b> & co");
  });
});

describe("shortThreadId", () => {
  it("keeps the daemon's eight-character ids whole", () => {
    expect(shortThreadId("1a2b3c4d")).toBe("1a2b3c4d");
    expect(shortThreadId("t1")).toBe("t1");
  });

  it("shortens a long id", () => {
    expect(shortThreadId("0123456789abcdef0123")).toBe("01234567…");
  });
});
