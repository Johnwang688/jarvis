import { describe, expect, it } from "vitest";
import { Capture } from "./capture";
import { HANGOVER_MS } from "./vad";
import type { ClaimSource, DropReason } from "./voiceDiagnostics";
const quiet = 0.0009, speech = 0.06;
function harness() {
  let muted = false, suppressed = false;
  const drops: { reason: DropReason; ms: number; source: ClaimSource }[] = [];
  const sends: { ms: number; source?: ClaimSource }[] = [];
  const cap = new Capture({ suppressed: () => suppressed, suppressionReason: () => "card", muted: () => muted,
    onLevel: () => {}, onListening: () => {}, onIdle: () => {},
    onDrop: (reason, ms, source) => drops.push({ reason, ms, source }),
    onChunk: () => {}, onUtteranceEnd: (e) => { if (e.chunks && !e.truncated) sends.push({ ms: e.ms, source: e.source }); } }, () => 10000);
  cap.feedMs(2000, quiet);
  return { cap, drops, sends, mute: () => { muted = true; }, block: () => { suppressed = true; } };
}
describe("capture observations do not own claim or upload decisions", () => {
  it("never includes the muted pre-roll in a PTT upload immediately after unmute", () => {
    let muted = true; const durations: number[] = [];
    const cap = new Capture({ suppressed: () => false, muted: () => muted, onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onChunk: () => {}, onUtteranceEnd: (e) => { if (e.chunks) durations.push(e.ms); } });
    cap.feedMs(3000, speech); muted = false; cap.markUnmute(); cap.press(); cap.feedMs(1200, speech); cap.release();
    expect(durations).toEqual([1200]);
  });
  it("reports an unclaimed utterance exactly once, not per frame", () => {
    const h = harness(); h.cap.feedMs(1200, speech); h.cap.feedMs(HANGOVER_MS + 300, quiet);
    expect(h.drops.map(x => x.reason)).toEqual(["unclaimed"]); expect(h.sends).toHaveLength(0);
  });
  it("reports muted and card-suppressed speech once at the end with zero upload", () => {
    for (const reason of ["muted", "suppressed:card"]) {
      const h = harness(); reason === "muted" ? h.mute() : h.block();
      h.cap.feedMs(3500, speech); expect(h.drops).toHaveLength(0);
      h.cap.feedMs(HANGOVER_MS + 300, quiet);
      expect(h.drops.map(x => x.reason)).toEqual([reason]); expect(h.sends).toHaveLength(0);
    }
  });
  it("does not double-report a claimed utterance interrupted by a card", () => {
    const h = harness(); h.cap.openFollowUp(); h.cap.feedMs(800, speech); h.block();
    h.cap.feedMs(1500, speech); h.cap.feedMs(HANGOVER_MS + 300, quiet);
    expect(h.drops.map(x => x.reason)).toEqual(["suppressed:card"]); expect(h.sends).toHaveLength(0);
  });
  it("does not add an unclaimed row for PTT, and preserves its source", () => {
    const h = harness(); h.cap.press(); h.cap.feedMs(1200, speech); h.cap.feedMs(HANGOVER_MS + 300, quiet); h.cap.release();
    expect(h.drops).toHaveLength(0); expect(h.sends).toHaveLength(1); expect(h.sends[0].source).toBe("ptt");
  });
  it("records a short tap and an abandoned PTT once each", () => {
    const h = harness(); h.cap.press(); h.cap.feedMs(80, speech); h.cap.release();
    expect(h.drops.map(x => x.reason)).toEqual(["too_short"]);
    const j = harness(); j.cap.press(); j.cap.feedMs(1200, speech); j.cap.abandon(); j.cap.release();
    expect(j.drops.map(x => x.reason)).toEqual(["suppressed:crash"]); expect(j.sends).toHaveLength(0);
  });
  it("a throwing observation cannot prevent a later real claimed upload", () => {
    const sends: Blob[] = [];
    const cap = new Capture({ suppressed: () => false, muted: () => false, onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onDrop: () => { throw Error("observer"); }, onChunk: () => {}, onUtteranceEnd: (e) => { if (e.chunks) sends.push(new Blob()); } }, () => 10000);
    cap.feedMs(2000, quiet); cap.feedMs(1000, speech); cap.feedMs(HANGOVER_MS + 300, quiet);
    cap.openFollowUp(); cap.feedMs(1000, speech); cap.feedMs(HANGOVER_MS + 300, quiet);
    expect(sends).toHaveLength(1);
  });
});
