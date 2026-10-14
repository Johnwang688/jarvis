import { describe, expect, it } from "vitest";
import type { SelfFrame } from "../selfvoice";
import { OwnerMark, hisOwnWord } from "./guard";

const frame = (active: boolean, self: boolean): SelfFrame => ({ active, playing: active, self, reason: active ? "echo" : "clear" });

describe("whose word was it", () => {
  it("his voice audible and no owner speech released over it: his own word", () => {
    expect(hisOwnWord(true, false)).toBe(true);
  });
  it("his voice audible but the gate released the owner over it: the owner's", () => {
    expect(hisOwnWord(true, true)).toBe(false);
  });
  it("his voice not audible: nobody's but the room's, so not his", () => {
    expect(hisOwnWord(false, false)).toBe(false);
    expect(hisOwnWord(false, true)).toBe(false);
  });
});

describe("OwnerMark", () => {
  it("remembers a frame released as the owner's over his voice, for the look-back", () => {
    const m = new OwnerMark();
    expect(m.within(1000, 5000)).toBe(false);
    m.note(frame(true, false), 5000);
    expect(m.within(1000, 5900)).toBe(true);
    expect(m.within(1000, 6100)).toBe(false);
  });
  it("his own frames, a clear room and a missing verdict mark nothing", () => {
    const m = new OwnerMark();
    m.note(frame(true, true), 100);
    m.note(frame(false, false), 200);
    m.note(null, 300);
    m.note(undefined, 400);
    expect(m.within(1e9, 500)).toBe(false);
  });
  it("reset forgets", () => {
    const m = new OwnerMark();
    m.note(frame(true, false), 10);
    m.reset();
    expect(m.within(1e9, 20)).toBe(false);
  });
});
