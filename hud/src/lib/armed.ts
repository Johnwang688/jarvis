// Approval summaries (AS-13, docs/plans/2026-10-28-approval-summaries.md):
// the hooks every approving button on the HUD shares.
//
//   - `useArmed(key)`: false for ARM_MS after mount and after `key` changes,
//     then true. Derived in render (`armedKey === key`), so the very commit
//     that draws a new key is already disarmed: a passive effect that reset
//     a boolean left one frame armed under the new content (review of PR
//     #330, LOW 2).
//   - `useWakeAt(at)`: one re-render at the given time (ms since the epoch),
//     for a summary that goes stale (LOW 3) on a view with no clock of its own.
import { useEffect, useState } from "react";
import { ARM_MS } from "./approvalSummary";

export function useArmed(key: string): boolean {
  const [armedKey, setArmedKey] = useState<string | null>(null);
  useEffect(() => {
    // Cleared too, so a key that comes back (A, B, A) waits again.
    setArmedKey(null);
    const t = setTimeout(() => setArmedKey(key), ARM_MS);
    return () => clearTimeout(t);
  }, [key]);
  return armedKey === key;
}

export function useWakeAt(at: number | null): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (at === null) return;
    const wait = at - Date.now();
    if (wait < 0) return;
    const t = setTimeout(() => setTick((n) => n + 1), wait + 50);
    return () => clearTimeout(t);
  }, [at]);
}
