// The window. One SSE subscription writes lifecycle state; every panel reads
// the store. The orb, the capture pipeline and the wake word live here because
// they are properties of the *window*, not of whichever tab is showing.
//
// Several chats at once (WP-B, 2026-10-09; plan §2.2): every chat pane holds
// its own conversation, input box, project and model chips, Send/Steer and
// Stop (`state.chats[pane]`, lib/chats.ts). The turn machinery — send, steer,
// Stop, give-back, held-back words, the 15-second busy reconcile — is the one
// that drove the single conversation, keyed by pane: in the single layout
// pane 1 is that conversation, unchanged. An SSE event with a `thread_id`
// reaches the pane showing that thread or tracking its turn; approvals,
// activity and lifecycle events stay window-wide. Voice goes to the **voice
// target**, the chat pane used last: the orb follows and interrupts its turn,
// the follow-up mic window opens only when its turn ends, and only its turn
// keeps the mic suppressed.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, subscribe } from "./api";
import {
  useStore, activeChat, compatActions, currentProjectId, currentTask, flatState,
  type ChatPatch,
} from "./state/store";
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
import { HeldBack, nextNonce } from "./lib/giveback";
import { useThreadModel } from "./components/ThreadModelControls";
import { ProjectDialog } from "./components/Pickers";
import { ArchiveConfirm, ArchiveView } from "./components/Archive";
import { afterProjectGone, afterThreadGone, forgetLastProject, projectNamesTaken } from "./lib/projects";
import { guildConfigured, ownerLine } from "./lib/discord";
import { CollapseButton, Rail, Splitter, TitleBar, useLayout } from "./components/Layout";
import { Workspace, type PaneInfo } from "./components/Workspace";
import { maxWidth } from "./lib/layout";
import { PANE_NOS, SHAPES, show as showOf, type PaneNo, type PaneSpec, type View } from "./lib/workspace";
import { ActivitySync, clearsOnRead } from "./lib/activity";
import {
  besideTarget, chatProjectId, chatTarget, composeRows, drawnShowing, eitherPane, giveBackPane, heldElsewhere,
  isEmptyChat, openRows, routeEvent, runningHere, voiceTargetOf,
} from "./lib/chats";

const PROPOSAL_WINDOW_MS = 60_000;
/** How many of this window's steered or queued messages it remembers, to hand
 * their words back if a Stop drops them. */
const WAITING_KEPT = 20;
/** Events after which an open confirmation or the Archive reads again. */
const LIFECYCLE_KINDS = new Set([
  "task_created", "task_status_changed", "project_created", "project_updated", "project_archived",
  "project_restored", "project_deleted", "thread_archived", "thread_restored", "thread_deleted",
  "thread_updated", "turn_started", "turn_finished",
]);
/** How long a pane says why it refused to switch away from an unsaved edit. */
const REFUSED_MS = 6000;

/** One value per pane, all null: the window's per-pane refs start here. */
const perPane = (): Record<PaneNo, string | null> => ({ 1: null, 2: null, 3: null, 4: null });

