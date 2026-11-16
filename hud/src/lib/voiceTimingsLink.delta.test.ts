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
  const emit = (kind: string, turn_id: string, data: SpeechEventLike["data"] = {}) => {
    const e = { kind, thread_id: "A", turn_id, data };
    link.event(e); controller.event(e); // App observes before speech.
  };
  const send = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", clock.now());
    diagnostics.mark(id, "transcribed", clock.now());
    let resolve!: (answer: unknown) => void;
    let reject!: (error: unknown) => void;
    const request = new Promise<unknown>((ok, no) => { resolve = ok; reject = no; });
    const returned = link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => request);
    return { id, request, returned, resolve, reject };
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(row => row.id === id)!.marks!;
  return { clock, controller, diagnostics, player, link, emit, send, marks };
}

describe("VoiceTimingsLink: probes from the PR #389 round-2 Sol review", () => {
  it("HIGH: another steer's requeue cannot identify an unacknowledged voice send", async () => {
    const h = harness();
    try {
      h.emit("turn_started", "old");
      const voice = h.send();
      h.emit("user_message", "old", { message_id: "voice", steer: true });
      // The provider consumes the voice steer, so it will never be requeued.
      // A later manual steer (another client) misses the final answer.
      h.emit("user_message", "old", { message_id: "manual", steer: true });
      await h.clock.advance(10);
      h.emit("turn_finished", "old", { stop: "end", next: 1 });
      h.emit("steer_queued", "manual-turn", { message_id: "manual", turn_id: "manual-turn" });
      h.emit("queued_started", "manual-turn", { message_id: "manual", turn_id: "manual-turn" });
      h.emit("turn_started", "manual-turn");
      await h.clock.advance(10);
      h.emit("text", "manual-turn", { text: "Answer to the manual message." });
      // HTTP is delayed relative to SSE; this is the voice send's real answer.
      voice.resolve({ status: "steered", mode: "native", turn_id: "old", message_id: "voice" });
      await voice.returned;
      // App also calls this after the HTTP answer. It cannot undo the mark.
      h.controller.messageSent("A");
      expect(h.marks(voice.id).firstToken, "manual text must not mark the voice send").toBeUndefined();
    } finally { h.controller.dispose(); }
  });

  it("HIGH: later manual reply audio cannot consume a cancelled voice reply's receipt", async () => {
    const h = harness();
    try {
      const voice = h.send();
      h.emit("user_message", "voice-turn", { text: "Voice request." });
      voice.resolve({ status: "started", turn_id: "voice-turn" });
      await voice.returned;
      h.controller.messageSent("A"); // App's HTTP acknowledgement hook.
      h.emit("turn_started", "voice-turn");
      await h.clock.advance(10);
      h.emit("text", "voice-turn", { text: "The voice reply is synthesizing." });
      h.emit("turn_finished", "voice-turn", { stop: "end" });
      await h.clock.advance(10); // Synthesis takes 100ms.
      const manual = Promise.resolve({ status: "started", turn_id: "manual-turn" });
      expect(h.link.issue("A", [], () => manual)).toBe(manual); // App's manual Send has no AUTO rows.
      h.emit("user_message", "manual-turn", { text: "A different request." });
      await manual;
      h.controller.messageSent("A");
      h.emit("turn_started", "manual-turn");
      await h.clock.advance(10);
      h.emit("text", "manual-turn", { text: "Answer to the manual message." });
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["Answer to the manual message."]);
      expect(h.marks(voice.id).firstToken).toBe(10);
      expect(h.marks(voice.id).firstAudio, "manual audio must not mark the voice send").toBeUndefined();
    } finally { h.controller.dispose(); }
  });

  it.each([
    { status: "started", turn_id: "own" },
    { status: "queued", turn_id: "old", queued_turn_id: "own", message_id: "voice" },
    { status: "steered", mode: "interrupt", turn_id: "old", queued_turn_id: "own", message_id: "voice" },
  ])("buffers early text and ignores stale acknowledgements ($status/$mode)", async answer => {
    const h = harness();
    try {
      const earlier = h.send();
      const current = h.send();
      h.emit("turn_started", "old");
      await h.clock.advance(10);
      h.emit("text", "old", { text: "Unrelated text." });
      h.emit("turn_finished", "old", { stop: "end" });
      h.emit("turn_started", "own");
      await h.clock.advance(10);
      h.emit("text_delta", "own", { text: "Own text." });
      expect(h.marks(current.id).firstToken).toBeUndefined();
      earlier.resolve({ status: "started", turn_id: "old" });
      await earlier.returned;
      expect(h.marks(earlier.id).firstToken).toBeUndefined();
      expect(h.marks(current.id).firstToken).toBeUndefined();
      current.resolve(answer);
      await current.returned;
      expect(h.marks(current.id).firstToken).toBe(20);
    } finally { h.controller.dispose(); }
  });

  it("preserves promise/rejection identity and absorbs observation errors without unhandled rejection", async () => {
    const h = harness();
    try {
      const send = h.send();
      expect(send.returned).toBe(send.request);
      const error = new Error("HTTP failure");
      send.reject(error);
      await expect(send.returned).rejects.toBe(error);
      // Exceptions while reading the fulfilled answer stay inside observation.
      const broken = h.send();
      const answer = { get status(): never { throw new Error("observation failure"); } };
      broken.resolve(answer);
      await expect(broken.returned).resolves.toBe(answer);
      const id = h.diagnostics.add(1000, "ptt", "transcribing");
      expect(() => h.link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => ({
        get then(): never { throw new Error("observation failure"); },
      }))).not.toThrow();
      // A synchronous exception from send itself remains the caller's exception.
      expect(() => h.link.issue("A", [], () => { throw error; })).toThrow(error);
      await h.clock.advance(0); // Drain the observer branches; Vitest detects unhandled rejections.
    } finally { h.controller.dispose(); }
  });
});
