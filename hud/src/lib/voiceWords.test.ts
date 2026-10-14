// V10: send and cancel words, read from the transcript (plan §3.3 phase 1, Q1).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CANCELLED_STATUS, DEFAULT_WORDS, DictationLedger, MAX_WORDS, MAX_WORD_LENGTH, SEND_WORD_SILENCE_MS, UNDO_MS, UndoHold,
  loadWords, normalizeList, normalizeWords, parseWord, planWords, readWord, refusal, saveWords, wordMaySend, type Words,
} from "./voiceWords";
import type { SttCapture, SttLanding } from "./stt";

const W: Words = { enabled: true, send: ["over"], cancel: ["cancel"] };
const QUIET = 600;

describe("constants (owner's answer to Q1)", () => {
  it("needs 500 ms of quiet, undo lasts 10 s, the defaults are over and cancel", () => {
    expect(SEND_WORD_SILENCE_MS).toBe(500);
    expect(UNDO_MS).toBe(10_000);
    expect(DEFAULT_WORDS).toEqual({ enabled: true, send: ["over"], cancel: ["cancel"] });
    expect(CANCELLED_STATUS).toBe("CANCELLED · UNDO");
  });
});

describe("readWord: only a last word, after enough quiet", () => {
  it("strips a trailing over and says send (send it over, 600 ms of quiet)", () => {
    expect(readWord("send it over", W, QUIET)).toEqual({ text: "send it", word: "send" });
  });

  it("is case and punctuation tolerant, and removes what the word leaves hanging", () => {
    expect(readWord("Send it, over.", W, QUIET)).toEqual({ text: "Send it", word: "send" });
    expect(readWord("Do the thing. Over!", W, QUIET)).toEqual({ text: "Do the thing.", word: "send" });
    expect(readWord("fix it - OVER", W, QUIET)).toEqual({ text: "fix it", word: "send" });
    expect(readWord('say "over"', W, QUIET)).toEqual({ text: "say", word: "send" });
    expect(readWord("over", W, QUIET)).toEqual({ text: "", word: "send" });
  });

  it("the game is over, with no quiet after it, is a sentence", () => {
    expect(readWord("the game is over", W, 0)).toEqual({ text: "the game is over", word: null });
    expect(readWord("the game is over", W, SEND_WORD_SILENCE_MS - 1)).toEqual({ text: "the game is over", word: null });
    expect(readWord("the game is over", W, SEND_WORD_SILENCE_MS)).toEqual({ text: "the game is", word: "send" });
  });

  it("over in the middle of a sentence is prose", () => {
    for (const t of ["the game is over there", "come over to my place", "over the hill and far away", "handover is tomorrow", "I'm all over it", "game-over screen", "over and out"])
      expect(readWord(t, W, QUIET)).toEqual({ text: t, word: null });
  });

  it("a different word that merely ends or begins the same way is not the word", () => {
    for (const t of ["see you later, rover", "it is moreover", "an overtime", "hangover"]) expect(readWord(t, W, QUIET).word).toBeNull();
  });

  it("a trailing cancel discards", () => {
    expect(readWord("never mind that cancel", W, QUIET)).toEqual({ text: "never mind that", word: "cancel" });
    expect(readWord("Cancel.", W, QUIET)).toEqual({ text: "", word: "cancel" });
    expect(readWord("please cancel my order for tomorrow", W, QUIET).word).toBeNull();
  });

  it("is off with the toggle, and a list that is empty is that function off", () => {
    expect(readWord("send it over", { ...W, enabled: false }, QUIET).word).toBeNull();
    expect(readWord("send it over", { ...W, send: [] }, QUIET).word).toBeNull();
    expect(readWord("never mind cancel", { ...W, cancel: [] }, QUIET).word).toBeNull();
  });

  it("reads an edited list", () => {
    const w: Words = { enabled: true, send: ["go", "done"], cancel: ["scratch"] };
    expect(readWord("write the report go", w, QUIET)).toEqual({ text: "write the report", word: "send" });
    expect(readWord("write the report, done.", w, QUIET)).toEqual({ text: "write the report", word: "send" });
    expect(readWord("forget that scratch", w, QUIET)).toEqual({ text: "forget that", word: "cancel" });
    expect(readWord("send it over", w, QUIET).word).toBeNull(); // no longer a word
  });

  it("curly apostrophes and a missing tail measurement fail safe", () => {
    expect(readWord("send it over", W, NaN).word).toBeNull();
    expect(readWord("send it over", W, undefined as unknown as number).word).toBeNull();
    expect(readWord("", W, QUIET)).toEqual({ text: "", word: null });
    expect(readWord("   ", W, QUIET).word).toBeNull();
  });
});

