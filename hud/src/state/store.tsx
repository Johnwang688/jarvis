// One store for the window. Panels read it and dispatch into it; the SSE
// subscription is the only thing that writes lifecycle state, which is the
// §10.3 rule carried into the frontend: **the status record drives the task
// view, never the model's prose.**
//
// Several chats at once (WP-B, 2026-10-09): the conversation and the turn it
// waits on are per chat pane (`chats`, lib/chats.ts), and the actions that
// write them name their pane. Approvals, activity, the projects and threads,
// the orb, the level meter, the error line and the pickers stay window-wide.
// The orb follows **the selected chat's** turn: a turn event in another pane
// never repaints it.

import { applyEvent, fold, historyOps, outcome, type TranscriptEvent } from "../lib/toolCalls";
import type { ToolEntry, ToolOutcome } from "../types";
import React, { createContext, useContext, useReducer } from "react";
import type {
  ApprovalRequest, Attachment, AvatarDesc, ChatMessage, Project, Schedule, Task, TaskThread,
  Thread, ToolOp, Usage, RouteView, DiscordStatus } from "../types";
import { DEFAULT_MODE, type DictationMode } from "../lib/dictation";
import { activeProjectId } from "../lib/compose";
import { NO_ACTIVITY, applyActivity, type ActivityView } from "../lib/activity";
import { pendingAfter } from "../lib/giveback";
import {
  CAPTURE_STATUSES, CHAT_KEYS, MAX_FILES, ORPHAN_DRAFT, draftKey, emptyChat, emptyChats, sameConversation, shownThread,
  type ChatState, type Chats,
} from "../lib/chats";
import { PANE_NOS, defaultWorkspace, type PaneNo } from "../lib/workspace";
import { applyTurn, turnsAfterActivity, turnsAfterThreads, type TurnEvent, type Turns } from "../lib/thinking";

// What the centre shows is the workspace's (lib/workspace.ts, 2026-10-09): a
// view per pane, stored with the layout, not a single `tab` in this store.
export type OrbState =
  | "idle" | "listening" | "transcribing" | "thinking"
  | "composing" | "tool" | "approval" | "speaking" | "error";

/** Unsent words and files a conversation left behind (`State.drafts`). */
export interface Draft {
  text: string;
  files: Attachment[];
  projects?: string[];
}

export interface State {
  projects: Project[];
  platforms: Record<string, { platform: string; note: string | null }>;
  threads: Thread[];
  tasks: Task[];
  taskThreads: Record<string, TaskThread[]>;
  /** The sidebar dots: what each thread and task is doing (lib/activity.ts). */
  activity: ActivityView;
  /** Each thread's running turn as this window knows it, keyed by thread and
   * window-wide (WP-H, lib/thinking.ts): the thinking line at the foot of
   * whichever pane shows it, fed by the turn events and the activity records. */
  turns: Turns;
  /** Each chat pane's conversation and the turn it waits on (lib/chats.ts). */
  chats: Chats;
  /** The selected chat (decisions W-6, lib/chats `selectedChatOf`): the drawn
   * chat pane most recently clicked, where ambiguous input goes — voice, the
   * orb, a no-thread event. Null with no chat pane drawn: such input then has
   * nowhere to go and is never sent. */
  selectedChat: PaneNo | null;
  /** The chat panes the owner selected, most recent first. */
  selectedOrder: PaneNo[];
  /** The pane in slot 1 (App's to say, from the workspace): the chat the
   * window is about when no chat was ever selected — never an id assumed to
   * exist (pane 1 may have been closed, WP-G2). */
  home: PaneNo;
  /** Turns still running in a thread no pane shows, because the pane that
   * tracked them was closed (WP-G2): thread id → its status line. The pane
   * that opens that thread next takes the turn up, Stop and all. */
  loose: Record<string, string>;
  /** Unsent words and staged files a conversation left behind when its pane
   * moved to another one, by `draftKey` (a thread id, or a pane's compose
   * row): they come back when a pane takes that conversation up again, and
   * are never left in a box now showing another thread (review of PR #27).
   * `projects`: the projects of the compose rows whose words wait under
   * `ORPHAN_DRAFT`, so the new thread that takes them up can say which they
   * were written in (review 2 of PR #47). */
  drafts: Record<string, Draft>;
  /** The last compose row id minted (`Compose.id`, `c<n>`): kept here so the
   * reducer stays pure. */
  composeSeq: number;
  /** Exact draft-location notice owned by the reducer, replaceable after takeup. */
  orphanNotice?: string;
  taskId: string | null;
  /** A task was picked more recently than a thread: the File tab follows it. */
  taskFocus: boolean;
  approvals: ApprovalRequest[];
  usage: Usage | null;
  /** The Discord light (PR A): `GET /discord`, refetched on `discord_status`. */
  discord: DiscordStatus | null;
  schedules: Schedule[];
  route: RouteView | null;
  avatar: AvatarDesc | null;
  wakePatterns: RegExp[];
  dictation: DictationMode;
  level: number;
  orb: OrbState;
  /** The window's one error line: a failure, or where unsent words went. */
  error: string;
  /** When `error` was last written (ms; 0 when empty): a notice arriving
   * within `NOTICE_BREATH_MS` of that joins the line rather than replacing it. */
  errorAt: number;
  /** A refused thread move, shown beside the tree it was reverted in. */
  moveError: string;
  picker: null | "model" | "voice" | "avatar" | "route" | "settings" | "newProject" | "newTask" | "schedule"
    | "editProject" | "archiveProject" | "archive"
    // ED-7: quick open and the command palette. Pickers like the rest:
    // push-to-talk refuses and the mic claims nothing while one is open.
    | "quickOpen" | "palette"
    // ED-8: the editor settings.
    | "editorSettings"
    // VX-3: a new VEX project from the extension's templates.
    | "newVexProject";
  /** Archived projects' names, from `/archive`: `/projects` hides them, but
   * the backend still counts them when it numbers a name (decisions B4). */
  archivedNames: string[];
}

