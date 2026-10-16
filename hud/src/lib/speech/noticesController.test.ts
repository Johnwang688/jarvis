// SV-5 through the speech controller: the bus's `subagent` record becomes one
// short spoken notice, under the same rules as a reply (which chats are read,
// a send, Stop, barge-in), and a child's prose is never read.

import { beforeEach, describe, expect, it } from "vitest";
import source from "./controller.ts?raw";
import { SpeechController } from "./controller";
import { NOTICE_STALE_MS, NOTICE_WINDOW_MS } from "./notices";
import { Floor, BACKLOG_MS, estimateMs } from "./floor";
import { SpeechQueue } from "./queue";
import { FakeClock, FakeGate, FakePlayer, FakeSay, FakeSynth } from "./testkit";

let clock: FakeClock;
let player: FakePlayer;
let say: FakeSay;
let gate: FakeGate;
let c: SpeechController;
let voices: Record<string, string>;

const T = "thread-1";
const bus = (sid: string, status: string, extra: Record<string, unknown> = {}, thread = T, op = "finished") =>
  c.event({ kind: "subagent", thread_id: thread, data: { sid, op, status, depth: 1, background: false, started_at: "2026-10-16T10:00:00Z", entries: 2, ...extra } });
const REPLY = "Right, here is what I found. The approval gate had seven separate ways past it, and every one was an older copy.";

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  say = new FakeSay(clock);
  gate = new FakeGate();
  voices = { [T]: "bf_emma" };
  c = new SpeechController({ say: say.fn, player, clock, gate, voiceFor: (t) => voices[t] ?? null });
  c.setSpeakers(new Set([T]));
});

describe("the notice", () => {
  it("one finished child is one short sentence in the chat's voice, after the window", async () => {
    bus("cl:toolu_a", "done");
    await clock.advance(NOTICE_WINDOW_MS - 1);
    expect(player.started).toEqual([]);
    await clock.advance(NOTICE_WINDOW_MS);
    expect(player.started).toEqual(["A sub-agent finished."]);
    expect(say.calls).toEqual([{ text: "A sub-agent finished.", voice: "bf_emma" }]);
    expect(c.items().past[0].kind).toBe("notice");
  });

  it("a burst is one sentence; a failure is counted apart", async () => {
    bus("cl:a", "done");
    bus("cl:b", "done");
    bus("cx:c", "failed");
    await clock.advance(NOTICE_WINDOW_MS + 5000);
    expect(player.started).toEqual(["Two sub-agents finished, one failed."]);
  });

  it("a stopped child is never spoken, nor one the owner asked to stop", async () => {
    bus("cl:a", "stopped", { stop_reason: "stopped by the owner" });
    bus("cl:b", "running", { stop: "stopping" }, T, "progress");
    bus("cl:b", "failed");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
  });

  it("a child's prose is never read, as text or as an inner event", async () => {
    c.event({ kind: "subagent_event", thread_id: T, data: { sid: "cl:a", event: { kind: "text", data: { text: "SECRET-CHILD-PROSE. It is quite long enough to be read aloud." } } } });
    c.event({ kind: "subagent_event", thread_id: T, data: { sid: "cl:a", text: "SECRET-CHILD-TOP. It is quite long enough to be read aloud." } });
    bus("cl:a", "done", { name: "SECRET-NAME", kind: "reviewer", last_tool: "Bash", stop_reason: "SECRET-REASON", text: "SECRET-TEXT. And a second sentence here." });
    await clock.advance(60_000);
    expect(player.started).toEqual(["A sub-agent finished."]);
    expect(JSON.stringify([say.calls, c.items()])).not.toMatch(/SECRET|reviewer|Bash/);
  });

  it("its own reply and a notice share the queue without either being taken for the other", async () => {
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "end" } });
    bus("cl:a", "done");
    await clock.advance(60_000);
    expect(player.started.slice(0, -1).join(" ")).toBe(REPLY);
    expect(player.started.at(-1)).toBe("A sub-agent finished.");
    expect(player.maxActive).toBe(1);
  });
});

