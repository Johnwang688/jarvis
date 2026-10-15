import { describe, expect, it } from "vitest";
import { VoiceDiagnostics, normalizeMicSettings, openMicrophone } from "./voiceDiagnostics";

describe("window-only bounded voice metadata", () => {
  it("falls back only for unsupported constraints, not microphone permission refusal", async () => {
    const calls: MediaStreamConstraints[] = []; const stream = {} as MediaStream;
    const result = await openMicrophone(async c => { calls.push(c); if (calls.length === 1) throw { name: "OverconstrainedError", message: "private" }; return stream; });
    expect(result).toBe(stream); expect(calls).toEqual([{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }, { audio: true }]);
    let deniedCalls = 0;
    await expect(openMicrophone(async () => { deniedCalls++; throw { name: "NotAllowedError", message: "private" }; })).rejects.toMatchObject({ name: "NotAllowedError" });
    expect(deniedCalls).toBe(1);
  });
  it("retains exactly the last 50 utterances, replacing a pending row once", () => {
    let clock = 0; const d = new VoiceDiagnostics(() => ++clock);
    for (let i = 0; i < 100; i++) d.add(1250, "wake", "unclaimed");
    expect(d.snapshot().rows).toHaveLength(50);
    expect(d.snapshot().rows[0].id).toBe(51);
    const id = d.add(2000, "ptt", "transcribing");
    d.finish(id, "stt_failed", 777); d.finish(id, "handed_to_chat", 123);
    expect(d.snapshot().rows.at(-1)).toMatchObject({ id, outcome: "stt_failed", latencyMs: 777 });
    expect(d.snapshot().rows).toHaveLength(50);
    expect(new VoiceDiagnostics().snapshot().rows).toEqual([]);
  });
  it("only accepts bounded numbers and known source/outcome fields", () => {
    const d = new VoiceDiagnostics(() => Infinity);
    d.add(NaN, "private transcript" as any, "secret exception" as any);
    d.add(1e20, "wake", "unclaimed");
    const rows = d.snapshot().rows;
    expect(rows[0]).toEqual({ id: 1, at: 0, ms: 0, source: "unclaimed", detector: "energy", outcome: "unclaimed" });
    expect(rows[1].ms).toBe(900000);
    expect(JSON.stringify(rows)).not.toMatch(/private|secret|Infinity|NaN/);
  });
  it("normalizes actual readback without retaining device IDs or unexpected values", () => {
    expect(normalizeMicSettings({ echoCancellation: true, noiseSuppression: false, autoGainControl: "remote-only", deviceId: "private", groupId: "private", channelCount: 2 })).toEqual({ echoCancellation: true, noiseSuppression: false, autoGainControl: "unknown" });
    expect(normalizeMicSettings({ echoCancellation: [true, false], noiseSuppression: { value: true } })).toEqual(normalizeMicSettings(null));
    expect(normalizeMicSettings([true])).toEqual(normalizeMicSettings(null));
    expect(normalizeMicSettings({ get echoCancellation() { throw Error("private"); } })).toEqual(normalizeMicSettings(null));
    const d = new VoiceDiagnostics(); d.microphone("available", { echoCancellation: true, deviceId: "private" });
    expect(JSON.stringify(d.snapshot())).not.toContain("private");
  });
  it("has a stable external-store snapshot until metadata changes", () => {
    const d = new VoiceDiagnostics(); const first = d.snapshot(); let calls = 0;
    const off = d.subscribe(() => calls++); d.subscribe(() => { throw Error("observer"); });
    expect(d.snapshot()).toBe(first); d.add(1, "wake", "unclaimed");
    expect(d.snapshot()).not.toBe(first); expect(calls).toBe(1); off();
    d.add(1, "wake", "unclaimed"); expect(calls).toBe(1);
  });
});