export const initialState: State = {
  projects: [], platforms: {}, threads: [], tasks: [], taskThreads: {}, activity: NO_ACTIVITY, turns: {},
  chats: emptyChats(), selectedChat: null, selectedOrder: [], home: defaultWorkspace().order[0], loose: {},
  drafts: {}, composeSeq: 0, taskId: null, taskFocus: false,
  approvals: [], usage: null, discord: null, schedules: [],
  route: null, avatar: null, wakePatterns: [], dictation: DEFAULT_MODE,
  level: 0, orb: "idle", error: "", errorAt: 0, moveError: "", picker: null, archivedNames: [],
};

/**
 * What a pane's patch may carry besides its own fields: the orb (applied only
 * for the selected chat's pane) and the window's error line. `takeOrphan`:
 * this is a new thread the owner started — New thread, or a split's copy —
 * so a new compose row takes up the words parked under `ORPHAN_DRAFT`. Only
 * those two say so: any other new compose row (a pane shown for the first
 * time, a re-aim after a project or thread went away, a pane nobody can see)
 * leaves them parked, since "back, in this new thread's box" must be true
 * (review 3 of PR #47).
 */
export type ChatPatch = Partial<ChatState> & { orb?: OrbState; error?: string; takeOrphan?: boolean };

export type Action =
  | { type: "patch"; patch: Partial<State> }
  /** One pane's conversation fields; `orb` lands only when it is the selected chat. */
  | { type: "chat"; pane: PaneNo; patch: ChatPatch }
  | { type: "message"; pane: PaneNo; message: ChatMessage }
  /** Patch the message this window drew as `local`, or the daemon's `message_id`, in whichever pane holds it. */
  | { type: "mark"; local?: string; message_id?: string; patch: Partial<ChatMessage> }
  /** Take back a message this window drew optimistically (its send failed). */
  | { type: "unmessage"; local: string }
  | { type: "delta"; pane: PaneNo; text: string }
  | { type: "settle"; pane: PaneNo; text: string; at?: string; turn_id?: string }
  | { type: "op_start"; pane: PaneNo; op: ToolOp }
  | { type: "op_done"; pane: PaneNo; call_id: string; ok: boolean; summary: string; turn_id?: string; outcome?: ToolOutcome; at?: string }
  | { type: "tool_stop"; pane: PaneNo; turn_id?: string; at: string }
  | { type: "transcript"; threadId: string; rows: (ChatMessage | ToolEntry)[] }
  | { type: "approval_add"; request: ApprovalRequest }
  | { type: "approval_drop"; req_id: string }
  | { type: "task_upsert"; task: Task }
  | { type: "thread_patch"; id: string; patch: Partial<Thread> }
  | { type: "activity"; record: any; at?: number }
  /** A turn event for one thread (lib/thinking `applyTurn`), whichever pane shows it. */
  | ({ type: "turn" } & TurnEvent)
  /** Hand words back to a pane's box. */
  | { type: "give_back"; pane: PaneNo; text: string; files: Attachment[]; nonce: number }
  /** That pane's box took every hand-back up to `nonce`. */
  | { type: "given_back"; pane: PaneNo; nonce: number }
  /** Open `threadId` in `pane` (nothing else holds it). A turn in it another
   * pane was tracking after moving on comes with it, so Stop and the finish
   * reach the pane that shows it; a turn of the opening pane's own trades
   * places with it. */
  | { type: "open_thread"; pane: PaneNo; threadId: string }
  /** The box's unsent words or staged files changed. */
  | { type: "input"; pane: PaneNo; text?: string; files?: Attachment[] }
  /** Files read in for conversation `key` (a drop, the picker, a paste): added
   * to the box of the pane holding that conversation when they are ready,
   * else to its parked draft — never to whatever the pane they were dropped
   * on shows by then (re-review of PR #27). */
  | { type: "stage"; key: string; files: Attachment[]; atomic?: boolean }
  | { type: "restore_compose"; pane: PaneNo; composeId: string }
  | { type: "notice"; text: string }
  /** Words for a conversation no pane shows: kept with its parked draft
   * (`projectId`: the compose row's, for words parked under `ORPHAN_DRAFT`). */
  | { type: "park"; key: string; text: string; files?: Attachment[]; projectId?: string | null }
  /** Two panes' conversations change places (a thread opened in one while the other held it off screen). */
  | { type: "chat_swap"; a: PaneNo; b: PaneNo }
  /** The selected chat changed (`front`: the owner selected it, so it heads
   * the order; else a fallback while the layout hides the one selected). The
   * orb now follows that pane's turn. */
  | { type: "select"; pane: PaneNo | null; front?: boolean }
  /** The pane in slot 1 changed (a close, a split, ⊞). */
  | { type: "home"; pane: PaneNo }
  /**
   * × on a pane (WP-G2, plan §3.6), or a split taking back a pane ⊞ hid: its
   * conversation leaves it. Unsent words and files in a thread's box are
   * parked under that thread and come back when it opens again. Words in a
   * compose row's box were asked about on a × and are dropped; a hidden pane
   * could not ask, so with `keep` they — and words handed back to it — are
   * parked for the next new thread (`ORPHAN_DRAFT`; review of PR #47). A
   * park is never silent: the window's error line says where the words went,
   * and how many files past `MAX_FILES` were left out (review 2 of PR #47).
   * A turn it tracked is left `loose` for the next pane that opens the
   * thread. The pane leaves the selection order; a capture status stays
   * until the selection moves on (`select` carries it to the next chat).
   */
  | { type: "release_chat"; pane: PaneNo; keep?: boolean }
  /** A turn running in a thread no pane shows (a first send whose pane was closed meanwhile). */
  | { type: "loose"; threadId: string; status: string }
  /** A turn finished in a thread no pane tracks: nothing is loose there any more. */
  | { type: "turn_over"; threadId: string };

