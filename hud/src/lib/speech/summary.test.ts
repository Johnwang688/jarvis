import { beforeEach, describe, expect, it } from "vitest";
import { SpeechController } from "./controller";
import {
  FALLBACK_CHARS, MORE_ON_SCREEN, SUMMARY_DEADLINE_MS, SUMMARY_MAX_CHARS, VERBATIM_MAX_CHARS,
  summaryChunk, truncatedProse,
} from "./summary";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./testkit";

/** A summariser the test settles by hand. */
class FakeSummarize {
  calls: { text: string; thread: string; signal: AbortSignal }[] = [];
  private waiting: { resolve: (s: string) => void; reject: (e: Error) => void }[] = [];
  /** Answer at once with this (a string), or reject with an Error; unset: wait for `answer`. */
  auto: string | Error | null = null;

  fn = (text: string, opts: { thread: string; signal: AbortSignal }): Promise<string> => {
    this.calls.push({ text, thread: opts.thread, signal: opts.signal });
    return new Promise((resolve, reject) => {
      opts.signal.addEventListener("abort", () => reject(new Error("aborted")));
      if (this.auto instanceof Error) reject(this.auto);
      else if (this.auto !== null) resolve(this.auto);
      else this.waiting.push({ resolve, reject });
    });
  };

  answer(summary: string, i = 0) { this.waiting[i].resolve(summary); }
}

let clock: FakeClock;
let player: FakePlayer;
let say: FakeSay;
let gate: FakeGate;
let sum: FakeSummarize;
let c: SpeechController;

const T = "thread-1";
const U = "thread-2";
const settle = (text: string, thread = T) => c.event({ kind: "text", thread_id: thread, data: { text } });
const delta = (text: string, thread = T) => c.event({ kind: "text_delta", thread_id: thread, data: { text } });
const tool = (thread = T) => c.event({ kind: "tool_started", thread_id: thread, data: {} });
const SUMMARY = "The change is done and the tests pass.";

/** Prose of exactly `n` spoken characters: plain sentences, the last padded to fit. */
function prose(n: number): string {
  const sentence = "A fairly ordinary sentence of the kind agents write.";
  const parts: string[] = [];
  let len = 0;
  for (;;) {
    const room = n - len - (parts.length ? 1 : 0);
    if (room <= 80) {
      if (room > 0) parts.push("w".repeat(room - 1) + ".");
      break;
    }
    parts.push(sentence);
    len += sentence.length + (parts.length > 1 ? 1 : 0);
  }
  const text = parts.join(" ");
  expect(text.length).toBe(n);
  return text;
}

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  say = new FakeSay(clock);
  gate = new FakeGate();
  sum = new FakeSummarize();
  c = new SpeechController({ say: say.fn, player, clock, gate, summarize: sum.fn });
  c.setSpeakers(new Set([T, U]));
});

