// VA-0 acceptance tests (orchestrator-owned; the implementer may not edit this file).
// Plan: docs/plans/2026-11-15-voice-anywhere-plan.md, package VA-0.
import { describe, expect, it } from "vitest";
import { STAGES, stageDurations, summarize, type Marks } from "./voiceTimings";
import { VoiceDiagnostics } from "./voiceDiagnostics";

describe("STAGES", () => {
  it("is the five stages in pipeline order", () => {
    expect([...STAGES]).toEqual(["endpoint", "transcribed", "sent", "firstToken", "firstAudio"]);
  });
});

describe("stageDurations", () => {
  it("measures each stage from the previous present mark, and total from speechEnd", () => {
    const m: Marks = { speechEnd: 1000, endpoint: 1700, transcribed: 2100, sent: 2150, firstToken: 3150, firstAudio: 3600 };
    expect(stageDurations(m)).toEqual({ endpoint: 700, transcribed: 400, sent: 50, firstToken: 1000, firstAudio: 450, total: 2600 });
  });
  it("skips a missing stage (push-to-talk has no endpoint wait) and measures the next from the one before", () => {
    expect(stageDurations({ speechEnd: 0, transcribed: 300, sent: 320 })).toEqual({ transcribed: 300, sent: 20, total: 320 });
  });
  it("is empty without speechEnd", () => {
    expect(stageDurations({ endpoint: 10, transcribed: 20 })).toEqual({});
  });
  it("rounds to whole milliseconds", () => {
    expect(stageDurations({ speechEnd: 0.4, endpoint: 700.9 })).toEqual({ endpoint: 701, total: 701 });
  });
  it("never reports a negative duration", () => {
    const d = stageDurations({ speechEnd: 1000, endpoint: 900, transcribed: 1200 });
    for (const v of Object.values(d)) expect(v).toBeGreaterThanOrEqual(0);
    expect(d.endpoint).toBeUndefined();
  });
});

describe("summarize", () => {
  it("gives n, p50 and p90 (nearest rank) per stage and for total", () => {
    const rows: Marks[] = [];
    for (let i = 1; i <= 10; i++) rows.push({ speechEnd: 0, endpoint: i * 100, transcribed: i * 100 + 50 });
    const s = summarize(rows);
    expect(s.endpoint).toEqual({ n: 10, p50: 500, p90: 900 });
    expect(s.transcribed).toEqual({ n: 10, p50: 50, p90: 50 });
    expect(s.total).toEqual({ n: 10, p50: 550, p90: 950 });
    expect(s.firstAudio).toBeUndefined();
  });
  it("counts only rows that have the stage", () => {
    const s = summarize([{ speechEnd: 0, sent: 10 }, { speechEnd: 0 }, {}, { speechEnd: 0, sent: 30, firstToken: 530 }]);
    expect(s.sent).toEqual({ n: 2, p50: 10, p90: 20 });
    expect(s.firstToken).toEqual({ n: 1, p50: 500, p90: 500 });
  });
  it("is empty for no rows", () => {
    expect(summarize([])).toEqual({});
  });
});

describe("VoiceDiagnostics.mark", () => {
  function diag() {
    let t = 0;
    const d = new VoiceDiagnostics(() => t);
    const id = d.add(1200, "wake", "transcribing", "silero");
    const row = () => d.snapshot().rows.find(r => r.id === id)!;
    return { d, id, row, at: (v: number) => { t = v; } };
  }
  it("records marks on the row, defaulting to the clock", () => {
    const { d, id, row, at } = diag();
    d.mark(id, "speechEnd", 100);
    at(800); d.mark(id, "endpoint");
    expect(row().marks).toEqual({ speechEnd: 100, endpoint: 800 });
  });
  it("keeps the first mark of a stage", () => {
    const { d, id, row } = diag();
    d.mark(id, "speechEnd", 100); d.mark(id, "speechEnd", 150);
    d.mark(id, "sent", 400); d.mark(id, "sent", 900);
    expect(row().marks).toEqual({ speechEnd: 100, sent: 400 });
  });
  it("ignores a stage before speechEnd exists", () => {
    const { d, id, row } = diag();
    d.mark(id, "transcribed", 300);
    expect(row().marks?.transcribed).toBeUndefined();
  });
  it("ignores a mark earlier than an earlier stage's mark", () => {
    const { d, id, row } = diag();
    d.mark(id, "speechEnd", 100); d.mark(id, "transcribed", 500); d.mark(id, "sent", 450);
    expect(row().marks).toEqual({ speechEnd: 100, transcribed: 500 });
  });
  it("ignores unknown ids, unknown stages and non-finite times", () => {
    const { d, id, row } = diag();
    d.mark(id + 99, "speechEnd", 1);
    d.mark(id, "bogus" as never, 1);
    d.mark(id, "speechEnd", Number.NaN);
    d.mark(id, "speechEnd", Number.POSITIVE_INFINITY);
    expect(row().marks?.speechEnd).toBeUndefined();
  });
  it("still accepts firstToken and firstAudio after the row settled as handed_to_chat", () => {
    const { d, id, row } = diag();
    d.mark(id, "speechEnd", 0); d.mark(id, "transcribed", 300);
    d.finish(id, "handed_to_chat", 300);
    d.mark(id, "sent", 310); d.mark(id, "firstToken", 1300); d.mark(id, "firstAudio", 1700);
    expect(stageDurations(row().marks!)).toEqual({ transcribed: 300, sent: 10, firstToken: 990, firstAudio: 400, total: 1700 });
  });
  it("a Retry (reopen) drops the row's marks, since its timing no longer means anything", () => {
    const { d, id, row } = diag();
    d.mark(id, "speechEnd", 0); d.mark(id, "transcribed", 300);
    d.finish(id, "stt_failed", 300);
    d.reopen(id);
    expect(row().marks ?? {}).toEqual({});
  });
  it("publishes to subscribers", () => {
    const { d, id } = diag();
    let n = 0; d.subscribe(() => { n++; });
    d.mark(id, "speechEnd", 5);
    expect(n).toBe(1);
  });
});
