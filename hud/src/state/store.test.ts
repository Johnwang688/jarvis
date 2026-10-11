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
