// Several chats at once (WP-B, plan docs/plans/2026-10-09-hud-workspace-
// plan.md §2.2, "Several chats at once"). Pure, like lib/workspace.ts, so every
// rule is tested without a browser.
//
// **Each chat pane holds its own conversation and its own input box.** What
// used to be one set of window-wide fields — the open thread or compose row,
// the turn being tracked, busy, the status line, the transcript, the draft,
// the tool ticker, the hand-backs and a REVIEW transcript — is now one
// `ChatState` per pane (`Chats`). The logic that drives each one is the logic
// that drove the window before, keyed by pane: in the single layout pane 1 is
// the window's conversation, exactly as it was.
//
// **A thread is open in at most one pane.** A click on a thread another pane
// shows focuses that pane; one held by a pane that is not on screen moves into
// the pane it was opened in (the two panes' conversations change places, so
// nothing either held — a running turn, a hand-back — is dropped).
//
// **Ambiguous input goes to the selected chat** (`selectedChatOf`; the
// owner's rule, decisions W-6): the chat pane most recently clicked — a
// pointer anywhere in it, keyboard focus moving into it, or a sidebar click
// that opens or focuses a thread there. Clicking a Preview, File or other
// pane does not change it. It is always a drawn chat pane: one the layout
// drops falls back to the most recently selected chat pane still drawn, a
// single chat pane is selected, and with no chat drawn there is none, so
// ambiguous input has nowhere to go and is never sent. Ambiguous input is
// input with no thread of its own — voice (AUTO and REVIEW, delivered to the
// selected chat as it is when the transcript lands), push-to-talk and the
// orb's interrupt, the follow-up listening window, files dropped outside a
// chat pane, an SSE event with no `thread_id`. Input that belongs to a thread
// — words typed in a pane's box, a steer or queued message, words handed back
// or held for a thread, a first send's result, a transcript — stays with that
// thread and is never redirected to the selected chat.
//
// **An event with a `thread_id` goes to the pane that shows that thread or
// tracks its turn** (`routeEvent`); approvals, activity and lifecycle events
// stay window-wide. An event with no thread goes to the selected chat.

import type { Attachment, ChatMessage, Thread, ToolOp } from "../types";
import type { Compose } from "./compose";
import type { GiveBack } from "./giveback";
import { PANE_NOS, panesOf, type PaneNo, type Preset, type Workspace } from "./workspace";

/** One pane's conversation, and the turn it is waiting on. */
export interface ChatState {
  /** The conversation is exactly one of: an existing thread (`threadId`), or
   * a new thread being composed (`compose`). There is no stored project;
   * lib/compose derives it, so the sidebar cannot drift from where a message
   * goes. */
  threadId: string | null;
  compose: Compose | null;
  /** The thread whose turn this pane started (or heard start) and waits on.
   * `busy` follows it, so switching the pane's thread mid-turn cannot wedge
   * it waiting for a finish it no longer listens for. */
  turnThreadId: string | null;
  busy: boolean;
  status: string;
  messages: ChatMessage[];
  draft: string;
  ops: ToolOp[];
  /** Words (and files) handed back to this pane's box, oldest first, until
   * the box takes them (lib/giveback.ts). */
  restore: GiveBack[];
  /** Set by REVIEW dictation: text handed to this pane's box, never sent. */
  pendingTranscript: string;
  /** The box's unsent words and staged files. They belong to the
   * conversation, not to the pane (review of PR #27): they move with it when
   * two panes trade conversations, and a pane that changes conversation
   * parks them (`State.drafts`) and takes up the new one's. */
  input: string;
  files: Attachment[];
  /** The thread whose transcript `messages` holds, once it has arrived (or the
   * pane wrote it: a thread it just opened from its compose row). A pane whose
   * `threadId` differs loads it; a late transcript for a thread no pane shows
   * is dropped (review of PR #27). */
  loadedThread: string | null;
}

export type Chats = Record<PaneNo, ChatState>;

/** The fields a `ChatState` has — what the test hook's legacy `patch` routes to a pane. */
export const CHAT_KEYS: readonly (keyof ChatState)[] = [
  "threadId", "compose", "turnThreadId", "busy", "status", "messages", "draft", "ops", "restore",
  "pendingTranscript", "input", "files", "loadedThread",
];

export function emptyChat(): ChatState {
  return {
    threadId: null, compose: null, turnThreadId: null, busy: false, status: "",
    messages: [], draft: "", ops: [], restore: [], pendingTranscript: "", input: "", files: [],
    loadedThread: null,
  };
}

export function emptyChats(): Chats {
  return { 1: emptyChat(), 2: emptyChat(), 3: emptyChat(), 4: emptyChat() };
}

/** Never opened: no thread, no compose row. Such a pane gets a compose row the first time it shows chat. */
export function isEmptyChat(c: ChatState): boolean {
  return !c.threadId && !c.compose;
}

/**
 * The thread a pane's conversation is on: the open thread, else the one its
 * compose row opened whose first message failed, else one it is opening right
 * now (`pending`, the window's in-flight first send).
 */
