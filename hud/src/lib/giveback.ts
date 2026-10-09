// Words and files handed back to the input box (2026-10-08, review of PR #22):
// a send that failed, a steer the owner's Stop dropped, queued messages a Stop
// cleared. Nothing typed is ever lost — and it comes back where it was typed:
// words for a thread the owner is not looking at are held until they open it,
// never written into another thread's box (Bugbot on PR #22).

import type { Attachment } from "../types";

/** One hand-back to the box. `nonce` grows for the life of the page, so the
 * box takes each exactly once however often it re-renders. */
export interface GiveBack {
  text: string;
  files: Attachment[];
  nonce: number;
}

let seq = 0;

/** A nonce no earlier hand-back had (`Date.now()` repeats within a millisecond). */
export function nextNonce(): number {
  seq += 1;
  return seq;
}

/** What the box has not yet taken, newest last; `upTo` drops what it took. */
export function pendingAfter(list: GiveBack[], upTo: number): GiveBack[] {
  return list.filter((g) => g.nonce > upTo);
}

/** Several hand-backs as one: oldest words first, files in order. */
export function joined(list: GiveBack[]): { text: string; files: Attachment[] } {
  return {
    text: list.map((g) => g.text).filter(Boolean).join("\n"),
    files: list.flatMap((g) => g.files),
  };
}

/** Words for threads not on screen, per thread, until the owner opens one. */
export class HeldBack {
  private held = new Map<string, { texts: string[]; files: Attachment[] }>();

  hold(threadId: string, text: string, files: Attachment[]): void {
    if (!text && !files.length) return;
    const entry = this.held.get(threadId) ?? { texts: [], files: [] };
    if (text) entry.texts.push(text);
    entry.files.push(...files);
    this.held.set(threadId, entry);
  }

  /** The thread's held words, once: taking them forgets them. */
  take(threadId: string): { text: string; files: Attachment[] } | null {
    const entry = this.held.get(threadId);
    if (!entry) return null;
    this.held.delete(threadId);
    return { text: entry.texts.join("\n"), files: entry.files };
  }

  has(threadId: string): boolean {
    return this.held.has(threadId);
  }
}
