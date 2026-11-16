import { expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SpeechController, type SpeechEventLike } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

// Probe from the PR #389 round-5 Sol review.
it("a manual native steer before the voice turn's first text must not credit its replacement answer to the voice send", async () => {
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
  const emit = (kind: string, data: SpeechEventLike["data"] = {}) => {
    const event = { kind, thread_id: "A", turn_id: "voice-turn", data };
    link.event(event);
    controller.event(event);
  };
  try {
    const id = diagnostics.add(1000, "wake", "transcribing");
    diagnostics.mark(id, "speechEnd", 0);
    diagnostics.mark(id, "transcribed", 0);
    const request = Promise.resolve({ status: "started", turn_id: "voice-turn" });
    expect(link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => request)).toBe(request);
    emit("user_message", { text: "Check the weather.", message_id: "voice" });
    await request;
    controller.messageSent("A");
    emit("turn_started");

    // The provider is still thinking: no assistant text, hence no Span yet.
    await clock.advance(1000);
    const manual = Promise.resolve({ status: "steered", mode: "native", turn_id: "voice-turn", message_id: "manual" });
    expect(link.issue("A", [], () => manual)).toBe(manual);
    emit("user_message", { text: "Forget the weather. What is two plus two?", message_id: "manual", steer: true });
    await manual;
    controller.messageSent("A");
    await clock.advance(10);
    emit("text", { text: "Two plus two is four." });
    emit("turn_finished", { stop: "end" });
    await clock.advance(1000);

    expect(player.started).toEqual(["Two plus two is four."]);
    const marks = diagnostics.snapshot().rows.find(row => row.id === id)!.marks!;
    expect.soft(marks.firstToken, "the only text answers the later manual send").toBeUndefined();
    expect.soft(marks.firstAudio, "the only speech answers the later manual send").toBeUndefined();
  } finally { controller.dispose(); }
});
