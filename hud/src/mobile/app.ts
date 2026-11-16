// The signed-in phone app (phone design §6, P-3): three tabs over the read
// API (jarvis/v2/remote_api.py), live over its event stream, read-only.
//
//   Inbox   the inbox rows, the HUD's groups; approvals and questions are
//           shown in full and say "Answer at the desk" (answering is P-4).
//   Chats   projects, then each project's five most recent chats (the
//           desktop sidebar's rule, lib/recentChats.ts), then a transcript.
//   Settings   connection, unlock window, notifications and session.
//   Tasks stays a filter in the Inbox, with its detail pages above it.
//
// **Live, and never stale while it says it is current.** The stream
// (`/m/api/events`) carries ids only; each event makes the app read the
// route it names again. When a read cannot reach Jarvis, or the stream
// drops and a check finds no way through, the app turns offline: a banner
// says so and when the shown data was last read, and the view is marked
// stale (dimmed, `data-stale`). Coming back reads everything shown again.
// Each list also keeps its own failed-refresh warning until its read succeeds,
// even when other routes recover and the connection orb says Live.
// A 401 anywhere means the session ended (logout,
// a revoke at the desk, expiry): the app stops and asks for a sign-in.
// There is no offline queue.
//
// P-4, answering (jarvis/v2/remote_answer.py): an approval row opens its
// card (./card.ts), read from `GET /m/api/inbox/<id>/detail`; Deny is one
// tap, the session only. AUTHORIZE and ALWAYS are gated actions (WS-5c,
// ./unlock.ts): each tap freezes its request with an op token from the
// pool; a locked phone asks for Face ID once (unlock/options, the passkey,
// unlock, then the frozen request, in the same gesture) and an unlocked one
// asks nothing. ALWAYS reads its rule (`answer/rule`) and takes one confirm
// tap, which sends the rule's `preview`. Questions and decisions are
// answered from their rows. Each answer is sent once, never queued; the one
// resend is WS-5c's, after a 423, of the same frozen request. A refusal
// shows the daemon's words. When §4.1 does not hold (`/m/api/me`'s
// `secure`), a red "limited: ..." line says so.
//
// P-5, writing (jarvis/v2/remote_write.py, ./write.ts): an owner's chat
// takes a message box (Send is a tap; Enter is a new line), the Chats tab a
// "New chat" sheet and the Tasks tab a "New task" sheet; a task offers
// Start, Resume and Cancel, each confirmed first, Cancel a gated action.
// A proposal in the inbox takes Withdraw. Nothing is shown as sent before
// its 2xx; offline, nothing is sent (no write queue).
//
// MR, the morning review (jarvis/v2/review.py, ./review.ts): reached from
// the Inbox line, including quiet hours while deferred approvals wait. A
// launch or notification tap opens it when the last push was the morning one
// (`opens_here`), else the Inbox. Questions and
// decisions are answered in place; deferred approvals are ticked one by one
// (never all at once) and approved as a set, ONE gated action naming exactly
// that set, all or nothing; a limited phone (§4.1) gets the per-row Approve
// and retry only.
//
// WS-5c, the unlock window (./unlock.ts): a lock pill in the header (it opens
// its two-row popover, Lock now and Settings); Done and Undo in an open chat's ⋯
// menu. The pill is the phone's copy of the window: it never authorizes.
//
// P-7, push to talk (jarvis/v2/remote_stt.py, ./talk.ts): an owner's chat's
// message box also takes "Hold to talk". Its words are added to that chat's
// draft and the box drawn again; Send is still the owner's tap. No inbox
// row, card, question or decision has a mic.

import { waitingCount, type InboxRow } from "../lib/inbox";
import { brandButton, orbState, renderOrb } from "./orb";
import { inboxIcon, chatsIcon, settingsIcon, lockIcon, unlockIcon } from "./icons";
import { createProjectFolds } from "./folds";
import { Drawer, drawerSwipe } from "./drawer";
import { getJson, errorWord, postJson, reachOfGot, reachOfPosted, type Got, type Posted, type Reach } from "./net";
import { absoluteWords, agoWords, refreshTimes } from "./reltime";
import { cardView, deadlineAt, deadlineWords, parseDetail, parseRule, parseSecure, type CardState, type Rule, type Secure } from "./card";
import { credentialJSON, prepareRequest } from "./webauthn";
import { NOT_READY, pillLabel, pillWords, realTimer, Unlock, UNLOCK_AND_SEND, type FrozenOp, type Outcome, type Stage, type Timer } from "./unlock";
import {
  RUNNING_STATES, parseDiff, parseEvent, parseProjects, parseTask, parseThreads,
  type ChatThread, type DiffView, type LogRow, type Project, type StreamEvent, type TaskView, type WorkingItem,
} from "./model";
import { chatsView, el, inboxRow, logRow, ownerChat, taskView, tasksView, transcriptView, tap, type InboxAnswers } from "./views";
import { serialRead, type SerialReader } from "./reader";
import { decodeInbox, decodeLog, decodeReview, decodeTasks, decodeWorking, readVerdict, type Decoded } from "./reads";
import { answerKey, waiting } from "./threads";
import { SlotCache, threadsView, type Filter } from "./threadsView";
import { browserEnv, browserSend, Notifications, type PushEnv, type Send } from "./notify";
import {
  chatSheetView, clientIdFor, composerView, MAYBE_DONE, MAYBE_SENT, MESSAGE_CHARS, parseModels, refusal, sentNote,
  taskControlsView, taskSheetView, updateComposer, verbsFor,
  type Composer, type ProviderChoice, type Sheet, type Verb,
} from "./write";
import { Talk, browserTalkEnv, type TalkEnv } from "./talk";
import { followViewport } from "./viewport";
import { afterChanged, allItems, approveBody, confirmationList, confirmedChanged, emptySelection, keptSelection, leaveBody, toggled,
  UNTICKED_GONE, type ConfirmLine, type ReviewItem, type ReviewReport } from "../lib/review";
import { reviewLine, reviewReachable, reviewView, type ReviewActions } from "./review";
import { ScrollMemory, byAttr, menuView, sheetView, signOutSheet, trayView, type MenuItem } from "./nav";
import { RowScroll } from "./rowScroll";
import { staleAt } from "../lib/approvalSummary";
import { ACTION_PROGRESS, actionBody, prRowOf, resultWords, type PrAction } from "../lib/prRow";

import { settingsView, updateSettings, type SettingsState } from "./settings";

export type Tab = "inbox" | "review" | "chats" | "tasks" | "settings";

/** What the app needs of `EventSource` (a fake in the tests). */
export interface StreamLike {
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close(): void;
}

export interface AppDeps {
  root: HTMLElement;
  /** The session ended (401): show the sign-in screen with this line. */
  onSignedOut: (message: string) => void;
  /** The owner pressed Sign out. */
  signOut: () => Promise<void>;
  /** RD-6 will use the whole brand to open navigation. */
  onBrand?: () => void;
  fetchJson?: (path: string) => Promise<Got>;
  /** P-4's writes (a fake in the tests). */
  postJson?: (path: string, value: unknown) => Promise<Posted>;
  /** The passkey for one unlock: the request options in, the assertion's JSON out (a fake in the tests). */
  getCredential?: (publicKey: Record<string, unknown>) => Promise<Record<string, unknown> | null>;
  /** WS-5c rule 6: whether the tap's user activation still holds (`navigator.userActivation.isActive`). */
  activation?: () => boolean | null;
  /** WS-5c: the op-token pool's half-life refill and the Undo window (real timers unless a test gives its own). */
  unlockTimer?: Timer;
  openStream?: (url: string) => StreamLike;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  /** P-6's notifications: the browser's push and the push routes (fakes in the tests). */
  pushEnv?: PushEnv;
  pushSend?: Send;
  /** P-7's microphone and clip upload (fakes in the tests). */
  talkEnv?: TalkEnv;
  /** The working read's one retry timer (30 s). A real timer unless a test gives its own: `setTimer` runs at once there. */
  setSlow?: (fn: () => void, ms: number) => unknown;
  clearSlow?: (handle: unknown) => void;
}

export const STREAM_URL = "/m/api/events";
// A write that met the proxy with nothing behind it (`tailscale serve`, the daemon down).
const OFFLINE: ReadonlySet<number> = new Set([502, 504]);
// How long after an answer box loses focus the inbox is drawn again (renderInbox).
const BLUR_REDRAW_MS = 400;
const CLOSED = 2;
const DEBOUNCE_MS = 120;
/** PX-2: the working read follows turn starts and ends, and the inbox, at most once a second. */
export const WORKING_DEBOUNCE_MS = 1000;
/** PX-2 (Sol, finding 5): after a failed working read, the one retry waits this long. */
export const WORKING_RETRY_MS = 30_000;
const RECENT_KEY = "jarvis.phone.recentAll";
const RETRY_MS = 4000;
/** M2 (phone audit): what the card says while it waits on Face ID, then on Jarvis. */
export const ASKING_FACE_ID = "Asking for Face ID…";
export const SENDING = "Sending…";
/** Opus L5: a sheet opened again while this row's earlier gated answer is still out. */
export const STILL_ANSWERING = "Your earlier answer to this is still being sent…";
/** WS-5c §5.4: the Always route's words when the rule changed since it was read (`remote_answer.RULE_CHANGED`). */
export const RULE_CHANGED = "the rule changed: read it again";
/** M13: what keeps the focus across a redraw (a redraw builds new nodes): never AUTHORIZE or ALWAYS. */
const KEEP_FOCUS: ReadonlySet<string> = new Set(["m-card-heading", "approval-deny", "m-flash", "m-item-review", "m-item-later", "m-item-unlater", "m-item-fold", "m-thread-open", "m-project-head", "m-review-open"]);
/** L1: how often the card's "N min left" is redrawn. */
const DEADLINE_TICK_MS = 15_000;
/** UX-2: the relative-time labels' one coarse tick. */
const TIME_TICK_MS = 60_000;

/** WS-5c §5.6: Done and Undo on the open chat: its words, and Done's undo token until its window ends. */
interface ThreadDone { id: string; busy: boolean; note: string; token: string | null; until: number }

/** `loaded`: the initial read finished (including a refusal); `hasRead`: a valid log was applied. */
interface OpenThread {
  id: string; meta: ChatThread | null; rows: LogRow[]; next: number; older: boolean; list: HTMLElement | null; loaded: boolean; hasRead: boolean;
}
interface OpenTask {
  id: string;
  view: TaskView | null;
  diff: DiffView | null | "loading" | string;
  /** Each file's fold as the owner left it, kept across a read again. */
  folds: Map<string, boolean>;
}

export class PhoneApp {
  tab: Tab = "inbox";
  online = true;
  failStatus: number | null = null;
  everOpened = false;
  private streamOpen = false;
  private firstOpenTimedOut = false;
  private firstOpenTimer: unknown = null;
  private sessionEnded = false;
  lastRead: number | null = null;
  inbox: InboxRow[] | null = null;
  projects: Project[] | null = null;
  threads: ChatThread[] = [];
  expanded = new Set<string>();
  readonly folds = createProjectFolds();
  private unsubscribeFolds: () => void;
  thread: OpenThread | null = null;
  tasks: TaskView[] | null = null;
  /** PX-2: the read cut the tasks at its cap (`more`), and the inbox likewise. */
  tasksMore = false;
  inboxMore = false;
  /** PX-2: the In progress list. `null` entries: not read, or the working route failed (its own state, never offline). */
  entries: WorkingItem[] | null = null;
  workingState: "loading" | "ok" | "unavailable" = "loading";
  workingCut = { more: false, total: 0 };
  /** The strip's filter, Recent's "Show all", and the inbox read's failure line. */
  filter: Filter = null;
  recentAll = false;
  listError: string | null = null;
  readonly slots = new SlotCache();
  /** PX-4: where "back" from an open chat goes: the view it was opened from (the Inbox, the review or the Chats list). */
  private threadBack: "inbox" | "review" | "chats" = "chats";
  /** Where "back" from a task goes: the list filter it was opened from. */
  private taskBack: Filter = "tasks";
  task: OpenTask | null = null;
  stopped = false;
  stream: StreamLike | null = null;
  /** P-4: the open answering card, and §4.1's last word (`/m/api/me`). */
  card: CardState | null = null;
  secure: Secure | null = null;
  readonly answering = new Set<string>();
  readonly notes = new Map<string, string>();
  readonly drafts = new Map<string, string>();
  readonly picks = new Map<string, number>();
  /** AS: inbox and review rows whose command the owner opened (true) or closed (false). */
  readonly opened = new Map<string, boolean>();
  private deferred = false;
  /** One line shown once above the inbox after an answer ("Denied.", where a decision's answer went). */
  private flash = "";
  /** P-6: "Turn on notifications", under the Inbox (./notify.ts). */
  readonly notify: Notifications;
  /** P-5: the open sheet (New chat, New task), what it offers, and each chat's and task's write state. */
  sheet: Sheet | null = null;
  models: ProviderChoice[] | null = null;
  readonly composeDrafts = new Map<string, string>();
  readonly composeNotes = new Map<string, string>();
  readonly sending = new Set<string>();
  /** Each draft's `client_id` (P-5): a retry after an unknown outcome is answered, not run, twice. */
  readonly clientIds = new Map<string, { id: string; text: string }>();
  readonly withdrawing = new Set<string>();
  /** PM-F: what each PR row's fixed notice says while its action runs ("Asking for Face ID…", "Merging…"). */
  readonly prProgress = new Map<string, string>();
  taskWrite: { id: string; confirming: Verb | null; busy: boolean; note: string } | null = null;
  /** P-7: the one "Hold to talk" button, shown in the open chat's box. */
  readonly talk: Talk;
  /** MR: the morning review, its ticks (empty until the owner's own taps), and its in-place receipts. */
  review: ReviewReport | null = null;
  reviewSel: Set<string> = emptySelection();
  readonly reviewReceipts = new Map<string, { item: ReviewItem; text: string }>();
  readonly reviewPicks = new Map<string, number>();
  reviewBusy = false;
  reviewConfirming = false;
  /** The exact set the open confirmation shows (ids and tags): only it is ever sent. */
  reviewConfirmed: { id: string; tag: string }[] | null = null;
  /** The open confirmation's lines, as they were when it opened (review of #330, LOW 4). */
  reviewConfirmLines: ConfirmLine[] = [];
  reviewFlash = "";
  private reviewFirst = true;

  private readonly fetchJson: (path: string) => Promise<Got>;
  private readonly postJson: (path: string, value: unknown) => Promise<Posted>;
  private readonly getCredential: (publicKey: Record<string, unknown>) => Promise<Record<string, unknown> | null>;
  private readonly openStream: (url: string) => StreamLike;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;
  private readonly timers = new Map<string, unknown>();
  private readonly frame: HTMLElement;
  private readonly banner: HTMLElement;
  private readonly brand: HTMLButtonElement;
  private readonly main: HTMLElement;
  private readonly rowScroll: RowScroll;
  private readonly tabs: Record<"inbox" | "chats" | "settings", HTMLButtonElement>;
  private inboxReader!: SerialReader;
  private workingReader!: SerialReader;
  private tasksReader!: SerialReader;
  private workingRetry: unknown = null;
  private readonly setSlow: (fn: () => void, ms: number) => unknown;
  private readonly clearSlow: (handle: unknown) => void;
  private logBusy = false;
  /** An open transcript drawn again: where its scroll was ("end": at the newest message). */
  private keepScroll: "end" | number | null = null;
  private logAgain = false;
  /** PX-4: the scroll each view had, and what is open besides the list. There is no browser-history integration. */
  private readonly mem = new ScrollMemory();
  /** Which ⋯ menu is open: the header's, an open task page's, or an open chat's. */
  menuOpen: "app" | "task" | "thread" | null = null;
  /** WS-5c: the op-token pool, the window's mirror, and every gated tap's flow (./unlock.ts). */
  readonly unlock: Unlock;
  /** WS-5c rule 6: controls whose Face ID needs a fresh tap: scope (a row id, "review", "task:<id>",
   * "thread:<id>") → which action, and its frozen op, which "Unlock and send" resumes unchanged. */
  readonly tapAgain = new Map<string, string>();
  private readonly frozen = new Map<string, FrozenOp>();
  /** WS-5c §5.6: Done and Undo on the open chat: its words, and Done's undo token until its window ends. */
  threadDone: ThreadDone | null = null;
  private readonly pill: HTMLButtonElement;
  private readonly unlockTimer: Timer;
  /** The open chat's Needs-you tray folded to its bar by the owner. */
  trayFolded = false;
  private menuWrap: HTMLElement;
  private drawer: Drawer | null = null;
  private unbindEdge: (() => void) | null = null;
  private signOutOverlay: { close: () => void } | null = null;
  private lockNote = "";
  private readonly topBar: HTMLElement;
  private readonly tabBar: HTMLElement;
  private readonly sheetHost: HTMLElement;
  /** The view the main area last drew, and (while the approval sheet is open) the one it keeps under the sheet. */
  private drawnKey: string | null = null;
  private held: string | null = null;
  /** Connection health and list freshness are independent: only this route's valid read clears it
   * (`applyRead`). "failed": nothing new was read; "partial": a read applied, but some records were dropped. */
  private readonly failedReads = new Map<string, "failed" | "partial">();

