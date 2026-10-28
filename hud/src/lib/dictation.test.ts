import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_MODE, DICTATION_MODES, isMuted, loadMode, mayCapture, mutedDictation, nextMode, orbLine, outcomeFor, saveMode,
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
    // The one thing OFF lets upload: the talk key's dictation, claimed while OFF (2026-10-27).
    expect(mutedDictation("ptt", "off")).toBe(true);
    expect(mutedDictation("ptt", "review")).toBe(false);
    expect(mutedDictation("wake", "off")).toBe(false);
    expect(mutedDictation("follow_up", "off")).toBe(false);
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

  it("says when Jarvis is speaking, over a turn's status but never over the mic's or a card's", () => {
    const talking = { state: "playing" as const, label: "Fix the build" };
    expect(orbLine({ mode: "review", approvals: 0, orb: "idle", speech: talking })).toBe("SPEAKING · Fix the build");
    // Mid-task updates are read while the turn runs.
    expect(orbLine({ status: "RESPONDING", mode: "auto", approvals: 0, orb: "thinking", speech: talking })).toBe("SPEAKING · Fix the build");
    expect(orbLine({ status: "RUNNING · bash", mode: "auto", approvals: 0, orb: "tool", speech: talking })).toBe("SPEAKING · Fix the build");
    expect(orbLine({ mode: "review", approvals: 0, orb: "idle", speech: { state: "paused" } })).toBe("SPEECH PAUSED");
    // The microphone's own lines and a pending card still win.
    for (const status of ["LISTENING · SPEAK NOW", "TRANSCRIBING", "STT FAILED", "DIDN'T CATCH THAT"]) {
      expect(orbLine({ status, mode: "auto", approvals: 0, orb: "listening", speech: talking })).toBe(status);
    }
    expect(orbLine({ mode: "review", approvals: 1, orb: "approval", speech: talking })).toBe("ANSWER THE AUTHORIZATION");
    // Idle speech says nothing; and a muted mic is not mistaken for muted speech.
    expect(orbLine({ mode: "off", approvals: 0, orb: "idle", speech: { state: "idle" } })).toBe("MIC MUTED");
    expect(orbLine({ mode: "off", approvals: 0, orb: "idle", speech: talking })).not.toMatch(/MUTED/);
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


describe("orbLine and the voice-check label (V-14, M3)", () => {
  it("adds the label to every line, so it is never absent while the check is not judging", () => {
    const cases: Parameters<typeof orbLine>[0][] = [
      { mode: "auto", approvals: 0, orb: "idle" },
      { mode: "auto", approvals: 0, orb: "listening" },
      { mode: "off", approvals: 0, orb: "idle" },
      { mode: "auto", approvals: 1, orb: "idle" },
      { mode: "auto", approvals: 0, orb: "idle", status: "TRANSCRIBING" },
      { mode: "auto", approvals: 0, orb: "idle", status: "RESPONDING", speech: { state: "playing", label: "chat" } },
    ];
    for (const c of cases) {
      const plain = orbLine(c);
      expect(orbLine({ ...c, voiceLabel: "" })).toBe(plain);
      expect(orbLine({ ...c, voiceLabel: "GUEST VOICE" })).toBe(plain ? `${plain} · GUEST VOICE` : "GUEST VOICE");
      expect(orbLine({ ...c, voiceLabel: "VOICE CHECK OFF (not enrolled)" })).toContain("VOICE CHECK OFF (not enrolled)");
    }
  });
  it("does not change what the rest of the line means", () => {
    expect(orbLine({ mode: "off", approvals: 0, orb: "idle", voiceLabel: "GUEST VOICE" })).toMatch(/^MIC MUTED/);
    expect(orbLine({ mode: "auto", approvals: 1, orb: "idle", voiceLabel: "GUEST VOICE" })).toMatch(/^ANSWER THE AUTHORIZATION/);
  });
});
