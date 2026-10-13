/** Stops pressed before a send acknowledges belong to that send, not a later turn. */
export class SendStops {
  private pending = new Map<string, { threadId: string | null; stopped: boolean }>();
  begin(local: string, threadId: string | null) { this.pending.set(local, { threadId, stopped: false }); }
  bind(local: string, threadId: string) {
    const send = this.pending.get(local);
    if (send) send.threadId = threadId;
  }
  stop(target: string): boolean {
    let found = false;
    for (const [local, send] of this.pending) {
      if (local === target || send.threadId === target) { send.stopped = true; found = true; }
    }
    return found;
  }
  acknowledge(local: string, result: { status?: string; turn_id?: string; queued_turn_id?: string }) {
    const send = this.pending.get(local);
    this.pending.delete(local);
    if (!send?.stopped || !send.threadId || result.status === "dropped") return null;
    // A stale local busy snapshot may have queued this send behind another
    // turn. Either exact ID is safe; a later unrelated turn never matches.
    const turnIds = [...new Set([result.turn_id, result.status === "queued" ? result.queued_turn_id : undefined]
      .filter((id): id is string => typeof id === "string" && !!id))];
    return { threadId: send.threadId, turnIds };
  }
  finish(local: string) { this.pending.delete(local); }
}