describe("whose and when", () => {
  it("a chat in no pane, a muted chat, read-aloud off, and a window that is not the speaker are silent", async () => {
    bus("cl:a", "done", {}, "other");
    c.setMuted(new Set([T]));
    bus("cl:b", "done");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);

    c.setMuted(new Set());
    c.setEnabled(false);
    bus("cl:c", "done");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);

    c.setEnabled(true);
    gate.set(false);
    bus("cl:d", "done");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
  });

  it("a chat that is muted or leaves while the window is open drops the notice", async () => {
    bus("cl:a", "done");
    await clock.advance(1000);
    c.setMuted(new Set([T]));
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    c.setMuted(new Set());
    bus("cl:b", "done");
    await clock.advance(1000);
    c.setSpeakers(new Set());
    c.setSpeakers(new Set([T]));
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    expect(c.noticesWaiting(T)).toEqual({ done: 0, failed: 0 });
  });

  it("the main Stop: a child live then is silent when it later ends failed, even for a chat not read at the time", async () => {
    bus("cx:a", "running", {}, T, "started");
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "interrupted" } });
    bus("cx:a", "failed");
    c.setSpeakers(new Set());
    bus("cx:b", "running", {}, T, "started");
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "interrupted" } });
    c.setSpeakers(new Set([T]));
    bus("cx:b", "failed");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    // an ordinary turn end does not silence a child that runs on
    bus("cx:c", "backgrounded", {}, T, "started");
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "end" } });
    bus("cx:c", "done");
    await clock.advance(NOTICE_WINDOW_MS + 3000);
    expect(player.started).toEqual(["A sub-agent finished."]);
  });

  it("a fresh list of the chat's children teaches it a Stop it missed", async () => {
    c.noticeSeed(T, [{ sid: "cl:a", status: "running", stop: "stopping" }]);
    bus("cl:a", "failed");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
  });

  it("a notice that has not begun to sound within 30 s of being queued is dropped, not said late", async () => {
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    await clock.advance(400);
    c.pause();                                   // the owner paused a reply: nothing sounds, and what is queued waits
    bus("cl:a", "done");
    await clock.advance(NOTICE_WINDOW_MS + 100);
    expect(c.items().live.map((i) => i.kind).filter((k) => k === "notice")).toEqual(["notice"]);
    await clock.advance(NOTICE_STALE_MS - 1000);
    expect(c.items().live.some((i) => i.kind === "notice")).toBe(true);       // not yet stale
    await clock.advance(1500);
    expect(c.items().live.some((i) => i.kind === "notice")).toBe(false);
    expect(c.items().past.find((i) => i.kind === "notice")).toMatchObject({ state: "dropped", mark: "stale" });
    const before = player.started.length;
    c.resume();
    await clock.advance(60_000);
    expect(player.started.slice(before)).not.toContain("A sub-agent finished.");
    expect(NOTICE_STALE_MS).toBe(30_000);
  });

  it("a notice held back (a barge-in on a summary, say) that goes stale is never queued; one sounding is spared", async () => {
    bus("cl:a", "done");
    await clock.advance(NOTICE_WINDOW_MS + 200);   // queued, synthesizing
    await clock.advance(300);
    expect(player.started).toEqual(["A sub-agent finished."]);
    await clock.advance(NOTICE_STALE_MS + 5000);   // its timer fires after it played: nothing to drop
    expect(c.items().past.at(-1)).toMatchObject({ kind: "notice", state: "done" });
  });

  it("an owner's Stop heard while the chat is not read still silences that child's end once it is", async () => {
    c.setSpeakers(new Set());
    bus("cl:a", "running", { stop: "stopping" }, T, "progress");
    c.setSpeakers(new Set([T]));
    bus("cl:a", "failed");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
  });

  it("the sub-agent switch: off says nothing and drops what waits", async () => {
    bus("cl:a", "done");
    c.setNotices(false);
    bus("cl:b", "done");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    expect(c.noticesOn()).toBe(false);
    c.setNotices(true);
    bus("cl:c", "done");
    await clock.advance(NOTICE_WINDOW_MS + 3000);
    expect(player.started).toEqual(["A sub-agent finished."]);
  });

  it("an owner send, Stop, or an interrupted turn drops a notice not yet said", async () => {
    bus("cl:a", "done");
    c.event({ kind: "user_message", thread_id: T, data: { text: "hello" } });
    await clock.advance(60_000);
    expect(player.started).toEqual([]);

    bus("cl:b", "done");
    c.stop();
    await clock.advance(60_000);
    expect(player.started).toEqual([]);

    bus("cl:c", "done");
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "interrupted" } });
    await clock.advance(60_000);
    expect(player.started).toEqual([]);

    // an ordinary end of turn keeps it
    bus("cl:d", "done");
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "end" } });
    await clock.advance(60_000);
    expect(player.started).toEqual(["A sub-agent finished."]);
  });

  it("Stop clears a notice already queued; a send to another chat does not touch it", async () => {
    c.setSpeakers(new Set([T, "t2"]));
    bus("cl:a", "done");
    c.event({ kind: "user_message", thread_id: "t2", data: { text: "hi" } });
    await clock.advance(NOTICE_WINDOW_MS + 100);
    expect(player.started).toEqual(["A sub-agent finished."]);
  });

  it("waits while the owner is talking, and says it when the owner is done", async () => {
    let talking = true;
    c.setOwnerBusy(() => talking);
    bus("cl:a", "done");
    await clock.advance(NOTICE_WINDOW_MS + 5000);
    expect(player.started).toEqual([]);
    talking = false;
    await clock.advance(2000);
    expect(player.started).toEqual(["A sub-agent finished."]);
  });

  it("a barge-in that paused the reply holds the notice behind it until the owner lets go", async () => {
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    await clock.advance(400);
    expect(c.bargeIn()).toBe(true);
    bus("cl:a", "done");
    await clock.advance(NOTICE_WINDOW_MS + 5000);
    expect(player.started).not.toContain("A sub-agent finished.");
    c.endBargeIn();
    await clock.advance(60_000);
    expect(player.started).toContain("A sub-agent finished.");
    expect(player.maxActive).toBe(1);
  });

  it("a recording under way when the window closes holds the notice until it ends", async () => {
    bus("cl:a", "done");
    await clock.advance(NOTICE_WINDOW_MS - 100);
    c.setOwnerBusy(() => true);
    await clock.advance(5000);
    expect(player.started).toEqual([]);
    c.setOwnerBusy(null);
    await clock.advance(2000);
    expect(player.started).toEqual(["A sub-agent finished."]);
  });
});

