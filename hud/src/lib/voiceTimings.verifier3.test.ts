import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink, REPLY_TIMING_TTL_MS } from "./voiceTimingsLink";
import { SpeechController, type SpeechEventLike } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

function harness(withSpeech = false) {
  const clock = new FakeClock();
  const diagnostics = new VoiceDiagnostics(() => clock.now());
  const player = new FakePlayer(clock);
  const controller = withSpeech ? new SpeechController({
    clock, player, say: new FakeSay(clock).fn, gate: new FakeGate(), timingNow: () => clock.now(),
  }) : undefined;
  controller?.setSpeakers(new Set(["A", "B"]));
  const link = new VoiceTimingsLink(diagnostics, () => clock.now(),
    controller ? chat => controller.audioTextStartedAt(chat) : undefined);
  controller?.onAudioStart(chat => link.firstAudio(chat));
  controller?.subscribe(() => link.retain(chat => controller.outputEnabled(chat)));
  const event = (e: SpeechEventLike) => { link.event(e); controller?.event(e); };
  const emit = (kind: string, data?: SpeechEventLike["data"], chat = "A") =>
    event({ kind, thread_id: chat, ...(data === undefined ? {} : { data }) });
  const row = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", clock.now());
    diagnostics.mark(id, "transcribed", clock.now());
    diagnostics.finish(id, "handed_to_chat", 0);
    return id;
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(r => r.id === id)!.marks!;
  return { clock, player, controller, link, event, emit, row, marks };
}

describe("VA-0 verifier 3: missing turn boundaries", () => {
  it.each(["text_delta", "text"])("recovers %s after reconnect loses both old finish and new start", async kind => {
    const h = harness();
    h.emit("turn_started");
    h.emit("text", { text: "Old turn, before the stream gap." });
    h.emit("tool_started");
    h.event({ kind: "_disconnected" });
    // The API can issue a send while SSE is disconnected. The old finish and
    // the queued send's turn_started occur in the gap and are not replayed
    // (src/api.ts subscribe explicitly documents this transport behavior).
    await h.clock.advance(100);
    const id = h.row(); h.link.sent("A", [id]);
    h.event({ kind: "_connected" }); // Real pseudo-events have no thread_id.
    await h.clock.advance(100);
    h.emit(kind, { text: "New send's reply, first observed after reconnect." });
    h.link.firstAudio("A");
    expect(h.marks(id), "a lost terminal event must not poison the turn-less fallback").toMatchObject({
      sent: 100, firstToken: 200, firstAudio: 200,
    });
  });

  it("does not retain a lost explicit turn forever after the pending receipt TTL", async () => {
    const h = harness();
    h.emit("turn_started"); h.emit("text", { text: "Old turn." });
    const abandoned = h.row(); h.link.sent("A", [abandoned]);
    h.event({ kind: "_disconnected" });
    await h.clock.advance(REPLY_TIMING_TTL_MS + 1);
    h.event({ kind: "_connected" });
    const id = h.row(); h.link.sent("A", [id]);
    await h.clock.advance(10);
    h.emit("text", { text: "Reply without a new observed turn_started." });
    expect(h.marks(abandoned).firstToken).toBeUndefined();
    expect(h.marks(id).firstToken, "expiry only clears pending; replies currently has no expiry/reset").toBe(REPLY_TIMING_TTL_MS + 11);
  });

  it("a fresh turn_started recovers normally even when the old turn never finishes", async () => {
    const h = harness();
    h.emit("turn_started"); h.emit("text", { text: "Old turn." });
    h.event({ kind: "_disconnected" }); h.event({ kind: "_connected" });
    for (let n = 0; n < 3; n++) {
      await h.clock.advance(100);
      const id = h.row(); h.link.sent("A", [id]);
      h.emit("turn_started"); h.emit("text", { text: "Fresh explicit reply." });
      expect(h.marks(id).firstToken).toBe(h.clock.now());
      // Deliberately no turn_finished on any iteration.
    }
  });

  for (const boundary of ["turn_finished", "fatal error"] as const) {
    it.each(["text_delta", "text"])(`${boundary} allows a new %s reply without turn_started`, async kind => {
      const h = harness();
      h.emit("turn_started"); h.emit("text", { text: "Old turn." });
      if (boundary === "turn_finished") h.emit("turn_finished", { stop: "end" });
      else h.emit("error", { fatal: true, message: "Session closed." });
      await h.clock.advance(100);
      const id = h.row(); h.link.sent("A", [id]);
      await h.clock.advance(100);
      h.emit(kind, { text: "New fallback reply." }); h.link.firstAudio("A");
      expect(h.marks(id)).toMatchObject({ sent: 100, firstToken: 200, firstAudio: 200 });
    });
  }
});

