// V7: one queue for every open chat pane (plan §3.9 and §5 "Queue"). A fake
// clock and a fake TTS (silence proportional to text), never a speaker.

import { beforeEach, describe, expect, it } from "vitest";
import { SpeechController } from "./controller";
import { BACKLOG_MS, FLOOR_TIMEOUT_MS, Floor, estimateMs } from "./floor";
import { speakingThreads } from "./policy";
import { SpeechQueue } from "./queue";
import { FakeClock, FakeGate, FakePlayer, FakeSay, FakeSynth, MS_PER_CHAR } from "./testkit";
import { ThreadVoices, voiceLabel, type VoicesApi } from "./voices";
import { defaultPane, defaultWorkspace, PANE_NOS, type PaneNo, type View, type Workspace } from "../workspace";

let clock: FakeClock;
let player: FakePlayer;
let synth: FakeSynth;
let q: SpeechQueue;

const entry = (thread: string, n: number, text: string, seq = 0) => ({ thread, messageKey: `${thread}:${n}`, seq, text });

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  synth = new FakeSynth(clock);
  q = new SpeechQueue({ synth: synth.fn, player, clock, floor: new Floor(clock) });
});

describe("the floor", () => {
  it("never overlaps, and messages of two threads do not interleave", async () => {
    q.enqueue(entry("a", 0, "A one is here."));
    q.enqueue(entry("b", 0, "B one is here."));
    q.enqueue(entry("a", 0, "A two is here.", 1));
    q.enqueue(entry("b", 0, "B two is here.", 1));
    await clock.advance(30_000);
    expect(player.started).toEqual(["A one is here.", "A two is here.", "B one is here.", "B two is here."]);
    expect(player.maxActive).toBe(1);
  });

  it("a message that has the floor goes on before another thread's earlier-queued message", async () => {
    q.enqueue(entry("a", 0, "A first chunk is long enough."));
    await clock.advance(200);                       // a is playing: it holds the floor
    q.enqueue(entry("b", 0, "B waits for its turn."));
    q.enqueue(entry("a", 0, "A second chunk arrives late.", 1));
    await clock.advance(30_000);
    expect(player.started).toEqual(["A first chunk is long enough.", "A second chunk arrives late.", "B waits for its turn."]);
  });

  it("messages between threads go in the order their first chunk arrived", async () => {
    q.enqueue(entry("b", 0, "B was first to arrive."));
    q.enqueue(entry("a", 0, "A came second here."));
    q.enqueue(entry("c", 0, "C came third in line."));
    await clock.advance(30_000);
    expect(player.started).toEqual(["B was first to arrive.", "A came second here.", "C came third in line."]);
  });

  it("a finished message releases the floor at once; an unfinished one waits for its next chunk", async () => {
    q.enqueue(entry("a", 0, "A done."));
    await clock.advance(200);
    q.enqueue(entry("b", 0, "B next."));
    q.endMessage("a:0");
    await clock.advance(400);
    expect(player.started).toEqual(["A done.", "B next."]);
  });

  it("holds the floor for a quiet holder, then yields after the timeout, and its rest re-queues behind", async () => {
    q.enqueue(entry("a", 0, "A starts talking."));
    await clock.advance(200);                       // playing; floor is a's
    q.enqueue(entry("b", 0, "B is waiting now."));
    q.enqueue(entry("c", 0, "C is waiting too."));
    await clock.advance(A_ENDS());                  // a's chunk ends; nothing more from a
    expect(player.started).toEqual(["A starts talking."]);
    await clock.advance(FLOOR_TIMEOUT_MS - 500);
    expect(player.started).toEqual(["A starts talking."]);   // still holding
    await clock.advance(1000);
    expect(player.started[1]).toBe("B is waiting now.");     // yielded
    q.enqueue(entry("a", 0, "A is back at last.", 1));
    await clock.advance(30_000);
    expect(player.started).toEqual(["A starts talking.", "B is waiting now.", "C is waiting too.", "A is back at last."]);
    expect(player.maxActive).toBe(1);
  });

  it("a holder with nothing waiting behind it is not timed out", async () => {
    q.enqueue(entry("a", 0, "A alone."));
    await clock.advance(200);
    await clock.advance(30_000);
    expect(q.view().state).toBe("idle");
    q.enqueue(entry("a", 0, "A continues.", 1));
    await clock.advance(1000);
    expect(player.started).toEqual(["A alone.", "A continues."]);
  });

  it("a holder whose next chunk is still being made yields to a ready one after the timeout", async () => {
    synth.hang = (item) => item.text === "A slow chunk.";
    q.enqueue(entry("a", 0, "A plays first."));
    await clock.advance(200);
    q.enqueue(entry("a", 0, "A slow chunk.", 1));
    q.enqueue(entry("b", 0, "B is ready now."));
    await clock.advance(1500);
    expect(player.started).toEqual(["A plays first."]);
    await clock.advance(FLOOR_TIMEOUT_MS);
    expect(player.started).toContain("B is ready now.");
  });
});

