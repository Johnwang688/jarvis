import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_MODE, isMuted, loadMode, mayCapture, outcomeFor, saveMode,
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
});
