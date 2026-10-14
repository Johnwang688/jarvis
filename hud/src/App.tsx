import { SendStops } from "./lib/sendStops";
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
// transcript does (steering its turn if one is running: V3), the orb follows
// its turn (Stop interrupts it; push-to-talk does not), the follow-up mic
// window opens only when its turn ends, no turn silences the mic, and files
// dropped outside a chat pane are staged in its box.
// Input that belongs to a thread — typed words, a steer, a hand-back, a first
// send, a transcript — stays with that thread.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { CaptureDrafts } from "./lib/captureDraft";
import type { CaptureDestination } from "./components/BrowserCapture";
import { api, subscribe } from "./api";
import { browserSound } from "./state/browserSoundClient";
import {
  useStore, activeChat, activePane, compatActions, currentProjectId, currentTask, flatState, turnOrb,
  type ChatPatch, type State,
} from "./state/store";
import { Sidebar } from "./components/Sidebar";
import { toolGroups } from "./state/toolGroups";
import { ChatTab } from "./components/ChatTab";
import { InputBar, MAX_FILES, toAttachment } from "./components/InputBar";
import { TaskView } from "./components/TaskView";
import { FileTab, type FileAsk } from "./components/FileTab";
import { VexView } from "./components/VexView";
import { DETECT_STALE_MS, vex } from "./state/vex";
import { offersVex } from "./lib/vex";
import { ENV_LANGUAGE, makeModel, monacoEditors, monacoLanguage, monacoModels, relanguage, setEditorsBlocked } from "./lib/monaco";
import { buffers, PlainModel } from "./state/buffers";
import { renameTabs, sameTabs, writtenPaths } from "./lib/buffers";
import { Palette, QuickOpen } from "./components/QuickOpen";
import { focusedEditor } from "./components/Editor";
import { EditorSettingsDialog } from "./components/EditorSettings";
import { applyEditorConfig } from "./state/editorconfig";
import { editorSettingsProbe } from "./state/editorSettings";
import { commandOf, paletteEntries, type CommandContext, type CommandId, type EditorAction, type Entry } from "./lib/commands";
import type { EditorShortcut } from "./lib/layout";
import { treeRoot } from "./lib/dirtyMarks";
import { DiffTab } from "./components/DiffTab";
import { ProjectDiff } from "./components/ProjectDiff";
import { diffMode, diffWhy, type DiffMode } from "./lib/scm";
import { BrowserTab } from "./components/BrowserTab";
import { useBrowser } from "./state/browser";
import { useBrowserFrames } from "./state/browserframes";
import { BROWSER_TABS_MAX, browserTabId, VIEW_LABEL } from "./lib/workspace";
import { autoOpenPlan, devUrl, sourceKey, DEV_OPEN_IDLE, type DevSource, type DevWindow } from "./lib/devopen";
import { devservers } from "./state/devserverClient";
import type { DevAttempt, PendingDev } from "./state/devservers";
import { DevServerPrompts, type DevTarget } from "./components/DevServerPrompt";
import { BrowserSettings } from "./components/BrowserSettings";
import { cleanTitle } from "./lib/remote";
import { judgeBrowserUrl } from "./lib/browser";
import { daemonPortsNow } from "./state/daemonPorts";
import { allowRestore, cancelLoad, installFrameGuard } from "./state/frameguard";
import { frameKey } from "./lib/frameguard";
import { ApprovalQueue, ApprovalVeil } from "./components/Approvals";
import { driving } from "./state/driving";
import { CrashProbe, DialogBoundary, WorkspaceBoundary } from "./components/Boundary";
import { DecisionsLog, DiscordPanel, SchedulesButton, UsagePanel } from "./components/Panels";
import { AvatarPicker, ModelPicker, NewProject, NewTask, GeneralSettings, RoutingSettings, VoicePicker } from "./components/Pickers";
import { SettingsPage } from "./components/SettingsPage";
import { loadSettingsSection, saveSettingsSection, type SettingsSection } from "./lib/settings";
import { useInstallApp } from "./state/installApp";
import { ScheduleDialog } from "./components/ScheduleDialog";
import { Orb } from "./components/Orb";
import { SpeechStrip, stripShown } from "./components/SpeechStrip";
import { ReadAloudSetting } from "./components/ReadAloud";
import { createSpeech, realClock, saveReadAloud, saveSelfVoiceGate } from "./lib/speech";
import { BargeIn } from "./lib/speech/barge";
import { CommandRouter } from "./lib/speech/commands";
import { SelfVoiceSetting } from "./components/SelfVoice";
import { VoiceChip } from "./components/VoiceChip";
import { micSuppressedBySpeech, onMicModeChange, speakingThreads } from "./lib/speech/policy";
import { Capture, type UtteranceChunk, type UtteranceEnd } from "./lib/capture";
import { STT_MAX_IN_FLIGHT, SttAssembler, placementFor, type SttCapture, type SttPlaced } from "./lib/stt";
import { startMicInput, type MicInput } from "./lib/micInput";
import micWorkletUrl from "./lib/micWorklet.ts?worker&url";
import { SttRetry } from "./components/SttRetry";
import { VoiceDiagnostics, openMicrophone, type SuppressionReason, type DropReason } from "./lib/voiceDiagnostics";
import { VoiceDiagnosticsPanel } from "./components/VoiceDiagnostics";
import { EndPauseSetting } from "./components/EndPause";
import { VadStage } from "./lib/listen/stage";
import { loadEndPause, saveEndPause } from "./lib/voiceTiming";
import { isMuted, loadMode, orbLine, outcomeFor, saveMode, type DictationMode } from "./lib/dictation";
import { WakeGate, compileWake, matchesWake, WAKE_PATTERNS } from "./lib/wake";
import type { Attachment, AvatarDesc, Schedule, Thread, VoiceEntry } from "./types";
import type { RosterView } from "./lib/roster";
import { moveThreadTo } from "./lib/threads";
import { lastProject, loadLastProject, saveLastProject } from "./lib/compose";
import { composeChoice, threadBody } from "./lib/threadmodel";
import { threadLabel } from "./lib/threadTitle";
import { HeldBack, nextNonce } from "./lib/giveback";
import { useThreadModel } from "./components/ThreadModelControls";
import { ProjectDialog } from "./components/Pickers";
import { NewVexProject } from "./components/NewVexProject";
import { ArchiveConfirm, ArchiveView } from "./components/Archive";
import { afterProjectGone, afterThreadGone, forgetLastProject, projectNamesTaken } from "./lib/projects";
import { guildConfigured, ownerLine } from "./lib/discord";
import { CollapseButton, Rail, Splitter, TitleBar, focusPaneElement, useLayout } from "./components/Layout";
import { Workspace, type PaneInfo } from "./components/Workspace";
import { terminalAttached, terminalRead, terminals } from "./components/Terminal";
import { inTerminal, terminalSpecFor } from "./lib/terminal";
import { SPACE_HINT, SPACE_HINT_KEY, TalkHold, isTalkKey, isTypingTarget, wantsSpaceHint } from "./lib/talkkey";
import { inMonaco, maxWidth } from "./lib/layout";
import { chordSecond } from "./lib/editorkeys";
import {
  PANE_NOS, SHAPES, allocate, closePane as closePaneOf, copySpec, inWorkspace, orderOf, show as showOf, slotOf, splitTitle,
  type PaneNo, type PaneSpec, type View,
} from "./lib/workspace";
import { ActivitySync, clearsOnRead } from "./lib/activity";
import {
  LISTENING_STATUS, ORPHAN_DRAFT, besideTarget, chatProjectId, chatTarget, composeRows, draftKey, drawnShowing, eitherPane, giveBackPane,
  heldElsewhere,
  isEmptyChat, nonePerPane, openRows, routeEvent, runningHere, selectedChatOf, shownThread,
} from "./lib/chats";
import { useStableHandlers, useStableValue } from "./lib/stable";
import BootOverlay from "./components/BootOverlay";
import { thinkingLine, type ThinkingLine } from "./lib/thinking";
import { thinkingClock } from "./components/MiniOrb";

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
const perPane = nonePerPane;

/** The editor's change poll (ED-2, plan §2.4): every 2 s while the window is visible. */
const BUFFER_POLL_MS = 2000;

// The window's one buffer registry talks to the daemon through the API and
// makes Monaco models — or, when Monaco cannot load, the fallback's shim, so
// the save path stays reachable.
buffers.configure({
  read: (projectId, path) => api.readFile(projectId, path),
  write: (projectId, body, signal) => api.writeFile(projectId, body, signal),
  stat: (projectId, paths) => api.statFiles(projectId, paths),
  makeModel: (text, abs, kind) => makeModel(text, abs, kind).catch(() => new PlainModel(text)),
  now: () => Date.now(),
  // A renamed file's model takes its new name's language (ED-7).
  relanguage: (model, abs, kind) => relanguage(model, abs, kind),
  // `.editorconfig` (ED-8), read through the same owner-only routes.
  prepare: (model, projectId, path) => applyEditorConfig(
    { stat: (id, paths) => api.statFiles(id, paths), read: (id, p) => api.readFile(id, p) },
    model as any, projectId, path),
});

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;
type FileAskBody = DistributiveOmit<FileAsk, "nonce">;

/** What quick open and the palette act on, taken the moment they open (ED-7). */
interface NavContext {
  /** The File pane they act in (or the pane that becomes one). */
  pane: PaneNo;
  projectId: string | null;
  /** The pane is not a File pane yet: it switches when something is opened. */
  switchTo: boolean;
  activePath: string | null;
  /** The editor that had focus, for Monaco's actions in the palette. */
  editor: any | null;
  ctx: CommandContext;
  actions: EditorAction[];
  /** Where focus goes back to when the box closes with nothing done. */
  returnTo: HTMLElement | null;
}

const PROVIDER_LABEL: Record<string, string> = { fast: "Jarvis", claude: "Claude", codex: "Codex" };

/** Who a thread is, for the editor's "changed on disk by …" toast. */
function writerOf(threadId: string | undefined, at: { threads: { id: string; provider: string; task_id: string | null; title: string }[]; tasks: { id: string; thread_ids: string[] }[] }): string {
  const thread = threadId ? at.threads.find((t) => t.id === threadId) : undefined;
  const task = threadId
    ? at.tasks.find((k) => k.id === thread?.task_id || k.thread_ids.includes(threadId)) : undefined;
  const who = thread ? PROVIDER_LABEL[thread.provider] || thread.provider : "an agent";
  if (task) return `${who} · task ${task.id.slice(0, 6)}`;
  return thread?.title ? `${who} · ${thread.title}` : who;
}

/**
 * The project a pane is about, for the VEX view (VX-2): whether its strip
 * offers "vex", and the project a pane switched to it is pinned to. A chat
 * pane is its own conversation's; any other pane its pin while that project
 * exists, else the chat the window is about (as File and Preview follow it)
 * — **except a VEX view, which never follows the chat**: its pin, or none
 * (review L7: a VEX pane whose project is gone says "Pick a project" rather
 * than building or downloading another robot's program).
 */
function stripProject(at: State, spec: PaneSpec, pane: PaneNo): string | null {
  if (spec.view === "chat") return chatProjectId(at.chats[pane], at.threads);
  if (spec.projectId && at.projects.some((p) => p.id === spec.projectId)) return spec.projectId;
  if (spec.view === "vex") return null;
  return currentProjectId(at);
}

