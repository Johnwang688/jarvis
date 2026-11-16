import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SpeechController, type SpeechEventLike } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

function harness() {
  const clock = new FakeClock();
  const diagnostics = new VoiceDiagnostics(() => clock.now());
  const player = new FakePlayer(clock);
  const controller = new SpeechController({
    clock, player, say: new FakeSay(clock).fn, gate: new FakeGate(), timingNow: () => clock.now(),
  });
  controller.setSpeakers(new Set(["A"]));
  const link = new VoiceTimingsLink(diagnostics, () => clock.now(), chat => controller.audioTextStartedAt(chat));
  controller.onAudioStart(chat => link.firstAudio(chat));
  controller.subscribe(() => link.retain(chat => controller.outputEnabled(chat)));
  const event = (e: SpeechEventLike & { turn_id?: string }) => { link.event(e); controller.event(e); };
  const emit = (kind: string, turn_id: string, data: SpeechEventLike["data"] = {}) =>
    event({ kind, thread_id: "A", turn_id, data });
  const id = diagnostics.add(1000, "wake", "transcribing");
  for (const stage of ["speechEnd", "endpoint", "transcribed"] as const) diagnostics.mark(id, stage, 0);
  let resolve!: (answer: unknown) => void;
  const request = new Promise<unknown>(ok => { resolve = ok; });
  const returned = link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => request);
  const acknowledge = async () => {
    resolve({ status: "started", turn_id: "voice" });
    await returned;
    controller.messageSent("A"); // App's acknowledgement hook.
  };
  const marks = () => diagnostics.snapshot().rows.find(row => row.id === id)!.marks!;
  return { clock, player, controller, link, event, emit, request, returned, acknowledge, marks };
}

describe("VoiceTimingsLink: probes from the PR #389 round-3 Sol review", () => {
  it("common case: one AUTO send, one reply, real playback gets every mark", async () => {
    const h = harness();
    try {
      expect(h.returned).toBe(h.request);
      h.emit("user_message", "voice", { text: "Voice request." });
      await h.acknowledge();
      h.emit("turn_started", "voice");
      await h.clock.advance(10);
      h.emit("text_delta", "voice", { text: "The voice reply." });
      await h.clock.advance(10);
      h.emit("text", "voice", { text: "The voice reply." });
      h.emit("turn_finished", "voice", { stop: "end" });
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["The voice reply."]);
      expect(h.marks()).toEqual({ speechEnd: 0, endpoint: 0, transcribed: 0, sent: 0, firstToken: 10, firstAudio: 120 });
    } finally { h.controller.dispose(); }
  });

  it("HIGH: an HTTP answer after the reply ended must not admit a later manual turn's speech", async () => {
    const h = harness();
    try {
      h.emit("user_message", "voice", { text: "Voice request." });
      h.emit("turn_started", "voice");
      await h.clock.advance(10);
      h.emit("text", "voice", { text: "The voice reply." });
      h.emit("turn_finished", "voice", { stop: "end" });
      // HTTP and SSE use separate connections. Another client's manual send
      // starts while our voice send's HTTP response is still delayed.
      await h.clock.advance(10);
      h.emit("user_message", "manual", { text: "Manual request.", message_id: "manual" });
      h.emit("turn_started", "manual");
      await h.clock.advance(10);
      await h.acknowledge();
      expect(h.marks().firstToken).toBe(10);
      await h.clock.advance(10);
      h.emit("text", "manual", { text: "The manual reply." });
      h.emit("turn_finished", "manual", { stop: "end" });
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["The manual reply."]);
      expect(h.marks().firstAudio, "manual speech must not mark the voice send").toBeUndefined();
    } finally { h.controller.dispose(); }
  });

  it("HIGH: a gap hiding reply end and manual turn start must not admit manual speech", async () => {
    const h = harness();
    try {
      h.emit("user_message", "voice", { text: "Voice request." });
      await h.acknowledge();
      h.emit("turn_started", "voice");
      await h.clock.advance(10);
      h.emit("text", "voice", { text: "The voice reply." });
      h.event({ kind: "_disconnected" });
      // The user types a manual request while SSE is disconnected. Its HTTP
      // answer still arrives; App cancels the previous speech as usual.
      // The voice turn end and manual turn start are not replayed by SSE.
      await h.clock.advance(10);
      const manual = Promise.resolve({ status: "started", turn_id: "manual" });
      expect(h.link.issue("A", [], () => manual)).toBe(manual);
      await manual;
      h.controller.messageSent("A");
      await h.clock.advance(1000); // subscribe() retries after one second.
      h.event({ kind: "_connected" });
      await h.clock.advance(10);
      h.emit("text", "manual", { text: "The manual reply." });
      h.emit("turn_finished", "manual", { stop: "end" });
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["The manual reply."]);
      expect(h.marks().firstToken).toBe(10);
      expect(h.marks().firstAudio, "manual speech after the gap must not mark the voice send").toBeUndefined();
    } finally { h.controller.dispose(); }
  });
});
