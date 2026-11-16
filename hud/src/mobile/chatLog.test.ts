import { describe, expect, it } from "vitest";
import { coverage, mergeLog, type LogKind, type LogWindow } from "./chatLog";
import type { LogPage, LogRow } from "./model";

const row = (seq: number): LogRow => ({ seq, role: "Claude", text: `message ${seq}`, at: null, marks: [], cut: false });
const page = (seqs: number[], next: number, extra: Partial<LogPage> = {}): LogPage =>
  ({ rows: seqs.map(row), next, more: false, older: false, thread: null, ...extra });
const range = (a: number, b: number) => Array.from({ length: b - a }, (_, i) => a + i);
const seqs = (win: LogWindow) => win.rows.map((r) => r.seq);

describe("what a page accounts for", () => {
  it("a tail with nothing older proves everything before its cursor; with older, from its first row", () => {
    expect(coverage("tail", null, page([3, 5], 7))).toEqual({ from: 0, to: 7 });
    expect(coverage("tail", null, page([3, 5], 7, { older: true }))).toEqual({ from: 3, to: 7 });
  });

  it("a forward page proves from its cursor to its next, and never less than nothing", () => {
    expect(coverage("forward", 4, page([5], 9))).toEqual({ from: 4, to: 9 });
    expect(coverage("forward", 4, page([], 2))).toEqual({ from: 4, to: 4 });
  });
});

describe("joining a page to the window", () => {
  const win: LogWindow = { rows: range(0, 60).map(row), base: 0, next: 60 };

  it("a tail after the window replaces it rather than leaving a hole (PR 384 MED 1)", () => {
    const joined = mergeLog(win, "tail", null, page(range(120, 180), 180, { older: true }));
    expect(joined.change).toBe("reset");
    expect(joined.win).toMatchObject({ base: 120, next: 180 });
    expect(seqs(joined.win)).toEqual(range(120, 180));
  });

  it("a forward page that reaches back into a replaced window fills it from below", () => {
    const tail = mergeLog(win, "tail", null, page(range(120, 180), 180, { older: true })).win;
    const joined = mergeLog(tail, "forward", 60, page(range(60, 180), 180));
    expect(joined.change).toBe("insert");
    expect(seqs(joined.win)).toEqual(range(60, 180));
    expect(joined.win.base).toBe(60);
  });

  it("a page wholly before the window is dropped; one ending at its start is joined", () => {
    const tail = mergeLog(win, "tail", null, page(range(120, 180), 180, { older: true })).win;
    expect(mergeLog(tail, "forward", 60, page(range(60, 100), 100, { more: true })).change).toBe("dropped");
    const touching = mergeLog(tail, "forward", 60, page(range(60, 120), 120, { more: true }));
    expect(seqs(touching.win)).toEqual(range(60, 180));
  });

  it("inside the window nothing is news: a repeat is not drawn twice and an older snapshot erases nothing", () => {
    const sparse: LogWindow = { rows: [row(9)], base: 0, next: 10 };
    const older = mergeLog(sparse, "tail", null, page([1], 2));
    expect(older.change).toBe("none");
    expect(older.win).toEqual(sparse);
    const repeat = mergeLog(sparse, "forward", 5, page([9, 12], 13));
    expect(repeat.added.map((r) => r.seq)).toEqual([12]);
    expect(seqs(repeat.win)).toEqual([9, 12]);
  });

  it("a forward page entirely after the cursor is dropped (it cannot be asked for, so it proves nothing)", () => {
    expect(mergeLog(win, "forward", 61, page([61], 62)).change).toBe("dropped");
  });

  it("rows before a forward page's cursor are not taken from it", () => {
    const joined = mergeLog(win, "forward", 60, page([10, 61], 62));
    expect(joined.added.map((r) => r.seq)).toEqual([61]);
    expect(seqs(joined.win)).toEqual(range(0, 60).concat(61));
  });
});

// A model of jarvis/v2/remote_api.py `log`, small enough to page often.
const TAIL = 5, PAGE = 3;
function serve(shown: boolean[], after: number | null): LogPage {
  let idx = shown.flatMap((s, i) => (s ? [i] : []));
  let older = false;
  if (after === null) { older = idx.length > TAIL; idx = idx.slice(-TAIL); } else idx = idx.filter((i) => i >= after);
  const out = idx.slice(0, PAGE);
  const more = idx.length > PAGE;
  const next = more ? out[out.length - 1] + 1 : Math.max(shown.length, after ?? 0);
  return page(out, next, { more, older });
}
function rand(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe("any order of true pages keeps one contiguous window (property)", () => {
  it.each(range(0, 300))("seed %i", (seed) => {
    const r = rand(seed);
    const shown: boolean[] = [];
    const grow = () => { for (let n = Math.floor(r() * 6); n > 0; n--) shown.push(r() < 0.7); };
    grow();
    // Pages taken at different moments, applied in a shuffled order, as racing responses arrive.
    const pages: { kind: LogKind; after: number | null; page: LogPage }[] = [];
    let win: LogWindow | null = null;
    for (let step = 0; step < 40; step++) {
      if (r() < 0.4) grow();
      if (r() < 0.5 || !pages.length) {
        const kind: LogKind = r() < 0.4 ? "tail" : "forward";
        const after = kind === "tail" ? null : Math.floor(r() * (shown.length + 1));
        pages.push({ kind, after, page: serve(shown, after) });
      } else {
        const [taken] = pages.splice(Math.floor(r() * pages.length), 1);
        const before = win;
        win = mergeLog(win, taken.kind, taken.after, taken.page).win;
        expect(win.next, "the cursor never moves back").toBeGreaterThanOrEqual(before?.next ?? 0);
        const truth = range(win.base, win.next).filter((i) => shown[i]);
        expect(seqs(win), "every seq in [base, next) is a row or absent: no hole, no repeat").toEqual(truth);
      }
    }
  });
});
