// Stable props for memoized components (re-review of PR #27).
//
// Typing in a chat box re-renders the whole window, because the box's words
// are the store's (they move with their conversation, WP-B). A memoized child
// skips that render only if every prop it gets is the same object as last
// time, so the window hands it:
//
//   - callbacks through `useStableHandlers`: one function per name for the
//     component's life, which always calls the **latest** closure the window
//     rendered — so nothing behaves differently, only identity is kept;
//   - computed lists through `useStableValue`: the previous value while the
//     new one is equal in content (JSON), so a list rebuilt every render does
//     not count as a change when nothing in it changed.

import { useRef, useState } from "react";

type Fn = (...args: any[]) => any;

/** The same functions every render, each calling the newest one passed. */
export function useStableHandlers<T extends Record<string, Fn>>(handlers: T): T {
  const latest = useRef(handlers);
  latest.current = handlers;
  const [stable] = useState(() => {
    const out: Record<string, Fn> = {};
    for (const name of Object.keys(handlers)) out[name] = (...args: any[]) => latest.current[name](...args);
    return out as T;
  });
  return stable;
}

/** `value`, or the previous one while it is equal in content (small, JSON-able values only). */
export function useStableValue<T>(value: T): T {
  const kept = useRef<{ key: string; value: T } | null>(null);
  const key = JSON.stringify(value);
  if (!kept.current || kept.current.key !== key) kept.current = { key, value };
  return kept.current.value;
}
