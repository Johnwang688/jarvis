import { describe, expect, it, vi } from "vitest";
import { activePane, compatActions, flatState, initialState, reduce, type State } from "./store";
import { emptyChat, emptyChats, runningHere, type ChatState } from "../lib/chats";
import type { PaneNo } from "../lib/workspace";
import type { Thread } from "../types";

const thread = (id: string, over: Partial<Thread> = {}): Thread => ({
  id, project_id: "p1", role: "chat", provider: "fast", provider_session_id: null, task_id: null,
  title: id, created: "", updated: "", turns: 0, cost_usd: 0, tokens: 0, model: null, effort: null,
  ...over,
});

describe("thread_patch", () => {
  it("merges into the threads as they are when it lands, not when the change was asked for", () => {
    // A PATCH's reply arrives after another update (a usage tick, a rename,
    // an SSE thread_updated) changed the list; writing back a list captured
    // at click time would undo that update.
    const atClick: State = { ...initialState, threads: [thread("a"), thread("b")] };
    const meanwhile = reduce(atClick, {
      type: "patch",
      patch: { threads: atClick.threads.map((t) => (t.id === "b" ? { ...t, title: "renamed", turns: 3 } : t)) },
    });
    const after = reduce(meanwhile, { type: "thread_patch", id: "a", patch: { model: "x/y", effort: "low" } });
    expect(after.threads.find((t) => t.id === "a")).toMatchObject({ model: "x/y", effort: "low" });
    expect(after.threads.find((t) => t.id === "b")).toMatchObject({ title: "renamed", turns: 3 });
  });

  it("keeps the id, and ignores a thread the list no longer holds", () => {
    const s: State = { ...initialState, threads: [thread("a")] };
    expect(reduce(s, { type: "thread_patch", id: "a", patch: { id: "zz", model: "m" } }).threads[0].id).toBe("a");
    expect(reduce(s, { type: "thread_patch", id: "gone", patch: { model: "m" } })).toBe(s);
  });
});

/** A state with some panes' conversations set. */
const withChats = (chats: Partial<Record<PaneNo, Partial<ChatState>>>, over: Partial<State> = {}): State => {
  const all = emptyChats();
  for (const [n, c] of Object.entries(chats)) all[Number(n) as PaneNo] = { ...all[Number(n) as PaneNo], ...c };
  return { ...initialState, chats: all, ...over };
};

describe("steering marks (2026-10-08)", () => {
  const sent = withChats({
    1: {
      messages: [
        { role: "user", text: "first" },
        { role: "assistant", text: "working on it" },
        { role: "user", text: "do it in rust instead", local: "local-1", mark: "steering" },
      ],
    },
  });

  it("marks this window's message by its local id, then by the daemon's id", () => {
    const queued = reduce(sent, { type: "mark", local: "local-1", patch: { mark: "queued", message_id: "m1" } });
    expect(queued.chats[1].messages[2]).toMatchObject({ mark: "queued", message_id: "m1", text: "do it in rust instead" });
    const ran = reduce(queued, { type: "mark", message_id: "m1", patch: { mark: undefined } });
    expect(ran.chats[1].messages[2].mark).toBeUndefined();
    expect(reduce(queued, { type: "mark", message_id: "nope", patch: { mark: "not sent" } })).toBe(queued);
  });

  it("takes back only the failed message, never a reply that settled meanwhile", () => {
    const settled = reduce(sent, { type: "settle", pane: 1, text: "a reply that landed during the send" });
    const after = reduce(settled, { type: "unmessage", local: "local-1" });
    expect(after.chats[1].messages.map((m) => m.text)).toEqual([
      "first", "working on it", "a reply that landed during the send",
    ]);
  });

  it("finds a message in whichever pane holds it", () => {
    const two = withChats({ 2: { messages: [{ role: "user", text: "in pane two", local: "local-7" }] } });
    const marked = reduce(two, { type: "mark", local: "local-7", patch: { mark: "queued" } });
    expect(marked.chats[2].messages[0].mark).toBe("queued");
    expect(marked.chats[1]).toBe(two.chats[1]);
    expect(reduce(two, { type: "unmessage", local: "local-7" }).chats[2].messages).toEqual([]);
  });
});

