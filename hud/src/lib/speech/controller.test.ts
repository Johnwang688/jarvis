import { beforeEach, describe, expect, it } from "vitest";
import source from "./controller.ts?raw";
import { SpeechController } from "./controller";
import { MIC_OFF_MUTES_SPEECH, onMicModeChange } from "./policy";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./testkit";

let clock: FakeClock;
let player: FakePlayer;
let say: FakeSay;
let gate: FakeGate;
let c: SpeechController;

const T = "thread-1";
const delta = (text: string, thread = T) => c.event({ kind: "text_delta", thread_id: thread, data: { text } });
const settle = (text: string, thread = T) => c.event({ kind: "text", thread_id: thread, data: { text } });
const finish = (stop = "end", thread = T) => c.event({ kind: "turn_finished", thread_id: thread, data: { stop } });
const stream = (text: string, size = 9, thread = T) => { for (let i = 0; i < text.length; i += size) delta(text.slice(i, i + size), thread); };

const REPLY = "Right, here is what I found. The approval gate had seven separate ways past it, and every one was an older copy. I have fixed all seven.";

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  say = new FakeSay(clock);
  gate = new FakeGate();
  c = new SpeechController({ say: say.fn, player, clock, gate });
  c.setSpeakers(new Set([T]));
});

describe("reading a streamed reply", () => {
  it("speaks the prose in order, one chunk at a time", async () => {
    stream(REPLY);
    settle(REPLY);
    finish();
    await clock.advance(60_000);
    expect(player.started.join(" ")).toBe(REPLY);
    expect(player.maxActive).toBe(1);
    expect(c.view().state).toBe("idle");
  });

  it("holds a message until it closes, because its length decides how it is said (V8)", async () => {
    stream(REPLY.slice(0, REPLY.length - 20));
    await clock.advance(5_000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
    stream(REPLY.slice(REPLY.length - 20));
    finish();
    await clock.advance(60_000);
    expect(player.started.join(" ")).toBe(REPLY);
  });

  it("never speaks code, a diff or a diagnostic", async () => {
    const text = "I made the change. It touches one file.\n```py\nsecret_key = 1\n```\nTests pass now, all of them.\n[session not saved: disk full]\n";
    stream(text, 5);
    settle(text);
    finish();
    await clock.advance(60_000);
    const said = player.started.join(" ");
    expect(said).toContain("Tests pass now");
    expect(said).not.toMatch(/secret_key|session|disk/);
  });

  it("a tool call flushes the partial sentence and the next message is a new one", async () => {
    delta("Let me look at that file now");
    c.event({ kind: "tool_started", thread_id: T, data: {} });
    await clock.advance(5000);
    expect(player.started).toEqual(["Let me look at that file now"]);
    stream("Found it. The bug is on line forty, in the parser.");
    finish();
    await clock.advance(30_000);
    expect(player.started.at(-1)).toContain("parser");
    const keys = new Set([...c.items().past].map((i) => i.messageKey));
    expect(keys.size).toBe(2);
  });

  it("speaks a settled text the deltas never carried, and the fast path's remainder once", async () => {
    settle("Done. The tests pass.");
    await clock.advance(5000);
    expect(player.started).toEqual(["Done. The tests pass."]);
  });

  it("asks the daemon for the thread's voice when it has one", async () => {
    const withVoice = new SpeechController({ say: say.fn, player, clock, gate, voiceFor: (t) => (t === T ? "bm_george" : null) });
    withVoice.setSpeakers(new Set([T]));
    withVoice.event({ kind: "text", thread_id: T, data: { text: "Hello there, friend." } });
    expect(say.calls).toEqual([{ text: "Hello there, friend.", voice: "bm_george" }]);
    const plain = new SpeechController({ say: say.fn, player, clock, gate });
    plain.setSpeakers(new Set([T]));
    plain.event({ kind: "text", thread_id: T, data: { text: "No voice named." } });
    expect(say.calls.at(-1)).toEqual({ text: "No voice named.", voice: null });
  });
});

describe("who is read", () => {
  it("reads only the threads it was told to; others and task views are silent", async () => {
    settle("Hidden thread speaking.", "other");
    settle("Task view speaking.", "task-thread");
    await clock.advance(5000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
  });

  it("closing the pane stops the sound at once and drops what was queued", async () => {
    stream(REPLY);
    settle(REPLY);
    await clock.advance(400);
    expect(player.active).toBe(1);
    c.setSpeakers(new Set());          // the pane went away
    expect(player.active).toBe(0);
    expect(c.view().queued).toBe(0);
    await clock.advance(60_000);
    expect(player.started).toHaveLength(1);
  });

  it("switching the pane to another thread drops the old thread's speech and reads the new one", async () => {
    settle("Old thread is speaking a long enough sentence here.");
    await clock.advance(300);
    c.setSpeakers(new Set(["thread-2"]));
    expect(player.active).toBe(0);
    settle("New thread speaks now.", "thread-2");
    await clock.advance(10_000);
    expect(player.started.at(-1)).toBe("New thread speaks now.");
  });
});

describe("stale speech", () => {
  it("a message sent to the thread drops what was queued and stops what is playing", async () => {
    stream(REPLY);
    settle(REPLY);
    await clock.advance(400);
    expect(player.active).toBe(1);
    c.event({ kind: "user_message", thread_id: T, data: { via: "hud" } });
    expect(player.active).toBe(0);
    expect(c.view()).toMatchObject({ state: "idle", queued: 0 });
    // The new turn's reply speaks.
    stream("Here is the new answer, in full.");
    finish();
    await clock.advance(30_000);
    expect(player.started.at(-1)).toBe("Here is the new answer, in full.");
  });

  it("a steer silences the rest of the message in progress, not the one after the tool", async () => {
    stream("The old answer keeps streaming and streaming. ");
    c.event({ kind: "user_message", thread_id: T, data: { via: "hud", steer: true } });
    stream("More of the old answer that must not be heard.");
    c.event({ kind: "tool_started", thread_id: T, data: {} });
    await clock.advance(30_000);
    expect(player.started.join(" ")).not.toContain("must not be heard");
    stream("The steered answer is spoken.");
    finish();
    await clock.advance(30_000);
    expect(player.started.at(-1)).toBe("The steered answer is spoken.");
  });

  it("a user message before a turn does not silence that turn's first reply", async () => {
    settle("Earlier reply, already done.");
    await clock.advance(10_000);
    c.event({ kind: "user_message", thread_id: T, data: {} });
    settle("The first reply of the new turn.");
    await clock.advance(10_000);
    expect(player.started.at(-1)).toBe("The first reply of the new turn.");
  });

  it("an interrupted turn is silent from the moment it stops", async () => {
    stream(REPLY);
    await clock.advance(400);
    finish("interrupted");
    expect(player.active).toBe(0);
    await clock.advance(30_000);
    expect(c.view().state).toBe("idle");
  });
});

describe("one speaking window", () => {
  it("a window without the lock builds no queue and asks for no audio", async () => {
    gate.set(false);
    stream(REPLY);
    settle(REPLY);
    await clock.advance(10_000);
    expect(say.calls).toEqual([]);
    expect(player.started).toEqual([]);
    expect(c.view().speaker).toBe(false);
  });

  it("losing the lock mid-reply silences this window; regaining it speaks the next reply", async () => {
    stream(REPLY);
    settle(REPLY);
    await clock.advance(400);
    gate.set(false);
    expect(player.active).toBe(0);
    await clock.advance(30_000);
    expect(player.started).toHaveLength(1);
    gate.set(true);
    settle("Back again, and speaking.");
    await clock.advance(10_000);
    expect(player.started.at(-1)).toBe("Back again, and speaking.");
  });
});

describe("read-aloud has its own switch, not the mic's", () => {
  it("off: silent, and turning it off stops what is playing", async () => {
    stream(REPLY);
    settle(REPLY);
    await clock.advance(400);
    c.setEnabled(false);
    expect(player.active).toBe(0);
    settle("Not spoken at all.");
    await clock.advance(30_000);
    expect(player.started).toHaveLength(1);
    c.setEnabled(true);
    settle("Spoken again.");
    await clock.advance(10_000);
    expect(player.started.at(-1)).toBe("Spoken again.");
  });

  it("the mic's OFF mode does not post /mute: voice output is untouched by it", () => {
    const posted: boolean[] = [];
    expect(MIC_OFF_MUTES_SPEECH).toBe(false);
    onMicModeChange(true, (m) => posted.push(m));
    onMicModeChange(false, (m) => posted.push(m));
    expect(posted).toEqual([]);
  });

  it("speech goes on while the mic is off: nothing in the controller reads the dictation mode", async () => {
    // The controller has no input for it; this pins that no such dependency creeps in.
    expect(source).not.toMatch(/dictation|isMuted|micMuted/i);
    settle("Spoken with the mic off.");
    await clock.advance(10_000);
    expect(player.started).toEqual(["Spoken with the mic off."]);
  });
});

describe("failure and the controls", () => {
  it("a chunk the local voice cannot make is skipped and counted; the next still plays", async () => {
    say.fail = (t) => t.startsWith("Broken");
    settle("Broken chunk here. Working chunk is next and long enough to stand alone.");
    await clock.advance(30_000);
    expect(player.started.join(" ")).toContain("Working chunk");
    expect(c.view().failed).toBeGreaterThan(0);
    c.clearMarks();
    expect(c.view().failed).toBe(0);
  });

  it("pause, resume, skip and stop act on the queue", async () => {
    settle("First message, long enough to hear for a while.");
    settle("Second message follows right after it.");
    await clock.advance(400);
    c.toggle();
    expect(c.view().state).toBe("paused");
    expect(player.active).toBe(0);
    c.toggle();
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    c.skip();
    await clock.advance(10_000);
    expect(player.started.at(-1)).toBe("Second message follows right after it.");
    c.stop();
    expect(c.view()).toMatchObject({ state: "idle", queued: 0 });
  });

  it("skip silences the rest of a message that is still streaming", async () => {
    stream("A long first sentence that is read out loud now. And a second one that follows it closely. ");
    await clock.advance(500);
    c.skip();
    stream("The tail of the same message must stay quiet. ");
    c.event({ kind: "tool_started", thread_id: T, data: {} });
    await clock.advance(30_000);
    expect(player.started.join(" ")).not.toContain("tail of the same");
  });

  it("a message whose text arrives after a reconnect is not spoken twice", async () => {
    const FIRST = "The first sentence was fully read out before the stream dropped. ";
    const FULL = FIRST + "The rest of the reply arrived while it was down, in full.";
    delta(FIRST + "The rest of the reply arr");
    await clock.advance(3000);
    c.event({ kind: "_disconnected" });
    c.event({ kind: "_connected" });
    settle(FULL);
    await clock.advance(30_000);
    const said = player.started.join(" ");
    expect(said.split("The first sentence was fully read out").length - 1).toBe(1);
    expect(said).toContain("arrived while it was down");
  });

  it("a reconnect discards text that can no longer be finished", async () => {
    delta("Half a sentence that never finishe");
    c.event({ kind: "_disconnected" });
    c.event({ kind: "_connected" });
    delta("d. A new sentence after the gap, long enough.");
    c.event({ kind: "tool_started", thread_id: T, data: {} });
    await clock.advance(10_000);
    expect(player.started.join(" ")).not.toContain("never finishe");
  });
});

describe("speech is output only", () => {
  it("nothing in the speech modules can reach an approval", () => {
    const modules = import.meta.glob("./*.ts", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const files = Object.keys(modules).filter((f) => !f.endsWith(".test.ts") && f !== "./testkit.ts");
    expect(files.length).toBeGreaterThan(5);
    for (const f of files) {
      const src = modules[f].replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
      expect(src, f).not.toMatch(/approv|resolveApproval|decide\(|\/approvals/i);
    }
  });
});

describe("barge-in pauses, then resumes (decision V-7)", () => {
  const LONG = "A long sentence that is being read out loud to the owner right now.";

  it("nothing sent: he resumes where he paused, restarting the sentence", async () => {
    settle(LONG);
    await clock.advance(500);
    expect(c.view().state).toBe("playing");
    c.bargeIn();
    expect(c.view().state).toBe("paused");
    expect(player.active).toBe(0);
    await clock.advance(20_000);
    expect(player.started).toEqual([LONG]);           // nothing plays while the owner talks
    c.endBargeIn();                                    // empty, dropped, failed, or placed for review
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
    expect(player.started).toEqual([LONG, LONG]);     // the sentence restarts (Q13)
    await clock.advance(20_000);
    expect(c.view().state).toBe("idle");
  });

  it("a message sent: that thread's items are gone, and what remains resumes", async () => {
    c.setSpeakers(new Set([T, "t2"]));
    settle(LONG);
    settle("Another thread has something to say as well, in full.", "t2");
    await clock.advance(500);
    c.bargeIn();
    expect(c.view().state).toBe("paused");
    c.event({ kind: "user_message", thread_id: T, data: { via: "hud" } });   // the send
    c.endBargeIn();
    await clock.advance(30_000);
    expect(player.started).toEqual([LONG, "Another thread has something to say as well, in full."]);
    expect(c.view().state).toBe("idle");
  });

  it("a message sent and nothing else left: nothing resumes, and nothing is stuck paused", async () => {
    settle(LONG);
    await clock.advance(500);
    c.bargeIn();
    c.messageSent(T);
    c.endBargeIn();
    expect(c.view().state).toBe("idle");
    settle("Next reply.");
    await clock.advance(5000);
    expect(player.started.at(-1)).toBe("Next reply.");
  });

  it("paused from the strip first: a talk-key release does not resume him", async () => {
    settle(LONG);
    await clock.advance(500);
    c.pause();                                         // the owner's own pause
    c.bargeIn();
    c.endBargeIn();
    await clock.advance(20_000);
    expect(c.view().state).toBe("paused");
    expect(player.started).toEqual([LONG]);
    c.resume();                                        // only the strip resumes it
    await clock.advance(200);
    expect(c.view().state).toBe("playing");
  });

  it("the owner taking over mid-barge-in wins: a strip pause or stop is not undone by the release", async () => {
    settle(LONG);
    await clock.advance(500);
    c.bargeIn();
    c.pause();                                         // the owner clicks pause while talking
    c.endBargeIn();
    expect(c.view().state).toBe("paused");
    c.stop();
    c.endBargeIn();
    expect(c.view().state).toBe("idle");
  });

  it("a barge-in with nothing playing pauses nothing, so a later pause is not undone", async () => {
    c.bargeIn();
    expect(c.view().state).toBe("idle");
    settle(LONG);
    await clock.advance(500);
    c.pause();
    c.endBargeIn();
    expect(c.view().state).toBe("paused");
  });
});