function withChat(s: State, pane: PaneNo, next: ChatState): State {
  return { ...s, chats: { ...s.chats, [pane]: next } };
}

/**
 * `pane` moves from conversation `was` to `next`: when that is another
 * conversation, the box's unsent words and files are parked under the old
 * one's key and the new one's parked draft (if any) comes back — a draft is
 * never left in a box that now shows another thread. `takeOrphan` (the
 * owner started this new thread: `ChatPatch.takeOrphan`) lets it take up the
 * words parked under `ORPHAN_DRAFT`; nothing else does.
 */
function reconverse(
  s: State, pane: PaneNo, was: ChatState, next: ChatState, takeOrphan = false,
): { next: ChatState; drafts: State["drafts"]; notice?: string; orphan?: boolean } {
  // A new thread the owner is starting: a compose row that has opened nothing yet.
  const fresh = takeOrphan && !next.threadId && !!next.compose && !next.compose.openedId;
  if (sameConversation(was, next, pane)) {
    // New thread while composing keeps the words, as it always did — and, a
    // new row (a new compose id), it also takes up words no pane's row holds
    // any more, added to what the box has (review 2 of PR #47: it returned
    // here, so they waited for a new thread that never came).
    const renewed = fresh && !was.threadId && !!was.compose && next.compose!.id !== was.compose.id;
    if (renewed && s.drafts[ORPHAN_DRAFT]) return orphanTaken(s, next, { ...s.drafts });
    return { next, drafts: s.drafts };
  }
  const drafts = { ...s.drafts };
  const out = draftKey(was, pane);
  if (out && (was.input || was.files.length)) drafts[out] = { text: was.input, files: was.files };
  const into = draftKey(next, pane);
  // A new thread with nothing of its own parked takes up words no pane's row
  // holds any more (review of PR #47): the next new thread, wherever.
  if (into && !drafts[into] && fresh && drafts[ORPHAN_DRAFT]) {
    return orphanTaken(s, { ...next, input: "", files: [] }, drafts);
  }
  const parked = into ? drafts[into] : undefined;
  if (into && parked) delete drafts[into];
  const notice = parked && fresh && drafts[ORPHAN_DRAFT]
    ? "This pane's draft is back. Other unsent words are still kept for the next new thread you start." : undefined;
  return { next: { ...next, input: parked?.text ?? "", files: parked?.files ?? [] }, drafts, notice };
}

/** A parked draft's projects with one more, once each (undefined: none). */
function withProject(had: string[] | undefined, id: string | null | undefined): string[] | undefined {
  if (!id) return had;
  return had?.includes(id) ? had : [...(had ?? []), id];
}

/** A project's name for a sentence, or a plain stand-in when it is gone. */
function projectName(s: State, id: string | null | undefined): string {
  return (id && s.projects.find((p) => p.id === id)?.name) || "a project no longer listed";
}

/** "1 file was" / "3 files were": for the notices below. */
function filesLeftOut(n: number): string {
  return n ? ` ${n} file${n === 1 ? " was" : "s were"} left out: a message holds ${MAX_FILES}.` : "";
}

/**
 * The words parked under `ORPHAN_DRAFT` go into `next`'s box (a new compose
 * row), after anything already in it, and the window says so (review 2 of
 * PR #47) — naming the project they were written in when the new thread is
 * in another. The row keeps the project it was opened in (a split's copy is
 * its source's, S-2; New thread names one), so nothing moves under the owner;
 * the project chip is one click away. Files past `MAX_FILES` are left out,
 * and said to be.
 */