describe("several chats: each pane its own conversation (WP-B)", () => {
  it("draws a delta, a settled reply and a tool only in the pane they name", () => {
    let s = withChats({ 1: { busy: true, threadId: "a" }, 2: { busy: true, threadId: "b" } });
    s = reduce(s, { type: "delta", pane: 2, text: "hel" });
    s = reduce(s, { type: "delta", pane: 2, text: "lo" });
    expect([s.chats[1].draft, s.chats[2].draft]).toEqual(["", "hello"]);
    s = reduce(s, { type: "op_start", pane: 2, op: { call_id: "c1", name: "grep", started: 1 } });
    expect([s.chats[1].ops.length, s.chats[2].ops.length]).toEqual([0, 1]);
    s = reduce(s, { type: "settle", pane: 2, text: "hello" });
    expect(s.chats[2].messages.map((m) => m.text)).toEqual(["", "hello"]);
    expect(s.chats[2].messages[0].calls?.[0].name).toBe("grep");
    expect(s.chats[1].messages).toEqual([]);
    expect(s.chats[2].draft).toBe("");
  });

  it("lets only the selected chat's turn move the orb", () => {
    let s = withChats({ 1: { busy: true }, 2: { busy: true } }, { selectedChat: 1, orb: "thinking" });
    s = reduce(s, { type: "op_start", pane: 2, op: { call_id: "c", name: "t", started: 1 } });
    expect(s.orb).toBe("thinking");
    s = reduce(s, { type: "chat", pane: 2, patch: { orb: "error", status: "FAILED" } });
    expect(s.orb).toBe("thinking");
    expect(s.chats[2].status).toBe("FAILED");
    s = reduce(s, { type: "op_start", pane: 1, op: { call_id: "d", name: "t", started: 1 } });
    expect(s.orb).toBe("tool");
    s = reduce(s, { type: "op_done", pane: 1, call_id: "d", ok: true, summary: "" });
    expect(s.orb).toBe("thinking");
    s = reduce(s, { type: "chat", pane: 1, patch: { orb: "idle", busy: false } });
    expect(s.orb).toBe("idle");
  });

  it("writes a pane patch's error to the window's error line, whichever pane", () => {
    const s = reduce(withChats({}, { selectedChat: 1 }), { type: "chat", pane: 3, patch: { error: "Could not send: x" } });
    expect(s.error).toBe("Could not send: x");
    expect((s.chats[3] as any).error).toBeUndefined();
  });

  it("repaints the orb from the newly selected chat's turn, but not over the microphone", () => {
    const s = withChats({ 1: { busy: true }, 2: { busy: false } }, { selectedChat: 1, orb: "thinking" });
    expect(reduce(s, { type: "select", pane: 2 }).orb).toBe("idle");
    const tooling = withChats({ 2: { busy: true, ops: [{ call_id: "c", name: "t", started: 1 }] } }, { selectedChat: 1 });
    expect(reduce(tooling, { type: "select", pane: 2 }).orb).toBe("tool");
    expect(reduce({ ...s, orb: "listening" }, { type: "select", pane: 2 }).orb).toBe("listening");
    expect(reduce({ ...s, orb: "transcribing" }, { type: "select", pane: 2 }).orb).toBe("transcribing");
    const asking = { ...s, approvals: [{ req_id: "r" } as any], orb: "approval" as const };
    expect(reduce(asking, { type: "select", pane: 2 }).orb).toBe("approval");
    expect(reduce(s, { type: "select", pane: 1 })).toBe(s);
  });

  it("drops an answered card back to the selected chat's turn", () => {
    const s = withChats({ 1: { busy: false }, 2: { busy: true } },
                        { selectedChat: 2, orb: "approval", approvals: [{ req_id: "r" } as any] });
    expect(reduce(s, { type: "approval_drop", req_id: "r" }).orb).toBe("thinking");
    expect(reduce({ ...s, selectedChat: 1 }, { type: "approval_drop", req_id: "r" }).orb).toBe("idle");
  });

  it("AS: a summary patches only its own public card's two fields", () => {
    const s = withChats({}, { approvals: [{ req_id: "r", command: "make", summary_state: "pending" } as any,
                                          { req_id: "p", hud_only: true } as any] });
    const patched = reduce(s, { type: "approval_patch", req_id: "r", patch: { summary: "Builds.", summary_state: "ready" } });
    expect(patched.approvals[0]).toEqual({ req_id: "r", command: "make", summary: "Builds.", summary_state: "ready" });
    expect(reduce(s, { type: "approval_patch", req_id: "gone", patch: { summary: "x", summary_state: "ready" } })).toBe(s);
    const priv = reduce(s, { type: "approval_patch", req_id: "p", patch: { summary: "x", summary_state: "ready" } });
    expect(priv.approvals[1]).toEqual({ req_id: "p", hud_only: true });
  });

  it("AS (review of #330, LOW 3): a re-read list never overwrites a summary already heard; it drops what was answered", () => {
    const s = withChats({}, { orb: "approval", approvals: [
      { req_id: "r", command: "make", summary: "Builds.", summary_state: "ready" } as any,
      { req_id: "gone", command: "x", summary_state: "pending" } as any] });
    const listed = [{ req_id: "r", command: "make", summary: "", summary_state: "pending" } as any,
                    { req_id: "new", command: "y", summary_state: "pending" } as any];
    const next = reduce(s, { type: "approvals_sync", approvals: listed });
    expect(next.approvals.map((r) => r.req_id)).toEqual(["r", "new"]);
    expect(next.approvals[0]).toMatchObject({ summary: "Builds.", summary_state: "ready" });
    expect(next.approvals[1].summary_state).toBe("pending");
    // The list's own word wins when it is past pending; an empty list clears the orb.
    const failed = reduce(s, { type: "approvals_sync", approvals: [{ req_id: "r", command: "make", summary_state: "failed" } as any] });
    expect(failed.approvals[0].summary_state).toBe("failed");
    expect(reduce(s, { type: "approvals_sync", approvals: [] }).orb).toBe("idle");
  });

  it("hands words back to one pane's box and takes them once", () => {
    let s = reduce(withChats({}), { type: "give_back", pane: 2, text: "again", files: [], nonce: 5 });
    expect(s.chats[2].restore.map((g) => g.text)).toEqual(["again"]);
    expect(s.chats[1].restore).toEqual([]);
    s = reduce(s, { type: "given_back", pane: 2, nonce: 5 });
    expect(s.chats[2].restore).toEqual([]);
  });

  it("opens a thread in a pane, bringing along a turn another pane started and then left", () => {
    const s = withChats({
      1: { threadId: "b", busy: true, turnThreadId: "a", status: "RUNNING · grep" },
      2: { compose: { projectId: "p1" } },
    });
    const after = reduce(s, { type: "open_thread", pane: 2, threadId: "a" });
    expect(after.chats[2]).toMatchObject({ threadId: "a", compose: null, busy: true, turnThreadId: "a",
                                           status: "RUNNING · grep" });
    expect(after.chats[1]).toMatchObject({ threadId: "b", busy: false, turnThreadId: null, status: "" });
  });

  it("never takes a turn from a pane still showing it", () => {
    const showing = withChats({ 1: { threadId: "a", busy: true, turnThreadId: "a" } });
    expect(reduce(showing, { type: "open_thread", pane: 2, threadId: "a" }).chats[1].busy).toBe(true);
  });

  it("trades turns with a pane waiting on its own, so the running thread on screen has its Stop", () => {
    // Review of PR #27: pane 2 kept its own turn (c) and t1's ran with no Stop anywhere.
    const own = withChats({
      1: { threadId: "b", busy: true, turnThreadId: "a", status: "RUNNING · a" },
      2: { threadId: "c", busy: true, turnThreadId: "c", status: "THINKING" },
    });
    const after = reduce(own, { type: "open_thread", pane: 2, threadId: "a" });
    expect(after.chats[2]).toMatchObject({ threadId: "a", turnThreadId: "a", busy: true, status: "RUNNING · a" });
    expect(after.chats[1]).toMatchObject({ threadId: "b", busy: true, turnThreadId: "c", status: "THINKING" });
    expect(runningHere(after.chats[2])).toBe(true);
  });

  it("swaps two panes' conversations whole", () => {
    const s = withChats({ 1: { threadId: "a", draft: "x" }, 3: { threadId: "b", busy: true } });
    const after = reduce(s, { type: "chat_swap", a: 1, b: 3 });
    expect([after.chats[1].threadId, after.chats[3].threadId]).toEqual(["b", "a"]);
    expect(after.chats[1].busy).toBe(true);
    expect(after.chats[3].draft).toBe("x");
    expect(reduce(s, { type: "chat_swap", a: 2, b: 2 })).toBe(s);
  });
});