  constructor(private readonly deps: AppDeps) {
    // EVERY request this app makes goes through these two (and the push and clip wrappers below), so each
    // outcome reaches the orb through `report`, the one path: no call site decides "offline" on its own.
    const rawGet = deps.fetchJson ?? getJson;
    const rawPost = deps.postJson ?? postJson;
    this.fetchJson = async (path) => {
      const got = await rawGet(path);
      const resource = path.split("?")[0];
      if (!this.stopped && /^\/m\/api\/(inbox|working|tasks|review|threads\/[^/]+\/log)$/.test(resource)) {
        // HTTP success proves reachability only. The loader clears freshness after applying valid data.
        if (!got.ok) this.failedReads.set(resource, "failed");
      }
      this.report(reachOfGot(got), got.ok ? 200 : got.status);
      if (!this.stopped) this.renderReadWarnings();
      return got;
    };
    this.postJson = async (path, value) => {
      const posted = await rawPost(path, value);
      this.reportWrite(posted);
      return posted;
    };
    this.getCredential = deps.getCredential ?? (async (publicKey) => {
      const credential = await navigator.credentials.get({ publicKey: prepareRequest(publicKey) });
      return credential ? credentialJSON(credential as PublicKeyCredential) : null;
    });
    this.openStream = deps.openStream ?? ((url) => new EventSource(url) as unknown as StreamLike);
    this.now = deps.now ?? Date.now;
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = deps.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
    this.setSlow = deps.setSlow ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearSlow = deps.clearSlow ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
    try { this.recentAll = localStorage.getItem(RECENT_KEY) === "1"; } catch { /* no storage: it just does not last */ }
    this.inboxReader = serialRead(() => this.read("/m/api/inbox"), (r) => this.applyInbox(r));
    this.workingReader = serialRead(() => this.fetchJson("/m/api/working"), (got) => this.applyWorking(got));
    this.unlockTimer = deps.unlockTimer ?? realTimer;
    this.unlock = new Unlock({
      fetchJson: this.fetchJson, postJson: this.postJson, getCredential: this.getCredential,
      activation: deps.activation ?? (() => {
        const ua = typeof navigator !== "undefined" ? (navigator as { userActivation?: { isActive: boolean } }).userActivation : undefined;
        return ua ? ua.isActive : null;
      }),
      now: this.now, timer: this.unlockTimer,
      signedOut: () => this.signedOut(),
      // Every answer Unlock gets came through the reporting `postJson`/`fetchJson` above. What is left is
      // the request that never answered within its bound: no response, so offline (amber).
      reached: (ok, status) => { if (!ok && status === 0) this.report("offline", 0); },
      changed: () => { this.lockNote = ""; this.renderPill(); },
      // Opus L1: when local state cannot be trusted about the window, `me` decides the pill.
      reread: () => { if (!this.stopped) void this.probe(); },
    });
    this.tasksReader = serialRead(() => this.read("/m/api/tasks"), (r) => this.applyTasks(r));
    const frame = el("div", "app");
    frame.dataset.screen = "app";
    frame.dataset.stale = "false";
    const top = el("header", "top");
    this.brand = brandButton(() => {
      this.deps.onBrand?.();
      this.openDrawer();
    });
    top.append(this.brand);
    // The pill opens Lock now and Settings.
    // The pill remains the phone's copy of the unlock window, never authorization.
    this.pill = tap("Locked", () => this.toggleMenu(this.menuOpen === "app" ? null : "app"), "m-lock-pill", "m-lock-pill");
    this.pill.dataset.state = "locked";
    top.append(this.pill);
    this.menuWrap = menuView({ id: "m-menu", label: "Menu", items: [], open: false, toggle: () => {} });
    top.append(this.menuWrap);
    this.renderOrb();
    this.banner = el("div", "offline");
    this.banner.dataset.testid = "m-offline";
    this.banner.setAttribute("role", "status");
    this.banner.hidden = true;
    this.main = el("main", "main");
    this.rowScroll = new RowScroll(this.main);
    this.main.dataset.testid = "m-main";
    const nav = el("nav", "tabs");
    nav.setAttribute("aria-label", "Sections");
    const make = (tab: "inbox" | "chats" | "settings", label: string, icon: () => SVGSVGElement) => {
      const b = tap(label, () => this.show(tab), "tab", "m-tab-" + tab);
      const glyph = el("span", "m-tab-icon");
      glyph.append(icon());
      b.replaceChildren(glyph, el("span", "m-tab-label", label));
      nav.append(b);
      return b;
    };
    this.tabs = { inbox: make("inbox", "Inbox", inboxIcon), chats: make("chats", "Chats", chatsIcon),
      settings: make("settings", "Settings", settingsIcon) };
    this.sheetHost = el("div", "m-sheet-host");
    frame.append(top, this.banner, this.main, nav, this.sheetHost);
    this.topBar = top;
    this.tabBar = nav;
    // A tap outside an open ⋯ menu closes it.
    frame.addEventListener("click", (e) => {
      if (this.menuOpen && !(e.target instanceof Element && e.target.closest(".m-menu, .m-lock-pill"))) this.toggleMenu(null);
    });
    this.frame = frame;
    // The push routes and the clip upload report through the same path as every other request.
    const rawPush = deps.pushSend ?? browserSend;
    const pushSend: Send = async (method, path, body) => {
      const sent = await rawPush(method, path, body);
      this.reportWrite(sent);
      return sent;
    };
    const rawTalk = deps.talkEnv ?? browserTalkEnv;
    const talkEnv: TalkEnv = {
      media: () => rawTalk.media(), context: () => rawTalk.context(), now: () => rawTalk.now(),
      setTimer: (fn, ms) => rawTalk.setTimer(fn, ms), clearTimer: (handle) => rawTalk.clearTimer(handle),
      upload: async (wav) => {
        const uploaded = await rawTalk.upload(wav);
        this.reportWrite(uploaded);
        return uploaded;
      },
    };
    this.notify = new Notifications(deps.pushEnv ?? browserEnv, pushSend,
      () => {
        if (this.tab === "settings" && !this.stopped) this.refreshNotifications();
        if (this.tab === "inbox" && !this.card && !this.stopped) this.renderInbox();
      },
      () => { if (!this.stopped) void this.openFromNotification(); });
    this.talk = new Talk(talkEnv, {
      insert: (threadId, text) => this.dictated(threadId, text),
      // No recording while this chat's send waits for its answer (its 202 would clear the box).
      busy: (threadId) => this.sending.has(threadId),
      unauthorized: () => void this.recheckSession(),
      online: () => this.online,
    });
    this.drawer = new Drawer(frame, this.brand, this.folds, () => this.drawerState(), {
      section: (section) => {
        // Tasks is an Inbox filter; a previous task's Back filter must not override this shortcut.
        if (section === "tasks") this.taskBack = "tasks";
        if (section === "chats") this.thread = null;
        if (section === "inbox") this.filter = null;
        this.show(section);
      },
      thread: (thread) => { void this.openThread(thread.id, thread); },
      lock: () => { void this.lockNow(); },
    });
    this.unbindEdge = drawerSwipe(frame, "open", () => this.canOpenDrawer() && !this.drawer?.isOpen, {
      begin: () => this.openDrawer(), move: (dx) => this.drawer?.follow(dx),
      end: (commit) => this.drawer?.finishOpening(commit),
    });
    this.unsubscribeFolds = this.folds.subscribe(() => {
      if (this.stopped || this.tab !== "chats" || this.thread || this.sheet) return;
      const focus = document.activeElement;
      const headId = focus instanceof HTMLElement && focus.dataset.testid === "m-project-head" ? focus.id : null;
      this.render();
      if (headId) document.getElementById(headId)?.focus();
    });
  }

  // -- life cycle ----------------------------------------------------------------

  start(): void {
    this.deps.root.className = "app-root";
    this.deps.root.replaceChildren(this.frame);
    this.renderMenu();
    this.renderPill();
    // WS-5c rule 1: the op-token pool fills before the first tap.
    void this.unlock.pool.refill();
    // A notification's tap opens /m/#inbox: the Inbox is the first tab anyway.
    if (typeof location !== "undefined" && location.hash === "#inbox") history.replaceState(null, "", "/m/");
    this.render();
    void this.loadInbox();
    void this.loadWorking(true);
    void this.loadTasks();
    // MR-12: a launch opens on the review when the morning push was the last one.
    void this.loadReview();
    void this.loadMe();
    void this.notify.sync();
    this.connect();
    this.tickTimes();
  }

  stop(): void {
    this.unbindEdge?.();
    this.unbindEdge = null;
    this.drawer?.stop();
    this.signOutOverlay?.close();
    this.stopped = true;
    this.unsubscribeFolds();
    this.rowScroll.stop();
    this.clearFirstOpenTimer();
    if (this.recoverTimer !== null) this.clearTimer(this.recoverTimer);
    this.recoverTimer = null;
    if (this.staleTimer) clearTimeout(this.staleTimer);
    this.staleTimer = null;
    if (this.deadlineTimer) clearTimeout(this.deadlineTimer);
    this.deadlineTimer = null;
    if (this.timeTimer) clearTimeout(this.timeTimer);
    this.timeTimer = null;
    this.talk.hidden();
    this.talk.dropRetry();
    this.stream?.close();
    this.stream = null;
    for (const r of [this.inboxReader, this.workingReader, this.tasksReader]) r.invalidate();
    if (this.workingRetry !== null) this.clearSlow(this.workingRetry);
    this.workingRetry = null;
    this.unlock.stop();
  }

  /** The app came back into view: the window's mirror (`me`) and the op-token pool, read again (WS-5c rules 1, 2). */
  visible(): void {
    if (this.stopped) return;
    void this.probe();
    void this.unlock.pool.refill();
  }

  private async signOut(): Promise<void> {
    this.stop();
    await this.deps.signOut();
  }

  private signedOut(): void {
    if (this.stopped) return;
    this.sessionEnded = true;
    this.renderOrb();
    this.stop();
    this.deps.onSignedOut("Your session ended. Sign in again.");
  }

  /**
   * THE one path from a request's outcome to the orb (net.ts `Reach`). The reporting `fetchJson` and
   * `postJson`, the push and clip wrappers, the stream's open and Unlock's unanswered request all end
   * here; call sites only decide what their own view says. A refusal (any other 4xx, a 409, the daemon's
   * own 503) changes nothing: Jarvis answered.
   */
  private report(reach: Reach, status: number): void {
    if (this.stopped) return;
    if (reach === "reached") this.setOnline(true);
    else if (reach === "signed-out") this.signedOut();
    else if (reach === "unreachable") this.setOnline(false, status);
    else if (reach === "offline") this.setOnline(false, 0);
  }

  /** A write's outcome. Its 401 may be a refusal rather than the session ending, so `me` decides (WS-5c
   * rule 9); the call site awaits the same check (`recheckSession` is shared) before saying anything. */
  private reportWrite(posted: { status: number; body: unknown; offline?: boolean } | null): void {
    const reach = reachOfPosted(posted);
    if (reach === "signed-out") { if (!this.stopped) void this.recheckSession(); return; }
    this.report(reach, posted?.status ?? 0);
  }

  private sessionCheck: Promise<void> | null = null;

  /** A 401 from a write or the clip upload: is the session over? One `me` read, however many ask at once;
   * its 401 signs out through `report`. */
  private recheckSession(): Promise<void> {
    if (!this.sessionCheck) {
      this.sessionCheck = this.fetchJson("/m/api/me").then(() => undefined, () => undefined)
        .finally(() => { this.sessionCheck = null; });
    }
    return this.sessionCheck;
  }

  // -- reading ---------------------------------------------------------------------

  /**
   * PR 381: the ONE step every list read's 200 goes through (the Inbox, In progress, Tasks, the
   * review, a chat log's first page and its "load more" pages). `readVerdict` (./reads.ts) judges the
   * decoded body. Malformed (wrong shape, or a non-empty record list with no valid record): nothing
   * is applied, no cursor moves, the warning stays or is raised. Otherwise `apply` runs first, and
   * only then is the warning cleared (valid) or kept as "incomplete" (partial). Returns the applied
   * value, or null when nothing was applied. A failed request never gets here: `fetchJson` marked it.
   */
  private applyRead<T>(resource: string, decoded: Decoded<T> | null, apply: (value: T, partial: boolean) => void): T | null {
    if (this.stopped) return null;
    const path = `/m/api/${resource}`;
    const verdict = readVerdict(decoded ? decoded.lists : null);
    if (!decoded || verdict === "malformed") {
      this.failedReads.set(path, "failed");
      this.renderReadWarnings();
      return null;
    }
    apply(decoded.value, verdict === "partial");
    if (verdict === "valid") this.failedReads.delete(path);
    else this.failedReads.set(path, "partial");
    this.renderReadWarnings();
    return decoded.value;
  }

  /** One read: the body, or null when offline or signed out (`report` has already said which). */
  private async read(path: string): Promise<{ body: unknown } | { error: string; status: number } | null> {
    const got = await this.fetchJson(path);
    if (this.stopped) return null;
    if (got.ok) {
      this.lastRead = this.now();
      return { body: got.body };
    }
    if (got.why === "signed-out" || got.why === "offline") return null;
    return { error: errorWord(got.body) || `could not be read (${got.status})`, status: got.status };
  }

  /** One inbox read at a time, plus one owed (reader.ts). It decides online and offline, as before. */
  loadInbox(afterWrite = false): Promise<void> {
    return this.inboxReader.trigger({ afterWrite });
  }

  private applyInbox(r: { body: unknown } | { error: string; status: number } | null): void {
    if (this.stopped || !r) return;
    if (!("body" in r)) {
      // Not offline, not signed out: the read itself failed. The last list stays, with the words and a Retry.
      this.listError = `The Inbox could not be read: ${r.error}`;
      if (this.tab === "inbox") this.renderInbox();
      return;
    }
    const applied = this.applyRead("inbox", decodeInbox(r.body), ({ rows, more }, partial) => {
      this.listError = null;
      this.inbox = rows;
      this.inboxMore = more;
      this.renderBadge();
      // An incomplete read may have dropped the open card's own row: its absence proves nothing.
      const card = this.card;
      if (!partial && card && !card.busy && !rows.some((row) => row.id === card.rowId)) {
        card.refused = "This card is no longer waiting: it was answered, or it timed out.";
        card.rule = null;
      }
    });
    if (!applied) return;
    if (this.tab === "inbox") this.renderInbox();
    else if (this.tab === "chats" && !this.thread && !this.sheet) this.render();
    // M5: the row a task's "Answer it in the Inbox" asked for, once the read redrew it.
    const pending = this.pendingRow;
    this.pendingRow = null;
    if (pending && this.tab === "inbox" && !this.card) this.revealRow(pending);
  }

