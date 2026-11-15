// A request to show the Needs you page at one thread's group (WS-3): a hint on a
// thread row in the sidebar asks, the page (InboxView) answers once its rows are
// drawn, and then the request is gone. It is held outside React like the other
// stores, because the page may not be mounted yet when the hint is clicked
// (showView opens it a render later). It carries a thread id and nothing else.

import { useSyncExternalStore } from "react";

type Listener = () => void;

export class InboxFocusStore {
  private target: string | null = null;
  private listeners = new Set<Listener>();

  /** The thread the page should scroll to, until it has. */
  get(): string | null {
    return this.target;
  }

  request(threadId: string): void {
    this.target = threadId;
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