describe("a draft belongs to its conversation (review of PR #27)", () => {
  it("is parked when a pane opens another thread, and comes back when it returns", () => {
    let s = withChats({ 1: { threadId: "a", input: "half typed", files: [{ name: "f", mime: "text/plain", data_b64: "" }] } });
    s = reduce(s, { type: "open_thread", pane: 1, threadId: "b" });
    expect([s.chats[1].input, s.chats[1].files.length]).toEqual(["", 0]);
    expect(s.drafts.a.text).toBe("half typed");
    s = reduce(s, { type: "chat", pane: 1, patch: { threadId: "a" } });
    expect([s.chats[1].input, s.chats[1].files.length]).toEqual(["half typed", 1]);
    expect(s.drafts.a).toBeUndefined();
  });

  it("moves with the conversation when two panes trade", () => {
    const s = reduce(withChats({ 1: { threadId: "a" }, 2: { threadId: "b", input: "for b" } }),
                     { type: "chat_swap", a: 1, b: 2 });
    expect([s.chats[1].threadId, s.chats[1].input, s.chats[2].input]).toEqual(["b", "for b", ""]);
  });

  it("stays in the box while a compose row becomes the thread it opened, or New thread replaces it", () => {
    let s = withChats({ 1: { compose: { projectId: "p", openedId: "t9" }, input: "next words" } });
    s = reduce(s, { type: "chat", pane: 1, patch: { threadId: "t9", compose: null } });
    expect(s.chats[1].input).toBe("next words");
    s = withChats({ 1: { compose: { projectId: "p" }, input: "kept" } });
    s = reduce(s, { type: "chat", pane: 1, patch: { compose: { projectId: "q" } } });
    expect(s.chats[1].input).toBe("kept");
  });

  it("parks words for a compose row no pane shows with its draft", () => {
    let s = reduce(withChats({}), { type: "park", key: "compose:2", text: "spoken" });
    s = reduce(s, { type: "park", key: "compose:2", text: "again" });
    expect(s.drafts["compose:2"].text).toBe("spoken\nagain");
  });

  it("stages files read in for a conversation where that conversation is when they are ready (re-review of PR #27)", () => {
    const f = (name: string) => ({ name, mime: "text/plain", data_b64: "" });
    // Dropped on pane 1 while it showed t1; pane 1 opened t2 and pane 2 took t1 meanwhile.
    let s = withChats({ 1: { threadId: "t2", files: [f("t2.txt")] }, 2: { threadId: "t1" } });
    s = reduce(s, { type: "stage", key: "t1", files: [f("a.txt")] });
    expect(s.chats[1].files.map((x) => x.name)).toEqual(["t2.txt"]);
    expect(s.chats[2].files.map((x) => x.name)).toEqual(["a.txt"]);
    // No pane holds it: kept with its parked draft, and added to, never replaced.
    s = reduce(s, { type: "stage", key: "t9", files: [f("b.txt")] });
    s = reduce(s, { type: "stage", key: "t9", files: [f("c.txt")] });
    expect(s.drafts["t9"].files.map((x) => x.name)).toEqual(["b.txt", "c.txt"]);
    // Never past the per-turn cap.
    s = reduce(s, { type: "stage", key: "t1", files: Array.from({ length: 12 }, (_, i) => f(`${i}`)) });
    expect(s.chats[2].files.length).toBe(8);
  });

  it("stages a capture pair atomically into its pinned conversation or parked draft", () => {
    const image = { name: "page.png", mime: "image/png", data_b64: "image" };
    const reference = { name: "page text", mime: "application/x-jarvis-page-capture", data_b64: "a".repeat(32) };
    const pair = [image, reference];
    let s = withChats({ 1: { threadId: "new", input: "new words" }, 2: { threadId: "original", input: "kept" } });
    s = reduce(s, { type: "stage", key: "original", files: pair, atomic: true });
    expect(s.chats[1].files).toEqual([]);
    expect(s.chats[2].files).toEqual(pair);
    expect(s.chats[2].input).toBe("kept");
    s = reduce(s, { type: "stage", key: "closed-thread", files: pair, atomic: true });
    expect(s.drafts["closed-thread"].files).toEqual(pair);
  });

  it("refuses the whole capture pair if capacity changed while export was pending", () => {
    const f = (name: string) => ({ name, mime: "text/plain", data_b64: "" });
    for (const count of [7, 8]) {
      const had = Array.from({ length: count }, (_, i) => f(String(i)));
      let s = withChats({ 1: { threadId: "t1", input: "words", files: had } });
      s = reduce(s, { type: "stage", key: "t1", files: [f("page.png"), f("page text")], atomic: true });
      expect(s.chats[1].files).toEqual(had);
      expect(s.chats[1].input).toBe("words");
      expect(s.error).toContain("two attachment slots");
      const parked = reduce({ ...withChats({}), drafts: { t1: { text: "parked", files: had } } },
        { type: "stage", key: "t1", files: [f("page.png"), f("page text")], atomic: true });
      expect(parked.drafts.t1.files).toEqual(had);
      expect(parked.drafts.t1.text).toBe("parked");
    }
    const had = Array.from({ length: 6 }, (_, i) => f(String(i)));
    const s = reduce(withChats({ 1: { threadId: "t1", files: had } }),
      { type: "stage", key: "t1", files: [f("page.png"), f("page text")], atomic: true });
    expect(s.chats[1].files.length).toBe(8);
  });

  it("parks a failed new-thread send's files with its words, and the next compose row there gets both (re-review of PR #27)", () => {
    const f = { name: "f.txt", mime: "text/plain", data_b64: "" };
    let s = reduce(withChats({ 1: { threadId: "t2" } }), { type: "park", key: "compose:1", text: "lost words", files: [f] });
    expect(s.chats[1].input).toBe("");
    s = reduce(s, { type: "chat", pane: 1, patch: { threadId: null, compose: { projectId: "p" } } });
    expect([s.chats[1].input, s.chats[1].files.length]).toEqual(["lost words", 1]);
    expect(s.drafts["compose:1"]).toBeUndefined();
  });
});

describe("a transcript belongs to its thread (review of PR #27)", () => {
  it("is already loaded when a compose row that opened the thread becomes it", () => {
    const s = reduce(withChats({ 1: { compose: { projectId: "p", openedId: "t9" }, messages: [{ role: "user", text: "first" }] } }),
                     { type: "open_thread", pane: 1, threadId: "t9" });
    expect(s.chats[1].loadedThread).toBe("t9");
    expect(s.chats[1].messages.length).toBe(1);
  });

  it("is still to load for a pane that opens another thread", () => {
    const s = reduce(withChats({ 1: { threadId: "a", loadedThread: "a" } }), { type: "open_thread", pane: 1, threadId: "b" });
    expect(s.chats[1].loadedThread).toBe("a");
  });
});

describe("what the microphone says follows the selected chat (review of PR #27)", () => {
  it("moves a capture status off the pane that stops being the target", () => {
    const s = reduce(withChats({ 1: { status: "LISTENING · SPEAK NOW" } }, { selectedChat: 1 }), { type: "select", pane: 2 });
    expect([s.chats[1].status, s.chats[2].status]).toEqual(["", "LISTENING · SPEAK NOW"]);
  });

  it("leaves a turn's status alone, and never overwrites the new target's own", () => {
    const busy = reduce(withChats({ 1: { status: "RUNNING · grep" } }, { selectedChat: 1 }), { type: "select", pane: 2 });
    expect([busy.chats[1].status, busy.chats[2].status]).toEqual(["RUNNING · grep", ""]);
    const own = reduce(withChats({ 1: { status: "MIC MUTED" }, 2: { status: "THINKING" } }, { selectedChat: 1 }),
                       { type: "select", pane: 2 });
    expect([own.chats[1].status, own.chats[2].status]).toEqual(["", "THINKING"]);
  });
});

describe("the test hook's view of the store", () => {
  it("lays the selected chat's conversation over the window's fields", () => {
    const s = withChats({ 1: { threadId: "a" }, 2: { threadId: "b", busy: true } }, { selectedChat: 2 });
    const flat = flatState(s);
    expect([flat.threadId, flat.busy]).toEqual(["b", true]);
    expect(flat.chats[1].threadId).toBe("a");
  });

  it("sends a legacy patch's conversation fields to the selected chat's pane, the rest to the window", () => {
    const acts = compatActions({ type: "patch", patch: { threadId: "kt9", compose: null, error: "" } }, 3);
    expect(acts).toEqual([
      { type: "chat", pane: 3, patch: { threadId: "kt9", compose: null } },
      { type: "patch", patch: { error: "" } },
    ]);
    expect(compatActions({ type: "patch", patch: {} }, 1)).toEqual([{ type: "patch", patch: {} }]);
    const other = { type: "approval_drop", req_id: "r" };
    expect(compatActions(other, 1)).toEqual([other]);
    let s = withChats({}, { selectedChat: 3 });
    for (const a of acts) s = reduce(s, a);
    expect(s.chats[3].threadId).toBe("kt9");
    expect(s.chats[1].threadId).toBeNull();
  });
});