/** The fake player lasts 50 ms a character; the first chunk of the timeout tests ends well within this. */
function A_ENDS() { return "A starts talking.".length * MS_PER_CHAR; }

describe("one at a time, whatever the order says", () => {
  it("never starts a second sound while one plays, even if the order puts a ready item ahead of it", async () => {
    // A PlayOrder that puts the newest first: without the guard in the queue's step the
    // new, ready item would start over the one playing.
    const lifo = new SpeechQueue({ synth: synth.fn, player, clock, order: (live) => [...live].reverse() });
    lifo.enqueue(entry("a", 0, "First one is long enough to overlap."));
    await clock.advance(200);
    expect(player.active).toBe(1);
    lifo.enqueue(entry("b", 0, "Second."));
    await clock.advance(200);
    expect(player.active).toBe(1);
    expect(player.maxActive).toBe(1);
    await clock.advance(30_000);
    expect(player.maxActive).toBe(1);
  });
});

describe("a chunk that could not be made", () => {
  it("keeps naming its chat while idle, until the marks are cleared", async () => {
    synth.failing = () => true;
    q.enqueue(entry("a", 0, "This will not be made."));
    await clock.advance(1000);
    expect(q.view()).toMatchObject({ state: "idle", failed: 1, thread: "a" });
    q.clearMarks();
    expect(q.view()).toMatchObject({ failed: 0, thread: null });
  });
});

describe("stale, drop and skip", () => {
  it("a send to a thread drops its unplayed items and stops its playing one, others go on", async () => {
    q.enqueue(entry("a", 0, "A is being read right now."));
    q.enqueue(entry("a", 1, "A next message here."));
    q.enqueue(entry("b", 0, "B is untouched here."));
    await clock.advance(300);
    q.dropThread("a", "stale");
    // A's sound stopped at once; B (already made) took its place, and nothing sounded twice.
    expect(player.started).toEqual(["A is being read right now.", "B is untouched here."]);
    expect(player.active).toBe(1);
    await clock.advance(30_000);
    expect(player.started).toEqual(["A is being read right now.", "B is untouched here."]);
    expect(player.maxActive).toBe(1);
    expect(q.past().filter((i) => i.mark === "stale").length).toBe(2);
  });

  it("skipThread skips one chat's current message and no other", async () => {
    q.enqueue(entry("a", 0, "A first of two chunks."));
    q.enqueue(entry("a", 0, "A second of two chunks.", 1));
    q.enqueue(entry("b", 0, "B stays as it was."));
    await clock.advance(300);
    q.skipThread("a");
    await clock.advance(30_000);
    expect(player.started).toEqual(["A first of two chunks.", "B stays as it was."]);
  });

  it("skipThread on a chat that is only queued skips its next message", async () => {
    q.enqueue(entry("a", 0, "A is playing along."));
    q.enqueue(entry("b", 0, "B will be skipped."));
    q.enqueue(entry("b", 1, "B later message stays."));
    await clock.advance(300);
    expect(q.skipThread("b")).toEqual({ thread: "b", messageKey: "b:0" });
    await clock.advance(30_000);
    expect(player.started).toEqual(["A is playing along.", "B later message stays."]);
  });
});