describe("how long a message is said (open question 2: summary only)", () => {
  it("pins the owner's numbers", () => {
    expect(VERBATIM_MAX_CHARS).toBe(600);
    expect(FALLBACK_CHARS).toBe(400);
  });

  it("a message of 600 characters is read as written and no summary is asked for", async () => {
    const text = prose(600);
    settle(text);
    await clock.advance(120_000);
    expect(player.started.join(" ")).toBe(text);
    expect(sum.calls).toEqual([]);
  });

  it("601 characters become the summary and nothing else: no verbatim lead-in", async () => {
    const text = prose(601);
    delta(text);
    expect(sum.calls).toEqual([]);   // not while it streams: the length is not known yet
    settle(text);
    expect(sum.calls).toHaveLength(1);
    expect(sum.calls[0]).toMatchObject({ thread: T, text });
    sum.answer(SUMMARY);
    await clock.advance(30_000);
    expect(player.started).toEqual([SUMMARY]);
    expect(say.calls.map((x) => x.text)).toEqual([SUMMARY]);
    expect(c.items().past.map((i) => i.kind)).toEqual(["summary"]);
  });

  it("only prose counts: code is neither measured nor sent", async () => {
    const code = "```ts\nconst secret_token = 'abc';\n".concat("const more = 1;\n".repeat(80), "```\n");
    const text = `${prose(300)}\n${code}${prose(250)}`;
    settle(text);
    await clock.advance(120_000);
    expect(sum.calls).toEqual([]);
    expect(player.started.join(" ")).not.toMatch(/secret_token|const /);
    // Long prose around code: the summariser gets the prose, not the code.
    settle(`${prose(400)}\n${code}${prose(400)}`);
    expect(sum.calls).toHaveLength(1);
    expect(sum.calls[0].text).not.toMatch(/secret_token|const |```/);
    expect(sum.calls[0].text.length).toBeGreaterThan(VERBATIM_MAX_CHARS);
  });

  it("a long reply streamed in deltas is asked for once, when it closes", async () => {
    const text = prose(900);
    for (let i = 0; i < text.length; i += 37) delta(text.slice(i, i + 37));
    await clock.advance(5_000);
    expect(sum.calls).toEqual([]);
    expect(player.started).toEqual([]);
    tool();
    expect(sum.calls).toHaveLength(1);
    expect(sum.calls[0].text).toBe(text);
  });

  it("a short message after the tool is verbatim: each message is judged alone", async () => {
    delta(prose(700));
    tool();
    sum.answer(SUMMARY);
    settle("Found it, the bug is on line forty.");
    await clock.advance(60_000);
    expect(player.started).toEqual([SUMMARY, "Found it, the bug is on line forty."]);
  });
});

describe("when the summary cannot be had (V-4)", () => {
  const LONG = Array.from({ length: 30 }, (_, i) => `This is sentence ${i}, and it says one more thing.`).join(" ");

  it("a failed request reads the opening and marks the rest", async () => {
    sum.auto = new Error("502");
    settle(LONG);
    await clock.advance(120_000);
    const said = player.started;
    expect(said.at(-1)).toBe(MORE_ON_SCREEN);
    const body = said.slice(0, -1).join(" ");
    expect(body.length).toBeLessThanOrEqual(FALLBACK_CHARS);
    expect(body.length).toBeGreaterThan(100);
    expect(LONG.startsWith(body)).toBe(true);
    expect(c.items().past.at(-1)?.kind).toBe("fallback");
  });

  it("no answer in time reads the same fallback, and the request is abandoned", async () => {
    settle(LONG);
    await clock.advance(SUMMARY_DEADLINE_MS - 100);
    expect(player.started).toEqual([]);
    await clock.advance(200);
    expect(sum.calls[0].signal.aborted).toBe(true);
    await clock.advance(60_000);
    expect(player.started.at(-1)).toBe(MORE_ON_SCREEN);
    expect(player.started.length).toBeGreaterThan(1);
  });

  it("an unusable answer (empty, or longer than promised) is a failure", async () => {
    for (const bad of ["", "   ", "x".repeat(SUMMARY_MAX_CHARS + 1)]) {
      sum.auto = bad;
      const before = player.started.length;
      settle(LONG);
      await clock.advance(120_000);
      expect(player.started.slice(before).at(-1), JSON.stringify(bad.slice(0, 5))).toBe(MORE_ON_SCREEN);
    }
  });

  it("with no summariser a long message is read truncated, never in full", async () => {
    const plain = new SpeechController({ say: say.fn, player, clock, gate });
    plain.setSpeakers(new Set([T]));
    plain.event({ kind: "text", thread_id: T, data: { text: LONG } });
    await clock.advance(120_000);
    expect(player.started.at(-1)).toBe(MORE_ON_SCREEN);
    expect(player.started.join(" ").length).toBeLessThan(LONG.length / 2);
  });

  it("truncatedProse: whole sentences up to 400, a first sentence over 400 is clipped at a word", () => {
    const chunks = Array.from({ length: 20 }, (_, i) => ({ text: `Sentence number ${i} here.`, kind: "verbatim" as const }));
    const out = truncatedProse(chunks);
    expect(out.at(-1)).toEqual({ text: MORE_ON_SCREEN, kind: "fallback" });
    expect(out.slice(0, -1).map((x) => x.text).join(" ").length).toBeLessThanOrEqual(FALLBACK_CHARS);
    const huge = truncatedProse([{ text: "word ".repeat(200).trim(), kind: "verbatim" }]);
    expect(huge).toHaveLength(2);
    expect(huge[0].text.length).toBeLessThanOrEqual(FALLBACK_CHARS);
    expect(huge[0].text.endsWith("word")).toBe(true);
  });

  it("summaryChunk accepts only a short non-empty string", () => {
    expect(summaryChunk("  A  short \n one. ")).toEqual({ text: "A short one.", kind: "summary" });
    for (const bad of [null, undefined, 7, "", "  "]) expect(summaryChunk(bad)).toBeNull();
  });
});

describe("order and cancellation", () => {
  const LONG = prose(900);

  it("a later message of the thread waits behind the summary it closed after", async () => {
    settle(LONG);
    settle("Short second message, said second.");
    await clock.advance(5_000);
    expect(player.started).toEqual([]);   // the long one is still out
    sum.answer(SUMMARY);
    await clock.advance(60_000);
    expect(player.started).toEqual([SUMMARY, "Short second message, said second."]);
  });

  it("another thread is not held up by a thread's summary", async () => {
    settle(LONG);
    settle("Quick word from the other thread.", U);
    await clock.advance(5_000);
    expect(player.started).toEqual(["Quick word from the other thread."]);
    sum.answer(SUMMARY);
    await clock.advance(60_000);
    expect(player.started.at(-1)).toBe(SUMMARY);
  });

  it("a message sent to the thread cancels the summary: nothing is said", async () => {
    settle(LONG);
    c.event({ kind: "user_message", thread_id: T, data: { via: "hud" } });
    expect(sum.calls[0].signal.aborted).toBe(true);
    await clock.advance(SUMMARY_DEADLINE_MS + 60_000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
  });

  it("an answer that arrives after the thread was silenced is not said", async () => {
    settle(LONG);
    c.event({ kind: "user_message", thread_id: T, data: {} });
    sum.answer(SUMMARY);
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    settle("The new turn speaks.");
    await clock.advance(30_000);
    expect(player.started).toEqual(["The new turn speaks."]);
  });

  it("stop, an interrupted turn, a pane closing, read-aloud off and losing the lock all cancel it", async () => {
    const cancels: [string, () => void][] = [
      ["stop", () => c.stop()],
      ["interrupted", () => c.event({ kind: "turn_finished", thread_id: T, data: { stop: "interrupted" } })],
      ["pane closed", () => c.setSpeakers(new Set([U]))],
      ["read-aloud off", () => c.setEnabled(false)],
      ["lock lost", () => gate.set(false)],
    ];
    for (const [name, act] of cancels) {
      c.setEnabled(true);
      gate.set(true);
      c.setSpeakers(new Set([T, U]));
      const n = sum.calls.length;
      settle(LONG);
      const call = sum.calls[n];
      act();
      expect(call.signal.aborted, name).toBe(true);
      await clock.advance(SUMMARY_DEADLINE_MS + 60_000);
      expect(player.started, name).toEqual([]);
    }
  });

  it("skip with nothing sounding abandons the message being waited for", async () => {
    settle(LONG);
    c.skip();
    expect(sum.calls[0].signal.aborted).toBe(true);
    await clock.advance(SUMMARY_DEADLINE_MS + 60_000);
    expect(player.started).toEqual([]);
  });

  it("skip while a message is held (still streaming) drops it", async () => {
    delta("This will be skipped, and never said at all. ");
    c.skip();
    delta("More that must not be said either.");
    tool();
    await clock.advance(30_000);
    expect(player.started).toEqual([]);
    settle("The next message is said.");
    await clock.advance(30_000);
    expect(player.started).toEqual(["The next message is said."]);
  });

  it("while a summary plays, the next long message gets its own request", async () => {
    settle(LONG);
    sum.answer("First summary.");
    await clock.advance(1_000);
    settle(LONG);
    expect(sum.calls).toHaveLength(2);
    sum.answer("Second summary.", 1);
    await clock.advance(60_000);
    expect(player.started).toEqual(["First summary.", "Second summary."]);
  });
});
