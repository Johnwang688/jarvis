import { describe, expect, it, vi } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SttAssembler } from "./stt";
import { SpeechController } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";

function harness() {
  const clock = new FakeClock();
  const diagnostics = new VoiceDiagnostics(() => clock.now());
  const link = new VoiceTimingsLink(diagnostics, () => clock.now());
  const row = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", clock.now());
    diagnostics.mark(id, "transcribed", clock.now());
    return id;
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(r => r.id === id)!.marks!;
  return { clock, diagnostics, link, row, marks };
}

describe("VA-0 verifier counterexamples: observation failure isolation", () => {
  it("still issues the send and preserves its synchronous error when a timing observation throws", () => {
    const h = harness(), id = h.row();
    const sendError = new Error("send failed");
    const send = vi.fn(() => { throw sendError; });
    vi.spyOn(h.diagnostics, "mark").mockImplementation(() => { throw new Error("timing observer failed"); });
    let caught: unknown;
    try { h.link.issue("A", [{ diag: id, meta: { mode: "auto" } }], send); }
    catch (error) { caught = error; }
    expect.soft(send, "an observation must not suppress api.send").toHaveBeenCalledOnce();
    expect(caught, "the original send error must reach the existing caller").toBe(sendError);
  });

  it("closes and places an STT group even if recording the close marks throws", async () => {
    const h = harness();
    const placed = vi.fn();
    let reply!: (text: string) => void;
    const stt = new SttAssembler({
      diagnostics: h.diagnostics, now: () => h.clock.now(), place: placed,
      transcribe: () => new Promise<string>(resolve => { reply = resolve; }),
    });
    const gid = stt.beginGroup("ptt", undefined);
    stt.submit(new Blob(["audio"]), 1000, "ptt", undefined, { group: gid });
    reply("hello"); await h.clock.flush();
    const mark = vi.spyOn(h.diagnostics, "mark").mockImplementation(() => { throw new Error("timing observer failed"); });
    let caught: unknown;
    try { stt.closeGroup(gid, { marks: { speechEnd: 0 } }); }
    catch (error) { caught = error; }
    await h.clock.flush(); await stt.settled();
    mark.mockRestore();
    expect.soft(caught, "capture's end hook must not lose its group closure").toBeUndefined();
    expect(placed, "an open group holds this message and every later message").toHaveBeenCalledOnce();
  });
});

describe("VA-0 verifier counterexamples: firstAudio belongs to the new reply", () => {
  it("does not consume a new receipt when the previous reply starts before send acknowledgement", async () => {
    const h = harness();
    const player = new FakePlayer(h.clock), say = new FakeSay(h.clock);
    const controller = new SpeechController({ clock: h.clock, player, gate: new FakeGate(), say: say.fn });
    controller.setSpeakers(new Set(["A", "B"]));
    controller.onAudioStart(chat => h.link.firstAudio(chat));
    controller.subscribe(() => h.link.retain(chat => controller.outputEnabled(chat)));
    try {
      // A is queued behind B. The owner's PTT interrupts B; ending the barge-in
      // skips B before App issues the A send, but A's old synthesis is still pending.
      controller.event({ kind: "text", thread_id: "B", data: { text: "Pane B is speaking a fairly long previous reply." } });
      await h.clock.advance(150);
      say.latency = 1000;
      controller.event({ kind: "text", thread_id: "A", data: { text: "Previous A reply still in the speech queue." } });
      expect(controller.bargeIn()).toBe(true);
      await h.clock.advance(400);
      controller.endBargeIn({ skip: true });
      const id = h.row();
      let acknowledge!: () => void;
      const pendingSend = h.link.issue("A", [{ diag: id, meta: { mode: "auto" } }],
        () => new Promise<void>(resolve => { acknowledge = resolve; }));
      await h.clock.advance(1500);
      const beforeReply = { ...h.marks(id) };
      // Preserve App's existing ordering: stop stale speech after the response.
      acknowledge(); await pendingSend;
      controller.messageSent("A");
      say.latency = 100;
      const replyAt = h.clock.now();
      h.link.event({ kind: "text", thread_id: "A", data: { text: "New reply." } });
      controller.event({ kind: "text", thread_id: "A", data: { text: "New reply." } });
      await h.clock.advance(200);
      expect.soft(beforeReply.firstToken).toBeUndefined();
      expect.soft(beforeReply.firstAudio, "old speech must not consume the new chat receipt").toBeUndefined();
      expect(h.marks(id), "the receipt must survive until the new reply is audible").toMatchObject({ firstToken: replyAt, firstAudio: replyAt + 100 });
    } finally { controller.dispose(); }
  });
});