describe("a notice gone stale", () => {
  it("one held back by a barge-in past the bound is never queued when it is let go", async () => {
    const hung = new Promise<string>(() => {});
    const held = new SpeechController({ say: say.fn, player, clock, gate, summarize: () => hung });
    held.setSpeakers(new Set([T]));
    const long = Array.from({ length: 12 }, (_, i) => `This is sentence number ${i} of a long reply, and it says one more thing.`).join(" ");
    held.event({ kind: "text", thread_id: T, data: { text: long } });     // a summary is awaited, nothing sounds
    expect(held.bargeIn()).toBe(true);                                    // so the barge-in holds deliveries
    held.event({ kind: "subagent", thread_id: T, data: { sid: "cl:a", op: "finished", status: "done" } });
    await clock.advance(NOTICE_WINDOW_MS + 8000);                         // the summary gives up; the notice is held
    await clock.advance(NOTICE_STALE_MS);
    held.endBargeIn();
    await clock.advance(60_000);
    expect(player.started).not.toContain("A sub-agent finished.");
    expect(held.items().past.concat(held.items().live).some((i) => i.kind === "notice")).toBe(false);
  });

  it("a notice that is already sounding is spared by the drop", async () => {
    const q = new SpeechQueue({ synth: new FakeSynth(clock).fn, player, clock, floor: new Floor(clock) });
    q.enqueue({ thread: T, messageKey: "t:notice:1", seq: 0, text: "x".repeat(2000), kind: "notice" });
    await clock.advance(500);
    expect(q.items()[0].state).toBe("playing");
    q.dropUnstarted("t:notice:1");
    expect(q.items().map((i) => i.state)).toEqual(["playing"]);
  });
});

describe("in the shared queue", () => {
  it("a notice is never collapsed as a backlog update, and never takes a reply's place as final", async () => {
    const clock2 = new FakeClock();
    const synth = new FakeSynth(clock2);
    synth.hang = () => true;
    const q = new SpeechQueue({ synth: synth.fn, player: new FakePlayer(clock2), clock: clock2, floor: new Floor(clock2) });
    const long = (tag: string, seconds: number) => `${tag} `.padEnd(Math.round(seconds * 16), "x");
    q.enqueue({ thread: "a", messageKey: "a:notice:1", seq: 0, text: "A sub-agent finished.", kind: "notice" });
    q.endMessage("a:notice:1");
    for (let i = 0; i < 4; i++) { q.enqueue({ thread: "a", messageKey: `a:${i}`, seq: 0, text: long(`u${i}`, 20) }); q.endMessage(`a:${i}`); }
    q.enqueue({ thread: "a", messageKey: "a:notice:2", seq: 0, text: "A sub-agent failed.", kind: "notice" });
    q.endMessage("a:notice:2");
    q.turnEnded("a");
    const live = q.items().filter((i) => i.thread === "a");
    const texts = live.map((i) => i.text);
    expect(texts).toContain("A sub-agent finished.");
    expect(texts).toContain("A sub-agent failed.");
    expect(texts.some((t) => t.startsWith("u3"))).toBe(true);     // u3 is the reply, not the later notice
    expect(texts.some((t) => t.startsWith("u0"))).toBe(false);    // the oldest interim went
    expect(live.reduce((n, i) => n + (i.kind === "note" || i.kind === "notice" ? 0 : estimateMs(i.text)), 0)).toBeLessThanOrEqual(BACKLOG_MS);
  });
});

describe("the controller's own wiring", () => {
  it("reads a sub-agent record only through the notices, and never a child's inner event", () => {
    const code = source.replace(/\/\/.*$/gm, "");
    expect(code).toMatch(/kind === "subagent"/);
    expect(code).not.toMatch(/subagent_event/);
    expect(code).not.toMatch(/\.event\??\.data|data\.event/);
  });
});
