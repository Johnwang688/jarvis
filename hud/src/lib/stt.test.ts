import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SttAssembler, STT_MAX_CLIPS, STT_RETRY_DELAY_MS, placementFor, retryable, sttDeadlineMs,
  type SttEvent, type SttPlaced,
} from "./stt";
import { VoiceDiagnostics } from "./voiceDiagnostics";

type Call = { blob: Blob; signal: AbortSignal; utteranceId: number; seq: number; budgetMs: number;
  resolve: (t: string) => void; reject: (e: unknown) => void };

function harness(over: { canUpload?: (explicit: boolean) => boolean; maxInFlight?: number } = {}) {
  const calls: Call[] = [];
  const placed: SttPlaced<string>[][] = [];
  const events: SttEvent[] = [];
  const diagnostics = new VoiceDiagnostics(() => 0);
  const stt = new SttAssembler<string>({
    transcribe: (blob, o) => new Promise<string>((resolve, reject) => { calls.push({ blob, ...o, resolve, reject }); }),
    place: b => { placed.push(b); },
    diagnostics, canUpload: over.canUpload, maxInFlight: over.maxInFlight, onEvent: e => events.push(e), now: () => Date.now(),
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
  const now = { mode: "auto" as const, chat: "t1", busy: false, steers: false, card: false };
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
    // V3: a busy chat is no longer a reason, but a turn that is not the
    // chat's own thread's would start a new turn rather than steer.
    ["the chat's running turn is not its own thread's", cap, { ...now, busy: true, steers: false }, fresh],
    ["a card is waiting", cap, { ...now, card: true }, fresh],
    ["the text is late", cap, now, { late: true, retried: false }],
    ["the owner pressed Retry", cap, now, { late: false, retried: true }],
  ])("goes to the box when %s", (_name, c, l, i) => {
    expect(placementFor(c as any, l as any, i)).toBe("review");
  });
  it("V3: steers the chat's own running turn instead of boxing the words", () => {
    expect(placementFor(cap, { ...now, busy: true, steers: true }, fresh)).toBe("send");
  });
  it.each([
    ["captured in REVIEW", { ...cap, mode: "review" as const }, now, fresh],
    ["another chat is selected", cap, { ...now, chat: "t2" }, fresh],
    ["the follow-up's pane was gone when claimed", { ...cap, boxOnly: true }, now, fresh],
    ["a card is waiting", cap, now, fresh],
    ["the text is late", cap, now, { late: true, retried: false }],
    ["the owner pressed Retry", cap, now, { late: false, retried: true }],
  ])("V3: a busy chat still boxes the words when %s", (name, c, l, i) => {
    const landing = { ...l, busy: true, steers: true, card: name === "a card is waiting" };
    expect(placementFor(c as any, landing as any, i)).toBe("review");
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

describe("chunked messages (V4)", () => {
  const sub = (h: ReturnType<typeof harness>, g: number, overlapMs = 0, ms = 8000) =>
    h.stt.submit(h.clip(), ms, "ptt", "meta", { group: g, overlapMs })!;
  const open = (h: ReturnType<typeof harness>) => h.stt.beginGroup("ptt", "meta");

  it("places one message, once the message has ended and every chunk has answered, chunks joined in order", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g); sub(h, g); sub(h, g);
    h.calls[2].resolve("three"); h.calls[0].resolve("one");
    await flush();
    expect(h.texts()).toEqual([]);                       // chunk two is still out, and the message is still open
    h.calls[1].resolve("two");
    await flush();
    expect(h.texts()).toEqual([]);                       // all answered, but the message has not ended
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["one two three"]);
    expect(h.placed).toHaveLength(1);
    expect(h.placed[0][0]).toMatchObject({ parts: 3, late: false, retried: false, meta: "meta" });
    expect(h.stt.snapshot().clips).toEqual([]);          // every chunk's audio released
  });

  it("names the daemon's utterance by its first chunk and each chunk by its own seq", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g); sub(h, g);
    expect(h.calls.map(c => [c.utteranceId, c.seq])).toEqual([[1, 1], [1, 2]]);
    void g;
  });

  it("an unchunked clip is still utterance = seq", () => {
    const h = harness();
    h.stt.submit(h.clip(), 1000, "ptt", "x");
    expect(h.calls[0]).toMatchObject({ utteranceId: 1, seq: 1 });
  });

  it("takes the words an overlap repeats off once, and only where the recorder overlapped", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g, 0); sub(h, g, 500); sub(h, g, 0);
    h.calls[0].resolve("we plan the work and then");
    h.calls[1].resolve("and then we build it");
    h.calls[2].resolve("build it again");            // a cut at a pause: the repeat is speech
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["we plan the work and then we build it build it again"]);
  });

  it("is one diagnostics row for the whole message, whose length is the audio, overlap once", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g, 0, 25000); sub(h, g, 500, 10000);
    expect(h.diagnostics.snapshot().rows).toHaveLength(1);
    expect(h.diagnostics.snapshot().rows[0]).toMatchObject({ outcome: "transcribing", ms: 34500, kept: true });
    h.calls[0].resolve("a"); h.calls[1].resolve("b");
    h.stt.closeGroup(g);
    await flush();
    expect(h.diagnostics.snapshot().rows[0].kept).toBe(false);
  });

  it("at most maxInFlight chunks upload at once, the rest in seq order", async () => {
    const h = harness({ maxInFlight: 2 });
    const g = open(h);
    for (let i = 0; i < 5; i++) sub(h, g);
    expect(h.calls.map(c => c.seq)).toEqual([1, 2]);
    expect(h.stt.snapshot().clips.map(c => c.state)).toEqual(["transcribing", "transcribing", "waiting", "waiting", "waiting"]);
    h.calls[1].resolve("b");                              // the later chunk finishes first
    await flush();
    expect(h.calls.map(c => c.seq)).toEqual([1, 2, 3]);
    h.calls[0].resolve("a"); await flush();
    expect(h.calls.map(c => c.seq)).toEqual([1, 2, 3, 4]);
    h.calls[2].resolve("c"); h.calls[3].resolve("d"); await flush();
    h.calls[4].resolve("e");
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["a b c d e"]);
  });

  it("a chunk waiting for its turn is not uploaded if uploads stopped being allowed meanwhile", async () => {
    let ok = true;
    const h = harness({ maxInFlight: 1, canUpload: () => ok });
    const g = open(h);
    sub(h, g); sub(h, g);
    expect(h.calls).toHaveLength(1);
    ok = false;                                           // a card came up, or the mic went OFF
    h.calls[0].resolve("one"); await flush();
    expect(h.calls).toHaveLength(1);                      // chunk two never left
    const failed = h.stt.snapshot().clips.filter(c => c.state === "failed");
    expect(failed.map(c => c.error)).toEqual(["not sent: dictation is paused"]);
    expect(h.stt.clip(failed[0].id)).not.toBeNull();     // kept, so Retry can send it when allowed
  });

  describe("a chunk that fails", () => {
    async function threeWithMiddleFailed() {
      const h = harness();
      const g = open(h);
      const ids = [sub(h, g, 0), sub(h, g, 500), sub(h, g, 500)];
      h.calls[0].resolve("alpha beta gamma");
      h.calls[2].resolve("delta epsilon zeta");
      h.calls[1].reject(Object.assign(new Error("down"), { status: 400 }));
      await flush();
      h.stt.closeGroup(g);
      await flush();
      return { h, g, ids };
    }

    it("is kept and retryable while the others are placed in order, marked late so AUTO will not send half a message", async () => {
      const { h, ids } = await threeWithMiddleFailed();
      expect(h.texts()).toEqual(["alpha beta gamma […] delta epsilon zeta"]);   // a mark where the missing part belongs
      expect(h.placed[0][0]).toMatchObject({ late: true, retried: false, parts: 2 });
      expect(h.stt.clip(ids[1])).not.toBeNull();
      expect(h.stt.snapshot().clips).toHaveLength(1);
      expect(h.stt.snapshot().clips[0]).toMatchObject({ id: ids[1], state: "failed", part: 2, of: 3 });
      expect(h.diagnostics.snapshot().rows[0]).toMatchObject({ outcome: "stt_failed", kept: true });
    });

    it("lands late, alone, and without the words its neighbours already said", async () => {
      const { h, ids } = await threeWithMiddleFailed();
      expect(h.stt.retry(ids[1])).toBe(true);
      expect(h.diagnostics.snapshot().rows[0].outcome).toBe("transcribing");
      h.calls[3].resolve("gamma and delta");              // overlaps both neighbours
      await flush();
      expect(h.texts()).toEqual(["alpha beta gamma […] delta epsilon zeta", "and"]);
      expect(h.placed[1][0]).toMatchObject({ late: true, retried: true, parts: 1 });
      expect(h.stt.snapshot().clips).toEqual([]);
    });

    it("whenGroupSettled waits for the automatic retry, then resolves as the chunk is left failed", async () => {
      const h = harness();
      const g = open(h);
      sub(h, g);
      h.stt.closeGroup(g);
      let done = false; void h.stt.whenGroupSettled(g).then(() => { done = true; });
      h.calls[0].reject(Object.assign(new Error("down"), { status: 502 }));
      await flush();
      expect(done).toBe(false);
      await vi.advanceTimersByTimeAsync(STT_RETRY_DELAY_MS);
      h.calls[1].reject(Object.assign(new Error("down"), { status: 502 }));
      await flush();
      expect(done).toBe(true);
    });
  });

  it("a chunk of silence in the middle is a normal answer, not an empty message", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g); sub(h, g); sub(h, g);
    h.calls[0].resolve("before"); h.calls[1].resolve("  "); h.calls[2].resolve("after");
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["before after"]);
    expect(h.events.some(e => e.kind === "empty")).toBe(false);
  });

  it("a message of only silence is empty, once", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g); sub(h, g);
    h.calls[0].resolve(""); h.calls[1].resolve(" ");
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual([]);
    expect(h.events.filter(e => e.kind === "empty")).toHaveLength(1);
    expect(h.diagnostics.snapshot().rows[0].outcome).toBe("empty_transcript");
  });

  it("holds the line for a later message while it is still being spoken, then frees it", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g);
    h.calls[0].resolve("long message");
    await flush();
    h.stt.submit(h.clip(), 1000, "ptt", "meta");        // the next message arrives while the first is open
    h.calls[1].resolve("short one");
    await flush();
    expect(h.texts()).toEqual([]);
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["long message", "short one"]);
  });

  it("a message that never uploaded anything closes without a trace; one lost to the backlog says so and is late", async () => {
    const h = harness();
    const empty = open(h);
    h.stt.closeGroup(empty);
    await expect(h.stt.whenGroupSettled(empty)).resolves.toBeUndefined();
    expect(h.diagnostics.snapshot().rows).toEqual([]);

    const g = open(h);
    sub(h, g);                                            // in flight
    expect(h.stt.submit({ size: 20 * 1024 * 1024 } as Blob, 1000, "ptt", "meta", { group: g })).not.toBeNull();
    expect(h.stt.submit({ size: 20 * 1024 * 1024 } as Blob, 1000, "ptt", "meta", { group: g })).toBeNull();
    h.calls[0].resolve("kept"); h.calls[1].resolve("also");
    h.stt.closeGroup(g);
    await flush();
    expect(h.placed[0][0]).toMatchObject({ text: "kept also", late: true });   // part of it is missing: never sent on its own
    expect(h.diagnostics.snapshot().rows[0].outcome).toBe("stt_failed");
  });

  it("lets a chunk's audio go once its words are in, so a long message is not bound by its length", async () => {
    const h = harness();
    const g = open(h);
    for (let i = 0; i < STT_MAX_CLIPS + 10; i++) {
      const id = sub(h, g);                              // each is accepted: none is waiting on the others' audio
      expect(id).not.toBeNull();
      h.calls[i].resolve(`w${i}`);
      await flush();
      expect(h.stt.clip(id)).toBeNull();
    }
    expect(h.stt.snapshot().clips).toEqual([]);
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()[0].split(" ")).toHaveLength(STT_MAX_CLIPS + 10);
  });

  it("a message closed `late` is placed late even if every chunk answered (it was cut short)", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g);
    h.calls[0].resolve("what was said");
    h.stt.closeGroup(g, { late: true });
    await flush();
    expect(h.placed[0][0]).toMatchObject({ text: "what was said", late: true });
    const k = harness();
    const g2 = open(k);
    sub(k, g2);
    k.calls[0].resolve("whole");
    k.stt.closeGroup(g2);
    await flush();
    expect(k.placed[0][0].late).toBe(false);
  });

  it("marks where a failed chunk belongs: first, middle and last, once each", async () => {
    const run = async (fail: boolean[]) => {
      const h = harness();
      const g = open(h);
      fail.forEach(() => sub(h, g));
      for (let i = 0; i < fail.length; i++) {
        if (fail[i]) h.calls[i].reject(Object.assign(new Error("x"), { status: 400 }));
        else h.calls[i].resolve(`t${i}`);
      }
      h.stt.closeGroup(g);
      await flush();
      return h.texts()[0];
    };
    expect(await run([true, false, false])).toBe("[…] t1 t2");
    expect(await run([false, true, true, false])).toBe("t0 […] t3");
    expect(await run([false, false, true])).toBe("t0 t1 […]");
    expect(await run([true, true])).toBeUndefined();            // nothing was heard: no message of marks
  });

  it("marks the gaps once: a retried chunk placed alone adds none (chunks 2 and 4 failed, retry 2)", async () => {
    const h = harness();
    const g = open(h);
    const ids = [sub(h, g), sub(h, g), sub(h, g), sub(h, g)];
    h.calls[0].resolve("t1"); h.calls[2].resolve("t3");
    h.calls[1].reject(Object.assign(new Error("x"), { status: 400 }));
    h.calls[3].reject(Object.assign(new Error("x"), { status: 400 }));
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["t1 […] t3 […]"]);
    h.stt.retry(ids[1]);
    h.calls[4].resolve("t2");
    await flush();
    expect(h.texts()).toEqual(["t1 […] t3 […]", "t2"]);          // not "t2 […]": chunk 4's gap was marked already
  });

  it("a cut at a pause is not de-duplicated even when the chunks share an edge word (repeats: false)", async () => {
    const h = harness();
    const g = open(h);
    h.stt.submit(h.clip(), 8000, "ptt", "meta", { group: g });
    h.stt.submit(h.clip(), 8200, "ptt", "meta", { group: g, overlapMs: 200, repeats: false });
    h.calls[0].resolve("this is very");
    h.calls[1].resolve("very good");
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["this is very very good"]);
    expect(h.stt.snapshot().clips).toEqual([]);
  });

  it("joins a fragment the cut left to the whole word the next chunk heard", async () => {
    const h = harness();
    const g = open(h);
    sub(h, g, 0); sub(h, g, 500);
    h.calls[0].resolve("sent to the transcrip");
    h.calls[1].resolve("transcription service");
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["sent to the transcription service"]);
  });

  it("asks whether an upload is allowed with `explicit` only for the owner's Retry", async () => {
    const seen: boolean[] = [];
    let allow = true;
    const h = harness({ canUpload: (explicit: boolean) => { seen.push(explicit); return allow || explicit; } });
    const id = h.stt.submit(h.clip(), 1000, "ptt", "x")!;
    h.calls[0].reject(Object.assign(new Error("x"), { status: 400 }));
    await flush();
    allow = false;                                         // Settings is open: nothing uploads on its own ...
    expect(h.stt.retry(id)).toBe(true);                    // ... but the owner's Retry from there is fine
    expect(h.calls).toHaveLength(2);
    expect(seen.at(-1)).toBe(true);
    expect(seen[0]).toBe(false);                           // the first upload was not an explicit Retry
  });

  it("cannot submit to a message that has ended", () => {
    const h = harness();
    const g = open(h);
    sub(h, g);
    h.stt.closeGroup(g);
    expect(h.stt.submit(h.clip(), 1000, "ptt", "meta", { group: g })).toBeNull();
  });

  it("discarding one chunk does not cancel the others", async () => {
    const h = harness();
    const g = open(h);
    const a = sub(h, g), b = sub(h, g);
    h.stt.discard(a);
    h.calls[1].resolve("rest");
    h.stt.closeGroup(g);
    await flush();
    expect(h.texts()).toEqual(["rest"]);
    expect(h.stt.clip(b)).toBeNull();
    expect(h.diagnostics.snapshot().rows[0].outcome).not.toBe("cancelled");
  });
});