export default function App() {
  const { state, dispatch } = useStore();
  // Zoom, pane widths and folded panes (§18, 2026-10-08): lib/layout.ts; the
  // centre's panes, their views and the bottom panel (2026-10-09):
  // lib/workspace.ts. Every layout key is inert while an authorization card
  // is up (as PTT is), and every layout button is disabled.
  const blocked = state.approvals.length > 0;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState(loadSettingsSection);
  const settingsNow = useRef(settingsOpen);
  settingsNow.current = settingsOpen;
  const pageSettingsReturn = useRef<HTMLElement | null>(null);
  const settingsKeyRef = useRef<() => void>(() => {});
  const workBlocked = blocked || settingsOpen;
  const devVersion = useSyncExternalStore(devservers.subscribe, devservers.version);
  const devPolicy = useRef(DEV_OPEN_IDLE);
  const devPointer = useRef({ down: false, released: 0 });
  const devUsed = useRef<Partial<Record<PaneNo, number>>>({});
  const [devUnseen, setDevUnseen] = useState<Record<string, string>>({});
  useEffect(() => {
    const paneOf = (target: EventTarget | null): PaneNo | null => {
      const node = target instanceof Element ? target.closest<HTMLElement>("[data-pane]") : null;
      const id = Number(node?.dataset.pane);
      if (PANE_NOS.includes(id as PaneNo)) return id as PaneNo;
      const test = target instanceof Element ? target.closest<HTMLElement>('[data-testid^="pane-"]')?.dataset.testid : "";
      const match = /^pane-([1-8])$/.exec(test ?? "");
      return match ? Number(match[1]) as PaneNo : null;
    };
    const used = (e: Event) => { const pane = paneOf(e.target); if (pane) devUsed.current[pane] = Date.now(); };
    const down = (e: PointerEvent) => { devPointer.current.down = e.buttons !== 0; used(e); };
    const up = (e?: PointerEvent) => {
      devPointer.current = { down: !!e?.buttons, released: Date.now() };
      setTimeout(devservers.wake, 150);
    };
    const blur = () => up();
    const focus = () => devservers.focus();
    if (document.hasFocus()) focus();
    document.addEventListener("pointerdown", down, true);
    document.addEventListener("pointerup", up, true);
    document.addEventListener("pointercancel", up, true);
    document.addEventListener("keydown", used, true);
    window.addEventListener("blur", blur);
    window.addEventListener("focus", focus);
    return () => {
      document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("pointerup", up, true);
      document.removeEventListener("pointercancel", up, true);
      document.removeEventListener("keydown", used, true);
      window.removeEventListener("blur", blur);
      window.removeEventListener("focus", focus);
    };
  }, []);
  const installApp = useInstallApp(blocked);
  // ED-7's keys (Ctrl+P, Ctrl+Shift+P / F1, Ctrl+Shift+F) are taken by
  // `useLayout`'s capture handler — the one interception place — and acted on
  // here with the latest render (`editorKeyRef`, set below).
  const editorKeyRef = useRef<(what: EditorShortcut) => void>(() => {});
  const view = useLayout(blocked, (what) => editorKeyRef.current(what),
    { open: settingsOpen, toggle: () => settingsKeyRef.current() });
  const browserFrames = useBrowserFrames(view.ws, view.fit.panes, settingsOpen);
  // Under a card nothing reaches a shell (WP-D). Said from here, above the
  // workspace's error boundary: a render error unmounts the workspace, but
  // the terminals' sockets and paste pumps live on, and a hold driven from in
  // there froze with it (review of PR #32). A layout effect, so the hold is on
  // before a paste's next chunk can leave.
  useLayoutEffect(() => terminals.setBlocked(workBlocked), [workBlocked]);
  useLayoutEffect(() => terminals.setCovered(settingsOpen), [settingsOpen]);
  // And nothing reaches an editor: every Monaco goes read-only while a card
  // is up (ED-1; the card takes focus itself), said from here for the same
  // reason.
  useLayoutEffect(() => setEditorsBlocked(workBlocked), [workBlocked]);
  // And nothing builds or downloads to a VEX brain (VX-2): every VEX action
  // refuses while a card is up, and a download's chain a card came into is
  // dropped (state/vex.ts) — said from here for the same reason.
  useLayoutEffect(() => vex.setBlocked(workBlocked), [workBlocked]);
  // And no Browser page takes the keyboard on its own (R-1, state/frameguard):
  // the window-level listeners, installed from here, above the boundary, in a
  // layout effect so they are on before any frame can load.
  useLayoutEffect(() => installFrameGuard(), []);
  useSyncExternalStore(vex.subscribe, vex.getVersion);
  // Whether each drawn pane's project is a VEX project: the strip offers the
  // VEX view only then (lib/vex.ts `offersVex`). Asked once per project.
  const vexWanted = Array.from(new Set(view.fit.panes
    .map((n) => stripProject(state, view.ws.panes[n - 1], n))
    .filter((x): x is string => !!x))).join("\u0000");
  useEffect(() => {
    const pids = vexWanted.split("\u0000").filter(Boolean);
    for (const pid of pids) vex.want(pid);
    // Asked again on the window's focus and when stale (review L4), so a
    // project that becomes a VEX project is offered the view without a reload.
    const fresh = () => vex.refreshDetections(pids, 0);
    const stale = setInterval(() => {
      if (document.visibilityState === "visible") vex.refreshDetections(pids, DETECT_STALE_MS);
    }, DETECT_STALE_MS);
    window.addEventListener("focus", fresh);
    return () => {
      clearInterval(stale);
      window.removeEventListener("focus", fresh);
    };
  }, [vexWanted]);
  // A render error stopped the title bar, the shell and the orb drawing
  // (components/Boundary.tsx): the owner sees a Reload prompt and nothing
  // else, so nothing is heard or sent on their behalf from then on — no
  // push-to-talk, wake word, follow-up window or dictated send (review of
  // PR #32). The ref is what the capture hooks read; the state re-runs the
  // wake recognizer's effect, which stops it.
  const [crashed, setCrashed] = useState(false);
  const browser = useBrowser(view, settingsOpen || crashed, blocked);
  const crashedRef = useRef(false);
  // File panes holding an unsaved edit: such a buffer is never closed behind
  // the owner's back — "follow chat" waits, a project that goes away keeps
  // the pane pinned until the owner discards the edit, closing the window
  // asks (2026-10-09, the minimal guard), and since WP-B nothing switches the
  // pane to another view — a tab, a sidebar click, a chat landing there —
  // until the edit is saved or discarded.
  //
  // Since ED-2 the truth is the buffer registry (`state/buffers.ts`): a pane
  // "holds an unsaved edit" when it holds the last view of a dirty buffer —
  // one no other pane has open (split-panes plan G-3): letting go of a view
  // another pane still shows loses nothing. It is read where it is needed
  // (`dirtyNow`), never copied a frame behind: the registry says so in the
  // keystroke's own change event (#43's M1), and this render follows.
  useSyncExternalStore(buffers.subscribe, buffers.version);
  const dirtyNow = useCallback((pane: PaneNo) => buffers.paneLoses(pane), []);
  const dirtyPanes: Partial<Record<PaneNo, boolean>> = {};
  for (const n of PANE_NOS) if (buffers.paneLoses(n)) dirtyPanes[n] = true;
  // Any unsaved edit at all — a buffer no tab holds any more included.
  const anyDirty = buffers.anyDirty();
  useEffect(() => {
    if (!anyDirty) return;
    const ask = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", ask);
    return () => window.removeEventListener("beforeunload", ask);
  }, [anyDirty]);
  // The change poll (ED-2, plan §2.4): every 2 s while the window is visible,
  // and the moment it is shown or focused again. inotify cannot see a write
  // made from Windows on the 9p `/mnt/c` mount, where VEX projects live.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") void buffers.poll();
    };
    const t = setInterval(tick, BUFFER_POLL_MS);
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(t);
      window.removeEventListener("focus", tick);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);
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
  const sendStops = useRef(new SendStops());
  const waitingHere = useRef(new Map<string, { text: string; files: Attachment[] }>());
  // Words handed back for a thread that is not on screen wait here until the
  // owner opens it: never in another thread's box (Bugbot on PR #22).
  const heldBack = useRef(new HeldBack());
  // Each chat pane's thinking line as last handed to its (memoized) ChatTab:
  // the same object while it says the same thing, so a keystroke anywhere
  // does not redraw a transcript (WP-H; the `useStableValue` rule, per pane).
  const linesKept = useRef(new Map<PaneNo, { key: string; value: ThinkingLine | null }>());
  const keepLine = (n: PaneNo, line: ThinkingLine | null): ThinkingLine | null => {
    const key = JSON.stringify(line);
    const had = linesKept.current.get(n);
    if (had && had.key === key) return had.value;
    linesKept.current.set(n, { key, value: line });
    return line;
  };

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
    return settingsNow.current ? [] : lay.fit.panes.filter((n) => lay.ws.panes[n - 1].view === "chat");
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
     parkKey: string | null = null, projectId: string | null = null): "box" | "held" | "parked" | "lost" => {
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
        dispatch({ type: "park", key: parkKey, text, files, projectId });
        return "parked";
      }
      dispatch({ type: "patch", patch: { error: "Words that were not sent had no chat to go back to: open a chat." } });
      return "lost";
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );
  // provider ▾ · model ▾ · effort ▾ in each chat pane's input bar (decisions 2026-10-06, A).
  // A crashed window sends nothing (PR #32): not even an effort move the
  // slider was settling when the workspace fell over (review of PR #30).
  const threadModel = useThreadModel(state, dispatch, () => void loadModels(), () => crashedRef.current);
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
  // The pane in slot 1: the chat the window is about when none was ever
  // selected. Never id 1, which a close may have taken away (WP-G2).
  const home: PaneNo = view.fit.panes[0] ?? orderOf(view.ws)[0];
  const activeNow: PaneNo = selected ?? state.selectedOrder[0] ?? home;
  useLayoutEffect(() => {
    if (live.current.home !== home) dispatch({ type: "home", pane: home });
  }, [home, dispatch]);
  useLayoutEffect(() => {
    const at = live.current;
    const front = focusedChat !== null && focusedChat === selected && at.selectedOrder[0] !== focusedChat;
    if (selected !== at.selectedChat || front) dispatch({ type: "select", pane: selected, front });
  }, [selected, focusedChat, state.selectedChat, state.selectedOrder, dispatch]);

  const pageDrafts = useRef(new CaptureDrafts());
  const pageDestination = useMemo<CaptureDestination>(() => ({
    target: () => {
      const pane = live.current.selectedChat;
      return pageDrafts.current.pin(live.current, pane !== null && chatsOnScreen().includes(pane) ? pane : null);
    },
    stage: (target, files) => {
      // Judge capacity against the actual commit state; acknowledge only the
      // intact pair so a refused or interrupted action cannot discard ink.
      flushSync(() => dispatch({ type: "stage", key: target.key, capture: target.compose, files, atomic: true }));
      const at = live.current;
      return [...PANE_NOS.map(n => at.chats[n].files), ...Object.values(at.drafts).map(d => d.files)]
        .some(staged => files.every(file => staged.includes(file)));
    },
    release: target => pageDrafts.current.release(target),
  }), [dispatch]);

  // ---- speech (V6) --------------------------------------------------------

  // Jarvis's voice: the event stream's prose, read aloud by this window if it
  // holds the speech lock (lib/speech). Output only; its own on/off switch, not
  // the mic's.
  const speech = useMemo(() => createSpeech(), []);
  useEffect(() => () => speech.dispose(), [speech]);
  const speechSubscribe = useCallback((cb: () => void) => speech.controller.subscribe(cb), [speech]);
  const speechView = useSyncExternalStore(speechSubscribe, () => speech.controller.view());
  // V6's blanket hold: the mic is deaf for the whole of a reply, between chunks included.
  // V9 replaces it with the self-voice gate (frames that are his own voice open nothing,
  // so the owner can talk over him) and keeps it as the fallback for whenever that gate
  // cannot run: no reference from the player, the owner's switch, an error in the gate.
  const speechPlaying = () => micSuppressedBySpeech(speech.controller.view()) && !speech.selfVoice.runnable();
  // A turn finished while its reply was still being read: the follow-up window
  // opens when the speech goes idle, not at `turn_finished` (it would run out,
  // or hear him, mid-reply).
  const followUpAfterSpeech = useRef(false);
  // The threads read now (lib/speech/policy `speakingThreads`): every thread
  // shown in any chat pane, hidden panes included, through the one queue. A
  // thread that leaves the set (its pane closed or changed) stops speaking at
  // once. A voice is given by the daemon only when a chat is first read
  // (`voiceFor`), so chats that are merely open use none up.
  const speakerKey = [...speakingThreads(view.ws, state.chats, pendingThread.current)].sort().join(",");
  useEffect(() => {
    const threads = speakerKey ? speakerKey.split(",") : [];
    speech.controller.setSpeakers(new Set(threads));
  }, [speech, speakerKey]);
  const voicesVersion = useSyncExternalStore(
    useCallback((cb: () => void) => speech.voices.subscribe(cb), [speech]), () => speech.voices.version());
  // The chats the owner muted from a pane's chip: silent like a thread in no pane.
  useEffect(() => { speech.controller.setMuted(speech.voices.muted()); }, [speech, voicesVersion]);

  // ---- barge-in and spoken commands (V9) -------------------------------------

  // The capture is made below; the barge-in's safety asks it whether a message is still being spoken.
  const captureRef = useRef<Capture | null>(null);
  // Talking over him pauses him, then he resumes (decision V-7): only the wake word and the talk key
  // do it, until a voice is enrolled (Q7). Output only: it reaches the speech controller's pause and resume.
  const barge = useMemo(() => new BargeIn({
    speech: speech.controller, clock: realClock,
    recording: () => !!captureRef.current && (captureRef.current.recording() || captureRef.current.ptt !== null),
  }), [speech]);
  // "Jarvis pause / resume / skip / stop": four calls on his voice and nothing else. Stop clears the
  // speech queue; it never interrupts an agent's turn (Q8), and nothing here can answer a card (V-6).
  const commands = useMemo(() => new CommandRouter({
    pause: () => speech.controller.pause(),
    resume: () => speech.controller.resume(),
    skip: () => speech.controller.skip(),
    stop: () => speech.controller.stop(),
  }), [speech]);

  // ---- capture ------------------------------------------------------------

  const voiceDiagnostics = useMemo(() => new VoiceDiagnostics(), []);
  const captureSuppression = (): SuppressionReason | null => {
    const at = live.current;
    if (at.orb === "speaking" || speechPlaying()) return "speaking";
    if (at.selectedChat === null) return "no_chat";
    if (at.approvals.length) return "card";
    if (at.picker !== null || chipOverlay.current) return "picker";
    if (settingsNow.current) return "settings";
    if (crashedRef.current) return "crash";
    return null;
  };
  // V3: listening on a chat that is running a turn borrows its orb and status
  // line; they come back when the capture ends (`endListening`).
  const listenWas = useRef<{ pane: PaneNo; status: string } | null>(null);
  // The selected chat when a follow-up window opened (Q5): a different one now
  // means the owner has chosen since, and the window no longer picks the pane.
  const followBase = useRef<PaneNo | null>(null);
  // The claimed messages in progress (V4), by Capture's utterance id. What
  // each is for is fixed when it is claimed: the chat it was said to (a
  // follow-up claim moves the selection, which itself only follows after the
  // next render) and whether its words may only wait in a box (the follow-up's
  // pane is gone). `gid` is its STT group, begun with its first chunk.
  const utterances = useRef(new Map<number, { gid: number | null; refused: boolean; meta: SttCapture }>());
  // Continuation windows whose message was placed (REVIEW box or chat); one
  // that never was (upload failed, nothing heard, refused) is taken back.
  const placedWindows = useRef(new Set<number>());
  const endListening = () => {
    const was = listenWas.current;
    listenWas.current = null;
    const at = live.current;
    // The claim may have begun on an idle pane whose turn started meanwhile
    // (a send landing while the next words were being said): then there is no
    // saved line, but the orb is still on "listening" for a running turn.
    const pane = was?.pane ?? at.selectedChat;
    if (pane === null) return;
    const c = at.chats[pane];
    if (!c.busy) return; // the turn finished meanwhile and set the pane itself
    if (!was && at.orb !== "listening") return; // nothing of ours to undo
    const status = c.status === LISTENING_STATUS ? (was?.status ?? "THINKING") : c.status;
    chatPatch(pane, { orb: turnOrb(at, c), status });
  };
  const capture = useMemo(
    () =>
      new Capture({
        // He does not answer himself, and never talks over a pending
        // authorization or an open picker; with no chat on screen there is
        // nobody to talk to. A running turn no longer silences the mic (V3,
        // decision V-10): what is said to a busy chat steers it.
        suppressed: () =>
          live.current.orb === "speaking" || speechPlaying() ||
          live.current.selectedChat === null ||
          live.current.approvals.length > 0 ||
          live.current.picker !== null ||
          chipOverlay.current || settingsNow.current ||
          crashedRef.current,
        suppressionReason: () => captureSuppression() ?? "speaking",
        // (A barge-in that came to nothing is over when its utterance ends -- `onUtteranceEnd`, which
        // every claimed utterance reaches exactly once, dropped or not.)
        onDrop: (reason, ms, source) => { voiceDiagnostics.add(ms, source, reason); },
        muted: () => isMuted(live.current.dictation),
        onLevel: (level) => dispatch({ type: "patch", patch: { level } }),
        onListening: (source, tag, utterance) => {
          const at = live.current;
          let n = at.selectedChat;
          let boxOnly = false;
          if (source === "follow_up" && typeof tag === "number" && tag !== n) {
            if (!chatsOnScreen().includes(tag as PaneNo)) {
              // The pane the window was opened for is gone or hidden: nobody
              // can say where these words belong, so they wait in the
              // selected chat's box, unsent.
              boxOnly = true;
            } else if (n === followBase.current) {
              // Q5: a follow-up opened by a drawn pane's reply belongs to that
              // pane, unless the owner chose another chat since (their click
              // is newer than the reply).
              layoutNow.current.focus(tag as PaneNo);
              dispatch({ type: "select", pane: tag as PaneNo, front: true });
              n = tag as PaneNo;
            }
          }
          // A talk key pressed over a hands-free message in progress carries on
          // the same one (the same id again): what it was claimed for stands.
          if (utterance !== undefined && !utterances.current.has(utterance)) {
            utterances.current.set(utterance, {
              gid: null, refused: false,
              meta: { mode: at.dictation, chat: n !== null ? draftKey(at.chats[n], n) : null, boxOnly, continuation: null, utterance },
            });
          }
          // The wake word and the talk key are the owner addressing him: they pause what he is saying (Q7).
          if (utterance !== undefined && (source === "wake" || source === "ptt")) barge.begin(source === "ptt" ? "key" : "wake", utterance);
          if (listenWas.current && listenWas.current.pane !== n) listenWas.current = null;
          if (n === null) return;
          // A turn's own orb and status come back when the listening is over.
          if (at.chats[n].busy && !listenWas.current) listenWas.current = { pane: n, status: at.chats[n].status };
          dispatch({ type: "chat", pane: n, patch: { orb: "listening", status: LISTENING_STATUS } });
        },
        onIdle: (note) => {
          const n = live.current.selectedChat;
          if (n !== null && live.current.chats[n].busy) { endListening(); return; }
          listenWas.current = null;
          if (n !== null) dispatch({ type: "chat", pane: n, patch: { orb: "idle", status: note || "" } });
          else dispatch({ type: "patch", patch: { orb: "idle" } });
        },
        onChunk: (chunk) => onChunk(chunk),
        onUtteranceEnd: (end) => onUtteranceEnd(end),
        onLimit: () => dispatch({ type: "patch", patch: { error: "That message reached the 15-minute limit and was sent as it stood. Keep talking to start another." } }),
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );
  captureRef.current = capture;

  /**
   * The follow-up window belongs to the pane whose reply opened it (Q5): a
   * pane the owner can see, the selected chat or another drawn one. A pane
   * that is not drawn opens none (hidden panes are silent and do not listen).
   * Today only the selected chat's turn ending calls this; a drawn pane's
   * *spoken* reply will (V7), when panes are read aloud.
   */
  const openFollowUpFor = useCallback((pane: PaneNo) => {
    if (pane !== live.current.selectedChat && !chatsOnScreen().includes(pane)) return;
    followBase.current = live.current.selectedChat;
    capture.openFollowUp(pane);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capture]);

  // A message continues only in the chat it went to: choosing another chat
  // ends the continuation window (the click is the owner's newer intent).
  // Its conversation changing (another thread opened in the pane) ends it
  // too -- but a compose row becoming its own just-opened thread does not.
  const voiceChat = state.selectedChat === null ? null : draftKey(state.chats[state.selectedChat], state.selectedChat);
  const voiceChatWas = useRef<{ pane: PaneNo | null; key: string | null }>({ pane: null, key: null });
  useEffect(() => {
    const was = voiceChatWas.current;
    voiceChatWas.current = { pane: state.selectedChat, key: voiceChat };
    const opened = state.selectedChat !== null ? live.current.chats[state.selectedChat].compose?.openedId : null;
    if (was.pane === state.selectedChat && was.key?.startsWith("compose:") && voiceChat !== null && voiceChat === opened) return;
    capture.closeContinuation();
  }, [state.selectedChat, voiceChat, capture]);

  // The follow-up window a finished turn owes, opened once its reply has been
  // read (v1's `doneSpeaking`): only if the selected chat is still idle.
  useEffect(() => speech.controller.onIdle(() => {
    if (!followUpAfterSpeech.current) return;
    followUpAfterSpeech.current = false;
    const at = live.current;
    if (at.selectedChat === null || at.chats[at.selectedChat].busy || at.approvals.length) return;
    if (isMuted(at.dictation)) return;
    openFollowUpFor(at.selectedChat);
  }), [speech, openFollowUpFor]);

  // Words spoken while no chat pane was on screen: they wait, unsent, for the
  // next chat selected (decisions W-6: ambiguous input goes to the selected
  // chat as it is when it is delivered, and there was none).
  const unplaced = useRef<string[]>([]);

  // V2: STT with deadlines, one automatic retry, kept clips for an explicit
  // Retry, and text placed in capture order (lib/stt.ts). The words go to the
  // selected chat as it is when each transcript lands (W-6).
  const sttSay = (p: ChatPatch) => {
    const n = live.current.selectedChat;
    if (n !== null) {
      // A chat running a turn keeps its own orb and status line: dictation
      // progress must not overwrite "working" (errors still show).
      if (live.current.chats[n].busy && p.orb !== "error") { const { orb: _o, status: _s, ...rest } = p; p = rest; }
      if (Object.keys(p).length) chatPatch(n, p);
    } else if (p.error !== undefined) dispatch({ type: "patch", patch: { error: p.error } });
  };
  const sttError = useRef("");
  const stt = useMemo(
    () =>
      new SttAssembler<SttCapture>({
        diagnostics: voiceDiagnostics,
        transcribe: (blob, o) => api.stt(blob, o),
        // The first attempt was judged by onChunk; a retry, automatic or
        // explicit, and a chunk that had to wait its turn, must not upload while
        // the mic is OFF, the window has crashed, or a card waits for an answer.
        canUpload: () => !crashedRef.current && !isMuted(live.current.dictation) && !live.current.approvals.length,
        maxInFlight: STT_MAX_IN_FLIGHT,
        place: (batch) => placeTranscripts(batch),
        onEvent: (e) => {
          // Chunks of a message still being spoken go out while the orb says LISTENING: it keeps saying so.
          if (e.kind === "busy") { if (!capture.recording()) sttSay({ orb: "transcribing", status: e.pending > 1 ? `TRANSCRIBING ${e.pending}` : "TRANSCRIBING" }); }
          else if (e.kind === "retrying") sttSay({ orb: "transcribing", status: "RETRYING" });
          else if (e.kind === "failed") {
            sttError.current = e.kept
              ? `Speech-to-text failed: ${e.message}. Your audio is kept: press Retry.`
              : `Speech-to-text failed: ${e.message}. The audio was too large to keep, so it cannot be retried.`;
            sttSay({ orb: "error", status: e.kept ? "STT FAILED · RETRY" : "STT FAILED", error: sttError.current });
          } else if (e.kind === "empty") sttSay({ orb: "idle", status: "DIDN'T CATCH THAT" });
          else if (e.kind === "blocked") {
            // On the window's error line as well: a busy chat's status line is
            // not ours to write, and a Retry that seems to do nothing is worse.
            sttSay({ status: e.message });
            dispatch({ type: "patch", patch: { error: "Not retried: the mic is OFF, the window is paused, or an authorization card is waiting. Fix that, then press Retry." } });
          }
        },
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [voiceDiagnostics],
  );

  // A chunk of a claimed message left the recorder (V4). Every chunk is judged
  // again here, as the whole utterance used to be: a crashed window, the mic
  // OFF or a waiting card uploads nothing, whatever Capture believed.
  const onChunk = useCallback(
    (chunk: UtteranceChunk) => {
      const u = utterances.current.get(chunk.utterance);
      if (!u || u.refused) return;
      const at = live.current;
      const refuse = (reason: DropReason) => { u.refused = true; voiceDiagnostics.add(chunk.ms, chunk.source, reason); };
      if (crashedRef.current) { refuse("suppressed:crash"); return; }
      // OFF never reaches here (Capture refuses to claim while muted), but the
      // mode is checked again rather than assumed: the fail-closed direction
      // costs a discarded chunk and the other costs an upload.
      if (outcomeFor(at.dictation) === "discard") { refuse("muted"); return; }
      if (at.approvals.length) { refuse("suppressed:card"); return; }
      if (!Number.isFinite(chunk.ms) || chunk.ms <= 0) { refuse("too_short"); return; }
      if (u.gid === null) u.gid = stt.beginGroup(chunk.source, u.meta);
      const id = stt.submit(chunk.wav, chunk.ms, chunk.source, u.meta, { group: u.gid, overlapMs: chunk.overlapMs });
      if (id === null) {
        sttSay({ orb: "error", status: "STT BACKLOG", error: "Too many clips are waiting to be transcribed, so part of this message was not kept. Wait for them to finish." });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch, stt],
  );

  // The message is over (sent whole, cut short, or never uploaded).
  const onUtteranceEnd = useCallback(
    (end: UtteranceEnd) => {
      const u = utterances.current.get(end.utterance);
      utterances.current.delete(end.utterance);
      endListening();
      // However it ends (sent, review, a command, empty, failed, crashed, never uploaded), the utterance
      // that paused him is over: he resumes what remains, unless a message went out (`placeTranscripts`
      // has said so by now) or a command decided otherwise.
      const finish = () => barge.end("dropped", end.utterance);
      // The message only keeps its continuation window if it goes out.
      const refuse = () => capture.cancelContinuation(end.continuation);
      if (!u || u.gid === null) { refuse(); finish(); return; }
      u.meta.continuation = end.continuation;
      stt.closeGroup(u.gid);
      // Resolve only when the message's fate is known (placed or sent, empty,
      // failed for good): a barge-in ends then (V-7), not when the first
      // upload merely starts.
      void stt.whenGroupSettled(u.gid).then(() => {
        if (end.continuation && !placedWindows.current.delete(end.continuation)) refuse();
        finish();
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch, stt],
  );

  // A batch of transcripts, in the order they were spoken. REVIEW never sends
  // on its own; AUTO sends only when nothing moved since the words were said
  // (see `placementFor`): a late or retried transcript, a different chat, a
  // busy chat whose turn is not its own thread's, or a waiting card all put
  // the words in the box instead. A busy chat's own turn is steered (V3).
  const placeTranscripts = useCallback(
    async (placed: SttPlaced<SttCapture>[]) => {
      const done = (outcome: "suppressed:crash" | "unplaced" | "review" | "handed_to_chat" | "command" | "suppressed:card" | "cancelled", items = placed) =>
        items.forEach((i) => voiceDiagnostics.finish(i.diag, outcome, i.latencyMs));
      // Whatever becomes of an utterance's words, the barge-in it began (V-7) hears of it: a message that went out
      // skips the message he was in the middle of; anything else lets him resume (`onUtteranceEnd` is the net).
      const bargeEnd = (outcome: "sent" | "boxed" | "dropped", items = placed) =>
        items.forEach((i) => { if (i.meta.utterance !== undefined) barge.end(outcome, i.meta.utterance); });
      if (sttError.current && live.current.error === sttError.current) dispatch({ type: "patch", patch: { error: "" } });
      sttError.current = "";
      // The window crashed while this was being transcribed: the words go
      // nowhere -- not to a chat the owner cannot see, not into a box.
      if (crashedRef.current) { done("suppressed:crash"); bargeEnd("dropped"); return; }
      // Spoken playback commands (V9): an utterance that is, as a whole, "Jarvis pause / resume / skip / stop"
      // is run on his voice and is never a message. Four calls on the speech controller and nothing else: not
      // an interrupt, not a send, not an approval. Under a card, or from a late or retried clip, it is not
      // obeyed (Q10: no command under a card without local recognition, which is V11).
      const batch: SttPlaced<SttCapture>[] = [];
      for (const i of placed) {
        const r = commands.fromTranscript(i.text, { card: live.current.approvals.length > 0, stale: i.late || i.retried });
        if (r.kind === "none") { batch.push(i); continue; }
        done(r.kind === "ran" ? "command" : r.why === "card" ? "suppressed:card" : "cancelled", [i]);
        bargeEnd("dropped", [i]);
      }
      if (!batch.length) return;
      const at = live.current;
      const n = at.selectedChat;
      if (n === null) {
        // No chat on screen: never sent anywhere. It waits for the next chat
        // selected, in its box, unsent -- and the window says so.
        done("unplaced", batch);
        bargeEnd("boxed", batch);
        unplaced.current.push(...batch.map((i) => i.text));
        dispatch({
          type: "patch",
          patch: { orb: "idle", error: "No chat is open, so what you said was not sent. It will be in the box of the next chat you select." },
        });
        return;
      }
      // The message goes out (to a box or a chat): its continuation window stays.
      for (const i of batch) if (i.meta.continuation) placedWindows.current.add(i.meta.continuation);
      const chat = at.chats[n];
      const landing = {
        mode: at.dictation, chat: draftKey(chat, n), busy: chat.busy, card: at.approvals.length > 0,
        // `send` steers exactly when the running turn is this thread's own
        // (the box offers Stop and Steer on the same test).
        steers: runningHere(chat),
      };
      // One spoken order: if any item must wait in the box, they all do.
      const sends = batch.every((i) => placementFor(i.meta, landing, i) === "send");
      if (!sends) {
        done("review", batch);
        bargeEnd("boxed", batch);
        // Words spoken in one chat that landed in another say so.
        const moved = batch.some((i) => i.meta.chat !== landing.chat);
        // A chat running a turn keeps its own orb and status line.
        chatPatch(n, { ...(chat.busy ? {} : { orb: "idle" as const, status: moved ? "SPOKEN IN ANOTHER CHAT · IN THE BOX" : "" }), pendingTranscript: batch.map((i) => i.text).join(" ") });
        if (moved && chat.busy) dispatch({ type: "patch", patch: { error: "What you said was spoken in another chat. It is in this chat's box, unsent." } });
        return;
      }
      // Dictated: the Discord mirror labels it "You (HUD, voice)" (PR C).
      done("handed_to_chat", batch);
      bargeEnd("sent", batch);
      await send(n, batch.map((i) => i.text).join(" "), [], true);
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
      if (!steering) sendStops.current.begin(local, threadId ?? compose?.openedId ?? null);
      // The thinking line counts from the owner's Enter (lib/thinking): now
      // when the thread is known, else once the first send has opened it. A
      // steer leaves the running turn's time as it is.
      const sentAt = Date.now();
      const known = threadId ?? compose?.openedId ?? null;
      if (known) dispatch({ type: "turn", event: "send", threadId: known, at: sentAt, by: local, steer: steering });
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
      // The thread this send opened, if it opened one.
      let opened: Thread | null = null;
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
            pageDrafts.current.opened(compose?.id, t.id);
            const n = conversation();
            threadId = t.id;
            opened = t;
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
        sendStops.current.bind(local, threadId);
        if (!known) dispatch({ type: "turn", event: "send", threadId, at: sentAt, by: local });
        const result = await api.send(threadId, {
          text, attachments: attachments.length ? attachments : undefined,
          ...(spoken ? { spoken: true } : {}),
        });
        const status = result?.status ?? "started";
        // A message went to this thread: the reply being read is stale (V-7).
        if (status !== "dropped") speech.controller.messageSent(threadId);
        if (status === "started" && result.turn_id) {
          dispatch({ type: "mark", local, patch: { turn_id: result.turn_id, mark: undefined } });
        }
        // Stop follows this acknowledged send by exact turn identity. A
        // delayed reply can never stop a successor, even if its SSE was missed.
        const deferredStop = sendStops.current.acknowledge(local, { ...result, status });
        if (deferredStop) void (async () => {
          if (!deferredStop.turnIds.length) {
            dispatch({ type: "patch", patch: { error: "Could not stop this send safely: the daemon returned no turn identity." } });
            return;
          }
          try {
            for (const id of deferredStop.turnIds) {
              const stopped = await api.interrupt(deferredStop.threadId, id);
              if (!stopped?.stale) break;
            }
          } catch (error: any) {
            dispatch({ type: "patch", patch: { error: `Could not stop: ${error?.message || "the request failed"}` } });
          }
        })();
        if (status === "dropped") {
          // The owner pressed Stop while this steer was on its way and the
          // turn would not take it: stop means stop, so it is not sent, and
          // its words go back to where they were typed (review of PR #22).
          dispatch({
            type: "mark", local,
            patch: { mark: "not sent", ...(result.message_id ? { message_id: result.message_id } : {}) },
          });
          giveBack(threadId, text, attachments, conversation());
          dispatch({ type: "turn", event: "send_failed", threadId, by: local });
          if (!steering) {
            onPane(turn, { busy: prior.busy, turnThreadId: prior.turnThreadId, orb: prior.orb, status: prior.status });
          }
          return true;
        }
        if (status === "steered" || status === "queued") {
          // Taken by the running turn, or waiting behind it: no turn of its
          // own will start for it, so its line entry is an ordinary one now.
          dispatch({ type: "turn", event: "sent", threadId, by: local });
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
        // A first send whose pane was closed while the thread opened (review
        // of PR #47, M2): nothing tracks or shows the turn now running, so it
        // is left loose — the pane that opens the thread next has its Stop.
        if (!steering && status === "started" && turn() === null) {
          dispatch({ type: "loose", threadId, status: "THINKING" });
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
        // Nothing started: the line this send drew goes (only if it is still
        // this send's — a turn heard starting meanwhile keeps it).
        if (threadId) dispatch({ type: "turn", event: "send_failed", threadId, by: local });
        // A first send whose compose row no pane holds any more (the owner
        // opened another thread there meanwhile): its words are parked under
        // that row, never put in a box showing another thread. A pane that
        // was closed meanwhile has no next new thread: they wait for the next
        // one anywhere (review of PR #47).
        const back = conversation();
        const { ws } = layoutNow.current;
        const draftPane = back ?? pane;
        const closed = !inWorkspace(ws, draftPane);
        const parkKey = !threadId ? (closed ? ORPHAN_DRAFT : `compose:${draftPane}`) : null;
        const went = giveBack(threadId, text, attachments, back, parkKey, compose?.projectId ?? null);
        // Held for a thread no drawn pane shows (a first send that failed at
        // `/send` after its pane closed): said, naming the thread, so the
        // words are found again (review 2 of PR #47).
        const named = (id: string) => {
          const title = live.current.threads.find((t) => t.id === id)?.title || (id === opened?.id ? opened.title : "");
          if (title) return `“${title}”`;
          const pid = compose?.projectId;
          const project = pid ? live.current.projects.find((p) => p.id === pid)?.name : null;
          return project ? `the new thread in ${project}` : "the new thread";
        };
        const error = `Could not send: ${e.message}`
          + (went === "parked"
            ? (closed ? " — what you wrote is kept for the next new thread you start."
              : " — what you wrote is kept until this pane shows a new thread again.")
            : went === "held" && threadId
              ? ` — what you wrote waits with ${named(threadId)}: it is back in the box when you open that thread.`
              : "");
        // A thread this send opened is listed, so the sidebar has it to open.
        if (opened && went === "held") void refreshThreads();
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
        sendStops.current.finish(local);
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
    if (sendStops.current.stop(turn)) return;
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
    // Turns closed panes left running (WP-G2) are checked the same way.
    const loose = Object.keys(live.current.loose);
    if (!PANE_NOS.some((n) => turns[n]) && !loose.length) return;
    let threads: any[];
    try {
      threads = await api.threads();
    } catch {
      return;
    }
    dispatch({ type: "patch", patch: { threads } });
    // A loose turn is over once its thread says so — or is no longer listed
    // (archived or deleted: `GET /threads` hides it, and the 15 s poll would
    // otherwise run for good; review of PR #47).
    for (const id of loose) {
      const record = threads.find((t) => t.id === id);
      if (!record || record.running === false) dispatch({ type: "turn_over", threadId: id });
    }
    for (const n of PANE_NOS) {
      const turn = turns[n];
      if (!turn) continue;
      const record = threads.find((t) => t.id === turn);
      const chat = live.current.chats[n];
      if (record && record.running === false && chat.busy && chat.turnThreadId === turn) {
        dispatch({ type: "tool_stop", pane: n, at: new Date().toISOString() });
        chatPatch(n, { busy: false, turnThreadId: null, orb: "idle", status: "" });
      }
    }
  }, [dispatch, chatPatch]);

  const anyBusy = PANE_NOS.some((n) => state.chats[n].busy) || Object.keys(state.loose).length > 0;
  useEffect(() => {
    if (!anyBusy) return;
    const timer = setInterval(() => void reconcileBusy(), 15_000);
    return () => clearInterval(timer);
  }, [anyBusy, reconcileBusy]);

  /**
   * A running turn no pane tracks is adopted (WP-H, plan H-3) by the drawn
   * chat pane showing its thread, when that pane waits on no turn of its own:
   * busy, tracking it, THINKING — so Stop and Enter-to-steer come back after a
   * reload, a pane closed and reopened, or a turn started from Discord before
   * the thread was opened. Only for a turn the window holds a live entry for
   * (lib/thinking) *and* the daemon calls working: the record that turns the
   * thread idle follows its `turn_finished`, and a finish heard first deleted
   * the entry, so a pane the finish just freed is never adopted back. The
   * 15 s reconcile undoes a wrong adoption (`running: false`).
   */
  useEffect(() => {
    const { ws, fit } = view;
    for (const n of fit.panes) {
      if (ws.panes[n - 1].view !== "chat") continue;
      const chat = state.chats[n];
      const t = chat.threadId;
      if (!t || chat.busy) continue;
      const turn = state.turns[t];
      if (!turn || turn.error === "ended" || state.activity.threads[t] !== "working") continue;
      if (PANE_NOS.some((m) => state.chats[m].turnThreadId === t)) continue;
      chatPatch(n, { busy: true, turnThreadId: t, status: "THINKING", ...(state.orb === "idle" ? { orb: "thinking" } : {}) });
    }
  }, [view, state.chats, state.turns, state.activity, state.orb, chatPatch]);

  // ---- events -------------------------------------------------------------

  useEffect(() => {
    const stop = subscribe((e: any) => {
      browser.onEvent(e);
      browserSound.event(e);
      const kind = e.kind;
      const data = e.data || e;
      try { speech.controller.event(e); } catch { /* speech never breaks the stream */ }
      if (devservers.event(e)) return;
      if (kind === "_connected") { devservers.connect(); void speech.voices.load(); }
      if (kind === "thread_voice") speech.voices.apply(data);
      if (kind === "_disconnected") { devservers.disconnect(); return; }
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
                  role: "user", text: ownerLine(data), via: data.via, at: e.at, turn_id: e.turn_id,
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
          // The thinking line's turn (lib/thinking): by thread, whichever
          // pane shows it — or none yet, for a turn started from Discord.
          if (tid) dispatch({ type: "turn", event: "started", threadId: tid, at: Date.now() });
          for (const n of mine) {
            chatPatch(n, {
              busy: true, orb: "thinking", draft: "", toolTurnId: e.turn_id ?? "",
              turnThreadId: at.chats[n].turnThreadId ?? tid ?? null,
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
          for (const n of mine) dispatch({ type: "settle", pane: n, text: data.text || "", at: e.at, turn_id: e.turn_id });
          break;
        case "tool_started": {
          // A running turn's write tool named a file (any thread, a task's
          // included): if that file is open and reloads in place, the toast
          // says who wrote it. Absolute paths only — a relative one is judged
          // from the thread's own folder, never matched by its tail.
          const cwd = tid ? at.threads.find((t) => t.id === tid)?.cwd : null;
          for (const p of writtenPaths(data.name, data.args, cwd)) buffers.noteTouch(p, writerOf(tid, at));
          for (const n of mine) {
            chatPatch(n, { status: `RUNNING · ${data.name || "tool"}`, orb: "tool" });
            dispatch({
              type: "op_start", pane: n,
              op: { call_id: data.call_id || String(Date.now()), name: data.name || "tool", started: Date.now(),
                turn_id: e.turn_id ?? at.chats[n].toolTurnId ?? "", label: data.label ?? "", at: e.at },
            });
          }
          break;
        }
        case "tool_finished":
          for (const n of mine) {
            chatPatch(n, { status: "THINKING", orb: "thinking" });
            dispatch({
              type: "op_done", pane: n,
              call_id: data.call_id || "",
              ok: data.ok !== false,
              summary: data.summary || "", turn_id: e.turn_id, outcome: data.outcome, at: e.at,
            });
          }
          break;
        case "turn_finished": {
          for (const n of mine) dispatch({ type: "tool_stop", pane: n,
            turn_id: e.turn_id ?? at.chats[n].toolTurnId ?? undefined, at: e.at ?? new Date().toISOString() });
          if (tid) {
            dispatch({
              type: "turn", event: "finished", threadId: tid, at: Date.now(),
              stop: typeof data.stop === "string" ? data.stop : undefined, next: Number(data.next) || 0,
            });
          }
          // A message waits behind this turn (`next`) and runs as the thread's
          // next turn at once: the pane stays on it — no flash of idle, no
          // follow-up mic window over a turn about to start. Its own
          // `turn_finished` (or the 15 s reconcile) frees the pane.
          if (data.next) break;
          // A turn a closed pane left running is over: no pane takes it up now.
          if (tid && tid in at.loose) dispatch({ type: "turn_over", threadId: tid });
          let followUp = false;
          for (const n of ours) {
            chatPatch(n, { busy: false, turnThreadId: null, orb: "idle", status: "" });
            if (n === at.selectedChat) followUp = true;
          }
          // Only the selected chat's own turn opens the follow-up window. A
          // Discord turn, or a turn in another pane, finishing must not start
          // the HUD listening.
          if (followUp) {
            if (speech.controller.view().active) followUpAfterSpeech.current = true;
            else openFollowUpFor(at.selectedChat!);
          }
          break;
        }
        case "error":
          // Shown, but the turn is not over until `turn_finished` (which the
          // daemon always sends): resetting `busy` here took the Stop button
          // away from a turn that was still running.
          for (const n of eitherPane(route)) chatPatch(n, { orb: "error", error: data.message || "error" });
          // The thread's line turns red, and stays red as "Ended on an error"
          // once the turn is over (the daemon ends a turn that said one `error`).
          if (tid) dispatch({ type: "turn", event: "error", threadId: tid, at: Date.now() });
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
          if (tid) for (const n of mine) dispatch({ type: "message", pane: n, message: { role: "system", text: data.text || "", at: e.at, turn_id: e.turn_id } });
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
        case "vex_device":
          // `{action, at}` only (VX-1): one this window did not start is said
          // in the VEX view (state/vex.ts).
          vex.heardDevice(data);
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
  // A task's question, answered from the task view: exactly `{index, text}`
  // (lib/taskQuestions). A refusal rejects with the daemon's sentence, which
  // the view shows under the box it was typed in, keeping the words — this
  // used to send `{question, answer}` and swallow the 400 (2026-10-11).
  const answerQuestion = useCallback(
    (taskId: string, index: number, text: string) =>
      api.answerTask(taskId, index, text).then(() => { void refreshTasks(); }),
    [refreshTasks],
  );
  // Every TaskView and transcript Cancel share this guard. A task can be
  // drawn twice; a second surface cannot race a pending action on the first.
  const taskActions = useRef(new Set<string>());
  const taskAction = useCallback(async (id: string, action: "steer" | "cancel" | "resume" | "start", text?: string) => {
    if (taskActions.current.has(id)) throw new Error("another action on this task is still pending");
    taskActions.current.add(id);
    try {
      if (action === "steer") await api.steerTask(id, text!);
      else if (action === "cancel") await api.cancelTask(id);
      else if (action === "resume") await api.resumeTask(id);
      else await api.startTask(id);
      // refreshTasks keeps the last snapshot if its read fails. An accepted
      // mutation is never described as refused because a later read failed.
      await refreshTasks();
    } finally { taskActions.current.delete(id); }
  }, [refreshTasks]);
  // Stable, so a memoized ChatTab is not redrawn by a keystroke.
  const cancelTask = useCallback(
    (id: string) => void taskAction(id, "cancel").catch((error: unknown) => {
      const why = error instanceof Error ? error.message : "the daemon could not be reached";
      dispatch({ type: "patch", patch: { error: `Could not cancel task ${id}: ${why}` } });
    }),
    [taskAction, dispatch],
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

  // ---- the editor --------------------------------------------------------------

  /**
   * "Add to chat" from a File pane (ED-2, the minimal half of ED-7's "Add
   * selection to chat"): the excerpt goes into the selected chat's box,
   * unsent (decisions W-6). With no chat on screen it is refused with a
   * sentence — never sent, never anywhere else. An env file never gets here
   * (FileTab asks `jarvisMayRead` first).
   */
  const addToChat = useCallback((text: string): string | null => {
    const at = live.current;
    const n = at.selectedChat;
    if (n === null) return "No chat is open to add it to.";
    const had = at.chats[n].input;
    dispatch({ type: "input", pane: n, text: had && !had.endsWith("\n") ? `${had}\n${text}` : had + text });
    return null;
  }, [dispatch]);

  // ---- placing a view in a pane ----------------------------------------------

  /**
   * A File pane holding an unsaved edit is never switched to another view
   * behind the owner's back — by its own tabs, a sidebar click, or a chat
   * landing in it (WP-B, review of PR #26): the switch is refused and the
   * pane says why. True when `pane` may show `next`.
   */
  /** The project pane `pane` is about (VX-2's strip and pin): see `stripProject`. */
  const stripProjectNow = (pane: PaneNo): string | null =>
    stripProject(live.current, layoutNow.current.ws.panes[pane - 1], pane);

  const mayShow = useCallback((pane: PaneNo, next: View): boolean => {
    const { ws, fit } = layoutNow.current;
    const spec = ws.panes[pane - 1];
    if (spec.view !== "file" || next === "file" || !dirtyNow(pane)) return true;
    const shownIn: PaneNo = fit.panes.includes(pane) ? pane : fit.panes.includes(ws.focused) ? ws.focused : fit.panes[0];
    setRefused((r) => ({ ...r, [shownIn]: { at: Date.now(), pane } }));
    return false;
  }, []);

  /**
   * The view strip's buttons and menu: switch a pane, unless that drops an
   * unsaved edit. Switched to the terminal view with no terminal of its own,
   * the pane opens one, as its `+` would, when none is open anywhere (the
   * owner's call, 2026-10-11; with some open it offers them, `+` first).
   */
  const setPaneView = useCallback((pane: PaneNo, next: View) => {
    if (!mayShow(pane, next)) return;
    const was = layoutNow.current.ws.panes[pane - 1];
    // The VEX view stays on the project it was opened for (VX-2): a pane
    // that followed the chat is pinned to the project it showed, so choosing
    // another chat never moves a build or a download to another robot. Read
    // before the switch: a VEX view itself never follows the chat (L7).
    const vexPin = next === "vex" && was.view !== "vex"
      && !(was.projectId && live.current.projects.some((p) => p.id === was.projectId))
      ? stripProjectNow(pane) : null;
    view.setView(pane, next);
    if (vexPin) view.pin(pane, vexPin);
    if (next === "terminal" && was.view !== "terminal" && !was.terminalId) {
      void terminals.ownerOpened(pane, () => {
        const now = layoutNow.current.ws.panes[pane - 1];
        return now.view === "terminal" && !now.terminalId;
      });
    }
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

  /**
   * "Open in Browser" from a terminal link (WP-E): the URL, judged with the
   * daemon's ports, goes to a drawn Browser pane — the focused one if it is
   * one — else the focused pane switches to Browser (the sidebar's click
   * rule, lib/workspace `show`), unless that pane holds an unsaved edit. The
   * pane adds and selects a tab (or selects its exact URL), judged again
   * there. Its previous page stays; this creates no frame-focus gesture.
   */
  const [browserFull, setBrowserFull] = useState<PaneNo | null>(null);

  // ---- the editor's navigation (ED-7) ------------------------------------------

  // What App asks of a File pane (open at a line, the search, a palette
  // command), acted on once by its nonce and cleared after the commit that
  // carried it, so a pane that mounts again later never acts on it twice.
  const [fileAsk, setFileAsk] = useState<Partial<Record<PaneNo, FileAsk>>>({});
  const fileSeq = useRef(0);
  const askFile = useCallback((n: PaneNo, ask: FileAskBody) => {
    const nonce = ++fileSeq.current;
    setFileAsk((a) => ({ ...a, [n]: { ...ask, nonce } as FileAsk }));
  }, []);
  useEffect(() => {
    if (Object.keys(fileAsk).length) setFileAsk({});
  }, [fileAsk]);
  const [nav, setNav] = useState<NavContext | null>(null);
  // Under a card quick open, the palette and the editor settings (ED-8) close
  // (editor plan §2.10); their keys are already inert there (`useLayout`).
  // Before paint, so the card is the first thing drawn without them.
  useLayoutEffect(() => {
    if (blocked && (state.picker === "quickOpen" || state.picker === "palette" || state.picker === "editorSettings")) {
      dispatch({ type: "patch", patch: { picker: null } });
      setNav(null);
    }
  }, [blocked, state.picker, dispatch]);

  /**
   * The explorer renamed `from` to `to` in `projectId` (FileTab; the buffers
   * have followed already): every pane's tabs follow — the project's own by
   * path, any other project reaching the same file through the registry's
   * holds — so an open tab keeps its edit under the new name.
   */
  const onRenamed = useCallback((projectId: string, from: string, to: string,
                                 moves: { pane: string; from: string; to: string }[],
                                 kept: { pane: string; path: string }[] = []) => {
    const { ws } = layoutNow.current;
    for (const n of PANE_NOS) {
      const spec = ws.panes[n - 1];
      const tabs = spec.editors;
      if (!tabs || !spec.projectId) continue;
      let next = tabs;
      // A tab whose buffer the registry kept in place (an unsaved edit
      // already at the new name) keeps its path.
      const keep = new Set(kept.filter((k) => k.pane === String(n)).map((k) => k.path));
      if (spec.projectId === projectId) next = renameTabs(next, from, to, keep);
      else for (const m of moves) if (m.pane === String(n)) next = renameTabs(next, m.from, m.to);
      if (!sameTabs(next, tabs)) view.setEditors(n, spec.projectId, next);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const openBrowser = useCallback((url: string): boolean => {
    const { status, ports } = daemonPortsNow();
    if (status === "pending") return false;
    const v = judgeBrowserUrl(url, ports);
    if (!v.ok) return false;
    const lay = layoutNow.current;
    const pane = showOf(lay.ws, "browser", lay.fit.panes).focused;
    if (!showView("browser")) return false;
    const tabs = lay.ws.panes[pane - 1].browser?.tabs ?? [];
    const same = tabs.find((t) => t.url === v.url);
    if (same) {
      cancelLoad(frameKey(pane, same.id));
      view.activateBrowserTab(pane, same.id);
    }
    else {
      if (tabs.length >= BROWSER_TABS_MAX) { setBrowserFull(pane); return false; }
      view.addBrowserTab(pane, v.url);
    }
    setBrowserFull(null);
    return true;
  }, [showView, view.activateBrowserTab, view.addBrowserTab]);

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
      const to = besideTarget(lay.ws, lay.fit.panes, dirtyIds(), holdingIds());
      if (to.source !== undefined) {
        // From one pane, or the right of two columns: a split, the thread's
        // chat in the new pane (WP-G2, plan §3.5).
        if (blockedNow.current) return;
        if (lay.ws.kept.includes(to.pane)) releasePane(to.pane, { keep: true });
        view.split(to.source, to.pane, { view: "chat", projectId: null, terminalId: null });
        openIn(to.pane, id);
        return;
      }
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

  // ---- split and close (WP-G2, plan §3.4–§3.6) --------------------------------

  /** The panes holding an unsaved edit, by id. */
  const dirtyIds = (): PaneNo[] => PANE_NOS.filter((n) => dirtyNow(n));
  /**
   * The panes ⊞ hid whose compose row holds unsent words, files or words
   * handed back: a split takes an empty hidden pane before one of these, and
   * when it must take one, its words are parked for the next new thread
   * (review of PR #47, H1) — a hidden pane cannot ask.
   */
  const holdingIds = (): PaneNo[] => {
    const { ws } = layoutNow.current;
    const chats = live.current.chats;
    return ws.kept.filter((n) => {
      const c = chats[n];
      return !!c.compose && !c.threadId && (!!c.input.trim() || c.files.length > 0 || c.restore.length > 0);
    });
  };
  const blockedNow = useRef(blocked);
  blockedNow.current = blocked;

  /**
   * A pane's conversation, terminal and notes let go of it: before × removes
   * it, and before a split takes back a pane ⊞ had hidden. Unsent words and
   * files in a thread's box are parked under the thread (they come back when
   * it opens again), words handed back to the box are held for the thread,
   * and a turn it tracked is left for the next pane that opens the thread —
   * its Stop goes there. A compose row's words are dropped after × asked, and
   * parked for the next new thread when a split takes a hidden pane back
   * (`keep`). A terminal it showed goes back to the panel, alive.
   */
  const releasePane = useCallback((n: PaneNo, opts: { keep?: boolean } = {}) => {
    const at = live.current;
    const spec = layoutNow.current.ws.panes[n - 1];
    const chat = at.chats[n];
    const thread = shownThread(chat, pendingThread.current[n]);
    if (thread) for (const r of chat.restore) heldBack.current.hold(thread, r.text, r.files);
    pendingThread.current[n] = null;
    // `keep`: a hidden pane a split takes back could not ask, so a compose
    // row's unsent words are parked for the next new thread, not dropped.
    dispatch({ type: "release_chat", pane: n, keep: !!opts.keep });
    if (spec.view === "terminal" && spec.terminalId && layoutNow.current.fit.panes.includes(n)) {
      terminals.releaseToPanel(spec.terminalId);
    }
    // Its tabs let go of their buffers; an edit only it held was given up
    // (× asked first; a pane a split takes back holds none).
    buffers.releasePane(n, { discard: true });
    setRefused((r) => {
      if (!r[n] && !Object.values(r).some((x) => x?.pane === n)) return r;
      const next = { ...r };
      delete next[n];
      for (const k of PANE_NOS) if (next[k]?.pane === n) delete next[k];
      return next;
    });
    setBrowserFull((full) => full === n ? null : full);
  }, [dispatch]);

  /**
   * What × asks before it closes (G-9): a File pane's unsaved edit only it
   * holds (ED-2: a file another pane also has open loses nothing), and a
   * compose row's unsent words — they belong to no thread, so they cannot be
   * parked. Words in a thread's box are parked, so that × does not ask.
   * The pane's conversation is judged **whatever it shows** (review 2 of
   * PR #47): switching a pane to File keeps its chat, words and all, and ×
   * releases that chat too — so the question, and the strip asking it, stay
   * while the words are there.
   */
  const closeQuestion = useCallback((n: PaneNo): string | null => {
    const spec = layoutNow.current.ws.panes[n - 1];
    const losing = spec.view === "file" ? buffers.paneLosing(n) : [];
    const chat = live.current.chats[n];
    const unsent = !shownThread(chat, pendingThread.current[n])
      && (!!chat.input.trim() || chat.files.length > 0 || chat.restore.length > 0);
    if (losing.length) {
      const names = losing.map((b) => b.path).join(", ");
      const edits = `${names} ${losing.length === 1 ? "has an" : "have"} unsaved edit${losing.length === 1 ? "" : "s"}`;
      // Save keeps the edits; the words go with the pane either way, and say so.
      if (unsent) return `${edits}, and this pane's new thread an unsent message, which closing loses. Save before closing?`;
      return `${edits}. Save before closing?`;
    }
    return unsent ? "Discard the unsent message?" : null;
  }, []);

  /**
   * × (plan §3.6): the pane lets go of what it held (`releasePane`), leaves
   * the workspace, and focus goes to the pane now in its slot — the pane
   * itself, never its box, so a held Enter cannot send what is typed there.
   * The last pane is never closed, nor anything under a card.
   */
  const closePane = useCallback((n: PaneNo) => {
    if (blockedNow.current) return;
    const lay = layoutNow.current;
    const at = live.current;
    // Focus goes where the mic already is (W-6: the chat the owner selected
    // last): the selected chat when it is another drawn pane — closing a File
    // pane must not hand the mic to the chat that slides into its slot
    // (review of PR #47, M1) — else, the selected chat closing, the chat
    // selected before it; else the pane now in that slot.
    const drawnChat = (k: PaneNo | null): k is PaneNo =>
      k !== null && k !== n && lay.fit.panes.includes(k) && lay.ws.panes[k - 1].view === "chat";
    const then = drawnChat(at.selectedChat) ? at.selectedChat
      : at.selectedChat === n ? at.selectedOrder.find(drawnChat) ?? null : null;
    const next = closePaneOf(lay.ws, n, then);
    if (next === lay.ws) return;
    releasePane(n);
    lay.close(n, then);
    requestAnimationFrame(() => {
      if (blockedNow.current) return;
      document.querySelector<HTMLElement>(`[data-testid="pane-${next.focused}"]`)?.focus({ preventScroll: true });
    });
  }, [releasePane]);

  /**
   * The split button (plan §3.4–§3.5): a copy of pane `source` right after
   * it, focused. A chat's copy is a new compose row in the source's project,
   * written before the layout changes; a terminal's is a new terminal in the
   * folder the source was opened in, unreadable to Jarvis if the source is.
   * Refused under a card, past eight, and when the next shape would not be
   * drawn whole. A File pane's copy holds the same buffers (ED-2), so one
   * holding an unsaved edit splits too.
   */
  const splitFrom = useCallback((source: PaneNo, opts: { focus?: boolean } = {}): PaneNo | null => {
    if (blockedNow.current) return null;
    const lay = layoutNow.current;
    const spec = lay.ws.panes[source - 1];
    const dirty = dirtyIds();
    if (!lay.splitVerdict(dirty).ok) return null;
    const id = allocate(lay.ws, dirty, holdingIds());
    if (id === null) return null;
    if (lay.ws.kept.includes(id)) releasePane(id, { keep: true });
    const copy = copySpec(spec);
    if (copy.view === "chat") {
      const at = live.current;
      const projectId = (spec.view === "chat" ? chatProjectId(at.chats[source], at.threads) : null)
        ?? lastProject(at.projects, at.threads, loadLastProject());
      pendingThread.current[id] = null;
      // A new thread the owner started, drawn once the split lands: it takes
      // up words no pane's row holds any more (`takeOrphan`).
      chatPatch(id, {
        threadId: null, compose: { projectId }, messages: [], loadedThread: null, draft: "", ops: [], status: "",
        takeOrphan: true,
      });
    }
    lay.split(source, id, copy, opts);
    if (spec.view === "terminal" && spec.terminalId) {
      const from = spec.terminalId;
      // Readable only when the source is known to be (a copy never widens
      // what Jarvis can read; an unknown source fails closed).
      const readable = terminals.readableOf(from);
      void terminals.create(terminals.specOf(from), id, undefined, { readable });
    }
    if (opts.focus !== false) focusPaneElement(id);
    return id;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [releasePane, chatPatch]);

  // ---- claimed local-server placement ------------------------------------
  const devSourceAt = (pane: PaneNo): DevSource | undefined => {
    const at = live.current, spec = layoutNow.current.ws.panes[pane - 1];
    if (spec.view === "terminal" && spec.terminalId) return { kind: "terminal", id: spec.terminalId };
    if (spec.view === "task" && at.taskId) return { kind: "task", id: at.taskId };
    const thread = spec.view === "chat" ? shownThread(at.chats[pane], pendingThread.current[pane]) : null;
    return thread ? { kind: "thread", id: thread } : undefined;
  };
  devservers.shows = source => !settingsNow.current && layoutNow.current.fit.panes.some(pane => {
    const at = devSourceAt(pane); return at && sourceKey(at) === sourceKey(source);
  });
  const devLabel = (row: PendingDev): string => {
    const at = live.current;
    const label = row.source.kind === "terminal" ? terminals.rows.find(t => t.id === row.source.id)?.title
      : row.source.kind === "task" ? at.tasks.find(t => t.id === row.source.id)?.brief
      : at.threads.find(t => t.id === row.source.id)?.title;
    return cleanTitle(label || (row.source.kind === "terminal" ? "Terminal" : row.source.kind === "task" ? "Task" : "Chat"), 60);
  };
  const devProject = (row: PendingDev): string | null => row.source.kind === "terminal"
    ? terminals.rows.find(t => t.id === row.source.id)?.project_id ?? null
    : row.source.kind === "task" ? live.current.tasks.find(t => t.id === row.source.id)?.project_id ?? null
    : live.current.threads.find(t => t.id === row.source.id)?.project_id ?? null;
  const devHeld = (): PaneNo[] => PANE_NOS.filter(n => {
    const c = live.current.chats[n];
    return dirtyNow(n) || c.busy || !!c.turnThreadId || !!c.threadId || !!c.compose?.openedId || !!c.input.trim()
      || c.files.length > 0 || c.restore.length > 0 || !!live.current.drafts["compose:" + n]
      || [...terminals.place.values()].includes(n);
  });
  const devTargets = (): DevTarget[] => layoutNow.current.fit.panes.map(n => {
    const spec = layoutNow.current.ws.panes[n - 1], c = live.current.chats[n];
    const dirty = spec.view === "file" && dirtyNow(n), chain = spec.view === "vex" && !!vex.chain;
    const privatePane = (spec as PaneSpec & { browserProfile?: string }).browserProfile === "private";
    const reason = dirty ? "has an unsaved edit" : chain ? "a VEX download is running"
      : privatePane ? "Private panes cannot load Direct URLs"
      : spec.view === "terminal" && terminals.rows.find(t => t.id === spec.terminalId)?.busy ? "its program keeps running in the panel"
      : spec.view === "chat" && (c.input || c.busy) ? "the chat keeps its words and its turn" : undefined;
    return { pane: n, slot: slotOf(layoutNow.current.ws, n), label: VIEW_LABEL[spec.view],
      disabled: dirty || chain || privatePane, reason, terminalId: spec.view === "terminal" ? spec.terminalId ?? undefined : undefined };
  });
  useEffect(() => {
    driving.retain(view.ws.panes.flatMap(p => p.browser?.tabs.flatMap(t => t.remoteTabId ? [t.remoteTabId] : []) ?? []));
  }, [view.ws]);
  const devAttempt = (row: PendingDev): DevAttempt => {
    const lay = layoutNow.current, ports = daemonPortsNow();
    if (crashedRef.current) return "drop";
    if (ports.status !== "ok") return "wait";
    const active = document.activeElement?.closest<HTMLElement>('[data-testid^="pane-"]');
    const activeMatch = /^pane-([1-8])$/.exec(active?.dataset.testid ?? "");
    const w: DevWindow = {
      enabled: devservers.enabled === true, blocked: blockedNow.current, covered: settingsNow.current,
      ports: ports.ports, controlPorts: ports.controlPorts,
      panes: PANE_NOS.map(n => {
        const spec = lay.ws.panes[n - 1];
        return { id: n, view: spec.view, source: devSourceAt(n), urls: spec.browser?.tabs.map(t => t.url) ?? [],
          tabCount: spec.browser?.tabs.length ?? 0, usedAt: devUsed.current[n],
          driving: driving.active(spec.browser?.tabs.find(t => t.id === spec.browser?.active)?.remoteTabId, Date.now()),
          browserProfile: (spec as PaneSpec & { browserProfile?: string }).browserProfile };
      }),
      drawn: lay.fit.panes, focused: lay.ws.focused, activePane: activeMatch ? Number(activeMatch[1]) as PaneNo : undefined,
      canSplit: lay.autoSplitVerdict(devHeld()).ok,
      pointerDown: devPointer.current.down, pointerReleasedAt: devPointer.current.released,
    };
    const result = autoOpenPlan(row, devPolicy.current, w, Date.now()), plan = result.plan;
    if (plan.kind === "wait") return "wait";
    if (plan.kind === "skip") return plan.why === "duplicate" ? "placed" : "drop";
    if (plan.kind === "prompt") { devPolicy.current = result.state; return "prompt"; }
    const auto = { kind: row.source.kind, label: devLabel(row) };
    const source = { kind: "dev_server" as const, entryId: row.id };
    if (plan.kind === "split") {
      const verdict = lay.autoSplitVerdict(devHeld());
      if (!verdict.ok || verdict.id === null) return "wait";
      const id = verdict.id, tab = { id: browserTabId(), url: plan.url, auto, source };
      if (lay.ws.kept.includes(id)) releasePane(id, { keep: true });
      lay.split(plan.source, id, { view: "browser", projectId: devProject(row), terminalId: null,
        browser: { tabs: [tab], active: tab.id }, browserUrl: plan.url }, { focus: false });
    } else {
      const tabs = lay.ws.panes[plan.pane - 1].browser?.tabs ?? [];
      if (tabs.length >= BROWSER_TABS_MAX) return "wait";
      const id = browserTabId(tabs.map(t => t.id));
      lay.addBrowserTab(plan.pane, plan.url, { id, auto, source, activate: plan.activate });
      if (!plan.activate && tabs.length) setDevUnseen(old => ({ ...old, [frameKey(plan.pane, id)]: new URL(plan.url).host }));
    }
    devPolicy.current = result.state;
    return "placed";
  };
  // Effects run after layout refs are current; the manager checks ownership
  // asynchronously, then calls the latest synchronous decision before mutation.
  const devAttemptNow = useRef(devAttempt); devAttemptNow.current = devAttempt;
  useEffect(() => { void devservers.attempt(row => devAttemptNow.current(row)); },
    [devVersion, view.ws, view.fit, blocked, settingsOpen]);
  useEffect(() => {
    setDevUnseen(old => {
      const next = { ...old };
      for (const key of Object.keys(next)) {
        const [paneText, id] = key.split(":"), pane = Number(paneText);
        const browser = view.ws.panes[pane - 1]?.browser;
        if (!browser?.tabs.some(t => t.id === id) || browser.active === id) delete next[key];
      }
      return Object.keys(next).length === Object.keys(old).length ? old : next;
    });
  }, [view.ws]);
  const chooseDevPane = (id: string, pane: PaneNo) => {
    void devservers.attempt(row => {
      if (blockedNow.current || settingsNow.current) return "wait";
      if (crashedRef.current || !devTargets().some(t => t.pane === pane && !t.disabled)) return "wait";
      const ports = daemonPortsNow(), lay = layoutNow.current;
      if (ports.status !== "ok") return "wait";
      const url = devUrl(row.url, ports.ports, ports.controlPorts);
      if (!url) return "drop";
      const old = lay.ws.panes[pane - 1];
      if (old.view === "terminal" && old.terminalId) terminals.releaseToPanel(old.terminalId);
      // An explicit owner pane choice replaces only that pane's Browser
      // contents; its chat/terminal/editor state remains in the normal stores.
      for (const tab of old.browser?.tabs ?? []) lay.closeBrowserTab(pane, tab.id);
      lay.addBrowserTab(pane, url, { auto: { kind: row.source.kind, label: devLabel(row) },
        source: { kind: "dev_server", entryId: row.id } });
      lay.setView(pane, "browser");
      return "placed";
    }, id);
  };

  /**
   * **Where a file opens**, the one rule for every way one is opened — ED-7's
   * quick open, find in files and palette, VX-2's build-error click: a drawn
   * File pane on that project (`from` itself first, when it is one); else a
   * new pane beside `from` (a split: what `from` shows stays — a VEX view is
   * never replaced, nor a terminal, a task or a preview); else `from` itself,
   * **only when it is a File or chat pane** (and `mayShow` lets it go). Null
   * when there is nowhere: the caller says so. The pane is pinned to
   * `projectId`; never under a card.
   */
  const placeFile = useCallback((from: PaneNo, projectId: string): PaneNo | null => {
    if (blockedNow.current || crashedRef.current) return null;
    const lay = layoutNow.current;
    const at = live.current;
    const projectOf = (n: PaneNo) => stripProject(at, lay.ws.panes[n - 1], n);
    const fileOn = (n: PaneNo) => lay.ws.panes[n - 1].view === "file" && projectOf(n) === projectId;
    if (lay.fit.panes.includes(from) && fileOn(from)) return from;
    const drawn = lay.fit.panes.find(fileOn);
    if (drawn !== undefined) return drawn;
    const id = splitFrom(from, { focus: false });
    if (id !== null) {
      view.setEditors(id, projectId, null);
      view.setView(id, "file");
      return id;
    }
    const v = lay.ws.panes[from - 1].view;
    if ((v === "file" || v === "chat") && mayShow(from, "file")) {
      if (lay.ws.panes[from - 1].projectId !== projectId) view.pin(from, projectId);
      view.setView(from, "file");
      return from;
    }
    return null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [splitFrom, mayShow]);

  const openAt = useCallback((from: PaneNo, projectId: string, path: string, line: number, col: number) => {
    if (blockedNow.current || crashedRef.current) return;
    const target = placeFile(from, projectId);
    if (target === null) {
      dispatch({ type: "patch", patch: { error: `No room to open ${path} beside this view: close a pane, or switch one to the editor.` } });
      return;
    }
    // The shared FileAsk path pins, admits the tab and reveals only once its
    // actual model is ready. It owns capacity refusal and stale cancellation.
    askFile(target, { kind: "open", path, line, col });
    focusPaneElement(target);
  }, [placeFile, askFile]);

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
      if (how === "deleted") buffers.projectDeleted(id);
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
        // A re-aim, in drawn and hidden panes alike, is not a new thread the
        // owner started: it never takes up parked words (no `takeOrphan`).
        chatPatch(n, {
          threadId: after.threadId, compose: after.compose, messages: [], loadedThread: null, draft: "", ops: [], status,
        });
      }
      // The task view it showed is empty now: the status line says why.
      if (taskGone && !displaced) chatPatch(activePane(at), { status });
      // A File or Preview pane pinned to it follows the chat again — except
      // the ones holding an unsaved edit no pane outside them holds, which
      // wait for the owner to discard it. Asked of the pinned panes together
      // (`panesHolding`): pane by pane, two panes sharing one edit each let
      // go and the edit lost every tab (review of PR #49, H2).
      const pinned = PANE_NOS.filter((n) => layoutNow.current.ws.panes[n - 1].projectId === id);
      view.unpinProject(id, buffers.panesHolding(pinned).map((k) => Number(k) as PaneNo));
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
        // As in `projectGone`: a re-aim never takes up parked words.
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
        // The owner started it, in a pane on screen: it takes up words no
        // pane's row holds any more.
        takeOrphan: true,
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
      // Claude Code opens on a new session — in every pane of the workspace
      // that shows chat (a free id gets one when it is drawn), and in the
      // pane the conversation is put on screen in. Nothing is sent until the
      // owner speaks.
      const lay = layoutNow.current;
      const pane = chatTarget(lay.ws, lay.fit.panes, live.current.selectedChat).pane;
      const projectId = lastProject(projects, threads, loadLastProject());
      for (const n of PANE_NOS) {
        if (n === pane || (inWorkspace(lay.ws, n) && lay.ws.panes[n - 1].view === "chat")) {
          dispatch({ type: "chat", pane: n, patch: { threadId: null, compose: { projectId } } });
        }
      }
      setBooted(true);
      // The conversation is on screen: in a drawn pane that already shows
      // chat, else in the focused pane — in the single layout, today's chat tab.
      // Never over a VEX view (review L5): a window with no chat drawn is
      // allowed (WP-B: speech waits, a press is refused with a sentence), and
      // a lone VEX pane at the lab must come back as itself after a reload.
      if (lay.ws.panes[pane - 1].view !== "vex" || lay.fit.panes.some((n) => lay.ws.panes[n - 1].view === "chat")) {
        placeChat(pane);
      }
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
  // worked in, as the window does. Only a pane in the workspace (drawn, or
  // hidden by ⊞): a pane × closed, or one no layout has drawn yet, holds no
  // conversation until it is drawn (review 3 of PR #47 — a closed pane was
  // given a row here at once). Never `takeOrphan`: words parked for the next
  // new thread wait for one the owner starts.
  const emptyChatPanes = PANE_NOS.filter(
    (n) => inWorkspace(view.ws, n) && view.ws.panes[n - 1].view === "chat" && isEmptyChat(state.chats[n]),
  ).join(",");
  useEffect(() => {
    if (!booted || !emptyChatPanes) return;
    const at = live.current;
    const { ws } = layoutNow.current;
    const projectId = lastProject(at.projects, at.threads, loadLastProject());
    for (const n of PANE_NOS) {
      if (inWorkspace(ws, n) && ws.panes[n - 1].view === "chat" && isEmptyChat(at.chats[n])) {
        chatPatch(n, { compose: { projectId } });
      }
    }
  }, [booted, emptyChatPanes, chatPatch]);

  // Failed first sends can return while their compose row is hidden. Take
  // them up only when that same row is actually drawn; its identity is checked
  // again by the reducer, and duplicate effects cannot consume it twice.
  const parkedCompose = (settingsOpen ? [] : view.fit.panes).filter(n => {
    const c = state.chats[n];
    return view.ws.panes[n - 1].view === "chat" && !c.threadId && c.compose?.id
      && !c.compose.openedId && state.drafts[`compose:${n}`];
  }).map(n => `${n}:${state.chats[n].compose!.id}`).join("|");
  useEffect(() => {
    if (!parkedCompose) return;
    for (const n of chatsOnScreen()) {
      const c = live.current.chats[n];
      if (c.compose?.id && !c.threadId && !c.compose.openedId && live.current.drafts[`compose:${n}`]) {
        dispatch({ type: "restore_compose", pane: n, composeId: c.compose.id });
      }
    }
    // The primitive key changes only for a drawn row with parked words.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parkedCompose, dispatch]);

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
    const land = (id: string, rows: Awaited<ReturnType<typeof api.transcript>>["messages"]) => {
      dispatch({ type: "transcript", threadId: id, rows });
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
  const onScreenThreads = (settingsOpen ? [] : view.fit.panes)
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
  const drawnViews = (settingsOpen ? [] : view.fit.panes).map((n) => view.ws.panes[n - 1].view);
  const taskShown = drawnViews.includes("task");
  const readThreads = (settingsOpen ? [] : view.fit.panes)
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
    if (live.current.picker || chipOverlay.current || settingsNow.current) return;
    capture.onWake();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capture]);

  // ---- push to talk -------------------------------------------------------

  const press = useCallback(() => {
    if (crashedRef.current) return; // nothing on screen to talk to
    if (live.current.approvals.length) return; // answer the authorization first
    if (live.current.picker || chipOverlay.current || settingsNow.current) return;
    // Push-to-talk and the orb act on the selected chat (decisions W-6): with
    // no chat pane on screen there is no turn to interrupt and nobody to
    // talk to, and the window says so rather than recording for nobody.
    const pane = live.current.selectedChat;
    if (pane === null) {
      dispatch({ type: "patch", patch: { error: "No chat is open to talk to: open a chat first." } });
      return;
    }
    // Push-to-talk no longer interrupts (V3, decision V-10): what is said to a
    // chat running a turn steers it, as typing does, and Stop is the interrupt.
    if (isMuted(live.current.dictation)) {
      chatPatch(pane, { status: "MIC MUTED" });
      return;
    }
    // Talking over him pauses him, and he resumes when the utterance is over
    // (decision V-7): `onListening` begins the barge-in, `onUtteranceEnd` ends it.
    capture.press();
  }, [capture, chatPatch, dispatch]);

  const release = useCallback(() => capture.release(), [capture]);

  // Space was push-to-talk until 2026-10-10. The first Space pressed outside a
  // typing target afterwards says, once per browser profile, what the key is
  // now (on the selected chat's line under the orb, for a few seconds); Space
  // itself does what it does on any page. Storage that throws says it once
  // per load.
  const hintSaid = useRef(false);
  const sayTalkKeyOnce = useCallback(() => {
    if (hintSaid.current) return;
    // Said where it is read — the selected chat's line — and never over a
    // turn's own status: with no chat on screen, or one busy, it waits.
    const pane = live.current.selectedChat;
    if (pane === null || live.current.chats[pane].busy) return;
    hintSaid.current = true;
    try {
      if (window.localStorage.getItem(SPACE_HINT_KEY)) return;
      window.localStorage.setItem(SPACE_HINT_KEY, "1");
    } catch {
      /* said once for this load */
    }
    chatPatch(pane, { status: SPACE_HINT });
    setTimeout(() => {
      if (live.current.chats[pane]?.status === SPACE_HINT) chatPatch(pane, { status: "" });
    }, 8000);
  }, [chatPatch]);
  // A card that arrives while the hint is up takes the line: a status reads
  // ahead of ANSWER THE AUTHORIZATION under the orb (`orbLine`), and a hint is
  // not worth holding the owner's attention off a card (review of PR #40).
  // Before paint, so the card's line is the first one drawn.
  useLayoutEffect(() => {
    if (!blocked) return;
    for (const n of PANE_NOS) {
      if (live.current.chats[n]?.status === SPACE_HINT) chatPatch(n, { status: "" });
    }
  }, [blocked, chatPatch]);

  // What the workspace's error boundary calls once it has caught a render
  // error: a paste going out to a shell stops (nothing on screen could show
  // its notice or Resume), and nothing in the microphone's hands is sent.
  const onCrash = useCallback(() => {
    crashedRef.current = true;
    terminals.abortPastes();
    vex.crash();
    capture.abandon();
    setCrashed(true);
  }, [capture]);

  // Push-to-talk's key is Ctrl+D (2026-10-10, lib/talkkey.ts): held to talk,
  // let go (D or Ctrl) to send. The capture phase on the window, so the chat
  // box, Monaco, a menu or a chip — each of which keeps its own keys from
  // bubbling — never stands between the owner and the mic, and nothing under
  // the cursor acts on the key: its default (Chrome's bookmark dialog) is
  // always prevented and it stops here, under a card, muted or crashed alike.
  // A focused terminal is the one exception: there it is the shell's end of
  // input, untouched.
  //
  // **A hold is one key, decided where it began** (review of PR #40;
  // `TalkHold` in lib/talkkey.ts, kept for the window's life so a re-run of
  // this effect cannot lose a hold): its auto-repeat keydowns stay the
  // window's when focus lands in a terminal mid-hold — a click into one, a
  // card answered by mouse, Discord or its timeout handing focus back, or a
  // Preview frame taking focus and giving it back — so the shell never gets
  // a run of end-of-input from it. A window that loses focus mid-hold may
  // never see the keyup, so the blur lets the mic go, but not the hold.
  const talkHold = useMemo(() => new TalkHold(), []);
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      const blocked = live.current.approvals.length > 0;
      if (wantsSpaceHint(e, { typing: isTypingTarget(e.target), blocked })) sayTalkKeyOnce();
      // **A pending Monaco chord's second key is Monaco's** (decisions E-10,
      // the window's one tracker in lib/editorkeys.ts, shared with the layout
      // keys): Ctrl+K then Ctrl+D in an editor is Monaco's chord, not
      // push-to-talk — taking it would leave the chord waiting to swallow the
      // next key typed. Its default is still kept from Chrome; Monaco hears it.
      const chord = isTalkKey(e) && !e.repeat && chordSecond(e, inMonaco(e.target));
      const verdict = talkHold.down(e, { terminal: inTerminal(e.target), blocked, chordSecond: chord });
      if (verdict === "pass") return;
      e.preventDefault();
      if (verdict === "monaco") return;
      e.stopPropagation();
      if (verdict === "press") press();
    };
    const up = (e: KeyboardEvent) => {
      if (talkHold.up(e)) release();
    };
    const lost = () => {
      if (talkHold.blur()) release();
    };
    window.addEventListener("keydown", down, true);
    window.addEventListener("keyup", up, true);
    window.addEventListener("blur", lost);
    return () => {
      window.removeEventListener("keydown", down, true);
      window.removeEventListener("keyup", up, true);
      window.removeEventListener("blur", lost);
    };
  }, [press, release, sayTalkKeyOnce, talkHold]);

  // ---- microphone ---------------------------------------------------------

  // V5: the ML speech detector. Between the microphone and the capture; the OFF
  // mode scores nothing (it has no frames posted while muted).
  const vadStage = useMemo(() => new VadStage({
    // Every frame is judged against what the window is playing (V9) before the capture sees it.
    deliver: (frame, p) => capture.onFrame(frame, p, speech.selfVoice.runnable() ? speech.selfVoice.push(frame) : null),
    active: () => !isMuted(live.current.dictation),
    onStatus: (s) => voiceDiagnostics.detector(s.detector, s.state, s.note, s.frameMs),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [capture, voiceDiagnostics, speech]);
  const [endPause, setEndPause] = useState(() => loadEndPause());
  useEffect(() => { capture.setEndPause(endPause); }, [capture, endPause]);

  // The live microphone input (V4): its frames are the seam V5's VAD worker and
  // V12's recorder read (`micInput.addFrameListener`).
  const micInput = useRef<MicInput | null>(null);
  useEffect(() => {
    let input: MicInput | null = null;
    let stream: MediaStream | null = null;
    let gone = false;
    (async () => {
      try {
        stream = await openMicrophone(constraints => navigator.mediaDevices.getUserMedia(constraints));
        let settings: unknown;
        try { settings = stream.getAudioTracks()[0]?.getSettings?.(); } catch { /* readback unavailable */ }
        voiceDiagnostics.microphone("available", settings);
        // An AudioWorklet resamples to 16 kHz mono in 512-sample frames (V4).
        // Without one the same resampler runs behind the old ScriptProcessor,
        // and the window says so rather than quietly capturing worse.
        // The frames go through the VAD stage (V5): Silero scores each in a worker and
        // the stage hands it on, in order, to the capture; until the worker is ready,
        // or if it cannot run, the capture judges by energy and the diagnostics say so.
        vadStage.start();
        input = await startMicInput({ stream, onFrame: (frame) => vadStage.push(frame), workletUrl: micWorkletUrl });
        if (gone) { input.stop(); vadStage.stop(); return; }
        micInput.current = input;
        voiceDiagnostics.capturePath(input.path, input.fallbackReason);
        if (input.path !== "worklet") {
          dispatch({ type: "patch", patch: { error: `The microphone is using the older capture path: ${input.fallbackReason}. Dictation works, but audio is read on the main thread and may drop frames when the window is busy.` } });
        }
      } catch {
        voiceDiagnostics.microphone("unavailable");
        // A window without a mic grant still types; every other affordance
        // stays exactly as it was.
      }
    })();
    return () => {
      gone = true;
      micInput.current = null;
      stream?.getTracks().forEach((t) => t.stop());
      input?.stop();
      vadStage.stop();
    };
  }, [capture, vadStage]);

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
      followUpFor: (pane: PaneNo) => openFollowUpFor(pane),
      voiceDiagnostics: () => voiceDiagnostics.snapshot(),
      // Jarvis's voice, read-only: the strip's view, the queue's live and past
      // items (text, thread, state), and whether this window holds the lock.
      speech: () => ({
        view: speech.controller.view(), ...speech.controller.items(), speaker: speech.gate.isSpeaker(),
        barge: barge.held(), selfVoice: speech.selfVoice.status(),
        voices: Object.fromEntries(PANE_NOS.map((n) => shownThread(live.current.chats[n], pendingThread.current[n]))
          .filter((id): id is string => !!id).map((id) => [id, speech.voices.info(id)])),
      }),
      mic: {
        feedMs: (ms: number, amp: number, frame?: number, prob?: number | null) => capture.feedMs(ms, amp, frame, prob),
        path: () => voiceDiagnostics.snapshot().path,
        // Frame listeners, as V5/V12 will register them: read-only audio, 512 samples at 16 kHz.
        listen: (fn: (frame: Float32Array) => void) => micInput.current?.addFrameListener(fn) ?? (() => {}),
        wake: () => onWakeHit(),
        // One frame through the stage, the gate and the capture, as the microphone's would go (V9 tests).
        frame: (samples: ArrayLike<number>) => vadStage.push(Float32Array.from(samples)),
        // The ML VAD stage (V5): what it is, and (for tests) a way to start it again.
        detector: () => vadStage.status(),
        retryDetector: () => vadStage.retry(),
        endPause: () => capture.endPauseMs,
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
      // The thinking lines' one clock (WP-H): how many drawn lines tick, and
      // whether its interval runs at all. Read-only.
      toolGroups: () => toolGroups.keys(),
      thinkingClock: () => ({ subscribers: thinkingClock.subscribers(), running: thinkingClock.running() }),
      // A split the owner did not click (WP-G2's `{focus: false}`, for the
      // browser's dev-server pane): the id it took, or null when refused.
      split: (source: PaneNo, opts: { focus?: boolean } = {}) => splitFrom(source, opts),
      // Monaco, read-only (lib/monaco.ts): live model URIs, live editors, and
      // the language the registry gives a path.
      monacoModels,
      monacoEditors,
      monacoLanguage,
      // The editor's buffers (ED-2), read-only: never any text.
      buffers: () => buffers.snapshot(),
      // The editor settings (ED-8), read-only: stored, in force, and each File editor's own.
      editorSettings: () => editorSettingsProbe(),
    };
  }, [capture, dispatch, onWakeHit, reconcileBusy, splitFrom]);

  // ---- actions ------------------------------------------------------------

  const setMode = (mode: DictationMode) => {
    const wasMuted = isMuted(live.current.dictation);
    saveMode(mode);
    dispatch({ type: "patch", patch: { dictation: mode } });
    if (live.current.selectedChat !== null) chatPatch(live.current.selectedChat, { status: "" });
    if (wasMuted && !isMuted(mode)) capture.markUnmute();
    if (isMuted(mode)) { capture.closeFollowUp(); followUpAfterSpeech.current = false; }
    // The mic's OFF no longer mutes Jarvis (lib/speech/policy, Q11).
    onMicModeChange(isMuted(mode), (m) => api.mute(m).catch(() => {}));
  };

  const decide = async (reqId: string, allow: boolean, always = false) => {
    const current = live.current.approvals.find(r => r.req_id === reqId);
    if (current?.hud_only && (always || (allow && live.current.approvals[0]?.req_id !== reqId))) return;
    if (current?.hud_only) always = false;
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
  // The strip names the chat being read, which in several panes is not the selected one.
  const speechThreadId = speechView.thread;
  const speechThread = speechThreadId ? state.threads.find((t) => t.id === speechThreadId) ?? null : null;
  const speechLabel = speechThread ? threadLabel(speechThread).text : "";
  const speaking = speechView.enabled && speechView.speaker;
  const orbStatus = orbLine({
    status: selected !== null ? state.chats[selected].status : "",
    mode: state.dictation,
    approvals: state.approvals.length,
    orb: state.orb,
    speech: speaking ? { state: speechView.state, label: speechLabel } : undefined,
  });
  // Playing, the orb shows his voice unless it carries a meaning of its own
  // (a tool, a card, an error) or the mic is mid-utterance.
  const orbShown = speaking && (speechView.state === "playing" || speechView.state === "buffering") && (state.orb === "idle" || state.orb === "thinking" || state.orb === "composing")
    ? "speaking" : state.orb;
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
  // The File panes pinned to a project that went away which hold an unsaved
  // edit no pane outside that project's panes holds: they stay on it, with
  // the gone prompt. Asked of the panes on the project together — two panes
  // sharing the edit each hold a view another pane also shows, so a per-pane
  // last-view rule let both go and the edit lost its every tab (review of PR
  // #49, H2).
  const goneHeld: Partial<Record<PaneNo, boolean>> = {};
  for (const pid of new Set(ws.panes.filter(pinGone).map((p) => p.projectId as string))) {
    const on = PANE_NOS.filter((k) => ws.panes[k - 1].projectId === pid);
    for (const k of buffers.panesHolding(on)) goneHeld[Number(k) as PaneNo] = true;
  }
  /**
   * A File or Preview pane's project: its pin while that project exists, else
   * the chat's. A File pane whose pinned project went away while it held an
   * unsaved edit keeps it — FileTab is keyed by this, so following the chat
   * would remount it and drop the edit; the header asks first instead.
   */
  const paneProject = (spec: PaneSpec, pane: PaneNo): string | null =>
    spec.projectId && (!pinGone(spec) || (spec.view === "file" && goneHeld[pane]))
      ? spec.projectId : activeProjectId;
  const pinnedElsewhere = (spec: PaneSpec, pane: PaneNo) =>
    (spec.view === "file" || spec.view === "browser") && paneProject(spec, pane) !== activeProjectId;

  // ---- quick open, the palette, find in files (ED-7) ----------------------------

  /**
   * What the editor's navigation is about: the focused pane when it is a File
   * pane (its project); else the focused pane's own project (a chat's
   * conversation's, a VEX view's pin) and a drawn File pane on it, if there
   * is one; else nothing is decided until something is opened — then
   * `placeFile` decides (a split beside the focused pane, which is never
   * replaced unless it is a File or chat pane).
   */
  const fileTarget = (): { pane: PaneNo; projectId: string | null; switchTo: boolean } => {
    const f = focusedDrawn;
    const spec = ws.panes[f - 1];
    if (spec.view === "file") return { pane: f, projectId: paneProject(spec, f), switchTo: false };
    const projectId = stripProjectNow(f) ?? (spec.view === "vex" ? null : activeProjectId);
    const drawn = fit.panes.find((n) => ws.panes[n - 1].view === "file" && paneProject(ws.panes[n - 1], n) === projectId);
    if (drawn !== undefined && projectId) return { pane: drawn, projectId, switchTo: false };
    return { pane: f, projectId, switchTo: true };
  };
  const activePathOf = (n: PaneNo): string | null => {
    const spec = ws.panes[n - 1];
    return spec.view === "file" ? spec.editors?.active ?? null : null;
  };
  /** The File pane to act in, placed by the one rule (`placeFile`), focused; null when there is none. */
  const toFilePane = (t: { pane: PaneNo; projectId: string | null; switchTo: boolean }): PaneNo | null => {
    if (!t.projectId) {
      patch({ error: "Pick a project first: the editor works in a project." });
      return null;
    }
    const n = t.switchTo ? placeFile(t.pane, t.projectId) : t.pane;
    if (n === null) {
      patch({ error: "No room to open a file beside this view: close a pane, or switch one to the editor." });
      return null;
    }
    view.focus(n);
    return n;
  };
  /**
   * What a Diff pane shows (lib/scm `diffMode`): the task a drawn Task pane
   * shows (a Diff pane never shows another), else the task the window is
   * about, else its pinned project's uncommitted changes, else the chat's.
   */
  // A drawn Task pane shows the window's task (`taskShown`, above): a Diff pane shows that one.
  const taskInPane = !!task && taskShown;
  const diffModeOf = (spec: PaneSpec): DiffMode => diffMode({
    pin: spec.projectId, projectIds: state.projects.map((p) => p.id),
    taskId: state.taskId, taskFocus: state.taskFocus, taskShown: taskInPane, taskIds: state.tasks.map((t) => t.id),
    follow: activeProjectId,
  });
  const diffHeading = (mode: DiffMode): string =>
    mode.kind === "project" ? `Uncommitted changes in ${projectName(mode.projectId)}`
      : mode.kind === "task" ? `Task ${mode.taskId.slice(0, 6)}${task?.brief ? ` · ${task.brief}` : ""}` : "";
  /** "Open" on a Diff pane's changed file: where `placeFile` says — never over the Diff or a VEX view. */
  const openFromDiff = (n: PaneNo, projectId: string, path: string) => {
    if (blockedNow.current || crashedRef.current) return;
    const pane = toFilePane({ pane: n, projectId, switchTo: true });
    if (pane !== null) askFile(pane, { kind: "open", path });
  };
  /** Another dialog is open, or the window crashed: ED-7's keys do nothing. */
  const closeSettings = useCallback(() => {
    if (live.current.approvals.length) return;
    settingsNow.current = false;
    setSettingsOpen(false);
    requestAnimationFrame(() => {
      const back = pageSettingsReturn.current;
      if (!settingsNow.current && !live.current.approvals.length && back?.isConnected
          && !back.closest("[inert]")) {
        allowRestore(back);
        back.focus({ preventScroll: true });
      }
    });
  }, []);
  settingsKeyRef.current = () => {
    if (live.current.approvals.length || live.current.picker || chipOverlay.current || crashedRef.current) return;
    if (settingsNow.current) closeSettings();
    else {
      pageSettingsReturn.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      settingsNow.current = true;
      setSettingsOpen(true);
    }
  };
  const chooseSettingsSection = (section: SettingsSection) => {
    if (blocked || threadModel.overlayOpen) return;
    setSettingsSection(section);
    saveSettingsSection(section);
  };

  const navBusy = () => settingsNow.current || !!live.current.picker || threadModel.overlayOpen || crashedRef.current
    || live.current.approvals.length > 0;
  const openNav = (kind: "quickOpen" | "palette") => {
    if (navBusy()) return;
    const t = fileTarget();
    if (kind === "quickOpen" && !t.projectId) {
      patch({ error: "Pick a project first: quick open lists a project's files." });
      return;
    }
    const editor = focusedEditor();
    const focused = !!editor && !!editor.hasTextFocus?.();
    let actions: EditorAction[] = [];
    if (kind === "palette" && focused) {
      try {
        actions = (editor.getSupportedActions?.() ?? [])
          .filter((a: any) => a.isSupported?.() !== false)
          .map((a: any) => ({ id: String(a.id), label: String(a.label || "") }));
      } catch {
        actions = [];
      }
    }
    const activePath = t.switchTo ? null : activePathOf(t.pane);
    setNav({
      ...t, activePath, editor: focused ? editor : null, actions,
      ctx: { filePane: !t.switchTo, activeFile: !!activePath, editor: focused },
      returnTo: document.activeElement instanceof HTMLElement ? document.activeElement : null,
    });
    patch({ picker: kind });
  };
  /** The editor settings (ED-8), from a File pane's ⚙ or the palette: never over another dialog or a card. */
  const settingsReturn = useRef<HTMLElement | null>(null);
  const openEditorSettings = () => {
    if (navBusy()) return;
    settingsReturn.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    patch({ picker: "editorSettings" });
  };
  const closeEditorSettings = () => {
    const back = settingsReturn.current;
    settingsReturn.current = null;
    patch({ picker: null });
    if (back && back.isConnected && !live.current.approvals.length) requestAnimationFrame(() => back.focus());
  };
  const closeNav = (refocus: boolean) => {
    const back = nav?.returnTo;
    patch({ picker: null });
    setNav(null);
    if (refocus && back && back.isConnected && !live.current.approvals.length) requestAnimationFrame(() => back.focus());
  };
  const openFindInFiles = () => {
    if (navBusy()) return;
    const t = fileTarget();
    // The selection, when it is one short line of a file Jarvis may read: an
    // env file's text goes in no request but its own save.
    let query: string | undefined;
    const editor = focusedEditor();
    if (editor?.hasTextFocus?.()) {
      const model = editor.getModel?.();
      const sel = editor.getSelection?.();
      if (model && sel && !sel.isEmpty() && sel.startLineNumber === sel.endLineNumber
          && model.getLanguageId?.() !== ENV_LANGUAGE) {
        const text = model.getValueInRange(sel);
        if (text.length <= 200) query = text;
      }
    }
    const pane = toFilePane(t);
    if (pane === null) return;
    askFile(pane, { kind: "search", query });
  };
  editorKeyRef.current = (what) => {
    if (what === "findInFiles") openFindInFiles();
    else openNav(what);
  };
  /** A palette entry: a HUD command, or one of Monaco's actions in the editor it was opened from. */
  const runEntry = (key: string) => {
    const at = nav;
    closeNav(false);
    if (!at || live.current.approvals.length) return;
    if (key.startsWith("monaco:")) {
      const ed = at.editor;
      const id = key.slice("monaco:".length);
      if (!ed) return;
      requestAnimationFrame(() => {
        if (live.current.approvals.length) return;
        ed.focus?.();
        void ed.getAction?.(id)?.run?.();
      });
      return;
    }
    const id: CommandId | null = commandOf(key);
    if (!id) return;
    const later = (f: () => void) => requestAnimationFrame(() => {
      if (!live.current.approvals.length) f();
    });
    const toPane = (kind: FileAskBody["kind"]) => {
      if (!at.ctx.filePane) return;
      view.focus(at.pane);
      askFile(at.pane, { kind } as FileAskBody);
    };
    switch (id) {
      case "file.quickOpen":
        return later(() => editorKeyRef.current("quickOpen"));
      case "search.findInFiles":
        return later(() => editorKeyRef.current("findInFiles"));
      case "file.newFile":
        return toPane("newFile");
      case "file.newFolder":
        return toPane("newFolder");
      case "file.save":
        return toPane("save");
      case "file.reload":
        return toPane("reload");
      case "file.closeTab":
        return toPane("closeTab");
      case "file.nextTab":
        return toPane("nextTab");
      case "file.prevTab":
        return toPane("prevTab");
      case "file.copyPath":
        return toPane("copyPath");
      case "chat.addSelection":
        return toPane("addToChat");
      case "file.saveAll":
        void buffers.saveAll();
        return;
      case "chat.newThread":
        newThread();
        return;
      case "view.files":
        toFilePane(fileTarget());
        return;
      case "view.toggleSidebar":
        return view.toggle("left");
      case "view.toggleRight":
        return view.toggle("right");
      case "view.togglePanel":
        return view.ownerTogglePanel();
      case "view.zoomIn":
        return view.zoomBy(1);
      case "view.zoomOut":
        return view.zoomBy(-1);
      case "view.zoomReset":
        return view.setZoom(100);
      case "view.editorSettings":
        return later(openEditorSettings);
      case "project.newVex":
        // VX-3: the dialog only; nothing is made until the owner approves its card.
        return later(() => {
          if (!live.current.picker) patch({ picker: "newVexProject" });
        });
    }
  };
  const paletteList: Entry[] = nav && state.picker === "palette" ? paletteEntries(nav.ctx, nav.actions) : [];

  /** One chat pane: its conversation, its box, its chips, its Send/Steer and Stop. */
  const renderChat = (n: PaneNo) => {
    const chat = state.chats[n];
    const thread = threadOf(chat.threadId);
    const target = n === selected;
    // Another chat pane's status line says only what its own turn is doing;
    // the selected chat's is on the orb — here only while the orb is folded.
    const status = !target ? chat.status : drawn.leftFolded ? orbStatus : null;
    const compose = chat.compose;
    // The foot of this chat while its thread's turn runs (WP-H): read from the
    // window's turns by thread, so it shows for a turn this pane did not start
    // — from Discord, before a reload, in a pane closed and reopened.
    const shown = shownThread(chat);
    const thinking = keepLine(n, thinkingLine({
      chat,
      threadId: shown,
      turn: shown ? state.turns[shown] : undefined,
      activity: shown ? state.activity.threads[shown] : undefined,
      approvalHere: !!shown && state.approvals.some((r) => r.thread_id === shown),
    }));
    return (
      <>
        <ChatTab
          thread={thread}
          messages={chat.messages}
          draft={chat.draft}
          toolTurnId={chat.toolTurnId}
          busy={chat.busy}
          onCancelTask={cancelTask}
          thinking={thinking}
          accent={state.avatar?.accent ?? null}
          drawn={!settingsOpen && fit.panes.includes(n)}
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
          // A compose row's first send in flight has no thread to steer yet,
          // but it can be stopped: the stop waits for the acknowledgement
          // (`sendStops`).
          stoppable={runningHere(chat) || (chat.busy && !!chat.compose?.sending)}
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
          footLine={!!thinking}
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
              onAnswer={answerQuestion}
              onSteer={(id, text) => taskAction(id, "steer", text)}
              onCancel={(id) => taskAction(id, "cancel")}
              onResume={(id) => taskAction(id, "resume")}
              onStart={(id) => taskAction(id, "start")}
            />
          </div>
        );
      case "file": {
        // Keyed by its project: its tabs are paths there, and a buffer saves
        // through the project it was read through (state/buffers.ts).
        const pid = paneProject(spec, n);
        return (
          <FileTab
            key={pid ?? "none"}
            pane={n}
            projectId={pid}
            tabs={spec.projectId && spec.projectId === pid ? spec.editors ?? null : null}
            onTabs={(id, tabs) => view.setEditors(n, id, tabs)}
            narrow={info.narrow}
            blocked={blocked}
            onAddToChat={addToChat}
            ask={fileAsk[n] ?? null}
            onRenamed={onRenamed}
            onEditorSettings={openEditorSettings}
            root={pid ? treeRoot(state.projects.find((p) => p.id === pid)) : null}
          />
        );
      }
      case "vex": {
        // Its pinned project (pinned when the pane switched to it), else the chat's.
        const pid = stripProject(state, spec, n);
        return (
          <VexView
            pane={n}
            drawn={info.drawn}
            projectId={pid}
            projectName={projectName(pid)}
            blocked={blocked}
            onOpenError={(path, line, col) => pid && openAt(n, pid, path, line, col)}
          />
        );
      }
      case "diff": {
        // A task's diff as before, or a project's uncommitted changes (lib/scm).
        const mode = diffModeOf(spec);
        return (
          <div className="diffpane">
            {mode.kind !== "none" ? (
              // What it shows, and why (review of PR #67, L3): the Task pane's
              // task, the task picked last, this pane's project or the chat's.
              <div className="diffhead" data-testid="diff-mode" data-mode={mode.kind}
                   data-why={mode.kind === "task" ? mode.why : mode.pinned ? "pin" : "chat"}>
                <span data-testid="diff-mode-what">{diffHeading(mode)}</span>
                <span className="muted" data-testid="diff-mode-why"> · {diffWhy(mode)}</span>
              </div>
            ) : null}
            {mode.kind === "project" ? (
              <ProjectDiff
                key={mode.projectId}
                projectId={mode.projectId}
                root={treeRoot(state.projects.find((p) => p.id === mode.projectId))}
                drawn={info.drawn}
                narrow={info.narrow}
                filePath={spec.filePath}
                blocked={blocked}
                onSelect={(path) => view.setFilePath(n, path)}
                onOpen={(path) => openFromDiff(n, mode.projectId, path)}
              />
            ) : mode.kind === "task" ? (
              <DiffTab taskId={mode.taskId} narrow={info.narrow} filePath={spec.filePath}
                       onSelect={(path) => view.setFilePath(n, path)} />
            ) : (
              <div className="pad muted" data-testid="diff-none">Pick a project, or a task, to see its changes.</div>
            )}
          </div>
        );
      }
      case "browser":
        return (
          <BrowserTab
            pane={n}
            captureDestination={pageDestination}
            slotOf={threadId => {
              if (settingsNow.current) return null;
              const lay = layoutNow.current;
              const pane = lay.fit.panes.find(id => lay.ws.panes[id - 1].view === "chat" && live.current.chats[id].threadId === threadId);
              return pane === undefined ? null : slotOf(lay.ws, pane);
            }}
            slot={info.slot}
            drawn={info.drawn}
            projectId={paneProject(spec, n)}
            browser={spec.browser}
            control={browser}
            spec={spec}
            zoom={view.zoom}
            liveFrames={browserFrames}
            unseen={Object.keys(devUnseen).filter(key => key.startsWith(n + ":")).map(key => key.slice(key.indexOf(":") + 1))}
            newTabNote={Object.entries(devUnseen).filter(([key]) => key.startsWith(n + ":")).map(([, host]) => host).at(-1)}
            blocked={workBlocked}
            limitMessage={browserFull === n}
            onSoundSettings={() => {
              if (blockedNow.current || settingsNow.current || live.current.picker || chipOverlay.current || crashedRef.current) return;
              chooseSettingsSection("Browser");
              settingsKeyRef.current();
            }}
            onLoaded={(id, url) => view.setBrowserUrl(n, url, id)}
            onSubmitted={(id, url) => view.submitBrowserUrl(n, url, id)}
            onKeepOrigin={(id, origin) => view.setTabKeepOrigin(n, id, origin)}
            onAdd={(id) => view.addBrowserTab(n, "", { id })}
            onClose={(id) => view.closeBrowserTab(n, id)}
            onActivate={(id) => view.activateBrowserTab(n, id)}
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
    // A chat pane's voice chip (V7): the voice it reads in, skip and mute.
    const readThread = spec.view === "chat" && info.drawn ? shownThread(state.chats[n], pendingThread.current[n]) : null;
    return (
      <>
        {readThread ? (
          <VoiceChip
            pane={n}
            info={speech.voices.info(readThread)}
            pool={speech.voices.pool()}
            speaking={speechView.state === "playing" && speechView.thread === readThread}
            active={speech.controller.items().live.some((i) => i.thread === readThread)}
            blocked={workBlocked}
            onChoose={(voice) => void speech.voices.choose(readThread, voice)}
            onSkip={() => speech.controller.skipThread(readThread)}
            onMute={(muted) => void speech.voices.mute(readThread, muted)}
          />
        ) : null}
        {refusal && dirtyPanes[refusal.pane] ? (
          <span className="chip panepin warn" data-testid={`pane-${n}-refused`} role="status"
                title="Switching that pane away would close its file and drop the edit">
            {refusal.pane === n
              ? "unsaved edit · save or reload first"
              : `pane ${slotOf(ws, refusal.pane)} has an unsaved edit · save or reload it first`}
          </span>
        ) : null}
        {spec.view === "file" && goneHeld[n] && pinGone(spec) ? (
          // The one remount nothing can avoid: ask before it drops the edit.
          <span className="chip panepin warn" data-testid={`pane-${n}-gone`}
                title="The project this file was read from is gone; the unsaved edit is kept here until you discard it">
            project gone · unsaved edit kept
            <button
              type="button"
              className="quiet"
              data-testid={`pane-${n}-discard`}
              onClick={() => {
                // The owner threw the edit away: the pane's holds go, and with
                // them every buffer of this pane's no other pane holds — one
                // another pane on the gone project still holds stays, with its
                // own prompt there.
                buffers.releasePane(n, { discard: true });
                view.pin(n, null);
              }}
            >
              discard edit
            </button>
          </span>
        ) : spec.view === "file" ? (
          // A File pane picks its own project (decisions E-13): every project,
          // or Follow chat. Only project roots are offered — a folder becomes
          // reachable by becoming a project, so folders.py stays the one gate.
          // A pane holding an unsaved edit refuses, with the refusal chip, and
          // the select snaps back; under a card it is disabled (and inert).
          <select
            className="panepick"
            data-testid={`pane-${n}-folder`}
            value={spec.projectId && !pinGone(spec) ? spec.projectId : ""}
            disabled={blocked}
            title={dirty
              ? "Save or reload the file first: another project closes it"
              : "The project this pane's files come from"}
            onChange={(e) => {
              if (dirtyNow(n)) {
                setRefused((r) => ({ ...r, [n]: { at: Date.now(), pane: n } }));
                return;
              }
              view.pin(n, e.target.value || null);
            }}
          >
            <option value="">Follow chat{activeProjectId ? ` · ${projectName(activeProjectId)}` : ""}</option>
            {state.projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        ) : spec.view === "vex" ? (
          // The VEX view's project (VX-2): its own, picked here; never the
          // chat's by surprise — a build or a download goes to the project
          // this says. Disabled under a card (and inert).
          <select
            className="panepick"
            data-testid={`pane-${n}-vex-project`}
            value={stripProject(state, spec, n) ?? ""}
            // Off while a download's chain runs (review M1): the chain never
            // goes on to a robot this pane no longer shows.
            disabled={blocked || !!vex.chain}
            title={vex.chain ? "A download is running: wait for it, or Cancel it, first"
              : "The VEX project this pane builds and downloads"}
            onChange={(e) => e.target.value && view.pin(n, e.target.value)}
          >
            {stripProject(state, spec, n) ? null : <option value="">Pick a project</option>}
            {state.projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        ) : spec.view === "diff" ? (
          // The Diff pane's project (2026-10-12): a task picked shows its diff
          // (Follow); else the pinned project's uncommitted changes, else the
          // chat's. Picking a project here takes the window off the task, as
          // picking a thread does, so the choice shows at once — except while
          // a drawn Task pane shows the task: the Diff then shows that task
          // (it never shows another, review of PR #67 L3), and the picker is
          // off and says so. Disabled under a card (and inert).
          <select
            className="panepick"
            data-testid={`pane-${n}-diff-project`}
            value={(() => {
              const mode = diffModeOf(spec);
              return mode.kind === "project" && mode.pinned ? mode.projectId : "";
            })()}
            disabled={blocked || taskInPane}
            title={taskInPane
              ? "A Task pane shows a task, so this pane shows that task's diff: clear the task in the Task pane's header, or switch that pane away, to see a project's uncommitted changes"
              : "Whose changes this pane shows: a project's uncommitted changes, or the selected task's"}
            onChange={(e) => {
              const id = e.target.value || null;
              if (id && live.current.taskFocus) patch({ taskFocus: false });
              view.pin(n, id);
            }}
          >
            <option value="">
              {(state.taskFocus || taskInPane) && task ? `Follow · task ${task.id.slice(0, 6)}`
                : `Follow chat${activeProjectId ? ` · ${projectName(activeProjectId)}` : ""}`}
            </option>
            {state.projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        ) : spec.view === "task" && task ? (
          // The way out of a task (the Diff follow-ups, 2026-10-12): the
          // default 2×2 grid draws a Task pane, and while one shows a task
          // every Diff pane shows that task's diff (review of PR #67, L3) —
          // nothing could take the window off it. Clearing unpicks the task
          // everywhere, so no Diff pane ever shows another task than this one.
          <span className="chip panepin" data-testid={`pane-${n}-task`} title={task.brief || task.id}>
            task {task.id.slice(0, 6)}
            <button
              type="button"
              className="quiet"
              data-testid={`pane-${n}-task-clear`}
              disabled={blocked}
              title="Unpick this task: the Task pane shows none, and a Diff pane shows its project's uncommitted changes"
              onClick={() => {
                if (blockedNow.current || crashedRef.current) return;
                patch({ taskId: null, taskFocus: false });
              }}
            >
              clear
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

  // The split button's state, shared by every pane (it depends on the count).
  // A File pane with an unsaved edit splits too since ED-2: the copy holds
  // the same buffers, edit and all (decisions S-8's "save first" was for two
  // copies of one file that could diverge).
  const verdict = view.splitVerdict(PANE_NOS.filter((n) => dirtyPanes[n]));
  const paneCount = orderOf(ws).length;
  const splitFor = (_n: PaneNo) => ({ ok: verdict.ok, title: splitTitle(verdict.why, paneCount) });

  const paneContext = (spec: PaneSpec, info: PaneInfo): string => {
    switch (spec.view) {
      case "chat": {
        const chat = state.chats[info.pane];
        // Its title, or "New thread"/"Untitled" — never the id (chat titles).
        const thread = threadOf(chat.threadId);
        if (thread) return threadLabel(thread).text;
        return chat.compose ? `New thread in ${projectName(chat.compose.projectId)}` : "";
      }
      case "task":
        return task?.brief || "";
      case "diff": {
        const mode = diffModeOf(spec);
        return mode.kind === "task" ? task?.brief || "" : diffHeading(mode);
      }
      case "file":
        return `in: ${projectName(paneProject(spec, info.pane))}`;
      case "vex":
        return `VEX · ${projectName(stripProject(state, spec, info.pane))}`;
      case "browser":
        return spec.browserUrl || "";
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
    onOpen: () => {
      setEditingSchedule(null);
      patch({ picker: "schedule" });
    },
  });

  return (
    <>
      {/* A render error in here — the title bar, the shell, the orb — shows a
          Reload prompt in their place and goes no further: the card below is
          outside it, so it stays up and answerable (components/Boundary.tsx). */}
      <WorkspaceBoundary onCrash={onCrash} blocked={blocked}>
      <TitleBar view={view} blocked={blocked} settingsOpen={settingsOpen}
        settingsDisabled={!!state.picker || threadModel.overlayOpen}
        onSettings={() => settingsKeyRef.current()} />
      <div className="workspace-surface" data-testid="workspace-surface" data-covered={settingsOpen}
        {...(workBlocked ? { inert: "" } as Record<string, string> : {})}>
      <div
        id="shell"
        className={(drawn.leftFolded ? "left-collapsed " : "") + (drawn.rightFolded ? "right-collapsed " : "")
          + (micTight ? "mic-tight " : "") + (!drawn.leftFolded && stripShown(speechView) ? "speech-on" : "")}
        style={{ ["--left-w" as any]: `${drawn.left}px`, ["--right-w" as any]: `${drawn.right}px` }}
      >
        {drawn.leftFolded ? (
          <Rail
            side="left"
            auto={drawn.autoLeft}
            disabled={workBlocked}
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
          onOpen={sidebarCalls.onOpen}
        />
        {drawn.leftFolded ? null : (
          <Splitter
            side="left"
            width={drawn.left}
            max={maxWidth("left", drawn.right, view.available, mainMin)}
            zoom={view.zoom}
            blocked={workBlocked}
            onResize={(w) => view.setWidth("left", w)}
            onReset={() => view.resetWidth("left")}
          />
        )}

        <div className="pane" id="main">
          <CrashProbe where="workspace" />
          <Workspace
            view={view}
            blocked={workBlocked}
            covered={settingsOpen}
            render={renderView}
            extras={paneExtras}
            context={paneContext}
            selectedPane={selected}
            onView={setPaneView}
            offersVex={(spec, info) => offersVex(vex.detection(stripProject(state, spec, info.pane)))}
            projects={state.projects}
            openBrowser={openBrowser}
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
            splitFor={splitFor}
            onSplit={(n) => void splitFrom(n)}
            closeQuestion={closeQuestion}
            closeSave={(n) => (buffers.paneLosing(n).length ? () => buffers.savePane(n) : null)}
            onClose={closePane}
          />
        </div>
        {drawn.rightFolded ? null : (
          <Splitter
            side="right"
            width={drawn.right}
            max={maxWidth("right", drawn.left, view.available, mainMin)}
            zoom={view.zoom}
            blocked={workBlocked}
            onResize={(w) => view.setWidth("right", w)}
            onReset={() => view.resetWidth("right")}
          />
        )}
        <div className="pane" id="right">
          <div className="barrow">
            <h2 className="bar">{task ? "Task" : "Status"}</h2>
            {/* The zoom control moved to the title bar (2026-10-09): here it
                folded away with the pane it sat in. */}
            <CollapseButton side="right" onCollapse={() => view.fold("right")} disabled={workBlocked} />
          </div>
          <div className="scroll">
            <ApprovalQueue requests={state.approvals} />
            <TaskView
              task={task}
              threads={state.taskId ? state.taskThreads[state.taskId] || [] : []}
              onAnswer={answerQuestion}
              onSteer={(id, text) => taskAction(id, "steer", text)}
              onCancel={(id) => taskAction(id, "cancel")}
              onResume={(id) => taskAction(id, "resume")}
              onStart={(id) => taskAction(id, "start")}
            />
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
            <SttRetry stt={stt} disabled={state.approvals.length > 0} />
            {state.error ? <div className="block err" data-testid="error">{state.error}</div> : null}
          </div>
        </div>
        {drawn.rightFolded ? (
          <Rail
            side="right"
            auto={drawn.autoRight}
            disabled={workBlocked}
            onExpand={() => view.open("right")}
            approvals={state.approvals.length}
            error={!!state.error}
          />
        ) : null}
      </div>

      <DevServerPrompts rows={devservers.rows().filter(row => ["prompt", "notice", "lost"].includes(row.phase))}
        hidden={workBlocked || crashed || devservers.enabled !== true}
        layoutKey={JSON.stringify([view.ws.order, view.fit.panes, view.layout, view.zoom])} zoom={view.zoom}
        label={devLabel} targets={devTargets()}
        onOpen={id => { if (!blockedNow.current && !settingsNow.current) void devservers.claim(id); }}
        onPlace={chooseDevPane}
        onDismiss={id => { if (!blockedNow.current && !settingsNow.current) void devservers.dismiss(id); }} />

      <Orb
        state={orbShown}
        level={state.level}
        outputLevel={() => speech.controller.level()}
        speech={<SpeechStrip view={speechView} label={speechLabel} blocked={workBlocked}
          onToggle={() => speech.controller.toggle()} onSkip={() => speech.controller.skip()}
          onStop={() => speech.controller.stop()} onClearMarks={() => speech.controller.clearMarks()} />}
        speechRail={<SpeechStrip compact view={speechView} label={speechLabel} blocked={workBlocked}
          onToggle={() => speech.controller.toggle()} onSkip={() => speech.controller.skip()}
          onStop={() => speech.controller.stop()} onClearMarks={() => speech.controller.clearMarks()} />}
        rings={state.avatar?.rings}
        accent={state.avatar?.accent}
        avatarUrl={state.avatar ? `/avatar.svg?v=${avatarCacheBust}` : null}
        // A folded sidebar leaves a 36px rail: the orb shrinks into its foot
        // rather than sitting on top of the input bar.
        compact={drawn.leftFolded}
        tight={micTight}
        blocked={workBlocked}
        zoom={view.zoom}
        status={orbStatus}
        mode={state.dictation}
        onModeChange={setMode}
        onPress={press}
        onRelease={release}
      />
      </div>
      </WorkspaceBoundary>

      <DialogBoundary resetKey={settingsOpen ? settingsSection : "closed"}
        onCrash={() => {
          threadModel.closeDialogs();
          // A Settings crash can occur under a card. Close it without moving
          // focus off that card; the work and authorization stay mounted.
          if (!live.current.approvals.length) closeSettings();
          else { settingsNow.current = false; setSettingsOpen(false); }
          patch({ error: "Settings hit an error and was closed. Reload if it keeps happening." });
        }}>
        {settingsOpen ? (
          <SettingsPage section={settingsSection} onSection={chooseSettingsSection}
            onClose={closeSettings} blocked={blocked} nested={threadModel.overlayOpen} zoom={view.zoom}>
            {settingsSection === "General" ? <GeneralSettings blocked={blocked}
              installStatus={installApp.status} onInstall={installApp.install} /> : null}
            {settingsSection === "Model" ? (
              <ModelPicker embedded
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
                onClose={closeSettings}
              />
            ) : null}
            {settingsSection === "Voice" ? (
              <><VoicePicker embedded
                voices={voices}
                override={voiceOverride}
                onPick={(name) => api.setVoice(name).then(loadVoices).catch(() => {})}
                onClose={closeSettings}
              /><ReadAloudSetting on={speechView.enabled}
                onChange={(on) => { saveReadAloud(on); speech.controller.setEnabled(on); }} /><SelfVoiceSetting gate={speech.selfVoice} onChange={(on) => { saveSelfVoiceGate(on); speech.selfVoice.setEnabled(on); }} /><EndPauseSetting ms={endPause} onChange={(ms) => { saveEndPause(ms); setEndPause(loadEndPause()); }} /><VoiceDiagnosticsPanel diagnostics={voiceDiagnostics} capture={capture} muted={isMuted(state.dictation)} stt={stt} onRetryDetector={() => vadStage.retry()} /></>
            ) : null}
            {settingsSection === "Avatar" ? (
              <AvatarPicker embedded
                avatars={avatars}
                active={state.avatar?.slug || ""}
                onPick={(slug) => api.setAvatar(slug).then(loadAvatar).catch(() => {})}
                onClose={closeSettings}
              />
            ) : null}
            {settingsSection === "Usage" ? <UsagePanel usage={state.usage} /> : null}
            {settingsSection === "Routing" ? <><RoutingSettings route={state.route} />
              <DecisionsLog route={state.route} /></> : null}
            {settingsSection === "Browser" ? <BrowserSettings blocked={blocked} /> : null}
            {settingsSection === "Archive" ? (
              <ArchiveView embedded blocked={blocked}
                version={lifecycle}
                onRestored={() => {
                  void refreshProjects();
                  void refreshThreads();
                  void refreshTasks();
                  void refreshSchedules();
                }}
                onDeleted={() => void refreshProjects()}
                onClose={closeSettings}
              />
            ) : null}
          </SettingsPage>
        ) : null}
      </DialogBoundary>

      {/* The boot animation: on the first frame, so the HUD never shows before
          it; below the authorization veil, and gone the moment a card is known.
          Automation never mounts it unless its own suite opts in. */}
      {!navigator.webdriver || window.__hudBootForce === true ? <BootOverlay cardUp={blocked} /> : null}
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
      {state.picker === "quickOpen" && nav?.projectId ? (
        <QuickOpen
          projectId={nav.projectId}
          projectName={projectName(nav.projectId)}
          activePath={nav.activePath}
          onOpen={(path, line, col) => {
            const at = nav;
            closeNav(false);
            const pane = at ? toFilePane(at) : null;
            if (pane !== null) askFile(pane, { kind: "open", path, line, col });
          }}
          onClose={() => closeNav(true)}
          onLeave={() => closeNav(false)}
        />
      ) : null}
      {state.picker === "palette" && nav ? (
        <Palette entries={paletteList} onRun={runEntry} onClose={() => closeNav(true)} onLeave={() => closeNav(false)} />
      ) : null}
      {state.picker === "editorSettings" && !blocked ? <EditorSettingsDialog onClose={closeEditorSettings} /> : null}
      {threadModel.picker}
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
                if (channelNote) dispatch({ type: "notice", text: channelNote });
              })
          }
          taken={projectNamesTaken(state.projects, null, state.archivedNames)}
          onVex={() => patch({ picker: "newVexProject" })}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "newVexProject" ? (
        // VX-3: a VEX project from the extension's templates, through the
        // `project_folder` card. Its controls are off under any card.
        <NewVexProject
          blocked={blocked}
          onCreated={(id) => {
            void refreshProjects(id).then(() => {
              if (live.current.picker === "newVexProject") patch({ picker: null });
              // A new project starts as a new thread in it, as New project's do.
              if (!live.current.approvals.length) newThread(id);
            });
          }}
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
