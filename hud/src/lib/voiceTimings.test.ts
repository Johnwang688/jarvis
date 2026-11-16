import { describe, expect, it } from "vitest";
import { VoiceDiagnostics } from "./voiceDiagnostics";
import { stageDurations, summarize } from "./voiceTimings";

describe("voice receipt defensive cases", () => {
  it("skips invalid marks and measures from the last valid mark without reordering", () => {
    expect(stageDurations({ speechEnd: 100, endpoint: 300, transcribed: 200, sent: 350, firstToken: NaN, firstAudio: 500 }))
      .toEqual({ endpoint: 200, sent: 50, firstAudio: 150, total: 400 });
    expect(stageDurations({ speechEnd: Infinity, sent: 100 })).toEqual({});
  });

  it("does not mutate receipts when computing a summary", () => {
    const rows = Object.freeze([Object.freeze({ speechEnd: 0, sent: 10 }), Object.freeze({ speechEnd: 0, sent: 40 })]);
    expect(summarize(rows).sent).toEqual({ n: 2, p50: 10, p90: 40 });
  });

  it("rejects a backwards fractional mark before rounding and only publishes changes", () => {
    const d = new VoiceDiagnostics(() => 0), id = d.add(1000, "wake", "transcribing");
    let published = 0; d.subscribe(() => published++);
    d.mark(id, "speechEnd", 10); d.mark(id, "endpoint", 9.6);
    d.mark(id, "sent", 20); d.mark(id, "sent", 30); d.mark(999, "speechEnd", 10);
    expect(d.snapshot().rows[0].marks).toEqual({ speechEnd: 10, sent: 20 });
    expect(published).toBe(2);
  });

  it("rounds elapsed time rather than rounding each performance-clock timestamp first", () => {
    const d = new VoiceDiagnostics(() => 0), id = d.add(1000, "wake", "transcribing");
    d.mark(id, "speechEnd", 0.4); d.mark(id, "endpoint", 700.6); d.mark(id, "transcribed", 700.7);
    expect(stageDurations(d.snapshot().rows[0].marks!)).toEqual({ endpoint: 700, transcribed: 0, total: 700 });
  });

  it("ignores marks for an evicted row and leaves the 50-row ring bounded", () => {
    const d = new VoiceDiagnostics(() => 0);
    const first = d.add(1000, "wake", "transcribing");
    for (let i = 0; i < 50; i++) d.add(1000, "ptt", "review");
    const before = d.snapshot(); d.mark(first, "speechEnd", 10);
    expect(d.snapshot()).toBe(before); expect(before.rows).toHaveLength(50);
  });
});
