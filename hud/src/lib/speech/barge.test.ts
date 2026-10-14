// V9: every barge-in transition of the plan's state machine (section 3.10, decision V-7)
// against the real SpeechController, queue and floor, with a fake clock and player.

import { beforeEach, describe, expect, it } from "vitest";
import { BARGE_HOLD_MS, BargeIn } from "./barge";
import { CommandRouter } from "./commands";
import { SpeechController } from "./controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./testkit";

let clock: FakeClock;
let player: FakePlayer;
let c: SpeechController;
let b: BargeIn;
let recording = false;

const A = "thread-a";
const B = "thread-b";
const settle = (text: string, thread = A) => c.event({ kind: "text", thread_id: thread, data: { text } });
const LONG = "A long sentence that is being read out loud to the owner right now.";
const NEXT = "The next thing that was waiting its turn in the queue as well.";

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  const say = new FakeSay(clock);
  c = new SpeechController({ say: say.fn, player, clock, gate: new FakeGate() });
  c.setSpeakers(new Set([A, B]));
  recording = false;
  b = new BargeIn({ speech: c, clock, recording: () => recording });
});

async function playing() {
  settle(LONG);
  await clock.advance(500);
  expect(c.view().state).toBe("playing");
}

describe("what pauses him (Q7)", () => {
  it("the talk key and the wake word do", async () => {
    await playing();
    expect(b.begin("key", 1)).toBe(true);
    expect(c.view().state).toBe("paused");
    b.end("dropped", 1);
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    expect(b.begin("wake", 2)).toBe(true);
    expect(c.view().state).toBe("paused");
  });

  it("another voice does not, before enrollment: no matter how it is reported", async () => {
    await playing();
    expect(b.begin("voice", 1)).toBe(false);
    expect(b.verifiedOnset(1)).toBe(false);
    expect(c.view().state).toBe("playing");
    expect(b.held()).toBe(false);
  });

  it("the V12 seam: once a voice is enrolled and verified, it may", async () => {
    await playing();
    b.setVerifiedVoice(true);
    expect(b.verifiedOnset(7)).toBe(true);
    expect(c.view().state).toBe("paused");
    b.end("not_owner", 7);                       // the check failed after all
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    b.setVerifiedVoice(false);
    expect(b.verifiedOnset(8)).toBe(false);
  });

  it("with nothing to hold, nothing is begun and a later pause is not undone", async () => {
    expect(b.begin("key", 1)).toBe(false);
    expect(b.held()).toBe(false);
    settle(LONG);
    await clock.advance(500);
    c.pause();
    b.end("dropped", 1);
    expect(c.view().state).toBe("paused");
  });
});

describe("held -> playing: nothing came of it (restart the sentence, Q13)", () => {
  it.each(["dropped", "boxed", "not_owner"] as const)("%s", async (outcome) => {
    await playing();
    b.begin("wake", 1);
    await clock.advance(5000);
    expect(player.started).toEqual([LONG]);       // nothing plays while he is held
    b.end(outcome, 1);
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    expect(player.started).toEqual([LONG, LONG]); // the interrupted sentence restarts, whole
  });

  it("a hold nothing is feeding ends after ten seconds", async () => {
    await playing();
    b.begin("wake", 1);
    await clock.advance(BARGE_HOLD_MS - 100);
    expect(c.view().state).toBe("paused");
    await clock.advance(200);
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    expect(b.held()).toBe(false);
  });

  it("but not while a message is still being spoken (a long dictation, a held key)", async () => {
    await playing();
    b.begin("key", 1);
    recording = true;
    await clock.advance(BARGE_HOLD_MS * 3);
    expect(c.view().state).toBe("paused");
    recording = false;
    await clock.advance(BARGE_HOLD_MS + 500);
    expect(c.view().state).toBe("playing");
  });
});