export default function App() {
  const { state, dispatch } = useStore();
  // Zoom, pane widths and folded panes (§18, 2026-10-08): lib/layout.ts; the
  // centre's panes, their views and the bottom panel (2026-10-09):
  // lib/workspace.ts. Every layout key is inert while an authorization card
  // is up (as PTT is), and every layout button is disabled.
  const blocked = state.approvals.length > 0;
  const view = useLayout(blocked);
  // File panes holding an unsaved edit (FileTab's `onDirty`): such a buffer
  // is never closed behind the owner's back — "follow chat" waits, a project
  // that goes away keeps the pane pinned until the owner discards the edit,
  // closing the window asks (2026-10-09, the minimal guard), and since WP-B
  // nothing switches the pane to another view — a tab, a sidebar click, a
  // chat landing there — until the edit is saved or discarded.
  const [dirtyPanes, setDirtyPanes] = useState<Record<number, boolean>>({});
  const dirtyNow = useRef(dirtyPanes);
  dirtyNow.current = dirtyPanes;
  const markDirty = useCallback(
    (pane: PaneNo, dirty: boolean) => setDirtyPanes((m) => (!!m[pane] === dirty ? m : { ...m, [pane]: dirty })),
    [],
  );
  const anyDirty = Object.values(dirtyPanes).some(Boolean);
  useEffect(() => {
    if (!anyDirty) return;
    const ask = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", ask);
    return () => window.removeEventListener("beforeunload", ask);
  }, [anyDirty]);
  // A pane that refused to switch away from an unsaved edit says why, briefly.
  const [refused, setRefused] = useState<Partial<Record<PaneNo, number>>>({});
  useEffect(() => {
    const live = Object.values(refused).filter(Boolean) as number[];
    if (!live.length) return;
    const t = setTimeout(() => setRefused({}), Math.max(0, Math.min(...live) + REFUSED_MS - Date.now()));
    return () => clearTimeout(t);
  }, [refused]);
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
  // Set once the first load has landed: a chat pane that has never had a
  // conversation gets a compose row from then on (below).
  const [booted, setBooted] = useState(false);

  // `live` gives the capture callbacks — which run on the audio thread's
  // cadence, not React's — the current state without stale closures.
  const live = useRef(state);
  live.current = state;
  // The layout as last drawn, for callbacks that place a chat.
  const layoutNow = useRef(view);
  layoutNow.current = view;
  // Per pane: a thread opened for a compose send, before it becomes the
  // pane's `threadId` — its events are that pane's from the moment it exists.
  const pendingThread = useRef(perPane());
  // Per pane: the thread whose transcript must not be reloaded when it becomes
  // the pane's open one, because the pane already holds it — the first
  // message, drawn optimistically, and a reply that may already be streaming;
  // or a conversation that moved here from another pane.
  const skipReload = useRef(perPane());
  // This window's own messages, named until the daemon names them; and the
  // ones waiting behind a turn (daemon id -> words and files), so a Stop that
  // drops them can put them back in the box (2026-10-08).
  const localSeq = useRef(0);
  const waitingHere = useRef(new Map<string, { text: string; files: Attachment[] }>());
  // Words handed back for a thread that is not on screen wait here until the
  // owner opens it: never in another thread's box (Bugbot on PR #22).
  const heldBack = useRef(new HeldBack());

  const patch = useCallback((p: Parameters<typeof dispatch>[0] extends any ? any : never) => {
    dispatch({ type: "patch", patch: p });
  }, [dispatch]);
  /** One pane's conversation fields; an `orb` in it lands only for the voice target. */
  const chatPatch = useCallback((pane: PaneNo, p: ChatPatch) => {
    dispatch({ type: "chat", pane, patch: p });
  }, [dispatch]);

  /**
   * Words and files back to a box: the box of the pane showing that thread
   * (`from`, where they were typed, first), else held until the owner opens
   * the thread — never another thread's box. With no thread yet (a compose
   * send that failed before it opened one), back where they were typed.
   */
  const giveBack = useCallback(
    (threadId: string | null, text: string, files: Attachment[], from: PaneNo | null = null) => {
      const at = live.current;
      const pane = giveBackPane(threadId, at.chats, pendingThread.current, at.voiceTarget, from);
      if (pane === null) heldBack.current.hold(threadId!, text, files);
      else dispatch({ type: "give_back", pane, text, files, nonce: nextNonce() });
    },
    [dispatch],
  );
  // provider ▾ · model ▾ · effort ▾ in each chat pane's input bar (decisions 2026-10-06, A).
  const threadModel = useThreadModel(state, dispatch, () => void loadModels());
  const reloadThreadModels = useRef(threadModel.reload);
  reloadThreadModels.current = threadModel.reload;
  // The chip's own dialogs (catalogue, provider default) are hook state, not
  // `state.picker`, but they are open pickers all the same: no open mic under
  // them (PR #15 review).
  const chipOverlay = useRef(false);
  chipOverlay.current = threadModel.overlayOpen;
  // The sidebar dots: a snapshot or a `/seen` answer older than a record
  // already heard must not undo it (lib/activity.ts, review 2026-10-09).
  const [activitySync] = useState(() => new ActivitySync());

  // ---- the voice target ----------------------------------------------------

  // The chat pane used last (lib/chats `voiceTargetOf`): the focused pane when
  // it shows chat, else the target so far while it is a drawn chat pane, else
  // the first drawn chat pane. Synced into the store before paint, so the
  // callbacks below (capture, the orb, the SSE routing) read it from `live`.
  const voicePane = voiceTargetOf(view.ws, view.fit.panes, state.voiceTarget);
  useLayoutEffect(() => {
    if (voicePane !== live.current.voiceTarget) dispatch({ type: "voice_target", pane: voicePane });
  }, [voicePane, state.voiceTarget, dispatch]);

  // ---- capture ------------------------------------------------------------

  const capture = useMemo(
    () =>
      new Capture({
        // He does not answer himself, and never talks over a pending
        // authorization or an open picker. Only the voice target's turn
        // suppresses the mic: a long turn in another pane must not silence it.
        suppressed: () =>
          live.current.orb === "speaking" ||
          activeChat(live.current).busy ||
          live.current.approvals.length > 0 ||
          live.current.picker !== null ||
          chipOverlay.current,
        muted: () => isMuted(live.current.dictation),
        onLevel: (level) => dispatch({ type: "patch", patch: { level } }),
        onListening: () =>
          dispatch({
            type: "chat", pane: live.current.voiceTarget,
            patch: { orb: "listening", status: "LISTENING · SPEAK NOW" },
          }),
        onIdle: (note) =>
          dispatch({ type: "chat", pane: live.current.voiceTarget, patch: { orb: "idle", status: note || "" } }),
        onUtterance: (wav) => void onUtterance(wav),
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  const onUtterance = useCallback(
    async (wav: Blob) => {
      // The words go where the owner was talking to when they stopped: the
      // voice target now, whatever it is by the time the transcript lands.
      const pane = live.current.voiceTarget;
      const mode = live.current.dictation;
      const what = outcomeFor(mode);
      // OFF never reaches here (Capture refuses to claim while muted), but the
      // mode is checked again rather than assumed: the fail-closed direction
      // costs a discarded utterance and the other costs an upload.
      if (what === "discard") return;
      chatPatch(pane, { orb: "transcribing", status: "TRANSCRIBING" });
      let text = "";
      try {
        text = await api.stt(wav);
      } catch (e: any) {
        chatPatch(pane, { orb: "error", status: "STT FAILED", error: e.message });
        return;
      }
      if (!text.trim()) {
        chatPatch(pane, { orb: "idle", status: "DIDN'T CATCH THAT" });
        return;
      }
      if (what === "review") {
        // REVIEW never sends on its own: the transcript lands in the box.
        chatPatch(pane, { orb: "idle", status: "", pendingTranscript: text });
        return;
      }
      // Dictated: the Discord mirror labels it "You (HUD, voice)" (PR C).
      await send(pane, text, [], true);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // ---- sending ------------------------------------------------------------

  const send = useCallback(
    async (pane: PaneNo, text: string, attachments: Attachment[], spoken = false) => {
      const at = live.current;
      const chat = at.chats[pane];
      let threadId = chat.threadId;
      const compose = chat.compose;
      const local = `local-${++localSeq.current}`;
      // A turn already running in this thread: the message steers it
      // (2026-10-08) — drawn at once, marked, and the box clears. The pane's
      // turn state is left alone: that turn is still the one running, and
      // Stop and the orb must still reach it.
      const steering = !!threadId && chat.busy && chat.turnThreadId === threadId;
      const prior = { busy: chat.busy, turnThreadId: chat.turnThreadId, orb: at.orb, status: chat.status };
      dispatch({
        type: "message", pane,
        message: { role: "user", text, local, ...(steering ? { mark: "steering" as const } : {}) },
      });
      if (steering) chatPatch(pane, { error: "" });
      else chatPatch(pane, { busy: true, orb: "thinking", status: "SENDING", draft: "", error: "" });
      try {
        let projectId = threadId ? at.threads.find((t) => t.id === threadId)?.project_id ?? null : null;
        if (!threadId) {
          // Composing: the thread is opened in the chip's project on the first
          // message, and only then. Nothing reaches the server before it.
          projectId = compose?.projectId ?? null;
          if (!projectId) throw new Error("Create a project first.");
          threadId = compose?.openedId || null;
          if (!threadId) {
            chatPatch(pane, { status: "OPENING THREAD" });
            // The provider, model and effort chosen while composing ride the
            // first message; a default sends no model (decisions A1, A5).
            const t = await api.openThread({
              project_id: projectId, role: "chat", ...threadBody(composeChoice(compose)),
            });
            threadId = t.id;
            // Remembered, so a retry after a failed send reuses this thread.
            // The whole compose row is kept, provider, model and effort with
            // it, so a failed first send retries on the same choice.
            chatPatch(pane, { compose: { ...compose, projectId, openedId: t.id } });
          }
          pendingThread.current[pane] = threadId;
        }
        if (!steering) chatPatch(pane, { turnThreadId: threadId });
        const result = await api.send(threadId, {
          text, attachments: attachments.length ? attachments : undefined,
          ...(spoken ? { spoken: true } : {}),
        });
        const status = result?.status ?? "started";
        if (status === "dropped") {
          // The owner pressed Stop while this steer was on its way and the
          // turn would not take it: stop means stop, so it is not sent, and
          // its words go back to where they were typed (review of PR #22).
          dispatch({
            type: "mark", local,
            patch: { mark: "not sent", ...(result.message_id ? { message_id: result.message_id } : {}) },
          });
          giveBack(threadId, text, attachments, pane);
          if (!steering) {
            chatPatch(pane, { busy: prior.busy, turnThreadId: prior.turnThreadId, orb: prior.orb, status: prior.status });
          }
          return true;
        }
        if (status === "steered" || status === "queued") {
          dispatch({
            type: "mark", local,
            patch: { mark: status === "queued" ? "queued" : "steering", message_id: result.message_id },
          });
          if (result.message_id) {
            // Queued *or* steered: a steer the turn never delivered (the fast
            // path's final answer came first) is dropped by a Stop too, and
            // its words come back the same way (review of PR #22). Bounded:
            // a delivered steer is never named again, so the oldest go.
            const mine = waitingHere.current;
            mine.set(result.message_id, { text, files: attachments });
            while (mine.size > WAITING_KEPT) mine.delete(mine.keys().next().value as string);
          }
          // A turn this pane had not heard of (one started from Discord) is
          // running here: track it, so Stop and its finish reach this pane.
          chatPatch(pane, { busy: true, turnThreadId: threadId });
        } else if (steering) {
          // The turn ended while this was on its way: it started its own.
          dispatch({ type: "mark", local, patch: { mark: undefined } });
          chatPatch(pane, { busy: true, turnThreadId: threadId, orb: "thinking", status: "THINKING" });
        }
        if (projectId) saveLastProject(projectId);
        if (!chat.threadId) {
          // The compose row becomes the thread. Its transcript is already on
          // screen; reloading it here is what used to wipe the first message.
          skipReload.current[pane] = threadId;
          await refreshThreads();
          chatPatch(pane, { threadId, compose: null });
          pendingThread.current[pane] = null;
        }
        const now = live.current.chats[pane];
        if (now.busy && now.turnThreadId === threadId && now.status === "SENDING") {
          chatPatch(pane, { status: "THINKING" });
        }
        return true;
      } catch (e: any) {
        // Only this message comes off the screen — a reply that settled
        // meanwhile stays — and its words and files go back in the box, so
        // a failed send costs nothing. Said inline, never as a dead end.
        dispatch({ type: "unmessage", local });
        giveBack(threadId, text, attachments, pane);
        const error = `Could not send: ${e.message}`;
        if (steering) {
          // A refused steer leaves the running turn as it was: still tracked,
          // still stoppable.
          chatPatch(pane, { error });
        } else {
          // Back to whatever the pane was tracking before this send (a turn
          // in another thread keeps running and keeps its Stop).
          chatPatch(pane, {
            busy: prior.busy, turnThreadId: prior.turnThreadId,
            orb: prior.busy ? prior.orb : "error", status: prior.busy ? prior.status : "FAILED",
            error,
          });
        }
        return false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  /** Stop the turn running in a pane's thread (the orb does the same for the voice target's). */
  const stopTurn = useCallback((pane: PaneNo) => {
    const chat = live.current.chats[pane];
    const turn = chat.turnThreadId ?? chat.threadId;
    if (!turn) return;
    api.interrupt(turn).catch((e: any) => {
      // Nothing running there any more: the window was behind. Catch up.
      if (e?.status === 409) void reconcileBusy();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * The busy state never wedges (2026-10-08): it follows `turn_finished`, and
   * when that was missed (an SSE reconnect, a dropped frame) the thread
   * record's live `running` says the turn is over. Run on every reconnect
   * and every 15 s while any pane is busy, for every busy pane. An older
   * daemon sends no `running`: then nothing is reset, as before.
   */
  const reconcileBusy = useCallback(async () => {
    const turns = perPane();
    for (const n of PANE_NOS) {
      const chat = live.current.chats[n];
      if (chat.busy && chat.turnThreadId) turns[n] = chat.turnThreadId;
    }
    if (!PANE_NOS.some((n) => turns[n])) return;
    let threads: any[];
    try {
      threads = await api.threads();
    } catch {
      return;
    }
    dispatch({ type: "patch", patch: { threads } });
    for (const n of PANE_NOS) {
      const turn = turns[n];
      if (!turn) continue;
      const record = threads.find((t) => t.id === turn);
      const chat = live.current.chats[n];
      if (record && record.running === false && chat.busy && chat.turnThreadId === turn) {
        chatPatch(n, { busy: false, turnThreadId: null, orb: "idle", status: "" });
      }
    }
  }, [dispatch, chatPatch]);

  const anyBusy = PANE_NOS.some((n) => state.chats[n].busy);
  useEffect(() => {
    if (!anyBusy) return;
    const timer = setInterval(() => void reconcileBusy(), 15_000);
    return () => clearInterval(timer);
  }, [anyBusy, reconcileBusy]);

  // ---- events -------------------------------------------------------------

  useEffect(() => {
    const stop = subscribe((e: any) => {
      const kind = e.kind;
      const data = e.data || e;
      const at = live.current;
      // Per pane (lib/chats `routeEvent`). Mine: the panes showing the thread
      // (or about to). Ours: the panes whose turn this is. They differ when
      // the owner switches a pane's thread mid-turn, and that pane must still
      // hear the turn finish, or it waits on THINKING — and, as the voice
      // target, keeps the mic suppressed — for good. No thread: the voice
      // target's, as it was the one conversation's.
      const tid: string | undefined = e.thread_id;
      const route = routeEvent(tid, at.chats, pendingThread.current, at.voiceTarget);
      const { mine, ours } = route;
      if (LIFECYCLE_KINDS.has(kind)) setLifecycle((n) => n + 1);
      switch (kind) {
        case "user_message":
          // PR C: a message the owner typed in Discord (a chat's thread or the
          // DM) appears in the open chat as theirs, labelled. A HUD message is
          // already on screen from send(), so it is never added twice.
          if (tid && (data.via === "discord" || data.via === "dm")) {
            const mark = data.steer ? "steering" : data.queued ? "queued" : undefined;
            for (const n of mine) {
              dispatch({
                type: "message", pane: n,
                message: {
                  role: "user", text: ownerLine(data), via: data.via,
                  ...(data.message_id ? { message_id: data.message_id } : {}),
                  ...(mark ? { mark } : {}),
                },
              });
            }
          }
          break;
        case "steer_queued":
          // A steer the provider could not take: it waits as its own turn.
          if (data.message_id) dispatch({ type: "mark", message_id: data.message_id, patch: { mark: "queued" } });
          break;
        case "queued_started":
          // A message that waited is now its own turn: no longer "queued".
          if (data.message_id) {
            waitingHere.current.delete(data.message_id);
            dispatch({ type: "mark", message_id: data.message_id, patch: { mark: undefined } });
          }
          break;
        case "queue_cleared": {
          // The owner stopped the turn: what waited behind it was not sent.
          // Words this window sent go back in the box, as Claude Code hands
          // queued messages back on a stop.
          const back: string[] = [];
          const files: Attachment[] = [];
          for (const m of data.messages || []) {
            if (!m?.message_id) continue;
            dispatch({ type: "mark", message_id: m.message_id, patch: { mark: "not sent" } });
            const mineHere = waitingHere.current.get(m.message_id);
            if (mineHere) {
              waitingHere.current.delete(m.message_id);
              if (mineHere.text) back.push(mineHere.text);
              files.push(...mineHere.files);
            }
          }
          // Into the box of the pane showing that thread; else held for it.
          if (back.length || files.length) giveBack(tid ?? null, back.join("\n"), files);
          break;
        }
        case "turn_started":
          for (const n of mine) {
            chatPatch(n, {
              busy: true, orb: "thinking", draft: "", turnThreadId: at.chats[n].turnThreadId ?? tid ?? null,
            });
          }
          break;
        case "text_delta":
          for (const n of mine) {
            chatPatch(n, { status: "RESPONDING" });
            dispatch({ type: "delta", pane: n, text: data.text || "" });
          }
          break;
        case "text":
          for (const n of mine) dispatch({ type: "settle", pane: n, text: data.text || "" });
          break;
        case "tool_started":
          for (const n of mine) {
            chatPatch(n, { status: `RUNNING · ${data.name || "tool"}`, orb: "tool" });
            dispatch({
              type: "op_start", pane: n,
              op: { call_id: data.call_id || String(Date.now()), name: data.name || "tool", started: Date.now() },
            });
          }
          break;
        case "tool_finished":
          for (const n of mine) {
            chatPatch(n, { status: "THINKING", orb: "thinking" });
            dispatch({
              type: "op_done", pane: n,
              call_id: data.call_id || "",
              ok: data.ok !== false,
              summary: data.summary || "",
            });
          }
          break;
        case "turn_finished": {
          // A message waits behind this turn (`next`) and runs as the thread's
          // next turn at once: the pane stays on it — no flash of idle, no
          // follow-up mic window over a turn about to start. Its own
          // `turn_finished` (or the 15 s reconcile) frees the pane.
          if (data.next) break;
          let followUp = false;
          for (const n of ours) {
            chatPatch(n, { busy: false, turnThreadId: null, orb: "idle", status: "" });
            if (n === at.voiceTarget) followUp = true;
          }
          // Only the voice target's own turn opens the follow-up window. A
          // Discord turn, or a turn in another pane, finishing must not start
          // the HUD listening.
          if (followUp) capture.openFollowUp();
          break;
        }
        case "error":
          // Shown, but the turn is not over until `turn_finished` (which the
          // daemon always sends): resetting `busy` here took the Stop and the
          // orb's interrupt away from a turn that was still running.
          for (const n of eitherPane(route)) chatPatch(n, { orb: "error", error: data.message || "error" });
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
          // pane showing the thread that proposed it (or waiting on its turn),
          // never whichever thread happens to be open.
          void refreshTasks();
          if (!tid) break;
          for (const n of eitherPane(route)) {
            dispatch({
              type: "message", pane: n,
              message: {
                role: "system",
                text: data.reply || "",
                proposal: data.task_id
                  ? { task_id: data.task_id, until: Date.now() + PROPOSAL_WINDOW_MS }
                  : undefined,
              },
            });
          }
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
          if (tid) for (const n of mine) dispatch({ type: "message", pane: n, message: { role: "system", text: data.text || "" } });
          break;
        case "discord_status":
          // The event says something changed; the route says what, filtered.
          api.discord().then((discord) => dispatch({ type: "patch", patch: { discord } })).catch(() => {});
          break;
        case "usage_updated":
          api.usage().then((usage) => dispatch({ type: "patch", patch: { usage } })).catch(() => {});
          break;
        case "codex_metadata":
          // A background refresh of Codex's account catalog and quota landed
          // (the daemon never makes a HUD read wait for one): re-read both.
          api.usage().then((usage) => dispatch({ type: "patch", patch: { usage } })).catch(() => {});
          void reloadThreadModels.current();
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
        case "activity":
          // The sidebar dots (lib/activity.ts). Marking read is the effect below.
          activitySync.heard(e);
          dispatch({ type: "activity", record: e });
          break;
        case "_connected":
          void refreshActivity();
          break;
        default:
          break;
      }
    }, () => {
      // Reconnected: a `turn_finished` published in the gap is gone.
      void reconcileBusy();
      void refreshThreads();
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

  const refreshActivity = useCallback(async () => {
    // Records heard while this is in flight are newer than the snapshot may
    // be, and the daemon never sends one twice: they are replayed over it.
    const pending = activitySync.begin();
    try {
      const snapshot = await api.activity();
      dispatch({ type: "patch", patch: { activity: activitySync.land(pending, snapshot) } });
    } catch {
      activitySync.drop(pending); /* keep what we had */
    }
  }, [dispatch, activitySync]);

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

  // ---- placing a view in a pane ----------------------------------------------

  /**
   * A File pane holding an unsaved edit is never switched to another view
   * behind the owner's back — by its own tabs, a sidebar click, or a chat
   * landing in it (WP-B, review of PR #26): the switch is refused and the
   * pane says why. True when `pane` may show `next`.
   */
  const mayShow = useCallback((pane: PaneNo, next: View): boolean => {
    const spec = layoutNow.current.ws.panes[pane - 1];
    if (spec.view !== "file" || next === "file" || !dirtyNow.current[pane]) return true;
    setRefused((r) => ({ ...r, [pane]: Date.now() }));
    return false;
  }, []);

  /** The view strip's buttons and menu: switch a pane, unless that drops an unsaved edit. */
  const setPaneView = useCallback((pane: PaneNo, next: View) => {
    if (mayShow(pane, next)) view.setView(pane, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mayShow]);

  /**
   * A sidebar click for a task (or anything but a chat): the pane already
   * showing that kind of thing, else the focused pane (lib/workspace `show`)
   * — unless that pane holds an unsaved edit.
   */
  const showView = useCallback((next: View): boolean => {
    const lay = layoutNow.current;
    const after = showOf(lay.ws, next, lay.fit.panes);
    const changed = PANE_NOS.find((n) => after.panes[n - 1].view !== lay.ws.panes[n - 1].view);
    if (changed && !mayShow(changed, next)) return false;
    view.show(next);
    return true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mayShow]);

  /** Put a chat on screen in `pane`: focus it, switching its view to chat when it shows something else. */
  const placeChat = useCallback((pane: PaneNo): boolean => {
    const spec = layoutNow.current.ws.panes[pane - 1];
    if (spec.view === "chat") {
      view.focus(pane);
      return true;
    }
    if (!mayShow(pane, "chat")) return false;
    view.setView(pane, "chat");
    return true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mayShow]);

  /** The pane a chat with no thread yet lands in (lib/chats `chatTarget`). */
  const chatPaneNow = (): PaneNo => {
    const lay = layoutNow.current;
    return chatTarget(lay.ws, lay.fit.panes, live.current.voiceTarget).pane;
  };

  /**
   * Open thread `id` in `pane`, keeping it in one pane only: a pane that
   * holds it off screen (hidden, or showing another view) trades
   * conversations with this one, so the thread — its turn, its hand-backs —
   * moves here and nothing the two panes held is dropped.
   */
  const openIn = useCallback((pane: PaneNo, id: string) => {
    const at = live.current;
    const pend = pendingThread.current;
    pend[pane] = null;
    dispatch({ type: "patch", patch: { taskFocus: false } });
    const holder = heldElsewhere(at.chats, pane, id, pend);
    if (holder === null) {
      dispatch({ type: "open_thread", pane, threadId: id });
      return;
    }
    const here = at.chats[pane];
    const there = at.chats[holder];
    [pend[pane], pend[holder]] = [pend[holder], pend[pane]];
    // Each conversation moves with its transcript: no reload for either.
    if (there.threadId === id) skipReload.current[pane] = id;
    if (here.threadId) skipReload.current[holder] = here.threadId;
    dispatch({ type: "chat_swap", a: pane, b: holder });
    // Held through a compose row that opened it: it is now the open thread.
    if (there.threadId !== id) dispatch({ type: "open_thread", pane, threadId: id });
  }, [dispatch]);

  /**
   * A thread picked in the sidebar (plan §2.2, "Where a sidebar click goes").
   * Already on screen in a chat pane: that pane is focused — a thread is open
   * in one pane at most. Otherwise the focused chat pane, else the voice
   * target, else the focused pane switches to chat. `beside` (Alt+click, or
   * "Open beside"): the next pane to the right, from one pane two columns.
   */
  const pickThread = useCallback((id: string, beside = false) => {
    const at = live.current;
    const lay = layoutNow.current;
    const showing = drawnShowing(lay.ws, lay.fit.panes, at.chats, id, pendingThread.current);
    if (showing !== null) {
      view.focus(showing);
      // On screen through a compose row whose first send failed: it is the thread now.
      if (at.chats[showing].threadId !== id) openIn(showing, id);
      else dispatch({ type: "patch", patch: { taskFocus: false } });
      return;
    }
    if (beside) {
      const to = besideTarget(lay.ws, lay.fit.panes);
      if (!mayShow(to.pane, "chat")) return;
      if (to.preset !== lay.ws.preset) view.setPreset(to.preset);
      view.setView(to.pane, "chat");
      openIn(to.pane, id);
      return;
    }
    const pane = chatPaneNow();
    if (!placeChat(pane)) return;
    openIn(pane, id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch, mayShow, placeChat, openIn]);

  /**
   * A project went away — archived or deleted, here or in another window. Its
   * rows go, a conversation inside it closes into a new thread in the last
   * project worked in — in every pane that had one — the remembered project
   * forgets it, and a dialog open on it closes and says why
   * (lib/projects.ts decides all of it, per pane).
   */
  const projectGone = useCallback(
    (id: string, how: "archived" | "deleted") => {
      if (!id) return;
      const at = live.current;
      const name = at.projects.find((p) => p.id === id)?.name || "the project";
      forgetLastProject(id);
      const stored = loadLastProject();
      const where = (n: PaneNo) => ({
        projects: at.projects, threads: at.threads, tasks: at.tasks, taskId: at.taskId,
        threadId: at.chats[n].threadId, compose: at.chats[n].compose,
      });
      const next = PANE_NOS.map((n) => afterProjectGone(where(n), id, stored));
      const first = next[0];
      const taskGone = first.taskId !== at.taskId;
      const platforms = { ...at.platforms };
      delete platforms[id];
      const patchOut: Record<string, unknown> = {
        projects: first.projects, threads: first.threads, tasks: first.tasks, taskId: first.taskId, platforms,
      };
      if (taskGone) patchOut.taskFocus = false;
      if ((at.picker === "editProject" || at.picker === "archiveProject") && projectTargetRef.current === id) {
        patchOut.picker = null;
        if (at.picker === "editProject") patchOut.error = `${name} was ${how} while you were editing it.`;
      }
      dispatch({ type: "patch", patch: patchOut as any });
      const status = `${name} was ${how}`.toUpperCase();
      let displaced = false;
      for (const n of PANE_NOS) {
        const was = at.chats[n];
        const after = next[n - 1];
        // This pane's own conversation was in the project (a task there going
        // away is not a reason to clear a chat in another project).
        const gone = after.threadId !== was.threadId || after.compose !== was.compose;
        if (!gone) continue;
        displaced = true;
        if (pendingThread.current[n] && after.threadId === null) pendingThread.current[n] = null;
        chatPatch(n, { threadId: after.threadId, compose: after.compose, messages: [], draft: "", ops: [], status });
      }
      // The task view it showed is empty now: the status line says why.
      if (taskGone && !displaced) chatPatch(at.voiceTarget, { status });
      // A File or Preview pane pinned to it follows the chat again — except
      // one holding an unsaved edit, which waits for the owner to discard it.
      view.unpinProject(id, PANE_NOS.filter((n) => dirtyNow.current[n]));
      if (displaced || taskGone) placeChat(chatPaneNow());
      void refreshThreads();
      void refreshTasks();
      void refreshSchedules();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  /** One chat thread went away (archived or deleted): every pane that had it opens a new thread. */
  const threadGone = useCallback(
    (id: string) => {
      if (!id) return;
      const at = live.current;
      const where = (n: PaneNo) => ({
        projects: at.projects, threads: at.threads, tasks: at.tasks, taskId: at.taskId,
        threadId: at.chats[n].threadId, compose: at.chats[n].compose,
      });
      const next = PANE_NOS.map((n) => afterThreadGone(where(n), id));
      dispatch({ type: "patch", patch: { threads: next[0].threads } });
      for (const n of PANE_NOS) {
        const after = next[n - 1];
        if (!after.displaced) continue;
        pendingThread.current[n] = null;
        chatPatch(n, { threadId: after.threadId, compose: after.compose, messages: [], draft: "", ops: [] });
      }
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

  /** Start a new thread: a compose row in the last project worked in, in the chat pane a click lands in. */
  const newThread = useCallback(
    (projectId?: string | null) => {
      const at = live.current;
      const pane = chatPaneNow();
      if (!placeChat(pane)) return;
      pendingThread.current[pane] = null;
      const chat = at.chats[pane];
      dispatch({ type: "patch", patch: { taskFocus: false, error: "" } });
      chatPatch(pane, {
        threadId: null,
        compose: { projectId: projectId ?? lastProject(at.projects, at.threads, loadLastProject()) },
        messages: [],
        draft: "",
        ops: [],
        status: chat.turnThreadId ? chat.status : "",
      });
    },
    // `placeChat` is stable (a callback over refs).
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      dispatch({ type: "patch", patch: { projects, threads, tasks, platforms } });
      // The window opens on a new thread in the last project worked in, as
      // Claude Code opens on a new session — in every pane that shows chat,
      // and in the pane the conversation is put on screen in. Nothing is sent
      // until the owner speaks.
      const lay = layoutNow.current;
      const pane = chatTarget(lay.ws, lay.fit.panes, live.current.voiceTarget).pane;
      const projectId = lastProject(projects, threads, loadLastProject());
      for (const n of PANE_NOS) {
        if (n === pane || lay.ws.panes[n - 1].view === "chat") {
          dispatch({ type: "chat", pane: n, patch: { threadId: null, compose: { projectId } } });
        }
      }
      setBooted(true);
      // The conversation is on screen: in a drawn pane that already shows
      // chat, else in the focused pane — in the single layout, today's chat tab.
      placeChat(pane);
      void refreshActivity();
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

  // A pane that shows chat for the first time — a second chat pane, chosen
  // from its tabs or a preset — opens on a new thread in the last project
  // worked in, as the window does.
  const emptyChatPanes = PANE_NOS.filter(
    (n) => view.ws.panes[n - 1].view === "chat" && isEmptyChat(state.chats[n]),
  ).join(",");
  useEffect(() => {
    if (!booted || !emptyChatPanes) return;
    const at = live.current;
    const projectId = lastProject(at.projects, at.threads, loadLastProject());
    for (const n of PANE_NOS) {
      if (layoutNow.current.ws.panes[n - 1].view === "chat" && isEmptyChat(at.chats[n])) {
        chatPatch(n, { compose: { projectId } });
      }
    }
  }, [booted, emptyChatPanes, chatPatch]);

  // A thread's transcript is loaded when it becomes a pane's open one, so
  // switching redraws from the saved conversation rather than from whatever
  // is on screen.
  const loadedFor = useRef(perPane());
  const openThreads = PANE_NOS.map((n) => state.chats[n].threadId ?? "").join("|");
  useEffect(() => {
    for (const n of PANE_NOS) {
      const id = live.current.chats[n].threadId;
      if (id === loadedFor.current[n]) continue;
      loadedFor.current[n] = id;
      if (!id) continue;
      if (skipReload.current[n] === id) {
        skipReload.current[n] = null;
        continue;
      }
      // A turn running in another thread keeps the pane busy, but its status
      // line is about that thread, not this one.
      const chat = live.current.chats[n];
      const status = chat.turnThreadId === id ? chat.status : "";
      api
        .transcript(id)
        .then((r) => chatPatch(n, { messages: r.messages || [], draft: "", ops: [], status }))
        .catch(() => chatPatch(n, { messages: [], draft: "", ops: [], status }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openThreads]);

  // Words handed back while a thread was not on screen come back when a pane opens it.
  const heldFor = useRef(perPane());
  useEffect(() => {
    for (const n of PANE_NOS) {
      const id = live.current.chats[n].threadId;
      if (id === heldFor.current[n]) continue;
      heldFor.current[n] = id;
      if (!id) continue;
      const held = heldBack.current.take(id);
      if (held) dispatch({ type: "give_back", pane: n, text: held.text, files: held.files, nonce: nextNonce() });
    }
  }, [openThreads, dispatch]);

  // Reading clears blue and red (2026-10-08): a thread shown in a chat pane,
  // or a task shown in a task pane, while the window is visible — on opening
  // it, and when it finishes with the owner watching. Since 2026-10-09 that
  // is **any drawn pane**, not only the focused one: a chat watched finishing
  // in a side pane must not turn blue — and with several chats, every drawn
  // chat pane's thread. The answer carries the status now, and is drawn at
  // once — the daemon's `activity` record may not reach a window whose stream
  // is reconnecting — unless something newer about that row has arrived
  // meanwhile (ActivitySync).
  const drawnViews = view.fit.panes.map((n) => view.ws.panes[n - 1].view);
  const taskShown = drawnViews.includes("task");
  const readThreads = view.fit.panes
    .filter((n) => view.ws.panes[n - 1].view === "chat")
    .map((n) => state.chats[n].threadId)
    .filter((t): t is string => !!t);
  const readKey = readThreads.join(",");
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  const marking = useRef(new Set<string>());
  useEffect(() => {
    if (!visible) return;
    const mark = (of: "thread" | "task", id: string, call: (id: string) => Promise<{ status: string }>) => {
      const key = `${of}:${id}`;
      if (marking.current.has(key)) return;
      marking.current.add(key);
      const ticket = activitySync.ask(of, id);
      call(id)
        .then((answer) => {
          const record = activitySync.answered(ticket, answer?.status);
          if (record) dispatch({ type: "activity", record });
        })
        .catch(() => {})
        .finally(() => marking.current.delete(key));
    };
    for (const t of readThreads) {
      if (clearsOnRead(state.activity.threads[t])) mark("thread", t, api.seenThread);
    }
    const k = state.taskId;
    if (k && taskShown && clearsOnRead(state.activity.tasks[k])) {
      mark("task", k, api.seenTask);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, readKey, state.taskId, taskShown, state.activity, activitySync, dispatch]);

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
    // orb as the way to shut him up. It interrupts the voice target's turn —
    // a turn running in another pane keeps running, and keeps its Stop.
    const pane = live.current.voiceTarget;
    const chat = live.current.chats[pane];
    if (chat.busy && chat.turnThreadId) api.interrupt(chat.turnThreadId).catch(() => {});
    if (isMuted(live.current.dictation)) {
      chatPatch(pane, { status: "MIC MUTED" });
      return;
    }
    capture.press();
  }, [capture, chatPatch]);

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
      // The window's state, the voice target's conversation laid over the
      // top as the one conversation's fields were (the single-layout suites
      // read those); `chats` has every pane's, `voiceTarget` says which.
      state: () => flatState(live.current),
      // A legacy `patch` of conversation fields lands in the voice target's pane.
      dispatch: (a: any) => {
        for (const x of compatActions(a, live.current.voiceTarget)) dispatch(x);
      },
      // What a reconnect or the 15 s timer runs: every busy pane checked
      // against its thread record's live `running`.
      reconcile: () => reconcileBusy(),
      // The centre's panes as stored and as drawn (lib/workspace.ts).
      workspace: () => ({ ws: layoutNow.current.ws, fit: layoutNow.current.fit }),
    };
  }, [capture, dispatch, onWakeHit, reconcileBusy]);

  // ---- actions ------------------------------------------------------------

  const setMode = (mode: DictationMode) => {
    const wasMuted = isMuted(live.current.dictation);
    saveMode(mode);
    dispatch({ type: "patch", patch: { dictation: mode } });
    chatPatch(live.current.voiceTarget, { status: "" });
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

  const activeProjectId = currentProjectId(state);
  const task = currentTask(state);
  const drawn = view.fitted;
  const fit = view.fit;
  const ws = view.ws;
  // The side panes leave the centre what the drawn shape needs (one pane: 480).
  const mainMin = SHAPES[fit.drawn].minW;
  const projectName = (id: string | null) => state.projects.find((p) => p.id === id)?.name || "…";
  const threadOf = (id: string | null) => (id ? state.threads.find((t) => t.id === id) || null : null);
  const drawnChats = fit.panes.filter((n) => ws.panes[n - 1].view === "chat");
  /** The pane that carries the profile select when no drawn pane shows a chat: the focused one. */
  const focusedDrawn: PaneNo = fit.panes.includes(ws.focused) ? ws.focused : fit.panes[0];
  /**
   * The profile belongs to a conversation's project: every drawn chat pane's
   * header carries one for its own conversation, and with no chat drawn the
   * focused pane's carries the project the window is about — in the single
   * layout, pane 1's, where it always was.
   */
  const profileProject = (spec: PaneSpec, n: PaneNo) => {
    if (!fit.panes.includes(n)) return null;
    const id = spec.view === "chat"
      ? chatProjectId(state.chats[n], state.threads)
      : !drawnChats.length && n === focusedDrawn ? activeProjectId : null;
    return id ? state.projects.find((p) => p.id === id) || null : null;
  };
  /** The pane's pinned project went away (archived, deleted). */
  const pinGone = (spec: PaneSpec) => !!spec.projectId && !state.projects.some((p) => p.id === spec.projectId);
  /**
   * A File or Preview pane's project: its pin while that project exists, else
   * the chat's. A File pane whose pinned project went away while it held an
   * unsaved edit keeps it — FileTab is keyed by this, so following the chat
   * would remount it and drop the edit; the header asks first instead.
   */
  const paneProject = (spec: PaneSpec, pane: PaneNo): string | null =>
    spec.projectId && (!pinGone(spec) || (spec.view === "file" && dirtyPanes[pane]))
      ? spec.projectId : activeProjectId;
  const pinnedElsewhere = (spec: PaneSpec, pane: PaneNo) =>
    (spec.view === "file" || spec.view === "preview") && paneProject(spec, pane) !== activeProjectId;

  /** One chat pane: its conversation, its box, its chips, its Send/Steer and Stop. */
  const renderChat = (n: PaneNo) => {
    const chat = state.chats[n];
    const thread = threadOf(chat.threadId);
    const target = n === voicePane;
    // The voice target's hint is the dictation strip's; another pane's says
    // only what its own turn is doing.
    const hint = target
      ? chat.status || (state.approvals.length ? "ANSWER THE AUTHORIZATION" : HINTS[state.dictation])
      : chat.status;
    const compose = chat.compose;
    return (
      <>
        <ChatTab
          thread={thread}
          messages={chat.messages}
          draft={chat.draft}
          ops={chat.ops}
          onCancelTask={(id) => api.cancelTask(id).then(refreshTasks).catch(() => {})}
        />
        <InputBar
          mode={state.dictation}
          level={target ? state.level : 0}
          hint={hint}
          strip={target}
          pendingTranscript={chat.pendingTranscript}
          disabled={state.approvals.length > 0}
          placeholder={
            compose
              ? `New thread in ${projectName(compose.projectId)} · message, or @path to attach`
              : undefined
          }
          projectChip={{
            projects: state.projects,
            value: chatProjectId(chat, state.threads),
            editable: !!compose && !compose.openedId,
            folder: thread?.cwd ?? null,
            onChange: (projectId) => {
              const c = live.current.chats[n].compose;
              if (c) chatPatch(n, { compose: { ...c, projectId } });
            },
            onNewProject: () => patch({ picker: "newProject" }),
          }}
          modelChip={threadModel.chipFor(n)}
          imageNote={threadModel.imageNoteFor(n)}
          onModeChange={setMode}
          onSend={(text, files) => void send(n, text, files)}
          onTranscriptTaken={() => chatPatch(n, { pendingTranscript: "" })}
          running={runningHere(chat)}
          onStop={() => stopTurn(n)}
          restore={chat.restore}
          onRestoreTaken={(nonce) => dispatch({ type: "given_back", pane: n, nonce })}
        />
      </>
    );
  };

  const renderView = (spec: PaneSpec, info: PaneInfo) => {
    const n = info.pane;
    switch (spec.view) {
      case "chat":
        return renderChat(n);
      case "task":
        return (
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
        );
      case "file": {
        // Keyed by its project: a save can only ever go where the file was read.
        const pid = paneProject(spec, n);
        return (
          <FileTab
            key={pid ?? "none"}
            projectId={pid}
            narrow={info.narrow}
            onOpened={(id) => view.pin(n, id)}
            onDirty={(dirty) => markDirty(n, dirty)}
          />
        );
      }
      case "diff":
        return <DiffTab taskId={state.taskId} narrow={info.narrow} />;
      case "preview":
        return (
          <PreviewTab
            projectId={paneProject(spec, n)}
            url={spec.previewUrl}
            onLoaded={(url) => view.setPreviewUrl(n, url)}
            onProjectRoot={(id) => view.pin(n, id)}
          />
        );
      default:
        return null;
    }
  };

  const paneExtras = (spec: PaneSpec, info: PaneInfo) => {
    const n = info.pane;
    const dirty = spec.view === "file" && !!dirtyPanes[n];
    const profileOf = profileProject(spec, n);
    return (
      <>
        {refused[n] && dirty ? (
          <span className="chip panepin warn" data-testid={`pane-${n}-refused`} role="status"
                title="Switching this pane away would close the file and drop the edit">
            unsaved edit · save or reload first
          </span>
        ) : null}
        {dirty && pinGone(spec) ? (
          // The one remount nothing can avoid: ask before it drops the edit.
          <span className="chip panepin warn" data-testid={`pane-${n}-gone`}
                title="The project this file was read from is gone; the unsaved edit is kept here until you discard it">
            project gone · unsaved edit kept
            <button
              type="button"
              className="quiet"
              data-testid={`pane-${n}-discard`}
              onClick={() => {
                markDirty(n, false);
                view.pin(n, null);
              }}
            >
              discard edit
            </button>
          </span>
        ) : pinnedElsewhere(spec, n) ? (
          <span className="chip panepin" data-testid={`pane-${n}-project`} title="This pane stays in its project">
            in: {projectName(paneProject(spec, n))}
            <button
              type="button"
              className="quiet"
              data-testid={`pane-${n}-follow`}
              disabled={dirty}
              title={dirty ? "Save or reload the file first: following the chat closes it" : "Follow the chat's project"}
              onClick={() => view.pin(n, null)}
            >
              follow chat
            </button>
          </span>
        ) : null}
        {profileOf ? (
          <select
            data-testid="profile"
            style={{ width: 110 }}
            value={profileOf.profile}
            title="Applies to threads and task workers opened from now on."
            onChange={(e) =>
              api
                .patchProject(profileOf.id, { profile: e.target.value })
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
      </>
    );
  };

  const paneContext = (spec: PaneSpec, info: PaneInfo): string => {
    switch (spec.view) {
      case "chat": {
        const chat = state.chats[info.pane];
        return threadOf(chat.threadId)?.title
          || (chat.compose ? `New thread in ${projectName(chat.compose.projectId)}` : "");
      }
      case "task":
      case "diff":
        return task?.brief || "";
      case "file":
        return `in: ${projectName(paneProject(spec, info.pane))}`;
      case "preview":
        return spec.previewUrl || "";
      default:
        return "";
    }
  };

  // The sidebar's view of the drawn chat panes: what each shows, which is active.
  const sidebarOpen = openRows(ws, fit.panes, state.chats, voicePane);
  const sidebarComposing = composeRows(ws, fit.panes, state.chats, voicePane);

  return (
    <>
      <TitleBar view={view} blocked={blocked} onPicker={(which) => patch({ picker: which })} />
      <div
        id="shell"
        className={(drawn.leftFolded ? "left-collapsed " : "") + (drawn.rightFolded ? "right-collapsed" : "")}
        style={{ ["--left-w" as any]: `${drawn.left}px`, ["--right-w" as any]: `${drawn.right}px` }}
      >
        {drawn.leftFolded ? (
          <Rail
            side="left"
            auto={drawn.autoLeft}
            disabled={blocked}
            onExpand={() => view.open("left")}
            onNewThread={() => newThread()}
          />
        ) : null}
        <Sidebar
          zoom={view.zoom}
          layoutBlocked={blocked}
          onCollapse={() => view.fold("left")}
          projects={state.projects}
          archivedNames={state.archivedNames}
          platforms={state.platforms}
          threads={state.threads}
          tasks={state.tasks}
          taskThreads={state.taskThreads}
          activity={state.activity}
          activeProjectId={activeProjectId}
          open={sidebarOpen}
          composing={sidebarComposing}
          taskId={state.taskId}
          onPickThread={pickThread}
          onPickTask={(id) => {
            patch({ taskId: id, taskFocus: true });
            showView("task");
          }}
          onNewProject={() => patch({ picker: "newProject" })}
          onNewThread={() => newThread()}
          onNewTask={(projectId) => {
            setNewTaskProject(projectId);
            patch({ picker: "newTask" });
          }}
          moveError={state.moveError}
          onMoveThread={moveThread}
          onMoveCompose={(projectId, pane) => {
            const c = live.current.chats[pane].compose;
            if (c) chatPatch(pane, { compose: { ...c, projectId } });
          }}
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
            max={maxWidth("left", drawn.right, view.available, mainMin)}
            zoom={view.zoom}
            blocked={blocked}
            onResize={(w) => view.setWidth("left", w)}
            onReset={() => view.resetWidth("left")}
          />
        )}

        <div className="pane" id="main">
          <Workspace
            view={view}
            blocked={blocked}
            render={renderView}
            extras={paneExtras}
            context={paneContext}
            voicePane={voicePane}
            onView={setPaneView}
          />
        </div>
        {drawn.rightFolded ? null : (
          <Splitter
            side="right"
            width={drawn.right}
            max={maxWidth("right", drawn.left, view.available, mainMin)}
            zoom={view.zoom}
            blocked={blocked}
            onResize={(w) => view.setWidth("right", w)}
            onReset={() => view.resetWidth("right")}
          />
        )}
        <div className="pane" id="right">
          <div className="barrow">
            <h2 className="bar">{task ? "Task" : "Status"}</h2>
            {/* The zoom control moved to the title bar (2026-10-09): here it
                folded away with the pane it sat in. */}
            <CollapseButton side="right" onCollapse={() => view.fold("right")} disabled={blocked} />
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
            disabled={blocked}
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
                patch({ picker: null, taskId: t.id, taskFocus: true });
                showView("task");
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