describe("VA-0 verifier 3: error severity must be explicit", () => {
  it.each([
    { label: "no data", data: undefined },
    { label: "empty data", data: {} },
    { label: "message but no fatal", data: { message: "An unspecified error." } },
  ])("does not let $label end an older live turn and credit its continuation", async ({ data }) => {
    const h = harness();
    h.emit("turn_started"); h.emit("text", { text: "Old turn before a tool." }); h.emit("tool_started");
    await h.clock.advance(100);
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("error", data);
    await h.clock.advance(100);
    h.emit("text", { text: "The old turn continues; no terminal event occurred." });
    const beforeNewTurn = { ...h.marks(id) };
    h.emit("turn_finished"); h.emit("turn_started");
    await h.clock.advance(100);
    h.emit("text", { text: "The queued send's actual reply." });
    expect.soft(beforeNewTurn.firstToken, "unknown severity is not an explicit fatal error").toBeUndefined();
    expect(h.marks(id).firstToken, "only the queued send's new turn should own this mark").toBe(300);
  });

  it("explicit fatal:false preserves an older turn until turn_finished", async () => {
    const h = harness();
    h.emit("turn_started");
    await h.clock.advance(100);
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("error", { fatal: false });
    h.emit("text", { text: "Still the old turn." }); h.link.firstAudio("A");
    expect(h.marks(id).firstToken).toBeUndefined();
    expect(h.marks(id).firstAudio).toBeUndefined();
    h.emit("turn_finished"); h.emit("turn_started");
    await h.clock.advance(100); h.emit("text", { text: "New turn." });
    expect(h.marks(id).firstToken).toBe(200);
  });
});

describe("VA-0 verifier 3: native steering", () => {
  it("omits both marks for a genuine steering response within the same turn, even when audible", async () => {
    const h = harness(true);
    try {
      h.emit("turn_started"); h.emit("text_delta", { text: "An unfinished old message" });
      await h.clock.advance(100);
      const id = h.row(); h.link.sent("A", [id]);
      h.emit("user_message", { steer: true, message_id: "native-steer" });
      h.emit("text", { text: "An unfinished old message ending late." });
      h.emit("tool_started");
      await h.clock.advance(100);
      h.emit("text", { text: "I applied your steering instruction." });
      h.emit("turn_finished", { stop: "end" });
      await h.clock.advance(200);
      expect(h.player.started).toContain("I applied your steering instruction.");
      expect(h.marks(id)).toEqual({ speechEnd: 100, transcribed: 100, sent: 100 });
    } finally { h.controller?.dispose(); }
  });

  it("does not transfer an omitted native-steer receipt to a later unrelated manual turn", async () => {
    const h = harness(true);
    try {
      h.emit("turn_started"); h.emit("text", { text: "Old response." });
      await h.clock.advance(100);
      const id = h.row(); h.link.sent("A", [id]);
      h.emit("user_message", { steer: true, message_id: "native-steer" });
      h.emit("text", { text: "Steering instruction answered within the same turn." });
      h.emit("turn_finished", { stop: "end" });
      await h.clock.advance(5000);
      expect(h.marks(id).firstToken).toBeUndefined();
      expect(h.marks(id).firstAudio).toBeUndefined();
      // Manual sends have no AUTO diagnostics rows, exactly as App's issue()
      // hook does. This later request is unrelated to the already answered steer.
      h.link.issue("A", [], () => {});
      h.emit("user_message", { message_id: "later-manual" });
      h.emit("turn_started");
      h.emit("text", { text: "The later manual message's unrelated answer." });
      h.emit("turn_finished", { stop: "end" });
      await h.clock.advance(5000);
      expect(h.player.started).toContain("The later manual message's unrelated answer.");
      expect(h.marks(id), "omitted steering marks must stay absent, not adopt a future reply").toEqual({
        speechEnd: 100, transcribed: 100, sent: 100,
      });
    } finally { h.controller?.dispose(); }
  });
});

describe("VA-0 verifier 3: observation order at identical clock ticks", () => {
  it("credits a reply that starts after sent even when the clock is equal", () => {
    const h = harness(), id = h.row();
    h.link.sent("A", [id]); h.emit("turn_started"); h.emit("text", { text: "New reply." });
    expect(h.marks(id).firstToken).toBe(0);
  });

  it("does not credit a reply that starts before sent at the same tick", () => {
    const h = harness(); h.emit("turn_started");
    const id = h.row(); h.link.sent("A", [id]);
    h.emit("text", { text: "Old reply." }); h.link.firstAudio("A");
    expect(h.marks(id).firstToken).toBeUndefined();
    expect(h.marks(id).firstAudio).toBeUndefined();
  });
});
