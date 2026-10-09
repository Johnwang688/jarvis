import { describe, expect, it } from "vitest";
import {
  ActivitySync, NO_ACTIVITY, applyActivity, clearsOnRead, mostUrgent, parseActivity, projectActivity,
} from "./activity";

const rec = (of: string, id: string, status: string) => ({ kind: "activity", data: { of, id, status } });

describe("activity", () => {
  it("orders by urgency: failed, waiting, working, unread, idle", () => {
    expect(mostUrgent([])).toBe("idle");
    expect(mostUrgent(["unread", "working"])).toBe("working");
    expect(mostUrgent(["working", "needs_input", "unread"])).toBe("needs_input");
    expect(mostUrgent(["needs_input", "failed", "idle"])).toBe("failed");
  });

  it("rolls a folded project up from its chats and tasks only", () => {
    const view = { threads: { a: "unread" as const, other: "failed" as const }, tasks: { t: "working" as const } };
    expect(projectActivity(view, ["a", "b"], ["t"])).toBe("working");
    expect(projectActivity(view, ["a"], [])).toBe("unread");
    expect(projectActivity(view, ["b"], ["u"])).toBe("idle");
  });

  it("folds a record in, drops idle, and ignores anything malformed", () => {
    let v = applyActivity(NO_ACTIVITY, rec("thread", "a", "working"));
    expect(v.threads).toEqual({ a: "working" });
    v = applyActivity(v, rec("task", "t", "unread"));
    expect(v.tasks).toEqual({ t: "unread" });
    const same = applyActivity(v, rec("thread", "a", "working"));
    expect(same).toBe(v);
    v = applyActivity(v, rec("thread", "a", "idle"));
    expect(v.threads).toEqual({});
    for (const bad of [null, {}, rec("project", "p", "unread"), rec("thread", "a", "busy"),
                       { kind: "activity", data: { of: "thread", status: "unread" } }]) {
      expect(applyActivity(v, bad)).toBe(v);
    }
  });

  it("reads GET /activity defensively", () => {
    expect(parseActivity({ threads: { a: "failed", b: "idle", c: "nonsense" }, tasks: null }))
      .toEqual({ threads: { a: "failed" }, tasks: {} });
    expect(parseActivity(undefined)).toEqual(NO_ACTIVITY);
  });

  it("replays a record heard while GET /activity was in flight over the older snapshot", () => {
    const sync = new ActivitySync();
    const pending = sync.begin();
    sync.heard(rec("thread", "a", "working"));      // published after the snapshot was taken
    sync.heard(rec("task", "t", "idle"));
    const view = sync.land(pending, { threads: { a: "failed", b: "unread" }, tasks: { t: "unread" } });
    expect(view).toEqual({ threads: { a: "working", b: "unread" }, tasks: {} });
    // Nothing is replayed over a later snapshot that did not miss it.
    expect(sync.land(sync.begin(), { threads: { a: "idle" }, tasks: {} })).toEqual({ threads: { a: "idle" }, tasks: {} });
    // A failed snapshot stops collecting.
    const failed = sync.begin();
    sync.drop(failed);
    sync.heard(rec("thread", "a", "unread"));
    expect(failed.records).toEqual([]);
  });

  it("draws a /seen answer unless something newer about that row arrived meanwhile", () => {
    const sync = new ActivitySync();
    expect(sync.answered(sync.ask("thread", "a"), "idle")).toEqual(rec("thread", "a", "idle"));
    // A record for the row after the ask is newer than the answer.
    let ticket = sync.ask("thread", "a");
    sync.heard(rec("thread", "a", "working"));
    expect(sync.answered(ticket, "idle")).toBeNull();
    // A record for another row is not.
    ticket = sync.ask("task", "t");
    sync.heard(rec("thread", "a", "unread"));
    expect(sync.answered(ticket, "idle")).toEqual(rec("task", "t", "idle"));
    // A snapshot sent after the ask is newer too...
    ticket = sync.ask("thread", "a");
    sync.land(sync.begin(), NO_ACTIVITY);
    expect(sync.answered(ticket, "idle")).toBeNull();
    // ...but one sent before it may be older: the answer is replayed over it.
    const pending = sync.begin();
    ticket = sync.ask("thread", "a");
    expect(sync.answered(ticket, "idle")).toEqual(rec("thread", "a", "idle"));
    expect(sync.land(pending, { threads: { a: "unread" }, tasks: {} })).toEqual(NO_ACTIVITY);
    // Garbage draws nothing.
    expect(sync.answered(sync.ask("thread", "a"), "busy")).toBeNull();
  });

  it("only unread and failed are cleared by reading", () => {
    expect(["idle", "working", "needs_input", "unread", "failed", undefined].map((s) => clearsOnRead(s as any)))
      .toEqual([false, false, false, true, true, false]);
  });
});
