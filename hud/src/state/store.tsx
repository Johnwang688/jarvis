// One store for the window. Panels read it and dispatch into it; the SSE
// subscription is the only thing that writes lifecycle state, which is the
// §10.3 rule carried into the frontend: **the status record drives the task
// view, never the model's prose.**

import React, { createContext, useContext, useReducer } from "react";
import type {
  ApprovalRequest, Attachment, AvatarDesc, ChatMessage, Project, Schedule, Task, TaskThread,
  Thread, ToolOp, Usage, RouteView, DiscordStatus } from "../types";
import { DEFAULT_MODE, type DictationMode } from "../lib/dictation";
import { activeProjectId, type Compose } from "../lib/compose";
import { NO_ACTIVITY, applyActivity, type ActivityView } from "../lib/activity";
import { pendingAfter, type GiveBack } from "../lib/giveback";

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
  /** The conversation is exactly one of: an existing thread (`threadId`), or
   * a new thread being composed (`compose`). There is no stored project;
   * `currentProject` derives it, so the sidebar cannot drift from where a
   * message goes (lib/compose.ts). */
  threadId: string | null;
  compose: Compose | null;
  taskId: string | null;
  /** A task was picked more recently than a thread: the File tab follows it. */
  taskFocus: boolean;
  /** The thread whose turn this window started and is waiting on. Window-wide
   * `busy` follows it, so switching threads mid-turn cannot wedge the window
   * waiting for a finish it no longer listens for. */
  turnThreadId: string | null;
  messages: ChatMessage[];
  draft: string;
  ops: ToolOp[];
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
  status: string;
  busy: boolean;
  /** Set by REVIEW dictation: text handed to the input box, never sent. */
  pendingTranscript: string;
  /** Words (and files) handed back to the input box — a send that failed, or
   * messages dropped when the owner stopped the turn — for the thread on
   * screen, oldest first, until the box takes them (lib/giveback.ts). A
   * fresh `nonce` each, so the same words handed back twice arrive twice and
   * one hand-back is never taken twice. */
  restore: GiveBack[];
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
  threadId: null, compose: null, taskId: null, taskFocus: false, turnThreadId: null,
  messages: [], draft: "", ops: [], approvals: [], usage: null, discord: null, schedules: [],
  route: null, avatar: null, wakePatterns: [], dictation: DEFAULT_MODE,
  level: 0, orb: "idle", status: "", busy: false, pendingTranscript: "", restore: [],
  error: "", moveError: "", picker: null, archivedNames: [],
};

export type Action =
  | { type: "patch"; patch: Partial<State> }
  | { type: "message"; message: ChatMessage }
  /** Patch the message this window drew as `local`, or the daemon's `message_id`. */
  | { type: "mark"; local?: string; message_id?: string; patch: Partial<ChatMessage> }
  /** Take back a message this window drew optimistically (its send failed). */
  | { type: "unmessage"; local: string }
  | { type: "delta"; text: string }
  | { type: "settle"; text: string }
  | { type: "op_start"; op: ToolOp }
  | { type: "op_done"; call_id: string; ok: boolean; summary: string }
  | { type: "approval_add"; request: ApprovalRequest }
  | { type: "approval_drop"; req_id: string }
  | { type: "task_upsert"; task: Task }
  | { type: "thread_patch"; id: string; patch: Partial<Thread> }
  | { type: "activity"; record: any }
  /** Hand words back to the box of the thread on screen. */
  | { type: "give_back"; text: string; files: Attachment[]; nonce: number }
  /** The box took every hand-back up to `nonce`. */
  | { type: "given_back"; nonce: number };

export function reduce(s: State, a: Action): State {
  switch (a.type) {
    case "patch":
      return { ...s, ...a.patch };

    case "message":
      // The owner's own line goes up verbatim; his is rendered. That decision
      // lives in the view, not here — the store keeps the role.
      return { ...s, messages: [...s.messages, a.message] };

    case "mark": {
      const hit = (m: ChatMessage) =>
        (!!a.local && m.local === a.local) || (!!a.message_id && m.message_id === a.message_id);
      if (!s.messages.some(hit)) return s;
      return { ...s, messages: s.messages.map((m) => (hit(m) ? { ...m, ...a.patch } : m)) };
    }

    case "unmessage":
      // Only that bubble: a reply that settled while the send was failing
      // stays (restoring a snapshot taken before the send used to drop it).
      return { ...s, messages: s.messages.filter((m) => m.local !== a.local) };

    case "delta":
      // Plain text while drafting: half a markdown document is not markdown,
      // and rendering `**bold` mid-word flickers.
      return { ...s, draft: s.draft + a.text };

    case "settle": {
      // The finished reply replaces the draft and is rendered once.
      const text = a.text || s.draft;
      if (!text) return { ...s, draft: "" };
      return { ...s, draft: "", messages: [...s.messages, { role: "assistant", text }] };
    }

    case "op_start":
      return { ...s, ops: [...s.ops.slice(-60), a.op], orb: s.orb === "approval" ? s.orb : "tool" };

    case "op_done": {
      const ops = s.ops.map((o) =>
        o.call_id === a.call_id && !o.finished
          ? { ...o, finished: Date.now(), ok: a.ok, summary: a.summary }
          : o,
      );
      const running = ops.some((o) => !o.finished);
      // The window falls back to THINKING when the running count hits zero —
      // v1's tool_done half, which is what stopped the slowest seconds of a
      // turn reading as "still running gmail_search".
      const orb = s.orb === "approval" ? s.orb : running ? "tool" : s.busy ? "thinking" : "idle";
      return { ...s, ops, orb };
    }

    case "approval_add": {
      if (s.approvals.some((r) => r.req_id === a.request.req_id)) return s;
      return { ...s, approvals: [...s.approvals, a.request], orb: "approval" };
    }

    case "approval_drop": {
      const approvals = s.approvals.filter((r) => r.req_id !== a.req_id);
      const orb =
        approvals.length === 0 && s.orb === "approval" ? (s.busy ? "thinking" : "idle") : s.orb;
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

    case "give_back":
      if (!a.text && !a.files.length) return s;
      return { ...s, restore: [...s.restore, { text: a.text, files: a.files, nonce: a.nonce }] };

    case "given_back": {
      const left = pendingAfter(s.restore, a.nonce);
      return left.length === s.restore.length ? s : { ...s, restore: left };
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

export const currentTask = (s: State) => s.tasks.find((t) => t.id === s.taskId) || null;
export const currentProjectId = (s: State) => activeProjectId(s);
export const currentProject = (s: State) => s.projects.find((p) => p.id === activeProjectId(s)) || null;

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
