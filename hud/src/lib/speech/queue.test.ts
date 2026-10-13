import { beforeEach, describe, expect, it } from "vitest";
import { SpeechQueue, type SpeechItem } from "./queue";
import { FakeClock, FakePlayer, FakeSynth, MS_PER_CHAR } from "./testkit";

let clock: FakeClock;
let player: FakePlayer;
let synth: FakeSynth;
let q: SpeechQueue;
let changes: number;

const entry = (text: string, thread = "t1", messageKey = `${thread}:1`, seq = 0) => ({ thread, messageKey, seq, text });

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  synth = new FakeSynth(clock);
  changes = 0;
  q = new SpeechQueue({ synth: synth.fn, player, clock, onChange: () => { changes++; } });
});

describe("playback order", () => {
  it("plays chunks one at a time, in order, never overlapping", async () => {
    q.enqueue(entry("First chunk here."));
    q.enqueue(entry("Second chunk here."));
    q.enqueue(entry("Third chunk here."));
    await clock.advance(10_000);
    expect(player.started).toEqual(["First chunk here.", "Second chunk here.", "Third chunk here."]);
    expect(player.maxActive).toBe(1);
    expect(q.view().state).toBe("idle");
    expect(q.past().map((i) => i.state)).toEqual(["done", "done", "done"]);
  });

  it("waits for an earlier chunk still being synthesized rather than playing a later one", async () => {
    synth.latency = 100;
    const slow = new Set(["slow one"]);
    const orig = synth.fn;
    // The first chunk is slow to synthesize; the second is quick.
    q = new SpeechQueue({
      synth: (item, signal) => (slow.has(item.text) ? new Promise((res) => clock.setTimeout(() => res({ text: item.text, ms: 100 }), 900)) : orig(item, signal)),
      player, clock,
    });
    q.enqueue(entry("slow one"));
    q.enqueue(entry("quick two"));
    await clock.advance(500);
    expect(player.started).toEqual([]);
    await clock.advance(5000);
    expect(player.started).toEqual(["slow one", "quick two"]);
  });

  it("synthesizes at most two ahead of what is playing", async () => {
    for (let i = 0; i < 6; i++) q.enqueue(entry(`Chunk number ${i} is here.`));
    expect(synth.calls).toHaveLength(2);
    await clock.advance(150);            // first two are ready, the first begins to play
    expect(synth.calls.length).toBeLessThanOrEqual(3);
    await clock.advance(60_000);
    expect(player.started).toHaveLength(6);
    expect(player.maxActive).toBe(1);
  });

  it("holds each chunk for as long as its audio lasts", async () => {
    const text = "Twelve chars";
    q.enqueue(entry(text));
    await clock.advance(100);            // synthesis done, playing
    expect(q.view().state).toBe("playing");
    await clock.advance(text.length * MS_PER_CHAR - 10);
    expect(q.view().state).toBe("playing");
    await clock.advance(20);
    expect(q.view().state).toBe("idle");
  });
});

describe("failure", () => {
  it("skips a chunk whose synthesis fails, marks it, and carries on", async () => {
    synth.failing = (item) => item.text.startsWith("bad");
    q.enqueue(entry("bad chunk"));
    q.enqueue(entry("good chunk"));
    await clock.advance(5000);
    expect(player.started).toEqual(["good chunk"]);
    expect(q.view().failed).toBe(1);
    expect(q.past()[0]).toMatchObject({ text: "bad chunk", state: "skipped", mark: "tts_failed" });
    q.clearMarks();
    expect(q.view().failed).toBe(0);
  });

  it("gives up on a synthesis that never answers", async () => {
    synth.hang = (item) => item.text === "hangs";
    q.enqueue(entry("hangs"));
    q.enqueue(entry("fine"));
    await clock.advance(19_000);
    expect(player.started).toEqual([]);
    await clock.advance(5000);
    expect(player.started).toEqual(["fine"]);
    expect(q.past()[0]).toMatchObject({ text: "hangs", mark: "tts_timeout" });
    expect(synth.aborted).toContain("hangs");
  });
});

