// Review of PR #330, NEW-3: a `GET /approvals` in flight never undoes what
// the stream said after it was sent.
import { describe, expect, it } from "vitest";
import { ApprovalSync, type ApprovalSyncAction } from "./approvalSync";
import { initialState, reduce, type State } from "../state/store";

const card = (id: string, extra: Record<string, unknown> = {}) =>
  ({ req_id: id, code: id.toUpperCase(), tool: "Bash", command: "make", summary_state: "pending", ...extra }) as any;
const run = (s: State, actions: ApprovalSyncAction[]) => actions.reduce((st, a) => reduce(st, a as any), s);

describe("ApprovalSync: a re-read is ordered against the stream", () => {
  it("a card resolved while the read was in flight stays gone (no ghost veil)", () => {
    const sync = new ApprovalSync();
    let s = reduce(initialState, { type: "approval_add", request: card("a") });
    const pending = sync.begin();                         // the read leaves; the broker still lists `a`
    const drop = { type: "approval_drop", req_id: "a" } as const;
    sync.heard(drop);
    s = reduce(s, drop);                                  // the owner answered; the stream said so
    s = run(s, sync.land(pending, [card("a")]));          // the stale list lands
    expect(s.approvals).toEqual([]);
    expect(s.orb).not.toBe("approval");
    // Mutation evidence: applying the list alone brings the ghost back.
    const naive = reduce(reduce(reduce(initialState, { type: "approval_add", request: card("a") }), drop),
      { type: "approvals_sync", approvals: [card("a")] });
    expect(naive.approvals.map((r) => r.req_id)).toEqual(["a"]);
  });

  it("a card added while the read was in flight stays, and so does a summary heard", () => {
    const sync = new ApprovalSync();
    let s = initialState;
    const pending = sync.begin();                         // the list will not have `b`
    const add = { type: "approval_add", request: card("b") } as const;
    sync.heard(add);
    s = reduce(s, add);
    const patch = { type: "approval_patch", req_id: "b", patch: { summary: "Builds.", summary_state: "ready" } } as const;
    sync.heard(patch);
    s = reduce(s, patch);
    s = run(s, sync.land(pending, []));
    expect(s.approvals.map((r) => r.req_id)).toEqual(["b"]);
    expect(s.approvals[0]).toMatchObject({ summary: "Builds.", summary_state: "ready" });
    const naive = reduce(s, { type: "approvals_sync", approvals: [] });
    expect(naive.approvals).toEqual([]);                  // the mutant drops it
  });

  it("only actions heard after the read began are replayed; a failed read changes nothing", () => {
    const sync = new ApprovalSync();
    sync.heard({ type: "approval_drop", req_id: "before" });
    const pending = sync.begin();
    expect(sync.land(pending, [card("x")])).toEqual([{ type: "approvals_sync", approvals: [card("x")] }]);
    const failed = sync.begin();
    sync.drop(failed);
    sync.heard({ type: "approval_drop", req_id: "x" });  // no read in flight: nothing kept
    const next = sync.begin();
    expect(sync.land(next, [])).toEqual([{ type: "approvals_sync", approvals: [] }]);
  });
});
