import { afterEach, describe, expect, it, vi } from "vitest";
import { Capture, type UtteranceEnd } from "./capture";
import { SttAssembler } from "./stt";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SpeechController } from "./speech/controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";
import { END_PAUSE_MS } from "./vad";

afterEach(() => vi.restoreAllMocks());
const fail = () => { throw new Error("timing observation failed"); };

function timingHarness() {
  const clock = new FakeClock();
  const diagnostics = new VoiceDiagnostics(() => clock.now());
  const link = new VoiceTimingsLink(diagnostics, () => clock.now());
  const id = diagnostics.add(1000, "ptt", "transcribing");
  diagnostics.mark(id, "speechEnd", 0);
  diagnostics.mark(id, "transcribed", 0);
  const rows = [{ diag: id, meta: { mode: "auto" } }];
  return { clock, diagnostics, link, id, rows, marks: () => diagnostics.snapshot().rows[0].marks! };
}

function speechHarness(timingNow?: () => number) {
  const h = timingHarness();
  const player = new FakePlayer(h.clock), say = new FakeSay(h.clock);
  let blockedChanged!: () => void;
  Object.assign(player, { subscribeBlocked: (cb: () => void) => { blockedChanged = cb; return () => {}; } });
  const controller = new SpeechController({ clock: h.clock, player, gate: new FakeGate(), say: say.fn, timingNow });
  controller.setSpeakers(new Set(["A", "B"]));
  const link = new VoiceTimingsLink(h.diagnostics, () => h.clock.now(), chat => controller.audioTextStartedAt(chat));
  controller.onAudioStart(chat => link.firstAudio(chat));
  const event = (text: string, kind = "text") => {
    const e = { kind, thread_id: "A", data: { text } };
    link.event(e); controller.event(e);
  };
  return { ...h, link, player, say, controller, event, blockedChanged: () => blockedChanged() };
}

describe("timing observations cannot control capture", () => {
  it.each(["PTT", "VAD"])("%s closes and emits its audio even when its receipt clock throws", mode => {
    const ends: UtteranceEnd[] = [], chunks = vi.fn();
    const now = vi.fn(() => 10_000);
    const capture = new Capture({
      suppressed: () => false, muted: () => false,
      onLevel: () => {}, onListening: () => {}, onIdle: () => {},
      onChunk: chunks, onUtteranceEnd: end => ends.push(end),
    }, now);
    if (mode === "PTT") capture.press();
    else { capture.feedMs(2000, 0.0009); capture.openFollowUp(); }
    capture.feedMs(1200, 0.06);
    now.mockImplementationOnce(fail);
    expect(() => mode === "PTT" ? capture.release() : capture.feedMs(END_PAUSE_MS + 300, 0.0009)).not.toThrow();
    expect(chunks).toHaveBeenCalledOnce();
    expect(ends).toHaveLength(1);
    expect(ends[0]).toMatchObject({ truncated: false, chunks: 1 });
    expect(ends[0].marks).toBeUndefined();
    capture.closeFollowUp();
  });
});