describe("V2 retained-clip bookkeeping", () => {
  it("reopens only a failed row, once settled again it keeps its kept flag and loses stale latency", () => {
    const d = new VoiceDiagnostics(() => 5);
    const id = d.add(1000, "ptt", "transcribing");
    d.keep(id, true);
    d.reopen(id);                                    // not failed: ignored
    expect(d.snapshot().rows[0].outcome).toBe("transcribing");
    d.finish(id, "stt_failed", 900);
    expect(d.snapshot().rows[0]).toMatchObject({ outcome: "stt_failed", latencyMs: 900, kept: true });
    d.reopen(id);
    expect(d.snapshot().rows[0]).toMatchObject({ outcome: "transcribing", kept: true });
    expect(d.snapshot().rows[0].latencyMs).toBeUndefined();
    d.finish(id, "review", 100);
    expect(d.snapshot().rows[0].outcome).toBe("review");
    d.keep(id, false);
    expect(d.snapshot().rows[0].kept).toBe(false);
  });
});

describe("the detector line (V5)", () => {
  it("starts as the energy detector, names Silero when it is live, and stamps each row with the detector that judged it", async () => {
    const { VoiceDiagnostics } = await import("./voiceDiagnostics");
    const d = new VoiceDiagnostics(() => 5);
    expect(d.snapshot()).toMatchObject({ detector: "energy", detectorPhase: "idle" });
    const a = d.add(1000, "wake", "unclaimed");
    d.detector("silero", "ready", "Silero VAD, running in a worker", 0.62);
    expect(d.snapshot()).toMatchObject({ detector: "silero", detectorPhase: "ready", detectorMs: 0.62 });
    const b = d.add(1000, "wake", "unclaimed");
    d.detector("energy", "failed", "the VAD answered too late");
    const c = d.add(1000, "wake", "unclaimed");
    const rows = d.snapshot().rows;
    expect([a, b, c].map((id) => rows.find((r) => r.id === id)!.detector)).toEqual(["energy", "silero", "energy"]);
    expect(d.snapshot().detectorNote).toBe("the VAD answered too late");
  });
  it("bounds what it will show", async () => {
    const { VoiceDiagnostics } = await import("./voiceDiagnostics");
    const d = new VoiceDiagnostics();
    d.detector("bogus" as never, "weird" as never, "x".repeat(900), Infinity);
    expect(d.snapshot()).toMatchObject({ detector: "energy", detectorPhase: "idle", detectorMs: 0 });
    expect(d.snapshot().detectorNote.length).toBe(200);
  });
});

describe("V13 served-by metadata", () => {
  it("keeps only the two known backends and clears them when the row is retried", () => {
    const d = new VoiceDiagnostics(() => 1);
    const id = d.add(1000, "ptt", "transcribing");
    d.served(id, "quantum"); d.served(id, { toString: () => "local" } as any); d.served(999, "local");
    expect(d.snapshot().rows[0].backend).toBeUndefined();
    d.served(id, "cloud", true);
    expect(d.snapshot().rows[0]).toMatchObject({ backend: "cloud", fellBack: true });
    d.finish(id, "stt_failed", 10);
    d.reopen(id);
    expect(d.snapshot().rows[0].backend).toBeUndefined();
  });

  it("a message served by several chunks never hides the cloud: it sticks, and so does a fallback", () => {
    const d = new VoiceDiagnostics(() => 1);
    const id = d.add(1000, "ptt", "transcribing");
    d.served(id, "cloud", true);
    d.served(id, "local", false);                       // a later chunk was local
    expect(d.snapshot().rows[0]).toMatchObject({ backend: "cloud", fellBack: true });
    const other = d.add(1000, "ptt", "transcribing");
    d.served(other, "local"); d.served(other, "cloud", false); d.served(other, "local");
    expect(d.snapshot().rows[1]).toMatchObject({ backend: "cloud", fellBack: false });
    const plain = d.add(1000, "ptt", "transcribing");
    d.served(plain, "local"); d.served(plain, "local");
    expect(d.snapshot().rows[2]).toMatchObject({ backend: "local", fellBack: false });
  });
});
