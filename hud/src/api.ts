// The one place that knows the wire. Every route here is in docs/hud-api.md;
// nothing invents one, and a contract change is proposed in the notes rather
// than worked around here.
//
// Same origin throughout: the daemon serves the HUD and the API on FACE_PORT,
// which is what keeps the mic grant and makes `/approvals` same-origin-only on
// the backend meaningful.

import type {
  ApprovalRequest, AvatarDesc, ChatMessage, ToolEntry, Diff, FileRead, ModelRow, Platform,
  Project, RouteView, Schedule, SchedulePreview, Task, TaskThread, Thread, Tree, Usage,
  VoiceEntry, Attachment, DirListing, DiscordStatus, ProjectChannel, BackfillResult, SendResult } from "./types";
import type { ThreadModels } from "./lib/threadmodel";
import type { RosterView } from "./lib/roster";
import type { ArchiveView, DeleteResult, ProjectImpact } from "./types";
import { parseActivity, type ActivityView } from "./lib/activity";
import { parseRow, parseRows, type InSpec } from "./lib/terminal";
import { parseBuild, parseDetection, parseStatus as parseVexStatus } from "./lib/vex";
import { parseNewCheck, parseNewRequest, parseTemplates, type NewBody } from "./lib/vexNew";

import type { CaptureIdentity } from "./lib/pageCapture";
import { WINDOW_ID, isWindowId } from "./lib/windowId";

export class ApiError extends Error {
  status: number;
  /** The error's whole body: `{"error": str}`, and on a save's 409 the disk's `etag` (ED-2). */
  body: Record<string, unknown> | null;
  constructor(status: number, message: string, body: Record<string, unknown> | null = null) {
    super(message);
    this.status = status;
    this.body = body;
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
    throw new ApiError(res.status, (body && body.error) || `HTTP ${res.status}`,
      body && typeof body === "object" && !Array.isArray(body) ? body : null);
  }
  return body as T;
}