export function shownThread(c: ChatState, pending: string | null = null): string | null {
  return c.threadId ?? c.compose?.openedId ?? pending ?? null;
}

/**
 * The key a pane's conversation parks its unsent draft under when the pane
 * moves to another conversation: the thread (or the one its compose row
 * opened), else the pane's own compose row.
 */
export function draftKey(c: ChatState, pane: PaneNo): string | null {
  return c.threadId ?? c.compose?.openedId ?? (c.compose ? `compose:${pane}` : null);
}

/**
 * Whether a pane moving from `a` to `b` stays in the same conversation, so
 * its draft stays in the box: the same thread, or a compose row becoming the
 * thread it opened, or one compose row replacing another (New thread while
 * composing keeps the words, as it always did).
 */
export function sameConversation(a: ChatState, b: ChatState, pane: PaneNo): boolean {
  // A compose row that opened its thread keys by that thread, so it is the
  // same key as the thread it becomes.
  if (draftKey(a, pane) === draftKey(b, pane)) return true;
  return !a.threadId && !b.threadId && !!a.compose && !!b.compose;
}

/** Files a box stages for one message, at most (the backend's per-turn cap). */
export const MAX_FILES = 8;

/** Capture statuses: what the microphone is doing, said in the selected chat's input bar. */
export const CAPTURE_STATUSES: ReadonlySet<string> = new Set([
  "LISTENING · SPEAK NOW", "TRANSCRIBING", "STT FAILED", "DIDN'T CATCH THAT", "TOO SHORT", "MIC MUTED",
]);

/** The project a pane's conversation is in: its compose row's, else its thread's. */
export function chatProjectId(c: ChatState, threads: readonly Thread[]): string | null {
  if (c.compose) return c.compose.projectId;
  const t = c.threadId ? threads.find((x) => x.id === c.threadId) : undefined;
  return t ? t.project_id : null;
}

/** A turn is running in the thread this pane shows: Enter steers it, and the box offers Stop. */
export function runningHere(c: ChatState): boolean {
  return c.busy && !!c.threadId && c.turnThreadId === c.threadId;
}

/**
 * Which panes an SSE event concerns. `mine`: the panes showing its thread —
 * the deltas, tool calls, settled replies and model lines draw there. `ours`:
 * the panes tracking its turn — the finish and an error reach them even when
 * they have moved on to another thread, or the pane waits on THINKING with
 * the mic suppressed for good. An event with no thread is the selected chat's
 * (it was the one conversation's before there were several).
 */
export function routeEvent(
  tid: string | null | undefined,
  chats: Chats,
  pending: Readonly<Record<PaneNo, string | null>>,
  target: PaneNo | null,
): { mine: PaneNo[]; ours: PaneNo[] } {
  if (!tid) return { mine: target === null ? [] : [target], ours: [] };
  return {
    mine: PANE_NOS.filter((n) => shownThread(chats[n], pending[n]) === tid),
    ours: PANE_NOS.filter((n) => chats[n].turnThreadId === tid),
  };
}

/** Every pane an event reaches, in pane order, once each. */
export function eitherPane(r: { mine: PaneNo[]; ours: PaneNo[] }): PaneNo[] {
  return PANE_NOS.filter((n) => r.mine.includes(n) || r.ours.includes(n));
}

/**
 * The pane whose box words handed back for `threadId` go into: a chat pane
 * **on screen** showing that thread (`prefer` first, the pane they were typed
 * in), else null — they are held until a drawn chat pane shows it, never
 * written into a box the owner cannot see, which a trade of conversations
 * could then carry to another thread (review of PR #27). With no thread (a
 * compose send that failed before its thread existed) they go back to the
 * pane holding that compose row (`prefer`), else nowhere: the caller parks
 * them under that compose row. **Never the selected chat** — a hand-back
 * belongs to its conversation, and the selected chat may show another
 * thread (decisions W-6; re-review of PR #27).
 */
export function giveBackPane(
  threadId: string | null,
  chats: Chats,
  pending: Readonly<Record<PaneNo, string | null>>,
  prefer: PaneNo | null = null,
  onScreen: readonly PaneNo[] = PANE_NOS,
): PaneNo | null {
  if (!threadId) return prefer;
  const shows = (n: PaneNo) => onScreen.includes(n) && shownThread(chats[n], pending[n]) === threadId;
  if (prefer && shows(prefer)) return prefer;
  return PANE_NOS.find(shows) ?? null;
}

const isChat = (ws: Workspace, n: PaneNo) => ws.panes[n - 1].view === "chat";

/**
 * The selected chat (decisions W-6): the drawn chat pane most recently
 * selected. `order` is the chat panes the owner selected, most recent first
 * (a pointer or keyboard focus into a chat pane puts it at the front); the
 * focused pane, when it is a drawn chat pane, is the newest selection. A
 * selected pane the layout no longer draws gives way to the next most recent
 * one that is drawn, else to any drawn chat pane (one chat pane open is
 * selected); with no chat pane drawn there is no selected chat (null).
 */
