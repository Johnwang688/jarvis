// The window. One SSE subscription writes lifecycle state; every panel reads
// the store. The orb, the capture pipeline and the wake word live here because
// they are properties of the *window*, not of whichever tab is showing.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, subscribe } from "./api";
import { useStore, currentProject, currentProjectId, currentTask, type Tab } from "./state/store";
import { Sidebar } from "./components/Sidebar";
import { ChatTab } from "./components/ChatTab";
import { InputBar } from "./components/InputBar";
import { TaskView } from "./components/TaskView";
import { FileTab } from "./components/FileTab";
import { DiffTab } from "./components/DiffTab";
import { PreviewTab } from "./components/PreviewTab";
import { ApprovalQueue, ApprovalVeil } from "./components/Approvals";
import { DecisionsLog, DiscordPanel, SchedulesButton, UsagePanel } from "./components/Panels";
import { AvatarPicker, ModelPicker, NewProject, NewTask, SettingsDialog, VoicePicker } from "./components/Pickers";
import { ScheduleDialog } from "./components/ScheduleDialog";
import { Orb } from "./components/Orb";
import { Capture } from "./lib/capture";
import { HINTS, isMuted, loadMode, outcomeFor, saveMode, type DictationMode } from "./lib/dictation";
import { WakeGate, compileWake, matchesWake, WAKE_PATTERNS } from "./lib/wake";
import type { Attachment, AvatarDesc, Schedule, VoiceEntry } from "./types";
import type { RosterView } from "./lib/roster";
import { moveThreadTo } from "./lib/threads";
import { lastProject, loadLastProject, saveLastProject } from "./lib/compose";
import { composeChoice, threadBody } from "./lib/threadmodel";
import { useThreadModel } from "./components/ThreadModelControls";
import { ProjectDialog } from "./components/Pickers";
import { ArchiveConfirm, ArchiveView } from "./components/Archive";
import { afterProjectGone, afterThreadGone, forgetLastProject, projectNamesTaken } from "./lib/projects";
import { guildConfigured, ownerLine } from "./lib/discord";
import { CollapseButton, Rail, Splitter, ZoomControl, useLayout } from "./components/Layout";
import { ZOOM_DEFAULT, maxWidth } from "./lib/layout";

const TABS: Tab[] = ["chat", "task", "file", "diff", "preview"];
const PROPOSAL_WINDOW_MS = 60_000;
/** Events after which an open confirmation or the Archive reads again. */
const LIFECYCLE_KINDS = new Set([
  "task_created", "task_status_changed", "project_created", "project_updated", "project_archived",
  "project_restored", "project_deleted", "thread_archived", "thread_restored", "thread_deleted",
  "thread_updated", "turn_started", "turn_finished",
]);

