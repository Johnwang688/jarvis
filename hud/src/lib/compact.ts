// Which Compact chip a thread gets (WP-C of docs/plans/2026-10-19-compact-button-plan.md,
// section 4). The chip is for an opened chat and nothing else: a compose pane,
// a task's thread and any other role get none. The words are the plan's.

import type { Thread } from "../types";

export type CompactState = "ready" | "running" | "off";
export interface CompactView { state: CompactState; title: string }

const READY: Record<Thread["provider"], string> = {
  claude: "Run Claude Code's /compact: it summarises the conversation so the model's context shrinks. "
    + "Every message stays here.",
  codex: "Run Codex's own compaction: it shrinks the context the model carries. Every message stays here.",
  fast: "Summarise older messages (your first request is kept) so the context shrinks. "
    + "Every message stays here.",
};

export function compactFor(
  thread: Thread | null | undefined,
  now: { turnRunning: boolean; card: boolean },
): CompactView | null {
  if (!thread || thread.role !== "chat" || thread.task_id) return null;
  // A compaction is a turn too, so this comes before the running turn's "off".
  if (thread.compacting) return { state: "running", title: "Compacting the conversation…" };
  if (thread.archived) return { state: "off", title: "This chat is archived." };
  if (now.card) return { state: "off", title: "Answer the card first." };
  if (now.turnRunning) return { state: "off", title: "A turn is running; compact when it ends." };
  return { state: "ready", title: READY[thread.provider] ?? READY.fast };
}
