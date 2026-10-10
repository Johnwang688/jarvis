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

import React, { createContext, useContext, useReducer } from "react";
import type {
  ApprovalRequest, Attachment, AvatarDesc, ChatMessage, Project, Schedule, Task, TaskThread,
  Thread, ToolOp, Usage, RouteView, DiscordStatus } from "../types";
import { DEFAULT_MODE, type DictationMode } from "../lib/dictation";
import { activeProjectId } from "../lib/compose";
import { NO_ACTIVITY, applyActivity, type ActivityView } from "../lib/activity";
import { pendingAfter } from "../lib/giveback";
import {
  CAPTURE_STATUSES, CHAT_KEYS, MAX_FILES, draftKey, emptyChats, sameConversation, type ChatState, type Chats,
} from "../lib/chats";
import { PANE_NOS, type PaneNo } from "../lib/workspace";

// What the centre shows is the workspace's (lib/workspace.ts, 2026-10-09): a
// view per pane, stored with the layout, not a single `tab` in this store.
export type OrbState =
  | "idle" | "listening" | "transcribing" | "thinking"
  | "composing" | "tool" | "approval" | "speaking" | "error";

export interface State {
  projects: Project[];
  platforms: Record<string, { platform: string; note: string | null }>;
  threads: Thread[];
  tasks: Task[];
  taskThreads: Record<string, TaskThread[]>;
  /** The sidebar dots: what each thread and task is doing (lib/activity.ts). */
  activity: ActivityView;
  /** Each chat pane's conversation and the turn it waits on (lib/chats.ts). */
  chats: Chats;
  /** The selected chat (decisions W-6, lib/chats `selectedChatOf`): the drawn
   * chat pane most recently clicked, where ambiguous input goes — voice, the
   * orb, a no-thread event. Null with no chat pane drawn: such input then has
   * nowhere to go and is never sent. */
  selectedChat: PaneNo | null;
  /** The chat panes the owner selected, most recent first. */
  selectedOrder: PaneNo[];
  /** Unsent words and staged files a conversation left behind when its pane
   * moved to another one, by `draftKey` (a thread id, or a pane's compose
   * row): they come back when a pane takes that conversation up again, and
   * are never left in a box now showing another thread (review of PR #27). */
  drafts: Record<string, { text: string; files: Attachment[] }>;
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
  error: string;
  /** A refused thread move, shown beside the tree it was reverted in. */
  moveError: string;
  picker: null | "model" | "voice" | "avatar" | "route" | "settings" | "newProject" | "newTask" | "schedule"
    | "editProject" | "archiveProject" | "archive";
  /** Archived projects' names, from `/archive`: `/projects` hides them, but
   * the backend still counts them when it numbers a name (decisions B4). */
  archivedNames: string[];
}

export const initialState: State = {
  projects: [], platforms: {}, threads: [], tasks: [], taskThreads: {}, activity: NO_ACTIVITY,
  chats: emptyChats(), selectedChat: 1, selectedOrder: [], drafts: {}, taskId: null, taskFocus: false,
  approvals: [], usage: null, discord: null, schedules: [],
  route: null, avatar: null, wakePatterns: [], dictation: DEFAULT_MODE,
  level: 0, orb: "idle", error: "", moveError: "", picker: null, archivedNames: [],
};

/** What a pane's patch may carry besides its own fields: the orb (applied only
 * for the selected chat's pane) and the window's error line. */
export type ChatPatch = Partial<ChatState> & { orb?: OrbState; error?: string };

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
  | { type: "settle"; pane: PaneNo; text: string }
  | { type: "op_start"; pane: PaneNo; op: ToolOp }
  | { type: "op_done"; pane: PaneNo; call_id: string; ok: boolean; summary: string }
  | { type: "approval_add"; request: ApprovalRequest }
  | { type: "approval_drop"; req_id: string }
  | { type: "task_upsert"; task: Task }
  | { type: "thread_patch"; id: string; patch: Partial<Thread> }
  | { type: "activity"; record: any }
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
  | { type: "stage"; key: string; files: Attachment[] }
  /** Words for a conversation no pane shows: kept with its parked draft. */
  | { type: "park"; key: string; text: string; files?: Attachment[] }
  /** Two panes' conversations change places (a thread opened in one while the other held it off screen). */
  | { type: "chat_swap"; a: PaneNo; b: PaneNo }
  /** The selected chat changed (`front`: the owner selected it, so it heads
   * the order; else a fallback while the layout hides the one selected). The
   * orb now follows that pane's turn. */
  | { type: "select"; pane: PaneNo | null; front?: boolean };

function withChat(s: State, pane: PaneNo, next: ChatState): State {
  return { ...s, chats: { ...s.chats, [pane]: next } };
}

/**
 * `pane` moves from conversation `was` to `next`: when that is another
 * conversation, the box's unsent words and files are parked under the old
 * one's key and the new one's parked draft (if any) comes back — a draft is
 * never left in a box that now shows another thread.
 */
function reconverse(s: State, pane: PaneNo, was: ChatState, next: ChatState): { next: ChatState; drafts: State["drafts"] } {
  if (sameConversation(was, next, pane)) return { next, drafts: s.drafts };
  const drafts = { ...s.drafts };
  const out = draftKey(was, pane);
  if (out && (was.input || was.files.length)) drafts[out] = { text: was.input, files: was.files };
  const into = draftKey(next, pane);
  const parked = into ? drafts[into] : undefined;
  if (into && parked) delete drafts[into];
  return { next: { ...next, input: parked?.text ?? "", files: parked?.files ?? [] }, drafts };
}

