import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SttAssembler, STT_MAX_CLIPS, STT_RETRY_DELAY_MS, placementFor, retryable, sttDeadlineMs,
  type SttEvent, type SttPlaced,
} from "./stt";
import { VoiceDiagnostics } from "./voiceDiagnostics";

type Call = { blob: Blob; signal: AbortSignal; utteranceId: number; seq: number; budgetMs: number;
  resolve: (t: string) => void; reject: (e: unknown) => void };

function harness(over: { canUpload?: () => boolean } = {}) {
  const calls: Call[] = [];
  const placed: SttPlaced<string>[][] = [];
  const events: SttEvent[] = [];
  const diagnostics = new VoiceDiagnostics(() => 0);
  const stt = new SttAssembler<string>({
    transcribe: (blob, o) => new Promise<string>((resolve, reject) => { calls.push({ blob, ...o, resolve, reject }); }),
    place: b => { placed.push(b); },
    diagnostics, canUpload: over.canUpload, onEvent: e => events.push(e), now: () => Date.now(),
  });
  const clip = (n = 100) => new Blob([new Uint8Array(n)], { type: "audio/wav" });
  const texts = () => placed.flat().map(p => p.text);
  const row = (diag: number) => diagnostics.snapshot().rows.find(r => r.id === diag)!;
  return { stt, calls, placed, events, diagnostics, clip, texts, row };
}
const flush = async () => { await vi.advanceTimersByTimeAsync(0); };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe("deadlines", () => {
  it("is max(15 s, 2 x the clip)", () => {
    expect(sttDeadlineMs(1000)).toBe(15_000);
    expect(sttDeadlineMs(10_000)).toBe(20_000);
    expect(sttDeadlineMs(NaN)).toBe(15_000);
  });

  it("expiry aborts the request, shows a visible failure and keeps the clip", async () => {
    const h = harness();
    const id = h.stt.submit(h.clip(), 1000, "ptt", "auto")!;
    expect(h.calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(h.calls[0].signal.aborted).toBe(true);
    // The one automatic retry is scheduled, still visible as pending.
    expect(h.events.at(-1)).toEqual({ kind: "retrying" });
    expect(h.stt.snapshot().clips[0].state).toBe("waiting");
    await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS);
    expect(h.calls).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(15_000);
    const failed = h.events.filter(e => e.kind === "failed");
    expect(failed).toHaveLength(1);
    expect((failed[0] as any).message).toBe("timed out after 15 s");
    expect(h.stt.clip(id)).not.toBeNull();            // retained
    expect(h.stt.snapshot().clips[0]).toMatchObject({ state: "failed", error: "timed out after 15 s", attempts: 2 });
    expect(h.row(h.stt.snapshot().clips[0].diag)).toMatchObject({ outcome: "stt_failed", kept: true });
    expect(h.texts()).toEqual([]);
  });

  it("fires even when the transport ignores its abort signal", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 5000, "wake", "auto");       // deadline 15 s; fake never settles
    await vi.advanceTimersByTimeAsync(15_000 + STT_RETRY_DELAY_MS + 15_000);
    expect(h.events.some(e => e.kind === "failed")).toBe(true);
  });
});

describe("bounded automatic retry", () => {
  it("retries a 503 once after 2 s and then stops", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.calls[0].reject(Object.assign(new Error("speech-to-text unavailable: a (HTTP 503)"), { status: 502 }));
    await flush();
    expect(h.calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS - 1);
    expect(h.calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(h.calls).toHaveLength(2);
    h.calls[1].reject(Object.assign(new Error("again"), { status: 502 }));
    await flush();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(h.calls).toHaveLength(2);                    // never a third, however long we wait
    expect(h.stt.snapshot().clips[0]).toMatchObject({ state: "failed", error: "again", attempts: 2 });
  });

  it("does not retry what the same bytes would be refused for again", async () => {
    expect(retryable(Object.assign(new Error("x"), { status: 400 }))).toBe(false);
    expect(retryable(Object.assign(new Error("x"), { status: 401 }))).toBe(false);
    expect(retryable(Object.assign(new Error("x"), { status: 429 }))).toBe(true);
    expect(retryable(new TypeError("Failed to fetch"))).toBe(true);
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.calls[0].reject(Object.assign(new Error("audio is empty"), { status: 400 }));
    await flush();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.calls).toHaveLength(1);
    expect(h.stt.snapshot().clips[0].state).toBe("failed");
  });

  it("a successful retry places the text", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.calls[0].reject(new TypeError("Failed to fetch"));
    await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS);
    h.calls[1].resolve("hello");
    await flush();
    expect(h.texts()).toEqual(["hello"]);
    expect(h.stt.snapshot().clips).toEqual([]);          // placed: audio released
  });

  it("the automatic retry is refused when uploads are no longer allowed", async () => {
    let ok = true;
    const h = harness({ canUpload: () => ok });
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.calls[0].reject(new TypeError("Failed to fetch"));
    await flush();
    ok = false;                                          // the owner muted the mic
    await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS + 100);
    expect(h.calls).toHaveLength(1);
    expect(h.stt.snapshot().clips[0].state).toBe("failed");
  });
});