describe("the list: single words, bounded", () => {
  it("parses one word", () => {
    expect(parseWord(" Over ")).toBe("over");
    expect(parseWord("don’t")).toBe("don't");
    expect(parseWord("mid-word")).toBe("mid-word");
    for (const bad of ["", " ", "a", "two words", "x".repeat(MAX_WORD_LENGTH + 1), "over!", "o;ver", "-over", "over-", "ov\ner", 7, null, undefined, {}])
      expect(parseWord(bad as unknown)).toBeNull();
  });

  it("says why an entry is refused", () => {
    expect(refusal("", [], [])).toMatch(/word/i);
    expect(refusal("send it", [], [])).toBe("One word only.");
    expect(refusal("a", [], [])).toMatch(/single word/);
    expect(refusal("over", ["over"], [])).toBe("Already in this list.");
    expect(refusal("cancel", ["over"], ["cancel"])).toBe("Already in the other list.");
    expect(refusal("six", ["a1", "b1", "c1", "d1", "e1"], [])).toBe(`At most ${MAX_WORDS} words.`);
    expect(refusal("fine", ["over"], ["cancel"])).toBeNull();
  });

  it("normalizes: valid, unique, bounded, and not in the other list", () => {
    expect(normalizeList(["Over", "over", "two words", 3, "done", "x", "ok1", "ok2", "ok3", "ok4"])).toEqual(["over", "done", "ok1", "ok2", "ok3"]);
    expect(normalizeList("over")).toEqual([]);
    expect(normalizeList(["over", "stop"], ["over"])).toEqual(["stop"]);
  });

  it("normalizes a stored value; a missing list is the default and an empty one stays empty", () => {
    expect(normalizeWords(null)).toEqual(DEFAULT_WORDS);
    expect(normalizeWords("junk")).toEqual(DEFAULT_WORDS);
    expect(normalizeWords([])).toEqual(DEFAULT_WORDS);
    expect(normalizeWords({ send: [] })).toEqual({ enabled: true, send: [], cancel: ["cancel"] });
    expect(normalizeWords({ enabled: false, send: ["go"], cancel: ["go", "stop"] })).toEqual({ enabled: false, send: ["go"], cancel: ["stop"] });
    expect(normalizeWords({ send: "over", cancel: 4 })).toEqual(DEFAULT_WORDS);
  });

  it("round-trips through storage, forgets quietly without it", () => {
    const mem = new Map<string, string>();
    const store = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => { mem.set(k, v); } };
    expect(loadWords(store)).toEqual(DEFAULT_WORDS);
    saveWords({ enabled: false, send: ["go"], cancel: [] }, store);
    expect(loadWords(store)).toEqual({ enabled: false, send: ["go"], cancel: [] });
    mem.set("jarvis.hud.voice.words", "{not json");
    expect(loadWords(store)).toEqual(DEFAULT_WORDS);
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(loadWords(broken)).toEqual(DEFAULT_WORDS);
    expect(() => saveWords(W, broken)).not.toThrow();
  });

  it("saving never stores an invalid word", () => {
    const mem = new Map<string, string>();
    saveWords({ enabled: true, send: ["ok", "two words", "<script>"], cancel: ["fine"] }, { setItem: (k, v) => { mem.set(k, v); } });
    expect(JSON.parse([...mem.values()][0])).toEqual({ enabled: true, send: ["ok"], cancel: ["fine"] });
  });
});

const item = (text: string, over: Partial<{ late: boolean; retried: boolean; tailMs: number | undefined }> = {}) => ({
  text, late: over.late ?? false, retried: over.retried ?? false, meta: { tailMs: "tailMs" in over ? over.tailMs : QUIET },
});

