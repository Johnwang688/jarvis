// One store for the window. Panels read it and dispatch into it; the SSE
// subscription is the only thing that writes lifecycle state, which is the
// §10.3 rule carried into the frontend: **the status record drives the task
// view, never the model's prose.**

import React, { createContext, useContext, useReducer } from "react";
import type {
  ApprovalRequest, AvatarDesc, ChatMessage, Project, Schedule, Task, TaskThread,
  Thread, ToolOp, Usage, RouteView,
} from "../types";
import { DEFAULT_MODE, type DictationMode } from "../lib/dictation";

export type Tab = "chat" | "task" | "file" | "diff" | "preview";
export type OrbState =
  | "idle" | "listening" | "transcribing" | "thinking"
  | "composing" | "tool" | "approval" | "speaking" | "error";

export interface State {
  projects: Project[];
  platforms: Record<string, { platform: string; note: string | null }>;
  threads: Thread[];
  tasks: Task[];
  taskThreads: Record<string, TaskThread[]>;
  projectId: string | null;
  threadId: string | null;
  taskId: string | null;
  tab: Tab;
  messages: ChatMessage[];
  draft: string;
  ops: ToolOp[];
  approvals: ApprovalRequest[];
  usage: Usage | null;
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
  error: string;
  picker: null | "model" | "voice" | "avatar" | "route" | "newProject" | "newTask" | "schedule";
}

export const initialState: State = {
  projects: [], platforms: {}, threads: [], tasks: [], taskThreads: {},
  projectId: null, threadId: null, taskId: null, tab: "chat",
  messages: [], draft: "", ops: [], approvals: [], usage: null, schedules: [],
  route: null, avatar: null, wakePatterns: [], dictation: DEFAULT_MODE,
  level: 0, orb: "idle", status: "", busy: false, pendingTranscript: "",
  error: "", picker: null,
};

export type Action =
  | { type: "patch"; patch: Partial<State> }
  | { type: "message"; message: ChatMessage }
  | { type: "delta"; text: string }
  | { type: "settle"; text: string }
  | { type: "op_start"; op: ToolOp }
  | { type: "op_done"; call_id: string; ok: boolean; summary: string }
  | { type: "approval_add"; request: ApprovalRequest }
  | { type: "approval_drop"; req_id: string }
  | { type: "task_upsert"; task: Task };

export function reduce(s: State, a: Action): State {
  switch (a.type) {
    case "patch":
      return { ...s, ...a.patch };

    case "message":
      // The owner's own line goes up verbatim; his is rendered. That decision
      // lives in the view, not here — the store keeps the role.
      return { ...s, messages: [...s.messages, a.message] };

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
export const currentProject = (s: State) => s.projects.find((p) => p.id === s.projectId) || null;

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
