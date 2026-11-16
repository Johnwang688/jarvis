import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink, REPLY_TIMING_TTL_MS } from "./voiceTimingsLink";
import { SpeechController, type SpeechEventLike } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

// Probes from the independent (GPT-6.1 Sol) review of PR #389. Match App's
// observation-before-speech ordering and use
// real speech correlation, rather than calling firstAudio without source identity.
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
  const send = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", clock.now());
    diagnostics.mark(id, "transcribed", clock.now());
    diagnostics.finish(id, "handed_to_chat", 0);
    link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => {});
    return id;
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(r => r.id === id)!.marks!;
  const checkOmitted = async (id: number, unrelatedText: string) => {
    await clock.advance(1000);
    expect(player.started).toContain(unrelatedText);
    expect.soft(marks(id).firstToken, "text belongs to another send").toBeUndefined();
    expect.soft(marks(id).firstAudio, "audio belongs to another send").toBeUndefined();
  };
  return { clock, controller, event, emit, send, checkOmitted };
}

describe("VoiceTimingsLink: attribution probes from the PR #389 Sol review", () => {
  it("a reconnect must not make a continuing old turn claim a send issued after the gap", async () => {
    const h = harness();
    try {
      h.emit("turn_started", "old");
      // An approval wait makes deliver() queue the voice send instead of steer.
      h.emit("tool_started", "old", { call_id: "approved-tool" });
      h.emit("approval_requested", "old", { req_id: "approval" });
      h.event({ kind: "_disconnected" });
      await h.clock.advance(100);
      h.event({ kind: "_connected" });
      const id = h.send();
      h.emit("user_message", "voice-turn", { message_id: "voice", queued: true });
      // Connection loss did not stop the daemon's old turn. Approval resolves.
      await h.clock.advance(100);
      h.emit("approval_resolved", "old", { req_id: "approval", decision: "allow" });
      h.emit("tool_finished", "old", { call_id: "approved-tool" });
      h.emit("text", "old", { text: "Old answer after reconnect." });
      await h.checkOmitted(id, "Old answer after reconnect.");
    } finally { h.controller.dispose(); }
  });

  it("120 seconds of silence must not make an old turn claim a newly queued send", async () => {
    const h = harness();
    try {
      h.emit("turn_started", "old");
      h.emit("tool_started", "old", { call_id: "approved-tool" });
      h.emit("approval_requested", "old", { req_id: "approval" });
      // The owner can leave an approval pending for longer than the timing TTL.
      await h.clock.advance(REPLY_TIMING_TTL_MS + 1);
      const id = h.send();
      h.emit("user_message", "voice-turn", { message_id: "voice", queued: true });
      await h.clock.advance(100);
      h.emit("approval_resolved", "old", { req_id: "approval", decision: "allow" });
      h.emit("tool_finished", "old", { call_id: "approved-tool" });
      h.emit("text", "old", { text: "Old answer after approval." });
      await h.checkOmitted(id, "Old answer after approval.");
    } finally { h.controller.dispose(); }
  });

  it("a requeued voice steer must not take an earlier undelivered steer's reply", async () => {
    const h = harness();
    try {
      h.emit("turn_started", "old");
      // First a manual steer, then the voice steer; both miss the final answer.
      h.emit("user_message", "old", { message_id: "manual", steer: true });
      await h.clock.advance(100);
      const id = h.send();
      h.emit("user_message", "old", { message_id: "voice", steer: true });
      h.emit("turn_finished", "old", { stop: "end", next: 2 });
      // daemon.py requeues reversed(leftover), each with front=True. This
      // publishes voice first, manual second, but runs manual first.
      h.emit("steer_queued", "voice-turn", { message_id: "voice", turn_id: "voice-turn" });
      h.emit("steer_queued", "manual-turn", { message_id: "manual", turn_id: "manual-turn" });
      h.emit("queued_started", "manual-turn", { message_id: "manual", turn_id: "manual-turn" });
      h.emit("turn_started", "manual-turn");
      await h.clock.advance(100);
      h.emit("text", "manual-turn", { text: "Answer to earlier manual steer." });
      await h.checkOmitted(id, "Answer to earlier manual steer.");
    } finally { h.controller.dispose(); }
  });
});
