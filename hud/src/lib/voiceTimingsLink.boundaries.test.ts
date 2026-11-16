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
