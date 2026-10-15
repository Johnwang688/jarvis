import { describe, expect, it } from "vitest";
import {
  RECENT_CHATS, SHOW_MORE_KEY, byRecent, loadShowMore, moreLabel, recentChats, saveShowMore,
} from "./recentChats";
import type { Thread } from "../types";

const thread = (id: string, updated: string, over: Partial<Thread> = {}): Thread => ({
  id, project_id: "p1", role: "chat", provider: "fast", provider_session_id: null, task_id: null,
  title: id, created: "2026-01-01T00:00:00+00:00", updated, turns: 1, cost_usd: 0, tokens: 0,
  model: null, effort: null, ...over,
});

/** c1 is the oldest, c8 the newest, handed over in a scrambled order. */
const EIGHT = [4, 1, 8, 2, 7, 3, 6, 5].map((n) => thread(`c${n}`, `2026-10-0${n}T00:00:00+00:00`));
const ids = (ts: Thread[]) => ts.map((t) => t.id);

class FakeStorage {
  data: Record<string, string> = {};
  getItem(k: string) { return k in this.data ? this.data[k] : null; }
  setItem(k: string, v: string) { this.data[k] = v; }
}

describe("which chats a project shows", () => {
  it("is the five most recently modified, newest first", () => {
    const r = recentChats(EIGHT, { expanded: false });
    expect(ids(r.shown)).toEqual(["c8", "c7", "c6", "c5", "c4"]);
    expect(r.hidden).toBe(3);
    expect(r.more).toBe(true);
    expect(RECENT_CHATS).toBe(5);
  });

  it("orders by the last activity, not by creation", () => {
    const old = thread("old", "2026-10-09T00:00:00+00:00", { created: "2025-01-01T00:00:00+00:00" });
    const fresh = thread("fresh", "2026-01-01T00:00:00+00:00", { created: "2026-10-09T00:00:00+00:00" });
    expect(ids(byRecent([fresh, old]))).toEqual(["old", "fresh"]);
  });

  it("compares instants, not strings (offsets differ)", () => {
    const a = thread("a", "2026-10-02T00:30:00+01:00"); // 2026-10-01T23:30Z
    const b = thread("b", "2026-10-02T00:10:00+00:00");
    expect(ids(byRecent([a, b]))).toEqual(["b", "a"]);
  });

  it("puts an unreadable stamp last and keeps ties in the order sent", () => {
    const a = thread("a", ""), b = thread("b", "nope"), c = thread("c", "2026-10-01T00:00:00+00:00");
    const d = thread("d", "2026-10-01T00:00:00+00:00");
    expect(ids(byRecent([a, d, b, c]))).toEqual(["d", "c", "a", "b"]);
  });

  it("does not mutate what it was given", () => {
    const before = ids(EIGHT);
    recentChats(EIGHT, { expanded: false });
    expect(ids(EIGHT)).toEqual(before);
  });

  it("has no button for five or fewer", () => {
    const five = EIGHT.slice(0, 5);
    const r = recentChats(five, { expanded: false });
    expect(r.more).toBe(false);
    expect(r.hidden).toBe(0);
    expect(r.shown).toHaveLength(5);
  });

  it("reveals all of them, still newest first, when expanded", () => {
    const r = recentChats(EIGHT, { expanded: true });
    expect(ids(r.shown)).toEqual(["c8", "c7", "c6", "c5", "c4", "c3", "c2", "c1"]);
    expect(r.hidden).toBe(0);
    expect(r.more).toBe(true);
  });
});

describe("what is never hidden", () => {
  it("an old chat that is selected or open in a pane stays, in its sorted place, and is not counted", () => {
    const r = recentChats(EIGHT, { expanded: false, kept: new Set(["c1"]) });
    expect(ids(r.shown)).toEqual(["c8", "c7", "c6", "c5", "c4", "c1"]);
    expect(r.hidden).toBe(2);
  });

  it("a running turn stays", () => {
    const list = EIGHT.map((t) => (t.id === "c2" ? { ...t, running: true } : t));
    expect(ids(recentChats(list, { expanded: false }).shown)).toContain("c2");
  });

  it.each(["needs_input", "unread", "failed", "working"] as const)("%s stays", (status) => {
    const r = recentChats(EIGHT, { expanded: false, status: (id) => (id === "c3" ? status : undefined) });
    expect(ids(r.shown)).toContain("c3");
    expect(r.hidden).toBe(2);
  });

  it("idle does not keep a chat", () => {
    const r = recentChats(EIGHT, { expanded: false, status: () => "idle" });
    expect(ids(r.shown)).not.toContain("c3");
  });

  it("everything beyond the five can be kept, and then nothing is hidden", () => {
    const r = recentChats(EIGHT, { expanded: false, kept: new Set(["c1", "c2", "c3"]) });
    expect(r.shown).toHaveLength(8);
    expect(r.hidden).toBe(0);
  });
});

describe("the button's words", () => {
  it("counts what is hidden, and folds back when open", () => {
    expect(moreLabel(3, false)).toBe("Show 3 more");
    expect(moreLabel(0, true)).toBe("Show less");
  });
});

describe("remembering the toggle", () => {
  it("round-trips only the projects switched on", () => {
    const s = new FakeStorage();
    saveShowMore({ p1: true, p2: false, p3: true }, s as unknown as Storage);
    expect(JSON.parse(s.data[SHOW_MORE_KEY])).toEqual({ p1: true, p3: true });
    expect(loadShowMore(s as unknown as Storage)).toEqual({ p1: true, p3: true });
  });

  it("reads garbage as nothing remembered", () => {
    for (const raw of ["{", "[1]", "null", '"x"', '{"p1":"yes"}']) {
      const s = new FakeStorage();
      s.data[SHOW_MORE_KEY] = raw;
      expect(loadShowMore(s as unknown as Storage)).toEqual({});
    }
  });

  it("survives storage that throws", () => {
    const boom = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
    expect(loadShowMore(boom as unknown as Storage)).toEqual({});
    expect(() => saveShowMore({ p1: true }, boom as unknown as Storage)).not.toThrow();
  });
});