describe("planWords", () => {
  const ctx = { words: W, card: false };

  it("no word anywhere: one plain run of the batch, untouched", () => {
    const batch = [item("hello there"), item("the game is over", { tailMs: 0 })];
    expect(planWords(batch, ctx)).toEqual([{ kind: "run", items: batch }]);
    expect(planWords([], ctx)).toEqual([]);
  });

  it("a send word sends the batch so far with the word stripped", () => {
    const [step, ...rest] = planWords([item("first part"), item("and send it over")], ctx);
    expect(rest).toEqual([]);
    expect(step.kind).toBe("send");
    expect(step.items.map((i) => i.text)).toEqual(["first part", "and send it"]);
    expect(step.kind === "send" && step.word.text).toBe("and send it");
  });

  it("a cancel discards the batch so far, and what follows is a new run", () => {
    const steps = planWords([item("one"), item("two cancel"), item("three")], ctx);
    expect(steps.map((s) => s.kind)).toEqual(["cancel", "run"]);
    expect(steps[0].items.map((i) => i.text)).toEqual(["one", "two"]);
    expect(steps[1].items.map((i) => i.text)).toEqual(["three"]);
  });

  it("a send, then a cancel, then more", () => {
    const steps = planWords([item("a over"), item("b cancel"), item("c")], ctx);
    expect(steps.map((s) => s.kind)).toEqual(["send", "cancel", "run"]);
  });

  it("a message without the quiet, or whose length of quiet is unknown, is never read", () => {
    expect(planWords([item("go over", { tailMs: 200 })], ctx)[0].kind).toBe("run");
    expect(planWords([item("go over", { tailMs: undefined })], ctx)[0].kind).toBe("run");
  });

  it("a late text (a chunk failed) or the owner's Retry is never read: the words are not the end of the message", () => {
    expect(planWords([item("go over", { late: true })], ctx)[0].kind).toBe("run");
    expect(planWords([item("go over", { retried: true })], ctx)[0].kind).toBe("run");
    expect(planWords([item("go cancel", { late: true })], ctx)[0].kind).toBe("run");
  });

  it("with a card up the words are inert: the text is placed verbatim, nothing stripped", () => {
    const batch = [item("approve it over"), item("deny it cancel")];
    expect(planWords(batch, { words: W, card: true })).toEqual([{ kind: "run", items: batch }]);
  });

  it("does not mutate the batch", () => {
    const batch = [item("send it over")];
    planWords(batch, ctx);
    expect(batch[0].text).toBe("send it over");
  });
});

describe("wordMaySend: the same conditions as placementFor, except the mode", () => {
  const cap = (over: Partial<SttCapture> = {}): SttCapture => ({ mode: "review", chat: "t1", ...over });
  const land = (over: Partial<SttLanding> = {}): SttLanding => ({ mode: "review", chat: "t1", busy: false, steers: false, card: false, ...over });
  const it0 = { late: false, retried: false };

  it("sends in REVIEW and in AUTO", () => {
    expect(wordMaySend(cap(), land(), it0)).toBe(true);
    expect(wordMaySend(cap({ mode: "auto" }), land({ mode: "auto" }), it0)).toBe(true);
    expect(wordMaySend(cap({ mode: "auto" }), land({ mode: "review" }), it0)).toBe(true);
  });

  it("waits in the box when anything moved", () => {
    expect(wordMaySend(cap(), land({ chat: "t2" }), it0)).toBe(false);
    expect(wordMaySend(cap({ chat: null }), land({ chat: null }), it0)).toBe(false);
    expect(wordMaySend(cap({ boxOnly: true }), land(), it0)).toBe(false);
    expect(wordMaySend(cap(), land({ card: true }), it0)).toBe(false);
    expect(wordMaySend(cap(), land({ busy: true, steers: false }), it0)).toBe(false);
    expect(wordMaySend(cap(), land({ busy: true, steers: true }), it0)).toBe(true);
    expect(wordMaySend(cap(), land(), { late: true, retried: false })).toBe(false);
    expect(wordMaySend(cap(), land(), { late: false, retried: true })).toBe(false);
  });

  it("never when the mic is OFF, then or now", () => {
    expect(wordMaySend(cap({ mode: "off" }), land(), it0)).toBe(false);
    expect(wordMaySend(cap(), land({ mode: "off" }), it0)).toBe(false);
  });
});

