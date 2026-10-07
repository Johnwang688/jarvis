// The one place that knows the wire. Every route here is in docs/hud-api.md;
// nothing invents one, and a contract change is proposed in the notes rather
// than worked around here.
//
// Same origin throughout: the daemon serves the HUD and the API on FACE_PORT,
// which is what keeps the mic grant and makes `/approvals` same-origin-only on
// the backend meaningful.

import type {
  ApprovalRequest, AvatarDesc, ChatMessage, Diff, FileRead, ModelRow, Platform,
  Project, RouteView, Schedule, SchedulePreview, Task, TaskThread, Thread, Tree, Usage,
  VoiceEntry, Attachment, DirListing,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  const text = await res.text();
  let body: any = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!res.ok) {
    // Every error is `{"error": str}`; never a stack trace, never a credential.
    throw new ApiError(res.status, (body && body.error) || `HTTP ${res.status}`);
  }
  return body as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const patch = (body: unknown): RequestInit => ({ ...json(body), method: "PATCH" });
const put = (body: unknown): RequestInit => ({ ...json(body), method: "PUT" });

export const api = {
  // --- status / config ----------------------------------------------------
  status: () => req<any>("/status"),
  config: () => req<any>("/config").catch(() => ({})),

  // --- projects -----------------------------------------------------------
  projects: () => req<Project[]>("/projects"),
  createProject: (body: {
    name: string; root: string; extra_dirs?: string[]; profile?: string;
  }) => req<Project>("/projects", json(body)),
  patchProject: (id: string, body: Record<string, unknown>) =>
    req<Project>(`/projects/${id}`, patch(body)),
  platform: (id: string) => req<Platform>(`/projects/${id}/platform`),

  // --- threads ------------------------------------------------------------
  threads: (projectId?: string) =>
    req<Thread[]>(projectId ? `/threads?project=${encodeURIComponent(projectId)}` : "/threads"),
  openThread: (body: { project_id: string; role?: string; title?: string }) =>
    req<Thread>("/threads", json(body)),
  transcript: (id: string) => req<{ messages: ChatMessage[] }>(`/threads/${id}/transcript`),
  send: (id: string, body: { text: string; images?: string[]; attachments?: Attachment[] }) =>
    req<{ turn_id: string }>(`/threads/${id}/send`, json(body)),
  interrupt: (id: string) => req<any>(`/threads/${id}/interrupt`, json({})),
  /** Re-parent a **chat** thread; a task's threads 409 (they move with the task). */
  moveThread: (id: string, projectId: string) =>
    req<Thread>(`/threads/${id}`, patch({ project_id: projectId })),

  // --- filesystem (directory picker only: names, never contents) -----------
  dirs: (path = "") => req<DirListing>(`/fs/dirs?path=${encodeURIComponent(path)}`),

  // --- tasks --------------------------------------------------------------
  tasks: (projectId?: string) =>
    req<Task[]>(projectId ? `/tasks?project=${encodeURIComponent(projectId)}` : "/tasks"),
  task: (id: string) => req<Task>(`/tasks/${id}`),
  createTask: (body: { project_id: string; brief: string }) =>
    req<Task>("/tasks", json(body)),
  startTask: (id: string) => req<Task>(`/tasks/${id}/start`, json({})),
  steerTask: (id: string, text: string) => req<any>(`/tasks/${id}/steer`, json({ text })),
  cancelTask: (id: string) => req<any>(`/tasks/${id}/cancel`, json({})),
  resumeTask: (id: string) => req<any>(`/tasks/${id}/resume`, json({})),
  answerTask: (id: string, body: { question: string; answer: string }) =>
    req<any>(`/tasks/${id}/answer`, json(body)),
  taskThreads: (id: string) => req<TaskThread[]>(`/tasks/${id}/threads`),
  journal: (id: string, after = 0) => req<any[]>(`/tasks/${id}/journal?after=${after}`),

  // --- files --------------------------------------------------------------
  tree: (projectId: string, path = "", depth = 2) =>
    req<Tree>(
      `/projects/${projectId}/tree?path=${encodeURIComponent(path)}&depth=${depth}`,
    ),
  readFile: (projectId: string, path: string) =>
    req<FileRead>(`/projects/${projectId}/file?path=${encodeURIComponent(path)}`),
  writeFile: (projectId: string, body: { path: string; content: string; expected_mtime: number }) =>
    req<{ mtime: number }>(`/projects/${projectId}/file`, put(body)),

  // --- diff ---------------------------------------------------------------
  diff: (taskId: string) => req<Diff>(`/tasks/${taskId}/diff`),
  diffFile: (taskId: string, path: string) =>
    req<{ before: string; after: string }>(
      `/tasks/${taskId}/diff/file?path=${encodeURIComponent(path)}`,
    ),

  // --- approvals ----------------------------------------------------------
  approvals: () => req<ApprovalRequest[]>("/approvals"),
  resolveApproval: (reqId: string, body: { decision: "allow" | "deny"; always?: boolean }) =>
    req<any>(`/approvals/${reqId}`, json(body)),

  // --- usage / route / schedules -----------------------------------------
  usage: () => req<Usage>("/usage"),
  route: (projectId?: string) =>
    req<RouteView>(projectId ? `/route?project=${encodeURIComponent(projectId)}` : "/route"),
  schedules: () => req<Schedule[]>("/schedules"),
  createSchedule: (body: {
    project_id: string; brief: string; cron?: string; every_s?: number; enabled: boolean;
  }) => req<Schedule>("/schedules", json(body)),
  patchSchedule: (id: string, body: Record<string, unknown>) =>
    req<Schedule>(`/schedules/${id}`, patch(body)),
  deleteSchedule: (id: string) => req<any>(`/schedules/${id}`, { method: "DELETE" }),
  runSchedule: (id: string) => req<any>(`/schedules/${id}/run-now`, json({})),
  /** What this schedule will actually do, read back rather than guessed. */
  previewSchedule: (body: { cron?: string; every_s?: number }) =>
    req<SchedulePreview>("/schedules/preview", json(body)),

  // --- v1-semantics surfaces ---------------------------------------------
  avatars: () => req<{ avatars: AvatarDesc[]; active?: string }>("/avatars"),
  setAvatar: (slug: string) => req<AvatarDesc>("/avatar", json({ slug })),
  voices: () => req<{ voices: VoiceEntry[]; override?: string }>("/voices"),
  /** `""` clears the override. The key is `voice` (the daemon refuses any other). */
  setVoice: (name: string) => req<any>("/voice", json({ voice: name })),
  models: () => req<{ models: ModelRow[]; selected?: string | null }>("/models"),
  catalog: () => req<{ models: ModelRow[]; stale?: string }>("/models/catalog"),
  /** Select the fast path's global model; `""` returns to the config default. */
  setModel: (id: string) => req<any>("/model", json({ model: id })),
  /** Pin one roster model's effort; `""` follows the global default. Never selects it. */
  setModelEffort: (model: string, effort: string) =>
    req<any>("/models", json({ model, effort })),
  mute: (on: boolean) => req<any>("/mute", json({ muted: on })),

  // --- speech -------------------------------------------------------------
  async stt(blob: Blob): Promise<string> {
    const res = await fetch("/stt", {
      method: "POST",
      headers: { "Content-Type": blob.type || "audio/wav" },
      body: blob,
    });
    if (!res.ok) throw new ApiError(res.status, `stt ${res.status}`);
    const body = await res.json();
    return body.text || "";
  },
  async say(text: string): Promise<Blob> {
    const res = await fetch("/say", json({ text }));
    if (!res.ok) throw new ApiError(res.status, `say ${res.status}`);
    return res.blob();
  },
};

/**
 * The event stream. One connection for the window; filters are applied here
 * rather than per-panel so a late subscriber cannot miss a frame another
 * panel already consumed.
 */
export function subscribe(onEvent: (e: any) => void): () => void {
  let source: EventSource | null = null;
  let closed = false;
  const open = () => {
    if (closed) return;
    source = new EventSource("/events");
    source.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch {
        /* a frame we cannot parse is a frame we ignore, never a crash */
      }
    };
    source.onerror = () => {
      // The daemon restarts; the window reconnects rather than going quiet.
      source?.close();
      source = null;
      if (!closed) setTimeout(open, 1000);
    };
  };
  open();
  return () => {
    closed = true;
    source?.close();
  };
}
