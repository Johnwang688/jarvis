// Moving a chat thread between projects, as a pure reducer.
//
// The rule the backend states and the window has to state too: **a task's
// threads move with their task.** So `canMoveThread` is the one place that
// decides, and both the drag affordance and the context menu's explanation
// read from it — the alternative is a row that looks draggable, a PATCH that
// 409s, and an owner who learns the rule from an error message.
//
// `moveThreadTo` is optimistic and reversible by construction: it returns a
// new array and never mutates, so the caller keeps the old one and puts it
// back when the PATCH is refused.

import type { Thread } from "../types";

export interface Movability {
  ok: boolean;
  why: string;
}

export function canMoveThread(t: Thread | null | undefined): Movability {
  if (!t) return { ok: false, why: "no such thread" };
  if (t.task_id) return { ok: false, why: "task threads move with their task" };
  return { ok: true, why: "" };
}

/**
 * The thread re-parented, or the list unchanged. Unchanged covers every case
 * the caller must not act on: no such thread, a task thread, and a move to the
 * project it is already in (which would otherwise send a pointless PATCH and
 * flash the row).
 */
export function moveThreadTo(threads: Thread[], id: string, projectId: string): Thread[] {
  const t = threads.find((x) => x.id === id);
  if (!t || !canMoveThread(t).ok) return threads;
  if (!projectId || t.project_id === projectId) return threads;
  return threads.map((x) => (x.id === id ? { ...x, project_id: projectId } : x));
}

/** Whether a drop would do anything — what the drop target uses to light up. */
export function wouldMove(threads: Thread[], id: string, projectId: string): boolean {
  return moveThreadTo(threads, id, projectId) !== threads;
}