describe("DictationLedger: a cancel removes what was dictated, not what was typed", () => {
  it("removes dictated pieces from the box and leaves typed text", () => {
    const l = new DictationLedger();
    l.add("t1", "first dictated");
    l.add("t1", "second dictated");
    const r = l.take("t1", "typed note first dictated second dictated");
    expect(r).toEqual({ input: "typed note", removed: ["first dictated", "second dictated"], kept: 0 });
  });

  it("text typed between the pieces stays", () => {
    const l = new DictationLedger();
    l.add("t1", "alpha");
    l.add("t1", "gamma");
    expect(l.take("t1", "alpha beta gamma").input).toBe("beta");
  });

  it("a piece the owner edited stays and is counted", () => {
    const l = new DictationLedger();
    l.add("t1", "the quick brown fox");
    l.add("t1", "jumps over");
    const r = l.take("t1", "the quick red fox jumps over");
    expect(r.input).toBe("the quick red fox");
    expect(r.removed).toEqual(["jumps over"]);
    expect(r.kept).toBe(1);
  });

  it("a box the owner emptied is not 'edited': nothing is kept, nothing is reported", () => {
    const l = new DictationLedger();
    l.add("t1", "gone words");
    expect(l.take("t1", "")).toEqual({ input: "", removed: [], kept: 0 });
    l.add("t1", "gone words");
    expect(l.take("t1", "something typed instead").kept).toBe(1);
  });

  it("the same words dictated twice are taken twice, one occurrence each", () => {
    const l = new DictationLedger();
    l.add("t1", "yes");
    l.add("t1", "yes");
    expect(l.take("t1", "yes yes")).toMatchObject({ input: "", removed: ["yes", "yes"] });
  });

  it("covers a piece that has not reached the box yet (pending)", () => {
    const l = new DictationLedger();
    l.add("t1", "one");
    l.add("t1", "two");
    const r = l.take("t1", "one", "two");
    expect(r).toEqual({ input: "", removed: ["one", "two"], kept: 0 });
  });

  it("is per chat, forgets on a send, and is bounded", () => {
    const l = new DictationLedger();
    l.add("t1", "mine");
    l.add("t2", "yours");
    expect(l.take("t2", "yours")).toMatchObject({ input: "", removed: ["yours"] });
    expect(l.has("t1")).toBe(true);
    l.forget("t1");
    expect(l.has("t1")).toBe(false);
    expect(l.take("t1", "mine")).toEqual({ input: "mine", removed: [], kept: 0 });
    for (let i = 0; i < 100; i++) l.add(`k${i}`, "x");
    expect(l.has("k0")).toBe(false);
    expect(l.has("k99")).toBe(true);
    for (let i = 0; i < 200; i++) l.add("big", `p${i}`);
    expect(l.take("big", "").kept).toBeLessThanOrEqual(50);
  });

  it("ignores blank text", () => {
    const l = new DictationLedger();
    l.add("t1", "   ");
    expect(l.has("t1")).toBe(false);
  });
});

describe("UndoHold: ten seconds, one cancel, memory only", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(0); });
  afterEach(() => { vi.useRealTimers(); });

  it("gives the text back once, for the chat it came from", () => {
    const h = new UndoHold();
    h.hold("t1", "  lost words ");
    expect(h.snapshot()).toMatchObject({ key: "t1", text: "lost words" });
    expect(h.take("t2")).toBeNull();
    expect(h.take("t1")).toBe("lost words");
    expect(h.take("t1")).toBeNull();
    expect(h.snapshot()).toBeNull();
  });

  it("expires after 10 s, on the timer and on a late read", () => {
    const h = new UndoHold();
    const seen: unknown[] = [];
    h.subscribe(() => seen.push(h.snapshot()));
    h.hold("t1", "words");
    vi.advanceTimersByTime(UNDO_MS - 1);
    expect(h.snapshot()).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(h.snapshot()).toBeNull();
    expect(h.take("t1")).toBeNull();
    expect(seen).toEqual([expect.objectContaining({ text: "words" }), null]);
  });

  it("a later cancel replaces it and restarts the clock; an empty one leaves it alone", () => {
    const h = new UndoHold();
    h.hold("t1", "first");
    vi.advanceTimersByTime(6000);
    h.hold("t1", "");
    h.hold("t1", "   ");
    expect(h.snapshot()?.text).toBe("first");
    h.hold("t1", "second");
    vi.advanceTimersByTime(6000);
    expect(h.take("t1")).toBe("second");
  });
});