function orphanTaken(
  s: State, next: ChatState, drafts: State["drafts"],
): { next: ChatState; drafts: State["drafts"]; notice: string; orphan: boolean } {
  const o = drafts[ORPHAN_DRAFT];
  delete drafts[ORPHAN_DRAFT];
  const text = next.input && o.text ? `${next.input}\n${o.text}` : next.input || o.text;
  const all = [...next.files, ...o.files];
  const files = all.slice(0, MAX_FILES);
  const were = o.projects ?? [];
  const now = next.compose?.projectId ?? null;
  const elsewhere = were.some((p) => p !== now);
  const wrote = elsewhere ? ` (written in ${were.map((p) => projectName(s, p)).join(" and ")})` : "";
  const where = elsewhere ? ` This new thread is in ${projectName(s, now)}; its project chip can move it.` : "";
  const notice = `Words you had not sent${wrote} are back, in this new thread's box.${where}`
    + filesLeftOut(all.length - files.length);
  return { next: { ...next, input: text, files }, drafts, notice, orphan: true };
}

/**
 * How close together writes to the error line must come for a notice to join
 * it rather than replace it: the dispatches of one handler, and the effects
 * its render runs, land well inside this; two things the owner did apart do not.
 */
export const NOTICE_BREATH_MS = 1000;

/** A plain write to the error line (a failure, or clearing it): it replaces what was there. */
function wrote(s: State, error: string): State {
  return { ...s, error, errorAt: error ? Date.now() : 0, orphanNotice: undefined };
}

/**
 * A notice of where unsent words went. There is one error line, so a notice
 * arriving in the same breath as what is already on it is added to it, never
 * written over it (review 3 of PR #47, LOW): a split that reuses a hidden pane
 * says both that its words were kept and that the new thread took them.
 */
function noticed(s: State, notice: string, orphan = false, append = false): State {
  const now = Date.now();
  // Replace only our own exact location notice; an unrelated refusal is retained.
  const previous = orphan && s.orphanNotice && s.error.includes(s.orphanNotice) ? s.orphanNotice : null;
  const together = !!s.error && (append || now - s.errorAt < NOTICE_BREATH_MS);
  const error = previous ? s.error.replace(previous, notice)
    : !together ? notice : s.error.includes(notice) ? s.error : `${s.error} ${notice}`;
  return { ...s, error, errorAt: now, orphanNotice: orphan ? notice
    : s.orphanNotice && error.includes(s.orphanNotice) ? s.orphanNotice : undefined };
}

/** The orb for a pane's turn, as the reducers below leave it. */
function turnOrb(s: State, c: ChatState): OrbState {
  if (s.approvals.length) return "approval";
  if (c.ops.some((o) => !o.finished)) return "tool";
  return c.busy ? "thinking" : "idle";
}

/** Record pending events only while this conversation's history is loading. */
function transcriptEvent(c: ChatState, event: TranscriptEvent): ChatState {
  return { ...c, messages: applyEvent(c.messages, event),
    toolReplay: c.threadId && c.loadedThread !== c.threadId ? [...c.toolReplay, event] : c.toolReplay };
}