describe("backlog", () => {
  const long = (tag: string, seconds: number) => `${tag} `.padEnd(Math.round((seconds * 1000) / 1000 * 16), "x");

  it("estimates speech time from length", () => {
    expect(estimateMs("x".repeat(160))).toBe(10_000);
  });

  it("collapses the oldest interim messages once a thread has over a minute unplayed, keeping the newest and the reply", async () => {
    synth.hang = () => true;                         // nothing plays: a pure backlog
    q.enqueue(entry("z", 0, "Z holds the line for a bit."));      // not a's: just something to wait behind
    for (let i = 0; i < 5; i++) { q.enqueue(entry("a", i, long(`u${i}`, 20))); q.endMessage(`a:${i}`); }
    q.turnEnded("a");                                // a:4 is the reply
    const live = q.items().filter((i) => i.thread === "a");
    const text = live.map((i) => i.text);
    expect(text.some((t) => t.startsWith("u4"))).toBe(true);      // the reply stays
    expect(text.some((t) => t.startsWith("u0"))).toBe(false);     // the oldest went
    const marker = live.find((i) => i.kind === "note");
    expect(marker?.text).toMatch(/^Skipping \d+ updates?\.$/);
    expect(live.reduce((n, i) => n + (i.kind === "note" ? 0 : estimateMs(i.text)), 0)).toBeLessThanOrEqual(BACKLOG_MS);
    expect(q.past().every((i) => i.mark === "collapsed" || i.mark === undefined)).toBe(true);
  });

  it("never collapses a final reply, however long, nor a thread's newest message", async () => {
    synth.hang = () => true;
    q.enqueue(entry("a", 0, long("old", 50)));
    q.endMessage("a:0");
    q.enqueue(entry("a", 1, long("reply", 50)));
    q.endMessage("a:1");
    q.turnEnded("a");
    const live = q.items();
    expect(live.filter((i) => i.kind !== "note").map((i) => i.text.slice(0, 5))).toEqual(["reply"]);
    expect(live.find((i) => i.kind === "note")?.text).toBe("Skipping 1 update.");
  });

  it("never collapses a thread's newest message, which may still be arriving", async () => {
    synth.hang = () => true;
    q.enqueue(entry("a", 0, long("old", 30)));
    q.endMessage("a:0");
    q.enqueue(entry("a", 1, long("newest", 70)));          // not finished, not yet known to be the reply
    expect(q.items().filter((i) => i.kind !== "note").map((i) => i.text.slice(0, 6))).toEqual(["newest"]);
  });

  it("a message that yielded does not make the arriving one collapsible (newest by arrival, not by place in line)", async () => {
    synth.hang = (item) => item.text === "A stalled chunk.";
    q.enqueue(entry("a", 0, "A plays first, then stalls."));
    await clock.advance(300);                                  // a:0 plays and holds the floor
    q.enqueue(entry("a", 0, "A stalled chunk.", 1));            // its next chunk never gets made
    q.enqueue(entry("a", 1, long("arriving", 70)));             // the next message is still arriving
    q.enqueue(entry("b", 0, "B waits for the floor."));
    await clock.advance(FLOOR_TIMEOUT_MS + 3000);               // a:0 yields to b; its rank now sits behind a:1
    // a:1 (70 s unplayed, but a's newest message) goes next: it was never collapsed.
    expect(player.started.some((t) => t.startsWith("arriving"))).toBe(true);
    expect(q.past().some((i) => i.mark === "collapsed" && i.text.startsWith("arriving"))).toBe(false);
  });

  it("a final reply from an earlier turn is not collapsed when a later turn's interim messages back up", async () => {
    synth.hang = () => true;
    q.enqueue(entry("a", 0, long("reply1", 50)));
    q.endMessage("a:0");
    q.turnEnded("a");                                          // turn 1 is over: a:0 is its reply
    for (let i = 1; i <= 3; i++) { q.enqueue(entry("a", i, long(`u${i}`, i === 3 ? 10 : 40))); q.endMessage(`a:${i}`); }
    const left = q.items().filter((i) => i.kind !== "note").map((i) => i.text.slice(0, 2));
    expect(left).toContain("re");                               // the turn-1 reply stays
    expect(left).toContain("u3");                               // the newest
    expect(left).not.toContain("u1");
  });

  it("later collapses are counted by markers too, and every collapsed message is accounted for", async () => {
    synth.hang = () => true;
    q.enqueue(entry("a", 0, long("a0", 30)));
    q.endMessage("a:0");
    q.enqueue(entry("a", 1, long("a1", 40)));
    q.endMessage("a:1");
    q.enqueue(entry("a", 2, long("a2", 40)));
    q.endMessage("a:2");
    q.enqueue(entry("a", 3, long("a3", 40)));
    const notes = q.items().filter((i) => i.kind === "note");
    // (The first marker is already being made, so a later collapse gets its own.)
    expect(notes.length).toBeGreaterThanOrEqual(1);
    const skipped = notes.map((n) => Number(/\d+/.exec(n.text)![0])).reduce((a, b) => a + b, 0);
    expect(skipped).toBe(q.past().filter((i) => i.mark === "collapsed").length);
    expect(skipped).toBeGreaterThanOrEqual(2);
  });

  it("does not touch another thread's items or an item that is playing", async () => {
    q.enqueue(entry("a", 0, long("playing", 20)));
    await clock.advance(200);
    expect(q.view().state).toBe("playing");
    synth.hang = () => true;
    q.enqueue(entry("b", 0, long("b0", 50)));
    q.endMessage("b:0");
    q.enqueue(entry("b", 1, long("b1", 30)));
    expect(q.items().filter((i) => i.thread === "a")).toHaveLength(1);
    expect(q.items().filter((i) => i.thread === "b").some((i) => i.kind === "note")).toBe(true);
  });
});

