// V11's two promises, held in source and in behaviour:
//   PRIVACY   the wake word is heard in this window. Nothing constructs Chrome's cloud recognizer, the keyword worker
//             has no way to send anything anywhere (its one fetch is its own pinned model files, its only output a
//             hit), and the stage never posts audio back or out.
//   APPROVALS no hit, local or not, reaches an approval: the modules have no approval name, import or route; a spoken
//             command from the local recognizer runs four calls on his voice and nothing else, with a card up only if
//             the owner left that on; "Jarvis approve" is not even a keyword.
import { describe, expect, it } from "vitest";
import appSource from "../../App.tsx?raw";
import { CommandRouter, type CommandEffects } from "../speech/commands";
import { COMMAND_PIECES, WAKE_PIECES } from "./keywords";

const strip = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

const wakeModules = import.meta.glob("./*.ts", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const hudSources = import.meta.glob(["../../**/*.ts", "../../**/*.tsx", "!../../**/*.test.ts", "!../../**/*.test.tsx"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const files = Object.entries(wakeModules).filter(([f]) => !f.endsWith(".test.ts") && !f.endsWith("testkit.ts"));
const settingsUi = strip((import.meta.glob("../../components/WakeWord.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>)["../../components/WakeWord.tsx"]);

describe("privacy: the room's audio is not sent to a speech service", () => {
  it("nothing in the window names a speech recognizer (code or comment-stripped)", () => {
    const offenders = Object.entries(hudSources).filter(([, src]) => /SpeechRecognition/.test(strip(src))).map(([f]) => f);
    expect(offenders).toEqual([]);
  });

  it("covers the modules of this package", () => {
    const names = files.map(([f]) => f.split("/").pop());
    for (const n of ["engine.ts", "stage.ts", "worker.ts", "decoder.ts", "fbank.ts", "keywords.ts", "pins.ts", "protocol.ts"]) expect(names).toContain(n);
  });

  it.each(files)("%s has no network or messaging primitive of its own", (f, src) => {
    const code = strip(src);
    expect(code).not.toMatch(/WebSocket|XMLHttpRequest|sendBeacon|EventSource|importScripts|navigator\.mediaDevices|getUserMedia|RTCPeerConnection|BroadcastChannel/);
    if (!f.endsWith("worker.ts")) expect(code).not.toMatch(/\bfetch\s*\(/);
    expect(code).not.toMatch(/from\s+["'](\.\.\/)+api["']|document\./);
    if (!f.endsWith("wakeSettings.ts")) expect(code).not.toMatch(/localStorage/); // only the owner's two switches are stored
  });

  it("the worker's single fetch is one of its own pinned model files", () => {
    const code = strip(wakeModules["./worker.ts"]);
    expect([...code.matchAll(/\bfetch\s*\(([^)]*)\)/g)].map((m) => m[1].split(",")[0].trim())).toEqual(["URLS[file]"]);
    expect(code).toMatch(/const URLS = \{ encoder: encoderUrl, decoder: decoderUrl, joiner: joinerUrl, tokens: tokensUrl \}/);
    for (const url of ["encoder", "decoder", "joiner"]) expect(code).toContain(`vendor/kws-zipformer-gigaspeech-3.3M/${url}.int8.onnx?url`);
  });

  it("the worker's output is a hit, a progress count, ready or a failure code, and never samples", () => {
    const code = strip(wakeModules["./worker.ts"]);
    const kinds = [...code.matchAll(/post\(\{\s*type:\s*"(\w+)"/g)].map((m) => m[1]);
    expect(new Set(kinds)).toEqual(new Set(["ready", "failed", "hit", "progress"]));
    expect(code).not.toMatch(/postMessage\(\s*\{[^}]*samples/);
    expect(code).not.toMatch(/post\(\{[^}]*Float32Array/);
  });

  it("the CSP needs nothing new: worker, wasm and model are same-origin assets", () => {
    const code = strip(wakeModules["./worker.ts"]);
    expect(code).not.toMatch(/https?:\/\//);
    expect(code).not.toMatch(/new Function|\beval\s*\(|blob:/);
  });
});

describe("approvals: no hit reaches one", () => {
  const FORBIDDEN = /approv|resolveApproval|\bdecide\b|onDecide|\/approvals/i;

  it.each(files)("%s has no approval name, import or route", (_f, src) => {
    expect(strip(src)).not.toMatch(FORBIDDEN);
  });

  it("the Settings control mentions no approval function", () => {
    expect(settingsUi).not.toMatch(/\bdecide\b|resolveApproval|api\./);
  });

  it("in the window, the stage's hits are the capture's wake path and four playback commands", () => {
    const a = appSource.indexOf("const wakeStage = useMemo(() => new WakeStage({");
    const b = appSource.indexOf("}), [commands, onWakeHit, speech]);");
    expect(a).toBeGreaterThan(0);
    expect(b).toBeGreaterThan(a);
    const block = strip(appSource.slice(a, b)).replace(/\s+/g, " ");
    expect(block).toContain("onWake: () => onWakeHit(speech.selfVoice.audibleWithin(WAKE_LAG_MS)),");
    expect(block).toContain("onCommand: (c) => { commands.fromLocal(c, { card: live.current.approvals.length > 0 }); },");
    expect(block).not.toMatch(/api\.|\bdecide\b|resolveApproval|interrupt|send\(/);
  });

  it("the only things that turn local commands on under a card are the owner's switch", () => {
    const code = strip(appSource);
    expect([...code.matchAll(/setLocalUnderCard\(([^)]*)\)/g)].map((m) => m[1])).toEqual(["cardCommands"]);
    expect([...code.matchAll(/wakeStage\.setCommands\(([^)]*)\)/g)].map((m) => m[1])).toEqual(["cardCommands && state.approvals.length > 0"]);
  });

  it("a keyword can only be the wake word or a Jarvis-prefixed playback command; nothing can say approve", () => {
    const all = [...Object.keys(WAKE_PIECES), ...Object.keys(COMMAND_PIECES)];
    expect(all.sort()).toEqual(["JARVIS", "JARVIS PAUSE", "JARVIS RESUME", "JARVIS SKIP", "JARVIS STOP"]);
    for (const p of all) expect(p).not.toMatch(/APPROV|ALLOW|YES|DENY|AUTHORI|CONFIRM/);
  });
});

describe("a command from the local recognizer", () => {
  function hostile() {
    const touched: string[] = [];
    const calls: string[] = [];
    const target: CommandEffects = {
      pause: () => calls.push("pause"), resume: () => calls.push("resume"), skip: () => calls.push("skip"), stop: () => calls.push("stop"),
    };
    const effects = new Proxy(target, { get(t, k) { if (!(k in t)) touched.push(String(k)); return (t as never)[k]; } });
    return { touched, calls, router: new CommandRouter(effects) };
  }

  it("is refused under a card unless the owner's switch is on (the seam is off by default)", () => {
    const h = hostile();
    for (const c of ["pause", "resume", "skip", "stop"] as const) {
      expect(h.router.fromLocal(c, { card: true })).toEqual({ kind: "refused", command: c, why: "card" });
    }
    expect(h.calls).toEqual([]);
  });

  it("with the switch on runs exactly the four playback calls under a card, and reads nothing else", () => {
    const h = hostile();
    h.router.setLocalUnderCard(true);
    for (const c of ["pause", "resume", "skip", "stop"] as const) h.router.fromLocal(c, { card: true });
    expect(h.calls).toEqual(["pause", "resume", "skip", "stop"]);
    expect(h.touched).toEqual([]);
  });

  it("anything that is not one of the four is nothing, card or not", () => {
    const h = hostile();
    h.router.setLocalUnderCard(true);
    for (const c of ["approve", "yes", "allow", "deny", "interrupt", ""]) {
      for (const card of [true, false]) expect(h.router.fromLocal(c as never, { card })).toEqual({ kind: "none" });
    }
    expect(h.calls).toEqual([]);
    expect(h.touched).toEqual([]);
  });
});