describe("each thread's turn, for the thinking line (WP-H, plan §4 H-1)", () => {
  const T0 = 1_000_000;
  const act = (id: string, status: string) => ({ type: "activity" as const, at: T0 + 500,
    record: { kind: "activity", data: { of: "thread", id, status } } });
  const run = (s: State, ...actions: any[]): State => actions.reduce((x, a) => reduce(x, a), s);
  const send = (id: string, by = "local-1", steer = false) =>
    ({ type: "turn" as const, event: "send" as const, threadId: id, at: T0, by, steer });
  const started = (id: string, at = T0 + 100) => ({ type: "turn" as const, event: "started" as const, threadId: id, at });
  const finished = (id: string, stop = "end", next = 0) =>
    ({ type: "turn" as const, event: "finished" as const, threadId: id, at: T0 + 900, stop, next });
  const error = (id: string) => ({ type: "turn" as const, event: "error" as const, threadId: id, at: T0 + 300 });

  it("a send with a thread id: tracked from the owner's Enter, no error", () => {
    const s = run(initialState, send("t1"));
    expect(s.turns.t1).toMatchObject({ since: T0, error: "", by: "local-1" });
  });

  it("a send clears an ended error (the next send clears the static red line)", () => {
    const s = run(initialState, send("t1"), started("t1"), error("t1"), finished("t1", "error"), send("t1", "local-2"));
    expect(s.turns.t1).toMatchObject({ since: T0, error: "" });
  });

  it("a steer leaves the running turn's time and error alone", () => {
    const s = run(initialState, started("t1"), error("t1"), send("t1", "local-2", true));
    expect(s.turns.t1).toMatchObject({ since: T0 + 100, error: "during" });
  });

  it("a send into a turn the window knows runs (a Discord turn the pane has not adopted) joins it", () => {
    const s = run(initialState, started("t1"), send("t1", "local-2", false));
    expect(s.turns.t1).toMatchObject({ since: T0 + 100, error: "" });
    expect(s.turns.t1.by).toBeUndefined();
  });

  it("turn_started: a new entry, unless the send that asked for it made one", () => {
    expect(run(initialState, started("t1")).turns.t1).toMatchObject({ since: T0 + 100, error: "" });
    const kept = run(initialState, send("t1"), started("t1"));
    expect(kept.turns.t1).toMatchObject({ since: T0, error: "" });
    expect(kept.turns.t1.by).toBeUndefined();
  });

  it("turn_started replaces an ended entry", () => {
    const s = run(initialState, started("t1"), finished("t1", "error"), started("t1", T0 + 2000));
    expect(s.turns.t1).toMatchObject({ since: T0 + 2000, error: "" });
  });

  it("activity working or needs_input with no entry: a turn noticed late, from running_since", () => {
    const threads = [thread("t1", { running: true, running_since: new Date(T0 - 42_000).toISOString() }), thread("t2")];
    const s = run({ ...initialState, threads }, act("t1", "working"), act("t2", "needs_input"));
    expect(s.turns.t1).toMatchObject({ since: T0 - 42_000, error: "" });
    expect(s.turns.t1.late).toBeUndefined();
    // No running_since (an older daemon, or the records not in yet): from now, corrected later.
    expect(s.turns.t2).toMatchObject({ since: T0 + 500, late: true });
  });

  it("…and the thread records' running_since corrects an entry noticed late", () => {
    let s = run(initialState, act("t2", "working"));
    expect(s.turns.t2.since).toBe(T0 + 500);
    s = reduce(s, { type: "patch", patch: { threads: [thread("t2", { running: true,
      running_since: new Date(T0 - 7000).toISOString() })] } });
    expect(s.turns.t2).toMatchObject({ since: T0 - 7000 });
    expect(s.turns.t2.late).toBeUndefined();
  });

  it("the GET /activity snapshot counts too: a thread turned working, a reload's running turn", () => {
    const threads = [thread("t1", { running: true, running_since: new Date(T0 - 9000).toISOString() })];
    const s = reduce({ ...initialState, threads }, { type: "patch", patch: { activity: { threads: { t1: "working" }, tasks: {} } } });
    expect(s.turns.t1).toMatchObject({ since: T0 - 9000, error: "" });
  });

  it("only a change counts: a view still saying working never revives a turn whose finish was heard", () => {
    let s = run(initialState, act("t1", "working"), started("t1"));
    s = run(s, finished("t1"));
    expect(s.turns.t1).toBeUndefined();
    // Another thread's record, or the same snapshot again, while t1 still reads working.
    s = run(s, act("t9", "unread"));
    s = reduce(s, { type: "patch", patch: { activity: { ...s.activity } } });
    expect(s.turns.t1).toBeUndefined();
  });

  it("an existing entry is left alone by working/needs_input (its time stands)", () => {
    const s = run(initialState, started("t1"), act("t1", "working"), act("t1", "needs_input"), act("t1", "working"));
    expect(s.turns.t1).toMatchObject({ since: T0 + 100, error: "" });
  });

  it("error: the running turn's entry says so", () => {
    expect(run(initialState, started("t1"), error("t1")).turns.t1).toMatchObject({ since: T0 + 100, error: "during" });
    // An error before any turn event this window heard: no start time to show.
    expect(run(initialState, error("t1")).turns.t1).toMatchObject({ since: null, error: "during" });
  });

  it("turn_finished: an error ends it as 'ended', kept", () => {
    const byStop = run(initialState, started("t1"), finished("t1", "error"));
    expect(byStop.turns.t1).toMatchObject({ error: "ended", since: T0 + 100 });
    const bySaid = run(initialState, started("t1"), error("t1"), finished("t1", "interrupted"));
    expect(bySaid.turns.t1).toMatchObject({ error: "ended" });
    // Unheard of until it ended on an error: still drawn as ended.
    expect(run(initialState, finished("t1", "error")).turns.t1).toMatchObject({ error: "ended", since: null });
  });

  it("turn_finished otherwise deletes it", () => {
    expect(run(initialState, started("t1"), finished("t1", "end")).turns.t1).toBeUndefined();
    expect(run(initialState, started("t1"), finished("t1", "interrupted")).turns.t1).toBeUndefined();
    expect(run(initialState, finished("t1", "end")).turns).toEqual({});
  });

  it("turn_finished with a message waiting (`next`): the next turn starts now", () => {
    const s = run(initialState, started("t1"), error("t1"), finished("t1", "error", 1));
    expect(s.turns.t1).toMatchObject({ since: T0 + 900, error: "" });
  });

  it("idle, unread and failed records never delete an ended error", () => {
    let s = run(initialState, started("t1"), finished("t1", "error"));
    s = run(s, act("t1", "failed"), act("t1", "idle"), act("t1", "unread"), act("t1", "idle"));
    expect(s.turns.t1).toMatchObject({ error: "ended" });
  });

  it("a later turn that ends well clears it, even one whose start was missed", () => {
    const s = run(initialState, started("t1"), finished("t1", "error"), finished("t1", "end"));
    expect(s.turns.t1).toBeUndefined();
  });

  it("activity idle or unread with a live entry: a missed finish — the entry goes", () => {
    for (const status of ["idle", "unread"]) {
      const s = run(initialState, act("t1", "working"), act("t1", status));
      expect(s.turns.t1, status).toBeUndefined();
    }
  });

  it("activity failed with a live entry: the missed finish was an error", () => {
    const s = run(initialState, act("t1", "working"), act("t1", "failed"));
    expect(s.turns.t1).toMatchObject({ error: "ended" });
  });

  it("a stale idle record (opening the thread) never takes the entry a send in flight made", () => {
    const s = run(withChats({}, { activity: { threads: { t1: "unread" }, tasks: {} } }), send("t1"), act("t1", "idle"));
    expect(s.turns.t1).toMatchObject({ since: T0, by: "local-1" });
  });

  it("a failed send takes back only the entry it made", () => {
    expect(run(initialState, send("t1"), { type: "turn", event: "send_failed", threadId: "t1", by: "local-1" })
      .turns.t1).toBeUndefined();
    const confirmed = run(initialState, send("t1"), started("t1"),
                          { type: "turn", event: "send_failed", threadId: "t1", by: "local-1" });
    expect(confirmed.turns.t1).toBeDefined();
    const other = run(initialState, send("t1", "local-2"), { type: "turn", event: "send_failed", threadId: "t1", by: "local-1" });
    expect(other.turns.t1).toBeDefined();
  });

  it("a send the running turn took (steered, queued) is an ordinary entry from then on", () => {
    const s = run(initialState, send("t1"), { type: "turn", event: "sent", threadId: "t1", by: "local-1" }, act("t1", "working"),
                  act("t1", "idle"));
    expect(s.turns.t1).toBeUndefined();
  });

  it("is keyed by thread, whichever pane shows it, and leaves every other thread alone", () => {
    const s = run(withChats({ 1: { threadId: "t1" } }), started("t1"), started("t3"), finished("t1"));
    expect(Object.keys(s.turns)).toEqual(["t3"]);
  });
});

