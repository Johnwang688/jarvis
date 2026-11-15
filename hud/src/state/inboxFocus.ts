// A request to show the Needs you page at one thread's group (WS-3): a hint on a
// thread row in the sidebar asks, the page (InboxView) answers once a read started
// for that request has landed and its rows are drawn, and then the request is gone.
// It is held outside React like the other stores, because the page may not be
// mounted yet when the hint is clicked (showView opens it a render later). It
// carries a thread id and a request id, and nothing else.
//
// Each request has its own id, so a second hint for the same thread is a new request
// that waits for its own read (review of PR #368, round 2). A request never outlives
// its navigation: an ordinary open clears it, the page going away clears it, a newer
// request replaces it, and one nobody claimed within `FOCUS_REQUEST_MS` (the page
// refused to open, say) is as good as none. Once the page has claimed it, it is kept
// through however slow the read is.

import { useSyncExternalStore } from "react";

type Listener = () => void;

/** How long an unclaimed request stays good. */
export const FOCUS_REQUEST_MS = 15_000;

export interface FocusRequest {
  readonly id: number;
  readonly thread: string;
}

export class InboxFocusStore {
  private req: FocusRequest | null = null;
  private at = 0;
  private claimed = false;
  private seq = 0;
  private listeners = new Set<Listener>();

  constructor(private now: () => number = () => Date.now()) {}

  /** The request the page should answer, until it has (the same object while it stands). */
  current(): FocusRequest | null {
    if (this.req !== null && !this.claimed && this.now() - this.at > FOCUS_REQUEST_MS) this.req = null;
    return this.req;
  }

  /** The thread asked for (a convenience for callers that only need it). */
  get(): string | null {
    return this.current()?.thread ?? null;
  }

  /** A new request, replacing any earlier one. */
  request(thread: string): FocusRequest {
    this.req = { id: ++this.seq, thread };
    this.at = this.now();
    this.claimed = false;
    this.listeners.forEach((l) => l());
    return this.req;
  }

  /** The page has taken this request up: it no longer expires (it still goes with the page, or a newer request). */
  claim(id: number): void {
    if (this.req?.id === id) this.claimed = true;
  }

  clear(): void {
    if (this.req === null) return;
    this.req = null;
    this.claimed = false;
    this.listeners.forEach((l) => l());
  }

  /** Clear only if this request is still the standing one (a newer one is left alone). */
  clearRequest(id: number): void {
    if (this.req?.id === id) this.clear();
  }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };
}

export const inboxFocus = new InboxFocusStore();

export function useInboxFocus(store: InboxFocusStore = inboxFocus): FocusRequest | null {
  return useSyncExternalStore(store.subscribe, () => store.current(), () => null);
}
