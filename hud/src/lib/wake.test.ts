import { describe, expect, it } from "vitest";
import { WAKE_PATTERNS, WakeGate, compileWake, matchesWake } from "./wake";

// The half of this suite that matters is the QUIET list: a wake hit takes the
// owner's words mid-sentence, so a pattern that fires on ordinary conversation
// is worse than one that never fires.
const QUIET = [
  "yahoo finance", "big yacht", "jarvisson", "the jar visible from here",
  "nathan is on the call", "netanya beach", "let me check the java version",
];

describe("wake matching", () => {
  it("fires on the built-in phrase", () => {
    for (const said of ["jarvis", "hey jarvis what's the weather", "JARVIS!"])
      expect(matchesWake(said)).toBe(true);
  });

  it("stays silent on ordinary speech", () => {
    for (const said of QUIET) expect(matchesWake(said)).toBe(false);
  });

  it("anchors every built-in pattern at both ends", () => {
    for (const re of WAKE_PATTERNS) {
      expect(re.source.startsWith("\\b")).toBe(true);
      expect(re.source.endsWith("\\b")).toBe(true);
    }
  });

  it("anchors an avatar's raw source on the way through", () => {
    const [re] = compileWake(["hoot"]);
    expect(re.source).toBe("\\bhoot\\b");
    expect(matchesWake("hey hoot", [re])).toBe(true);
    // Unanchored, this fires on a substring of ordinary conversation and takes
    // the owner's words mid-sentence.
    expect(matchesWake("stop hooting about it", [re])).toBe(false);
    expect(matchesWake("shoot", [re])).toBe(false);
  });

  it("leaves an already-anchored source alone rather than doubling it", () => {
    const [re] = compileWake(["\\bbig ?ya-?h(oo|u|o)\\b"]);
    expect(re.source).toBe("\\bbig ?ya-?h(oo|u|o)\\b");
    expect(matchesWake("big yahu", [re])).toBe(true);
    expect(matchesWake("big yahoo now", [re])).toBe(true);
    expect(matchesWake("yahoo finance", [re])).toBe(false);
  });

  it("degrades to the built-in rather than leaving him unsummonable", () => {
    // A regex that will not compile, an empty list, and a nullish list all
    // land on the phrase that always works.
    expect(compileWake(["(unclosed"])).toEqual(WAKE_PATTERNS);
    expect(compileWake([])).toEqual(WAKE_PATTERNS);
    expect(compileWake(null)).toEqual(WAKE_PATTERNS);
  });
});

describe("recognizer de-dup", () => {
  // `onresult` is level-triggered: Chrome delivers a growing interim
  // transcript, then the same words once more as the final result.
  it("fires once per phrase, not once per delivery", () => {
    const g = new WakeGate();
    let t = 1000;
    // interim "jar", interim "jarvis", final "jarvis" — all segment 0
    expect(g.shouldFire(0, (t += 40))).toBe(true);
    expect(g.shouldFire(0, (t += 40))).toBe(false);
    expect(g.shouldFire(0, (t += 40))).toBe(false);
  });

  it("does not re-fire while the owner carries on talking in that segment", () => {
    const g = new WakeGate();
    g.shouldFire(0, 1000);
    for (let i = 0; i < 6; i++) expect(g.shouldFire(0, 1000 + i * 120)).toBe(false);
  });

  it("fires again on a new phrase in a later segment past the floor", () => {
    const g = new WakeGate();
    expect(g.shouldFire(0, 1000)).toBe(true);
    expect(g.shouldFire(1, 1400)).toBe(false); // inside the 1500ms floor
    expect(g.shouldFire(2, 3200)).toBe(true);
  });

  it("records the decision even when the floor suppresses it", () => {
    // A hit suppressed by the time floor must not fire late when that same
    // segment is revised.
    const g = new WakeGate();
    g.shouldFire(0, 1000);
    expect(g.shouldFire(1, 1100)).toBe(false);
    expect(g.shouldFire(1, 9000)).toBe(false);
  });

  it("treats a lower index as a restarted session, not an old phrase", () => {
    // onend restarts recognition every few seconds and indices restart with
    // it; without this rule the wake word goes silent until as many segments
    // have accumulated again.
    const g = new WakeGate();
    g.shouldFire(4, 1000);
    expect(g.shouldFire(0, 5000)).toBe(true);
  });

  it("restart() re-arms without waiting for a lower index", () => {
    const g = new WakeGate();
    g.shouldFire(3, 1000);
    g.restart();
    expect(g.shouldFire(0, 5000)).toBe(true);
  });
});
