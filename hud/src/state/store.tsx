// One store for the window. Panels read it and dispatch into it; the SSE
// subscription is the only thing that writes lifecycle state, which is the
// §10.3 rule carried into the frontend: **the status record drives the task
// view, never the model's prose.**
//
// Several chats at once (WP-B, 2026-10-09): the conversation and the turn it
// waits on are per chat pane (`chats`, lib/chats.ts), and the actions that
// write them name their pane. Approvals, activity, the projects and threads,
// the orb, the level meter, the error line and the pickers stay window-wide.
// The orb follows **the voice target's** turn: a turn event in another pane
// never repaints it.

import React, { createContext, useContext, useReducer } from "react";
import type {
  ApprovalRequest, Attachment, AvatarDesc, ChatMessage, Project, Schedule, Task, TaskThread,
  Thread, ToolOp, Usage, RouteView, DiscordStatus } from "../types";
import { DEFAULT_MODE, type DictationMode } from "../lib/dictation";
import { activeProjectId } from "../lib/compose";
import { NO_ACTIVITY, applyActivity, type ActivityView } from "../lib/activity";
import { pendingAfter } from "../lib/giveback";
import { CHAT_KEYS, emptyChats, type ChatState, type Chats } from "../lib/chats";
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
  /** The chat pane voice goes to: the one used last (lib/chats `voiceTargetOf`). */
  voiceTarget: PaneNo;
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
  chats: emptyChats(), voiceTarget: 1, taskId: null, taskFocus: false,
  approvals: [], usage: null, discord: null, schedules: [],
  route: null, avatar: null, wakePatterns: [], dictation: DEFAULT_MODE,
  level: 0, orb: "idle", error: "", moveError: "", picker: null, archivedNames: [],
};

/** What a pane's patch may carry besides its own fields: the orb (applied only
 * for the voice target's pane) and the window's error line. */
export type ChatPatch = Partial<ChatState> & { orb?: OrbState; error?: string };

export type Action =
  | { type: "patch"; patch: Partial<State> }
  /** One pane's conversation fields; `orb` lands only when it is the voice target. */
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
   * reach the pane that shows it. */
  | { type: "open_thread"; pane: PaneNo; threadId: string }
  /** Two panes' conversations change places (a thread opened in one while the other held it off screen). */
  | { type: "chat_swap"; a: PaneNo; b: PaneNo }
  /** The voice target moved: the orb now follows that pane's turn. */
  | { type: "voice_target"; pane: PaneNo };

function withChat(s: State, pane: PaneNo, next: ChatState): State {
  return { ...s, chats: { ...s.chats, [pane]: next } };
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
      let next = withChat(s, a.pane, { ...s.chats[a.pane], ...fields });
      if (orb !== undefined && a.pane === s.voiceTarget) next = { ...next, orb };
      if (error !== undefined) next = { ...next, error };
      return next;
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
      if (a.pane !== s.voiceTarget) return next;
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
      if (a.pane !== s.voiceTarget) return next;
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
      const busy = s.chats[s.voiceTarget].busy;
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
      const here = { ...chats[a.pane], threadId: a.threadId, compose: null };
      for (const n of PANE_NOS) {
        if (n === a.pane) continue;
        const q = chats[n];
        // Another pane started this thread's turn and then moved on to another
        // thread: the turn comes with the thread — unless this pane is waiting
        // on a turn of its own, which it keeps (the other pane then still
        // hears this one finish, so neither wedges).
        if (q.busy && q.turnThreadId === a.threadId && q.threadId !== a.threadId
            && (!here.busy || here.turnThreadId === a.threadId)) {
          here.busy = true;
          here.turnThreadId = a.threadId;
          here.status = q.status;
          chats[n] = { ...q, busy: false, turnThreadId: null, status: "" };
        }
      }
      chats[a.pane] = here;
      return { ...s, chats };
    }

    case "chat_swap": {
      if (a.a === a.b) return s;
      return { ...s, chats: { ...s.chats, [a.a]: s.chats[a.b], [a.b]: s.chats[a.a] } };
    }

    case "voice_target": {
      if (a.pane === s.voiceTarget) return s;
      const next = { ...s, voiceTarget: a.pane };
      // What the microphone is doing belongs to the window, whichever pane it
      // will speak into; everything else is the new target's turn.
      if (s.orb === "listening" || s.orb === "transcribing" || s.orb === "speaking") return next;
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

/** The voice target's conversation: the chat the window is about. */
export const activeChat = (s: State) => s.chats[s.voiceTarget];

export const currentTask = (s: State) => s.tasks.find((t) => t.id === s.taskId) || null;
/** The project the window is about — the voice target's conversation's, or a
 * task picked more recently — which the sidebar highlights and the File and
 * Preview panes follow. A chat pane's own chip shows its own conversation's. */
export const currentProjectId = (s: State) =>
  activeProjectId({ ...activeChat(s), taskId: s.taskId, taskFocus: s.taskFocus, threads: s.threads, tasks: s.tasks });
export const currentProject = (s: State) => s.projects.find((p) => p.id === currentProjectId(s)) || null;

/**
 * The state as the single-layout suites read it (`window.__hud.state()`): the
 * window's fields with the voice target's conversation laid over the top, as
 * the one conversation's fields used to be. `chats` still has every pane.
 */
export function flatState(s: State): State & ChatState {
  return { ...s, ...activeChat(s) };
}

/**
 * A test hook's action, translated: a legacy `patch` carrying conversation
 * fields (`threadId`, `compose`, `busy`, …) writes them to the voice target's
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