describe("timing observations cannot hold STT placement", () => {
  it.each(["speechEnd", "endpoint", "transcribed"] as const)("a throwing %s close mark still closes this group and releases the next", async stage => {
    const h = timingHarness(), placed = vi.fn();
    const replies: ((text: string) => void)[] = [];
    const stt = new SttAssembler({ diagnostics: h.diagnostics, now: () => h.clock.now(), place: placed,
      transcribe: () => new Promise<string>(resolve => replies.push(resolve)) });
    const first = stt.beginGroup("ptt", undefined);
    stt.submit(new Blob(["one"]), 1000, "ptt", undefined, { group: first });
    replies[0]("first"); await h.clock.flush();
    const mark = h.diagnostics.mark.bind(h.diagnostics);
    vi.spyOn(h.diagnostics, "mark").mockImplementation((id, name, at) => name === stage ? fail() : mark(id, name, at));
    expect(() => stt.closeGroup(first, { marks: { speechEnd: 0, endpoint: 0 } })).not.toThrow();
    const second = stt.beginGroup("ptt", undefined);
    stt.submit(new Blob(["two"]), 1000, "ptt", undefined, { group: second });
    stt.closeGroup(second);
    replies[1]("second"); await h.clock.flush(); await stt.settled();
    expect(placed.mock.calls.flatMap(([batch]) => batch.map((row: { text: string }) => row.text))).toEqual(["first", "second"]);
  });

  it("a throwing transcribed mark on the async result still places exactly once", async () => {
    const h = timingHarness(), placed = vi.fn();
    let reply!: (text: string) => void;
    const stt = new SttAssembler({ diagnostics: h.diagnostics, now: () => h.clock.now(), place: placed,
      transcribe: () => new Promise<string>(resolve => { reply = resolve; }) });
    const gid = stt.beginGroup("ptt", undefined);
    stt.submit(new Blob(["one"]), 1000, "ptt", undefined, { group: gid });
    stt.closeGroup(gid, { marks: { speechEnd: 0 } });
    vi.spyOn(h.diagnostics, "mark").mockImplementation(fail);
    reply("hello"); await h.clock.flush(); await stt.settled();
    expect(placed).toHaveBeenCalledOnce();
    expect(placed.mock.calls[0][0][0].text).toBe("hello");
  });

  it("a throwing transcription receipt clock still places the result", async () => {
    const h = timingHarness(), placed = vi.fn(), now = vi.fn(() => 100);
    let reply!: (text: string) => void;
    const stt = new SttAssembler({ diagnostics: h.diagnostics, now, place: placed,
      transcribe: () => new Promise<string>(resolve => { reply = resolve; }) });
    const gid = stt.beginGroup("ptt", undefined);
    stt.submit(new Blob(["one"]), 1000, "ptt", undefined, { group: gid }); stt.closeGroup(gid);
    now.mockImplementationOnce(fail);
    reply("hello"); await h.clock.flush(); await stt.settled();
    expect(placed).toHaveBeenCalledOnce();
  });
});

describe("timing link failure boundaries", () => {
  it.each(["mark", "sent", "clock"])("a throwing %s observation preserves the send result and calls it once", fault => {
    const h = timingHarness();
    const result = { status: "started" }, send = vi.fn(() => result);
    if (fault === "mark") vi.spyOn(h.diagnostics, "mark").mockImplementation(fail);
    if (fault === "sent") vi.spyOn(h.link, "sent").mockImplementation(fail);
    if (fault === "clock") vi.spyOn(h.clock, "now").mockImplementation(fail);
    expect(h.link.issue("A", h.rows, send)).toBe(result);
    expect(send).toHaveBeenCalledExactlyOnceWith("A");
  });

  it("keeps the send's rejected promise and its rejection identity", async () => {
    const h = timingHarness(), error = new Error("api failure");
    vi.spyOn(h.diagnostics, "mark").mockImplementation(fail);
    const result = Promise.reject(error), send = vi.fn(() => result);
    const issued = h.link.issue("A", h.rows, send);
    expect(issued).toBe(result); await expect(issued).rejects.toBe(error);
    expect(send).toHaveBeenCalledOnce();
  });

  it.each(["sent", "event", "firstAudio", "retain"] as const)("%s swallows a throwing observation", hook => {
    const h = timingHarness();
    h.link.sent("A", [h.id]); h.link.event({ kind: "text", thread_id: "A", data: { text: "reply" } });
    vi.spyOn(h.diagnostics, "mark").mockImplementation(fail);
    const calls = {
      sent: () => h.link.sent("A", [h.id]),
      event: () => h.link.event({ kind: "text", thread_id: "A", data: { text: "reply" } }),
      firstAudio: () => h.link.firstAudio("A"),
      retain: () => h.link.retain(fail),
    };
    // Start a fresh receipt so event() really attempts a firstToken mark.
    if (hook === "event") h.link.sent("A", [h.id]);
    expect(calls[hook]).not.toThrow();
  });
});