export function reduce(s: State, a: Action): State {
  switch (a.type) {
    case "patch": {
      let next = { ...s, ...a.patch };
      // The activity snapshot (`GET /activity`) and the thread records feed
      // the thinking line too: a thread that turned working is a turn noticed
      // late, and a record's `running_since` is when it really began.
      if (a.patch.activity && a.patch.activity !== s.activity) {
        next = { ...next, turns: turnsAfterActivity(next.turns, s.activity, next.activity, next.threads, Date.now()) };
      }
      if (a.patch.threads) next = { ...next, turns: turnsAfterThreads(next.turns, next.threads) };
      return a.patch.error !== undefined && a.patch.errorAt === undefined ? wrote(next, a.patch.error) : next;
    }

    case "turn": {
      const { type: _t, ...event } = a;
      const turns = applyTurn(s.turns, event as TurnEvent);
      return turns === s.turns ? s : { ...s, turns };
    }

    case "chat": {
      const { orb, error, takeOrphan, ...fields } = a.patch;
      // A compose row arriving without an identity is a new row: it gets one.
      let composeSeq = s.composeSeq ?? 0;
      if (fields.compose && !fields.compose.id) {
        composeSeq += 1;
        fields.compose = { ...fields.compose, id: `c${composeSeq}` };
      }
      const was = s.chats[a.pane];
      const moved = reconverse(s, a.pane, was, { ...was, ...fields }, !!takeOrphan);
      let next = { ...withChat(s, a.pane, moved.next), drafts: moved.drafts, composeSeq };
      if (orb !== undefined && a.pane === s.selectedChat) next = { ...next, orb };
      if (error !== undefined) next = wrote(next, error);
      // Words taken up from a pane that let them go are never back unsaid.
      if (moved.notice) next = noticed(next, moved.notice, moved.orphan);
      return next;
    }

    case "park": {
      const add = a.files ?? [];
      if (!a.text && !add.length) return s;
      const had = s.drafts[a.key];
      const text = had?.text && a.text ? `${had.text}\n${a.text}` : a.text || had?.text || "";
      const draft: Draft = { text, files: [...(had?.files ?? []), ...add] };
      const projects = withProject(had?.projects, a.projectId);
      if (projects) draft.projects = projects;
      return { ...s, drafts: { ...s.drafts, [a.key]: draft } };
    }

    case "input": {
      const c = s.chats[a.pane];
      return withChat(s, a.pane, { ...c, input: a.text ?? c.input, files: a.files ?? c.files });
    }

    case "notice":
      return a.text ? noticed(s, a.text, false, true) : s;

    case "restore_compose": {
      const c = s.chats[a.pane];
      if (c.threadId || !c.compose || c.compose.openedId || c.compose.id !== a.composeId) return s;
      const key = `compose:${a.pane}`;
      const had = s.drafts[key];
      if (!had) return s;
      const drafts = { ...s.drafts };
      delete drafts[key];
      const all = [...c.files, ...had.files];
      const text = c.input && had.text ? `${c.input}\n${had.text}` : c.input || had.text;
      const next = { ...withChat(s, a.pane, { ...c, input: text, files: all.slice(0, MAX_FILES) }), drafts };
      return noticed(next, "Words that were not sent are back in this box." + filesLeftOut(Math.max(0, all.length - MAX_FILES)));
    }

    case "stage": {
      if (!a.files.length) return s;
      const n = PANE_NOS.find((p) => draftKey(s.chats[p], p) === a.key);
      const had = s.drafts[a.key];
      const all = [...(n !== undefined ? s.chats[n].files : had?.files ?? []), ...a.files];
      // A page picture and its opaque text reference are one owner-selected
      // capture. Capacity changes while export runs must never split the pair.
      if (a.atomic && all.length > MAX_FILES)
        return noticed(s, "This capture needs two attachment slots: remove a file and try again.");
      const files = all.slice(0, MAX_FILES);
      const next = n !== undefined ? withChat(s, n, { ...s.chats[n], files })
        : { ...s, drafts: { ...s.drafts, [a.key]: { ...had, text: had?.text ?? "", files } } };
      return all.length > MAX_FILES ? noticed(next, filesLeftOut(all.length - MAX_FILES).trim()) : next;
    }

    case "message":
      // The owner's own line goes up verbatim; his is rendered. That decision
      // lives in the view, not here — the store keeps the role.
      return withChat(s, a.pane, transcriptEvent(s.chats[a.pane], { kind: "message", message: a.message }));

    case "mark": {
      const event: TranscriptEvent = { kind: "mark", local: a.local, message_id: a.message_id, patch: a.patch };
      const chats = { ...s.chats };
      let changed = false;
      for (const n of PANE_NOS) {
        const c = chats[n];
        if (c.messages.some(m => (!!a.local && m.local === a.local) || (!!a.message_id && m.message_id === a.message_id))
          || (c.threadId && c.loadedThread !== c.threadId)) { chats[n] = transcriptEvent(c, event); changed = true; }
      }
      return changed ? { ...s, chats } : s;
    }

    case "unmessage": {
      // Only that bubble: a reply that settled while the send was failing
      // stays (restoring a snapshot taken before the send used to drop it).
      const chats = { ...s.chats };
      for (const n of PANE_NOS) {
        if (!chats[n].messages.some((m) => m.local === a.local)) continue;
        chats[n] = transcriptEvent(chats[n], { kind: "remove", local: a.local });
      }
      return { ...s, chats };
    }

    case "delta": {
      // Plain text while drafting: half a markdown document is not markdown,
      // and rendering `**bold` mid-word flickers.
      const c = s.chats[a.pane];
      return withChat(s, a.pane, { ...c, draft: c.draft + a.text });
    }

    case "settle": {
      // The finished reply replaces the draft and is rendered once.
      const c = s.chats[a.pane];
      const text = a.text || c.draft;
      if (!text) return withChat(s, a.pane, { ...c, draft: "" });
      return withChat(s, a.pane, transcriptEvent({ ...c, draft: "" },
        { kind: "message", message: { role: "assistant", text, at: a.at, turn_id: a.turn_id } }));
    }

    case "op_start": {
      const c = s.chats[a.pane];
      const turn = a.op.turn_id ?? c.toolTurnId ?? "";
      const op = { ...a.op, turn_id: turn };
      const ops = c.ops.some(o => o.call_id === op.call_id && o.turn_id === turn) ? c.ops : [...c.ops.slice(-60), op];
      const next = withChat(s, a.pane, transcriptEvent({ ...c, ops, draft: "", toolTurnId: turn },
        { kind: "start", call: { call_id: op.call_id, turn_id: turn, name: op.name, label: op.label ?? "",
          at: op.at ?? new Date(op.started).toISOString(), outcome: "running" } }));
      if (a.pane !== s.selectedChat) return next;
      return { ...next, orb: s.orb === "approval" ? s.orb : "tool" };
    }

    case "op_done": {
      const c = s.chats[a.pane];
      const turn = a.turn_id ?? c.toolTurnId ?? "";
      const ops = c.ops.map((o) =>
        o.call_id === a.call_id && (o.turn_id ?? "") === turn && !o.finished
          ? { ...o, finished: Date.now(), ok: a.ok, summary: a.summary }
          : o,
      );
      const next = withChat(s, a.pane, transcriptEvent({ ...c, ops }, { kind: "finish",
        turn_id: turn, call_id: a.call_id, outcome: outcome(a.outcome, a.ok), at: a.at ?? new Date().toISOString() }));
      if (a.pane !== s.selectedChat) return next;
      const running = ops.some((o) => !o.finished);
      // The window falls back to THINKING when the running count hits zero —
      // v1's tool_done half, which is what stopped the slowest seconds of a
      // turn reading as "still running gmail_search".
      const orb = s.orb === "approval" ? s.orb : running ? "tool" : c.busy ? "thinking" : "idle";
      return { ...next, orb };
    }

    case "tool_stop": {
      const c = s.chats[a.pane];
      const ops = c.ops.map(o => !o.finished && (a.turn_id === undefined || o.turn_id === a.turn_id)
        ? { ...o, finished: Date.parse(a.at) || Date.now(), ok: false } : o);
      const next = transcriptEvent({ ...c, ops, toolTurnId: a.turn_id === undefined || c.toolTurnId === a.turn_id ? null : c.toolTurnId },
        { kind: "stop", turn_id: a.turn_id, at: a.at });
      return withChat(s, a.pane, next);
    }
    case "transcript": {
      const n = PANE_NOS.find(p => s.chats[p].threadId === a.threadId);
      if (n === undefined) return s;
      const c = s.chats[n];
      const messages = fold(a.rows, c.toolReplay);
      const ops = historyOps(messages, Date.now());
      // A finish heard during the request wins over a stale running snapshot.
      const active = [...ops].reverse().find(o => !o.finished)?.turn_id ?? null;
      return withChat(s, n, { ...c, messages, ops, loadedThread: a.threadId, toolReplay: [],
        toolTurnId: c.toolTurnId ?? active,
        status: c.turnThreadId === a.threadId ? c.status : "" });
    }

    case "approval_add": {
      if (s.approvals.some((r) => r.req_id === a.request.req_id)) return s;
      return { ...s, approvals: [...s.approvals, a.request], orb: "approval" };
    }

    case "approval_drop": {
      const approvals = s.approvals.filter((r) => r.req_id !== a.req_id);
      const busy = s.selectedChat !== null && s.chats[s.selectedChat].busy;
      const orb = approvals.length === 0 && s.orb === "approval" ? (busy ? "thinking" : "idle") : s.orb;
      return { ...s, approvals, orb };
    }

    case "task_upsert": {
      const i = s.tasks.findIndex((t) => t.id === a.task.id);
      const tasks = i < 0 ? [...s.tasks, a.task] : s.tasks.map((t) => (t.id === a.task.id ? a.task : t));
      return { ...s, tasks };
    }

    case "thread_patch":
      // One thread's fields, merged into the list as it is *now*. A caller
      // that wrote back a whole `threads` array it captured earlier (a click
      // handler's closure) would undo every update that landed in between.
      if (!s.threads.some((t) => t.id === a.id)) return s;
      return { ...s, threads: s.threads.map((t) => (t.id === a.id ? { ...t, ...a.patch, id: t.id } : t)) };

    case "activity": {
      // Folded into the map as it is now, for the thread_patch reason.
      const activity = applyActivity(s.activity, a.record);
      if (activity === s.activity) return s;
      return { ...s, activity, turns: turnsAfterActivity(s.turns, s.activity, activity, s.threads, a.at ?? Date.now()) };
    }

    case "give_back": {
      if (!a.text && !a.files.length) return s;
      const c = s.chats[a.pane];
      return withChat(s, a.pane, { ...c, restore: [...c.restore, { text: a.text, files: a.files, nonce: a.nonce }] });
    }

    case "given_back": {
      const c = s.chats[a.pane];
      const left = pendingAfter(c.restore, a.nonce);
      return left.length === c.restore.length ? s : withChat(s, a.pane, { ...c, restore: left });
    }

    case "open_thread": {
      const chats = { ...s.chats };
      let loose = s.loose;
      const was = chats[a.pane];
      // A compose row that already opened this thread is the same
      // conversation: what it shows is that thread's, so nothing reloads.
      const same = was.compose?.openedId === a.threadId;
      const moved = reconverse(s, a.pane, was, {
        ...was, threadId: a.threadId, compose: null, loadedThread: same ? a.threadId : was.loadedThread,
        ...(same || was.threadId === a.threadId ? {} : { messages: [], ops: [], draft: "", toolReplay: [], toolTurnId: null }),
      });
      const here = { ...moved.next };
      for (const n of PANE_NOS) {
        if (n === a.pane) continue;
        const q = chats[n];
        // Another pane started this thread's turn and then moved on to another
        // thread: the turn comes with the thread, so the pane showing it has
        // its Stop. If this pane was waiting on a turn of its own, the two
        // trade: that turn is off screen here now, as this one was there, and
        // the other pane still hears it finish (review of PR #27 — keeping it
        // here left the running thread with no Stop anywhere).
        if (q.busy && q.turnThreadId === a.threadId && q.threadId !== a.threadId) {
          const mine = here.busy && here.turnThreadId !== a.threadId
            ? { busy: true, turnThreadId: here.turnThreadId, status: here.status }
            : { busy: false, turnThreadId: null, status: "" };
          here.busy = true;
          here.turnThreadId = a.threadId;
          here.status = q.status;
          chats[n] = { ...q, ...mine };
        }
      }
      // A turn left running by a pane that was closed (WP-G2): this pane
      // takes it up, so its Stop is here. A turn of this pane's own on another
      // thread is left loose in its place, for whichever pane opens that one.
      if (a.threadId in loose) {
        loose = { ...loose };
        const status = loose[a.threadId];
        delete loose[a.threadId];
        const tracked = PANE_NOS.some((n) => chats[n].busy && chats[n].turnThreadId === a.threadId)
          || (here.busy && here.turnThreadId === a.threadId);
        if (!tracked) {
          if (here.busy && here.turnThreadId) loose[here.turnThreadId] = here.status;
          here.busy = true;
          here.turnThreadId = a.threadId;
          here.status = status;
        }
      }
      chats[a.pane] = here;
      return { ...s, chats, drafts: moved.drafts, loose };
    }

    case "release_chat": {
      const c = s.chats[a.pane];
      const drafts = { ...s.drafts };
      const thread = shownThread(c);
      let dropped = 0;
      const park = (key: string, text: string, files: Attachment[], projectId: string | null = null) => {
        if (!text && !files.length) return;
        const had = drafts[key];
        const all = [...(had?.files ?? []), ...files];
        dropped += Math.max(0, all.length - MAX_FILES);
        const draft: Draft = {
          text: had?.text && text ? `${had.text}\n${text}` : text || had?.text || "",
          files: all.slice(0, MAX_FILES),
        };
        const projects = withProject(had?.projects, projectId);
        if (projects) draft.projects = projects;
        drafts[key] = draft;
      };
      // Where the words went is said, never left for the owner to discover
      // (review 2 of PR #47): the error line, as a failed send says it.
      let notice = "";
      const unsent = !!c.input || c.files.length > 0 || c.restore.length > 0;
      if (thread) {
        park(thread, c.input, c.files);
        if (unsent) {
          const title = s.threads.find((t) => t.id === thread)?.title;
          notice = `What you had not sent waits with ${title ? `“${title}”` : "its thread"}: it is back in the box when you open that thread.`;
        }
      } else if (a.keep && c.compose) {
        const projectId = c.compose.projectId;
        park(ORPHAN_DRAFT, c.input, c.files, projectId);
        for (const r of c.restore) park(ORPHAN_DRAFT, r.text, r.files, projectId);
        if (unsent) {
          notice = `A hidden pane was reused, and what you had not sent in it (a new thread in ${projectName(s, projectId)})`
            + " is kept for the next new thread you start.";
        }
      }
      let orphanText = !thread && a.keep && c.compose ? notice : "";
      if (orphanText) notice = "";
      const oldCompose = drafts[`compose:${a.pane}`];
      if (oldCompose) {
        delete drafts[`compose:${a.pane}`];
        park(ORPHAN_DRAFT, oldCompose.text, oldCompose.files);
        const saved = drafts[ORPHAN_DRAFT];
        for (const project of saved ? oldCompose.projects ?? [] : []) saved.projects = withProject(saved.projects, project);
        orphanText += (orphanText ? " " : "") + "Previously unsent words are kept for the next new thread you start.";
      }
      if (dropped) {
        if (orphanText) orphanText += filesLeftOut(dropped);
        else if (notice) notice += filesLeftOut(dropped);
      }
      const loose = { ...s.loose };
      // A placeholder (`local-N`, a first send whose thread does not exist
      // yet) is no thread: nothing can open it.
      if (c.busy && c.turnThreadId && !c.turnThreadId.startsWith("local-")) loose[c.turnThreadId] = c.status;
      const keep = a.pane === s.selectedChat && CAPTURE_STATUSES.has(c.status) ? c.status : "";
      const next: State = {
        ...s, drafts, loose,
        chats: { ...s.chats, [a.pane]: { ...emptyChat(), status: keep } },
        selectedOrder: s.selectedOrder.filter((n) => n !== a.pane),
      };
      const said = notice ? noticed(next, notice) : next;
      return orphanText ? noticed(said, orphanText, true) : said;
    }

    case "loose":
      if (s.loose[a.threadId] === a.status) return s;
      return { ...s, loose: { ...s.loose, [a.threadId]: a.status } };

    case "turn_over": {
      if (!(a.threadId in s.loose)) return s;
      const loose = { ...s.loose };
      delete loose[a.threadId];
      return { ...s, loose };
    }

    case "home":
      return s.home === a.pane ? s : { ...s, home: a.pane };

    case "chat_swap": {
      if (a.a === a.b) return s;
      return { ...s, chats: { ...s.chats, [a.a]: s.chats[a.b], [a.b]: s.chats[a.a] } };
    }

    case "select": {
      const order = a.front && a.pane !== null && s.selectedOrder[0] !== a.pane
        ? [a.pane, ...s.selectedOrder.filter((n) => n !== a.pane)]
        : s.selectedOrder;
      if (a.pane === s.selectedChat) return order === s.selectedOrder ? s : { ...s, selectedOrder: order };
      let next: State = { ...s, selectedChat: a.pane, selectedOrder: order };
      // What the microphone said belongs to the selected chat's input bar: it
      // moves with the selection (onto an idle pane) and never stays behind in
      // a pane that no longer draws the strip (review of PR #27).
      const was = s.selectedChat;
      const old = was === null ? null : s.chats[was];
      if (was !== null && old && CAPTURE_STATUSES.has(old.status)) {
        const chats = { ...s.chats, [was]: { ...old, status: "" } };
        if (a.pane !== null && !chats[a.pane].status) chats[a.pane] = { ...chats[a.pane], status: old.status };
        next = { ...next, chats };
      }
      // What the microphone is doing belongs to the window, whichever pane it
      // will speak into; everything else is the selected chat's turn.
      if (s.orb === "listening" || s.orb === "transcribing" || s.orb === "speaking") return next;
      if (a.pane === null) return { ...next, orb: next.approvals.length ? "approval" : "idle" };
      return { ...next, orb: turnOrb(next, next.chats[a.pane]) };
    }
  }
}

