import { afterEach, describe, expect, it, vi } from "vitest";
import appSource from "../App.tsx?raw";
import { Capture, type UtteranceEnd } from "./capture";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { SttAssembler, type SttPlaced } from "./stt";
import { VoiceTimingsLink } from "./voiceTimingsLink";
import { SpeechController } from "./speech/controller";
import { NOTICE_WINDOW_MS } from "./speech/notices";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./speech/testkit";
import { END_PAUSE_MS, samplesToMs } from "./vad";

afterEach(() => vi.useRealTimers());
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function captureHarness() {
  const ends: UtteranceEnd[] = [];
  let now = 10_000;
  let quietAtClose = 0;
  const capture = new Capture({
    suppressed: () => false, muted: () => false,
    onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onChunk: () => {},
    onUtteranceEnd: end => { quietAtClose = capture.vad.quiet; ends.push(end); },
  }, () => now);
  return { capture, ends, quiet: () => quietAtClose, at: (at: number) => { now = at; } };
}

describe("capture timing hooks", () => {
  it("timestamps VAD close and subtracts the whole quiet hangover (not the recording's 200 ms padding)", () => {
    const h = captureHarness();
    h.capture.feedMs(2000, 0.0009);
    h.capture.openFollowUp(); h.capture.feedMs(1200, 0.06);
    h.at(20_000); h.capture.feedMs(END_PAUSE_MS + 300, 0.0009);
    expect(h.ends).toHaveLength(1);
    expect(h.ends[0].marks).toEqual({ speechEnd: 20_000 - samplesToMs(h.quiet()), endpoint: 20_000 });
    expect(h.quiet()).toBeGreaterThanOrEqual(END_PAUSE_MS * 16);
    h.capture.closeFollowUp();
  });

  it("timestamps PTT release even after quiet, without an endpoint mark", () => {
    const h = captureHarness();
    h.capture.press(); h.capture.feedMs(1200, 0.06); h.capture.feedMs(1000, 0.0009);
    expect(h.ends).toHaveLength(0);
    h.at(12_345); h.capture.release();
    expect(h.ends[0].marks).toEqual({ speechEnd: 12_345 });
    h.capture.closeFollowUp();
  });

  it("does not invent speech end when a recording is interrupted", () => {
    const h = captureHarness(); h.capture.press(); h.capture.feedMs(1200, 0.06); h.capture.abandon();
    expect(h.ends[0].marks).toBeUndefined();
  });
});

function sttHarness() {
  let now = 100;
  const replies: ((text: string) => void)[] = [];
  const diagnostics = new VoiceDiagnostics(() => now);
  const placed: SttPlaced<string>[] = [];
  const stt = new SttAssembler<string>({
    diagnostics, now: () => now,
    transcribe: () => new Promise<string>(resolve => replies.push(resolve)),
    place: batch => { placed.push(...batch); },
  });
  const group = () => stt.beginGroup("wake", "auto");
  const submit = (group: number) => stt.submit(new Blob(["audio"]), 1000, "wake", "auto", { group });
  const marks = () => diagnostics.snapshot().rows.at(-1)?.marks;
  return { stt, diagnostics, replies, placed, group, submit, marks, at: (at: number) => { now = at; } };
}

describe("STT timing hooks", () => {
  it("marks transcribed at the final whole-message result, never an intermediate chunk", async () => {
    const h = sttHarness(), gid = h.group();
    h.submit(gid); h.submit(gid);
    h.stt.closeGroup(gid, { marks: { speechEnd: 1000, endpoint: 3500 } });
    h.at(3600); h.replies[0]("first"); await flush();
    expect(h.marks()).toEqual({ speechEnd: 1000, endpoint: 3500 });
    h.at(4000); h.replies[1]("last"); await flush();
    expect(h.marks()).toEqual({ speechEnd: 1000, endpoint: 3500, transcribed: 4000 });
    expect(h.placed.map(row => row.text)).toEqual(["first last"]);
  });

  it("waits for all outstanding chunks even when the last chunk answers out of order", async () => {
    const h = sttHarness(), gid = h.group(); h.submit(gid); h.submit(gid);
    h.stt.closeGroup(gid, { marks: { speechEnd: 100, endpoint: 200 } });
    h.at(300); h.replies[1]("last"); await flush();
    expect(h.marks()?.transcribed).toBeUndefined();
    h.at(500); h.replies[0]("first"); await flush();
    expect(h.marks()?.transcribed).toBe(500);
  });

  it("omits an early first-cut result that predates endpoint instead of fabricating a zero STT duration", async () => {
    const h = sttHarness(), gid = h.group(); h.submit(gid);
    h.at(1500); h.replies[0]("whole message"); await flush();
    expect(h.marks()).toBeUndefined(); expect(h.placed).toHaveLength(0);
    h.at(3500); h.stt.closeGroup(gid, { marks: { speechEnd: 1000, endpoint: 3500 } }); await flush();
    expect(h.marks()).toEqual({ speechEnd: 1000, endpoint: 3500 });
  });

  it("does not charge another message's outstanding STT to this message's transcription stage", async () => {
    const h = sttHarness(), first = h.group(); h.submit(first);
    h.stt.closeGroup(first, { marks: { speechEnd: 0 } });
    const second = h.group(); h.submit(second);
    h.stt.closeGroup(second, { marks: { speechEnd: 100 } });
    h.at(300); h.replies[1]("second"); await flush();
    expect(h.marks()?.transcribed).toBe(300); expect(h.placed).toHaveLength(0);
    h.at(500); h.replies[0]("first"); await flush();
    expect(h.marks()?.transcribed).toBe(300); expect(h.placed).toHaveLength(2);
  });

  it("throwing diagnostics subscribers cannot prevent capture-to-STT placement", async () => {
    const h = sttHarness(); h.diagnostics.subscribe(() => { throw Error("observer"); });
    const gid = h.group(); h.submit(gid);
    h.stt.closeGroup(gid, { marks: { speechEnd: 0 } });
    h.at(400); h.replies[0]("hello"); await flush();
    expect(h.marks()?.transcribed).toBe(400); expect(h.placed).toHaveLength(1);
  });
});

