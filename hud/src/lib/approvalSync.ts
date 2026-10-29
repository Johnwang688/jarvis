// The approval cards' re-read, ordered against the event stream (review of
// PR #330, NEW-3): `GET /approvals` answers with the broker's list as it was
// when it was read, so a card resolved (or added, or summarized) while the
// request was in flight would be undone by applying the list as it lands —
// a resolved card brought back is a ghost veil the owner cannot answer, and
// a card dropped is a question that times out unseen.
//
// `ActivitySync`'s pattern (lib/activity.ts): `begin()` when the request is
// sent; every card action heard meanwhile is kept for it (`heard`); `land()`
// gives the list first and then those actions again, in order, so whatever
// happened after the read began wins. Pure: actions in, actions out.
import type { ApprovalRequest } from "../types";

export type ApprovalAction =
  | { type: "approval_add"; request: ApprovalRequest }
  | { type: "approval_drop"; req_id: string }
  | { type: "approval_patch"; req_id: string; patch: Pick<ApprovalRequest, "summary" | "summary_state"> };

export type ApprovalSyncAction = ApprovalAction | { type: "approvals_sync"; approvals: ApprovalRequest[] };

interface Pending { actions: ApprovalAction[] }

export class ApprovalSync {
  private inFlight = new Set<Pending>();

  /** A card action, as it is dispatched: replayed over every read still in flight. */
  heard(action: ApprovalAction): void {
    for (const p of this.inFlight) p.actions.push(action);
  }

  /** `GET /approvals` is sent. */
  begin(): Pending {
    const p: Pending = { actions: [] };
    this.inFlight.add(p);
    return p;
  }

  /** …and answered: the list, then every card action heard since it was sent. */
  land(p: Pending, approvals: ApprovalRequest[]): ApprovalSyncAction[] {
    this.inFlight.delete(p);
    return [{ type: "approvals_sync", approvals }, ...p.actions];
  }

  /** …or failed: the cards on screen stay. */
  drop(p: Pending): void {
    this.inFlight.delete(p);
  }
}