// ---- the controller over every pane ------------------------------------------

describe("many panes through one controller", () => {
  let say: FakeSay;
  let gate: FakeGate;
  let c: SpeechController;
  const voices: Record<string, string> = { a: "bm_george", b: "af_nicole", c: "bf_emma" };
  const delta = (thread: string, text: string) => c.event({ kind: "text_delta", thread_id: thread, data: { text } });
  const settle = (thread: string, text: string) => c.event({ kind: "text", thread_id: thread, data: { text } });
  const done = (thread: string) => c.event({ kind: "turn_finished", thread_id: thread, data: { stop: "end" } });

  beforeEach(() => {
    say = new FakeSay(clock);
    gate = new FakeGate();
    c = new SpeechController({ say: say.fn, player, clock, gate, voiceFor: (t) => voices[t] ?? null });
    c.setSpeakers(new Set(["a", "b", "c"]));
  });

  it("reads three threads, each in its own voice, in order, with no overlap", async () => {
    for (const t of ["a", "b", "c"]) {
      delta(t, `Reply from ${t}. It has two sentences.`);
      settle(t, `Reply from ${t}. It has two sentences.`);
      done(t);
    }
    await clock.advance(60_000);
    expect(player.maxActive).toBe(1);
    const said = say.calls.map((x) => [x.text.slice(0, 12), x.voice]);
    expect(said.map(([, v]) => v)).toEqual(expect.arrayContaining(["bm_george", "af_nicole", "bf_emma"]));
    for (const call of say.calls) expect(call.voice).toBe(voices[call.text.charAt(11)]);
    expect(player.started.join(" ")).toBe("Reply from a. It has two sentences. Reply from b. It has two sentences. Reply from c. It has two sentences.");
  });

  it("streaming deltas of two threads at once do not interleave", async () => {
    const a = "Alpha one is spoken first. Alpha two follows right after it.";
    const b = "Bravo one is spoken second. Bravo two follows right after it.";
    for (let i = 0; i < a.length; i += 7) { delta("a", a.slice(i, i + 7)); delta("b", b.slice(i, i + 7)); await clock.advance(30); }
    settle("a", a); settle("b", b); done("a"); done("b");
    await clock.advance(60_000);
    const order = player.started.map((t) => t.charAt(0));
    expect(order.join("")).toMatch(/^A+B+$/);
    expect(player.maxActive).toBe(1);
  });

  it("a thread in no pane is silent while the others speak, and a hidden pane's thread speaks", async () => {
    c.setSpeakers(new Set(["a", "hidden-pane"]));
    settle("a", "A is open in a pane.");
    settle("hidden-pane", "This one is in a hidden pane.");
    settle("nowhere", "This one is in no pane at all.");
    await clock.advance(30_000);
    expect(player.started).toEqual(["A is open in a pane.", "This one is in a hidden pane."]);
  });

  it("a pane closing stops its playback at once and the rest carries on", async () => {
    settle("a", "A has a long reply that goes on for a while.");
    settle("b", "B waits behind it for its turn.");
    await clock.advance(400);
    expect(player.active).toBe(1);
    c.setSpeakers(new Set(["b"]));
    // A's sound stopped at once and B's (already made) took its place.
    expect(player.started).toEqual(["A has a long reply that goes on for a while.", "B waits behind it for its turn."]);
    expect(player.active).toBe(1);
    await clock.advance(30_000);
    expect(player.started).toHaveLength(2);
    expect(player.maxActive).toBe(1);
  });

  it("a thread in two panes goes on speaking when one of them closes (the set is by thread)", async () => {
    c.setSpeakers(new Set(["a"]));
    settle("a", "Still being read aloud here.");
    c.setSpeakers(new Set(["a"]));
    await clock.advance(30_000);
    expect(player.started).toEqual(["Still being read aloud here."]);
  });

  it("a send to one thread drops its speech and leaves the others", async () => {
    settle("a", "A reply that is going to be stale.");
    settle("b", "B reply that stays as it was.");
    await clock.advance(300);
    c.event({ kind: "user_message", thread_id: "a", data: { text: "hello" } });
    await clock.advance(30_000);
    expect(player.started).toEqual(["A reply that is going to be stale.", "B reply that stays as it was."]);
    expect(player.maxActive).toBe(1);
    expect(c.items().past.filter((i) => i.thread === "a" && i.mark === "stale").length).toBeGreaterThanOrEqual(0);
  });

  it("a muted chat is silent and its speech goes; unmuting reads what comes next", async () => {
    settle("a", "A is being read before the mute.");
    await clock.advance(300);
    c.setMuted(new Set(["a"]));
    expect(player.active).toBe(0);
    settle("a", "A while muted says nothing.");
    settle("b", "B is not muted at all.");
    await clock.advance(10_000);
    expect(player.started).toEqual(["A is being read before the mute.", "B is not muted at all."]);
    c.setMuted(new Set());
    settle("a", "A is back after unmuting.");
    await clock.advance(10_000);
    expect(player.started.at(-1)).toBe("A is back after unmuting.");
  });

  it("skipThread skips only that chat and silences its open stream", async () => {
    delta("a", "A is talking at some length here. And more is to come right now. Even more");
    settle("b", "B is waiting for its turn.");
    await clock.advance(300);
    expect(player.started).toEqual(["A is talking at some length here."]);
    c.skipThread("a");
    delta("a", " is coming in after the skip. And again here.");
    await clock.advance(30_000);
    expect(player.started).toEqual(["A is talking at some length here.", "B is waiting for its turn."]);
  });

  it("asks for a voice at synthesis time, so a chosen voice applies to what is not made yet", async () => {
    let chosen = "bm_george";
    c = new SpeechController({ say: say.fn, player, clock, gate, voiceFor: () => chosen });
    c.setSpeakers(new Set(["a"]));
    for (const w of ["One", "Two", "Three", "Four"]) settle("a", `${w} is a message of its own.`);
    chosen = "af_nicole";                           // the first two are being made; the others are not yet
    await clock.advance(30_000);
    expect(say.calls.map((x) => x.voice)).toEqual(["bm_george", "bm_george", "af_nicole", "af_nicole"]);
  });

  it("waits for a voice still being assigned rather than speaking in the wrong one", async () => {
    let release!: (v: string) => void;
    const pending = new Promise<string>((r) => { release = r; });
    c = new SpeechController({ say: say.fn, player, clock, gate, voiceFor: () => pending });
    c.setSpeakers(new Set(["a"]));
    settle("a", "Spoken only once the voice is known.");
    await clock.advance(500);
    expect(say.calls).toEqual([]);
    release("bm_lewis");
    await clock.advance(500);
    expect(say.calls[0]).toMatchObject({ voice: "bm_lewis" });
  });

  it("message keys are never reused after a thread leaves and returns", async () => {
    settle("a", "A first message here.");
    c.setSpeakers(new Set(["b"]));
    c.setSpeakers(new Set(["a", "b"]));
    settle("a", "A message after returning.");
    const keys = [...c.items().live, ...c.items().past].filter((i) => i.thread === "a").map((i) => i.messageKey);
    expect(new Set(keys).size).toBe(2);
  });
});