describe("explicit Retry", () => {
  async function failed() {
    const h = harness();
    const id = h.stt.submit(h.clip(321), 1000, "ptt", "auto")!;
    h.calls[0].reject(Object.assign(new Error("down"), { status: 502 }));
    await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS);
    h.calls[1].reject(Object.assign(new Error("down"), { status: 502 }));
    await flush();
    return { h, id };
  }

  it("re-sends the retained clip itself, once per press, and places the text", async () => {
    const { h, id } = await failed();
    const kept = h.stt.clip(id)!;
    expect(h.stt.retry(id)).toBe(true);
    expect(h.calls).toHaveLength(3);
    expect(h.calls[2].blob).toBe(kept);                  // the same bytes, not a new capture
    expect(h.calls[2].utteranceId).toBe(id);
    expect(h.row(h.stt.snapshot().clips[0].diag).outcome).toBe("transcribing");
    h.calls[2].resolve("finally");
    await flush();
    expect(h.texts()).toEqual(["finally"]);
    expect(h.placed[0][0]).toMatchObject({ late: true });
    expect(h.stt.clip(id)).toBeNull();
    expect(h.stt.retry(id)).toBe(false);                 // nothing left to retry
  });

  it("a failing explicit attempt does not retry on its own", async () => {
    const { h, id } = await failed();
    h.stt.retry(id);
    h.calls[2].reject(Object.assign(new Error("still down"), { status: 502 }));
    await flush();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(h.calls).toHaveLength(3);
    expect(h.stt.snapshot().clips[0]).toMatchObject({ state: "failed", error: "still down" });
  });

  it("is refused while uploads are not allowed, and says so", async () => {
    let ok = true;
    const h = harness({ canUpload: () => ok });
    const id = h.stt.submit(h.clip(), 1000, "ptt", "auto")!;
    h.calls[0].reject(Object.assign(new Error("x"), { status: 400 }));
    await flush();
    ok = false;
    expect(h.stt.retry(id)).toBe(false);
    expect(h.calls).toHaveLength(1);
    expect(h.events.at(-1)?.kind).toBe("blocked");
  });

  it("discard releases the audio at once", async () => {
    const { h, id } = await failed();
    h.stt.discard(id);
    expect(h.stt.clip(id)).toBeNull();
    expect(h.stt.snapshot().clips).toEqual([]);
  });
});

describe("capture order (seq)", () => {
  it("places out-of-order replies in seq order", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    expect(h.calls.map(c => c.seq)).toEqual([1, 2, 3]);
    h.calls[2].resolve("third");
    h.calls[1].resolve("second");
    await flush();
    expect(h.texts()).toEqual([]);                       // still waiting on the first
    h.calls[0].resolve("first");
    await flush();
    expect(h.texts()).toEqual(["first", "second", "third"]);
    expect(h.placed).toHaveLength(1);                    // one batch, in order
  });

  it("a failed clip does not hold later text, whose own retry lands late", async () => {
    const h = harness();
    const a = h.stt.submit(h.clip(), 1000, "ptt", "auto")!;
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.calls[1].resolve("second");
    await flush();
    expect(h.texts()).toEqual([]);
    h.calls[0].reject(Object.assign(new Error("bad"), { status: 400 }));
    await flush();
    expect(h.texts()).toEqual(["second"]);               // no longer blocked
    h.stt.retry(a);
    h.calls[2].resolve("first");
    await flush();
    expect(h.texts()).toEqual(["second", "first"]);
    expect(h.placed[1][0].late).toBe(true);
  });

  it("an empty transcript places nothing and does not block", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.stt.submit(h.clip(), 1000, "ptt", "auto");
    h.calls[1].resolve("two");
    h.calls[0].resolve("   ");
    await flush();
    expect(h.texts()).toEqual(["two"]);
    expect(h.events.some(e => e.kind === "empty")).toBe(true);
  });

  it("carries the mode the words were said in", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "review");
    h.calls[0].resolve("x");
    await flush();
    expect(h.placed[0][0].meta).toBe("review");
  });
});