describe("V13: which backend served the clip", () => {
  const reply = (backend: unknown, fellBack?: boolean) => ({ text: "hello", backend, fellBack });
  function withReplies() {
    const diagnostics = new VoiceDiagnostics(() => 0);
    const placed: string[] = [];
    let next: ReturnType<typeof reply> | string = reply("local");
    const stt = new SttAssembler<string>({
      transcribe: async () => next,
      place: b => { placed.push(...b.map(p => p.text)); },
      diagnostics, now: () => Date.now(),
    });
    return { stt, diagnostics, placed, set: (r: typeof next) => { next = r; } };
  }
  it("records the backend on the diagnostics row, and the text still lands", async () => {
    const h = withReplies();
    h.stt.submit(new Blob([new Uint8Array(10)]), 1000, "ptt", "auto");
    await vi.advanceTimersByTimeAsync(0);
    await h.stt.settled();
    expect(h.placed).toEqual(["hello"]);
    expect(h.diagnostics.snapshot().rows[0]).toMatchObject({ backend: "local", fellBack: false });
  });
  it("marks a clip the cloud answered because local failed", async () => {
    const h = withReplies();
    h.set(reply("cloud", true));
    h.stt.submit(new Blob([new Uint8Array(10)]), 1000, "ptt", "auto");
    await vi.advanceTimersByTimeAsync(0);
    expect(h.diagnostics.snapshot().rows[0]).toMatchObject({ backend: "cloud", fellBack: true });
  });
  it("a plain string reply still works, and an unknown backend is ignored", async () => {
    const h = withReplies();
    h.set("hi there");
    h.stt.submit(new Blob([new Uint8Array(10)]), 1000, "ptt", "auto");
    await vi.advanceTimersByTimeAsync(0);
    expect(h.placed).toEqual(["hi there"]);
    expect(h.diagnostics.snapshot().rows[0].backend).toBeUndefined();
    h.set(reply("<script>", true));
    h.stt.submit(new Blob([new Uint8Array(10)]), 1000, "ptt", "auto");
    await vi.advanceTimersByTimeAsync(0);
    expect(h.diagnostics.snapshot().rows[1].backend).toBeUndefined();
    expect(JSON.stringify(h.diagnostics.snapshot())).not.toContain("script");
  });
  it("a two-chunk message answered by the cloud (fallback) and then locally still shows the cloud was used", async () => {
    const h = withReplies();
    const g = h.stt.beginGroup("ptt", "auto");
    const replies = [reply("cloud", true), reply("local", false)];
    let n = 0;
    (h.stt as any).o.transcribe = async () => replies[n++];
    h.stt.submit(new Blob([new Uint8Array(10)]), 8000, "ptt", "auto", { group: g });
    h.stt.submit(new Blob([new Uint8Array(10)]), 8000, "ptt", "auto", { group: g });
    await vi.advanceTimersByTimeAsync(0);
    h.stt.closeGroup(g);
    await vi.advanceTimersByTimeAsync(0);
    expect(h.diagnostics.snapshot().rows).toHaveLength(1);
    expect(h.diagnostics.snapshot().rows[0]).toMatchObject({ backend: "cloud", fellBack: true });
  });

  it("an empty transcript still says who heard it", async () => {
    const h = withReplies();
    h.set({ text: "  ", backend: "local", fellBack: false });
    h.stt.submit(new Blob([new Uint8Array(10)]), 1000, "ptt", "auto");
    await vi.advanceTimersByTimeAsync(0);
    expect(h.diagnostics.snapshot().rows[0]).toMatchObject({ outcome: "empty_transcript", backend: "local" });
  });
});
