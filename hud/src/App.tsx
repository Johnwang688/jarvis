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
// activity and lifecycle events stay window-wide. **Ambiguous input goes to
// the selected chat** (decisions W-6): the chat pane most recently clicked,
// always one on screen, none with no chat drawn. Voice lands there when its
// transcript does, the orb follows and interrupts its turn, the follow-up mic
// window opens only when its turn ends, only its turn keeps the mic
// suppressed, and files dropped outside a chat pane are staged in its box.
// Input that belongs to a thread — typed words, a steer, a hand-back, a first
// send, a transcript — stays with that thread.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, subscribe } from "./api";
import {
  useStore, activeChat, activePane, compatActions, currentProjectId, currentTask, flatState,
  type ChatPatch,
} from "./state/store";
import { Sidebar } from "./components/Sidebar";
import { ChatTab } from "./components/ChatTab";
import { InputBar, MAX_FILES, toAttachment } from "./components/InputBar";
import { TaskView } from "./components/TaskView";
import { FileTab } from "./components/FileTab";
import { DiffTab } from "./components/DiffTab";
import { PreviewTab } from "./components/PreviewTab";
import { ApprovalQueue, ApprovalVeil } from "./components/Approvals";
import { CrashProbe, DialogBoundary, WorkspaceBoundary } from "./components/Boundary";
import { DecisionsLog, DiscordPanel, SchedulesButton, UsagePanel } from "./components/Panels";
import { AvatarPicker, ModelPicker, NewProject, NewTask, SettingsDialog, VoicePicker } from "./components/Pickers";
import { ScheduleDialog } from "./components/ScheduleDialog";
import { Orb } from "./components/Orb";
import { Capture } from "./lib/capture";
import { isMuted, loadMode, orbLine, outcomeFor, saveMode, type DictationMode } from "./lib/dictation";
import { WakeGate, compileWake, matchesWake, WAKE_PATTERNS } from "./lib/wake";
import type { Attachment, AvatarDesc, ChatMessage, Schedule, VoiceEntry } from "./types";
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
import { terminalAttached, terminalRead, terminals } from "./components/Terminal";
import { terminalSpecFor } from "./lib/terminal";
import { maxWidth } from "./lib/layout";
import { PANE_NOS, SHAPES, show as showOf, type PaneNo, type PaneSpec, type View } from "./lib/workspace";
import { ActivitySync, clearsOnRead } from "./lib/activity";
import {
  besideTarget, chatProjectId, chatTarget, composeRows, draftKey, drawnShowing, eitherPane, giveBackPane, heldElsewhere,
  isEmptyChat, openRows, routeEvent, runningHere, selectedChatOf, shownThread,
} from "./lib/chats";
import { useStableHandlers, useStableValue } from "./lib/stable";

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
/** Below this height (zoomed px, under the title bar) the mic strip folds into
 * one button beside the orb: the sidebar needs those 44px for its rows more
 * than the strip needs them (review of PR #29: 1920x700 at 160% showed under
 * two thread rows). */
