import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink, REPLY_TIMING_TTL_MS } from "./voiceTimingsLink";

function harness() {
  let t = 100;
  const diagnostics = new VoiceDiagnostics(() => t);
  const link = new VoiceTimingsLink(diagnostics, () => t);
  const row = () => {
    const id = diagnostics.add(1000, "ptt", "transcribing");
    diagnostics.mark(id, "speechEnd", 0);
    diagnostics.mark(id, "transcribed", 50);
    diagnostics.finish(id, "handed_to_chat", 50);
    return id;
  };
  const marks = (id: number) => diagnostics.snapshot().rows.find(row => row.id === id)!.marks!;
  const event = (chat: string, text = "Hello") => link.event({ kind: "text_delta", thread_id: chat, data: { text } });
  return { diagnostics, link, row, marks, event, at: (at: number) => { t = at; } };
}

describe("voice reply timing links", () => {
  it("marks sent at request issuance, before an asynchronous send acknowledgement", async () => {
    const h = harness(), id = h.row();
    let ack!: () => void;
    const request = new Promise<void>(resolve => { ack = resolve; });
    h.at(300);
    const result = h.link.issue("A", [{ diag: id, meta: { mode: "auto" } }], chat => {
      expect(chat).toBe("A");
      expect(h.marks(id).sent).toBe(300);
      return request;
    });
    expect(result).toBe(request);
    h.at(500); h.event("A");
    expect(h.marks(id).firstToken).toBe(500);
    ack(); await result;
  });

  it("ignores another chat's reply and audio while independently tracking both panes", () => {
    const h = harness(), a = h.row(), b = h.row();
    h.link.sent("A", [a]); h.link.sent("B", [b]);
    h.at(200); h.event("B"); h.link.firstAudio("C");
    expect(h.marks(a).firstToken).toBeUndefined();
    expect(h.marks(b).firstToken).toBe(200);
    h.at(300); h.event("A"); h.link.firstAudio("A");
    expect(h.marks(a)).toMatchObject({ firstToken: 300, firstAudio: 300 });
    expect(h.marks(b).firstAudio).toBeUndefined();
  });

  it("a newer send replaces the pending receipt in that chat", () => {
    const h = harness(), a = h.row(), b = h.row();
    h.link.sent("A", [a]); h.at(200); h.link.sent("A", [b]);
    h.at(300); h.event("A"); h.link.firstAudio("A");
    expect(h.marks(a)).toEqual({ speechEnd: 0, transcribed: 50, sent: 100 });
    expect(h.marks(b)).toMatchObject({ sent: 200, firstToken: 300, firstAudio: 300 });
  });

  it("expires at 120 seconds, including when no event arrived in between", () => {
    for (const stage of ["token", "audio"]) {
      const h = harness(), id = h.row();
      h.link.sent("A", [id]); h.at(100 + REPLY_TIMING_TTL_MS);
      if (stage === "token") h.event("A"); else h.link.firstAudio("A");
      expect(h.marks(id)).toEqual({ speechEnd: 0, transcribed: 50, sent: 100 });
    }
  });

  it("keeps the first text event and clears at first audio", () => {
    const h = harness(), id = h.row();
    h.link.sent("A", [id]); h.at(200); h.event("A");
    h.at(300); h.event("A"); h.link.firstAudio("A");
    h.at(400); h.link.firstAudio("A");
    expect(h.marks(id)).toMatchObject({ firstToken: 200, firstAudio: 300 });
  });

  it("REVIEW and a later manual Send mark nothing after transcribed", () => {
    const h = harness(), id = h.row();
    h.link.issue("A", [{ diag: id, meta: { mode: "review" } }], () => {});
    h.at(50_000); h.link.issue("A", [], () => {});
    h.event("A"); h.link.firstAudio("A");
    expect(h.marks(id)).toEqual({ speechEnd: 0, transcribed: 50 });
  });

  it("output off or per-chat mute clears pending links and re-enabling cannot revive them", () => {
    const h = harness(), a = h.row(), b = h.row();
    h.link.sent("A", [a]); h.link.sent("B", [b]);
    h.link.retain(chat => chat !== "A"); h.link.retain(() => true);
    h.at(200); h.event("A"); h.link.firstAudio("A"); h.event("B");
    expect(h.marks(a).firstAudio).toBeUndefined();
    expect(h.marks(a).firstToken).toBeUndefined();
    expect(h.marks(b).firstToken).toBe(200);
    h.link.retain(() => false); h.link.firstAudio("B");
    expect(h.marks(b).firstAudio).toBeUndefined();
  });

  it("sending with output already off leaves firstAudio absent", () => {
    const h = harness(), id = h.row();
    h.link.issue("A", [{ diag: id, meta: { mode: "auto" } }], () => {}, false);
    h.at(200); h.link.firstAudio("A");
    expect(h.marks(id).sent).toBe(100);
    expect(h.marks(id).firstAudio).toBeUndefined();
  });

  it("ignores empty, subagent, relayed, compaction and self-started text; accepts final text without deltas", () => {
    const h = harness(), id = h.row(); h.link.sent("A", [id]);
    h.event("A", "");
    for (const extras of [{ kind: "subagent_event" }, { self_started: true }, { relayed: {} }, { compaction: true }])
      h.link.event({ kind: "text_delta", thread_id: "A", data: { text: "hello" }, ...extras });
    expect(h.marks(id).firstToken).toBeUndefined();
    h.at(200); h.link.event({ kind: "text", thread_id: "A", data: { text: "hello" } });
    expect(h.marks(id).firstToken).toBe(200);
  });

  it("marks all utterances combined in a single send", () => {
    const h = harness(), a = h.row(), b = h.row();
    h.link.sent("A", [a, b]); h.at(200); h.event("A"); h.link.firstAudio("A");
    expect(h.marks(a)).toEqual(h.marks(b));
  });
});