describe("a compose row's identity (review of PR #30)", () => {
  it("is minted for every new row, in the same project too, and kept by a row spread from it", () => {
    let s = reduce(initialState, { type: "chat", pane: 1, patch: { compose: { projectId: "p1" } } });
    const first = s.chats[1].compose!.id;
    expect(first).toBeTruthy();
    // A change to the same row (the chips, a send) spreads it: the same row.
    s = reduce(s, { type: "chat", pane: 1, patch: { compose: { ...s.chats[1].compose!, effort: "low" } } });
    expect(s.chats[1].compose!.id).toBe(first);
    // New thread, or a re-aim when a project goes: a fresh row, even in the same project.
    s = reduce(s, { type: "chat", pane: 1, patch: { compose: { projectId: "p1" } } });
    const second = s.chats[1].compose!.id;
    expect(second).toBeTruthy();
    expect(second).not.toBe(first);
    // Another pane's row is another row.
    s = reduce(s, { type: "chat", pane: 2, patch: { compose: { projectId: "p1" } } });
    expect(new Set([first, second, s.chats[2].compose!.id]).size).toBe(3);
  });

  it("travels with its conversation when two panes trade them", () => {
    let s = reduce(initialState, { type: "chat", pane: 1, patch: { compose: { projectId: "p1" } } });
    const id = s.chats[1].compose!.id;
    s = reduce(s, { type: "chat", pane: 2, patch: { threadId: "t1" } });
    s = reduce(s, { type: "chat_swap", a: 1, b: 2 });
    expect(s.chats[2].compose?.id).toBe(id);
  });
});

describe("× on a chat pane: release_chat (WP-G2, plan §3.6)", () => {
  it("parks the box's words and files under the thread; they come back when it opens in another pane", () => {
    const file = { name: "a.txt", mime: "text/plain", data_b64: "YQ==" } as any;
    let s = withChats({ 2: { threadId: "t1", input: "half a thought", files: [file] } }, { selectedChat: 2 });
    s = reduce(s, { type: "release_chat", pane: 2 });
    expect(s.chats[2]).toEqual(emptyChat());
    expect(s.drafts.t1).toEqual({ text: "half a thought", files: [file] });
    s = reduce(s, { type: "chat", pane: 3, patch: { compose: { projectId: "p1" } } });
    s = reduce(s, { type: "open_thread", pane: 3, threadId: "t1" });
    expect([s.chats[3].input, s.chats[3].files]).toEqual(["half a thought", [file]]);
    expect(s.drafts.t1).toBeUndefined();
  });

  it("adds to words already parked for that thread rather than replacing them", () => {
    let s = withChats({ 2: { threadId: "t1", input: "second" } }, { drafts: { t1: { text: "first", files: [] } } });
    s = reduce(s, { type: "release_chat", pane: 2 });
    expect(s.drafts.t1.text).toBe("first\nsecond");
  });

  it("drops a compose row's words (× asked first, G-9) and never parks them for the id's next compose row", () => {
    let s = withChats({ 4: { compose: { projectId: "p1", id: "c9" }, input: "never sent" } });
    s = reduce(s, { type: "release_chat", pane: 4 });
    expect(s.drafts).toEqual({});
    s = reduce(s, { type: "chat", pane: 4, patch: { compose: { projectId: "p1" } } });
    expect(s.chats[4].input).toBe("");
  });

  it("takes the pane out of the selection order, so an id a split reuses inherits no selection", () => {
    let s = withChats({ 1: { threadId: "a" }, 2: { threadId: "b" } }, { selectedChat: 2, selectedOrder: [2, 1] });
    s = reduce(s, { type: "release_chat", pane: 2 });
    expect(s.selectedOrder).toEqual([1]);
  });

  it("keeps the microphone's status for the next selected chat to take, and nothing else", () => {
    let s = withChats({ 2: { threadId: "b", status: "LISTENING · SPEAK NOW" } }, { selectedChat: 2, selectedOrder: [2, 1] });
    s = reduce(s, { type: "release_chat", pane: 2 });
    s = reduce(s, { type: "select", pane: 1 });
    expect([s.chats[1].status, s.chats[2].status]).toEqual(["LISTENING · SPEAK NOW", ""]);
    const busy = reduce(withChats({ 2: { threadId: "b", status: "RUNNING · ls" } }, { selectedChat: 2 }),
                        { type: "release_chat", pane: 2 });
    expect(busy.chats[2].status).toBe("");
  });

  it("leaves a running turn loose; the pane that opens its thread next takes it up, Stop and all", () => {
    let s = withChats({ 2: { threadId: "t1", busy: true, turnThreadId: "t1", status: "RUNNING · pytest" } });
    s = reduce(s, { type: "release_chat", pane: 2 });
    expect(s.loose).toEqual({ t1: "RUNNING · pytest" });
    s = reduce(s, { type: "open_thread", pane: 5, threadId: "t1" });
    expect(s.chats[5]).toMatchObject({ threadId: "t1", busy: true, turnThreadId: "t1", status: "RUNNING · pytest" });
    expect(s.loose).toEqual({});
  });

  it("trades: a pane busy on another thread's turn leaves that one loose in its place", () => {
    let s = withChats({ 3: { threadId: "t2", busy: true, turnThreadId: "t2", status: "THINKING" } },
                      { loose: { t1: "RUNNING · ls" } });
    s = reduce(s, { type: "open_thread", pane: 3, threadId: "t1" });
    expect(s.chats[3]).toMatchObject({ threadId: "t1", busy: true, turnThreadId: "t1" });
    expect(s.loose).toEqual({ t2: "THINKING" });
  });

  it("never leaves a placeholder (a first send's local-N) loose: no thread can open it", () => {
    const s = reduce(withChats({ 2: { compose: { projectId: "p", id: "c1" }, busy: true, turnThreadId: "local-3" } }),
                     { type: "release_chat", pane: 2 });
    expect(s.loose).toEqual({});
  });

  it("forgets a loose turn once it is over", () => {
    const s = withChats({}, { loose: { t1: "THINKING" } });
    expect(reduce(s, { type: "turn_over", threadId: "t1" }).loose).toEqual({});
    expect(reduce(s, { type: "turn_over", threadId: "other" })).toBe(s);
  });

  it("does not adopt a loose turn another pane already tracks", () => {
    let s = withChats({ 1: { threadId: "x", busy: true, turnThreadId: "t1" } }, { loose: { t1: "THINKING" } });
    s = reduce(s, { type: "open_thread", pane: 2, threadId: "t1" });
    // Pane 1 moved on from t1 mid-turn: the existing hand-off brings it here, once.
    expect(s.chats[2]).toMatchObject({ busy: true, turnThreadId: "t1" });
    expect(s.chats[1]).toMatchObject({ busy: false, turnThreadId: null });
    expect(s.loose).toEqual({});
  });
});

