// What a chat is called on screen (chat titles, 2026-10-12; design §18).
//
// The daemon names a chat from the owner's first words (`jarvis/v2/titles.py`)
// and the owner can rename it; until then a row reads "New thread" (nothing
// said yet, or the title is still being made) or "Untitled" (an older chat
// the start-up backfill has not reached) — never the bare id, which moved to
// the row's ⋯ menu as "Copy id". A title is text: React escapes it.

import type { Thread } from "../types";

export const UNTITLED_NEW = "New thread";
export const UNTITLED_OLD = "Untitled";
/** The sidebar wraps a title and clamps it at this many lines (theme.css). */
export const TITLE_LINES = 3;

export interface ThreadLabel {
  text: string;
  untitled: boolean;
}

/** The label a thread row, a pane header or a notice shows for a chat. */
export function threadLabel(thread: Pick<Thread, "title" | "turns">): ThreadLabel {
  const title = (thread.title || "").split(/\s+/).filter(Boolean).join(" ");
  if (title) return { text: title, untitled: false };
  return { text: (thread.turns || 0) > 0 ? UNTITLED_OLD : UNTITLED_NEW, untitled: true };
}

/** A thread id as the ⋯ menu shows it: whole when short (the daemon's are
 * eight hex characters), else its first eight and an ellipsis. */
export function shortThreadId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}