function speechHarness() {
  const clock = new FakeClock(), player = new FakePlayer(clock), gate = new FakeGate(), say = new FakeSay(clock);
  let blockedChanged: (() => void) | undefined;
  Object.assign(player, { subscribeBlocked: (cb: () => void) => { blockedChanged = cb; return () => { blockedChanged = undefined; }; } });
  const controller = new SpeechController({ clock, player, gate, say: say.fn });
  controller.setSpeakers(new Set(["A", "B"]));
  const diagnostics = new VoiceDiagnostics(() => clock.now());
  const link = new VoiceTimingsLink(diagnostics, () => clock.now());
  const id = diagnostics.add(1000, "ptt", "transcribing");
  diagnostics.mark(id, "speechEnd", 0); diagnostics.mark(id, "transcribed", 0);
  link.sent("A", [id]);
  controller.onAudioStart(chat => link.firstAudio(chat));
  controller.subscribe(() => link.retain(chat => controller.outputEnabled(chat)));
  const event = (kind: string, text = "", chat = "A") => {
    const e = { kind, thread_id: chat, data: { text } }; link.event(e); controller.event(e);
  };
  return { clock, player, controller, diagnostics, link, event, unblock: () => { player.isBlocked = false; blockedChanged?.(); }, marks: () => diagnostics.snapshot().rows[0].marks! };
}

describe("reply event and speech timing hooks", () => {
  it("marks firstToken on the first matching text and firstAudio at playback, not synthesis", async () => {
    const h = speechHarness();
    await h.clock.advance(25); h.event("text_delta", "Hello there.", "B");
    expect(h.marks().firstToken).toBeUndefined();
    await h.clock.advance(25); h.event("text_delta", "Hello there.");
    expect(h.marks().firstToken).toBe(50);
    h.event("turn_finished");
    await h.clock.advance(99); expect(h.marks().firstAudio).toBeUndefined();
    await h.clock.advance(1); expect(h.player.started).toEqual(["Hello there."]);
    expect(h.marks().firstAudio).toBe(150);
    h.controller.dispose();
  });

  it("waits through an autoplay block until the playing source can be heard", async () => {
    const h = speechHarness(); h.player.isBlocked = true;
    h.event("text", "Hello there."); await h.clock.advance(150);
    expect(h.player.started).toEqual(["Hello there."]);
    expect(h.marks().firstAudio).toBeUndefined();
    await h.clock.advance(100); h.unblock();
    expect(h.marks().firstAudio).toBe(250);
    h.controller.dispose();
  });

  it("an unrelated queue notification cannot attach an already playing chunk to a newer send", async () => {
    const h = speechHarness(); h.event("text", "A fairly long reply so it is still playing.");
    await h.clock.advance(150);
    const next = h.diagnostics.add(1000, "ptt", "transcribing");
    h.diagnostics.mark(next, "speechEnd", 150); h.diagnostics.mark(next, "transcribed", 150);
    h.link.sent("A", [next]);
    h.controller.clearMarks(); h.unblock();
    expect(h.diagnostics.snapshot().rows.at(-1)!.marks!.firstAudio).toBeUndefined();
    h.controller.dispose();
  });

  it("does not mistake a subagent notice for the reply's first audio", async () => {
    const h = speechHarness();
    h.controller.setNotices(true);
    h.controller.event({ kind: "subagent", thread_id: "A", data: {
      sid: "cl:a", op: "finished", status: "done", depth: 1, background: false,
      started_at: "2026-10-16T10:00:00Z", entries: 2,
    } });
    await h.clock.advance(NOTICE_WINDOW_MS + 5000);
    expect(h.player.started).toEqual(["A sub-agent finished."]);
    expect(h.marks().firstAudio).toBeUndefined();
    h.controller.dispose();
  });

  it("output off or mute drops the pending audio receipt through the real controller", async () => {
    for (const mute of [false, true]) {
      const h = speechHarness(); h.event("text_delta", "Hello there.");
      if (mute) h.controller.setMuted(new Set(["A"])); else h.controller.setEnabled(false);
      h.controller.setEnabled(true); h.controller.setMuted(new Set());
      h.event("text", "A later reply.");
      await h.clock.advance(1000);
      expect(h.marks().firstAudio).toBeUndefined();
      h.controller.dispose();
    }
  });

  it("throwing audio observers cannot interrupt playback", async () => {
    const h = speechHarness(); h.controller.onAudioStart(() => { throw Error("observer"); });
    h.event("text", "Hello there."); await h.clock.advance(1000);
    expect(h.player.started).toEqual(["Hello there."]);
    expect(h.controller.items().past[0].state).toBe("done");
    h.controller.dispose();
  });

  it("App wires capture, AUTO sends, events and playback to the same receipt", () => {
    expect(appSource).toContain("marks: end.marks");
    expect(appSource).toContain('await send(n, items.map((i) => i.text).join(" "), [], true, items)');
    expect(appSource).toContain("voiceTimings.issue(threadId, timingRows, chat => api.send(chat,");
    expect(appSource).toContain("voiceTimings.event(e)");
    expect(appSource).toContain("speech.controller.onAudioStart(chat => voiceTimings.firstAudio(chat))");
    expect(appSource).toContain("await send(n, body, files, true, boxed ? [] : step.items)");
  });
});
