import { expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SpeechController, type SpeechEventLike } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

// Probe from the PR #389 round-4 Sol review.
it("a manual native steer's spoken answer must not mark the earlier voice send whose speech was cancelled", async () => {
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
    await clock.advance(10);
    emit("text", { text: "I will check the weather." });
    emit("tool_started", { call_id: "weather" });

    // Before synthesis finishes, the owner changes the request. Native steering
    // keeps the running turn id (daemon.py), while speech cancels its old audio.
    await clock.advance(10);
    const manual = Promise.resolve({ status: "steered", mode: "native", turn_id: "voice-turn", message_id: "manual" });
    expect(link.issue("A", [], () => manual)).toBe(manual);
    emit("user_message", { text: "Forget the weather. What is two plus two?", message_id: "manual", steer: true });
    await manual;
    controller.messageSent("A");
    await clock.advance(10);
    emit("tool_finished", { call_id: "weather" });
    emit("text", { text: "Two plus two is four." });
    emit("turn_finished", { stop: "end" });
    await clock.advance(1000);

    expect(player.started).toEqual(["Two plus two is four."]);
    const marks = diagnostics.snapshot().rows.find(row => row.id === id)!.marks!;
    expect(marks.firstToken).toBe(10);
    expect(marks.firstAudio, "only the later manual request's answer was spoken").toBeUndefined();
  } finally { controller.dispose(); }
});
