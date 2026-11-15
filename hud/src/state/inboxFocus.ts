// A request to show the Needs you page at one thread's group (WS-3): a hint on a
// thread row in the sidebar asks, the page (InboxView) answers once its opening
// read has landed and its rows are drawn, and then the request is gone. It is held
// outside React like the other stores, because the page may not be mounted yet
// when the hint is clicked (showView opens it a render later). It carries a thread
// id and nothing else.
//
// A request never outlives its navigation (review of PR #368): an ordinary open of
// the page clears it, the page clears it when it goes away, and one nobody answered
// within `FOCUS_REQUEST_MS` (the page refused to open, say) is as good as none.

import { useSyncExternalStore } from "react";

type Listener = () => void;

/** How long an unanswered request stays good. */
export const FOCUS_REQUEST_MS = 15_000;

export class InboxFocusStore {
  private target: string | null = null;
  private at = 0;
  private listeners = new Set<Listener>();

  constructor(private now: () => number = Date.now) {}

  /** The thread the page should scroll to, until it has (or the request is too old to mean anything). */
  get(): string | null {
    if (this.target !== null && this.now() - this.at > FOCUS_REQUEST_MS) this.target = null;
    return this.target;
  }

  request(threadId: string): void {
    this.target = threadId;
    this.at = this.now();
    this.listeners.forEach((l) => l());
  }

  clear(): void {
    if (this.target === null) return;
    this.target = null;
    this.listeners.forEach((l) => l());
  }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };
}

export const inboxFocus = new InboxFocusStore();

export function useInboxFocus(store: InboxFocusStore = inboxFocus): string | null {
  return useSyncExternalStore(store.subscribe, () => store.get(), () => null);
}