const Ctx = createContext<{ state: State; dispatch: React.Dispatch<Action> }>({
  state: initialState,
  dispatch: () => {},
});

export function StoreProvider({ children, initial }: { children: React.ReactNode; initial?: Partial<State> }) {
  const [state, dispatch] = useReducer(reduce, { ...initialState, ...initial });
  return <Ctx.Provider value={{ state, dispatch }}>{children}</Ctx.Provider>;
}

export const useStore = () => useContext(Ctx);

/**
 * The chat the window is about: the selected chat, else (no chat pane drawn)
 * the one selected last — the sidebar marks its conversation and File and
 * Preview panes follow its project, as they followed the one conversation
 * whatever the single pane showed; with none ever selected, the pane in slot
 * 1 (`home`). Never where ambiguous input goes: that is `selectedChat`, or
 * nowhere.
 */
export const activePane = (s: State): PaneNo => s.selectedChat ?? s.selectedOrder[0] ?? s.home;
export const activeChat = (s: State) => s.chats[activePane(s)];

export const currentTask = (s: State) => s.tasks.find((t) => t.id === s.taskId) || null;
/** The project the window is about — the active chat's conversation's, or a
 * task picked more recently — which the sidebar highlights and the File and
 * Preview panes follow. A chat pane's own chip shows its own conversation's. */