  /**
   * PX-2 (plan C §4.1): the In progress read. **Its failure is its own** (Sol, finding 3): it goes
   * through `fetchJson`, never `read()`, so the route's own refusal (the daemon's 503 "the In progress
   * list is not running", with its words) never turns the app offline and never stamps "last read".
   * Like every request it still reports through `report`: a 401 ends the session, and the proxy's
   * 502/503/504 (no daemon words: Jarvis is down) is "Jarvis unreachable". One policy for every trigger (Sol, finding 5):
   * `force` (the list shown, the stream reopened, coming back online, a resync) reads now; the rest
   * (working_changed, an inbox change, a turn starting or ending) read after a one-second debounce
   * and, while a retry is waiting, leave the next read to its timer. A failed read arms ONE 30 s
   * retry timer; a success disarms it.
   */
  loadWorking(force = false): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (!force && this.workingRetry !== null) return Promise.resolve();
    if (force && this.workingRetry !== null) { this.clearSlow(this.workingRetry); this.workingRetry = null; }
    return this.workingReader.trigger();
  }

  private applyWorking(got: Got): void {
    if (this.stopped) return;
    // A failed request was marked by `fetchJson`; a 200 is judged by `applyRead`.
    const applied = got.ok ? this.applyRead("working", decodeWorking(got.body), (list) => {
      this.entries = list.entries;
      this.workingState = "ok";
      this.workingCut = { more: list.more, total: list.total };
      if (this.workingRetry !== null) { this.clearSlow(this.workingRetry); this.workingRetry = null; }
    }) : null;
    if (!applied) {
      // A failed refresh keeps the last entries (and their cutoff) visible, with its own warning.
      this.workingState = "unavailable";
      if (this.workingRetry === null) {
        this.workingRetry = this.setSlow(() => { this.workingRetry = null; void this.loadWorking(true); }, WORKING_RETRY_MS);
      }
    }
    this.renderOrb();
    if (this.tab === "inbox" && !this.card) this.renderInbox();
    else if (this.tab === "chats" && !this.thread && !this.sheet) this.render();
  }

  /** The working read, after the debounce (a burst of events is one read). */
  private workingSoon(): void {
    this.later("working", () => void this.loadWorking(false), WORKING_DEBOUNCE_MS);
  }

  /** §4.1's last result, to show (`/m/api/me`): never what authorizes. */
  async loadMe(): Promise<void> {
    // Display only. Its outcome (a 401, the proxy's 502/503/504, no answer) reached the orb in `fetchJson`.
    const got = await this.fetchJson("/m/api/me");
    if (this.stopped || !got.ok) return;
    this.secure = parseSecure((got.body as { secure?: unknown })?.secure);
    this.renderOrb();
    // WS-5c rule 2: the window's mirror follows `me.unlock_s` (display and path only).
    this.unlock.fromMe(got.body);
    if (this.tab === "inbox") this.renderInbox();
  }

  /** MR: the morning review. The first read decides where a launch opens (MR-12). */
  async loadReview(): Promise<void> {
    const r = await this.read("/m/api/review");
    if (!r || !("body" in r)) return;
    const report = this.applyRead("review", decodeReview(r.body), (value) => {
      this.review = value;
      // Under an open confirmation, a re-read that drops or changes any
      // confirmed item closes it: nothing is approved but the set the owner
      // confirmed (review of #329, MED 1). On an incomplete read that is the
      // safe direction too: a dropped item is unticked, never approved.
      this.checkConfirmed(value);
      // A re-read unticks whatever left the review.
      this.reviewSel = keptSelection(value, this.reviewSel);
    });
    if (!report) return;
    this.refreshSettings();
    this.drawer?.refresh();
    const first = this.reviewFirst;
    this.reviewFirst = false;
    if (first && report.opensHere && this.tab === "inbox" && !this.card) {
      this.show("review");
      return;
    }
    if (this.tab === "review") this.renderKeepingFocus();
    // PX-4: the Inbox's review line follows the review as it is read.
    else if (this.tab === "inbox" && !this.card) this.renderInbox();
  }

  /** A notification's tap on an open app (sw.ts posts "open-inbox"): the review when the
   * morning push was the last one and it has open items, else the Inbox (MR-12). */
  async openFromNotification(): Promise<void> {
    this.card = null;
    const r = await this.read("/m/api/review");
    if (this.stopped) return;
    const report = r && "body" in r ? this.applyRead("review", decodeReview(r.body), (value) => { this.review = value; }) : null;
    // Opened from a notification: nothing is ticked (review of #329, MED 2).
    this.resetReviewSelection();
    this.show(report?.opensHere ? "review" : "inbox");
  }

  /** The review entered afresh: no box ticked, no confirmation open. */
  private resetReviewSelection(): void {
    if (this.reviewBusy) return;
    this.reviewSel = emptySelection();
    this.reviewConfirming = false;
    this.reviewConfirmed = null;
    this.reviewFlash = "";
  }

  private checkConfirmed(report: ReviewReport): void {
    if (!this.reviewConfirming || !this.reviewConfirmed || this.reviewBusy) return;
    const changed = confirmedChanged(report, this.reviewConfirmed);
    if (!changed.length) return;
    this.reviewConfirming = false;
    this.reviewConfirmed = null;
    const next = new Set(this.reviewSel);
    for (const id of changed) {
      next.delete(id);
      this.notes.set(id, UNTICKED_GONE);
    }
    this.reviewSel = next;
    this.reviewFlash = "Something you selected changed: nothing was approved. Look again, then confirm again.";
  }


  /** One question, decision or proposal, answered in place; once sent it folds to a receipt where it stood. */
  async answerInReview(item: ReviewItem, body: Record<string, unknown>): Promise<void> {
    const row = item.row;
    if (this.answering.has(row.id)) return;
    this.answering.add(row.id);
    this.notes.delete(row.id);
    this.reviewFlash = "";
    this.renderKeepingFocus();
    const path = row.kind === "decision" && typeof row.detail.decision_id === "string"
      ? `/m/api/inbox/decisions/${encodeURIComponent(row.detail.decision_id)}` : this.path(row.id, "answer");
    const result = await this.post(path, body);
    this.answering.delete(row.id);
    if (result && result.status === 200) {
      this.drafts.delete(row.id);
      this.reviewPicks.delete(row.id);
      this.withdrawing.delete(row.id);
      const words = typeof result.body.note === "string" ? result.body.note
        : row.kind === "proposal" ? "Withdrawn: the task will not start." : "Sent.";
      this.reviewReceipts.set(row.id, { item, text: oneLineNote(words) });
    } else if (result) {
      this.notes.set(row.id, await this.refusedWith(result.status, result.body));
    }
    this.renderKeepingFocus();
    void this.loadReview();
    void this.loadInbox(true);
  }

  /** Leave it on one deferred row (the session only). */
  async leaveInReview(row: InboxRow): Promise<void> {
    const item = this.review ? allItems(this.review).find((i) => i.row.id === row.id) : undefined;
    if (!item) return;
    await this.answerInReview(item, { action: "leave" });
  }

  /** Approve selected, step two: exactly the confirmed set (ids and tags), frozen with its op token, as ONE
   * gated action (WS-5c): Face ID once if the phone is locked; all or nothing at the daemon. `confirmed` is the
   * set the drawn confirmation was built from (captured when its button was drawn); a different one is refused. */
  async approveSelected(confirmed: readonly { id: string; tag: string }[] | null): Promise<void> {
    const report = this.review;
    if (!report || this.reviewBusy || !this.reviewConfirming || !confirmed || this.reviewConfirmed !== confirmed) return;
    // Exactly what the confirmation showed, and only while it still holds.
    if (confirmedChanged(report, confirmed).length) { this.checkConfirmed(report); this.render(); return; }
    const items = confirmed.map((c) => ({ id: c.id, tag: c.tag }));
    this.reviewBusy = true;
    this.reviewFlash = "";
    this.render();
    const out = await this.gated("review", "approve", "/m/api/review/approve", { items }, () => {});
    this.reviewBusy = false;
    if (out !== "not-ready" && out.kind === "signed-out") return;
    if (out !== "not-ready" && out.kind === "needs-tap") {
      // Rule 6: the confirmation stays open; its button reads "Unlock and send" and resumes this same set.
      this.reviewFlash = this.gatedWords(out, "review", "approve");
      this.render();
      return;
    }
    this.reviewConfirming = false;
    this.reviewConfirmed = null;
    if (out !== "not-ready" && out.kind === "done") {
      const n = typeof out.body.approved === "number" ? out.body.approved : items.length;
      this.reviewSel = emptySelection();
      this.reviewFlash = `Approved ${n} once each, for 30 minutes; their chats were told.`;
    } else if (out !== "not-ready" && out.kind === "refused" && out.status === 409 && Array.isArray(out.body.changed)) {
      // All or nothing: what changed is unticked, with why; the rest need a new confirmation.
      const after = afterChanged(this.reviewSel, out.body.changed);
      this.reviewSel = after.selection;
      for (const [id, words2] of after.notes) this.notes.set(id, words2);
      this.reviewFlash = "Nothing was approved: something you selected changed. Look again, then confirm again.";
    } else if (out !== "not-ready" && out.kind === "refused") {
      this.reviewFlash = "Nothing was approved. " + this.refusedWords(out.status, out.body);
    } else {
      this.reviewFlash = this.gatedWords(out, "review", "approve");
    }
    this.render();
    void this.loadReview();
    void this.loadInbox(true);
  }

  /** Leave selected: the session only, one result each. */
  async leaveSelected(): Promise<void> {
    const report = this.review;
    if (!report || this.reviewBusy) return;
    const body = leaveBody(report, this.reviewSel);
    if (!body) return;
    this.reviewBusy = true;
    this.reviewFlash = "";
    this.render();
    const result = await this.post("/m/api/review/leave", body);
    this.reviewBusy = false;
    if (result && result.status === 200) {
      const results = Array.isArray(result.body.results) ? result.body.results : [];
      const left = results.filter((x) => x && typeof x === "object" && (x as { ok?: unknown }).ok === true).length;
      this.reviewSel = emptySelection();
      this.reviewFlash = `Left ${left} of ${body.items.length}: nothing was sent.`;
    } else if (result) {
      this.reviewFlash = "Not left. " + await this.refusedWith(result.status, result.body);
    }
    this.render();
    void this.loadReview();
    void this.loadInbox(true);
  }

  /** The inbox, drawn again, unless an answer is being typed (it waits for the box to lose focus). */
  private renderInbox(): void {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (!this.card && active instanceof HTMLTextAreaElement && this.main.contains(active)) {
      if (!this.deferred) {
        this.deferred = true;
        // Not at the blur itself: a tap on Send blurs the box first, and a
        // redraw then would take the button away before its click lands.
        active.addEventListener("blur", () => {
          setTimeout(() => {
            this.deferred = false;
            if (this.tab === "inbox" && !this.stopped) this.renderInbox();
          }, BLUR_REDRAW_MS);
        }, { once: true });
      }
      return;
    }
    this.render();
  }

  // -- answering (P-4) ---------------------------------------------------------------

  private path(rowId: string, tail: string): string {
    return `/m/api/inbox/${encodeURIComponent(rowId)}/${tail}`;
  }

  /** A write's answer: null when it met no Jarvis (`report` has already told the orb). */
  private async post(path: string, value: unknown): Promise<{ status: number; body: Record<string, unknown> } | null> {
    const got = await this.postJson(path, value);
    if (this.stopped) return null;
    if ("offline" in got || OFFLINE.has(got.status)) return null;
    this.slid(got.status);
    return got;
  }

  /** WS-5c rule 2: a session-only action's 2xx slides the mirror, only while it is open (`touch_unlock`). */
  private slid(status: number): void {
    if (status < 200 || status >= 300) return;
    this.unlock.mirror.slideSession();
    this.renderPill();
  }

  /** A refusal's words. A 401 is the session ending (the `me` probe signs out); never "locked" (WS-5c rule 9). */
  private async refusedWith(status: number, body: Record<string, unknown>): Promise<string> {
    if (status === 401) {
      // The write's report already asked `me`; this waits for that same read.
      await this.recheckSession();
      return this.stopped ? "" : "Not accepted (401).";
    }
    const word = typeof body.error === "string" ? body.error : "";
    return word ? word[0].toUpperCase() + word.slice(1) + "." : `Not answered (${status}).`;
  }

  openCard(row: InboxRow): void {
    this.flash = "";
    this.card = { rowId: row.id, row, detail: null, refused: null, busy: false, rule: null, notice: "" };
    this.invoker = this.tab === "review" ? "m-review-open-card" : "m-item-review";
    // PX-4: the sheet opens over whatever the owner is looking at (the list, a chat, the review), its scroll kept.
    if (this.menuOpen) { this.menuOpen = null; this.renderMenu(); }
    this.render();
    void this.loadCard();
  }

  closeCard(): void {
    this.card = null;
    this.render();
    void this.loadInbox();
  }

  async loadCard(): Promise<void> {
    const card = this.card;
    if (!card) return;
    const got = await this.fetchJson(this.path(card.rowId, "detail"));
    if (this.stopped || this.card !== card) return;
    // H5: offline, the card says it was not read (setOnline, from `report`, leaves an open card alone).
    if (!got.ok && got.why === "offline") { this.render(); return; }
    if (got.ok) {
      card.detail = parseDetail(got.body);
      card.refused = card.detail ? null : "This card could not be read.";
      if (card.detail) this.secure = card.detail.secure;
    } else {
      const body = (got.body ?? {}) as Record<string, unknown>;
      card.refused = errorWord(body) || `This card could not be read (${got.status}).`;
      if (body.secure) this.secure = parseSecure(body.secure);
    }
    this.render();
  }

  private async finish(card: CardState, result: { status: number; body: Record<string, unknown> } | null,
                       what: string): Promise<void> {
    if (this.card !== card) return;
    card.busy = false;
    card.progress = "";
    if (!result) { this.render(); return; }
    if (result.status === 200) {
      this.notes.set(card.rowId, what);
      this.card = null;
      this.flash = what;
      this.render();
      void this.loadInbox();
      return;
    }
    card.notice = await this.refusedWith(result.status, result.body);
    card.rule = null;
    this.focusNotice = true;
    this.render();
    if (result.status === 409) void this.loadCard();
  }

  async denyCard(): Promise<void> {
    const card = this.card;
    if (!card || card.busy || this.answering.has(card.rowId)) return;
    card.busy = true;
    card.notice = "";
    card.progress = SENDING;
    this.render();
    const result = await this.post(this.path(card.rowId, "answer"), { decision: "deny" });
    await this.finish(card, result, "Denied.");
  }

  // -- gated actions (WS-5c, ./unlock.ts) ---------------------------------------------

  /**
   * One gated tap. The request is frozen NOW (its URL, body and a pool token): nothing later changes it.
   * When the control is waiting for "Unlock and send" (rule 6) for this same action, that frozen op is
   * resumed instead, with its own token: no new token, and no action POST before the unlock.
   * "not-ready": the pool was empty and nothing was sent (rule 1).
   */
  private async gated(scope: string, action: string, url: string, fields: Record<string, unknown>,
                      stage: (s: Stage) => void): Promise<Outcome | "not-ready"> {
    const waiting = this.tapAgain.get(scope) === action ? this.frozen.get(scope) : undefined;
    let op: FrozenOp;
    let running: Promise<Outcome>;
    if (waiting) {
      op = waiting;
      running = this.unlock.resume(op, stage);
    } else {
      const made = this.unlock.freeze(`${scope}:${action}`, url, fields);
      if (!made) return "not-ready";
      op = made;
      running = this.unlock.run(op, stage);
    }
    this.tapAgain.delete(scope);
    this.frozen.delete(scope);
    const out = await running;
    if (out.kind === "needs-tap" && !this.stopped) {
      this.tapAgain.set(scope, action);
      this.frozen.set(scope, op);
    }
    return out;
  }

  /** The words a gated outcome shows when it is not the action's own answer (`done`/`refused` are the caller's). */
  private gatedWords(out: Outcome | "not-ready", scope: string, action: string): string {
    return out === "not-ready" ? NOT_READY : this.unlock.words(out, `${scope}:${action}`);
  }

  /** What a stage of the flow says while it runs. */
  private stageWords(s: Stage, sending: string): string {
    return s === "unlocking" ? ASKING_FACE_ID : sending;
  }

  /** The first words a gated tap shows: Face ID when the phone believes it is locked, else what it sends. */
  private firstStage(sending: string): string {
    return this.unlock.mirror.open() ? sending : ASKING_FACE_ID;
  }

  /** A refusal's words without a second `me` probe (the flow already made it for a 401). */
  private refusedWords(status: number, body: Record<string, unknown>): string {
    if (status === 401) return "Not accepted (401).";
    const word = typeof body.error === "string" ? body.error : "";
    return word ? word[0].toUpperCase() + word.slice(1) + "." : `Not answered (${status}).`;
  }

  /** A gated answer on the open card: what it did, or why not, where the owner is looking. */
  private gatedCardFinish(card: CardState, out: Outcome | "not-ready", action: "allow" | "always", what: string): void {
    if (out !== "not-ready" && out.kind === "signed-out") return;
    let ok = false;
    let words: string;
    if (out !== "not-ready" && out.kind === "done") {
      ok = true;
      words = what + (out.body.authority === "limited" ? " (with Discord's authority: limited)" : "");
    } else if (out !== "not-ready" && out.kind === "refused") {
      words = this.refusedWords(out.status, out.body);
    } else {
      words = this.gatedWords(out, card.rowId, action);
    }
    if (this.card !== card) {
      // The sheet closed while it ran: the words go to the row and the list's line, and to the same row's
      // sheet if the owner opened it again meanwhile (its AUTHORIZE was off while this ran, Opus L5).
      this.notes.set(card.rowId, words);
      this.flash = words;
      const again = this.card && this.card.rowId === card.rowId && !this.card.busy ? this.card : null;
      if (again && ok) this.card = null;
      else if (again) { again.notice = words; this.focusNotice = true; }
      this.render();
      if (ok) void this.loadInbox();
      return;
    }
    card.busy = false;
    card.progress = "";
    if (ok) {
      this.notes.set(card.rowId, words);
      this.card = null;
      this.flash = words;
      this.render();
      void this.loadInbox();
      return;
    }
    const waiting = out !== "not-ready" && out.kind === "needs-tap";
    card.tapAgain = waiting ? action : null;
    if (!waiting) card.rule = null;
    card.notice = words;
    this.focusNotice = true;
    this.render();
    if (out !== "not-ready" && out.kind === "refused" && out.status === 409) void this.loadCard();
  }

  /** AUTHORIZE: a gated action (WS-5c): Face ID once if the phone is locked, then the frozen answer. `card` is
   * the sheet the button was drawn on. The row is held in `answering` until the answer settles, so a sheet
   * closed and opened again meanwhile cannot mint a second op for the same approval (Opus L5). */
  async allowCard(card: CardState | null = this.card): Promise<void> {
    if (!card || this.card !== card || card.busy || this.answering.has(card.rowId)) return;
    card.busy = true;
    card.notice = "";
    card.progress = this.firstStage(SENDING);
    this.answering.add(card.rowId);
    this.render();
    const out = await this.gated(card.rowId, "allow", this.path(card.rowId, "answer"), { decision: "allow" },
      (s) => { card.progress = this.stageWords(s, SENDING); if (this.card === card) this.render(); });
    this.answering.delete(card.rowId);
    this.gatedCardFinish(card, out, "allow", "Authorized.");
  }

  /** ALWAYS, step one: the rule it would write, read without a challenge (`answer/rule`, WS-5c §5.4). */
  async alwaysCard(): Promise<void> {
    const card = this.card;
    if (!card || card.busy || this.answering.has(card.rowId)) return;
    card.busy = true;
    card.notice = "";
    card.progress = "Reading the rule ALWAYS would write…";
    this.render();
    await this.readRule(card, "");
  }

  /** The rule read (again): shown with its Confirm, or why it could not be. Never sends anything. */
  private async readRule(card: CardState, notice: string): Promise<void> {
    const got = await this.fetchJson(this.path(card.rowId, "answer/rule"));
    if (this.stopped || this.card !== card) return;
    card.busy = false;
    card.progress = "";
    if (!got.ok && got.why === "offline") {
      card.rule = null;
      card.notice = "Offline: the rule could not be read.";
    } else if (!got.ok) {
      card.rule = null;
      card.notice = this.refusedWords(got.status, (got.body ?? {}) as Record<string, unknown>);
    } else {
      card.rule = parseRule(got.body);
      card.notice = card.rule ? notice : "The rule could not be read.";
    }
    if (card.notice) this.focusNotice = true;
    this.render();
  }

  /** ALWAYS, step two: the owner read the rule; one confirm tap freezes `{decision: "always", preview, op}`.
   * `card` and `rule` are the sheet and the rule its Confirm was drawn with: the preview sent is that rule's,
   * and a rule read again since (another object) is refused, never substituted. Held in `answering` (Opus L5). */
  async confirmAlways(card: CardState | null = this.card, rule: Rule | null = card?.rule ?? null): Promise<void> {
    if (!card || this.card !== card || card.busy || !rule || card.rule !== rule || this.answering.has(card.rowId)) return;
    card.busy = true;
    card.notice = "";
    card.progress = this.firstStage(SENDING);
    this.answering.add(card.rowId);
    this.render();
    const out = await this.gated(card.rowId, "always", this.path(card.rowId, "answer"),
      { decision: "always", preview: rule.preview },
      (s) => { card.progress = this.stageWords(s, SENDING); if (this.card === card) this.render(); });
    this.answering.delete(card.rowId);
    if (out !== "not-ready" && out.kind === "refused" && out.status === 409 && out.body.error === RULE_CHANGED
        && this.card === card) {
      // Rule 8: the rule changed. It is read again and shown; only a new confirm tap sends.
      card.busy = true;
      card.progress = "Reading the rule ALWAYS would write…";
      card.rule = null;
      this.render();
      await this.readRule(card, "The rule changed: read it again, then confirm.");
      return;
    }
    this.gatedCardFinish(card, out, "always", "Authorized, and the rule is written.");
  }

  cancelAlways(): void {
    if (!this.card) return;
    this.card.rule = null;
    this.render();
  }

  /** A row's gated action finished: its words, and whether it was the action's own success. */
  private rowWords(rowId: string, action: string, out: Outcome | "not-ready", ok: (body: Record<string, unknown>) => string): {
    words: string; ok: boolean;
  } {
    if (out !== "not-ready" && out.kind === "done") return { words: ok(out.body), ok: true };
    if (out !== "not-ready" && out.kind === "refused") return { words: this.refusedWords(out.status, out.body), ok: false };
    return { words: this.gatedWords(out, rowId, action), ok: false };
  }

  /** QH, Approve and retry on a deferred row: a gated action bound to the deferral id in its URL. */
  async retryDeferred(row: InboxRow): Promise<void> {
    if (this.answering.has(row.id)) return;
    this.flash = "";
    this.answering.add(row.id);
    this.notes.delete(row.id);
    this.render();
    const out = await this.gated(row.id, "retry", this.path(row.id, "answer"), { action: "retry" }, () => {});
    this.answering.delete(row.id);
    if (out !== "not-ready" && out.kind === "signed-out") return;
    const r = this.rowWords(row.id, "retry", out, (body) => typeof body.note === "string" ? body.note : "Approved once.");
    if (r.ok) this.flash = r.words; else this.notes.set(row.id, r.words);
    this.render();
    void this.loadInbox(true);
  }

  /**
   * PM-F, one action on a PR row (PM-D's phone route; `fix` is PM-E's), a gated action (WS-5c): the body is
   * the row's head and card **as drawn** (`row` is the object the button was drawn from; nothing is read
   * again here, PM-D's MED 2), frozen with its op token at the tap. A 409 says the daemon's words in the
   * row's notice and the inbox is read again, so the row is drawn as it is now. Never sent again.
   */
  async prAct(row: InboxRow, action: PrAction): Promise<void> {
    const view = prRowOf(row);
    const body = view ? actionBody(view, action) : null;
    if (!view || !body || this.answering.has(row.id)) return;
    const bound = { id: row.id, action, head: body.head, card: body.card };
    this.flash = "";
    this.answering.add(row.id);
    this.notes.delete(row.id);
    this.prProgress.set(row.id, this.firstStage(ACTION_PROGRESS[action]));
    this.render();
    const out = await this.gated(row.id, action, "/m/api/inbox/pr", bound, (s) => {
      this.prProgress.set(row.id, this.stageWords(s, ACTION_PROGRESS[action]));
      this.render();
    });
    this.answering.delete(row.id);
    this.prProgress.delete(row.id);
    if (out !== "not-ready" && out.kind === "signed-out") return;
    const r = this.rowWords(row.id, action, out, (b) =>
      resultWords(action, b) + (b.authority === "limited" ? " (with Discord's authority: limited)" : ""));
    this.notes.set(row.id, r.words);
    this.render();
    void this.loadInbox(true);
  }

  /** PM-F: a PR fix's Cancel: the task's cancel, a gated action (WS-5c). */
  async cancelFix(row: InboxRow): Promise<void> {
    const fix = prRowOf(row)?.fix;
    const id = fix?.taskId;
    if (!fix || !id || !fix.live || this.answering.has(row.id)) return;
    this.flash = "";
    this.answering.add(row.id);
    this.notes.delete(row.id);
    this.prProgress.set(row.id, this.firstStage("Cancelling the fix…"));
    this.render();
    const out = await this.gated(row.id, "cancel-fix", `/m/api/tasks/${encodeURIComponent(id)}/cancel`, {}, (s) => {
      this.prProgress.set(row.id, this.stageWords(s, "Cancelling the fix…"));
      this.render();
    });
    this.answering.delete(row.id);
    this.prProgress.delete(row.id);
    if (out !== "not-ready" && out.kind === "signed-out") return;
    const words = out !== "not-ready" && out.kind === "refused"
      ? refusal(out.status, out.body, "Not cancelled")
      : this.rowWords(row.id, "cancel-fix", out, (b) => resultWords("cancel", b)).words;
    this.notes.set(row.id, words);
    this.render();
    void this.loadInbox(true);
  }

  /** A question's or a decision's answer, from its row. Sent once. `echo` is the option's words as the phone
   * drew them (PX-3a): the receipt says them, never the server's `note`. */
  async sendAnswer(row: InboxRow, body: Record<string, unknown>, echo?: string): Promise<void> {
    if (this.answering.has(row.id)) return;
    // The row as it was answered: only that same row is removed on the 200 (a GET may have installed new contents under its id).
    const answered = answerKey(row);
    this.flash = "";
    this.answering.add(row.id);
    this.notes.delete(row.id);
    this.render();
    const path = row.kind === "decision" && typeof row.detail.decision_id === "string"
      ? `/m/api/inbox/decisions/${encodeURIComponent(row.detail.decision_id)}` : this.path(row.id, "answer");
    const result = await this.post(path, body);
    this.answering.delete(row.id);
    if (!result) { this.render(); return; }
    if (result.status === 200) {
      this.drafts.delete(row.id);
      this.picks.delete(row.id);
      this.withdrawing.delete(row.id);
      // D6: its row leaves at once (only this one); the re-read below restores it if the server still lists it.
      // A read that was in flight before this 200 is dropped by the afterWrite trigger, so it cannot bring the row back.
      const current = this.inbox?.find((r) => r.id === row.id);
      if (this.inbox && current && answerKey(current) === answered) {
        this.inbox = this.inbox.filter((r) => r.id !== row.id);
        this.renderBadge();
      }
      const note = echo !== undefined ? `Sent: ${echo}`
        : typeof result.body.note === "string" ? result.body.note
        : row.kind === "proposal" ? "Withdrawn: the task will not start." : "Sent.";
      this.flash = note;
    } else {
      this.notes.set(row.id, await this.refusedWith(result.status, result.body));
    }
    this.render();
    void this.loadInbox(true);
  }

  // -- writing (P-5) -------------------------------------------------------------------

  /** Draw again, unless a box is being typed in: then once it loses focus. */
  private renderKeepingFocus(): void {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (active instanceof HTMLTextAreaElement && this.main.contains(active)) {
      if (!this.deferred) {
        this.deferred = true;
        active.addEventListener("blur", () => {
          setTimeout(() => { this.deferred = false; if (!this.stopped) this.render(); }, BLUR_REDRAW_MS);
        }, { once: true });
      }
      return;
    }
    this.render();
  }

  /** A write's answer, or null when the session ended. `ambiguous` when no
   * answer came back (the request may have landed): never "not sent". */
  private async write(path: string, value: unknown): Promise<{ status: number; body: Record<string, unknown>; ambiguous?: boolean } | null> {
    const got = await this.postJson(path, value);
    if (this.stopped) return null;
    if ("offline" in got || OFFLINE.has(got.status)) return { status: 0, body: {}, ambiguous: true };
    this.slid(got.status);
    const result = got;
    // The write took effect, or may have, but its answer failed (remote_write's UNCERTAIN).
    if (result.body.uncertain === true) return { status: result.status, body: result.body, ambiguous: true };
    if (result.status === 401) {
      // The write's report already asked `me`; this waits for that same read.
      await this.recheckSession();
      if (this.stopped) return null;
    }
    return result;
  }

  /** A message to the open chat: sent once; shown only after its 2xx and a read of the log. */
  async sendMessage(threadId: string, text: string): Promise<void> {
    if (this.sending.has(threadId) || !this.online) return;
    this.sending.add(threadId);
    this.composeNotes.delete(threadId);
    // The box as it was sent: a clip transcribed while the send was on its way adds after it (P-7).
    const sentDraft = this.composeDrafts.get(threadId) ?? text;
    this.renderComposer();
    const key = "msg:" + threadId;
    const result = await this.write(`/m/api/threads/${threadId}/messages`,
      { text, client_id: clientIdFor(this.clientIds, key, text) });
    this.sending.delete(threadId);
    if (this.stopped) return;
    if (!result) {
      this.composeNotes.delete(threadId);
    } else if (result.ambiguous) {
      this.composeNotes.set(threadId, MAYBE_SENT);
    } else if (result.status === 202) {
      this.clientIds.delete(key);
      // Only what was sent leaves the box: words added since (a clip that was already transcribing) stay.
      const now = this.composeDrafts.get(threadId) ?? "";
      const rest = now !== sentDraft && now.startsWith(sentDraft) ? now.slice(sentDraft.length).replace(/^\s+/, "") : "";
      if (rest) this.composeDrafts.set(threadId, rest);
      else this.composeDrafts.delete(threadId);
      this.composeNotes.set(threadId, sentNote(result.body));
      if (this.thread?.id === threadId) void this.moreLog();
    } else {
      this.composeNotes.set(threadId, refusal(result.status, result.body));
    }
    this.renderComposer(true);
  }

  /** The message box drawn again in place (the transcript and its scroll kept). */
  private renderComposer(clear = false): void {
    const open = this.thread;
    if (this.tab !== "chats" || !open || this.sheet) return;
    const old = this.main.querySelector('[data-testid="m-compose"]');
    // The same box drawn again in place: the textarea stays, and with it the focus and the keyboard.
    const state = this.composerState(open.id);
    if (old && state && updateComposer(old, state)) return;
    const fresh = this.composer(open.id);
    if (!old || !fresh) return;
    old.replaceWith(fresh);
    if (clear) fresh.querySelector("textarea")!.value = this.composeDrafts.get(open.id) ?? "";
  }

  private composer(threadId: string): HTMLElement | null {
    const meta = this.thread?.id === threadId ? this.thread.meta : null;
    // Only once the chat is known to be an owner's: a task's chats are the runner's.
    if (!meta) return null;
    if (meta.task_id || meta.role !== "chat") return el("p", "muted small center", "This chat is read only on the phone.");
    return composerView(this.composerState(threadId)!);
  }

  /** What an owner's chat's message box shows, or null for any other chat. */
  private composerState(threadId: string): Composer | null {
    const meta = this.thread?.id === threadId ? this.thread.meta : null;
    if (!meta || meta.task_id || meta.role !== "chat") return null;
    return {
      draft: this.composeDrafts.get(threadId) ?? "", busy: this.sending.has(threadId),
      note: this.composeNotes.get(threadId) ?? "", online: this.online,
      send: (text) => void this.sendMessage(threadId, text),
      edit: (text) => { this.composeDrafts.set(threadId, text); },
      mic: this.talk.view(threadId),
      // The box grew a line: the newest message stays in view if it was.
      around: (change) => { const end = this.nearEnd(); change(); if (end) this.scrollToEnd(); },
    };
  }

  /** P-7: a clip's words, added to that chat's draft after what is typed, and the box drawn again.
   * Never sent: Send is the owner's tap. */
  dictated(threadId: string, text: string): void {
    if (this.stopped) return;
    const shown = this.thread?.id === threadId
      ? this.main.querySelector<HTMLTextAreaElement>('[data-testid="m-compose-text"]') : null;
    const draft = shown ? shown.value : (this.composeDrafts.get(threadId) ?? "");
    const joined = draft && !/\s$/.test(draft) ? `${draft} ${text}` : draft + text;
    this.composeDrafts.set(threadId, joined.slice(0, MESSAGE_CHARS));
    // "May not have been sent" stays: it is about the words already in the box.
    if (this.composeNotes.get(threadId) !== MAYBE_SENT) this.composeNotes.delete(threadId);
    this.renderComposer(true);
  }

  private firstProject(): string {
    const list = this.projects ?? [];
    const open = this.thread?.meta?.project_id ?? this.task?.view?.project_id;
    return (open && list.some((p) => p.id === open) ? open : list[0]?.id) ?? "";
  }

  async openNewChat(): Promise<void> {
    this.tab = "chats";
    this.thread = null;
    this.sheet = { kind: "chat", projectId: this.firstProject(), provider: "fast", model: "", text: "", busy: false,
      note: "", openedId: null };
    this.render();
    if (this.projects === null) await this.loadChats();
    const sheet = this.sheet;
    if (sheet?.kind === "chat" && !sheet.projectId) { sheet.projectId = this.firstProject(); }
    const r = await this.read("/m/api/thread-models");
    if (!r || this.sheet !== sheet) return;
    if ("body" in r) {
      this.models = parseModels(r.body);
      const usable = this.models.filter((m) => m.available);
      if (sheet && sheet.kind === "chat" && !usable.some((m) => m.name === sheet.provider) && usable.length) {
        sheet.provider = usable[0].name;
      }
    } else if (sheet) {
      sheet.note = r.error;
    }
    this.renderKeepingFocus();
  }

  async createChat(): Promise<void> {
    const sheet = this.sheet;
    if (!sheet || sheet.kind !== "chat" || sheet.busy || !this.online) return;
    const text = sheet.text.trim();
    if (!text || !sheet.projectId) return;
    sheet.busy = true;
    sheet.note = "";
    this.render();
    const body: Record<string, unknown> = { project_id: sheet.projectId, provider: sheet.provider, text };
    if (sheet.model) body.model = sheet.model;
    body.client_id = clientIdFor(this.clientIds, "chat", JSON.stringify(body));
    const result = await this.write("/m/api/threads", body);
    if (this.sheet !== sheet) return;
    sheet.busy = false;
    if (!result) { this.render(); return; }
    if (result.ambiguous) { sheet.note = MAYBE_SENT; this.render(); return; }
    this.clientIds.delete("chat");
    const thread = (result.body.thread ?? null) as { id?: unknown } | null;
    if (result.status === 201 && thread && typeof thread.id === "string") {
      const id = thread.id;
      this.sheet = null;
      const send = (result.body.send ?? {}) as Record<string, unknown>;
      this.composeNotes.set(id, sentNote(send));
      void this.loadChats();
      await this.openThread(id);
      return;
    }
    sheet.note = refusal(result.status, result.body, "Not started");
    if (thread && typeof thread.id === "string") sheet.openedId = thread.id;
    this.render();
  }

  openNewTask(): void {
    if (this.tab !== "tasks") this.taskBack = this.tab === "inbox" ? this.filter : "tasks";
    this.tab = "tasks";
    this.task = null;
    this.sheet = { kind: "task", projectId: this.firstProject(), brief: "", busy: false, note: "" };
    this.render();
    if (this.projects === null) {
      void this.loadChats().then(() => {
        const sheet = this.sheet;
        if (sheet?.kind === "task" && !sheet.projectId) sheet.projectId = this.firstProject();
        if (this.sheet === sheet) this.renderKeepingFocus();
      });
    }
  }

  async createTask(): Promise<void> {
    const sheet = this.sheet;
    if (!sheet || sheet.kind !== "task" || sheet.busy || !this.online) return;
    const brief = sheet.brief.trim();
    if (!brief || !sheet.projectId) return;
    sheet.busy = true;
    sheet.note = "";
    this.render();
    const value = { project_id: sheet.projectId, brief };
    const result = await this.write("/m/api/tasks",
      { ...value, client_id: clientIdFor(this.clientIds, "task", JSON.stringify(value)) });
    if (this.sheet !== sheet) return;
    sheet.busy = false;
    if (!result) { this.render(); return; }
    if (result.ambiguous) { sheet.note = MAYBE_DONE; this.render(); return; }
    this.clientIds.delete("task");
    const task = (result.body.task ?? null) as { id?: unknown } | null;
    if (result.status === 201 && task && typeof task.id === "string") {
      this.sheet = null;
      void this.loadTasks();
      this.taskWrite = { id: task.id, confirming: null, busy: false, note: "Created. It waits for Start." };
      await this.openTask(task.id);
      return;
    }
    sheet.note = refusal(result.status, result.body, "Not created");
    this.render();
  }

  private taskState(id: string) {
    if (this.taskWrite?.id !== id) this.taskWrite = { id, confirming: null, busy: false, note: "" };
    return this.taskWrite;
  }

  askVerb(id: string, verb: Verb): void {
    const w = this.taskState(id);
    if (w.busy) return;
    w.confirming = verb;
    w.note = "";
    this.render();
  }

  backVerb(id: string): void {
    const w = this.taskState(id);
    if (w.busy) return;
    w.confirming = null;
    this.render();
  }

  /** A confirmed verb: Start and Resume on the session; Cancel a gated action (WS-5c), frozen at this tap. */
  async confirmVerb(id: string, verb: Verb): Promise<void> {
    const w = this.taskState(id);
    if (w.busy || w.confirming !== verb || !this.online) return;
    w.busy = true;
    w.note = "";
    this.render();
    if (verb === "cancel") {
      const out = await this.gated("task:" + id, "cancel", `/m/api/tasks/${id}/cancel`, {}, () => {});
      if (out !== "not-ready" && out.kind === "signed-out") return;
      if (this.taskWrite !== w) return;
      w.busy = false;
      if (out !== "not-ready" && out.kind === "needs-tap") {
        // Rule 6: the confirmation stays; its button reads "Unlock and send" and resumes this same cancel.
        w.note = this.gatedWords(out, "task:" + id, "cancel");
        this.render();
        return;
      }
      w.confirming = null;
      if (out !== "not-ready" && out.kind === "done") {
        w.note = "Cancelled.";
        void this.loadTasks();
        if (this.task?.id === id) { void this.openTask(id); return; }
      } else if (out !== "not-ready" && out.kind === "refused") {
        w.note = out.status === 401 ? "Not accepted (401)." : refusal(out.status, out.body, "Not cancelled");
      } else {
        w.note = this.gatedWords(out, "task:" + id, "cancel");
      }
      this.render();
      return;
    }
    const result = await this.write(`/m/api/tasks/${id}/${verb}`, {});
    if (this.taskWrite !== w) return;
    w.busy = false;
    w.confirming = null;
    if (!result) {
      w.note = "";
    } else if (result.ambiguous) {
      w.note = MAYBE_DONE;
    } else if (result.status === 200) {
      w.note = verb === "start" ? "Started." : "Resumed.";
      void this.loadTasks();
      if (this.task?.id === id) { void this.openTask(id); return; }
    } else {
      w.note = refusal(result.status, result.body, "Not done");
    }
    this.render();
  }

  // -- Done, Undo and Lock now (WS-5c §5.6, §5.1) ------------------------------------------

  /** The open chat's working-set entry, when it has one with a `seq` (Done binds that thread's `seq`). */
  private doneEntry(id: string): WorkingItem | null {
    const entry = this.entries?.find((e) => e.thread_id === id) ?? null;
    return entry && entry.seq !== null ? entry : null;
  }

  /**
   * Done on the open chat: a gated action bound to the chat in its URL and its `seq` AS DRAWN. `seq` is captured
   * when the menu item was drawn (doneControls) and frozen at the tap: the working set is never read again here,
   * so a background read that brought a newer `seq` cannot change what is sent (Sol MED 1). If the thread moved
   * on since it was drawn, the server's 409 is the right answer.
   */
  async doneThread(id: string, seq: number): Promise<void> {
    if (this.threadDone?.id === id && this.threadDone.busy) return;
    const st = { id, busy: true, note: this.firstStage("Marking it done…"), token: null as string | null, until: 0 };
    this.threadDone = st;
    this.render();
    const out = await this.gated("thread:" + id, "done", `/m/api/working/${encodeURIComponent(id)}/done`, { seq },
      (s) => { st.note = this.stageWords(s, "Marking it done…"); if (this.threadDone === st) this.render(); });
    if (out !== "not-ready" && out.kind === "signed-out") return;
    st.busy = false;
    if (out !== "not-ready" && out.kind === "done" && typeof out.body.token === "string") {
      const undoS = typeof out.body.undo_s === "number" && out.body.undo_s > 0 ? Math.min(out.body.undo_s, 60) : 0;
      st.token = out.body.token;
      st.until = this.now() + undoS * 1000;
      st.note = "Marked done.";
      // The Undo leaves when its window ends.
      if (undoS) this.unlockTimer.set(() => { if (this.threadDone === st && !st.busy) this.render(); }, undoS * 1000 + 50);
      void this.loadWorking(true);
    } else if (out !== "not-ready" && out.kind === "refused") {
      const why = typeof out.body.why_not === "string" && out.body.why_not ? " " + this.refusedWords(out.status, { error: out.body.why_not }) : "";
      st.note = "Not done. " + this.refusedWords(out.status, out.body) + why;
    } else {
      st.note = this.gatedWords(out, "thread:" + id, "done");
    }
    if (this.threadDone === st) this.render();
  }

  /** Undo, within its window, with Done's own token: a gated action of its own (a fresh op token). `drawn` and
   * `token` are the Done and its undo token the button was drawn with; anything else since is refused. */
  async undoThread(id: string, drawn: ThreadDone | null = this.threadDone, token: string | null = drawn?.token ?? null): Promise<void> {
    const st = drawn;
    if (!st || this.threadDone !== st || st.id !== id || st.busy || !token || st.token !== token || this.now() >= st.until) return;
    st.busy = true;
    st.note = this.firstStage("Undoing…");
    this.render();
    const out = await this.gated("thread:" + id, "undo", `/m/api/working/${encodeURIComponent(id)}/undo`, { token },
      (s) => { st.note = this.stageWords(s, "Undoing…"); if (this.threadDone === st) this.render(); });
    if (out !== "not-ready" && out.kind === "signed-out") return;
    st.busy = false;
    if (out !== "not-ready" && out.kind === "done") {
      st.token = null;
      st.note = "Undone: it is back in the list.";
      void this.loadWorking(true);
    } else if (out !== "not-ready" && out.kind === "refused") {
      st.note = "Not undone. " + this.refusedWords(out.status, out.body);
    } else {
      st.note = this.gatedWords(out, "thread:" + id, "undo");
    }
    if (this.threadDone === st) this.render();
  }

  /** Lock now (the ⋯ menu's first row): the window ends at Jarvis; the pill follows only its 200. */
  async lockNow(): Promise<void> {
    const r = await this.unlock.lockNow();
    if (this.stopped || r === "signed-out") return;
    this.flash = r === "locked" ? "Locked. The next Allow, cancel or PR action asks for Face ID."
      : r === "overtaken" ? "Lock now reached Jarvis, but this phone unlocked again since: the pill shows what Jarvis says now."
      : r === "unreached" ? "Lock now did not reach Jarvis: this phone may still be unlocked."
      : "Lock now was refused: this phone may still be unlocked.";
    this.lockNote = this.flash;
    this.renderPill();
    if (this.tab === "inbox" && !this.card) this.render();
  }

  private renderOrb(): void {
    if (!this.brand) return;
    const working = (this.entries ?? []).filter((entry) => entry.status === "working").length
      + (this.tasks ?? []).filter((task) => RUNNING_STATES.has(task.state)).length
      + (this.thread?.meta?.running ? 1 : 0);
    renderOrb(this.brand, orbState({ signedOut: this.sessionEnded, online: this.online,
      failStatus: this.failStatus, streamOpen: this.streamOpen && this.stream?.readyState === 1,
      everOpened: this.everOpened, firstOpenTimedOut: this.firstOpenTimedOut,
      secure: this.secure, working, lastRead: this.lastRead, now: this.now() }));
    this.refreshSettings();
    this.drawer?.refresh();
  }

  /** The lock pill: the phone's copy of the window (display only). */
  private renderPill(): void {
    const pill = this.pill;
    if (!pill) return;
    const open = this.unlock.mirror.open();
    const words = pillWords(this.unlock.mirror, this.now());
    if (pill.textContent !== words || pill.dataset.state !== (open ? "unlocked" : "locked") || !pill.querySelector("svg")) {
      const label = el("span");
      if (open) {
        const prefix = el("span", "m-pill-prefix", "Unlocked · ");
        label.append(prefix, document.createTextNode(words.replace(/^Unlocked · /, "")));
      } else label.textContent = words;
      pill.replaceChildren(open ? unlockIcon() : lockIcon(), label);
    }
    pill.dataset.state = open ? "unlocked" : "locked";
    pill.setAttribute("aria-label", `${pillLabel(this.unlock.mirror, this.now())}. Open the menu`);
    pill.setAttribute("aria-haspopup", "true");
    pill.setAttribute("aria-controls", "m-menu-panel");
    pill.setAttribute("aria-expanded", String(this.menuOpen === "app"));
    this.refreshSettings();
    this.drawer?.refresh();
  }

  async loadChats(): Promise<void> {
    const [p, t] = [await this.read("/m/api/projects"), await this.read("/m/api/threads")];
    if (!p || !("body" in p) || !t || !("body" in t)) return;
    this.projects = parseProjects(p.body);
    this.threads = parseThreads(t.body);
    this.drawer?.refresh();
    if (this.tab === "chats" && !this.thread && !this.sheet) this.render();
  }

  async loadProjectThreads(projectId: string): Promise<void> {
    const r = await this.read(`/m/api/threads?project=${encodeURIComponent(projectId)}`);
    if (!r) return;
    if (!("body" in r)) {
      if (r.status === 404) return void this.loadChats();     // archived meanwhile
      return;
    }
    this.threads = this.threads.filter((t) => t.project_id !== projectId).concat(parseThreads(r.body));
    this.drawer?.refresh();
    if (this.tab === "chats" && !this.thread && !this.sheet) this.render();
  }

  async openThread(id: string, meta: ChatThread | null = null): Promise<void> {
    if (this.thread?.id !== id || this.tab !== "chats") { this.leftFrom("thread", id); this.trayFolded = false; }
    // Every new opening sets where back goes: from the Inbox or the review, or from the Chats list.
    if (this.tab !== "chats") this.threadBack = this.tab === "review" ? "review" : "inbox";
    else if (!this.thread) this.threadBack = "chats";
    this.tab = "chats";
    this.sheet = null;
    this.thread = { id, meta: meta ?? this.threads.find((t) => t.id === id) ?? null, rows: [], next: 0, older: false, list: null,
      loaded: false, hasRead: false };
    this.render();
    const r = await this.read(`/m/api/threads/${id}/log`);
    if (!r || this.thread?.id !== id) return;
    if (!("body" in r)) { this.threadGone(r.error); return; }
    const opened = this.thread;
    const page = this.applyRead(`threads/${id}/log`, decodeLog(r.body), (value) => {
      this.thread = { id, meta: value.thread ?? opened.meta, rows: value.rows, next: value.next, older: value.older, list: null,
        loaded: true, hasRead: true };
    });
    // Malformed: the read finished but nothing was read ("Not read yet.", never "No messages yet.").
    if (!page) opened.loaded = true;
    this.render();
    if (page) this.scrollToEnd();
  }

  private threadGone(why: string): void {
    if (!this.thread) return;
    this.thread.rows = [];
    this.thread.loaded = true;
    this.thread.hasRead = false;
    this.render();
    this.main.append(el("p", "muted center", why === "not found" ? "This chat is not open any more." : why));
  }

  /** The rows after the cursor; repeated while events keep coming. */
  async moreLog(): Promise<void> {
    if (!this.thread) return;
    if (this.logBusy) { this.logAgain = true; return; }
    this.logBusy = true;
    try {
      do {
        this.logAgain = false;
        const open: OpenThread | null = this.thread;
        if (!open) return;
        let more = true;
        while (more && this.thread === open) {
          const r = await this.read(`/m/api/threads/${open.id}/log?after=${open.next}`);
          if (!r || !("body" in r) || this.thread !== open) return;
          // decodeLog cuts a partial page before its first invalid line, so `next` never passes a line not applied.
          const page = this.applyRead(`threads/${open.id}/log`, decodeLog(r.body), (value) => {
            const firstRead = !open.hasRead;
            open.loaded = true;
            open.hasRead = true;
            const last = open.rows.length ? open.rows[open.rows.length - 1].seq : -1;
            const fresh = value.rows.filter((row) => row.seq > last);
            open.next = Math.max(open.next, value.next);
            if (value.thread) { open.meta = value.thread; this.renderOrb(); }
            more = value.more;
            if (firstRead) { open.rows.push(...fresh); this.render(); }   // "Not read yet." becomes the log
            else if (fresh.length) this.appendRows(open, fresh);
          });
          if (!page) return;
        }
      } while (this.logAgain);
    } finally {
      this.logBusy = false;
    }
  }

  private appendRows(open: OpenThread, rows: LogRow[]): void {
    const atEnd = this.nearEnd();
    open.rows.push(...rows);
    if (this.tab !== "chats" || !open.list) return;
    open.list.querySelector('[data-testid="m-empty"]')?.remove();
    for (const row of rows) open.list.append(logRow(row, ownerChat(open.meta)));
    if (atEnd) this.scrollToEnd();
  }

  loadTasks(): Promise<void> {
    return this.tasksReader.trigger();
  }

  private applyTasks(r: { body: unknown } | { error: string; status: number } | null): void {
    if (this.stopped || !r || !("body" in r)) return;
    const applied = this.applyRead("tasks", decodeTasks(r.body), ({ tasks, more }) => {
      this.tasks = tasks;
      this.tasksMore = more;
    });
    if (!applied) return;
    this.renderOrb();
    if (this.tab === "inbox" && !this.card) this.renderInbox();
    else if (this.tab === "tasks" && !this.task && !this.sheet) this.render();
  }

  /** Open a task, or read the open one again (its diff too, once shown; its folds kept). */
  async openTask(id: string): Promise<void> {
    if (this.task?.id !== id || this.tab !== "tasks") this.leftFrom("task", id);
    if (this.tab !== "tasks") this.taskBack = this.tab === "inbox" ? this.filter : "tasks";
    this.tab = "tasks";
    if (this.sheet?.kind === "task") this.sheet = null;
    if (this.task?.id !== id) this.task = { id, view: null, diff: null, folds: new Map() };
    const open: OpenTask = this.task;
    this.render();
    const r = await this.read(`/m/api/tasks/${id}`);
    if (!r || this.task !== open) return;
    if (!("body" in r)) {
      open.view = null;
      this.render();
      this.main.append(el("p", "muted center", r.status === 404 ? "This task is not shown on the phone." : r.error));
      return;
    }
    open.view = parseTask((r.body as { task?: unknown })?.task);
    if (open.diff && open.diff !== "loading") void this.loadDiff();
    this.render();
  }

  async loadDiff(): Promise<void> {
    const open: OpenTask | null = this.task;
    if (!open) return;
    if (open.diff === null) { open.diff = "loading"; this.render(); }
    const r = await this.read(`/m/api/tasks/${open.id}/diff`);
    if (this.task !== open) return;
    if (!r) { if (open.diff === "loading") open.diff = null; this.render(); return; }
    open.diff = "body" in r ? (parseDiff(r.body) ?? "The diff could not be read.") : (r.error[0]?.toUpperCase() ?? "") + r.error.slice(1) + ".";
    this.render();
  }

  /** Everything the screen shows, read again (after the stream reconnects, or coming back online). */
  refreshVisible(): void {
    void this.loadInbox();
    void this.loadReview();
    // PX-2: whenever the list is the view (and it is the first screen), working and tasks are read again
    // too: the stream reopened, coming back online and a resync all land here (Sol 6, finding 5).
    if (this.tab === "inbox" || this.tab === "tasks") {
      void this.loadWorking(true);
      void this.loadTasks();
    }
    if (this.tab === "chats") {
      if (this.thread) void this.moreLog();
      void this.loadChats();
    } else if (this.tab === "tasks") {
      if (this.task) void this.openTask(this.task.id);
    }
  }

  // -- the stream ----------------------------------------------------------------------

  private clearFirstOpenTimer(): void {
    if (this.firstOpenTimer !== null) this.clearTimer(this.firstOpenTimer);
    this.firstOpenTimer = null;
  }

  connect(): void {
    if (this.stopped) return;
    this.stream?.close();
    const stream = this.openStream(STREAM_URL);
    this.stream = stream;
    this.streamOpen = stream.readyState === 1;
    if (this.streamOpen) this.everOpened = true;
    // One launch deadline: replacing a stream must not restart the ten-second wait.
    if (!this.everOpened && !this.firstOpenTimedOut && this.firstOpenTimer === null) {
      this.firstOpenTimer = this.setTimer(() => {
        if (this.stopped || this.everOpened) return;
        this.firstOpenTimer = null;
        this.firstOpenTimedOut = true;
        this.renderOrb();
      }, 10_000);
    }
    this.renderOrb();
    stream.onopen = () => {
      if (this.stream !== stream) return;
      this.streamOpen = true;
      this.everOpened = true;
      this.clearFirstOpenTimer();
      // An open event stream is Jarvis's own 200: reached, through the one path.
      const recoveries = this.recoveries;
      this.report("reached", 200);
      this.renderOrb();
      // Anything published while the stream was down is read now (unless coming back online just did),
      // and the window's mirror (WS-5c rule 2).
      if (this.recoveries === recoveries) this.refreshVisible();
      void this.loadMe();
    };
    stream.onmessage = (ev) => {
      if (this.stream !== stream) return;
      const event = parseEvent(typeof ev.data === "string" ? ev.data : "");
      if (event) this.handle(event);
    };
    stream.onerror = () => {
      if (this.stream !== stream) return;
      this.streamOpen = false;
      this.renderOrb();
      void this.probe(stream);
    };
  }

  /** The stream dropped: is it the network, the session, or a passing break? */
  async probe(stream: StreamLike | null = this.stream): Promise<void> {
    // Its outcome reached the orb in `fetchJson` (a refusal leaves the orb on "Reconnecting…").
    const got = await this.fetchJson("/m/api/me");
    if (this.stopped) return;
    if (got.ok) {
      this.secure = parseSecure((got.body as { secure?: unknown })?.secure);
      this.renderOrb();
      this.unlock.fromMe(got.body);
    }
    if (stream && stream === this.stream && stream.readyState === CLOSED) {
      this.setTimer(() => { if (this.stream === stream) this.connect(); }, RETRY_MS);
    }
  }

  handle(event: StreamEvent): void {
    if (event.kind === "inbox_changed") {
      this.later("inbox", () => void this.loadInbox());
      this.later("review", () => void this.loadReview());
      this.workingSoon();
      if (this.card && event.ids.includes(this.card.rowId)) this.later("card", () => void this.loadCard());
      if (event.ids.some((id) => /^(task|prop|tq):/.test(id))) {
        this.later("tasks", () => void this.loadTasks());
      }
    } else if (event.kind === "working_changed") {
      this.workingSoon();
    } else if (event.kind === "thread_changed") {
      if (event.reason === "turn_started" || event.reason === "turn_finished") this.workingSoon();
      if (this.thread?.id === event.threadId) this.later("log", () => void this.moreLog());
      if (this.projects !== null) {
        const project = event.projectId;
        if (project) this.later("threads:" + project, () => void this.loadProjectThreads(project));
        else this.later("chats", () => void this.loadChats());
      }
    } else if (event.kind === "task_changed") {
      this.later("tasks", () => void this.loadTasks());
      // Checked again when the debounce fires: leaving the task (Chats, Review, the Inbox) meanwhile must not reopen it.
      // One debounce per task, scheduled only for the open task: an event for another task can never claim it.
      if (this.tab === "tasks" && this.task?.id === event.taskId) {
        this.later("task:" + event.taskId, () => { if (this.tab === "tasks" && this.task?.id === event.taskId) void this.openTask(event.taskId); });
      }
    } else {
      this.refreshVisible();
    }
  }

  private later(key: string, fn: () => void, ms = DEBOUNCE_MS): void {
    if (this.timers.has(key)) return;
    this.timers.set(key, this.setTimer(() => { this.timers.delete(key); if (!this.stopped) fn(); }, ms));
  }

  // -- online and offline ---------------------------------------------------------------

  setOnline(online: boolean, failStatus?: number): void {
    if (failStatus !== undefined) this.failStatus = failStatus;
    if (online === this.online) {
      if (!online) this.renderBanner();
      else this.renderOrb();
      return;
    }
    this.online = online;
    this.frame.dataset.stale = online ? "false" : "true";
    this.renderBanner();
    // The write controls (P-5) say offline and turn off, or come back.
    if (!this.card) this.renderKeepingFocus();
    if (online) this.recovered();
  }

  private recoverTimer: unknown = null;
  private recoverOwed = false;
  /** How many recovery refreshes ran (the stream's open reads only when coming back online did not). */
  private recoveries = 0;

  /**
   * Back online: what is visible is read again, at most once per RETRY_MS. Every request reports through
   * `report`, so one route the proxy keeps failing (a 504 on a slow route) beside routes that answer would
   * otherwise flip offline and online forever, each flip re-reading everything: a request storm. A flip
   * inside the window is owed and read once when it ends.
   */
  private recovered(): void {
    if (this.recoverTimer !== null) { this.recoverOwed = true; return; }
    this.recoveries += 1;
    this.refreshVisible();
    this.recoverTimer = this.setTimer(() => {
      this.recoverTimer = null;
      const owed = this.recoverOwed;
      this.recoverOwed = false;
      if (owed && this.online && !this.stopped) this.recovered();
    }, RETRY_MS);
  }

  private renderBanner(): void {
    this.banner.hidden = this.online;
    this.renderOrb();
    if (this.online) return;
    // UX-2: "last read 3 min ago" (the clock time is the banner's title); the minute tick redraws it.
    const ago = this.lastRead !== null ? agoWords(this.lastRead, this.now()) : "";
    this.banner.textContent = ago
      ? `Offline. Nothing here is current: it was last read ${ago}.`
      : "Offline. Nothing here could be read yet.";
    this.banner.title = this.lastRead !== null ? `Last read at ${absoluteWords(this.lastRead, this.now())}` : "";
  }

  /** List-header warnings survive unrelated successes, reconnects and every surface redraw. */
  private renderReadWarnings(): void {
    this.main.querySelectorAll(".m-resource-warning").forEach((node) => node.remove());
    const resources: [string, string, boolean][] = [];
    if (this.tab === "inbox") resources.push(
      ["inbox", "Inbox", this.inbox !== null], ["working", "Running", this.entries !== null],
      ["tasks", "Tasks", this.tasks !== null], ["review", "Morning review", this.review !== null]);
    if (this.tab === "review") resources.push(["review", "Morning review", this.review !== null]);
    if (this.tab === "tasks" && !this.task && !this.sheet) resources.push(["tasks", "Tasks", this.tasks !== null]);
    if (this.tab === "chats" && this.thread) resources.push(
      [`threads/${this.thread.id}/log`, "Chat log", this.thread.hasRead], ["inbox", "Inbox", this.inbox !== null]);
    const warnings = resources.filter(([resource]) => this.failedReads.has(`/m/api/${resource}`))
      .map(([resource, label, loaded]) => {
        const state = this.failedReads.get(`/m/api/${resource}`);
        const note = el("p", "m-error m-resource-warning", state === "partial"
          ? `${label} is incomplete: some items couldn't be read.`
          : `${label} couldn't refresh: ${loaded ? "showing the last read" : "not read yet"}.`);
        note.dataset.resource = resource;
        note.dataset.read = state === "partial" ? "partial" : "failed";
        note.dataset.stale = "true";
        note.dataset.testid = resource === "inbox" && !this.main.querySelector('[data-testid="m-list-error"]')
          ? "m-list-error" : "m-resource-stale";
        note.setAttribute("role", "status");
        return note;
      });
    // Under the filter strip, at the list header; transcript updates keep the same log and scroll.
    const surface = this.main.firstElementChild;
    if (surface?.classList.contains("view")) {
      const strip = surface.querySelector(":scope > .m-strip");
      if (strip) strip.after(...warnings); else surface.prepend(...warnings);
    } else this.main.prepend(...warnings);
  }

  // -- navigation (PX-4) -----------------------------------------------------------------------

  /** How many levels are open above the list: the approval sheet, a New chat or New task form, an open chat or task. */
  depth(): number {
    return (this.card ? 1 : 0) + (this.sheet ? 1 : 0) + (this.tab === "chats" && this.thread ? 1 : 0)
      + (this.tab === "tasks" && this.task ? 1 : 0);
  }

  /**
   * Back (the in-app button, the dim area, the sheet's back): the topmost level closes, now and in place, one
   * level per tap. The browser's own Back and swipe are not involved: no history entry is ever made.
   */
  goBack(): void {
    if (this.signOutOverlay) { this.signOutOverlay.close(); return; }
    if (this.menuOpen) { this.toggleMenu(null); return; }
    if (this.tab === "review" && !this.card) { this.show("inbox"); return; }
    if (this.card) { this.closeCard(); return; }
    if (this.sheet) {
      if (this.sheet.kind === "chat") { this.sheet = null; this.render(); void this.loadChats(); } else this.show("tasks");
      return;
    }
    if (this.tab === "chats" && this.thread) this.closeThread();
    else if (this.tab === "tasks" && this.task) this.show("tasks");
  }

  /** An open chat's back: to the view it was opened from, and that view as the owner left it. */
  private closeThread(): void {
    const to = this.threadBack;
    this.threadBack = "chats";
    this.thread = null;
    this.show(to);
  }

  /** Open or close a ⋯ menu (`null` closes whichever is open). The focus follows: into its panel, back to its button. */
  toggleMenu(which: "app" | "task" | "thread" | null): void {
    const was = this.menuOpen;
    if (which === was) return;
    const inside = typeof document !== "undefined" && !!(document.activeElement as HTMLElement | null)?.closest?.(".m-menu");
    this.menuOpen = which;
    if (was === "app" || which === "app") this.renderMenu();
    if (was === "task" || which === "task" || was === "thread" || which === "thread") this.render();
    if (typeof document === "undefined") return;
    const id = (which ?? was) === "task" ? "m-task-more" : (which ?? was) === "thread" ? "m-thread-more" : "m-menu";
    if (which !== null) {
      this.frame.querySelector<HTMLElement>(`[data-testid="${id}-panel"] button`)?.focus();
    } else if (inside) {
      this.frame.querySelector<HTMLElement>(`[data-testid="${id === "m-menu" ? "m-lock-pill" : id}"]`)?.focus();
    }
  }

  /** The pill's two-row popover: Lock now and Settings (§3.1). */
  private renderMenu(): void {
    const open = this.menuOpen === "app";
    const items: MenuItem[] = [];
    if (open) {
      // Always offered: the pill is only the phone's copy, so Lock now never waits on it.
      items.push({ id: "lock", testid: "m-lock-now", label: "Lock now", onSelect: () => void this.lockNow() });
      items.push({ id: "settings", testid: "m-menu-settings", label: "Settings", onSelect: () => {
        this.show("settings");
        this.main.querySelector<HTMLElement>('[data-testid="m-settings-unlock"]')?.closest<HTMLElement>("section")?.scrollIntoView?.({ block: "nearest" });
        this.main.querySelector<HTMLElement>('[data-testid="m-lock-now"]')?.focus();
      } });
    }
    const next = menuView({ id: "m-menu", label: "Menu", items, open, toggle: (o) => this.toggleMenu(o ? "app" : null) });
    // Reuse the existing rows but discard the old ⋯ opener. The stable pill is the sole opener.
    next.querySelector(".m-more")?.remove();
    next.prepend(this.pill);
    const panel = next.querySelector<HTMLElement>('[data-testid="m-menu-panel"]');
    if (panel) panel.id = "m-menu-panel";
    next.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        this.toggleMenu(null);
        this.pill.focus();
      }
      if (event.key === "Tab" && open) {
        const buttons = [...next.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }, true);
    this.pill.setAttribute("aria-expanded", String(open));
    this.menuWrap.replaceWith(next);
    this.menuWrap = next;
    this.menuInert();
  }

  private menuInert(): void {
    const open = this.menuOpen === "app" || !!this.signOutOverlay;
    if (this.signOutOverlay) this.topBar.setAttribute("inert", "");
    for (const node of [this.brand, this.main, this.tabBar, this.banner]) {
      if (open || this.card) node.setAttribute("inert", ""); else node.removeAttribute("inert");
    }
  }

  /** The view the main area shows now, as a key: scroll is remembered per key (§3.1, D4). */
  private surfaceKey(): string {
    if (this.tab === "settings") return "settings";
    if (this.tab === "review") return "review";
    if (this.tab === "inbox") return "inbox:" + (this.filter ?? "");
    if (this.tab === "chats") return this.sheet?.kind === "chat" ? "chat-sheet" : this.thread ? "thread:" + this.thread.id : "chats";
    return this.sheet?.kind === "task" ? "task-sheet" : this.task ? "task:" + this.task.id : "tasks";
  }

  /** Remember the row a list view is left from, to come back to it (the list's own attribute names it). */
  private leftFrom(kind: "thread" | "task", id: string): void {
    const key = this.drawnKey;
    if (!key) return;
    if (key === "chats" || key === "inbox:tasks") this.mem.leftFrom(key, "data-id", id);
    else if (key.startsWith("inbox:")) this.mem.leftFrom(key, kind === "thread" ? "data-thread" : "data-task", id);
  }

  /** Back on a list: the row opened from it visible and focused (its scroll is restored by `render`). */
  private returnToRow(key: string): void {
    const from = this.mem.row(key);
    if (!from) return;
    this.mem.clearRow(key);
    const card = byAttr(this.main, from.attr, from.value);
    const target = card?.querySelector<HTMLElement>("button") ?? card;
    if (!card || !target) return;
    this.rowScroll.returnTo(card);
    target.focus({ preventScroll: true });
  }

  // -- drawing ---------------------------------------------------------------------------

  show(tab: Tab): void {
    // Morning review is a level above the Inbox: return to its line, including after live redraws.
    if (tab === "review" && this.tab === "inbox" && this.drawnKey) {
      this.mem.leftFrom(this.drawnKey, "data-testid", "m-review-open");
    }
    this.signOutOverlay?.close();
    this.card = null;
    if (this.menuOpen) { this.menuOpen = null; this.renderMenu(); }
    // PX-2: Tasks is a filter of the list, not a tab. Asking for it (a task's back, a closed sheet) shows that list.
    if (tab === "tasks") {
      this.task = null;
      this.sheet = null;
      this.card = null;
      this.filter = this.taskBack;
      this.taskBack = "tasks";
      this.tab = "inbox";
      this.render();
      void this.loadInbox();
      void this.loadTasks();
      return;
    }
    if (tab === "inbox" && this.tab === "tasks") { this.task = null; this.sheet = null; this.filter = null; this.taskBack = "tasks"; }
    else if (tab === "inbox" && this.tab === "inbox" && !this.card) this.filter = null;
    if (tab === this.tab) {
      // A second tap on the open tab goes back to its list.
      if (tab === "chats") this.thread = null;
      this.sheet = null;
    } else if (this.sheet && this.sheet.kind !== (tab === "chats" ? "chat" : "")) {
      this.sheet = null;
    }
    // Entering Morning review: nothing ticked, nothing confirmed (review of #329, MED 2).
    if (tab === "review" && this.tab !== "review") this.resetReviewSelection();
    this.tab = tab;
    this.render();
    if (tab === "inbox") { void this.loadInbox(); void this.loadWorking(true); void this.loadTasks(); }
    if (tab === "review") void this.loadReview();
    if (tab === "chats" && !this.thread) void this.loadChats();
  }

  private canOpenDrawer(): boolean {
    return !this.stopped && !this.card && !this.sheet && !this.signOutOverlay
      && !(this.tab === "chats" && this.thread) && ["inbox", "chats", "settings"].includes(this.tab);
  }

  private openDrawer(): void {
    if (this.stopped || this.card || this.sheet || this.signOutOverlay) return;
    if (this.menuOpen) this.toggleMenu(null);
    this.drawer?.open();
  }

  private drawerState() {
    const connection = this.brand.querySelector<HTMLElement>('[data-testid="m-conn"]')!;
    return {
      section: this.tab === "inbox" && this.filter === "tasks" ? "tasks" as const : this.tab,
      connection: { state: connection.dataset.state, tone: connection.dataset.tone, word: connection.textContent ?? "Connecting…" } as ReturnType<typeof orbState>,
      lastRead: this.lastRead === null ? "not read yet" : absoluteWords(this.lastRead, this.now()),
      projects: this.projects, threads: this.threads,
      needs: new Set([...(this.inbox ?? []).filter(waiting).flatMap((r) => r.thread_id ? [r.thread_id] : []),
        ...(this.entries ?? []).filter((e) => e.status === "needs_input").map((e) => e.thread_id)]),
      running: new Set((this.entries ?? []).filter((e) => e.status === "working").map((e) => e.thread_id)),
      count: this.inbox ? waitingCount(this.inbox) : 0,
      review: reviewReachable(this.review) ? this.review!.open : 0,
      unlock: pillLabel(this.unlock.mirror, this.now()), unlocked: this.unlock.mirror.open(), lockNote: this.lockNote,
    };
  }

  private confirmSignOut(): void {
    if (this.signOutOverlay) return;
    this.signOutOverlay = signOutSheet(this.frame, () => void this.signOut(), () => {
      this.signOutOverlay = null;
      this.menuInert();
    }, this.main.querySelector<HTMLElement>('[data-testid="m-signout"]'));
  }

  private settingsState(): SettingsState {
    const word = this.brand.querySelector<HTMLElement>('[data-testid="m-conn"]');
    const quiet = this.review?.quiet;
    return {
      connection: word?.textContent ?? "Connecting…", tone: word?.dataset.tone ?? "ok",
      connectionState: word?.dataset.state ?? "connecting",
      lastRead: this.lastRead === null ? "Not read yet" : absoluteWords(this.lastRead, this.now()),
      security: this.secure ? this.secure.ok ? "Secure: full answers allowed" : `Limited: ${this.secure.label || "a check failed"}` : "Checking…",
      limited: !!this.secure && !this.secure.ok,
      unlock: pillLabel(this.unlock.mirror, this.now()), unlocked: this.unlock.mirror.open(), lockNote: this.lockNote,
      quiet: quiet?.enabled === false ? "Quiet hours are off."
        : `No pushes ${quiet?.start ?? "23:00"} to ${quiet?.end ?? "07:00"}. What waits is in the morning review.`,
    };
  }

  private refreshSettings(): void {
    if (this.tab !== "settings" || !this.main || !this.unlock) return;
    const view = this.main.querySelector<HTMLElement>('[data-testid="m-settings"]');
    if (view) updateSettings(view, this.settingsState());
  }

  private refreshNotifications(): void {
    const old = this.main.querySelector<HTMLElement>('[data-testid="m-notify"]');
    if (!old) return;
    const focused = old.contains(document.activeElement);
    const next = this.notify.view();
    old.replaceWith(next);
    if (focused) {
      next.tabIndex = -1;
      (next.querySelector<HTMLElement>("button") ?? next).focus();
    }
  }

  private renderBadge(): void {
    this.refreshSettings();
    this.drawer?.refresh();
    const n = this.inbox ? waitingCount(this.inbox) : 0;
    const icon = this.tabs.inbox.querySelector(".m-tab-icon")!;
    let badge = icon.querySelector<HTMLElement>(".m-tab-badge");
    if (n) {
      if (!badge) { badge = el("span", "m-tab-badge"); badge.setAttribute("aria-hidden", "true"); icon.append(badge); }
      badge.textContent = String(n);
    } else badge?.remove();
    this.tabs.inbox.setAttribute("aria-label", n ? `Inbox, ${n} need you` : "Inbox");
  }

  /**
   * AS (review of #330, LOW 3): one redraw when a pending summary on the
   * open card or an inbox row goes stale, so a missed update never leaves a
   * command collapsed (lib/approvalSummary `staleAt`).
   */
  private staleTimer: ReturnType<typeof setTimeout> | null = null;
  private wakeForStale(): void {
    if (this.staleTimer) clearTimeout(this.staleTimer);
    this.staleTimer = null;
    const now = Date.now();
    const at = [staleAt(this.card?.detail?.card),
                ...(this.inbox ?? []).filter((row) => row.kind === "approval").map((row) => staleAt(row.detail))]
      .filter((t): t is number => t !== null && t > now).sort((a, b) => a - b)[0];
    if (at === undefined) return;
    this.staleTimer = setTimeout(() => { this.staleTimer = null; if (!this.stopped) this.renderKeepingFocus(); },
      Math.min(2 ** 31 - 1, at - now + 50));
  }

  /** M13: the card last drawn, so focus moves to its heading when one opens and back to its row when it closes. */
  private drawnCard: string | null = null;
  /** M5: an Inbox row to scroll to and focus after the next read. */
  private pendingRow: string | null = null;
  /** The card's one status node, kept across redraws so VoiceOver announces its changes (review of #338, LOW). */
  private cardNotice: HTMLElement | null = null;
  /** After a cancelled Face ID or a refusal: the focus goes to the notice, not the page (review of #338, LOW). */
  private focusNotice = false;
  /** The control that opened the sheet (its test id), so closing returns to it wherever it lives (the list, the review). */
  private invoker = "m-item-review";
  private deadlineTimer: ReturnType<typeof setTimeout> | null = null;
  private timeTimer: ReturnType<typeof setTimeout> | null = null;

  /** UX-2: once a minute, the text of every relative-time label (and the offline banner) is rewritten
   * in place. Not a redraw: no list, card or focus is touched, and nothing runs per second. */
  private tickTimes(): void {
    if (this.timeTimer !== null || this.stopped) return;
    this.timeTimer = setTimeout(() => {
      this.timeTimer = null;
      if (this.stopped) return;
      refreshTimes(this.main, this.now());
      if (!this.online) this.renderBanner();
      this.renderPill();
      this.tickTimes();
    }, TIME_TICK_MS);
  }

  /** After a draw: a card that just opened takes the focus on its heading (in the sheet); one that just closed
   * gives it to the line saying what happened, or back to its row's button. */
  private moveFocus(kept: string | null): void {
    const now = this.card ? this.card.rowId : null;
    const was = this.drawnCard;
    this.drawnCard = now;
    if (now === was) {
      if (now && this.focusNotice) {
        this.focusNotice = false;
        const notice = this.sheetHost.querySelector<HTMLElement>('[data-testid="m-card-notice"]');
        if (notice) { notice.tabIndex = -1; notice.focus({ preventScroll: true }); return; }
      }
      if (kept) this.restoreFocus(kept);
      return;
    }
    this.focusNotice = false;
    if (now) {
      this.sheetHost.querySelector<HTMLElement>('[data-testid="m-card-heading"]')?.focus();
      this.tickDeadline();
    } else if (was) {
      // Answered: the line saying what happened ("Denied."); else (back, or gone) the row's own button.
      const flash = this.main.querySelector<HTMLElement>('[data-testid="m-flash"]');
      const holder = Array.from(this.main.querySelectorAll<HTMLElement>("[data-id]")).find((n) => n.dataset.id === was
        && n.querySelector(`[data-testid="${this.invoker}"]`));
      const row = holder?.querySelector<HTMLElement>(`[data-testid="${this.invoker}"]`);
      if (flash) { flash.tabIndex = -1; flash.focus({ preventScroll: true }); }
      else row?.focus({ preventScroll: true });
    }
  }

  /** What had the focus before a redraw, when it is one of the few things focus is kept on. */
  private focusedKey(): string | null {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (!(active instanceof HTMLElement) || !(this.main.contains(active) || this.sheetHost.contains(active))) return null;
    const id = active.dataset.testid ?? "";
    if (!KEEP_FOCUS.has(id)) return null;
    return id + "\n" + (active.closest<HTMLElement>("[data-id]")?.dataset.id ?? "") + "\n"
      + (active.closest<HTMLElement>("[data-key]")?.dataset.key ?? "");
  }

  /** The same element in the redrawn view takes the focus back (a redraw builds new nodes). */
  private restoreFocus(key: string): void {
    const [id, rowId, cardKey] = key.split("\n");
    const same = [...Array.from(this.sheetHost.querySelectorAll<HTMLElement>(`[data-testid="${id}"]`)),
      ...Array.from(this.main.querySelectorAll<HTMLElement>(`[data-testid="${id}"]`))]
      .find((n) => (n.closest<HTMLElement>("[data-id]")?.dataset.id ?? "") === rowId
        && (n.closest<HTMLElement>("[data-key]")?.dataset.key ?? "") === cardKey);
    if (same && document.activeElement !== same) {
      if (id === "m-flash") same.tabIndex = -1;
      same.focus({ preventScroll: true });
    }
  }

  /** L1: the open card's "N min left", redrawn in place (its text only) while the card is open. */
  private tickDeadline(): void {
    if (this.deadlineTimer !== null) return;
    // A real timer, as wakeForStale's: the injected one runs at once in the tests.
    this.deadlineTimer = setTimeout(() => {
      this.deadlineTimer = null;
      if (this.stopped || !this.card) return;
      const line = this.sheetHost.querySelector('[data-testid="m-card-deadline"]');
      const card = this.card.detail?.card;
      if (line && card) {
        const due = deadlineAt(card);
        line.textContent = deadlineWords(due, this.now());
        if (due !== null) (line as HTMLElement).title = `Denies automatically at ${absoluteWords(due, this.now())}`;
      }
      this.tickDeadline();
    }, DEADLINE_TICK_MS);
  }

  /** M5: a task's question, answered where the phone answers it: the Inbox, at its row. */
  showInboxRow(rowId: string): void {
    this.card = null;
    this.pendingRow = rowId;
    this.show("inbox");
    this.revealRow(rowId);
  }

  /** The Inbox row asked for, scrolled to and focused (its article, tabIndex -1: no keyboard rises). */
  private revealRow(rowId: string): void {
    const row = this.inboxRowEl(rowId);
    if (!row) return;
    row.tabIndex = -1;
    this.rowScroll.jump(row);
    row.focus({ preventScroll: true });
  }

  /** The drawn inbox row with this id (matched as data, never spliced into a selector). */
  private inboxRowEl(rowId: string): HTMLElement | undefined {
    return Array.from(this.main.querySelectorAll<HTMLElement>('[data-testid="m-inbox-row"]')).find((r) => r.dataset.id === rowId);
  }

  /** WS-5c rule 6: an "Unlock and send" is dropped once its sheet, item, confirmation or page has gone. */
  private dropGoneFallbacks(): void {
    for (const [scope, action] of [...this.tapAgain]) {
      let alive: boolean;
      if (action === "allow" || action === "always") alive = this.card?.rowId === scope && (action === "allow" || !!this.card.rule);
      else if (scope === "review") alive = this.tab === "review" && this.reviewConfirming;
      else if (scope.startsWith("task:")) {
        const id = scope.slice(5);
        alive = this.tab === "tasks" && this.task?.id === id && this.taskWrite?.id === id && this.taskWrite.confirming === "cancel";
      } else if (scope.startsWith("thread:")) alive = this.tab === "chats" && this.thread?.id === scope.slice(7);
      else {
        const row = this.inbox?.find((r) => r.id === scope) ?? (this.review ? allItems(this.review).find((i) => i.row.id === scope)?.row : undefined);
        alive = !!row;
        // A PR action's frozen head and card must still be the row's: a changed row is a new decision.
        const body = this.frozen.get(scope)?.body;
        if (row && body && typeof body.head === "string") {
          const view = prRowOf(row);
          alive = !!view && view.head === body.head && view.card === body.card;
        }
      }
      if (!alive) { this.tapAgain.delete(scope); this.frozen.delete(scope); }
    }
  }

  render(): void {
    this.wakeForStale();
    // Before anything is built: a reused card (threadsView) is moved out of the old view as the new one is made, which blurs it.
    const kept = this.focusedKey();
    for (const [tab, b] of Object.entries(this.tabs) as [Tab, HTMLButtonElement][]) {
      // A task's own pages belong to the Inbox (Tasks is a filter of it, not a tab).
      const active = tab === this.tab || (tab === "inbox" && (this.tab === "tasks" || this.tab === "review"));
      if (active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    }
    this.renderBadge();
    // A task page's ⋯ is open only while that page is; an open chat's likewise.
    if (this.menuOpen === "task" && !(this.tab === "tasks" && this.task)) this.menuOpen = null;
    if (this.menuOpen === "thread" && !(this.tab === "chats" && this.thread && !this.sheet)) this.menuOpen = null;
    if (this.threadDone && this.thread?.id !== this.threadDone.id && !this.threadDone.busy) this.threadDone = null;
    this.dropGoneFallbacks();
    if (this.card) this.card.tapAgain = (this.tapAgain.get(this.card.rowId) as "allow" | "always" | undefined) ?? null;
    this.renderPill();
    this.renderOrb();
    const key = this.surfaceKey();
    // PX-4: while the approval sheet is open, the view under it stays as it was drawn: its scroll, rows and
    // order are exactly what the owner left, and closing the sheet draws it again in place.
    if (!(this.card && this.held === key && this.drawnKey === key)) this.drawSurface(key);
    this.held = this.card ? key : null;
    this.drawSheet();
    this.moveFocus(kept);
    this.drawer?.refresh();
  }

  /** The approval sheet over the view (§3.3): `cardView`, unchanged, in a bottom sheet; nothing behind it takes a tap or Tab. */
  private drawSheet(): void {
    const open = !!this.card;
    for (const node of [this.topBar, this.banner, this.main, this.tabBar]) {
      if (open) node.setAttribute("inert", ""); else node.removeAttribute("inert");
    }
    this.menuInert();
    if (open) this.frame.dataset.sheet = "open"; else delete this.frame.dataset.sheet;
    if (!this.card) { this.sheetHost.replaceChildren(); return; }
    // Captured as drawn: AUTHORIZE answers this card, Confirm ALWAYS sends this rule's preview.
    const card = this.card;
    const rule = card.rule;
    // Opus L5: this row's earlier gated answer is still out (the sheet was closed and opened again): every
    // answer stays off until it settles, so a second AUTHORIZE cannot mint a second op.
    const drawn: CardState = !card.busy && this.answering.has(card.rowId)
      ? { ...card, busy: true, progress: card.progress || STILL_ANSWERING } : card;
    const view = cardView(drawn, {
      back: () => this.goBack(),
      deny: () => void this.denyCard(),
      allow: () => void this.allowCard(card),
      always: () => void this.alwaysCard(),
      confirmAlways: () => void this.confirmAlways(card, rule),
      cancelAlways: () => this.cancelAlways(),
      // AS: the owner's own open or close of the command, kept across a re-read of the card.
      toggleCommand: (o) => { if (this.card) { this.card.expanded = o; this.render(); } },
    }, this.now(), this.online);
    // The same status node across redraws of this card: only its words and tone change.
    const fresh = view.querySelector<HTMLElement>('[data-testid="m-card-notice"]');
    const old = this.cardNotice;
    const same = this.drawnCard === this.card.rowId;
    if (fresh && old && same) {
      if (old.textContent !== fresh.textContent) old.textContent = fresh.textContent;
      old.dataset.tone = fresh.dataset.tone ?? "";
      fresh.replaceWith(old);
    } else {
      this.cardNotice = fresh;
    }
    const top = same ? this.sheetHost.querySelector<HTMLElement>(".m-sheet")?.scrollTop ?? 0 : 0;
    this.sheetHost.replaceChildren(...sheetView(view, () => { if (this.card && !this.card.busy) this.goBack(); }));
    const sheet = this.sheetHost.querySelector<HTMLElement>(".m-sheet");
    if (sheet && top) sheet.scrollTop = top;
  }

  /** What the inbox rows draw with, for the list and for an open chat's tray. */
  private inboxAnswers(): InboxAnswers {
    return {
      openCard: (row) => this.openCard(row),
      send: (row, body, echo) => void this.sendAnswer(row, body, echo),
      retry: (row) => void this.retryDeferred(row),
      tapAgain: this.tapAgain,
      busy: this.answering, notes: this.notes, drafts: this.drafts, picks: this.picks,
      expanded: this.opened,
      toggle: (row, open) => { this.opened.set(row.id, open); this.render(); },
      withdraw: {
        confirming: this.withdrawing,
        ask: (row) => { this.withdrawing.add(row.id); this.notes.delete(row.id); this.render(); },
        back: (row) => { this.withdrawing.delete(row.id); this.render(); },
      },
      pr: {
        act: (row, action) => void this.prAct(row, action),
        cancelFix: (row) => void this.cancelFix(row),
        question: (rowId, taskId) => {
          if (rowId) this.revealRow(rowId);
          else if (taskId) void this.openTask(taskId);
        },
        limited: !!this.secure && !this.secure.ok,
        progress: this.prProgress,
        rows: this.inbox ?? [],
      },
    };
  }

  /** The main area: the list, the review, a chat, a task. The scroll each view had is remembered (D4). */
  private drawSurface(key: string): void {
    const redraw = this.drawnKey === key;
    if (this.drawnKey && !redraw) this.mem.save(this.drawnKey, this.main.scrollTop);
    const top = this.main.scrollTop;
    let view: HTMLElement;
    if (this.tab === "settings") {
      if (redraw && this.main.querySelector('[data-testid="m-settings"]')) {
        this.refreshSettings();
        return;
      }
      view = settingsView(this.settingsState(), this.notify.view(), {
        lock: () => void this.lockNow(), signOut: () => this.confirmSignOut(),
      });
    } else if (this.tab === "review") {
      view = reviewView({
        report: this.review, selection: this.reviewSel, notes: this.notes, receipts: this.reviewReceipts,
        busy: this.answering, groupBusy: this.reviewBusy, confirming: this.reviewConfirming, flash: this.reviewFlash,
        drafts: this.drafts, picks: this.reviewPicks, withdrawing: this.withdrawing, online: this.online,
        opened: this.opened, confirmLines: this.reviewConfirmed ? this.reviewConfirmLines : null,
        unlocked: this.unlock.mirror.open(), tapAgain: this.tapAgain,
      }, { ...this.reviewActions(), expand: (row, open) => { this.opened.set(row.id, open); this.render(); } }, this.secure && !this.secure.ok ? `limited: ${this.secure.label || "a check failed"}`
        : this.review?.group && !this.review.group.ok ? (this.review.group.why || "limited") : null);
      view.prepend(tap("‹ Inbox", () => this.goBack(), "quiet", "m-review-back"));
    } else if (this.tab === "inbox") {
      const answers = this.inboxAnswers();
      const open = (row: InboxRow) => {
        if (row.thread_id) void this.openThread(row.thread_id);
        else if (row.task_id) void this.openTask(row.task_id);
      };
      const tasksPanel = this.filter === "tasks" ? this.tasksPanel() : null;
      view = threadsView({
        rows: this.inbox, entries: this.entries, working: this.workingState, workingCut: this.workingCut,
        tasks: this.tasks, tasksMore: this.tasksMore, inboxMore: this.inboxMore, filter: this.filter,
        recentAll: this.recentAll, error: this.listError,
      }, {
        answers, openRow: open,
        openThread: (id) => void this.openThread(id),
        openTask: (id) => void this.openTask(id),
        setFilter: (f) => { this.filter = f; this.render(); if (f === "tasks") void this.loadTasks(); },
        setRecentAll: (all) => {
          this.recentAll = all;
          try { localStorage.setItem(RECENT_KEY, all ? "1" : "0"); } catch { /* no storage: it just does not last */ }
          this.render();
        },
        retry: () => { void this.loadInbox(); },
      }, this.slots, this.now(), tasksPanel, this.secure);
      // RD-2: Morning review stays reachable under every Inbox filter; Notifications lives in Settings.
      const line = reviewLine(this.review, () => this.show("review"));
      if (line) {
        const first = view.querySelector(".m-band");
        if (first) first.before(line); else view.append(line);
      }
      if (this.flash) {
        const line = el("p", "m-notice", this.flash);
        line.dataset.testid = "m-flash";
        line.setAttribute("role", "status");
        view.prepend(line);
      }
    } else if (this.tab === "chats") {
      if (this.sheet?.kind === "chat") {
        view = chatSheetView(this.sheet, this.projects ?? [], this.models, this.online, {
          close: () => this.goBack(),
          submit: () => void this.createChat(),
          change: () => this.render(),
          openChat: (id) => void this.openThread(id),
        });
      } else if (this.thread) {
        // Drawn again in place: at the bottom it stays there; scrolled up, it is not moved.
        if (this.main.querySelector('[data-testid="m-transcript"]')) this.keepScroll = this.nearEnd() ? "end" : this.main.scrollTop;
        const thread = this.thread;
        const built = transcriptView(thread.meta, thread.rows, thread.older, () => this.goBack(),
          this.composer(thread.id), { loaded: thread.loaded, online: this.online, read: thread.hasRead });
        const back = built.view.querySelector('[data-testid="m-back"]');
        if (back) back.textContent = this.threadBack === "inbox" ? "‹ Inbox" : this.threadBack === "review" ? "‹ Review" : "‹ Chats";
        thread.list = built.list;
        view = built.view;
        // PX-4 (§3.4): this chat's rank 1 to 5 rows, in a tray under the header.
        const answers = this.inboxAnswers();
        const tray = trayView({
          rows: (this.inbox ?? []).filter((r) => r.thread_id === thread.id),
          draw: (row) => inboxRow(row, () => this.scrollToEnd(), answers),
          folded: this.trayFolded,
          fold: (f) => { this.trayFolded = f; this.render(); },
        });
        if (tray) view.querySelector(".subbar")?.after(tray);
        // WS-5c §5.6: Done (and its Undo) in the open chat's ⋯, for a chat in the In progress list.
        const doneBox = this.doneControls(thread.id);
        if (doneBox.menu) view.querySelector(".subbar")?.append(doneBox.menu);
        if (doneBox.note) view.querySelector(".subbar")?.after(doneBox.note);
      } else {
        view = chatsView(this.projects, this.threads, this.expanded, {
          open: (t) => void this.openThread(t.id, t),
          toggle: (id) => {
            if (this.expanded.has(id)) this.expanded.delete(id); else this.expanded.add(id);
            this.render();
          },
          fold: (id, folded) => this.folds.set(id, folded),
        }, {
          folds: this.folds,
          needs: new Set([...(this.inbox ?? []).filter(waiting).flatMap((r) => r.thread_id ? [r.thread_id] : []),
            ...(this.entries ?? []).filter((e) => e.status === "needs_input").map((e) => e.thread_id)]),
          running: new Set((this.entries ?? []).filter((e) => e.status === "working").map((e) => e.thread_id)),
        });
        view.querySelector('[data-testid="m-chats-toolbar"]')?.append(
          tap("+ New chat", () => void this.openNewChat(), "quiet primary m-new", "m-new-chat-open"));
      }
    } else if (this.sheet?.kind === "task") {
      view = taskSheetView(this.sheet, this.projects ?? [], this.online, {
        close: () => this.goBack(),
        submit: () => void this.createTask(),
        change: () => this.render(),
      });
    } else if (this.task) {
      const open = this.task;
      // A proposal still inside its grace window starts on its own: no Start
      // (the runner refuses it there too); Withdraw is on its inbox row.
      const proposed = !!this.inbox?.some((row) => row.id === "prop:" + open.id);
      const allowed = open.view ? verbsFor(open.view.state).filter((v) => !(proposed && v === "start")) : [];
      const controls = open.view && allowed.length ? (() => {
        const w = this.taskState(open.id);
        const box = taskControlsView(open.view!, {
          confirming: w.confirming, busy: w.busy, note: w.note, online: this.online,
          ask: (verb) => this.askVerb(open.id, verb), back: () => this.backVerb(open.id),
          confirm: (verb) => void this.confirmVerb(open.id, verb),
          locked: !this.unlock.mirror.open(), tapAgain: this.tapAgain.get("task:" + open.id) === "cancel",
          // PX-4: Cancel is in this page's ⋯ menu.
          menu: { open: this.menuOpen === "task", toggle: (o) => this.toggleMenu(o ? "task" : null) },
        }, allowed);
        if (proposed) {
          box.prepend(el("p", "m-notice", "Proposed: it starts on its own when its grace window ends. Withdraw it from the inbox."));
        }
        return box;
      })() : (this.taskWrite?.id === open.id && this.taskWrite.note
        ? el("p", "m-notice", this.taskWrite.note) : null);
      view = open.view
        ? taskView(open.view, open.diff, { back: () => this.goBack(), showDiff: () => void this.loadDiff(),
          folds: open.folds, controls, answerInInbox: (index) => this.showInboxRow(`tq:${open.id}:${index}`),
          inInbox: (index) => !!this.inbox?.some((row) => row.id === `tq:${open.id}:${index}`) })
        : el("p", "empty", this.online ? "Loading…" : "Not read yet: you're offline.");
      // The ⋯ sits in the page's header, at the right of its title.
      const menu = controls?.querySelector<HTMLElement>(".m-menu");
      if (menu) view.querySelector(".subbar")?.append(menu);
    } else {
      view = this.tasksPanel();
    }
    this.main.replaceChildren(view);
    this.renderReadWarnings();
    this.rowScroll.sync();
    const keep = this.keepScroll;
    this.keepScroll = null;
    if (keep === "end") this.scrollToEnd();
    else if (keep !== null) this.main.scrollTop = keep;
    else this.main.scrollTop = redraw ? top : this.mem.top(key);
    this.drawnKey = key;
    if (!redraw) this.returnToRow(key);
  }


  /** The open chat's ⋯ (Done) and Done's line with its Undo while its window lasts. */
  private doneControls(id: string): { menu: HTMLElement | null; note: HTMLElement | null } {
    const st = this.threadDone?.id === id ? this.threadDone : null;
    const entry = this.doneEntry(id);
    const undoing = !!st?.token && this.now() < st.until;
    const pending = this.tapAgain.get("thread:" + id);
    let menu: HTMLElement | null = null;
    if (entry && entry.seq !== null && !undoing) {
      // The seq this item is drawn with is the one its tap sends (Sol MED 1): captured here, never re-read.
      const seq = entry.seq;
      menu = menuView({
        id: "m-thread-more", label: "Chat actions", open: this.menuOpen === "thread",
        toggle: (o) => this.toggleMenu(o ? "thread" : null),
        items: [{ id: "done", testid: "m-thread-done", label: pending === "done" ? UNLOCK_AND_SEND : "Done",
          disabled: !!st?.busy || !this.online, onSelect: () => void this.doneThread(id, seq) }],
      });
    }
    let note: HTMLElement | null = null;
    if (st && (st.note || undoing)) {
      note = el("div", "m-thread-done");
      note.dataset.testid = "m-thread-done-note";
      const line = el("p", "m-notice", st.note);
      line.setAttribute("role", "status");
      note.append(line);
      if (undoing) {
        const token = st.token;
        const undo = tap(pending === "undo" ? UNLOCK_AND_SEND : "Undo", () => void this.undoThread(id, st, token), "quiet m-send", "m-thread-undo");
        undo.disabled = st.busy || !this.online;
        note.append(undo);
      }
    }
    return { menu, note };
  }

  /** The Tasks filter's body: today's task list, headed by "New task", and the cap's notice. */
  private tasksPanel(): HTMLElement {
    const view = tasksView(this.tasks, (t) => void this.openTask(t.id));
    if (this.tasksMore) {
      const more = el("p", "m-band-note", "Showing the newest 200 tasks: older ones, running ones included, are at the desk");
      more.dataset.testid = "m-tasks-more-list";
      view.append(more);
    }
    view.prepend(tap("New task", () => this.openNewTask(), "quiet m-new", "m-new-task-open"));
    return view;
  }

  private reviewActions(): ReviewActions {
    // The confirmed set this draw shows: the confirm button sends exactly it (never a later re-read).
    const confirmed = this.reviewConfirmed;
    return {
      toggle: (item) => {
        if (this.reviewBusy) return;
        this.reviewSel = toggled(this.reviewSel, item.row.id, item);
        // A tick changed under the confirmation: it closes; the set must be confirmed again.
        this.reviewConfirming = false;
        this.reviewConfirmed = null;
        this.notes.delete(item.row.id);
        this.render();
      },
      approveAsk: () => {
        const body = !this.reviewBusy && this.review ? approveBody(this.review, this.reviewSel) : null;
        if (!body) return;
        this.reviewConfirmed = body.items;
        // Frozen for display while it is open (review of #330, LOW 4).
        this.reviewConfirmLines = confirmationList(this.review!, new Set(body.items.map((c) => c.id)));
        this.reviewConfirming = true;
        this.render();
      },
      approveConfirm: () => void this.approveSelected(confirmed),
      approveCancel: () => { if (!this.reviewBusy) { this.reviewConfirming = false; this.reviewConfirmed = null; this.render(); } },
      leaveSelected: () => void this.leaveSelected(),
      retry: (row) => void this.retryDeferred(row).then(() => {
        // Its words show here, on the review (retryDeferred writes the Inbox's line).
        if (this.flash) { this.reviewFlash = this.flash; this.flash = ""; }
        this.render();
        void this.loadReview();
      }),
      leaveOne: (row) => void this.leaveInReview(row),
      send: (item, body) => void this.answerInReview(item, body),
      withdrawAsk: (row) => { this.withdrawing.add(row.id); this.notes.delete(row.id); this.render(); },
      withdrawBack: (row) => { this.withdrawing.delete(row.id); this.render(); },
      openCard: (row) => this.openCard(row),
      open: (row) => {
        if (row.thread_id) void this.openThread(row.thread_id);
        else if (row.task_id) void this.openTask(row.task_id);
      },
      openInbox: () => this.show("inbox"),
      jump: (anchor) => {
        const target = this.main.querySelector<HTMLElement>("#" + CSS.escape(anchor));
        if (target) this.rowScroll.jump(target);
      },
    };
  }

  private nearEnd(): boolean {
    const m = this.main;
    return m.scrollHeight - m.scrollTop - m.clientHeight < 80;
  }

  private scrollToEnd(): void {
    this.main.scrollTop = this.main.scrollHeight;
  }
}

export function startApp(deps: AppDeps): PhoneApp {
  const app = new PhoneApp(deps);
  app.start();
  if (typeof window !== "undefined") {
    // The keyboard never covers the message box: the app follows the visible viewport.
    followViewport(window, document.documentElement);
    window.addEventListener("offline", () => app.setOnline(false, 0));
    window.addEventListener("online", () => app.visible());
    // WS-5c rules 1 and 2: back in view, the window's mirror (`me`) and the op-token pool are read again.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !app.stopped) app.visible();
    });
    // P-7: a recording never continues in the background; its microphone is released.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") app.talk.hidden();
    });
  }
  return app;
}

/** A reply's note, as one line of the daemon's fixed words. */
function oneLineNote(words: string): string {
  return words.replace(/\s+/g, " ").trim().slice(0, 200);
}
