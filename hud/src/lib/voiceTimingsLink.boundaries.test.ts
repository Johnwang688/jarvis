import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink, REPLY_TIMING_TTL_MS } from "./voiceTimingsLink";
import type { SpeechEventLike } from "./speech/controller";

// Lost-boundary follow-ups to VA-0 (PR #387 edge cases a–c): when a reply's end
// is not observed, the link forgets it and omits marks rather than mis-attribute.
function harness() {
  let t = 0;
  const diagnostics = new VoiceDiagnostics(() => t);
  const link = new VoiceTimingsLink(diagnostics, () => t);
  const emit = (kind: string, data?: SpeechEventLike["data"], chat = "A") =>
    link.event({ kind, thread_id: chat, ...(data === undefined ? {} : { data }) });
  const row = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", t);
    diagnostics.mark(id, "transcribed", t);
    diagnostics.finish(id, "handed_to_chat", 0);
    return id;
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(r => r.id === id)!.marks!;
  return { link, emit, row, marks, advance: (ms: number) => { t += ms; }, now: () => t };
}

describe("VoiceTimingsLink: lost turn boundaries", () => {
  it("a send queued behind a live turn is dropped at a stream gap, so the old turn's continuation cannot claim it", () => {
    const h = harness();
    h.emit("turn_started"); h.emit("text", { text: "Old turn." });
    h.advance(100);
    const id = h.row(); h.link.sent("A", [id]);
    h.link.event({ kind: "_disconnected" }); h.link.event({ kind: "_connected" });
    h.advance(100);
    h.emit("text", { text: "Old turn continues after the gap." });
    h.emit("turn_finished"); h.emit("turn_started"); h.emit("text", { text: "The send's reply." });
    expect(h.marks(id)).toEqual({ speechEnd: 100, transcribed: 100, sent: 100 });
  });

  it("a send with no live turn survives a gap", () => {
    const h = harness();
    const id = h.row(); h.link.sent("A", [id]);
    h.link.event({ kind: "_disconnected" }); h.link.event({ kind: "_connected" });
    h.advance(50);
    h.emit("text_delta", { text: "Reply." });
    expect(h.marks(id).firstToken).toBe(50);
  });

  it("a live turn silent for the TTL is forgotten without a gap, and the send behind it with it", () => {
    const h = harness();
    h.emit("turn_started"); h.emit("text", { text: "Turn that never reports its end." });
    const behind = h.row(); h.link.sent("A", [behind]);
    h.advance(REPLY_TIMING_TTL_MS);
    const id = h.row(); h.link.sent("A", [id]);
    h.advance(10);
    h.emit("text", { text: "Turn-less reply." });
    expect(h.marks(behind).firstToken).toBeUndefined();
    expect(h.marks(id).firstToken).toBe(REPLY_TIMING_TTL_MS + 10);
  });

  it("a turn's own events keep it live past the TTL", () => {
    const h = harness();
    h.emit("turn_started");
    for (let n = 0; n < 3; n++) { h.advance(REPLY_TIMING_TTL_MS - 1); h.emit("tool_started"); }
    const id = h.row(); h.link.sent("A", [id]);
    h.advance(10);
    h.emit("text", { text: "Still the long old turn." });
    expect(h.marks(id).firstToken).toBeUndefined();
    h.emit("turn_finished"); h.emit("turn_started"); h.emit("text", { text: "The send's reply." });
    expect(h.marks(id).firstToken).toBe(h.now());
  });

  it("another chat's activity does not keep a silent turn live", () => {
    const h = harness();
    h.emit("turn_started", undefined, "A");
    h.advance(REPLY_TIMING_TTL_MS / 2); h.emit("tool_started", undefined, "B");
    h.advance(REPLY_TIMING_TTL_MS / 2);
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("text", { text: "Turn-less reply." }, "A");
    expect(h.marks(id).firstToken).toBe(REPLY_TIMING_TTL_MS);
  });
});

describe("VoiceTimingsLink: error severity", () => {
  it("an error with fatal:\"true\" (not the boolean) does not end a turn", () => {
    const h = harness();
    h.emit("turn_started");
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("error", { fatal: "true" });
    h.advance(10); h.emit("text", { text: "Old turn continues." });
    expect(h.marks(id).firstToken).toBeUndefined();
  });

  it("a turn-less message is still closed by an error of unknown severity", () => {
    const h = harness();
    h.emit("text_delta", { text: "Turn-less old message" });
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("error");
    h.advance(10); h.emit("text_delta", { text: "New message." });
    expect(h.marks(id).firstToken).toBe(10);
  });
});