export default function App() {
  const { state, dispatch } = useStore();
  // Zoom, pane widths and folded panes (§18, 2026-10-08): lib/layout.ts.
  // Every layout key is inert while an authorization card is up (as PTT is).
  const view = useLayout(state.approvals.length > 0);
  const [avatars, setAvatars] = useState<AvatarDesc[]>([]);
  // The fast path's roster and its default (`GET /models`), re-read on every
  // `model` broadcast so another window's change relabels this one.
  const [roster, setRoster] = useState<RosterView | null>(null);
  const [voices, setVoices] = useState<VoiceEntry[]>([]);
  const [voiceOverride, setVoiceOverride] = useState("");
  const [avatarCacheBust, setAvatarCacheBust] = useState(0);
  const [editingSchedule, setEditingSchedule] = useState<Schedule | null>(null);
  // "+ new task" belongs to the row it was clicked under, not to a selection.
  const [newTaskProject, setNewTaskProject] = useState<string | null>(null);
  // The project the edit or archive dialog is about (decisions B), and a
  // counter every task/project/thread event bumps so open confirmations and
  // the Archive view read the backend again rather than trusting a count.
  const [projectTarget, setProjectTarget] = useState<string | null>(null);
  const projectTargetRef = useRef<string | null>(null);
  projectTargetRef.current = projectTarget;
  const [lifecycle, setLifecycle] = useState(0);

  // `live` gives the capture callbacks — which run on the audio thread's
  // cadence, not React's — the current state without stale closures.
  const live = useRef(state);
  live.current = state;
  // A thread opened for a compose send, before it becomes `threadId`: its
  // events are this window's from the moment it exists.
  const pendingThread = useRef<string | null>(null);
  // The thread whose transcript must not be reloaded when it becomes the open
  // one, because the window already holds it: the first message, drawn
  // optimistically, and a reply that may already be streaming.
  const skipReload = useRef<string | null>(null);
  // provider ▾ · model ▾ · effort ▾ in the input bar (decisions 2026-10-06, A).
  const threadModel = useThreadModel(state, dispatch, () => void loadModels());
  const reloadThreadModels = useRef(threadModel.reload);
  reloadThreadModels.current = threadModel.reload;
  // The chip's own dialogs (catalogue, provider default) are hook state, not
  // `state.picker`, but they are open pickers all the same: no open mic under
  // them (PR #15 review).
  const chipOverlay = useRef(false);
  chipOverlay.current = threadModel.overlayOpen;

  const patch = useCallback((p: Parameters<typeof dispatch>[0] extends any ? any : never) => {
    dispatch({ type: "patch", patch: p });
  }, [dispatch]);

  // ---- capture ------------------------------------------------------------

  const capture = useMemo(
    () =>
      new Capture({
        // He does not answer himself, and never talks over a pending
        // authorization or an open picker.
        suppressed: () =>
          live.current.orb === "speaking" ||
          live.current.busy ||
          live.current.approvals.length > 0 ||
          live.current.picker !== null ||
          chipOverlay.current,
        muted: () => isMuted(live.current.dictation),
        onLevel: (level) => dispatch({ type: "patch", patch: { level } }),
        onListening: () =>
          dispatch({ type: "patch", patch: { orb: "listening", status: "LISTENING · SPEAK NOW" } }),
        onIdle: (note) => dispatch({ type: "patch", patch: { orb: "idle", status: note || "" } }),
        onUtterance: (wav) => void onUtterance(wav),
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  const onUtterance = useCallback(
    async (wav: Blob) => {
      const mode = live.current.dictation;
      const what = outcomeFor(mode);
      // OFF never reaches here (Capture refuses to claim while muted), but the
      // mode is checked again rather than assumed: the fail-closed direction
      // costs a discarded utterance and the other costs an upload.
      if (what === "discard") return;
      dispatch({ type: "patch", patch: { orb: "transcribing", status: "TRANSCRIBING" } });
      let text = "";
      try {
        text = await api.stt(wav);
      } catch (e: any) {
        dispatch({ type: "patch", patch: { orb: "error", status: "STT FAILED", error: e.message } });
        return;
      }
      if (!text.trim()) {
        dispatch({ type: "patch", patch: { orb: "idle", status: "DIDN'T CATCH THAT" } });
        return;
      }
      if (what === "review") {
        // REVIEW never sends on its own: the transcript lands in the box.
        dispatch({ type: "patch", patch: { orb: "idle", status: "", pendingTranscript: text } });
        return;
      }
      // Dictated: the Discord mirror labels it "You (HUD, voice)" (PR C).
      await send(text, [], true);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // ---- sending ------------------------------------------------------------

  const send = useCallback(
    async (text: string, attachments: Attachment[], spoken = false) => {
      const at = live.current;
      const before = at.messages;
      let threadId = at.threadId;
      const compose = at.compose;
      dispatch({ type: "message", message: { role: "user", text } });
      dispatch({ type: "patch", patch: { busy: true, orb: "thinking", status: "SENDING", draft: "", error: "" } });
      try {
        let projectId = threadId ? at.threads.find((t) => t.id === threadId)?.project_id ?? null : null;
        if (!threadId) {
          // Composing: the thread is opened in the chip's project on the first
          // message, and only then. Nothing reaches the server before it.
          projectId = compose?.projectId ?? null;
          if (!projectId) throw new Error("Create a project first.");
          threadId = compose?.openedId || null;
          if (!threadId) {
            dispatch({ type: "patch", patch: { status: "OPENING THREAD" } });
            // The provider, model and effort chosen while composing ride the
            // first message; a default sends no model (decisions A1, A5).
            const t = await api.openThread({
              project_id: projectId, role: "chat", ...threadBody(composeChoice(compose)),
            });
            threadId = t.id;
            // Remembered, so a retry after a failed send reuses this thread.
            // The whole compose row is kept, provider, model and effort with
            // it, so a failed first send retries on the same choice.
            dispatch({ type: "patch", patch: { compose: { ...compose, projectId, openedId: t.id } } });
          }
          pendingThread.current = threadId;
        }
        dispatch({ type: "patch", patch: { turnThreadId: threadId } });
        await api.send(threadId, {
          text, attachments: attachments.length ? attachments : undefined,
          ...(spoken ? { spoken: true } : {}),
        });
        if (projectId) saveLastProject(projectId);
        if (!at.threadId) {
          // The compose row becomes the thread. Its transcript is already on
          // screen; reloading it here is what used to wipe the first message.
          skipReload.current = threadId;
          await refreshThreads();
          dispatch({ type: "patch", patch: { threadId, compose: null } });
          pendingThread.current = null;
        }
        if (live.current.busy && live.current.turnThreadId === threadId && live.current.status === "SENDING") {
          dispatch({ type: "patch", patch: { status: "THINKING" } });
        }
      } catch (e: any) {
        // The words go back in the box, so a failed send costs nothing.
        dispatch({
          type: "patch",
          patch: {
            busy: false, turnThreadId: null, orb: "error", status: "FAILED",
            error: `Could not send: ${e.message}`, messages: before, pendingTranscript: text,
          },
        });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // ---- events -------------------------------------------------------------

  useEffect(() => {
    const stop = subscribe((e: any) => {
      const kind = e.kind;
      const data = e.data || e;
      const at = live.current;
      // On screen: the thread the chat pane shows (or is about to). Ours: the
      // thread whose turn this window started. They differ when the owner
      // switches threads mid-turn, and the window must still hear that turn
      // finish, or it waits on THINKING with the mic suppressed for good.
      const shown = at.threadId ?? at.compose?.openedId ?? pendingThread.current;
      const tid: string | undefined = e.thread_id;
      const mine = !tid || tid === shown;
      const ours = !!tid && tid === at.turnThreadId;
      if (LIFECYCLE_KINDS.has(kind)) setLifecycle((n) => n + 1);
      switch (kind) {
        case "user_message":
          // PR C: a message the owner typed in Discord (a chat's thread or the
          // DM) appears in the open chat as theirs, labelled. A HUD message is
          // already on screen from send(), so it is never added twice.
          if (mine && tid && (data.via === "discord" || data.via === "dm")) {
            dispatch({
              type: "message",
              message: { role: "user", text: ownerLine(data), via: data.via },
            });
          }
          break;
        case "turn_started":
          if (mine) {
            dispatch({
              type: "patch",
              patch: { busy: true, orb: "thinking", draft: "", turnThreadId: at.turnThreadId ?? tid ?? null },
            });
          }
          break;
        case "text_delta":
          if (mine) dispatch({ type: "patch", patch: { status: "RESPONDING" } });
          if (mine) dispatch({ type: "delta", text: data.text || "" });
          break;
        case "text":
          if (mine) dispatch({ type: "settle", text: data.text || "" });
          break;
        case "tool_started":
          if (mine) dispatch({ type: "patch", patch: { status: `RUNNING · ${data.name || "tool"}`, orb: "tool" } });
          if (mine)
            dispatch({
              type: "op_start",
              op: { call_id: data.call_id || String(Date.now()), name: data.name || "tool", started: Date.now() },
            });
          break;
        case "tool_finished":
          if (mine) dispatch({ type: "patch", patch: { status: "THINKING", orb: "thinking" } });
          if (mine)
            dispatch({
              type: "op_done",
              call_id: data.call_id || "",
              ok: data.ok !== false,
              summary: data.summary || "",
            });
          break;
        case "turn_finished":
          if (ours) {
            dispatch({ type: "patch", patch: { busy: false, turnThreadId: null, orb: "idle", status: "" } });
            // Only this window's own turn opens the follow-up window. A Discord
            // turn finishing must not start the HUD listening.
            capture.openFollowUp();
          }
          break;
        case "error":
          if (ours)
            dispatch({
              type: "patch",
              patch: { busy: false, turnThreadId: null, orb: "error", error: data.message || "error" },
            });
          else if (mine) dispatch({ type: "patch", patch: { orb: "error", error: data.message || "error" } });
          break;
        case "approval_requested":
          // Only the broker asks the owner, and every broker question carries
          // a code. A record without one is a provider saying "the gate was
          // consulted", which the daemon no longer publishes; if one ever
          // arrives it must not flash a card nobody can answer.
          if (!data.code) break;
          dispatch({ type: "approval_add", request: { ...data, req_id: data.req_id } });
          break;
        case "approval_resolved":
          dispatch({ type: "approval_drop", req_id: data.req_id });
          break;
        case "proposal_reply":
          // A proposal is a system line with a 60-second cancel: the fast path
          // has already answered and a task is about to run. Only in the
          // thread that proposed it, never whichever thread happens to be open.
          void refreshTasks();
          if (!(tid && (tid === shown || ours))) break;
          dispatch({
            type: "message",
            message: {
              role: "system",
              text: data.reply || "",
              proposal: data.task_id
                ? { task_id: data.task_id, until: Date.now() + PROPOSAL_WINDOW_MS }
                : undefined,
            },
          });
          break;
        case "task_created":
        case "task_status_changed":
        case "task_question":
          void refreshTasks();
          break;
        case "thread_opened":
        case "thread_closed":
        case "thread_moved":
          void refreshThreads();
          break;
        case "thread_updated": {
          // Two producers: a model/effort change sends the whole record
          // (`changed: ["model","effort"]`), patched in place so a click's
          // stale closure cannot undo it; a rename sends only `{thread_id,
          // title, changed}`, and the list is read again. The envelope keys
          // are never spread onto the thread.
          const threadId = tid || data.thread_id;
          if (threadId && data.id === threadId) {
            const { thread_id: _t, changed: _c, effective_model: _m, effective_effort: _e, ...record } = data;
            dispatch({ type: "thread_patch", id: threadId, patch: record });
          } else {
            void refreshThreads();
          }
          break;
        }
        case "model_set":
          // Every model and effort change is a line in the chat (A7).
          if (tid && tid === shown) dispatch({ type: "message", message: { role: "system", text: data.text || "" } });
          break;
        case "discord_status":
          // The event says something changed; the route says what, filtered.
          api.discord().then((discord) => dispatch({ type: "patch", patch: { discord } })).catch(() => {});
          break;
        case "usage_updated":
          api.usage().then((usage) => dispatch({ type: "patch", patch: { usage } })).catch(() => {});
          break;
        case "schedule_fired":
          api.schedules().then((schedules) => dispatch({ type: "patch", patch: { schedules } })).catch(() => {});
          break;
        case "avatar":
          void loadAvatar();
          break;
        case "model":
          void loadModels();
          // A default thread's label names the global choice; re-read it.
          void reloadThreadModels.current();
          break;
        case "voice":
          void loadVoices();
          break;
        case "mute":
          break;
        // --- projects and threads changing in this or another window (B) ---
        case "project_created":
        case "project_updated":
        case "project_restored":
          void refreshProjects(e.project_id || data.project_id);
          if (kind === "project_restored") {
            void refreshThreads();
            void refreshTasks();
            void refreshSchedules();
            void refreshArchivedNames();
          }
          break;
        case "project_archived":
        case "project_deleted":
          projectGone(e.project_id || data.project_id, kind === "project_archived" ? "archived" : "deleted");
          void refreshArchivedNames();
          break;
        case "thread_restored":
          void refreshThreads();
          break;
        case "thread_archived":
        case "thread_deleted":
          threadGone(e.thread_id || data.thread_id);
          break;
        default:
          break;
      }
    });
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch]);

  // ---- loading ------------------------------------------------------------

  const refreshTasks = useCallback(async () => {
    try {
      const tasks = await api.tasks();
      dispatch({ type: "patch", patch: { tasks } });
      const id = live.current.taskId;
      if (id) {
        const threads = await api.taskThreads(id).catch(() => []);
        dispatch({ type: "patch", patch: { taskThreads: { ...live.current.taskThreads, [id]: threads } } });
      }
    } catch {
      /* the window keeps what it had rather than blanking */
    }
  }, [dispatch]);

  const refreshThreads = useCallback(async () => {
    try {
      dispatch({ type: "patch", patch: { threads: await api.threads() } });
    } catch {
      /* keep what we had */
    }
  }, [dispatch]);

  /** Projects again, and the platform of the one that changed (its root may have). */
  const refreshProjects = useCallback(
    async (changed?: string | null) => {
      try {
        const projects = await api.projects();
        const platforms = { ...live.current.platforms };
        await Promise.all(
          projects
            .filter((p) => p.id === changed || !platforms[p.id])
            .map(async (p) => {
              platforms[p.id] = await api.platform(p.id).catch(() => ({ platform: "wsl", note: null }));
            }),
        );
        dispatch({ type: "patch", patch: { projects, platforms } });
        return projects;
      } catch {
        return live.current.projects; /* keep what we had */
      }
    },
    [dispatch],
  );

  /**
   * A project went away — archived or deleted, here or in another window. Its
   * rows go, a conversation inside it closes into a new thread in the last
   * project worked in, the remembered project forgets it, and a dialog open
   * on it closes and says why (lib/projects.ts decides all of it).
   */
  const projectGone = useCallback(
    (id: string, how: "archived" | "deleted") => {
      if (!id) return;
      const at = live.current;
      const name = at.projects.find((p) => p.id === id)?.name || "the project";
      forgetLastProject(id);
      const next = afterProjectGone(at, id, loadLastProject());
      const platforms = { ...at.platforms };
      delete platforms[id];
      const patchOut: Record<string, unknown> = { ...next, platforms };
      delete patchOut.displaced;
      if (next.displaced) {
        Object.assign(patchOut, { messages: [], draft: "", ops: [], tab: "chat", taskFocus: false,
                                  status: `${name} was ${how}`.toUpperCase() });
      }
      if ((at.picker === "editProject" || at.picker === "archiveProject") && projectTargetRef.current === id) {
        patchOut.picker = null;
        if (at.picker === "editProject") patchOut.error = `${name} was ${how} while you were editing it.`;
      }
      dispatch({ type: "patch", patch: patchOut as any });
      void refreshThreads();
      void refreshTasks();
      void refreshSchedules();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  /** One chat thread went away (archived or deleted). */
  const threadGone = useCallback(
    (id: string) => {
      if (!id) return;
      const next = afterThreadGone(live.current, id);
      const patchOut: Record<string, unknown> = { threads: next.threads, threadId: next.threadId,
                                                  compose: next.compose };
      if (next.displaced) Object.assign(patchOut, { messages: [], draft: "", ops: [] });
      dispatch({ type: "patch", patch: patchOut as any });
      void refreshThreads();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  /**
   * Move a chat thread to another project. Optimistic — the row follows the
   * drop immediately — and **reverted from the list captured before it**, with
   * the backend's own words, because the two refusals that matter (a task
   * thread, 409; a project that went away, 404) are exactly the cases where a
   * row left in the wrong place would be believed.
   */
  const moveThread = useCallback(
    (threadId: string, projectId: string) => {
      const before = live.current.threads;
      const after = moveThreadTo(before, threadId, projectId);
      if (after === before) return; // nothing to do: no PATCH, no flash
      dispatch({ type: "patch", patch: { threads: after, moveError: "" } });
      api
        .moveThread(threadId, projectId)
        .then(() => refreshThreads())
        .catch((e) =>
          dispatch({ type: "patch", patch: { threads: before, moveError: e.message } }),
        );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // A refused move explains itself, then gets out of the way.
  useEffect(() => {
    if (!state.moveError) return;
    const t = setTimeout(() => dispatch({ type: "patch", patch: { moveError: "" } }), 6000);
    return () => clearTimeout(t);
  }, [state.moveError, dispatch]);

  /** Start a new thread: a compose row in the last project worked in. */
  const newThread = useCallback(
    (projectId?: string | null) => {
      const at = live.current;
      pendingThread.current = null;
      dispatch({
        type: "patch",
        patch: {
          threadId: null,
          compose: { projectId: projectId ?? lastProject(at.projects, at.threads, loadLastProject()) },
          taskFocus: false,
          tab: "chat",
          messages: [],
          draft: "",
          ops: [],
          error: "",
          status: at.turnThreadId ? at.status : "",
        },
      });
    },
    [dispatch],
  );

  /** Archived projects' names, so a rename preview numbers the way the backend will. */
  const refreshArchivedNames = useCallback(
    () =>
      api
        .archive()
        .then((v) => dispatch({ type: "patch", patch: { archivedNames: v.projects.map((p) => p.name) } }))
        .catch(() => {}),
    [dispatch],
  );

  const refreshSchedules = useCallback(
    () =>
      api
        .schedules()
        .then((schedules) => dispatch({ type: "patch", patch: { schedules } }))
        .catch(() => {}),
    [dispatch],
  );

  const loadAvatar = useCallback(async () => {
    try {
      const r = await api.avatars();
      setAvatars(r.avatars || []);
      const active = (r.avatars || []).find((a) => a.slug === r.active) || null;
      setAvatarCacheBust(Date.now());
      dispatch({
        type: "patch",
        patch: {
          avatar: active,
          // Every degradation path leaves him summonable: with no avatar, or
          // an avatar with no stated phrase, the built-in still fires.
          wakePatterns: compileWake(active?.wake_source || active?.wake) || WAKE_PATTERNS,
        },
      });
    } catch {
      dispatch({ type: "patch", patch: { wakePatterns: WAKE_PATTERNS.slice() } });
    }
  }, [dispatch]);

  const loadModels = useCallback(async () => {
    try {
      const r = await api.models();
      setRoster({ ...r, models: r.models || [] });
    } catch {
      /* the picker just has nothing to show */
    }
  }, []);

  const rosterChanged = useCallback((r: RosterView) => {
    setRoster({ ...r, models: r.models || [] });
    void reloadThreadModels.current();
  }, []);

  const loadVoices = useCallback(async () => {
    try {
      const r = await api.voices();
      setVoices(r.voices || []);
      setVoiceOverride(r.override || "");
    } catch {
      /* ditto */
    }
  }, []);

  useEffect(() => {
    dispatch({ type: "patch", patch: { dictation: loadMode() } });
    (async () => {
      const [projects, threads, tasks] = await Promise.all([
        api.projects().catch(() => []),
        api.threads().catch(() => []),
        api.tasks().catch(() => []),
      ]);
      const platforms: Record<string, any> = {};
      await Promise.all(
        projects.map(async (p) => {
          platforms[p.id] = await api.platform(p.id).catch(() => ({ platform: "wsl", note: null }));
        }),
      );
      dispatch({
        type: "patch",
        patch: {
          projects, threads, tasks, platforms,
          // The window opens on a new thread in the last project worked in,
          // as Claude Code opens on a new session. Nothing is sent until
          // the owner speaks.
          threadId: null,
          compose: { projectId: lastProject(projects, threads, loadLastProject()) },
        },
      });
      const [usage, schedules, route, approvals, discord] = await Promise.all([
        api.usage().catch(() => null),
        api.schedules().catch(() => []),
        api.route().catch(() => null),
        api.approvals().catch(() => []),
        api.discord().catch(() => null),
      ]);
      dispatch({ type: "patch", patch: { usage, schedules, route, approvals, discord } });
      await Promise.all([loadAvatar(), loadModels(), loadVoices(), refreshArchivedNames()]);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A thread's transcript is loaded when it becomes the live one, so switching
  // redraws from the saved conversation rather than from whatever is on screen.
  useEffect(() => {
    if (!state.threadId) return;
    if (skipReload.current === state.threadId) {
      skipReload.current = null;
      return;
    }
    // A turn running in another thread keeps the window busy, but its status
    // line is about that thread, not this one.
    const status = live.current.turnThreadId === state.threadId ? live.current.status : "";
    api
      .transcript(state.threadId)
      .then((r) => dispatch({ type: "patch", patch: { messages: r.messages || [], draft: "", ops: [], status } }))
      .catch(() => dispatch({ type: "patch", patch: { messages: [], draft: "", ops: [], status } }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.threadId]);

  useEffect(() => {
    if (!state.taskId) return;
    api
      .taskThreads(state.taskId)
      .then((threads) =>
        dispatch({ type: "patch", patch: { taskThreads: { ...live.current.taskThreads, [state.taskId!]: threads } } }),
      )
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.taskId]);

  // ---- wake word ----------------------------------------------------------

  useEffect(() => {
    const SR: any = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return;
    if (isMuted(state.dictation)) return; // a muted mic stops the recognizer outright
    const gate = new WakeGate();
    let recog: any = new SR();
    let dead = false;
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = "en-US";
    recog.onstart = () => gate.restart();
    recog.onresult = (e: any) => {
      let text = "";
      // Concatenated from resultIndex: a two-word phrase can straddle a
      // segment boundary, and matching the pieces separately would miss it.
      for (let i = e.resultIndex; i < e.results.length; i++) text += e.results[i][0].transcript;
      const patterns = live.current.wakePatterns.length ? live.current.wakePatterns : WAKE_PATTERNS;
      if (!matchesWake(text, patterns)) return;
      if (!gate.shouldFire(e.results.length - 1, performance.now())) return;
      onWakeHit();
    };
    recog.onend = () => {
      if (dead) return;
      setTimeout(() => {
        gate.restart();
        try {
          recog.start();
        } catch {
          /* already running */
        }
      }, 300);
    };
    try {
      recog.start();
    } catch {
      /* already running */
    }
    (window as any).__hudRecog = recog;
    return () => {
      dead = true;
      try {
        recog.onend = null;
        recog.stop();
      } catch {
        /* nothing to stop */
      }
      recog = null;
      // The handle has to go with it: a stopped recognizer that still answers
      // "yes, I am here" is exactly the kind of state a surface confabulates
      // around — and here it would say the mic is still streaming to Google's
      // speech service when it is not.
      delete (window as any).__hudRecog;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.dictation]);

  const onWakeHit = useCallback(() => {
    if (live.current.approvals.length) return; // never talk over a pending authorization
    if (live.current.picker || chipOverlay.current) return;
    capture.onWake();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capture]);

  // ---- push to talk -------------------------------------------------------

  const press = useCallback(() => {
    if (live.current.approvals.length) return; // answer the authorization first
    if (live.current.picker || chipOverlay.current) return;
    // The interrupt half comes first: muting yourself must not take away the
    // orb as the way to shut him up.
    const turn = live.current.turnThreadId;
    if (live.current.busy && turn) api.interrupt(turn).catch(() => {});
    if (isMuted(live.current.dictation)) {
      dispatch({ type: "patch", patch: { status: "MIC MUTED" } });
      return;
    }
    capture.press();
  }, [capture, dispatch]);

  const release = useCallback(() => capture.release(), [capture]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code === "Space" && !e.repeat) {
        e.preventDefault();
        press();
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        e.preventDefault();
        release();
      }
    };
    document.addEventListener("keydown", down);
    document.addEventListener("keyup", up);
    return () => {
      document.removeEventListener("keydown", down);
      document.removeEventListener("keyup", up);
    };
  }, [press, release]);

  // ---- microphone ---------------------------------------------------------

  useEffect(() => {
    let ctx: AudioContext | null = null;
    let stream: MediaStream | null = null;
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        ctx = new AudioContext();
        const ratio = ctx.sampleRate / 16000;
        const src = ctx.createMediaStreamSource(stream);
        const node = ctx.createScriptProcessor(2048, 1, 1);
        let acc = 0, n = 0, pos = 0;
        node.onaudioprocess = (e) => {
          const frame = e.inputBuffer.getChannelData(0);
          const out = new Float32Array(Math.ceil(frame.length / ratio) + 1);
          let k = 0;
          for (let i = 0; i < frame.length; i++) {
            acc += frame[i];
            n++;
            pos += 1;
            if (pos >= ratio) {
              out[k++] = acc / n;
              pos -= ratio;
              acc = 0;
              n = 0;
            }
          }
          capture.onFrame(out.subarray(0, k));
        };
        src.connect(node);
        const mute = ctx.createGain();
        mute.gain.value = 0;
        node.connect(mute).connect(ctx.destination);
      } catch {
        // A window without a mic grant still types; every other affordance
        // stays exactly as it was.
      }
    })();
    return () => {
      stream?.getTracks().forEach((t) => t.stop());
      ctx?.close().catch(() => {});
    };
  }, [capture]);

  // ---- test hooks ---------------------------------------------------------

  useEffect(() => {
    (window as any).__hud = {
      capture,
      mic: {
        feedMs: (ms: number, amp: number, frame?: number) => capture.feedMs(ms, amp, frame),
        wake: () => onWakeHit(),
        state: () => ({
          open: capture.vad.open,
          thresh: capture.vad.thresh,
          claimed: capture.segClaimed,
          written: capture.ring.written,
        }),
      },
      state: () => live.current,
      dispatch,
    };
  }, [capture, dispatch, onWakeHit]);

  // ---- actions ------------------------------------------------------------

  const setMode = (mode: DictationMode) => {
    const wasMuted = isMuted(live.current.dictation);
    saveMode(mode);
    dispatch({ type: "patch", patch: { dictation: mode, status: "" } });
    if (wasMuted && !isMuted(mode)) capture.markUnmute();
    if (isMuted(mode)) capture.closeFollowUp();
    api.mute(isMuted(mode)).catch(() => {});
  };

  const decide = async (reqId: string, allow: boolean, always = false) => {
    try {
      await api.resolveApproval(reqId, { decision: allow ? "allow" : "deny", always });
    } catch {
      /* the broker may have timed out already; the card goes either way */
    }
    dispatch({ type: "approval_drop", req_id: reqId });
  };

  const project = currentProject(state);
  const activeProjectId = currentProjectId(state);
  const openThread = state.threadId ? state.threads.find((t) => t.id === state.threadId) || null : null;
  const task = currentTask(state);
  const drawn = view.fitted;
  const hint =
    state.status ||
    (state.approvals.length ? "ANSWER THE AUTHORIZATION" : HINTS[state.dictation]);

  return (
    <>
      <div
        id="shell"
        className={(drawn.leftFolded ? "left-collapsed " : "") + (drawn.rightFolded ? "right-collapsed" : "")}
        style={{ ["--left-w" as any]: `${drawn.left}px`, ["--right-w" as any]: `${drawn.right}px` }}
      >
        {drawn.leftFolded ? (
          <Rail side="left" auto={drawn.autoLeft} onExpand={() => view.open("left")} onNewThread={() => newThread()} />
        ) : null}
        <Sidebar
          zoom={view.zoom}
          onCollapse={() => view.fold("left")}
          projects={state.projects}
          archivedNames={state.archivedNames}
          platforms={state.platforms}
          threads={state.threads}
          tasks={state.tasks}
          taskThreads={state.taskThreads}
          activeProjectId={activeProjectId}
          compose={state.compose}
          threadId={state.threadId}
          taskId={state.taskId}
          onPickThread={(id) => {
            pendingThread.current = null;
            patch({ threadId: id, compose: null, taskFocus: false, tab: "chat" });
          }}
          onPickTask={(id) => patch({ taskId: id, taskFocus: true, tab: "task" })}
          onNewProject={() => patch({ picker: "newProject" })}
          onNewThread={() => newThread()}
          onNewTask={(projectId) => {
            setNewTaskProject(projectId);
            patch({ picker: "newTask" });
          }}
          moveError={state.moveError}
          onMoveThread={moveThread}
          onMoveCompose={(projectId) =>
            state.compose && patch({ compose: { ...state.compose, projectId } })
          }
          onEditProject={(id) => {
            setProjectTarget(id);
            patch({ picker: "editProject" });
          }}
          onArchiveProject={(id) => {
            setProjectTarget(id);
            patch({ picker: "archiveProject" });
          }}
          onRenameProject={(id, name) =>
            api.patchProject(id, { name }).then(async (saved) => {
              await refreshProjects(id);
              return saved;
            })
          }
          onRenameThread={(id, title) =>
            api.renameThread(id, title).then(async (saved) => {
              await refreshThreads();
              return saved;
            })
          }
          onArchiveThread={(id) => api.archiveThread(id).then(() => threadGone(id))}
          onOpenArchive={() => patch({ picker: "archive" })}
          onOpen={(what) => {
            if (what === "schedules") {
              setEditingSchedule(null);
              patch({ picker: "schedule" });
              return;
            }
            if (what === "route") {
              patch({ picker: "settings" });
              return;
            }
            // The usage meters live in the status pane: a folded one opens first.
            view.open("right");
            setTimeout(() => document.querySelector('[data-testid="usage"]')?.scrollIntoView({ block: "nearest" }), 0);
          }}
        />
        {drawn.leftFolded ? null : (
          <Splitter
            side="left"
            width={drawn.left}
            max={maxWidth("left", drawn.right, view.available)}
            zoom={view.zoom}
            onResize={(w) => view.setWidth("left", w)}
            onReset={() => view.resetWidth("left")}
          />
        )}

        <div className="pane" id="main">
          <div id="tabs">
            {TABS.map((t) => (
              <button
                type="button"
                key={t}
                data-testid={`tab-${t}`}
                className={state.tab === t ? "on" : ""}
                onClick={() => patch({ tab: t })}
              >
                {t}
              </button>
            ))}
            <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center", paddingRight: 8 }}>
              {project ? (
                <select
                  data-testid="profile"
                  style={{ width: 110 }}
                  value={project.profile}
                  title="Applies to threads and task workers opened from now on."
                  onChange={(e) =>
                    api
                      .patchProject(project.id, { profile: e.target.value })
                      .then(() => api.projects())
                      .then((projects) => patch({ projects }))
                      // Every refusal is shown (§18): a select that snaps back
                      // with no word is a profile the owner believes changed.
                      .catch((err) => patch({ error: `Could not change the profile: ${err.message}` }))
                  }
                >
                  <option value="auto">auto</option>
                  <option value="ask">ask</option>
                  <option value="strict">strict</option>
                </select>
              ) : null}
              <button type="button" data-testid="open-model" onClick={() => patch({ picker: "model" })}>
                Model
              </button>
              <button type="button" data-testid="open-voice" onClick={() => patch({ picker: "voice" })}>
                Voice
              </button>
              <button type="button" data-testid="open-avatar" onClick={() => patch({ picker: "avatar" })}>
                Avatar
              </button>
              <button type="button" data-testid="open-settings" onClick={() => patch({ picker: "settings" })}>
                Settings
              </button>
            </div>
          </div>

          {state.tab === "chat" ? (
            <>
              <ChatTab
                thread={openThread}
                messages={state.messages}
                draft={state.draft}
                ops={state.ops}
                onCancelTask={(id) => api.cancelTask(id).then(refreshTasks).catch(() => {})}
              />
              <InputBar
                mode={state.dictation}
                level={state.level}
                hint={hint}
                pendingTranscript={state.pendingTranscript}
                disabled={state.approvals.length > 0}
                placeholder={
                  state.compose
                    ? `New thread in ${project?.name || "…"} · message, or @path to attach`
                    : undefined
                }
                projectChip={{
                  projects: state.projects,
                  value: activeProjectId,
                  editable: !!state.compose && !state.compose.openedId,
                  folder: openThread?.cwd ?? null,
                  onChange: (projectId) =>
                    state.compose && patch({ compose: { ...state.compose, projectId } }),
                  onNewProject: () => patch({ picker: "newProject" }),
                }}
                modelChip={threadModel.chip}
                imageNote={threadModel.imageNote}
                onModeChange={setMode}
                onSend={(text, files) => void send(text, files)}
                onTranscriptTaken={() => patch({ pendingTranscript: "" })}
              />
            </>
          ) : null}
          {state.tab === "task" ? (
            <div className="scroll">
              <TaskView
                task={task}
                threads={state.taskId ? state.taskThreads[state.taskId] || [] : []}
                onAnswer={(question, answer) =>
                  task && api.answerTask(task.id, { question, answer }).then(refreshTasks).catch(() => {})
                }
                onSteer={(text) => task && api.steerTask(task.id, text).catch(() => {})}
                onCancel={() => task && api.cancelTask(task.id).then(refreshTasks).catch(() => {})}
                onResume={() => task && api.resumeTask(task.id).then(refreshTasks).catch(() => {})}
                onStart={() => task && api.startTask(task.id).then(refreshTasks).catch(() => {})}
              />
            </div>
          ) : null}
          {state.tab === "file" ? <FileTab projectId={activeProjectId} /> : null}
          {state.tab === "diff" ? <DiffTab taskId={state.taskId} /> : null}
          {state.tab === "preview" ? <PreviewTab projectId={activeProjectId} /> : null}
        </div>

        {drawn.rightFolded ? null : (
          <Splitter
            side="right"
            width={drawn.right}
            max={maxWidth("right", drawn.left, view.available)}
            zoom={view.zoom}
            onResize={(w) => view.setWidth("right", w)}
            onReset={() => view.resetWidth("right")}
          />
        )}
        <div className="pane" id="right">
          <div className="barrow">
            <h2 className="bar">{task ? "Task" : "Status"}</h2>
            <ZoomControl zoom={view.zoom} onStep={view.zoomBy} onReset={() => view.setZoom(ZOOM_DEFAULT)} />
            <CollapseButton side="right" onCollapse={() => view.fold("right")} />
          </div>
          <div className="scroll">
            <ApprovalQueue requests={state.approvals} />
            <TaskView
              task={task}
              threads={state.taskId ? state.taskThreads[state.taskId] || [] : []}
              onAnswer={(question, answer) =>
                task && api.answerTask(task.id, { question, answer }).then(refreshTasks).catch(() => {})
              }
              onSteer={(text) => task && api.steerTask(task.id, text).catch(() => {})}
              onCancel={() => task && api.cancelTask(task.id).then(refreshTasks).catch(() => {})}
              onResume={() => task && api.resumeTask(task.id).then(refreshTasks).catch(() => {})}
              onStart={() => task && api.startTask(task.id).then(refreshTasks).catch(() => {})}
            />
            <UsagePanel usage={state.usage} />
            <DiscordPanel
              discord={state.discord}
              projects={state.projects}
              onBackfill={() =>
                api.discordBackfill().then(async (result) => {
                  await refreshProjects();
                  return result;
                })
              }
            />
            <SchedulesButton
              count={state.schedules.length}
              onOpen={() => {
                setEditingSchedule(null);
                patch({ picker: "schedule" });
              }}
            />
            <DecisionsLog route={state.route} />
            {state.error ? <div className="block err" data-testid="error">{state.error}</div> : null}
          </div>
        </div>
        {drawn.rightFolded ? (
          <Rail
            side="right"
            auto={drawn.autoRight}
            onExpand={() => view.open("right")}
            approvals={state.approvals.length}
            error={!!state.error}
          />
        ) : null}
      </div>

      <Orb
        state={state.orb}
        level={state.level}
        rings={state.avatar?.rings}
        accent={state.avatar?.accent}
        avatarUrl={state.avatar ? `/avatar.svg?v=${avatarCacheBust}` : null}
        // A folded sidebar leaves a 36px rail: the orb shrinks into its foot
        // rather than sitting on top of the input bar.
        compact={drawn.leftFolded}
        zoom={view.zoom}
        status={state.orb === "idle" ? "" : state.orb}
        onPress={press}
        onRelease={release}
      />

      <ApprovalVeil requests={state.approvals} onDecide={decide} />

      {state.picker === "model" ? (
        <ModelPicker
          view={roster}
          // Each answer is the new describe(): drawn at once, and the chips
          // re-read too (a default thread's label names the default). The
          // `model` broadcast does the same for every other window. A refusal
          // propagates to the picker, which shows it.
          onPick={(id) => api.setModel(id).then(rosterChanged)}
          onEffort={(id, effort) => api.setModelEffort(id, effort).then(rosterChanged)}
          onRemove={(id) => api.removeModel(id).then((r) => {
            rosterChanged(r);
            return r;
          })}
          onReset={() => api.setModel("").then(rosterChanged)}
          onPinMore={threadModel.openRosterCatalog}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {/* After the Model picker, so its "Pin a model…" opens on top of it. */}
      {threadModel.picker}
      {state.picker === "voice" ? (
        <VoicePicker
          voices={voices}
          override={voiceOverride}
          onPick={(name) => api.setVoice(name).then(loadVoices).catch(() => {})}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "avatar" ? (
        <AvatarPicker
          avatars={avatars}
          active={state.avatar?.slug || ""}
          onPick={(slug) => api.setAvatar(slug).then(loadAvatar).catch(() => {})}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "settings" || state.picker === "route" ? (
        <SettingsDialog route={state.route} onClose={() => patch({ picker: null })} />
      ) : null}
      {state.picker === "newProject" ? (
        <NewProject
          discordConfigured={guildConfigured(state.discord)}
          onCreate={(b, options) =>
            api
              .createProject(b)
              .then(async (created) => {
                // Every project gets a channel (decisions O1). The project
                // exists either way; a refused channel is said, not swallowed.
                let channelNote = "";
                if (options.channel) {
                  await api.projectDiscordAction(created.id, { action: "create" }).catch((e) => {
                    channelNote = `Project ${created.name} was created, but its Discord channel was not: `
                      + `${e?.message || "the request failed"}. Create it from the project dialog.`;
                  });
                }
                const projects = await api.projects();
                const platform = await api
                  .platform(created.id)
                  .catch(() => ({ platform: "wsl", note: null }));
                patch({
                  projects,
                  platforms: { ...live.current.platforms, [created.id]: platform },
                  picker: null,
                });
                // A new project starts as a new thread in it.
                newThread(created.id);
                if (channelNote) patch({ error: channelNote });
              })
          }
          taken={projectNamesTaken(state.projects, null, state.archivedNames)}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "editProject" && projectTarget ? (
        <ProjectDialog
          mode="edit"
          initial={state.projects.find((p) => p.id === projectTarget) || null}
          taken={projectNamesTaken(state.projects, projectTarget, state.archivedNames)}
          onDiscordChanged={() => void refreshProjects(projectTarget)}
          onSave={(body) =>
            api.patchProject(projectTarget, body).then(async () => {
              await refreshProjects(projectTarget);
              patch({ picker: null });
            })
          }
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "archiveProject" && projectTarget && state.projects.some((p) => p.id === projectTarget) ? (
        <ArchiveConfirm
          project={state.projects.find((p) => p.id === projectTarget)!}
          version={lifecycle}
          onCancelTask={(id) => api.cancelTask(id).then(refreshTasks)}
          onArchived={(id) => {
            patch({ picker: null });
            projectGone(id, "archived");
          }}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "archive" ? (
        <ArchiveView
          version={lifecycle}
          onRestored={() => {
            void refreshProjects();
            void refreshThreads();
            void refreshTasks();
            void refreshSchedules();
          }}
          onDeleted={() => void refreshProjects()}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "schedule" ? (
        <ScheduleDialog
          projects={state.projects}
          schedules={state.schedules}
          editing={editingSchedule}
          defaultProject={activeProjectId || ""}
          onToggle={(id, enabled) =>
            api.patchSchedule(id, { enabled }).then(refreshSchedules).catch(() => {})
          }
          onRunNow={(id) => api.runSchedule(id).catch(() => {})}
          onDelete={(id) => api.deleteSchedule(id).then(refreshSchedules).catch(() => {})}
          onSave={(body, id) =>
            (id ? api.patchSchedule(id, body) : api.createSchedule(body)).then(async () => {
              await refreshSchedules();
              setEditingSchedule(null);
              patch({ picker: null });
            })
          }
          onClose={() => {
            setEditingSchedule(null);
            patch({ picker: null });
          }}
        />
      ) : null}
      {state.picker === "newTask" ? (
        <NewTask
          onCreate={(brief) => {
            const projectId = newTaskProject || activeProjectId;
            if (!projectId) return;
            api
              .createTask({ project_id: projectId, brief })
              .then((t) => {
                patch({ picker: null, taskId: t.id, taskFocus: true, tab: "task" });
                return refreshTasks();
              })
              .catch((e) => patch({ error: e.message }));
          }}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
    </>
  );
}