export const currentProjectId = (s: State) =>
  activeProjectId({ ...activeChat(s), taskId: s.taskId, taskFocus: s.taskFocus, threads: s.threads, tasks: s.tasks });
export const currentProject = (s: State) => s.projects.find((p) => p.id === currentProjectId(s)) || null;

/**
 * The state as the single-layout suites read it (`window.__hud.state()`): the
 * window's fields with the active chat's conversation laid over the top, as
 * the one conversation's fields used to be. `chats` still has every pane.
 */
export function flatState(s: State): State & ChatState {
  return { ...s, ...activeChat(s) };
}

/**
 * A test hook's action, translated: a legacy `patch` carrying conversation
 * fields (`threadId`, `compose`, `busy`, …) writes them to the active chat's
 * pane, and the rest to the window.
 */
export function compatActions(a: any, target: PaneNo): Action[] {
  if (!a || a.type !== "patch" || !a.patch) return [a as Action];
  const chat: Record<string, unknown> = {};
  const rest: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(a.patch)) {
    if ((CHAT_KEYS as readonly string[]).includes(k)) chat[k] = v;
    else rest[k] = v;
  }
  const out: Action[] = [];
  if (Object.keys(chat).length) out.push({ type: "chat", pane: target, patch: chat as ChatPatch });
  if (Object.keys(rest).length || !out.length) out.push({ type: "patch", patch: rest as Partial<State> });
  return out;
}

/**
 * The routing line every status embed and the HUD task view show
 * (design §8.5) — "a routing decision the owner cannot see is one they will
 * assume was wrong". Built from the status record's decisions, newest per role.
 */
export function routingLine(task: Task | null): string {
  if (!task) return "";
  const latest = new Map<string, { provider: string; reason: string }>();
  for (const d of task.status.routing || []) latest.set(d.role, { provider: d.provider, reason: d.reason });
  if (!latest.size) return "";
  return [...latest.entries()]
    .map(([role, d]) => `${role}: ${d.provider}${d.reason ? ` (${d.reason})` : ""}`)
    .join(" · ");
}