describe("controller observations cannot change playback", () => {
  it.each(["text", "text_delta"])("a throwing first-text clock does not suppress %s speech", async kind => {
    const h = speechHarness(fail);
    try {
      h.event("Hello there.", kind);
      if (kind === "text_delta") h.event("Hello there.");
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["Hello there."]);
      expect(h.controller.items().past[0].state).toBe("done");
    } finally { h.controller.dispose(); }
  });

  it("a throwing audio observation cannot break queue settling or unblock notifications", async () => {
    const h = speechHarness();
    try {
      const observer = h.controller as unknown as { observeAudio(): void };
      vi.spyOn(observer, "observeAudio").mockImplementation(fail);
      const changed = vi.fn(); h.controller.subscribe(changed);
      h.event("Hello there."); await h.clock.advance(150);
      expect(h.player.started).toEqual(["Hello there."]);
      changed.mockClear(); expect(h.blockedChanged).not.toThrow();
      expect(changed).toHaveBeenCalledOnce();
      await h.clock.advance(1000);
      expect(h.controller.items().past[0].state).toBe("done");
    } finally { h.controller.dispose(); }
  });

  it("a throwing enqueue timestamp observation still queues and plays the same text", async () => {
    const h = speechHarness();
    try {
      const observer = h.controller as unknown as { audioTextTimes: Map<string, number> };
      vi.spyOn(observer.audioTextTimes, "set").mockImplementation(fail);
      h.event("Hello there."); await h.clock.advance(1000);
      expect(h.player.started).toEqual(["Hello there."]);
      expect(h.controller.items().past[0].state).toBe("done");
    } finally { h.controller.dispose(); }
  });

  it("throwing audio listeners and source lookups leave playback and other listeners intact", async () => {
    const h = speechHarness();
    try {
      h.link.sent("A", [h.id]);
      vi.spyOn(h.controller, "audioTextStartedAt").mockImplementation(fail);
      h.controller.onAudioStart(fail);
      const heard = vi.fn(); h.controller.onAudioStart(heard);
      h.event("Hello there."); await h.clock.advance(1000);
      expect(heard).toHaveBeenCalledExactlyOnceWith("A");
      expect(h.marks().firstAudio).toBeUndefined();
      expect(h.player.started).toEqual(["Hello there."]);
    } finally { h.controller.dispose(); }
  });
});

describe("reply attribution across sends", () => {
  it.each([false, true])("ignores text continuing a reply begun before sent (explicit turn: %s)", async turn => {
    const h = timingHarness();
    const event = (kind: string, text = "") => h.link.event({ kind, thread_id: "A", data: { text } });
    if (turn) event("turn_started");
    event("text_delta", "Old reply");
    await h.clock.advance(50); h.link.sent("A", [h.id]);
    event("text_delta", " continues."); event("text", "Old reply continues.");
    if (turn) { event("tool_started"); event("text", "Another old-turn message."); }
    h.link.firstAudio("A");
    expect(h.marks().firstToken).toBeUndefined(); expect(h.marks().firstAudio).toBeUndefined();
    if (turn) { event("turn_finished"); event("turn_started"); }
    await h.clock.advance(50); event("text", "New reply."); h.link.firstAudio("A");
    expect(h.marks()).toMatchObject({ firstToken: 100, firstAudio: 100 });
  });

  it("does not credit old queued text even after the new reply's token arrived", async () => {
    const h = speechHarness();
    try {
      h.say.latency = 1000; h.event("Old reply.");
      await h.clock.advance(100); h.link.sent("A", [h.id]);
      h.event("New reply.");
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["Old reply."]);
      expect(h.marks()).toMatchObject({ sent: 100, firstToken: 100 });
      expect(h.marks().firstAudio).toBeUndefined();
      await h.clock.advance(1000);
      expect(h.player.started).toEqual(["Old reply.", "New reply."]);
      expect(h.marks().firstAudio).toBe(1500);
    } finally { h.controller.dispose(); }
  });

  it("requires the row's firstToken mark even if marking it failed", () => {
    const h = timingHarness(); h.link.sent("A", [h.id]);
    const mark = vi.spyOn(h.diagnostics, "mark").mockImplementation(fail);
    h.link.event({ kind: "text", thread_id: "A", data: { text: "reply" } });
    mark.mockRestore(); h.link.firstAudio("A");
    expect(h.marks().firstAudio).toBeUndefined();
  });
});