const MIC_TIGHT_H = 560;

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
  // Under a card nothing reaches a shell (WP-D). Said from here, above the
  // workspace's error boundary: a render error unmounts the workspace, but
  // the terminals' sockets and paste pumps live on, and a hold driven from in
  // there froze with it (review of PR #32). A layout effect, so the hold is on
  // before a paste's next chunk can leave.
  useLayoutEffect(() => terminals.setBlocked(blocked), [blocked]);
  // A render error stopped the title bar, the shell and the orb drawing
  // (components/Boundary.tsx): the owner sees a Reload prompt and nothing
  // else, so nothing is heard or sent on their behalf from then on — no
  // push-to-talk, wake word, follow-up window or dictated send (review of
  // PR #32). The ref is what the capture hooks read; the state re-runs the
  // wake recognizer's effect, which stops it.
  const [crashed, setCrashed] = useState(false);
  const crashedRef = useRef(false);
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
  // A refused switch away from an unsaved edit says why, briefly, in a pane
  // the owner can see (by where it is shown): the File pane itself when it
  // is drawn, else the focused pane, naming the File pane — Open beside can
  // aim at a pane the layout hides (review of PR #27).
  const [refused, setRefused] = useState<Partial<Record<PaneNo, { at: number; pane: PaneNo }>>>({});
  useEffect(() => {
    const live = Object.values(refused).filter(Boolean).map((r) => r!.at);
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
  // Transcripts on their way, by thread: one load per thread at a time, and
  // it lands in whichever pane shows that thread when it arrives
  // (`ChatState.loadedThread`; review of PR #27).
  const loading = useRef(new Set<string>());
  // This window's own messages, named until the daemon names them; and the
  // ones waiting behind a turn (daemon id -> words and files), so a Stop that
  // drops them can put them back in the box (2026-10-08).
  const localSeq = useRef(0);
  // A first send's turn is tracked under its own `local-N` id until its
  // thread exists. That id is never sent to the daemon (re-review of PR #27):
  // a Stop or an orb press on it is remembered here and becomes a real
  // interrupt once the thread exists and the message is in.
  const placeholders = useRef(new Set<string>());
  const stopWhenOpen = useRef(new Set<string>());
  const waitingHere = useRef(new Map<string, { text: string; files: Attachment[] }>());
  // Words handed back for a thread that is not on screen wait here until the
  // owner opens it: never in another thread's box (Bugbot on PR #22).
  const heldBack = useRef(new HeldBack());

  const patch = useCallback((p: Parameters<typeof dispatch>[0] extends any ? any : never) => {
    dispatch({ type: "patch", patch: p });
  }, [dispatch]);
  /** One pane's conversation fields; an `orb` in it lands only for the selected chat. */
  const chatPatch = useCallback((pane: PaneNo, p: ChatPatch) => {
    dispatch({ type: "chat", pane, patch: p });
  }, [dispatch]);

  /** The chat panes the owner can see: drawn, and showing chat. */
  const chatsOnScreen = (): PaneNo[] => {
    const lay = layoutNow.current;
    return lay.fit.panes.filter((n) => lay.ws.panes[n - 1].view === "chat");
  };

  /**
   * Words and files back to a box: the box of a chat pane **on screen**
   * showing that thread (`from`, where they were typed, first), else held
   * until a drawn chat pane shows the thread — never another thread's box,
   * and never a hidden box a trade of conversations could carry to another
   * thread (review of PR #27). With no thread yet (a compose send that failed
   * before it opened one), back to the pane holding that compose row; if no
   * pane holds it any more, parked under that row's key (`park`), so the next
   * compose row there gets them — **never the selected chat**, which may
   * show another thread (decisions W-6; re-review of PR #27).
   */
  const giveBack = useCallback(
    (threadId: string | null, text: string, files: Attachment[], from: PaneNo | null = null,
     parkKey: string | null = null): "box" | "held" | "parked" | "lost" => {
      const at = live.current;
      const onScreen = chatsOnScreen();
      // A compose row under that key already on screen (New thread pressed
      // in that pane meanwhile) is where they belong: its box, at once.
      const holder = parkKey === null ? null
        : onScreen.find((n) => draftKey(at.chats[n], n) === parkKey) ?? null;
      const pane = giveBackPane(threadId, at.chats, pendingThread.current, from ?? holder, onScreen);
      if (pane !== null) {
        dispatch({ type: "give_back", pane, text, files, nonce: nextNonce() });
        return "box";
      }
      if (threadId) {
        heldBack.current.hold(threadId, text, files);
        return "held";
      }
      if (parkKey) {
        dispatch({ type: "park", key: parkKey, text, files });
        return "parked";
      }
      dispatch({ type: "patch", patch: { error: "Words that were not sent had no chat to go back to: open a chat." } });
      return "lost";
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // ---- the selected chat ---------------------------------------------------

  // Where ambiguous input goes (decisions W-6, lib/chats `selectedChatOf`):
  // the chat pane most recently clicked — a pointer or keyboard focus into a
  // chat pane focuses it, and that heads the order — always one the layout
  // draws (a dropped one gives way to the next most recently selected), and
  // none at all with no chat pane drawn. Synced into the store before paint,
  // so the callbacks below (capture, the orb, the SSE routing) read it from
  // `live`. `activeNow` is the chat the window is about: the selected one,
  // else the one selected last.
  const focusedChat: PaneNo | null =
    view.fit.panes.includes(view.ws.focused) && view.ws.panes[view.ws.focused - 1].view === "chat"
      ? view.ws.focused : null;
  const selected = selectedChatOf(view.ws, view.fit.panes, state.selectedOrder);
  const activeNow: PaneNo = selected ?? state.selectedOrder[0] ?? 1;
  useLayoutEffect(() => {
    const at = live.current;
    const front = focusedChat !== null && focusedChat === selected && at.selectedOrder[0] !== focusedChat;
    if (selected !== at.selectedChat || front) dispatch({ type: "select", pane: selected, front });
  }, [selected, focusedChat, state.selectedChat, state.selectedOrder, dispatch]);

  // ---- capture ------------------------------------------------------------

  const capture = useMemo(
    () =>
      new Capture({
        // He does not answer himself, and never talks over a pending
        // authorization or an open picker. Only the selected chat's turn
        // suppresses the mic — a long turn in another pane must not silence
        // it — and with no chat on screen there is nobody to talk to.
        suppressed: () =>
          live.current.orb === "speaking" ||
          live.current.selectedChat === null ||
          live.current.chats[live.current.selectedChat].busy ||
          live.current.approvals.length > 0 ||
          live.current.picker !== null ||
          chipOverlay.current ||
          crashedRef.current,
        muted: () => isMuted(live.current.dictation),
        onLevel: (level) => dispatch({ type: "patch", patch: { level } }),
        onListening: () => {
          const n = live.current.selectedChat;
          if (n !== null) dispatch({ type: "chat", pane: n, patch: { orb: "listening", status: "LISTENING · SPEAK NOW" } });
        },
        onIdle: (note) => {
          const n = live.current.selectedChat;
          if (n !== null) dispatch({ type: "chat", pane: n, patch: { orb: "idle", status: note || "" } });
          else dispatch({ type: "patch", patch: { orb: "idle" } });
        },
        onUtterance: (wav) => void onUtterance(wav),
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // Words spoken while no chat pane was on screen: they wait, unsent, for the
  // next chat selected (decisions W-6: ambiguous input goes to the selected
  // chat as it is when it is delivered, and there was none).
  const unplaced = useRef<string[]>([]);

  const onUtterance = useCallback(
    async (wav: Blob) => {
      // Ambiguous input (decisions W-6): what was said goes to **the selected
      // chat as it is when the transcript lands** — the chat pane most
      // recently clicked, whatever thread it shows by then. Clicking another
      // chat during STT sends the words there: the owner's call. What the
      // microphone is doing is said in the selected chat's input bar.
      const say = (p: ChatPatch) => {
        const n = live.current.selectedChat;
        if (n !== null) chatPatch(n, p);
        else if (p.error !== undefined) dispatch({ type: "patch", patch: { error: p.error } });
      };
      // A crashed window uploads nothing, and sends nothing, for anyone.
      if (crashedRef.current) return;
      const mode = live.current.dictation;
      const what = outcomeFor(mode);
      // OFF never reaches here (Capture refuses to claim while muted), but the
      // mode is checked again rather than assumed: the fail-closed direction
      // costs a discarded utterance and the other costs an upload.
      if (what === "discard") return;
      say({ orb: "transcribing", status: "TRANSCRIBING" });
      let text = "";
      try {
        text = await api.stt(wav);
      } catch (e: any) {
        say({ orb: "error", status: "STT FAILED", error: e.message });
        return;
      }
      // The window crashed while this was being transcribed: the words go
      // nowhere — not to a chat the owner cannot see, not into a box.
      if (crashedRef.current) return;
      if (!text.trim()) {
        say({ orb: "idle", status: "DIDN'T CATCH THAT" });
        return;
      }
      const n = live.current.selectedChat;
      if (n === null) {
        // No chat on screen: never sent anywhere. It waits for the next chat
        // selected, in its box, unsent — and the window says so.
        unplaced.current.push(text);
        dispatch({
          type: "patch",
          patch: { orb: "idle", error: "No chat is open, so what you said was not sent. It will be in the box of the next chat you select." },
        });
        return;
      }
      // REVIEW never sends on its own: the transcript lands in the box.
      if (what === "review") {
        chatPatch(n, { orb: "idle", status: "", pendingTranscript: text });
        return;
      }
      // Dictated: the Discord mirror labels it "You (HUD, voice)" (PR C).
      await send(n, text, [], true);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // Words that had no chat to go to come back, unsent, in the next one selected.
  useEffect(() => {
    if (state.selectedChat === null || !unplaced.current.length) return;
    const text = unplaced.current.join("\n");
    unplaced.current = [];
    dispatch({ type: "give_back", pane: state.selectedChat, text, files: [], nonce: nextNonce() });
  }, [state.selectedChat, dispatch]);

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
      // What this send tracks until it knows its thread: a compose row's turn
      // is tracked under this send's own id, then under the thread it opens.
      // A retry whose thread was opened by a failed first send knows it now:
      // tracked under it from the start, because nothing is awaited before
      // the retarget below, so `live` would not yet hold the placeholder and
      // the turn would stay tracked under it — its `turn_finished` would never
      // free the pane, and the mic would stay suppressed.
      let tracked = threadId ?? compose?.openedId ?? local;
      if (tracked === local) placeholders.current.add(local);
      dispatch({
        type: "message", pane,
        message: { role: "user", text, local, ...(steering ? { mark: "steering" as const } : {}) },
      });
      if (steering) chatPatch(pane, { error: "" });
      else {
        chatPatch(pane, {
          busy: true, orb: "thinking", status: "SENDING", draft: "", error: "", turnThreadId: tracked,
          // The compose row carries this send's id, so the send finds it again.
          ...(compose && !threadId ? { compose: { ...compose, sending: local } } : {}),
        });
      }
      /**
       * After every await, the pane is found again by what it holds (review of
       * PR #27): the owner may have traded this conversation into another
       * pane, or opened another thread in this one. `conversation`: the pane
       * holding this compose row or showing this thread — for what is drawn.
       * `turn`: the pane tracking this send's turn — for busy and Stop. Null:
       * nothing on screen is this send's any more, and no pane is touched.
       */
      const conversation = (): PaneNo | null => {
        const now = live.current.chats;
        const pend = pendingThread.current;
        return PANE_NOS.find((n) =>
          (!!compose && now[n].compose?.sending === local)
          || (!!threadId && (now[n].threadId === threadId || now[n].compose?.openedId === threadId
                             || pend[n] === threadId)),
        ) ?? null;
      };
      const turn = (): PaneNo | null => {
        const now = live.current.chats;
        return PANE_NOS.find((n) => now[n].busy && now[n].turnThreadId === tracked) ?? conversation();
      };
      const onPane = (which: () => PaneNo | null, p: ChatPatch) => {
        const n = which();
        if (n !== null) chatPatch(n, p);
      };
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
            const n = conversation();
            threadId = t.id;
            // Remembered, so a retry after a failed send reuses this thread.
            // The whole compose row is kept, provider, model and effort with
            // it, so a failed first send retries on the same choice.
            if (n !== null) {
              const row = live.current.chats[n].compose;
              chatPatch(n, { compose: { ...(row ?? compose), projectId, openedId: t.id } });
            }
          }
          const n = conversation();
          if (n !== null) pendingThread.current[n] = threadId;
          // The turn is now this thread's, wherever the pane tracking it is.
          const tracking = turn();
          const was = tracked;
          tracked = threadId;
          if (!steering && tracking !== null && live.current.chats[tracking].turnThreadId === was) {
            chatPatch(tracking, { turnThreadId: threadId });
          }
        }
        const result = await api.send(threadId, {
          text, attachments: attachments.length ? attachments : undefined,
          ...(spoken ? { spoken: true } : {}),
        });
        const status = result?.status ?? "started";
        // A Stop pressed while the thread was opening: the turn exists now,
        // so the stop it asked for goes to it.
        if (stopWhenOpen.current.delete(local) && status === "started") api.interrupt(threadId).catch(() => {});
        if (status === "dropped") {
          // The owner pressed Stop while this steer was on its way and the
          // turn would not take it: stop means stop, so it is not sent, and
          // its words go back to where they were typed (review of PR #22).
          dispatch({
            type: "mark", local,
            patch: { mark: "not sent", ...(result.message_id ? { message_id: result.message_id } : {}) },
          });
          giveBack(threadId, text, attachments, conversation());
          if (!steering) {
            onPane(turn, { busy: prior.busy, turnThreadId: prior.turnThreadId, orb: prior.orb, status: prior.status });
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
          onPane(conversation, { busy: true, turnThreadId: threadId });
        } else if (steering) {
          // The turn ended while this was on its way: it started its own.
          dispatch({ type: "mark", local, patch: { mark: undefined } });
          onPane(conversation, { busy: true, turnThreadId: threadId, orb: "thinking", status: "THINKING" });
        }
        if (projectId) saveLastProject(projectId);
        if (!chat.threadId) {
          // The compose row becomes the thread — in the pane that still holds
          // it, and only there: one the owner moved on is left where it is. Its
          // transcript is already on screen; reloading it here is what used to
          // wipe the first message.
          await refreshThreads();
          const n = conversation();
          if (n !== null) {
            if (live.current.chats[n].threadId !== threadId) {
              chatPatch(n, { threadId, compose: null, loadedThread: threadId });
            }
            pendingThread.current[n] = null;
          }
        }
        const n = turn();
        const now = n !== null ? live.current.chats[n] : null;
        if (n !== null && now && now.busy && now.turnThreadId === threadId && now.status === "SENDING") {
          chatPatch(n, { status: "THINKING" });
        }
        return true;
      } catch (e: any) {
        // Only this message comes off the screen — a reply that settled
        // meanwhile stays — and its words and files go back in the box, so
        // a failed send costs nothing. Said inline, never as a dead end.
        dispatch({ type: "unmessage", local });
        // A first send whose compose row no pane holds any more (the owner
        // opened another thread there meanwhile): its words are parked under
        // that row, never put in a box showing another thread.
        const back = conversation();
        const parkKey = !threadId && back === null ? `compose:${pane}` : null;
        const went = giveBack(threadId, text, attachments, back, parkKey);
        const error = `Could not send: ${e.message}`
          + (went === "parked" ? " — what you wrote is kept for the next new thread in this pane." : "");
        if (steering) {
          // A refused steer leaves the running turn as it was: still tracked,
          // still stoppable.
          chatPatch(pane, { error });
        } else {
          // Back to whatever the pane was tracking before this send (a turn
          // in another thread keeps running and keeps its Stop).
          const p: ChatPatch = {
            busy: prior.busy, turnThreadId: prior.turnThreadId,
            orb: prior.busy ? prior.orb : "error", status: prior.busy ? prior.status : "FAILED",
          };
          const n = turn();
          if (n !== null) chatPatch(n, { ...p, error });
          else dispatch({ type: "patch", patch: { error } });
        }
        return false;
      } finally {
        // Nothing is tracked under the placeholder once the send is over:
        // the turn is its thread's, or the pane went back to what it had.
        placeholders.current.delete(local);
        stopWhenOpen.current.delete(local);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  /**
   * Interrupt a tracked turn — never a placeholder: a first send's `local-N`
   * id is no thread the daemon knows (re-review of PR #27). A stop asked of
   * one is kept until its thread exists (`send` delivers it).
   */
  const interruptTurn = useCallback((turn: string, onError?: (e: any) => void) => {
    if (placeholders.current.has(turn)) {
      stopWhenOpen.current.add(turn);
      return;
    }
    api.interrupt(turn).catch((e: any) => onError?.(e));
  }, []);

  /** Stop the turn running in a pane's thread (the orb does the same for the selected chat's). */
  const stopTurn = useCallback((pane: PaneNo) => {
    const chat = live.current.chats[pane];
    const turn = chat.turnThreadId ?? chat.threadId;
    if (!turn) return;
    interruptTurn(turn, (e: any) => {
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
      // hear the turn finish, or it waits on THINKING — and, as the selected
      // chat, keeps the mic suppressed — for good. No thread: the selected
      // chat's (ambiguous input, decisions W-6), or no pane's with none.
      const tid: string | undefined = e.thread_id;
      const route = routeEvent(tid, at.chats, pendingThread.current, at.selectedChat);
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
            if (n === at.selectedChat) followUp = true;
          }
          // Only the selected chat's own turn opens the follow-up window. A
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
        case "terminal_attached":
          // `{terminal_id, at}` only: the terminals say so when it was not
          // this window's own attach (components/Terminal.tsx).
          terminalAttached(data);
          break;
        case "terminal_read":
          // `{terminal_id, lines, at, refused}` only: the terminal's bar says
          // "Jarvis read 200 lines · 15:42" (components/Terminal.tsx).
          terminalRead(data);
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
  // Stable, so a memoized ChatTab is not redrawn by a keystroke.
  const cancelTask = useCallback(
    (id: string) => void api.cancelTask(id).then(refreshTasks).catch(() => {}),
    [refreshTasks],
  );

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
    const { ws, fit } = layoutNow.current;
    const spec = ws.panes[pane - 1];
    if (spec.view !== "file" || next === "file" || !dirtyNow.current[pane]) return true;
    const shownIn: PaneNo = fit.panes.includes(pane) ? pane : fit.panes.includes(ws.focused) ? ws.focused : fit.panes[0];
    setRefused((r) => ({ ...r, [shownIn]: { at: Date.now(), pane } }));
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
    return chatTarget(lay.ws, lay.fit.panes, live.current.selectedChat).pane;
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
    dispatch({ type: "patch", patch: { taskFocus: false } });
    const holder = heldElsewhere(at.chats, pane, id, pend);
    if (holder === null) {
      pend[pane] = null;
      dispatch({ type: "open_thread", pane, threadId: id });
      return;
    }
    const there = at.chats[holder];
    // Everything a conversation holds moves with it — its transcript and
    // whether that has arrived yet (`loadedThread`), its draft, its turn, a
    // first send in flight — so neither pane reloads what it already has, and
    // one still loading keeps loading for the pane it is in now.
    [pend[pane], pend[holder]] = [pend[holder], pend[pane]];
    dispatch({ type: "chat_swap", a: pane, b: holder });
    // Held through a compose row that opened it: it is now the open thread.
    if (there.threadId !== id) dispatch({ type: "open_thread", pane, threadId: id });
  }, [dispatch]);

  /**
   * A thread picked in the sidebar (plan §2.2, "Where a sidebar click goes").
   * Already on screen in a chat pane: that pane is focused — a thread is open
   * in one pane at most. Otherwise the focused chat pane, else the selected
   * chat, else the focused pane switches to chat. `beside` (Alt+click, or
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
        chatPatch(n, {
          threadId: after.threadId, compose: after.compose, messages: [], loadedThread: null, draft: "", ops: [], status,
        });
      }
      // The task view it showed is empty now: the status line says why.
      if (taskGone && !displaced) chatPatch(activePane(at), { status });
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
        chatPatch(n, {
          threadId: after.threadId, compose: after.compose, messages: [], loadedThread: null, draft: "", ops: [],
        });
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
        loadedThread: null,
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
      const pane = chatTarget(lay.ws, lay.fit.panes, live.current.selectedChat).pane;
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

  // A thread's transcript is loaded when a pane opens it, so switching redraws
  // from the saved conversation rather than from whatever is on screen. A
  // load lands in **whichever pane shows that thread when it arrives**, and
  // is dropped if none does: the pane it was asked for may have moved on, or
  // traded conversations with another, meanwhile (review of PR #27 — a late
  // transcript used to land in the pane number it was asked for, drawing one
  // thread's messages under another). `loadedThread` says whose transcript a
  // pane's messages are; a conversation that moves between panes takes it
  // along, so one that has arrived is never loaded twice and one still on its
  // way is still loaded.
  const toLoad = PANE_NOS.map((n) => `${state.chats[n].threadId ?? ""}:${state.chats[n].loadedThread ?? ""}`).join("|");
  useEffect(() => {
    const land = (id: string, messages: ChatMessage[]) => {
      const at = live.current;
      const n = PANE_NOS.find((p) => at.chats[p].threadId === id);
      if (n === undefined) return;
      // A turn running in another thread keeps the pane busy, but its status
      // line is about that thread, not this one.
      const chat = at.chats[n];
      const status = chat.turnThreadId === id ? chat.status : "";
      chatPatch(n, { messages, draft: "", ops: [], status, loadedThread: id });
    };
    for (const n of PANE_NOS) {
      const chat = live.current.chats[n];
      const id = chat.threadId;
      if (!id || chat.loadedThread === id || loading.current.has(id)) continue;
      loading.current.add(id);
      api
        .transcript(id)
        .then((r) => land(id, r.messages || []))
        .catch(() => land(id, []))
        .finally(() => loading.current.delete(id));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toLoad]);

  // Words handed back while a thread was not on screen come back when a
  // **drawn** chat pane shows it — on opening it, and when a pane already
  // holding it comes on screen again (review of PR #27).
  const onScreenThreads = view.fit.panes
    .filter((n) => view.ws.panes[n - 1].view === "chat")
    .map((n) => `${n}:${shownThread(state.chats[n]) ?? ""}`)
    .join("|");
  useEffect(() => {
    for (const n of chatsOnScreen()) {
      const id = shownThread(live.current.chats[n]);
      if (!id) continue;
      const held = heldBack.current.take(id);
      if (held) dispatch({ type: "give_back", pane: n, text: held.text, files: held.files, nonce: nextNonce() });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onScreenThreads, dispatch]);

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
    if (crashed) return; // so does a window that stopped drawing
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
  }, [state.dictation, crashed]);

  const onWakeHit = useCallback(() => {
    if (crashedRef.current) return;
    if (live.current.approvals.length) return; // never talk over a pending authorization
    if (live.current.picker || chipOverlay.current) return;
    capture.onWake();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capture]);

  // ---- push to talk -------------------------------------------------------

  const press = useCallback(() => {
    if (crashedRef.current) return; // nothing on screen to talk to
    if (live.current.approvals.length) return; // answer the authorization first
    if (live.current.picker || chipOverlay.current) return;
    // Push-to-talk and the orb act on the selected chat (decisions W-6): with
    // no chat pane on screen there is no turn to interrupt and nobody to
    // talk to, and the window says so rather than recording for nobody.
    const pane = live.current.selectedChat;
    if (pane === null) {
      dispatch({ type: "patch", patch: { error: "No chat is open to talk to: open a chat first." } });
      return;
    }
    // The interrupt half comes first: muting yourself must not take away the
    // orb as the way to shut him up. It interrupts the selected chat's turn —
    // a turn running in another pane keeps running, and keeps its Stop.
    const chat = live.current.chats[pane];
    if (chat.busy && chat.turnThreadId) interruptTurn(chat.turnThreadId);
    if (isMuted(live.current.dictation)) {
      chatPatch(pane, { status: "MIC MUTED" });
      return;
    }
    capture.press();
  }, [capture, chatPatch, dispatch, interruptTurn]);

  const release = useCallback(() => capture.release(), [capture]);

  // What the workspace's error boundary calls once it has caught a render
  // error: a paste going out to a shell stops (nothing on screen could show
  // its notice or Resume), and nothing in the microphone's hands is sent.
  const onCrash = useCallback(() => {
    crashedRef.current = true;
    terminals.abortPastes();
    capture.abandon();
    setCrashed(true);
  }, [capture]);

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

  // ---- files dropped on the window -----------------------------------------

  // Files dropped anywhere but a box (which stages its own): on a chat pane,
  // into that pane's box; anywhere else — the sidebar, the status pane, a File
  // or Preview pane — they are ambiguous input and go to the selected chat's
  // box (decisions W-6), staged, never sent. With no chat on screen the window
  // says so. Without this, the browser opened the dropped file in place of
  // the HUD.
  useEffect(() => {
    const over = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
    };
    const drop = (e: DragEvent) => {
      const dropped = Array.from(e.dataTransfer?.files || []);
      if (!dropped.length || e.defaultPrevented) return;
      e.preventDefault();
      const el = e.target instanceof Element ? e.target : null;
      const own = el?.closest('[data-testid^="pane-"][data-view="chat"]');
      const ownNo = own ? (Number(own.getAttribute("data-testid")!.slice(5)) as PaneNo) : null;
      const pane = ownNo ?? live.current.selectedChat;
      if (pane === null) {
        dispatch({ type: "patch", patch: { error: "No chat is open to attach files to: open a chat first." } });
        return;
      }
      // The conversation they were dropped for, not the pane: reading them in
      // takes a while, and by then that pane may show another thread. The
      // store adds them to whichever pane holds that conversation when they
      // are ready, else to its parked draft (`stage`; re-review of PR #27).
      const key = draftKey(live.current.chats[pane], pane);
      if (key === null) {
        dispatch({ type: "patch", patch: { error: "No chat is open to attach files to: open a chat first." } });
        return;
      }
      void (async () => {
        const notes: string[] = [];
        const read: Attachment[] = [];
        const room = MAX_FILES - live.current.chats[pane].files.length;
        for (const f of dropped) {
          if (read.length >= room) {
            notes.push(`[${f.name} skipped: 8 files per turn]`);
            continue;
          }
          const a = await toAttachment(f);
          if (typeof a === "string") notes.push(a);
          else read.push(a);
        }
        dispatch({ type: "stage", key, files: read });
        if (notes.length) dispatch({ type: "patch", patch: { error: notes.join(" ") } });
      })();
    };
    document.addEventListener("dragover", over);
    document.addEventListener("drop", drop);
    return () => {
      document.removeEventListener("dragover", over);
      document.removeEventListener("drop", drop);
    };
  }, [dispatch]);

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
      // The window's state, the active chat's conversation laid over the
      // top as the one conversation's fields were (the single-layout suites
      // read those); `chats` has every pane's, `selectedChat` says which is
      // selected (null with no chat on screen).
      state: () => flatState(live.current),
      // A legacy `patch` of conversation fields lands in the active chat's pane.
      dispatch: (a: any) => {
        for (const x of compatActions(a, activePane(live.current))) dispatch(x);
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
    if (live.current.selectedChat !== null) chatPatch(live.current.selectedChat, { status: "" });
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
  // A short window folds the orb's mic strip into one button beside it.
  const micTight = !drawn.leftFolded && view.availableH < MIC_TIGHT_H;
  // What the orb says under itself (PR #29; the input bar's hint used to say
  // it): the selected chat's turn status, else a card's question, else MIC
  // MUTED, else the orb's state (lib/dictation `orbLine`). Folded, the orb is
  // a 36px dot with no line under it, so the selected chat's bar says it.
  const orbStatus = orbLine({
    status: selected !== null ? state.chats[selected].status : "",
    mode: state.dictation,
    approvals: state.approvals.length,
    orb: state.orb,
  });
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
    const target = n === selected;
    // Another chat pane's status line says only what its own turn is doing;
    // the selected chat's is on the orb — here only while the orb is folded.
    const status = !target ? chat.status : drawn.leftFolded ? orbStatus : null;
    const compose = chat.compose;
    return (
      <>
        <ChatTab
          thread={thread}
          messages={chat.messages}
          draft={chat.draft}
          ops={chat.ops}
          onCancelTask={cancelTask}
        />
        <InputBar
          status={status}
          pendingTranscript={chat.pendingTranscript}
          // Not `disabled` under a card: everything outside the card is inert,
          // which keeps the box unreachable, and a disabled box dropped focus
          // to the page before the card could record it — so focus never came
          // back to the box after the card (re-review of PR #27).
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
          modelChip={threadModel.chipFor(n, view.zoom)}
          imageNote={threadModel.imageNoteFor(n)}
          onSend={(text, files) => void send(n, text, files)}
          onTranscriptTaken={() => chatPatch(n, { pendingTranscript: "" })}
          running={runningHere(chat)}
          onStop={() => stopTurn(n)}
          restore={chat.restore}
          onRestoreTaken={(nonce) => dispatch({ type: "given_back", pane: n, nonce })}
          // The draft is the conversation's: it moves with it (review of PR #27).
          text={chat.input}
          files={chat.files}
          onText={(text) => dispatch({ type: "input", pane: n, text })}
          onFiles={(files) => dispatch({ type: "input", pane: n, files })}
          stageKey={draftKey(chat, n)}
          onStage={(key, files) => dispatch({ type: "stage", key, files })}
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
    const refusal = refused[n];
    return (
      <>
        {refusal && dirtyPanes[refusal.pane] ? (
          <span className="chip panepin warn" data-testid={`pane-${n}-refused`} role="status"
                title="Switching that pane away would close its file and drop the edit">
            {refusal.pane === n
              ? "unsaved edit · save or reload first"
              : `pane ${refusal.pane} has an unsaved edit · save or reload it first`}
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
  // Kept as the same lists while their content is unchanged, and its callbacks
  // stable (lib/stable.ts): the Sidebar is memoized, and a keystroke in a chat
  // box must not redraw every thread row (re-review of PR #27).
  const sidebarOpen = useStableValue(openRows(ws, fit.panes, state.chats, activeNow));
  const sidebarComposing = useStableValue(composeRows(ws, fit.panes, state.chats, activeNow));
  const sidebarCalls = useStableHandlers({
    onCollapse: () => view.fold("left"),
    onPickThread: pickThread,
    onPickTask: (id: string) => {
      patch({ taskId: id, taskFocus: true });
      showView("task");
    },
    onNewProject: () => patch({ picker: "newProject" }),
    onNewThread: () => newThread(),
    onNewTask: (projectId: string) => {
      setNewTaskProject(projectId);
      patch({ picker: "newTask" });
    },
    onMoveThread: moveThread,
    onMoveCompose: (projectId: string, pane: PaneNo) => {
      const c = live.current.chats[pane].compose;
      if (c) chatPatch(pane, { compose: { ...c, projectId } });
    },
    onEditProject: (id: string) => {
      setProjectTarget(id);
      patch({ picker: "editProject" });
    },
    onArchiveProject: (id: string) => {
      setProjectTarget(id);
      patch({ picker: "archiveProject" });
    },
    onRenameProject: (id: string, name: string) =>
      api.patchProject(id, { name }).then(async (saved) => {
        await refreshProjects(id);
        return saved;
      }),
    onRenameThread: (id: string, title: string) =>
      api.renameThread(id, title).then(async (saved) => {
        await refreshThreads();
        return saved;
      }),
    onArchiveThread: (id: string) => api.archiveThread(id).then(() => threadGone(id)),
    onOpenArchive: () => patch({ picker: "archive" }),
    onOpen: (what: "schedules" | "route" | "usage") => {
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
    },
  });

  return (
    <>
      {/* A render error in here — the title bar, the shell, the orb — shows a
          Reload prompt in their place and goes no further: the card below is
          outside it, so it stays up and answerable (components/Boundary.tsx). */}
      <WorkspaceBoundary onCrash={onCrash}>
      <TitleBar view={view} blocked={blocked} onPicker={(which) => patch({ picker: which })} />
      <div
        id="shell"
        className={(drawn.leftFolded ? "left-collapsed " : "") + (drawn.rightFolded ? "right-collapsed " : "")
          + (micTight ? "mic-tight" : "")}
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
          onCollapse={sidebarCalls.onCollapse}
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
          onPickThread={sidebarCalls.onPickThread}
          onPickTask={sidebarCalls.onPickTask}
          onNewProject={sidebarCalls.onNewProject}
          onNewThread={sidebarCalls.onNewThread}
          onNewTask={sidebarCalls.onNewTask}
          moveError={state.moveError}
          onMoveThread={sidebarCalls.onMoveThread}
          onMoveCompose={sidebarCalls.onMoveCompose}
          onEditProject={sidebarCalls.onEditProject}
          onArchiveProject={sidebarCalls.onArchiveProject}
          onRenameProject={sidebarCalls.onRenameProject}
          onRenameThread={sidebarCalls.onRenameThread}
          onArchiveThread={sidebarCalls.onArchiveThread}
          onOpenArchive={sidebarCalls.onOpenArchive}
          onOpen={sidebarCalls.onOpen}
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
          <CrashProbe where="workspace" />
          <Workspace
            view={view}
            blocked={blocked}
            render={renderView}
            extras={paneExtras}
            context={paneContext}
            selectedPane={selected}
            onView={setPaneView}
            projects={state.projects}
            // Where `+` opens a terminal: the folder of what the focused pane
            // shows, as an id. A chat pane is its own conversation; any other
            // pane defers to the chat the window is about (`activeChat`: the
            // selected chat, else the one selected last) — the same chat
            // File and Preview panes follow, so a terminal opened from an
            // unpinned File pane lands where that pane points.
            terminalIn={() => {
              const n = view.ws.focused;
              const spec = view.ws.panes[n - 1];
              const chat = spec.view === "chat" ? state.chats[n] : activeChat(state);
              return terminalSpecFor(spec.view, {
                thread: chat.threadId, compose: chat.compose?.projectId ?? null,
                project: paneProject(spec, n), task: state.taskId,
              });
            }}
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
        tight={micTight}
        blocked={blocked}
        zoom={view.zoom}
        status={orbStatus}
        mode={state.dictation}
        onModeChange={setMode}
        onPress={press}
        onRelease={release}
      />
      </WorkspaceBoundary>

      <ApprovalVeil requests={state.approvals} onDecide={decide} />

      {/* A picker or dialog that throws is closed, and the window carries on
          (components/Boundary.tsx). */}
      <DialogBoundary
        resetKey={`${state.picker ?? ""}|${threadModel.overlayOpen}`}
        onCrash={() => {
          threadModel.closeDialogs();
          patch({ picker: null, error: "A dialog hit an error and was closed. Reload if it keeps happening." });
        }}
      >
      <CrashProbe where="dialog" />
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
      </DialogBoundary>
    </>
  );
}
