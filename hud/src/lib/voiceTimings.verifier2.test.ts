import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SpeechController, type SpeechEventLike } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

function harness() {
  const clock = new FakeClock(), player = new FakePlayer(clock), say = new FakeSay(clock);
  const controller = new SpeechController({ clock, player, say: say.fn, gate: new FakeGate(), timingNow: () => clock.now() });
  controller.setSpeakers(new Set(["A", "B"]));
  const diagnostics = new VoiceDiagnostics(() => clock.now());
  const link = new VoiceTimingsLink(diagnostics, () => clock.now(), chat => controller.audioTextStartedAt(chat));
  controller.onAudioStart(chat => link.firstAudio(chat));
  controller.subscribe(() => link.retain(chat => controller.outputEnabled(chat)));
  const row = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", clock.now());
    diagnostics.mark(id, "transcribed", clock.now());
    return id;
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(r => r.id === id)!.marks!;
  const event = (kind: string, text = "", data: Record<string, unknown> = {}, thread = "A") => {
    const e: SpeechEventLike = { kind, thread_id: thread, data: { text, ...data } };
    link.event(e); controller.event(e);
  };
  return { clock, player, controller, link, row, marks, event };
}

describe("VA-0 re-check: reply provenance", () => {
  it.each(["queued user_message", "refused steer", "nonfatal error"])("does not end an existing turn at %s", async boundary => {
    const h = harness();
    try {
      h.event("turn_started"); h.event("text", "Old turn's first message."); h.event("tool_started");
      await h.clock.advance(100);
      const id = h.row(); h.link.sent("A", [id]);
      if (boundary === "queued user_message") h.event("user_message", "", { queued: true, message_id: "waiting" });
      if (boundary === "refused steer") {
        h.event("user_message", "", { steer: true, message_id: "waiting" });
        h.event("steer_queued", "", { message_id: "waiting", turn_id: "next" });
      }
      // The send request may still be in flight when this older turn reports an error.
      if (boundary === "nonfatal error") h.event("error", "", { fatal: false, message: "request declined; turn continues" });
      await h.clock.advance(100);
      h.event("text", "Old turn continues after its tool.");
      const beforeNextTurn = { ...h.marks(id) };
      h.event("turn_finished");
      await h.clock.advance(100);
      h.event("turn_started"); h.event("text", "The new send's actual reply.");
      expect.soft(beforeNextTurn.firstToken, "an unfinished older turn cannot claim a queued/new send").toBeUndefined();
      expect(h.marks(id).firstToken, "the new turn's text must own the receipt").toBe(300);
    } finally { h.controller.dispose(); }
  });

  it("a native steer cannot turn the tail of an already streaming message into its first token", async () => {
    const h = harness();
    try {
      h.event("turn_started"); h.event("text_delta", "An old sentence");
      await h.clock.advance(100);
      const id = h.row(); h.link.sent("A", [id]);
      h.event("user_message", "", { steer: true });
      await h.clock.advance(100); h.event("text_delta", " still arriving.");
      h.event("text", "An old sentence still arriving.");
      expect(h.player.started).toEqual([]); // Existing controller correctly silences that stale message.
      expect(h.marks(id).firstToken).toBeUndefined();
    } finally { h.controller.dispose(); }
  });

  it("does not treat an older turn as new when sent shares its clock tick", () => {
    const h = harness();
    try {
      h.event("turn_started");
      const id = h.row(); h.link.sent("A", [id]);
      // performance.now() is monotonic, but successive observations may be equal.
      h.event("text", "This reply still belongs to the turn that started before send.");
      expect(h.marks(id).firstToken).toBeUndefined();
    } finally { h.controller.dispose(); }
  });
});