/** The orb for a pane's turn, as the reducers below leave it. */
function turnOrb(s: State, c: ChatState): OrbState {
  if (s.approvals.length) return "approval";
  if (c.ops.some((o) => !o.finished)) return "tool";
  return c.busy ? "thinking" : "idle";
}

export function reduce(s: State, a: Action): State {
  switch (a.type) {
    case "patch":
      return { ...s, ...a.patch };

    case "chat": {
      const { orb, error, ...fields } = a.patch;
      const was = s.chats[a.pane];
      const moved = reconverse(s, a.pane, was, { ...was, ...fields });
      let next = { ...withChat(s, a.pane, moved.next), drafts: moved.drafts };
      if (orb !== undefined && a.pane === s.selectedChat) next = { ...next, orb };
      if (error !== undefined) next = { ...next, error };
      return next;
    }

    case "park": {
      const add = a.files ?? [];
      if (!a.text && !add.length) return s;
      const had = s.drafts[a.key];
      const text = had?.text && a.text ? `${had.text}\n${a.text}` : a.text || had?.text || "";
      return { ...s, drafts: { ...s.drafts, [a.key]: { text, files: [...(had?.files ?? []), ...add] } } };
    }

    case "input": {
      const c = s.chats[a.pane];
      return withChat(s, a.pane, { ...c, input: a.text ?? c.input, files: a.files ?? c.files });
    }

    case "stage": {
      if (!a.files.length) return s;
      const n = PANE_NOS.find((p) => draftKey(s.chats[p], p) === a.key);
      if (n !== undefined) {
        const c = s.chats[n];
        return withChat(s, n, { ...c, files: [...c.files, ...a.files].slice(0, MAX_FILES) });
      }
      const had = s.drafts[a.key];
      const files = [...(had?.files ?? []), ...a.files].slice(0, MAX_FILES);
      return { ...s, drafts: { ...s.drafts, [a.key]: { text: had?.text ?? "", files } } };
    }

    case "message":
      // The owner's own line goes up verbatim; his is rendered. That decision
      // lives in the view, not here — the store keeps the role.
      return withChat(s, a.pane, { ...s.chats[a.pane], messages: [...s.chats[a.pane].messages, a.message] });

    case "mark": {
      const hit = (m: ChatMessage) =>
        (!!a.local && m.local === a.local) || (!!a.message_id && m.message_id === a.message_id);
      if (!PANE_NOS.some((n) => s.chats[n].messages.some(hit))) return s;
      const chats = { ...s.chats };
      for (const n of PANE_NOS) {
        if (!chats[n].messages.some(hit)) continue;
        chats[n] = { ...chats[n], messages: chats[n].messages.map((m) => (hit(m) ? { ...m, ...a.patch } : m)) };
      }
      return { ...s, chats };
    }

    case "unmessage": {
      // Only that bubble: a reply that settled while the send was failing
      // stays (restoring a snapshot taken before the send used to drop it).
      const chats = { ...s.chats };
      for (const n of PANE_NOS) {
        if (!chats[n].messages.some((m) => m.local === a.local)) continue;
        chats[n] = { ...chats[n], messages: chats[n].messages.filter((m) => m.local !== a.local) };
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
      return withChat(s, a.pane, { ...c, draft: "", messages: [...c.messages, { role: "assistant", text }] });
    }

    case "op_start": {
      const c = s.chats[a.pane];
      const next = withChat(s, a.pane, { ...c, ops: [...c.ops.slice(-60), a.op] });
      if (a.pane !== s.selectedChat) return next;
      return { ...next, orb: s.orb === "approval" ? s.orb : "tool" };
    }

    case "op_done": {
      const c = s.chats[a.pane];
      const ops = c.ops.map((o) =>
        o.call_id === a.call_id && !o.finished
          ? { ...o, finished: Date.now(), ok: a.ok, summary: a.summary }
          : o,
      );
      const next = withChat(s, a.pane, { ...c, ops });
      if (a.pane !== s.selectedChat) return next;
      const running = ops.some((o) => !o.finished);
      // The window falls back to THINKING when the running count hits zero —
      // v1's tool_done half, which is what stopped the slowest seconds of a
      // turn reading as "still running gmail_search".
      const orb = s.orb === "approval" ? s.orb : running ? "tool" : c.busy ? "thinking" : "idle";
      return { ...next, orb };
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
      return activity === s.activity ? s : { ...s, activity };
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
      const was = chats[a.pane];
      // A compose row that already opened this thread is the same
      // conversation: what it shows is that thread's, so nothing reloads.
      const same = was.compose?.openedId === a.threadId;
      const moved = reconverse(s, a.pane, was, {
        ...was, threadId: a.threadId, compose: null, loadedThread: same ? a.threadId : was.loadedThread,
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
      chats[a.pane] = here;
      return { ...s, chats, drafts: moved.drafts };
    }

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
 * whatever the single pane showed. Never where ambiguous input goes: that is
 * `selectedChat`, or nowhere.
 */
export const activePane = (s: State): PaneNo => s.selectedChat ?? s.selectedOrder[0] ?? 1;
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