/** Owner Browser routes. Page data is JSON/text only; attach tickets stay in memory. */
export const browserApi = {
  get: <T>(path: string, signal?: AbortSignal) => req<T>("/browser" + path, { signal }),
  write: <T>(path: string, body: unknown = {}, method = "POST", signal?: AbortSignal) =>
    req<T>("/browser" + path, { method, headers: {"Content-Type":"application/json"}, body:JSON.stringify(body), signal }),
  remove: <T>(path: string) => req<T>("/browser" + path, {method:"DELETE"}),
  async download(id:string):Promise<Blob> {
    const response = await fetch("/browser/downloads/" + encodeURIComponent(id));
    if (!response.ok) throw new ApiError(response.status, "The download could not be saved.");
    return response.blob();
  },
};

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

  // Automatic Direct opening uses only owner registry routes.
  devServers: (window: string) => req<unknown>("/dev-servers?window=" + encodeURIComponent(window)),
  claimDevServer: (id: string, body: { window: string; shows: boolean; focused_at: number }) =>
    req<unknown>("/dev-servers/" + encodeURIComponent(id) + "/claim", json(body)),
  actDevServer: (id: string, action: "renew" | "placed" | "dismiss", window: string) =>
    req<unknown>("/dev-servers/" + encodeURIComponent(id) + "/" + action, json({ window })),
  setBrowserSound: (sound: boolean) => req<unknown>("/browser/settings", patch({ sound })),
  setBrowserAutoOpen: (auto_open: boolean) => req<{ auto_open: boolean }>("/browser/settings", patch({ auto_open })),

  captureBrowser: (tab: string, identity: CaptureIdentity, whole: boolean) => req<unknown>(
    "/browser/tabs/" + encodeURIComponent(tab) + "/capture?" + new URLSearchParams({
      window: identity.window, pane: String(identity.pane_id), local_tab: identity.local_tab_id,
      view: identity.view_id, whole: whole ? "1" : "0",
    })),

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
  openThread: (body: {
    project_id: string; role?: string; title?: string;
    /** fast | claude | codex; fixed once the thread exists (decisions A1). */
    provider?: string;
    brief?: { model?: string; effort?: string };
  }) => req<Thread>("/threads", json(body)),
  /** A chat thread's model and effort, from its next message; `model: null`
   * is the default. Never its provider. */
  setThreadModel: (id: string, body: { model?: string | null; effort?: string | null }) =>
    req<Thread>(`/threads/${id}`, patch(body)),
  /** What the provider / model / effort chips offer, per provider. */
  threadModels: () => req<ThreadModels>("/thread-models"),
  /** Claude's or Codex's default for chat threads (2026-10-08); `model: ""`
   * resets to the built-in (Claude) or routing (Codex) default. Answers the
   * new `GET /thread-models`. */
  setProviderDefault: (body: { provider: string; model: string; effort?: string }) =>
    req<ThreadModels>("/thread-models", json(body)),
  transcript: async (id: string, tools = true): Promise<{ messages: (ChatMessage | ToolEntry)[] }> => {
    if (!tools) return req(`/threads/${id}/transcript`);
    try { return await req(`/threads/${id}/transcript?tools=1`); }
    catch (error) {
      if (!(error instanceof ApiError) || error.status !== 400) throw error;
      return req(`/threads/${id}/transcript`);
    }
  },
  /** `spoken` (PR C): dictated, so the Discord mirror says "You (HUD, voice)".
   * While a turn runs the message is steered into it or queued behind it
   * (2026-10-08) — never refused; `status` says which. */
  send: (id: string, body: { text: string; images?: string[]; attachments?: Attachment[]; spoken?: boolean }) =>
    req<SendResult>(`/threads/${id}/send`, json(body)),
  interrupt: (id: string, expectedTurnId?: string) => req<any>(`/threads/${id}/interrupt`, json(expectedTurnId ? { expected_turn_id: expectedTurnId } : {})),
  /** The sidebar dots (2026-10-08): every thread and task that is not idle. */
  activity: () => req<any>("/activity").then(parseActivity) as Promise<ActivityView>,
  /** The owner opened it: no longer unread or failed. */
  seenThread: (id: string) => req<{ status: string }>(`/threads/${id}/seen`, json({})),
  seenTask: (id: string) => req<{ status: string }>(`/tasks/${id}/seen`, json({})),
  /** Re-parent a **chat** thread; a task's threads 409 (they move with the task). */
  moveThread: (id: string, projectId: string) =>
    req<Thread>(`/threads/${id}`, patch({ project_id: projectId })),

  // --- rename, archive, restore, delete (decisions part B) -----------------
  // Archive, restore and delete answer only this window (the HUD's listener
  // and its Origin); a project's archive and delete send back the token the
  // confirmation was read from, so a project that changed meanwhile refuses.
  /** A colliding title comes back numbered: read the result, not the input. */
  renameThread: (id: string, title: string) => req<Thread>(`/threads/${id}`, patch({ title })),
  projectImpact: (id: string) => req<ProjectImpact>(`/projects/${id}/impact`),
  archiveProject: (id: string, token: string) =>
    req<{ project: Project; paused_schedules: string[] }>(
      `/projects/${id}/archive?expect=${encodeURIComponent(token)}`, json({})),
  restoreProject: (id: string) => req<Project>(`/projects/${id}/restore`, json({})),
  deleteProject: (id: string, token: string) =>
    req<DeleteResult>(`/projects/${id}?expect=${encodeURIComponent(token)}`, { method: "DELETE" }),
  archiveThread: (id: string) => req<Thread>(`/threads/${id}/archive`, json({})),
  restoreThread: (id: string) => req<Thread>(`/threads/${id}/restore`, json({})),
  deleteThread: (id: string) => req<DeleteResult>(`/threads/${id}`, { method: "DELETE" }),
  archive: () => req<ArchiveView>("/archive"),
  emptyTrash: () => req<{ removed: number }>("/trash/empty", json({})),

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
  /** Exactly `{index, text}`: `index` is the question's place in
   * `spec.questions` (lib/taskQuestions). A refusal rejects with the daemon's
   * sentence; the caller shows it. Answers the task as it now is. */
  answerTask: (id: string, index: number, text: string) =>
    req<Task>(`/tasks/${id}/answer`, json({ index, text })),
  taskThreads: (id: string) => req<TaskThread[]>(`/tasks/${id}/threads`),
  journal: (id: string, after = 0) => req<any[]>(`/tasks/${id}/journal?after=${after}`),

  // --- files --------------------------------------------------------------
  tree: (projectId: string, path = "", depth = 2) =>
    req<Tree>(
      `/projects/${projectId}/tree?path=${encodeURIComponent(path)}&depth=${depth}`,
    ),
  // `signal` drops a read nobody waits for any more (the Diff pane's list).
  readFile: (projectId: string, path: string, signal?: AbortSignal) =>
    req<FileRead>(`/projects/${projectId}/file?path=${encodeURIComponent(path)}`, signal ? { signal } : undefined),
  // The editor's save (ED-2): against the bytes it was read or saved as
  // (`expected_etag`; null means the file must not exist). 409 carries the
  // disk's etag in `ApiError.body`. `signal` gives up on a write that never
  // answers (the registry's WRITE_TIMEOUT_MS).
  writeFile: (projectId: string, body: { path: string; content: string; expected_etag: string | null },
    signal?: AbortSignal) =>
    req<{ etag: string; abs: string; mtime: number; mtime_ns: number; size: number }>(
      `/projects/${projectId}/file`, { ...put(body), signal }),
  // Where each open file stands (ED-2's change poll): at most 100 paths.
  statFiles: (projectId: string, paths: string[]) =>
    req<{ files: Record<string, import("./lib/buffers").StatRow> }>(
      `/projects/${projectId}/files/stat`, json({ paths })),

  // --- the editor's navigation (ED-7): owner-only, like every file route ---
  /** Every file quick open may offer, relative to the project. */
  listFiles: (projectId: string) =>
    req<import("./types").FileList>(`/projects/${projectId}/files`),
  /** Find in files: at most 2,000 matches and 10 s. Refused on the Inbox. */
  searchFiles: (projectId: string, body: import("./types").SearchQuery) =>
    req<import("./types").SearchResult>(`/projects/${projectId}/search`, json(body)),
  /** New file, new folder, rename (never onto anything) or delete (to the trash). */
  fileOp: (projectId: string, body: { op: "new_file" | "new_folder" | "rename" | "delete"; path: string; to?: string }) =>
    req<import("./types").FsResult>(`/projects/${projectId}/fs`, json(body)),

  // --- the editor's git (ED-9): owner-only, like every file route ---------
  /** What HEAD holds for a file (the gutter's base). Refused for env, withheld and gate files. */
  gitBase: (projectId: string, path: string, signal?: AbortSignal) =>
    req<import("./types").GitBase>(`/projects/${projectId}/git/base?path=${encodeURIComponent(path)}`,
      signal ? { signal } : undefined),
  /** The explorer's colours; the daemon caches it for 2 s. */
  gitStatus: (projectId: string) =>
    req<import("./types").GitStatus>(`/projects/${projectId}/git/status`),

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
  discord: () => req<DiscordStatus>("/discord"),
  // B1: a project's channel. Reading is open; create, link, unlink and the
  // backfill answer only this window (owner-only, like archive).
  projectDiscord: (id: string, refresh = false) =>
    req<ProjectChannel>(`/projects/${id}/discord${refresh ? "?refresh=1" : ""}`),
  projectDiscordAction: (id: string,
    body: { action: "create" | "unlink" } | { action: "link"; channel_id: string }) =>
    req<ProjectChannel>(`/projects/${id}/discord`, json(body)),
  discordBackfill: () => req<BackfillResult>("/discord/backfill", json({})),
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
  /** Read-aloud's per-chat voices and mutes (V7): the daemon's map and the voices it hands out. */
  speechVoices: () => req<{ threads: Record<string, { voice: string | null; muted: boolean }>; pool: string[] }>("/speech/voices"),
  /** Assign (no more keys), choose (`voice`; "" is automatic again) or mute one chat's voice. */
  speechVoice: (threadId: string, change: { voice?: string; muted?: boolean } = {}) =>
    req<{ thread_id: string; voice: string | null; muted: boolean }>("/speech/voices", json({ thread_id: threadId, ...change })),
  /** The fast path's roster and its default (`models.describe()`). */
  models: () => req<RosterView>("/models"),
  catalog: () => req<{ models: ModelRow[]; stale?: string }>("/models/catalog"),
  /** Set the fast path's default (persisted, beats JARVIS_ORCHESTRATOR);
   * `""` returns to the config default. */
  setModel: (id: string) => req<RosterView>("/model", json({ model: id })),
  /** Pin one roster model's effort; `""` follows the global default. Never selects it. */
  setModelEffort: (model: string, effort: string) =>
    req<any>("/models", json({ model, effort })),
  mute: (on: boolean) => req<any>("/mute", json({ muted: on })),
  /** Pin a catalogue model to the roster (refused unless it can call tools). */
  addModel: (id: string) => req<RosterView>("/models", json({ add: id })),
  /** Unpin a model from the roster (the config default included). Refused
   * with a sentence when it would leave the default unlisted. Never selects. */
  removeModel: (id: string) => req<RosterView>("/models", json({ remove: id })),

  // --- terminals (WP-C's routes; owner-only: this window's Origin) ----------
  // Never an id the window did not get from the daemon, and the folder is
  // always an id (`in`), never a path. The socket itself is in
  // components/Terminal.tsx, built from `location` with a fresh ticket.
  terminals: () => req<unknown>("/terminals").then(parseRows),
  createTerminal: (body: { in: InSpec; cols?: number; rows?: number; readable?: boolean }) =>
    req<unknown>("/terminals", json(body)).then((r) => {
      const row = parseRow(r);
      if (!row) throw new ApiError(500, "the daemon answered without a terminal");
      return row;
    }),
  /** Single use, 30 seconds, this terminal only: fetched right before each attach. */
  terminalTicket: (id: string) => req<{ ticket: string; expires_in: number }>(
    `/terminals/${encodeURIComponent(id)}/ticket`, json({})),
  /** The owner's "Jarvis can read" switch (W-2): exactly `{readable}`. */
  setTerminalReadable: (id: string, readable: boolean) =>
    req<unknown>(`/terminals/${encodeURIComponent(id)}`, patch({ readable })).then(parseRow),
  closeTerminal: (id: string) =>
    req<{ ok: boolean; id: string; exit_code: number | null }>(`/terminals/${encodeURIComponent(id)}`,
      { method: "DELETE" }),

  // --- VEX (VX-1's routes; owner-only; VX-2's view) --------------------------
  // **No request names a program, a path or an argument**: the project is an
  // id, a build an action word, a download a slot and a name. Download is
  // sent only from the VEX view's button, on the owner's own click
  // (lib/vex.ts `isExplicitClick`) — never from a key, a palette or a tool.
  vexStatus: () => req<unknown>("/vex/status").then(parseVexStatus),
  vexProject: (projectId: string) =>
    req<unknown>(`/projects/${encodeURIComponent(projectId)}/vex`).then((r) => parseDetection(r, projectId)),
  vexBuild: (projectId: string, action: "build" | "rebuild" | "clean") =>
    req<{ build_id: string; terminal_id: string }>(`/projects/${encodeURIComponent(projectId)}/vex/build`,
      json({ action })),
  vexBuildState: (buildId: string) =>
    req<unknown>(`/vex/builds/${encodeURIComponent(buildId)}`, { signal: AbortSignal.timeout(10_000) }).then((r) => {
      const b = parseBuild(r);
      if (!b) throw new ApiError(500, "the daemon answered without a build");
      return b;
    }),
  vexDownload: (projectId: string, body: { slot: number; name: string }) =>
    req<{ terminal_id: string }>(`/projects/${encodeURIComponent(projectId)}/vex/download`, json(body)),
  vexBrain: () => req<{ terminal_id: string; started: boolean }>("/vex/brain", json({})),
  vexInfo: (read: "system" | "programs" = "system") =>
    req<{ text: string; exit_code: number | null; read?: string }>("/vex/device/info", json({ read })),
  // A new VEX project (VX-3): the template list is the daemon's, and the
  // folder shown is the daemon's answer to the body Create sends.
  vexTemplates: () => req<unknown>("/vex/templates").then(parseTemplates),
  vexCheckNew: (body: NewBody) =>
    req<unknown>("/vex/projects/check", json(body)).then((r) => {
      const c = parseNewCheck(r);
      if (!c) throw new ApiError(500, "the daemon's answer was not a folder");
      return c;
    }),
  vexNewProject: (body: NewBody) =>
    req<unknown>("/vex/projects", json(body)).then((r) => {
      const n = parseNewRequest(r);
      if (!n) throw new ApiError(500, "the daemon answered without a request");
      return n;
    }),
  vexNewState: (id: string) =>
    req<unknown>(`/vex/projects/${encodeURIComponent(id)}`).then((r) => {
      const n = parseNewRequest(r);
      if (!n) throw new ApiError(500, "the daemon answered without a request");
      return n;
    }),

  // --- speech -------------------------------------------------------------
  /** `utteranceId`/`seq` name the clip for the daemon, which echoes them; `budgetMs` is the deadline it must fit inside (V2). */
  async stt(blob: Blob, opts: { signal?: AbortSignal; utteranceId?: number; seq?: number; budgetMs?: number } = {}): Promise<string> {
    const ids = new URLSearchParams();
    if (Number.isSafeInteger(opts.utteranceId) && opts.utteranceId! >= 0) ids.set("utterance_id", String(opts.utteranceId));
    if (Number.isSafeInteger(opts.seq) && opts.seq! >= 0) ids.set("seq", String(opts.seq));
    // The client's whole deadline for this attempt: the daemon fits its model chain inside it.
    if (Number.isSafeInteger(opts.budgetMs) && opts.budgetMs! > 0) ids.set("budget_ms", String(Math.round(opts.budgetMs!)));
    const res = await fetch(ids.size ? `/stt?${ids}` : "/stt", {
      method: "POST",
      headers: { "Content-Type": blob.type || "audio/wav" },
      body: blob,
      signal: opts.signal,
    });
    if (!res.ok) {
      // The daemon says which models failed and how (`speech-to-text
      // unavailable: … (HTTP 404)`); show that rather than a bare status.
      let message = `stt ${res.status}`;
      try {
        const err = await res.json();
        if (err && typeof err.error === "string" && err.error) message = err.error;
      } catch {
        /* not JSON: keep the status */
      }
      throw new ApiError(res.status, message);
    }
    const body = await res.json();
    return body.text || "";
  },
  /**
   * One chunk of speech as audio bytes (`POST /say`). Read-aloud asks
   * `localOnly`, so the daemon never sends the text to a cloud voice; a local
   * failure is a 503 and the chunk is skipped. An unknown `voice` is a 400.
   */
  async say(text: string, opts: { voice?: string | null; localOnly?: boolean; signal?: AbortSignal } = {}): Promise<ArrayBuffer> {
    const body: Record<string, unknown> = { text };
    if (opts.voice) body.voice = opts.voice;
    if (opts.localOnly) body.local_only = true;
    const res = await fetch("/say", { ...json(body), signal: opts.signal });
    if (!res.ok) throw new ApiError(res.status, `say ${res.status}`);
    return res.arrayBuffer();
  },
};

