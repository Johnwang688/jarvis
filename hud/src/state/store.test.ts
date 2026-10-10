import { describe, expect, it } from "vitest";
import { compatActions, flatState, initialState, reduce, type State } from "./store";
import { emptyChats, runningHere, type ChatState } from "../lib/chats";
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
    expect(s.chats[2].messages.map((m) => m.text)).toEqual(["hello"]);
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