describe("held -> a message was sent: skip the one he was saying, go on with the queue", () => {
  it("to the same chat: its items are gone, the other chat's resume", async () => {
    settle(LONG);
    settle(NEXT, B);
    await clock.advance(500);
    b.begin("wake", 1);
    c.event({ kind: "user_message", thread_id: A, data: { via: "hud" } });
    b.end("sent", 1);
    await clock.advance(30_000);
    expect(player.started).toEqual([LONG, NEXT]);
    expect(c.view().state).toBe("idle");
  });

  it("to another chat: the message he was in the middle of is still skipped (V-7), not restarted", async () => {
    settle(LONG);
    settle(NEXT, B);
    await clock.advance(500);
    b.begin("key", 1);
    // The owner's message went to a third chat; nothing about A or B was dropped by it.
    b.end("sent", 1);
    await clock.advance(30_000);
    expect(player.started).toEqual([LONG, NEXT]);   // A's message was not played again
  });

  it("with nothing else queued, nothing is left paused", async () => {
    await playing();
    b.begin("wake", 1);
    b.end("sent", 1);
    expect(c.view().state).toBe("idle");
    settle("A fresh reply afterwards.");
    await clock.advance(5000);
    expect(player.started.at(-1)).toBe("A fresh reply afterwards.");
  });
});

describe("held -> a command", () => {
  function router() {
    return new CommandRouter({ pause: () => c.pause(), resume: () => c.resume(), skip: () => c.skip(), stop: () => c.stop() });
  }
  const run = (text: string) => { const r = router().fromTranscript(text, { card: false, stale: false }); b.end("dropped", 1); return r; };

  it("pause: he stays paused after the barge-in lets go", async () => {
    await playing();
    b.begin("wake", 1);
    run("Jarvis pause");
    await clock.advance(10_000);
    expect(c.view().state).toBe("paused");
    expect(player.started).toEqual([LONG]);
  });

  it("resume: he plays again, from the start of the sentence", async () => {
    await playing();
    b.begin("wake", 1);
    run("Jarvis resume");
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    expect(player.started).toEqual([LONG, LONG]);
  });

  it("resume after the owner's own pause", async () => {
    await playing();
    c.pause();
    b.begin("wake", 1);                              // nothing sounds, but the owner is speaking
    run("Jarvis resume");
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
  });

  it("skip: the rest of that message goes, the queue goes on", async () => {
    settle(LONG);
    settle(NEXT, B);
    await clock.advance(500);
    b.begin("wake", 1);
    run("Jarvis skip");
    await clock.advance(30_000);
    expect(player.started).toEqual([LONG, NEXT]);
  });

  it("stop: speech stops and the queue is cleared", async () => {
    settle(LONG);
    settle(NEXT, B);
    await clock.advance(500);
    b.begin("wake", 1);
    run("Jarvis stop");
    await clock.advance(30_000);
    expect(player.started).toEqual([LONG]);
    expect(c.view().state).toBe("idle");
    expect(c.view().queued).toBe(0);
  });
});

describe("the owner's own controls win, and holders count", () => {
  it("a strip pause made during a barge-in is not undone by its end", async () => {
    await playing();
    b.begin("key", 1);
    c.pause();
    b.end("dropped", 1);
    await clock.advance(10_000);
    expect(c.view().state).toBe("paused");
  });

  it("two utterances hold him until the last lets go", async () => {
    await playing();
    b.begin("key", 1);
    b.begin("wake", 2);
    b.end("dropped", 1);
    await clock.advance(200);
    expect(c.view().state).toBe("paused");
    b.end("dropped", 2);
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
  });

  it("ending an utterance that never held him, or twice, does nothing", async () => {
    await playing();
    b.end("dropped", 99);
    expect(c.view().state).toBe("playing");
    b.begin("key", 1);
    b.end("dropped", 1);
    b.end("sent", 1);                                 // the second word is too late to skip anything
    await clock.advance(200);
    expect(player.started).toEqual([LONG, LONG]);
  });

  it("release forgets every hold without resuming (the window is going away)", async () => {
    await playing();
    b.begin("key", 1);
    b.release();
    expect(b.held()).toBe(false);
    await clock.advance(BARGE_HOLD_MS * 2);
    expect(clock.pendingTimers()).toBeLessThan(5);
  });
});
