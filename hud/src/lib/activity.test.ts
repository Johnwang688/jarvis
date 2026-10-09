import { describe, expect, it } from "vitest";
import {
  NO_ACTIVITY, applyActivity, clearsOnRead, mostUrgent, parseActivity, projectActivity,
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

  it("only unread and failed are cleared by reading", () => {
    expect(["idle", "working", "needs_input", "unread", "failed", undefined].map((s) => clearsOnRead(s as any)))
      .toEqual([false, false, false, true, true, false]);
  });
});
