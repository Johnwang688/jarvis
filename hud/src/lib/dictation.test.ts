import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_MODE, DICTATION_MODES, isMuted, loadMode, mayCapture, nextMode, orbLine, outcomeFor, saveMode,
  sendsOnItsOwn,
} from "./dictation";

function fakeStorage(initial: Record<string, string> = {}) {
  const map = { ...initial };
  return {
    getItem: (k: string) => (k in map ? map[k] : null),
    setItem: (k: string, v: string) => {
      map[k] = v;
    },
    map,
  };
}

describe("dictation mode", () => {
  beforeEach(() => localStorage.clear());

  it("boots a fresh window into REVIEW", () => {
    // The mic-fails-toward-muted reasoning applied to sending: a window that
    // restarts into a mic that sends on its own is the wrong surprise.
    expect(DEFAULT_MODE).toBe("review");
    expect(loadMode(fakeStorage())).toBe("review");
  });

  it("persists the owner's choice and reads it back", () => {
    const s = fakeStorage();
    saveMode("auto", s);
    expect(loadMode(s)).toBe("auto");
  });

  it("falls back to REVIEW on a value it does not recognise", () => {
    expect(loadMode(fakeStorage({ "jarvis.dictation": "banana" }))).toBe("review");
  });

  it("survives storage being blocked", () => {
    const throwing = {
      getItem() { throw new Error("blocked"); },
      setItem() { throw new Error("blocked"); },
    };
    expect(loadMode(throwing as any)).toBe("review");
    expect(() => saveMode("auto", throwing as any)).not.toThrow();
  });

  it("decides an utterance's fate in exactly one place", () => {
    expect(outcomeFor("auto")).toBe("send");
    expect(outcomeFor("review")).toBe("review");
    expect(outcomeFor("off")).toBe("discard");
  });

  it("only AUTO sends on its own", () => {
    expect(sendsOnItsOwn("auto")).toBe(true);
    expect(sendsOnItsOwn("review")).toBe(false);
    expect(sendsOnItsOwn("off")).toBe(false);
  });

  it("OFF is the mute, and nothing else is", () => {
    expect(isMuted("off")).toBe(true);
    expect(isMuted("auto")).toBe(false);
    expect(isMuted("review")).toBe(false);
    expect(mayCapture("off")).toBe(false);
    expect(mayCapture("review")).toBe(true);
  });

  it("says the mic is muted rather than inviting speech, and only when it is", () => {
    // The orb's line took over from the input bar's hint (PR #29): a muted
    // mic must say so, and a live one must never read as muted.
    for (const orb of ["idle", "listening", "thinking", "speaking"]) {
      expect(orbLine({ mode: "off", approvals: 0, orb })).toMatch(/MUTED/);
      expect(orbLine({ mode: "auto", approvals: 0, orb })).not.toMatch(/MUTED/);
      expect(orbLine({ mode: "review", approvals: 0, orb })).not.toMatch(/MUTED/);
    }
    expect(orbLine({ mode: "review", approvals: 0, orb: "idle" })).toBe("");
    expect(orbLine({ mode: "auto", approvals: 0, orb: "listening" })).toBe("listening");
  });

  it("puts the turn's own status first, and a card's question before the mic", () => {
    expect(orbLine({ status: "STT FAILED", mode: "off", approvals: 1, orb: "error" })).toBe("STT FAILED");
    // Under a card it asks for the answer in words, not the orb's state name.
    expect(orbLine({ mode: "review", approvals: 1, orb: "approval" })).toBe("ANSWER THE AUTHORIZATION");
    expect(orbLine({ mode: "off", approvals: 2, orb: "approval" })).toBe("ANSWER THE AUTHORIZATION");
  });

  it("cycles OFF → REVIEW → AUTO → OFF, so unmuting never lands on AUTO", () => {
    expect(nextMode("off")).toBe("review");
    expect(nextMode("review")).toBe("auto");
    expect(nextMode("auto")).toBe("off");
    // A full round visits every mode once.
    const seen = new Set<string>();
    let m = nextMode("off");
    for (let i = 0; i < DICTATION_MODES.length; i++) {
      seen.add(m);
      m = nextMode(m);
    }
    expect([...seen].sort()).toEqual([...DICTATION_MODES].sort());
  });
});
