// V9, the isolation that matters most: no spoken word ever answers an authorization card
// (decision V-6; CLAUDE.md, "Speech never authorizes an approval"). Three layers:
//   1. the modules that hear and act on speech have no import, name or route that reaches
//      an approval, and the ones that do reach the API reach only the speech routes;
//   2. a hostile list of phrases -- "Jarvis approve", "Jarvis yes", "approve" -- is not a
//      command, and touches nothing with a card up or without one;
//   3. in the window, the code that places a transcript calls no approval function.
// The browser check (tests/face/hud_v2_barge_check.py) adds the end-to-end one: a card up,
// those phrases spoken, zero approval requests and zero uploads.

import { describe, expect, it } from "vitest";
import appSource from "../../App.tsx?raw";
import { CommandRouter, parseSpeechCommand, type CommandEffects } from "./commands";

const strip = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

const speechModules = import.meta.glob("./*.ts", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const others = import.meta.glob(["../selfvoice.ts", "../capture.ts", "../vad.ts", "../recorder.ts", "../../components/SelfVoice.tsx",
  "../../components/SpeechStrip.tsx", "../../components/VoiceChip.tsx", "../../components/ReadAloud.tsx"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

const FORBIDDEN = /approv|resolveApproval|\bdecide\b|onDecide|\/approvals/i;

describe("nothing that hears or acts on speech can reach an approval", () => {
  const files = [...Object.entries(speechModules), ...Object.entries(others)]
    .filter(([f]) => !f.endsWith(".test.ts") && !f.endsWith("testkit.ts"));

  it("covers the modules of this package", () => {
    const names = files.map(([f]) => f.split("/").pop());
    for (const n of ["commands.ts", "barge.ts", "controller.ts", "selfvoice.ts", "capture.ts", "player.ts"]) expect(names).toContain(n);
  });

  it.each(files)("%s has no approval name, import or route", (_f, src) => {
    expect(strip(src)).not.toMatch(FORBIDDEN);
  });

  it("only the speech index touches the API, and only its speech routes", () => {
    const touching = files.filter(([, src]) => /from\s+["'](\.\.\/)+api["']/.test(strip(src))).map(([f]) => f);
    expect(touching).toEqual(["./index.ts"]);
    const index = strip(speechModules["./index.ts"]);
    const used = [...index.matchAll(/\bapi\.(\w+)/g)].map((m) => m[1]).sort();
    expect(used).toEqual(["say", "speechSummary", "speechVoice", "speechVoices"]);
  });
});

describe("approval-shaped phrases are not commands and touch nothing", () => {
  const PHRASES = [
    "Jarvis approve", "Jarvis yes", "approve", "yes", "Jarvis allow", "Jarvis authorize", "Jarvis deny", "Jarvis always allow",
    "Jarvis, approve it", "Jarvis confirm", "Jarvis go ahead", "Jarvis accept", "Jarvis approve the request", "Approve", "Allow",
    "Jarvis yes allow it", "Jarvis okay approve",
  ];

  function hostile() {
    // Every property read or call beyond the four playback effects is recorded: a router that reached for
    // anything else would show up here.
    const touched: string[] = [];
    const calls: string[] = [];
    const target: CommandEffects = {
      pause: () => calls.push("pause"), resume: () => calls.push("resume"), skip: () => calls.push("skip"), stop: () => calls.push("stop"),
    };
    const effects = new Proxy(target, {
      get(t, k) { if (!(k in t)) touched.push(String(k)); return (t as never)[k]; },
    });
    return { touched, calls, router: new CommandRouter(effects) };
  }

  it.each(PHRASES)("%s: no command, no effect, with a card up or not", (text) => {
    expect(parseSpeechCommand(text)).toBeNull();
    for (const card of [true, false]) {
      const h = hostile();
      expect(h.router.fromTranscript(text, { card, stale: false })).toEqual({ kind: "none" });
      expect(h.calls).toEqual([]);
      expect(h.touched).toEqual([]);
    }
  });

  it("a real command under a card runs nothing either", () => {
    const h = hostile();
    for (const w of ["pause", "resume", "skip", "stop"]) h.router.fromTranscript(`Jarvis ${w}`, { card: true, stale: false });
    expect(h.calls).toEqual([]);
    expect(h.touched).toEqual([]);
  });

  it("a command with no card runs only its own effect and reads nothing else", () => {
    const h = hostile();
    h.router.fromTranscript("Jarvis stop", { card: false, stale: false });
    expect(h.calls).toEqual(["stop"]);
    expect(h.touched).toEqual([]);
  });
});

describe("in the window, placing a transcript calls no approval function", () => {
  const body = (() => {
    const a = appSource.indexOf("const placeTranscripts = useCallback(");
    const b = appSource.indexOf("// Words that had no chat to go to come back");
    return strip(appSource.slice(a, b));
  })();

  it("the placement code is found", () => {
    expect(body.length).toBeGreaterThan(500);
    expect(body).toContain("commands.fromTranscript(");
  });

  it("and has no approval call", () => {
    expect(body).not.toMatch(/\bdecide\s*\(|resolveApproval|approval_drop|\/approv/);
  });

  it("the router is told whether a card is up, from the window's own approvals, and a stale clip from the clip itself", () => {
    // Mutating either to a constant (a card is never up) would let a command through under a card.
    expect(body).toContain("commands.fromTranscript(i.text, { card: live.current.approvals.length > 0, stale: i.late || i.retried })");
  });

  it("a command's effects are the four controller calls, and 'stop' reaches no agent turn", () => {
    const m = appSource.match(/const commands = useMemo\(\(\) => new CommandRouter\(\{([\s\S]*?)\}\), \[speech\]\);/);
    expect(m).not.toBeNull();
    const inner = strip(m![1]).replace(/\s+/g, " ").trim();
    expect(inner).toBe("pause: () => speech.controller.pause(), resume: () => speech.controller.resume(), skip: () => speech.controller.skip(), stop: () => speech.controller.stop(),");
    expect(inner).not.toMatch(/api\.|interrupt|stopTurn|send\(/);
  });
});