describe("VoiceTimingsLink: steering", () => {
  it("a steer requeued after turn_finished (the daemon's leftover order) gets its marks from the next turn", () => {
    const h = harness();
    h.emit("turn_started");
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("user_message", { steer: true, message_id: "m1" });
    h.advance(10); h.emit("text", { text: "Final answer came first." });
    h.emit("turn_finished");
    h.emit("steer_queued", { message_id: "m1", turn_id: "t2" });
    h.advance(10); h.emit("turn_started"); h.emit("text", { text: "The steer's own turn." });
    expect(h.marks(id).firstToken).toBe(20);
  });

  it("a steer_queued for another message does not restore a steered send", () => {
    const h = harness();
    h.emit("turn_started");
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("user_message", { steer: true, message_id: "m1" });
    h.emit("steer_queued", { message_id: "other" });
    h.emit("turn_finished"); h.emit("turn_started");
    h.advance(10); h.emit("text", { text: "Some later turn." });
    expect(h.marks(id).firstToken).toBeUndefined();
  });

  it("a steer in another chat leaves this chat's send alone", () => {
    const h = harness();
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("user_message", { steer: true, message_id: "m1" }, "B");
    h.advance(10); h.emit("turn_started"); h.emit("text", { text: "Reply." });
    expect(h.marks(id).firstToken).toBe(10);
  });

  it("a steer after the send already got its first token changes nothing", () => {
    const h = harness();
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("turn_started"); h.advance(10); h.emit("text_delta", { text: "Reply" });
    h.emit("user_message", { steer: true, message_id: "m1" });
    h.advance(10); h.link.firstAudio("A");
    expect(h.marks(id)).toMatchObject({ firstToken: 10, firstAudio: 20 });
  });
});

// The production path: App issues the send through `issue`, the daemon answers
// with the turn that replies (`SendResult`), and every turn event carries its id.
describe("VoiceTimingsLink: matched by the send's answer", () => {
  function idHarness() {
    const h = harness();
    const ev = (kind: string, turn: string, data?: SpeechEventLike["data"]) =>
      h.link.event({ kind, thread_id: "A", turn_id: turn, ...(data === undefined ? {} : { data }) } as SpeechEventLike);
    const send = (answer: unknown) => {
      const id = h.row();
      let settle!: (v: unknown) => void, fail!: (e: unknown) => void;
      const p = new Promise((ok, no) => { settle = ok; fail = no; });
      const back = h.link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => p);
      return { id, back, p, answer: async () => { settle(answer); await p; await Promise.resolve(); }, fail };
    };
    return { ...h, ev, send };
  }

  it("a queued send's mark comes from its queued turn, not the running turn's later text", async () => {
    const h = idHarness();
    h.ev("turn_started", "old");
    const s = h.send({ status: "queued", turn_id: "old", queued_turn_id: "q1", message_id: "m" });
    await s.answer();
    h.advance(10); h.ev("text", "old", { text: "Old turn keeps talking." });
    h.ev("turn_finished", "old");
    h.advance(10); h.ev("turn_started", "q1"); h.ev("text_delta", "q1", { text: "Reply." });
    expect(h.marks(s.id).firstToken).toBe(20);
  });

  it("text that arrives before the answer is credited once the answer names its turn", async () => {
    const h = idHarness();
    const s = h.send({ status: "started", turn_id: "t1" });
    h.ev("turn_started", "t1"); h.advance(5); h.ev("text_delta", "t1", { text: "Fast reply" });
    h.advance(5);
    await s.answer();
    expect(h.marks(s.id).firstToken).toBe(5);
  });

  it("an answer naming another turn leaves earlier text uncredited", async () => {
    const h = idHarness();
    const s = h.send({ status: "queued", turn_id: "x", queued_turn_id: "q1" });
    h.ev("turn_started", "x"); h.advance(5); h.ev("text", "x", { text: "Someone else's reply." });
    await s.answer();
    expect(h.marks(s.id).firstToken).toBeUndefined();
  });

  it("a native steer answer marks nothing until a steer_queued for its own message names a turn", async () => {
    const h = idHarness();
    h.ev("turn_started", "old");
    const s = h.send({ status: "steered", mode: "native", turn_id: "old", message_id: "mine" });
    await s.answer();
    h.ev("user_message", "old", { steer: true, message_id: "mine" });
    h.advance(10); h.ev("text", "old", { text: "Steer answered in the old turn." });
    h.ev("turn_finished", "old");
    h.ev("steer_queued", "other-turn", { message_id: "other", turn_id: "other-turn" });
    h.ev("turn_started", "other-turn"); h.ev("text", "other-turn", { text: "Not mine." });
    expect(h.marks(s.id).firstToken).toBeUndefined();
  });

  it("a native steer requeued under its own message id is credited from that turn", async () => {
    const h = idHarness();
    h.ev("turn_started", "old");
    const s = h.send({ status: "steered", mode: "native", turn_id: "old", message_id: "mine" });
    await s.answer();
    h.ev("turn_finished", "old");
    h.ev("steer_queued", "t-mine", { message_id: "mine", turn_id: "t-mine" });
    h.ev("steer_queued", "t-other", { message_id: "other", turn_id: "t-other" });
    h.ev("turn_started", "t-other"); h.advance(10); h.ev("text", "t-other", { text: "Other runs first." });
    h.ev("turn_finished", "t-other");
    h.ev("turn_started", "t-mine"); h.advance(10); h.ev("text", "t-mine", { text: "Mine." });
    expect(h.marks(s.id).firstToken).toBe(20);
  });

  it("an interrupting send is matched by its queued turn", async () => {
    const h = idHarness();
    h.ev("turn_started", "old");
    const s = h.send({ status: "steered", mode: "interrupt", turn_id: "old", queued_turn_id: "q2" });
    await s.answer();
    h.advance(10); h.ev("text", "old", { text: "Old tail." });
    h.ev("turn_finished", "old"); h.ev("turn_started", "q2"); h.ev("text", "q2", { text: "Reply." });
    expect(h.marks(s.id).firstToken).toBe(10);
  });

  it("a dropped send is forgotten", async () => {
    const h = idHarness();
    const s = h.send({ status: "dropped", reason: "stopped", turn_id: "old" });
    await s.answer();
    h.ev("turn_started", "t9"); h.ev("text", "t9", { text: "Later." });
    expect(h.marks(s.id).firstToken).toBeUndefined();
  });

  it("issue returns the send's own promise, and a rejection still reaches the caller", async () => {
    const h = idHarness();
    const s = h.send(undefined);
    expect(s.back).toBe(s.p);
    s.fail(new Error("409"));
    await expect(s.back).rejects.toThrow("409");
  });
});