describe("the chat the window is about, with pane 1 closed (WP-G2)", () => {
  it("starts with no selected chat and falls back to the pane in slot 1, never id 1", () => {
    expect(initialState.selectedChat).toBeNull();
    const s = withChats({ 3: { threadId: "t3" } }, { selectedChat: null, selectedOrder: [], home: 3 });
    expect(activePane(s)).toBe(3);
    expect(flatState(s).threadId).toBe("t3");
    expect(reduce(s, { type: "home", pane: 3 })).toBe(s);
    expect(reduce(s, { type: "home", pane: 2 }).home).toBe(2);
    // A selection, or one made before, still comes first.
    expect(activePane({ ...s, selectedOrder: [5] })).toBe(5);
    expect(activePane({ ...s, selectedChat: 6, selectedOrder: [5] })).toBe(6);
  });
});

describe("words no pane's compose row holds any more (review of PR #47)", () => {
  it("a hidden compose row a split takes back parks its words for the next new thread, wherever it opens", () => {
    const file = { name: "a.txt", mime: "text/plain", data_b64: "YQ==" } as any;
    let s = withChats({ 8: { compose: { projectId: "p1", id: "c3" }, input: "precious unsent words", files: [file],
                             restore: [{ text: "handed back", files: [], nonce: 4 }] } });
    s = reduce(s, { type: "release_chat", pane: 8, keep: true });
    expect(s.chats[8]).toEqual(emptyChat());
    expect(s.drafts["compose:*"]).toEqual({ text: "precious unsent words\nhanded back", files: [file], projects: ["p1"] });
    // The split's own new compose row in that pane takes them up.
    s = reduce(s, { type: "chat", pane: 8, patch: { compose: { projectId: "p2" }, takeOrphan: true } });
    expect([s.chats[8].input, s.chats[8].files]).toEqual(["precious unsent words\nhanded back", [file]]);
    expect(s.drafts["compose:*"]).toBeUndefined();
  });

  it("a × (which asked) still drops them, and a row's own parked words come first", () => {
    let s = withChats({ 4: { compose: { projectId: "p1", id: "c9" }, input: "discarded" } });
    s = reduce(s, { type: "release_chat", pane: 4 });
    expect(s.drafts).toEqual({});
    s = withChats({}, { drafts: { "compose:*": { text: "orphan", files: [] }, "compose:3": { text: "mine", files: [] } } });
    s = reduce(s, { type: "chat", pane: 3, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
    expect(s.chats[3].input).toBe("mine");
    expect(s.drafts["compose:*"]).toEqual({ text: "orphan", files: [] });
    // A thread opened is not a new thread: it never takes them.
    s = reduce(s, { type: "open_thread", pane: 5, threadId: "t1" });
    expect(s.chats[5].input).toBe("");
    s = reduce(s, { type: "chat", pane: 6, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
    expect(s.chats[6].input).toBe("orphan");
  });

  it("New thread in a pane already composing takes the parked words up, after its own (review 2 of PR #47, MED-2)", () => {
    const projects = [{ id: "p1", name: "jarvis" }, { id: "p2", name: "schoolwork" }] as any;
    let s = withChats({ 1: { compose: { projectId: "p1", id: "x1" }, input: "mine" },
                        8: { compose: { projectId: "p2", id: "x8" }, input: "orphaned" } }, { projects });
    s = reduce(s, { type: "release_chat", pane: 8, keep: true });
    expect(s.drafts["compose:*"]).toEqual({ text: "orphaned", files: [], projects: ["p2"] });
    expect(s.error).toContain("kept for the next new thread you start");
    expect(s.error).toContain("schoolwork");
    // The project chip moves the row (same id): not a new thread, nothing taken.
    s = reduce(s, { type: "chat", pane: 1, patch: { compose: { projectId: "p2", id: "x1" } } });
    expect(s.chats[1].input).toBe("mine");
    expect(s.drafts["compose:*"]).toBeDefined();
    s = reduce(s, { type: "chat", pane: 1, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
    expect(s.chats[1].input).toBe("mine\norphaned");
    expect(s.chats[1].compose?.projectId).toBe("p1");
    expect(s.drafts["compose:*"]).toBeUndefined();
    expect(s.error).toContain("are back");
    expect(s.error).toContain("written in schoolwork");
    expect(s.error).toContain("This new thread is in jarvis");
  });

  it("a release that parks says where, and how many files past eight were left out (MED-2, NIT-3)", () => {
    const f = (n: number, tag: string) =>
      Array.from({ length: n }, (_, i) => ({ name: `${tag}${i}`, mime: "text/plain", data_b64: "YQ==" })) as any;
    const threads = [{ id: "t2", title: "second chat", project_id: "p1" }] as any;
    let s = withChats({ 2: { threadId: "t2", input: "for t2", files: f(8, "b") } },
                      { threads, drafts: { t2: { text: "", files: f(3, "p") } } });
    s = reduce(s, { type: "release_chat", pane: 2 });
    expect(s.drafts.t2.files).toHaveLength(8);
    expect(s.error).toContain("“second chat”");
    expect(s.error).toContain("3 files were left out");
    // Nothing to park: nothing said.
    s = reduce(withChats({ 2: { threadId: "t2" } }, { threads }), { type: "release_chat", pane: 2 });
    expect(s.error).toBe("");
    // A × that asked (no keep) drops a compose row's words, silently.
    s = reduce(withChats({ 2: { compose: { projectId: "p1", id: "c2" }, input: "gone" } }), { type: "release_chat", pane: 2 });
    expect([s.drafts, s.error]).toEqual([{}, ""]);
  });

  it("only a new thread the owner started takes them up, never a row given to a pane nobody sees (review 3 of PR #47)", () => {
    const projects = [{ id: "p1", name: "jarvis" }, { id: "p2", name: "schoolwork" }] as any;
    const orphan = { "compose:*": { text: "orphan words", files: [], projects: ["p1"] } };
    // × reset pane 1 and the window gave the closed pane a fresh row: no take-up, nothing said.
    let s = withChats({}, { projects, drafts: { ...orphan } });
    s = reduce(s, { type: "chat", pane: 1, patch: { compose: { projectId: "p1" } } });
    expect([s.chats[1].input, s.drafts, s.error]).toEqual(["", orphan, ""]);
    // A project or thread going away re-aims rows, drawn or not, with new ids: no take-up.
    s = withChats({ 5: { compose: { projectId: "p1", id: "c5" } }, 6: { threadId: "t1" } },
                  { projects, drafts: { ...orphan } });
    s = reduce(s, { type: "chat", pane: 5, patch: { threadId: null, compose: { projectId: "p2" }, messages: [] } });
    s = reduce(s, { type: "chat", pane: 6, patch: { threadId: null, compose: { projectId: "p2" }, messages: [] } });
    expect([s.chats[5].input, s.chats[6].input, s.drafts, s.error]).toEqual(["", "", orphan, ""]);
    // New thread in a pane already composing, said by the owner: it does.
    s = reduce(s, { type: "chat", pane: 5, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
    expect([s.chats[5].input, s.drafts]).toEqual(["orphan words", {}]);
    expect(s.error).toContain("are back, in this new thread's box");
    // The flag never lands in a pane's conversation.
    expect("takeOrphan" in s.chats[5]).toBe(false);
  });

  it("notices that arrive together share the error line; a later one replaces it (review 3 of PR #47, LOW)", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(1_000_000);
      const threads = [{ id: "t2", title: "second chat", project_id: "p1" }] as any;
      let s = withChats({ 2: { threadId: "t2", input: "for t2" }, 7: { compose: { projectId: "p1", id: "c7" }, input: "kept" } },
                        { threads, projects: [{ id: "p1", name: "jarvis" }] as any });
      s = reduce(s, { type: "release_chat", pane: 2, issuedAt: 1_000_000 });
      s = reduce(s, { type: "release_chat", pane: 7, keep: true, issuedAt: 1_000_000 });
      expect(s.error).toContain("waits with “second chat”");
      expect(s.error).toContain("kept for the next new thread you start");
      // The new thread that takes them, in the same breath: said too, and nothing said twice.
      s = reduce(s, { type: "chat", pane: 7, issuedAt: 1_000_000, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
      expect(s.error).toContain("waits with “second chat”");
      expect(s.error).toContain("are back, in this new thread's box");
      expect(s.error.split("second chat").length).toBe(2);
      // Later, a notice replaces what is there; a plain write always does.
      vi.setSystemTime(1_000_000 + 5_000);
      s = reduce(withChats({ 3: { threadId: "t2", input: "again" } }, { threads, error: s.error, errorAt: s.errorAt }),
                 { type: "release_chat", pane: 3, issuedAt: 1_005_000 });
      expect(s.error.startsWith("What you had not sent waits with")).toBe(true);
      s = reduce(s, { type: "patch", issuedAt: 1_005_000, patch: { error: "Could not send: x" } });
      expect([s.error, s.errorAt]).toEqual(["Could not send: x", 1_005_000]);
      s = reduce(s, { type: "patch", issuedAt: 1_005_000, patch: { error: "" } });
      expect([s.error, s.errorAt]).toEqual(["", 0]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a turn no pane tracks is left loose, once", () => {
    const s = reduce(withChats({}), { type: "loose", threadId: "t4", status: "THINKING" });
    expect(s.loose).toEqual({ t4: "THINKING" });
    expect(reduce(s, { type: "loose", threadId: "t4", status: "THINKING" })).toBe(s);
    const opened = reduce(s, { type: "open_thread", pane: 1, threadId: "t4" });
    expect(opened.chats[1]).toMatchObject({ busy: true, turnThreadId: "t4" });
  });
});


describe("tool transcript state (TC-2)", () => {
  const at = "2026-10-13T00:00:00Z";
  const start = (s: State, id: string, turn = "turn") => reduce(s, { type: "op_start", pane: 1,
    op: { call_id: id, name: "Read", label: "safe", started: Date.parse(at), at, turn_id: turn } });
  it("clears interim drafts and writes both states without leaking result/args", () => {
    let s = withChats({ 1: { threadId: "a", loadedThread: "a", draft: "interim", busy: true } });
    s = start(s, "1");
    expect(s.chats[1].draft).toBe(""); expect(s.chats[1].ops).toHaveLength(1);
    expect(s.chats[1].messages[0].calls?.[0]).toMatchObject({ label: "safe", outcome: "running" });
    s = reduce(s, { type: "op_done", pane: 1, call_id: "1", turn_id: "turn", ok: false, outcome: "denied", summary: "SECRET RESULT", at });
    expect(s.chats[1].messages[0].calls?.[0].outcome).toBe("denied");
    expect(JSON.stringify(s.chats[1].messages)).not.toContain("SECRET RESULT");
  });
  it("a stop before queued-next state stops that turn, not later calls with reused ids", () => {
    let s = start(withChats({ 1: { threadId: "a", busy: true } }), "same");
    s = start(s, "same", "next");
    s = reduce(s, { type: "tool_stop", pane: 1, turn_id: "turn", at });
    s = reduce(s, { type: "turn", event: "finished", threadId: "a", at: Date.parse(at), next: 1 });
    expect(s.chats[1].messages.flatMap(m => m.calls ?? []).map(c => c.outcome)).toEqual(["stopped", "running"]);
    expect(s.chats[1].busy).toBe(true);
  });
  it("replays an overlapping delayed history with steer splits, finishes and stopped calls", () => {
    let s = start(withChats({ 1: { threadId: "a", loadedThread: null, busy: true } }), "1");
    s = reduce(s, { type: "message", pane: 1, message: { role: "user", text: "steer", message_id: "m", at, mark: "queued" } });
    s = start(s, "2");
    s = reduce(s, { type: "op_done", pane: 1, call_id: "1", turn_id: "turn", ok: true, summary: "", at });
    s = reduce(s, { type: "tool_stop", pane: 1, turn_id: "turn", at });
    s = reduce(s, { type: "mark", message_id: "m", patch: { mark: undefined } });
    s = reduce(s, { type: "transcript", threadId: "a", rows: [
      { role: "tool", call_id: "1", turn_id: "turn", name: "Read", label: "safe", outcome: "running", at },
      { role: "user", text: "steer", message_id: "m", at, mark: "queued" },
    ] });
    expect(s.chats[1].messages.map(m => m.calls?.map(c => c.outcome) ?? m.text)).toEqual([["ok"], "steer", ["stopped"]]);
    expect(s.chats[1].messages[1].mark).toBeUndefined();
    expect(s.chats[1].ops.every(o => o.finished)).toBe(true);
    expect(s.chats[1].toolTurnId).toBeNull(); expect(s.chats[1].toolReplay).toEqual([]);
  });
  it.each([false, true])("uses the initial send's acknowledged turn before/after history (ack first: %s)", ackFirst => {
    let s = withChats({ 1: { threadId: "a", loadedThread: null } });
    s = reduce(s, { type: "message", pane: 1, message: { role: "user", text: "again", local: "initial" } });
    const ack = () => { s = reduce(s, { type: "mark", local: "initial", patch: { turn_id: "new" } }); };
    if (ackFirst) ack();
    s = reduce(s, { type: "transcript", threadId: "a", rows: [
      { role: "user", text: "again", at, turn_id: "old" },
      { role: "user", text: "again", at, turn_id: "new" },
      { role: "user", text: "again", at, turn_id: "new", message_id: "steer-1" },
      { role: "user", text: "again", at, turn_id: "new", message_id: "steer-2" },
      { role: "assistant", text: "answer", at, turn_id: "new" },
    ] });
    if (!ackFirst) ack();
    expect(s.chats[1].messages.map(m => m.local ?? m.message_id ?? m.turn_id)).toEqual(["old", "initial", "steer-1", "steer-2", "new"]);
  });
  it("keeps an acknowledged send absent from the history snapshot", () => {
    let s = withChats({ 1: { threadId: "a", loadedThread: null } });
    s = reduce(s, { type: "message", pane: 1, message: { role: "user", text: "again", local: "initial" } });
    s = reduce(s, { type: "mark", local: "initial", patch: { turn_id: "new" } });
    s = reduce(s, { type: "transcript", threadId: "a", rows: [{ role: "user", text: "again", at, turn_id: "old" }] });
    expect(s.chats[1].messages).toHaveLength(2);
    expect(s.chats[1].messages[1]).toMatchObject({ local: "initial", turn_id: "new" });
  });
  it("binds an optimistic steer before history replay and never revives a failed send", () => {
    let s = withChats({ 1: { threadId: "a", loadedThread: null } });
    s = reduce(s, { type: "message", pane: 1, message: { role: "user", text: "steer", local: "local" } });
    s = start(s, "1");
    s = reduce(s, { type: "mark", local: "local", patch: { message_id: "server", mark: "steering" } });
    s = reduce(s, { type: "message", pane: 1, message: { role: "user", text: "failed", local: "failed" } });
    s = reduce(s, { type: "unmessage", local: "failed" });
    s = reduce(s, { type: "transcript", threadId: "a", rows: [
      { role: "user", text: "steer", at, message_id: "server", mark: "steering" },
      { role: "tool", call_id: "1", turn_id: "turn", name: "Read", label: "safe", outcome: "running", at },
    ] });
    expect(s.chats[1].messages.map(m => m.calls ? "tool" : m.text)).toEqual(["steer", "tool"]);
    expect(s.chats[1].messages[0]).toMatchObject({ local: "local", message_id: "server", mark: "steering" });
  });
  it("reconciles a server id learned after history has landed, preserving history order", () => {
    let s = withChats({ 1: { threadId: "a", loadedThread: null } });
    s = reduce(s, { type: "message", pane: 1, message: { role: "user", text: "steer", local: "local" } });
    s = reduce(s, { type: "transcript", threadId: "a", rows: [
      { role: "user", text: "steer", at, message_id: "server" }, { role: "assistant", text: "reply" },
    ] });
    s = reduce(s, { type: "mark", local: "local", patch: { message_id: "server", mark: "steering" } });
    expect(s.chats[1].messages.map(m => m.text)).toEqual(["steer", "reply"]);
    expect(s.chats[1].messages[0]).toMatchObject({ local: "local", at, mark: "steering" });
  });
  it("lands history in the pane that now owns the thread and drops it after closing", () => {
    const s = withChats({ 1: { threadId: "b" }, 2: { threadId: "a" } });
    const after = reduce(s, { type: "transcript", threadId: "a", rows: [{ role: "user", text: "a" }] });
    expect(after.chats[1].messages).toEqual([]); expect(after.chats[2].messages[0].text).toBe("a");
    expect(reduce(s, { type: "transcript", threadId: "gone", rows: [] })).toBe(s);
  });
});


describe("checkpoint compose draft regressions", () => {
  const files = (count: number) => Array.from({ length: count }, (_, i) => ({ name: `f${i}.txt`, mime: "text/plain", data_b64: "" }));
  it("reports concurrent attachment overflow against the reducer's actual current state", () => {
    for (const shown of [false, true]) {
      let s = withChats(shown ? { 2: { threadId: "t1" } } : {});
      s = reduce(s, { type: "stage", key: "t1", files: files(5) });
      s = reduce(s, { type: "stage", key: "t1", files: files(5) });
      expect(shown ? s.chats[2].files.length : s.drafts.t1.files.length).toBe(8);
      expect(s.error).toContain("2 files were left out");
    }
  });
  it("keeps already parked compose words when their pane closes, even if it shows another thread", () => {
    for (const threadId of [null, "t2"]) {
      let s = withChats({ 2: { threadId, compose: threadId ? null : { projectId: "p1" } } });
      s = reduce(s, { type: "park", key: "compose:2", text: "failed send", files: files(1), projectId: "p1" });
      s = reduce(s, { type: "release_chat", pane: 2 });
      expect(s.drafts["compose:2"]).toBeUndefined();
      expect(s.drafts["compose:*"].text).toBe("failed send");
      expect(s.drafts["compose:*"].files).toHaveLength(1);
      expect(s.drafts["compose:*"].projects).toEqual(["p1"]);
      expect(s.error).toContain("next new thread");
    }
  });
});


describe("drawn compose restoration", () => {
  it("merges the current parked draft once, and refuses stale compose identities", () => {
    let s = withChats({ 2: { compose: { id: "c2", projectId: "p1" }, input: "new words" } });
    s = reduce(s, { type: "park", key: "compose:2", text: "failed words" });
    const action = { type: "restore_compose" as const, pane: 2 as const, composeId: "old" };
    expect(reduce(s, action)).toBe(s);
    const current = { ...action, composeId: "c2" };
    s = reduce(s, current);
    expect(s.chats[2].input).toBe("new words\nfailed words");
    expect(s.drafts["compose:2"]).toBeUndefined();
    expect(reduce(s, current)).toBe(s);
    expect(reduce(s, { ...current, pane: 3 })).toBe(s);
  });
  it("does not restore compose words into an existing or opened thread", () => {
    for (const fields of [{ threadId: "t1" }, { compose: { id: "c2", projectId: "p1", openedId: "t1" } }]) {
      const s = withChats({ 2: { compose: { id: "c2", projectId: "p1" }, ...fields } },
        { drafts: { "compose:2": { text: "private draft", files: [] } } });
      expect(reduce(s, { type: "restore_compose", pane: 2, composeId: "c2" })).toBe(s);
    }
  });
  it("replaces its own obsolete location notice and keeps a later channel failure", () => {
    let s = withChats({ 8: { compose: { id: "c8", projectId: "p1" }, input: "saved" } });
    s = reduce(s, { type: "release_chat", pane: 8, keep: true });
    expect(s.error).toContain("kept for the next new thread");
    s = reduce(s, { type: "chat", pane: 8, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
    expect(s.error).toContain("are back");
    expect(s.error).not.toContain("kept for the next new thread");
    s = reduce(s, { type: "notice", text: "The project's channel could not be created." });
    expect(s.error).toContain("are back");
    expect(s.error).toContain("channel could not");
  });
  it("says when a pane's own pending draft leaves the orphan waiting", () => {
    let s = withChats({}, { drafts: { "compose:2": { text: "mine", files: [] }, "compose:*": { text: "other", files: [] } } });
    s = reduce(s, { type: "chat", pane: 2, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
    expect(s.chats[2].input).toBe("mine");
    expect(s.drafts["compose:*"].text).toBe("other");
    expect(s.error).toContain("Other unsent words are still kept");
  });
});


it("an orphan takeup retains an unrelated thread-draft location notice", () => {
  let s = withChats({ 2: { threadId: "t2", input: "thread words" } },
    { drafts: { "compose:2": { text: "compose words", files: [] } } });
  s = reduce(s, { type: "release_chat", pane: 2 });
  s = reduce(s, { type: "chat", pane: 3, patch: { compose: { projectId: "p1" }, takeOrphan: true } });
  expect(s.error).toContain("waits with its thread");
  expect(s.error).toContain("are back");
  expect(s.error).not.toContain("kept for the next new thread");
});
