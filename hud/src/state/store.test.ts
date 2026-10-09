import { describe, expect, it } from "vitest";
import { initialState, reduce, type State } from "./store";
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

describe("steering marks (2026-10-08)", () => {
  const sent: State = {
    ...initialState,
    messages: [
      { role: "user", text: "first" },
      { role: "assistant", text: "working on it" },
      { role: "user", text: "do it in rust instead", local: "local-1", mark: "steering" },
    ],
  };

  it("marks this window's message by its local id, then by the daemon's id", () => {
    const queued = reduce(sent, { type: "mark", local: "local-1", patch: { mark: "queued", message_id: "m1" } });
    expect(queued.messages[2]).toMatchObject({ mark: "queued", message_id: "m1", text: "do it in rust instead" });
    const ran = reduce(queued, { type: "mark", message_id: "m1", patch: { mark: undefined } });
    expect(ran.messages[2].mark).toBeUndefined();
    expect(reduce(queued, { type: "mark", message_id: "nope", patch: { mark: "not sent" } })).toBe(queued);
  });

  it("takes back only the failed message, never a reply that settled meanwhile", () => {
    const settled = reduce(sent, { type: "settle", text: "a reply that landed during the send" });
    const after = reduce(settled, { type: "unmessage", local: "local-1" });
    expect(after.messages.map((m) => m.text)).toEqual([
      "first", "working on it", "a reply that landed during the send",
    ]);
  });
});