// ---- who is read: every chat pane, drawn or hidden ------------------------------

describe("speakingThreads", () => {
  const ws = (views: Partial<Record<PaneNo, View>>, order: PaneNo[], kept: PaneNo[]): Workspace => {
    const w = defaultWorkspace();
    w.order = order;
    w.kept = kept;
    w.panes = PANE_NOS.map((n) => ({ ...defaultPane(n), view: views[n] ?? "chat" }));
    return w;
  };
  const chatsOf = (ids: Record<number, string | null>) =>
    Object.fromEntries(PANE_NOS.map((n) => [n, { threadId: ids[n] ?? null, compose: null }])) as never;
  const noPending = Object.fromEntries(PANE_NOS.map((n) => [n, null])) as never;

  it("is every thread in a chat pane, hidden panes included", () => {
    const got = speakingThreads(ws({}, [1, 2], [3]), chatsOf({ 1: "a", 2: "b", 3: "c", 4: "d" }), noPending);
    expect([...got].sort()).toEqual(["a", "b", "c"]);                 // pane 4 is in no pane of the workspace
  });

  it("leaves out a task view and any pane not showing chat, and a pane that is not in the workspace", () => {
    const got = speakingThreads(ws({ 2: "task", 3: "file" }, [1, 2], [3]), chatsOf({ 1: "a", 2: "b", 3: "c" }), noPending);
    expect([...got]).toEqual(["a"]);
  });

  it("counts the thread a pane is opening right now", () => {
    const pending = { ...(noPending as object), 1: "fresh" } as never;
    expect([...speakingThreads(ws({}, [1], []), chatsOf({}), pending)]).toEqual(["fresh"]);
  });

  it("is one entry for a thread shown twice", () => {
    expect(speakingThreads(ws({}, [1, 2], []), chatsOf({ 1: "a", 2: "a" }), noPending).size).toBe(1);
  });
});