export function selectedChatOf(ws: Workspace, drawn: readonly PaneNo[], order: readonly PaneNo[]): PaneNo | null {
  const ok = (n: PaneNo) => drawn.includes(n) && isChat(ws, n);
  if (ok(ws.focused)) return ws.focused;
  return order.find(ok) ?? drawn.find(ok) ?? null;
}

/**
 * Where a chat lands when nothing already shows it (plan §2.2, the click
 * rule): the focused pane if it shows chat, else the selected chat if it is
 * a drawn chat pane (else any drawn chat pane — the selected chat is one
 * whenever one is drawn, but this holds before it is known), else the focused
 * pane, which switches to chat. `switches` says whether that pane's view
 * changes.
 */
export function chatTarget(
  ws: Workspace, drawn: readonly PaneNo[], selected: PaneNo | null,
): { pane: PaneNo; switches: boolean } {
  const visible = drawn.length ? drawn : [1 as PaneNo];
  const focused = visible.includes(ws.focused) ? ws.focused : visible[0];
  if (isChat(ws, focused)) return { pane: focused, switches: false };
  if (selected !== null && visible.includes(selected) && isChat(ws, selected)) return { pane: selected, switches: false };
  const any = visible.find((n) => isChat(ws, n));
  if (any !== undefined) return { pane: any, switches: false };
  return { pane: focused, switches: true };
}

/** The drawn chat pane showing `threadId`, if any: a click on that thread just focuses it. */
export function drawnShowing(
  ws: Workspace, drawn: readonly PaneNo[], chats: Chats, threadId: string,
  pending: Readonly<Record<PaneNo, string | null>>,
): PaneNo | null {
  return drawn.find((n) => isChat(ws, n) && shownThread(chats[n], pending[n]) === threadId) ?? null;
}

/**
 * A pane other than `pane` whose conversation holds `threadId` without it
 * being on screen there (hidden, or the pane shows another view). Opening the
 * thread in `pane` swaps the two panes' conversations, so the thread is in one
 * pane only and nothing either pane held is dropped.
 */
export function heldElsewhere(
  chats: Chats, pane: PaneNo, threadId: string, pending: Readonly<Record<PaneNo, string | null>>,
): PaneNo | null {
  return PANE_NOS.find((n) => n !== pane && shownThread(chats[n], pending[n]) === threadId) ?? null;
}

/**
 * "Open beside" (Alt+click, or the row's ⋯ menu): the next drawn pane to the
 * right of the focused one. From the single layout it is two columns, pane 2
 * (VS Code's "open to the side"); from the last column of two it is a third
 * column; from the last pane of any other shape it wraps to the first drawn
 * pane that is not the focused one.
 */
export function besideTarget(
  ws: Workspace, drawn: readonly PaneNo[],
): { preset: Preset; pane: PaneNo } {
  const visible = drawn.length ? drawn : [1 as PaneNo];
  if (visible.length === 1) {
    if (ws.preset === "single") return { preset: "cols2", pane: 2 };
    // A preset drawn as one pane for room: the next of its own panes.
    const own = panesOf(ws.preset);
    const at = own.indexOf(visible[0]);
    return { preset: ws.preset, pane: own[(at + 1) % own.length] };
  }
  const at = visible.indexOf(ws.focused);
  const from = at < 0 ? 0 : at;
  if (from < visible.length - 1) return { preset: ws.preset, pane: visible[from + 1] };
  if (ws.preset === "cols2" && visible.length === 2) return { preset: "cols3", pane: 3 };
  return { preset: ws.preset, pane: visible.find((n) => n !== ws.focused) ?? visible[0] };
}

/**
 * The panes whose conversations the sidebar marks: every drawn chat pane, and
 * the last selected chat's always — it is the conversation the window is about
 * even while its pane shows a task or a file, as the one conversation was
 * marked whatever the single pane showed.
 */
export function sidebarPanes(ws: Workspace, drawn: readonly PaneNo[], active: PaneNo): PaneNo[] {
  return PANE_NOS.filter((n) => n === active || (drawn.includes(n) && isChat(ws, n)));
}

/**
 * The compose rows the sidebar draws: one per marked pane that is composing,
 * numbered by pane when more than one pane is marked (so the number says
 * which pane the new thread is in).
 */
export function composeRows(
  ws: Workspace, drawn: readonly PaneNo[], chats: Chats, active: PaneNo,
): { pane: PaneNo; compose: Compose; label: string; active: boolean }[] {
  const panes = sidebarPanes(ws, drawn, active);
  const numbered = panes.length > 1;
  return panes
    .filter((n) => !!chats[n].compose && !chats[n].threadId)
    .map((n) => ({
      pane: n, compose: chats[n].compose!, label: numbered ? `New thread · ${n}` : "New thread", active: n === active,
    }));
}

/** The threads the sidebar marks as open: each marked pane's, the active one (the selected chat) flagged. */
export function openRows(
  ws: Workspace, drawn: readonly PaneNo[], chats: Chats, active: PaneNo,
): { pane: PaneNo; threadId: string; active: boolean }[] {
  return sidebarPanes(ws, drawn, active)
    .filter((n) => !!chats[n].threadId)
    .map((n) => ({ pane: n, threadId: chats[n].threadId!, active: n === active }));
}