describe("where AUTO may send (placementFor)", () => {
  const cap = { mode: "auto" as const, chat: "t1" };
  const now = { mode: "auto" as const, chat: "t1", busy: false, card: false };
  const fresh = { late: false, retried: false };
  it("sends only when nothing has moved", () => {
    expect(placementFor(cap, now, fresh)).toBe("send");
  });
  it.each([
    ["captured in REVIEW", { ...cap, mode: "review" as const }, now, fresh],
    ["REVIEW now", cap, { ...now, mode: "review" as const }, fresh],
    ["OFF now", cap, { ...now, mode: "off" as const }, fresh],
    ["another chat is selected", cap, { ...now, chat: "t2" }, fresh],
    ["no chat was selected at capture", { ...cap, chat: null }, { ...now, chat: null }, fresh],
    ["the chat is running a turn", cap, { ...now, busy: true }, fresh],
    ["a card is waiting", cap, { ...now, card: true }, fresh],
    ["the text is late", cap, now, { late: true, retried: false }],
    ["the owner pressed Retry", cap, now, { late: false, retried: true }],
  ])("goes to the box when %s", (_name, c, l, i) => {
    expect(placementFor(c as any, l as any, i)).toBe("review");
  });
  it("marks text produced by Retry or after a failure", async () => {
    const h = harness();
    const a = h.stt.submit(h.clip(), 1000, "ptt", "x")!;
    h.calls[0].reject(Object.assign(new Error("bad"), { status: 400 }));
    await flush();
    h.stt.retry(a);
    h.calls[1].resolve("t");
    await flush();
    expect(h.placed[0][0]).toMatchObject({ late: true, retried: true });
  });
});

describe("whenSettled (V6's barge-in resumes only when the fate is known)", () => {
  it("waits for the placer, and for failure after the automatic retry", async () => {
    const h = harness();
    const a = h.stt.submit(h.clip(), 1000, "ptt", "x")!;
    let doneA = false; void h.stt.whenSettled(a).then(() => { doneA = true; });
    await flush();
    expect(doneA).toBe(false);                       // upload started: not settled
    h.calls[0].resolve("hi");
    await flush();
    expect(doneA).toBe(true);
    expect(h.texts()).toEqual(["hi"]);

    const b = h.stt.submit(h.clip(), 1000, "ptt", "x")!;
    let doneB = false; void h.stt.whenSettled(b).then(() => { doneB = true; });
    h.calls[1].reject(Object.assign(new Error("down"), { status: 502 }));
    await flush();
    expect(doneB).toBe(false);                       // waiting on its automatic retry
    await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS);
    h.calls[2].reject(Object.assign(new Error("down"), { status: 502 }));
    await flush();
    expect(doneB).toBe(true);
    await expect(h.stt.whenSettled(b)).resolves.toBeUndefined();   // already failed: immediate
  });

  it("an empty transcript and a discard settle too", async () => {
    const h = harness();
    const a = h.stt.submit(h.clip(), 1000, "ptt", "x")!;
    const b = h.stt.submit(h.clip(), 1000, "ptt", "x")!;
    let n = 0; void h.stt.whenSettled(a).then(() => n++); void h.stt.whenSettled(b).then(() => n++);
    h.calls[0].resolve("  ");
    await flush();
    expect(n).toBe(1);
    h.stt.discard(b);
    await flush();
    expect(n).toBe(2);
  });
});

describe("the budget handed to the daemon", () => {
  it("is the attempt's whole client deadline", () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "x");
    h.stt.submit(h.clip(), 20_000, "ptt", "x");
    expect(h.calls.map(c => c.budgetMs)).toEqual([15_000, 40_000]);
  });
});