describe("controls", () => {
  it("pause stops the sound and resume restarts the interrupted sentence", async () => {
    q.enqueue(entry("A sentence that takes a while to say."));
    q.enqueue(entry("Next."));
    await clock.advance(500);
    expect(q.view().state).toBe("playing");
    q.pause();
    expect(q.view().state).toBe("paused");
    expect(player.active).toBe(0);
    await clock.advance(60_000);
    expect(player.started).toEqual(["A sentence that takes a while to say."]);
    q.resume();
    await clock.advance(100);
    expect(player.started).toEqual(["A sentence that takes a while to say.", "A sentence that takes a while to say."]);
    await clock.advance(60_000);
    expect(player.started.at(-1)).toBe("Next.");
    expect(player.maxActive).toBe(1);
  });

  it("a paused queue holds new chunks until resumed", async () => {
    q.enqueue(entry("Before the pause, and a while."));
    await clock.advance(200);
    q.pause();
    q.enqueue(entry("During the pause."));
    await clock.advance(30_000);
    expect(player.started).toHaveLength(1);
    q.resume();
    await clock.advance(30_000);
    expect(player.started.at(-1)).toBe("During the pause.");
  });

  it("pausing with nothing live is not a pause that eats the next message", async () => {
    q.pause();
    expect(q.view().state).toBe("idle");
    q.enqueue(entry("Speaks anyway."));
    await clock.advance(5000);
    expect(player.started).toEqual(["Speaks anyway."]);
  });

  it("skip drops the rest of the message, not the next one", async () => {
    q.enqueue(entry("Message one, part one is here.", "t1", "t1:1", 0));
    q.enqueue(entry("Message one, part two is here.", "t1", "t1:1", 1));
    q.enqueue(entry("Message two begins here now.", "t1", "t1:2", 0));
    await clock.advance(300);
    expect(q.skip()).toEqual({ thread: "t1", messageKey: "t1:1" });
    expect(player.maxActive).toBe(1);
    await clock.advance(60_000);
    expect(player.started).toEqual(["Message one, part one is here.", "Message two begins here now."]);
  });

  it("stop clears everything, and what is enqueued afterwards still speaks", async () => {
    q.enqueue(entry("One that is playing now."));
    q.enqueue(entry("Two waits behind it."));
    await clock.advance(300);
    q.stop();
    expect(q.view()).toMatchObject({ state: "idle", queued: 0 });
    expect(player.active).toBe(0);
    await clock.advance(30_000);
    expect(player.started).toEqual(["One that is playing now."]);
    q.enqueue(entry("Fresh start."));
    await clock.advance(5000);
    expect(player.started.at(-1)).toBe("Fresh start.");
  });
});

describe("a thread going away", () => {
  it("drops that thread's items and stops its sound at once (pane closed, or the owner sent)", async () => {
    q.enqueue(entry("Thread one is speaking now.", "t1"));
    q.enqueue(entry("Thread one has more to say.", "t1"));
    await clock.advance(300);
    expect(player.active).toBe(1);
    q.dropThread("t1", "stale");
    expect(player.active).toBe(0);
    expect(q.view()).toMatchObject({ state: "idle", queued: 0 });
    await clock.advance(30_000);
    expect(player.started).toHaveLength(1);
    expect(q.past().map((i) => [i.state, i.mark])).toEqual([["dropped", "stale"], ["dropped", "stale"]]);
  });

  it("leaves other threads alone", async () => {
    q.enqueue(entry("Alpha is speaking here.", "a"));
    q.enqueue(entry("Beta waits for its turn.", "b"));
    await clock.advance(300);
    q.dropThread("b");
    await clock.advance(30_000);
    expect(player.started).toEqual(["Alpha is speaking here."]);
  });

  it("aborts a synthesis in flight for a dropped item and never plays it", async () => {
    synth.latency = 5000;
    q.enqueue(entry("In flight right now."));
    await clock.advance(100);
    q.dropThread("t1");
    expect(synth.aborted).toEqual(["In flight right now."]);
    await clock.advance(20_000);
    expect(player.started).toEqual([]);
  });
});

describe("what the view says", () => {
  it("reports state, thread, queue length and notifies on change", async () => {
    expect(q.view()).toMatchObject({ state: "idle", thread: null, queued: 0 });
    q.enqueue(entry("Hello there, friend.", "t9"));
    q.enqueue(entry("And another one.", "t9"));
    await clock.advance(200);
    expect(q.view()).toMatchObject({ state: "playing", thread: "t9", queued: 1 });
    expect(changes).toBeGreaterThan(0);
    const same = q.view();
    expect(q.view()).toBe(same);   // a stable snapshot until something changes
  });

  it("only the playing item leaves the queued count", async () => {
    for (const t of ["a", "b", "c"]) q.enqueue(entry(`Chunk ${t} is here.`));
    await clock.advance(200);
    expect(q.view().queued).toBe(2);
  });

  it("an item records its thread, message, sequence, voice and kind for V7", () => {
    const item: SpeechItem = q.enqueue({ ...entry("Hi."), voice: "bm_george", kind: "fallback" });
    expect(item).toMatchObject({ thread: "t1", messageKey: "t1:1", seq: 0, voice: "bm_george", kind: "fallback" });
  });
});

describe("play order is replaceable", () => {
  it("plays in the order the strategy gives (V7's floor rules plug in here)", async () => {
    q = new SpeechQueue({
      synth: synth.fn, player, clock,
      order: (live) => [...live].sort((a, b) => (a.thread < b.thread ? 1 : a.thread > b.thread ? -1 : a.id - b.id)),
    });
    q.enqueue(entry("From a.", "a"));
    q.enqueue(entry("From b.", "b"));
    await clock.advance(10_000);
    expect(player.started[0]).toBeDefined();
    expect(new Set(player.started)).toEqual(new Set(["From a.", "From b."]));
    expect(player.maxActive).toBe(1);
  });
});