/**
 * The event stream. One connection for the window; filters are applied here
 * rather than per-panel so a late subscriber cannot miss a frame another
 * panel already consumed.
 */
export function subscribe(onEvent: (e: any) => void, onReopen?: () => void): () => void {
  let source: EventSource | null = null;
  let closed = false;
  let opened = 0;
  let retry: ReturnType<typeof setTimeout> | null = null;
  const open = () => {
    if (closed || source) return;
    source = new EventSource("/events" + (isWindowId(WINDOW_ID) ? "?window=" + WINDOW_ID : ""));
    const current = source;
    // Every (re)connect, as a pseudo-record: what the window holds from
    // before a daemon restart (a running turn, say) is read again. And every
    // open after the first is a reconnect: whatever was published in the gap
    // is gone, so the window re-reads what it cannot afford to have missed
    // (a turn finishing, above all).
    source.onopen = () => {
      if (closed || source !== current) return;
      try {
        onEvent({ kind: "_connected" });
      } catch {
        /* never a crash */
      }
      opened += 1;
      if (opened > 1) onReopen?.();
    };
    source.onmessage = (m) => {
      if (closed || source !== current) return;
      try {
        onEvent(JSON.parse(m.data));
      } catch {
        /* a frame we cannot parse is a frame we ignore, never a crash */
      }
    };
    source.onerror = () => {
      if (closed || source !== current) return;
      onEvent({ kind: "_disconnected" });
      // The daemon restarts; the window reconnects rather than going quiet.
      source?.close();
      source = null;
      if (!closed) retry = setTimeout(() => { retry = null; open(); }, 1000);
    };
  };
  const hide = () => {
    closed = true; source?.close(); source = null;
    if (retry) clearTimeout(retry);
    retry = null; onEvent({ kind: "_disconnected" });
  };
  const show = (e: PageTransitionEvent) => { if (e.persisted) { closed = false; open(); } };
  window.addEventListener("pagehide", hide);
  window.addEventListener("pageshow", show);
  open();
  return () => {
    hide();
    window.removeEventListener("pagehide", hide);
    window.removeEventListener("pageshow", show);
  };
}