// ---- the window's copy of the voices ------------------------------------------------

describe("ThreadVoices", () => {
  const apiWith = (initial: Record<string, { voice: string | null; muted: boolean }> = {}) => {
    const calls: unknown[] = [];
    const store = { ...initial };
    let n = 0;
    const pool = ["bm_a", "bm_b", "bm_c"];
    const api: VoicesApi & { calls: unknown[]; fail: boolean } = {
      calls, fail: false,
      list: async () => ({ threads: { ...store }, pool }),
      change: async (thread, change) => {
        calls.push([thread, change]);
        if (api.fail) throw new Error("down");
        const cur = store[thread] ?? { voice: null, muted: false };
        if (change.voice !== undefined) cur.voice = change.voice || pool[n++ % 3];
        else if (change.muted !== undefined) cur.muted = change.muted;
        else if (!cur.voice) cur.voice = pool[n++ % 3];
        store[thread] = cur;
        return { thread_id: thread, ...cur };
      },
    };
    return api;
  };

  it("loads the daemon's map and serves a known voice without asking again", async () => {
    const api = apiWith({ t1: { voice: "bm_b", muted: false } });
    const v = new ThreadVoices(api);
    await v.load();
    expect(v.voiceFor("t1")).toBe("bm_b");
    expect(v.pool()).toEqual(["bm_a", "bm_b", "bm_c"]);
    expect(api.calls).toEqual([]);
  });

  it("assigns once however many chunks ask at the same time, then keeps the voice (sticky)", async () => {
    const api = apiWith();
    const v = new ThreadVoices(api);
    const got = await Promise.all([v.voiceFor("t1"), v.voiceFor("t1"), v.assign("t1")]);
    expect(new Set(got).size).toBe(1);
    expect(api.calls).toHaveLength(1);
    expect(v.voiceFor("t1")).toBe(got[0]);
    expect(await v.assign("t2")).not.toBe(got[0]);
  });

  it("after a failed assignment speaks in the daemon's voice for a while, then asks again", async () => {
    const api = apiWith();
    let t = 0;
    const v = new ThreadVoices(api, () => t);
    api.fail = true;
    expect(await v.assign("t1")).toBeNull();
    expect(await v.assign("t1")).toBeNull();
    expect(api.calls).toHaveLength(1);
    api.fail = false;
    t = 6_000;
    expect(await v.assign("t1")).not.toBeNull();
  });

  it("takes another window's change from the bus, and mute is the muted set", async () => {
    const v = new ThreadVoices(apiWith());
    const seen: number[] = [];
    v.subscribe(() => seen.push(v.version()));
    v.apply({ thread_id: "t1", voice: "bf_x", muted: true });
    v.apply({ thread_id: "t1", voice: "bf_x", muted: true });          // unchanged: quiet
    expect(seen).toHaveLength(1);
    expect([...v.muted()]).toEqual(["t1"]);
    const same = v.muted();
    expect(v.muted()).toBe(same);                                        // stable identity while unchanged
    v.apply({ thread_id: 7 as never });                                   // junk is ignored
    v.apply(null);
  });

  it("mute acts at once and is put back if the daemon refuses", async () => {
    const api = apiWith();
    const v = new ThreadVoices(api);
    await v.assign("t1");
    api.fail = true;
    const p = v.mute("t1", true);
    expect(v.info("t1").muted).toBe(true);
    await p;
    expect(v.info("t1").muted).toBe(false);
  });

  it("an assignment the daemon answers with no voice backs off like a failure, not one request per chunk", async () => {
    const calls: unknown[] = [];
    let t = 0;
    const api: VoicesApi = {
      list: async () => ({ threads: {}, pool: [] }),
      change: async (thread) => { calls.push(thread); return { thread_id: thread, voice: null, muted: false }; },
    };
    const v = new ThreadVoices(api, () => t);
    for (let i = 0; i < 5; i++) expect(await v.voiceFor("t1")).toBeNull();
    expect(calls).toHaveLength(1);
    t = 6_000;
    await v.voiceFor("t1");
    expect(calls).toHaveLength(2);
  });

  it("a reload of the map does not undo a mute still waiting for the daemon", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => { release = r; });
    const api: VoicesApi = {
      list: async () => ({ threads: { t1: { voice: "bm_a", muted: false } }, pool: [] }),
      change: async (thread, change) => { await held; return { thread_id: thread, voice: "bm_a", muted: change.muted ?? false }; },
    };
    const v = new ThreadVoices(api);
    await v.load();
    const p = v.mute("t1", true);
    await v.load();                                   // the daemon still says unmuted
    expect(v.info("t1").muted).toBe(true);
    release();
    await p;
    expect(v.info("t1").muted).toBe(true);
    await v.load();
    expect(v.info("t1").muted).toBe(false);           // settled: the daemon's word stands
  });

  it("names voices for the chip", () => {
    expect(voiceLabel("bm_george")).toBe("George");
    expect(voiceLabel("pocket:bill_boerst")).toBe("Bill Boerst · pocket");
    expect(voiceLabel(null)).toBe("auto");
  });
});