describe("VoiceTimingsLink: audio must come from the send's own reply", () => {
  function audioHarness() {
    let t = 0, playingTextAt: number | undefined;
    const diagnostics = new VoiceDiagnostics(() => t);
    const link = new VoiceTimingsLink(diagnostics, () => t, () => playingTextAt);
    const ev = (kind: string, turn: string, data?: SpeechEventLike["data"]) =>
      link.event({ kind, thread_id: "A", turn_id: turn, ...(data === undefined ? {} : { data }) } as SpeechEventLike);
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", 0); diagnostics.mark(id, "transcribed", 0);
    link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => ({ status: "started", turn_id: "mine" }));
    const marks = () => diagnostics.snapshot().rows.find(r => r.id === id)!.marks!;
    return { link, ev, marks, at: (ms: number) => { t = ms; }, playing: (v: number) => { playingTextAt = v; } };
  }

  it("audio whose text began inside the reply is credited", () => {
    const h = audioHarness();
    h.ev("turn_started", "mine"); h.at(10); h.ev("text", "mine", { text: "One." });
    h.at(20); h.ev("text", "mine", { text: "Two." }); h.ev("turn_finished", "mine");
    h.at(300); h.playing(20); h.link.firstAudio("A");
    expect(h.marks()).toMatchObject({ firstToken: 10, firstAudio: 300 });
  });

  it("audio whose text began after the reply ended is omitted", () => {
    const h = audioHarness();
    h.ev("turn_started", "mine"); h.at(10); h.ev("text", "mine", { text: "One." }); h.ev("turn_finished", "mine");
    h.at(30); h.ev("turn_started", "next"); h.ev("text", "next", { text: "Later message." });
    h.at(300); h.playing(30); h.link.firstAudio("A");
    expect(h.marks().firstAudio).toBeUndefined();
  });

  it("another message's text on the reply's end tick makes that edge unsure", () => {
    const h = audioHarness();
    h.ev("turn_started", "mine"); h.at(10); h.ev("text", "mine", { text: "One." }); h.ev("turn_finished", "mine");
    h.ev("turn_started", "next"); h.ev("text", "next", { text: "Same tick." });
    h.at(300); h.playing(10); h.link.firstAudio("A");
    expect(h.marks().firstAudio).toBeUndefined();
  });

  it("another message's text on the first token's tick makes that edge unsure", () => {
    const h = audioHarness();
    h.ev("turn_started", "older"); h.at(10); h.ev("text_delta", "older", { text: "Older tail" });
    h.ev("turn_finished", "older"); h.ev("turn_started", "mine"); h.ev("text", "mine", { text: "Mine." });
    h.at(300); h.playing(10); h.link.firstAudio("A");
    expect(h.marks().firstToken).toBe(10);
    expect(h.marks().firstAudio).toBeUndefined();
  });
});

describe("VoiceTimingsLink: steers without the send's answer", () => {
  it("with turn ids, any steer after the send leaves its owner unknown: nothing is marked", () => {
    const h = harness();
    const ev = (kind: string, turn: string, data?: SpeechEventLike["data"]) =>
      h.link.event({ kind, thread_id: "A", turn_id: turn, ...(data === undefined ? {} : { data }) } as SpeechEventLike);
    ev("turn_started", "old");
    const id = h.row(); h.link.sent("A", [id]);
    ev("user_message", "old", { steer: true, message_id: "s1" });
    ev("turn_finished", "old");
    ev("steer_queued", "t-s1", { message_id: "s1", turn_id: "t-s1" });
    ev("turn_started", "t-s1"); h.advance(10); ev("text", "t-s1", { text: "Whose?" });
    expect(h.marks(id).firstToken).toBeUndefined();
  });
});
