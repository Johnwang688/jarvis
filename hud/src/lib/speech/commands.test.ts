import { describe, expect, it } from "vitest";
import { CommandRouter, parseSpeechCommand, type CommandEffects } from "./commands";

function effects() {
  const calls: string[] = [];
  const e: CommandEffects = {
    pause: () => calls.push("pause"), resume: () => calls.push("resume"),
    skip: () => calls.push("skip"), stop: () => calls.push("stop"),
  };
  return { calls, e };
}

describe("parseSpeechCommand: the whole utterance is the command", () => {
  it.each([
    ["Jarvis pause", "pause"], ["Jarvis, pause.", "pause"], ["jarvis resume", "resume"], ["Jarvis skip!", "skip"],
    ["JARVIS STOP", "stop"], ["Jarvis. Stop.", "stop"], ["Hey Jarvis, stop", "stop"], ["Okay Jarvis pause please", "pause"],
    ["Uh, Jarvis, skip now", "skip"], ["jarvis  resume", "resume"], ["Jarvis — pause", "pause"], ["Jarvis stop,", "stop"],
  ])("%s -> %s", (text, want) => {
    expect(parseSpeechCommand(text)).toBe(want);
  });

  it.each([
    "Pause the build", "pause", "stop", "Jarvis", "Jarvis pause the build", "Jarvis, stop the build", "Jarvis stop it",
    "I said Jarvis stop", "Jarvis please stop talking", "Stop, Jarvis, the build is broken", "Jarvis stop stop", "Jarvis pause resume",
    "Jarvis paws", "Jarvis stopping", "Jarvis skip this one", "Jarvis, can you pause", "the jarvis pause", "",
    "Jarvis fix the pause button", "Travis stop", "Jarvis skip ahead to the end",
  ])("%j is a message, not a command", (text) => {
    expect(parseSpeechCommand(text)).toBeNull();
  });

  it("is not an answer to anything: approval-shaped phrases are not commands", () => {
    for (const text of ["Jarvis approve", "Jarvis yes", "Jarvis allow", "Jarvis authorize", "Jarvis deny", "Jarvis always allow", "approve",
      "yes", "Jarvis approve it", "Jarvis confirm", "Jarvis go ahead", "Jarvis accept", "Jarvis ok", "Jarvis okay"]) {
      expect(parseSpeechCommand(text), text).toBeNull();
    }
  });

  it("refuses an absurd input without work", () => {
    expect(parseSpeechCommand("Jarvis pause " + "x".repeat(500))).toBeNull();
    expect(parseSpeechCommand(undefined as unknown as string)).toBeNull();
    expect(parseSpeechCommand(42 as unknown as string)).toBeNull();
  });
});

describe("CommandRouter: four calls on his voice", () => {
  it("each command is exactly one call", () => {
    const { calls, e } = effects();
    const r = new CommandRouter(e);
    for (const c of ["pause", "resume", "skip", "stop"]) {
      expect(r.fromTranscript(`Jarvis ${c}`, { card: false, stale: false })).toEqual({ kind: "ran", command: c });
    }
    expect(calls).toEqual(["pause", "resume", "skip", "stop"]);
  });

  it("a message runs nothing and says so, so the caller sends it", () => {
    const { calls, e } = effects();
    const r = new CommandRouter(e);
    expect(r.fromTranscript("Jarvis, stop the build", { card: false, stale: false })).toEqual({ kind: "none" });
    expect(r.fromTranscript("pause the build", { card: false, stale: false })).toEqual({ kind: "none" });
    expect(calls).toEqual([]);
  });

  it("under a card nothing is obeyed (Q10: commands are local-only there, and V11 is not built)", () => {
    const { calls, e } = effects();
    const r = new CommandRouter(e);
    for (const c of ["pause", "resume", "skip", "stop"]) {
      expect(r.fromTranscript(`Jarvis ${c}`, { card: true, stale: false })).toEqual({ kind: "refused", command: c, why: "card" });
    }
    expect(calls).toEqual([]);
  });

  it("a late or retried clip is not obeyed", () => {
    const { calls, e } = effects();
    const r = new CommandRouter(e);
    expect(r.fromTranscript("Jarvis stop", { card: false, stale: true })).toEqual({ kind: "refused", command: "stop", why: "stale" });
    expect(calls).toEqual([]);
  });

  it("the card wins over a stale clip in what it reports", () => {
    const { e } = effects();
    expect(new CommandRouter(e).fromTranscript("Jarvis stop", { card: true, stale: true })).toMatchObject({ why: "card" });
  });

  it("the V11 seam: a local command under a card is refused until V11 says it may, and then runs only the same four calls", () => {
    const { calls, e } = effects();
    const r = new CommandRouter(e);
    expect(r.fromLocal("pause", { card: true })).toMatchObject({ kind: "refused", why: "card" });
    expect(calls).toEqual([]);
    r.setLocalUnderCard(true);
    expect(r.fromLocal("pause", { card: true })).toEqual({ kind: "ran", command: "pause" });
    expect(calls).toEqual(["pause"]);
    // ...and a transcript under a card is still refused: nothing may be uploaded there to begin with.
    expect(r.fromTranscript("Jarvis pause", { card: true, stale: false })).toMatchObject({ kind: "refused", why: "card" });
    expect(calls).toEqual(["pause"]);
  });

  it("a command word from outside the four is not run", () => {
    const { calls, e } = effects();
    expect(new CommandRouter(e).fromLocal("approve" as never, { card: false })).toEqual({ kind: "none" });
    expect(calls).toEqual([]);
  });
});
