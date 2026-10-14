// The mic is held off while Jarvis speaks (he does not answer himself), for the
// whole of a reply: playing, buffering between chunks, and a short hangover
// after the last one. Tested through the real Capture, so flipping the policy
// constant, or wiring the hook to the wrong thing, fails here.

import { beforeEach, describe, expect, it, vi } from "vitest";
import appSource from "../../App.tsx?raw";
import indexSource from "./index.ts?raw";
import { Capture } from "../capture";
import { HANGOVER_MS as VAD_HANGOVER } from "../vad";
import { HANGOVER_MS, SpeechController } from "./controller";
import { micSuppressedBySpeech } from "./policy";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./testkit";

const QUIET = 0.0009;
const SPEECH = 0.06;
const T = "thread-1";

let clock: FakeClock;
let player: FakePlayer;
let c: SpeechController;
let sent: number;
let drops: string[];
let cap: Capture;

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  c = new SpeechController({ say: new FakeSay(clock).fn, player, clock, gate: new FakeGate() });
  c.setSpeakers(new Set([T]));
  sent = 0;
  drops = [];
  cap = new Capture({
    suppressed: () => micSuppressedBySpeech(c.view()),
    suppressionReason: () => "speaking",
    muted: () => false,
    onLevel: () => {},
    onListening: () => {},
    onIdle: () => {},
    onDrop: (reason) => { drops.push(reason); },
    onChunk: () => {},
    onUtteranceEnd: (e) => { if (e.chunks && !e.truncated) sent++; },
  }, () => 10_000);
});

/** A claimed utterance: the follow-up window is open, then speech, then quiet. */
function speakIntoMic() {
  cap.feedMs(1500, QUIET);
  cap.openFollowUp();
  cap.feedMs(1200, SPEECH);
  cap.feedMs(VAD_HANGOVER + 300, QUIET);
}

const REPLY = "Right, here is what I found. The approval gate had seven separate ways past it, and every one was older. Fixed.";

describe("the mic and his voice", () => {
  it("hears nothing while a chunk plays", async () => {
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    await clock.advance(400);
    expect(c.view().state).toBe("playing");
    speakIntoMic();
    expect(sent).toBe(0);
    expect(drops).toContain("suppressed:speaking");
  });

  it("hears nothing in the gaps between chunks, while the next is being made", async () => {
    const say = new FakeSay(clock);
    say.latency = 2000;
    c = new SpeechController({ say: say.fn, player, clock, gate: new FakeGate() });
    c.setSpeakers(new Set([T]));
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    await clock.advance(1000);   // the first chunk is still being synthesized: nothing plays yet
    expect(c.view()).toMatchObject({ state: "buffering", active: true });
    speakIntoMic();
    expect(sent).toBe(0);
    // And between two chunks: let the first play out while the second is slow.
    await clock.advance(30_000);
    expect(c.view().state).not.toBe("paused");
  });

  it("hears nothing within the hangover, and hears again after it", async () => {
    c.event({ kind: "text", thread_id: T, data: { text: "Short reply to hear." } });
    await clock.advance(1200);                    // her last chunk ended at ~1.1 s
    expect(c.view().state).toBe("idle");
    expect(c.view().active).toBe(true);          // the hangover
    expect(micSuppressedBySpeech(c.view())).toBe(true);
    await clock.advance(HANGOVER_MS + 50);
    expect(c.view().active).toBe(false);
    speakIntoMic();
    expect(sent).toBe(1);
  });

  it("is not suppressed while paused, nor while the platform blocks audio", async () => {
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    await clock.advance(400);
    c.pause();
    expect(c.view().active).toBe(false);
    expect(micSuppressedBySpeech(c.view())).toBe(false);
    c.resume();
    player.isBlocked = true;
    await clock.advance(100);
    expect(c.view().active).toBe(true);
    expect(micSuppressedBySpeech(c.view())).toBe(false);   // nothing is audible: nothing to echo
  });

  it("says when speech goes idle, after the hangover", async () => {
    const idle = vi.fn();
    c.onIdle(idle);
    c.event({ kind: "text", thread_id: T, data: { text: "Short reply to hear." } });
    await clock.advance(1200);
    expect(idle).not.toHaveBeenCalled();
    await clock.advance(HANGOVER_MS + 50);
    expect(idle).toHaveBeenCalledTimes(1);
  });
});

describe("the wiring is pinned", () => {
  it("App holds the detector off with micSuppressedBySpeech, in both places", () => {
    expect(appSource.match(/micSuppressedBySpeech\(speech\.controller\.view\(\)\)/g)?.length).toBe(1);
    expect(appSource.match(/speechPlaying\(\)/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("a talk-key press pauses speech, and a turn's follow-up waits for the speech to end", () => {
    expect(appSource).toMatch(/if \(capture\.press\(\)\) speech\.controller\.bargeIn\(\)/);
    // He resumes when the talk-key utterance is over: on a drop, and however it ends.
    expect(appSource).toMatch(/source === "ptt"\) speech\.controller\.endBargeIn\(\)/);
    // V4: the end of the whole (possibly chunked) message, once its fate is known, and also when nothing was uploaded.
    expect(appSource).toMatch(/const finish = \(\) => \{ if \(end\.source === "ptt"\) speech\.controller\.endBargeIn\(\); \}/);
    expect(appSource).toMatch(/whenGroupSettled\(u\.gid\)\.then\([\s\S]{0,200}finish\(\)/);
    expect(appSource).toMatch(/u\.gid === null\) \{ refuse\(\); finish\(\); return; \}/);
    expect(appSource).toContain("speech.controller.messageSent(threadId)");
    expect(appSource).toContain("followUpAfterSpeech.current = true");
    expect(appSource).toContain("speech.controller.onIdle(");
  });

  it("createSpeech asks /say for a local voice", async () => {
    expect(indexSource).toContain("localOnly: SAY_LOCAL_ONLY");
    const bodies: Record<string, unknown>[] = [];
    const ducks: Record<string, unknown>[] = [];
    // Read-aloud also asks the daemon for the chat's voice (`/speech/voices`, V7):
    // it answers with a voice here, so the one `/say` that follows names it.
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      if (String(url).startsWith("/speech/voices")) {
        return new Response(JSON.stringify({ thread_id: T, voice: "bm_test", muted: false, threads: {}, pool: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (String(url) === "/speech/duck") {
        // V15: the lease that lowers the Remote Browser's sound while speech is active.
        ducks.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ on: true, ducked: 0, lease_s: 3 }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      bodies.push(JSON.parse(String(init?.body)));
      return new Response(new Uint8Array([1]), { status: 200 });
    });
    const { createSpeech } = await import("./index");
    const speech = createSpeech();
    try {
      speech.controller.setSpeakers(new Set([T]));
      speech.controller.event({ kind: "text", thread_id: T, data: { text: "Hello there, friend." } });
      for (let i = 0; i < 20 && !bodies.length; i++) await new Promise((r) => setTimeout(r, 0));
      expect(bodies[0]).toEqual({ text: "Hello there, friend.", voice: "bm_test", local_only: true });
      // Speech going active takes the Browser-duck lease; nothing else is ever put in that body.
      for (let i = 0; i < 20 && !ducks.length; i++) await new Promise((r) => setTimeout(r, 0));
      expect(ducks[0]).toMatchObject({ on: true });
      expect(Object.keys(ducks[0]!).sort()).toEqual(["on", "window"]);
    } finally {
      speech.dispose();
      vi.unstubAllGlobals();
    }
  });
});