describe("clip retention is bounded", () => {
  it("says so when the clip that just failed was itself too large to keep", async () => {
    const h = harness();
    h.stt.submit({ size: 33 * 1024 * 1024 } as Blob, 1000, "ptt", "x");
    h.calls[0].reject(Object.assign(new Error("bad"), { status: 400 }));
    await flush();
    expect(h.events.at(-1)).toMatchObject({ kind: "failed", kept: false });
    h.stt.submit(h.clip(), 500, "ptt", "x");
    h.calls[1].reject(Object.assign(new Error("bad"), { status: 400 }));
    await flush();
    expect(h.events.at(-1)).toMatchObject({ kind: "failed", kept: true });
  });

  it("counts the incoming clip against the time cap, evicting failed clips to fit", async () => {
    const h = harness();
    for (let i = 0; i < 2; i++) { h.stt.submit(h.clip(), 4 * 60_000, "ptt", "x"); h.calls[i].reject(Object.assign(new Error("b"), { status: 400 })); await flush(); }
    expect(h.stt.submit(h.clip(), 4 * 60_000, "ptt", "x")).not.toBeNull();
    const ms = h.stt.snapshot().clips.reduce((n, c) => n + c.ms, 0);
    expect(ms).toBeLessThanOrEqual(10 * 60_000);
    expect(h.stt.snapshot().clips).toHaveLength(2);        // the oldest failed one made room
  });

  it("never evicts failed clips for an incoming clip that will be refused anyway", async () => {
    const h = harness();
    h.stt.submit(h.clip(), 6 * 60_000, "ptt", "x");                   // in flight
    h.stt.submit(h.clip(), 3 * 60_000, "ptt", "x");
    h.calls[1].reject(Object.assign(new Error("b"), { status: 400 }));
    await flush();
    const failedId = h.stt.snapshot().clips.find(c => c.state === "failed")!.id;
    expect(h.stt.submit(h.clip(), 6 * 60_000, "ptt", "x")).toBeNull(); // 6+3+6 > 10, and 6+6 > 10 even without the failed one
    expect(h.stt.clip(failedId)).not.toBeNull();                       // still retryable
  });

  it("refuses an incoming clip that would exceed the caps while other audio is in flight", () => {
    const h = harness();
    h.stt.submit(h.clip(), 6 * 60_000, "ptt", "x");
    expect(h.stt.submit(h.clip(), 6 * 60_000, "ptt", "x")).toBeNull();
    h.stt.submit({ size: 20 * 1024 * 1024 } as Blob, 1000, "ptt", "x");
    expect(h.stt.submit({ size: 20 * 1024 * 1024 } as Blob, 1000, "ptt", "x")).toBeNull();
  });

  async function fail(h: ReturnType<typeof harness>, i: number) {
    h.calls[i].reject(Object.assign(new Error("bad"), { status: 400 }));
    await flush();
  }

  it("holds at most 20 clips, evicting the oldest failed one", async () => {
    const h = harness();
    for (let i = 0; i < STT_MAX_CLIPS; i++) { h.stt.submit(h.clip(), 500, "ptt", "auto"); await fail(h, i); }
    expect(h.stt.snapshot().clips).toHaveLength(STT_MAX_CLIPS);
    const oldest = h.stt.snapshot().clips[0];
    expect(h.stt.submit(h.clip(), 500, "ptt", "auto")).not.toBeNull();
    const clips = h.stt.snapshot().clips;
    expect(clips).toHaveLength(STT_MAX_CLIPS);
    expect(clips.some(c => c.id === oldest.id)).toBe(false);
    expect(h.row(oldest.diag)).toMatchObject({ outcome: "stt_failed", kept: false });
  });

  it("refuses a new clip, visibly, when 20 are all still in flight", () => {
    const h = harness();
    for (let i = 0; i < STT_MAX_CLIPS; i++) h.stt.submit(h.clip(), 500, "ptt", "auto");
    expect(h.stt.submit(h.clip(), 500, "ptt", "auto")).toBeNull();
    expect(h.stt.snapshot().clips).toHaveLength(STT_MAX_CLIPS);   // in-flight audio untouched
    expect(h.diagnostics.snapshot().rows.at(-1)?.outcome).toBe("stt_failed");
  });

  it("holds at most 10 minutes of audio", async () => {
    const h = harness();
    for (let i = 0; i < 3; i++) { h.stt.submit(h.clip(), 4 * 60_000, "ptt", "auto"); await fail(h, i); }
    const ms = h.stt.snapshot().clips.reduce((n, c) => n + c.ms, 0);
    expect(ms).toBeLessThanOrEqual(10 * 60_000);
    expect(h.stt.snapshot().clips).toHaveLength(2);
  });

  it("holds at most 32 MiB, and a lone oversize clip is dropped when it fails", async () => {
    const h = harness();
    h.stt.submit({ size: 33 * 1024 * 1024 } as Blob, 1000, "ptt", "auto");
    await fail(h, 0);
    expect(h.stt.snapshot().clips).toEqual([]);
  });

  it("releases the audio of a clip once its text is placed", async () => {
    const h = harness();
    const id = h.stt.submit(h.clip(), 500, "ptt", "auto")!;
    h.calls[0].resolve("ok");
    await flush();
    expect(h.stt.clip(id)).toBeNull();
    expect(h.stt.snapshot().clips).toEqual([]);
    expect(h.diagnostics.snapshot().rows[0].kept).toBe(false);
  });
});
