// The signed-in phone app (phone design §6, P-3): three tabs over the read
// API (jarvis/v2/remote_api.py), live over its event stream, read-only.
//
//   Inbox   the inbox rows, the HUD's groups; approvals and questions are
//           shown in full and say "Answer at the desk" (answering is P-4).
//   Chats   projects, then each project's five most recent chats (the
//           desktop sidebar's rule, lib/recentChats.ts), then a transcript.
//   Tasks   running first, then blocked, then the rest; a task's state,
//           plan, last report and diff, folded file by file.
//
// **Live, and never stale while it says it is current.** The stream
// (`/m/api/events`) carries ids only; each event makes the app read the
// route it names again. When a read cannot reach Jarvis, or the stream
// drops and a check finds no way through, the app turns offline: a banner
// says so and when the shown data was last read, and the view is marked
// stale (dimmed, `data-stale`). Coming back reads everything shown again
// before the banner goes. A 401 anywhere means the session ended (logout,
// a revoke at the desk, expiry): the app stops and asks for a sign-in.
// There is no offline queue.
//
// P-4, answering (jarvis/v2/remote_answer.py): an approval row opens its
// card (./card.ts), read from `GET /m/api/inbox/<id>/detail`; Deny is one
// tap; AUTHORIZE and ALWAYS each ask `answer/options` for a challenge bound
// to that card and that answer, then the passkey (Face ID or the passcode),
// then send the assertion with the answer. ALWAYS shows the rule it would
// write first and asks again before the passkey. Questions and decisions
// are answered from their rows. Each answer is sent once, never queued or
// retried; a refusal shows the daemon's words. When §4.1 does not hold
// (`/m/api/me`'s `secure`), a red "limited: ..." line says so.
//
// P-5, writing (jarvis/v2/remote_write.py, ./write.ts): an owner's chat
// takes a message box (Send is a tap; Enter is a new line), the Chats tab a
// "New chat" sheet and the Tasks tab a "New task" sheet; a task offers
// Start, Resume and Cancel, each confirmed first, Cancel then the passkey.
// A proposal in the inbox takes Withdraw. Nothing is shown as sent before
// its 2xx; offline, nothing is sent (no write queue).
//
// MR, the morning review (jarvis/v2/review.py, ./review.ts): a Review tab
// beside the Inbox while the review has open items and the next night has
// not begun. A launch or a notification's tap opens it when the last push
// was the morning one (`opens_here`), else the Inbox. Questions and
// decisions are answered in place; deferred approvals are ticked one by one
// (never all at once) and approved as a set with ONE passkey assertion
// bound to exactly that set, all or nothing; a limited phone (§4.1) gets the
// per-row Approve and retry only.
//
// P-7, push to talk (jarvis/v2/remote_stt.py, ./talk.ts): an owner's chat's
// message box also takes "Hold to talk". Its words are added to that chat's
// draft and the box drawn again; Send is still the owner's tap. No inbox
// row, card, question or decision has a mic.

import { parseInbox, waitingCount, clockOf, type InboxRow } from "../lib/inbox";
import { getJson, errorWord, postJson, type Got, type Posted } from "./net";
import { cardView, parseDetail, parseSecure, type CardState, type Secure } from "./card";
import { credentialJSON, prepareRequest } from "./webauthn";
import {
  parseDiff, parseEvent, parseLog, parseProjects, parseTask, parseTasks, parseThreads,
  type ChatThread, type DiffView, type LogRow, type Project, type StreamEvent, type TaskView,
} from "./model";
import { chatsView, el, inboxView, logRow, taskView, tasksView, transcriptView, tap, type InboxAnswers } from "./views";
import { browserEnv, browserSend, Notifications, type PushEnv, type Send } from "./notify";
import {
  chatSheetView, clientIdFor, composerView, MAYBE_DONE, MAYBE_SENT, MESSAGE_CHARS, parseModels, refusal, sentNote,
  taskControlsView, taskSheetView, verbsFor,
  type ProviderChoice, type Sheet, type Verb,
} from "./write";
import { Talk, browserTalkEnv, type TalkEnv } from "./talk";
import { afterChanged, allItems, approveBody, confirmationList, confirmedChanged, emptySelection, keptSelection, leaveBody, parseReview, toggled,
  UNTICKED_GONE, type ConfirmLine, type ReviewItem, type ReviewReport } from "../lib/review";
import { reviewTabShown, reviewView, type ReviewActions } from "./review";
import { staleAt } from "../lib/approvalSummary";

export type Tab = "inbox" | "review" | "chats" | "tasks";

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
  fetchJson?: (path: string) => Promise<Got>;
  /** P-4's writes (a fake in the tests). */
  postJson?: (path: string, value: unknown) => Promise<Posted>;
  /** The passkey for one answer: the request options in, the assertion's JSON out (a fake in the tests). */
  getCredential?: (publicKey: Record<string, unknown>) => Promise<Record<string, unknown> | null>;
  openStream?: (url: string) => StreamLike;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  /** P-6's notifications: the browser's push and the push routes (fakes in the tests). */
  pushEnv?: PushEnv;
  pushSend?: Send;
  /** P-7's microphone and clip upload (fakes in the tests). */
  talkEnv?: TalkEnv;
}

export const STREAM_URL = "/m/api/events";
// A write that met the proxy with nothing behind it (`tailscale serve`, the daemon down).
const OFFLINE: ReadonlySet<number> = new Set([502, 504]);
// How long after an answer box loses focus the inbox is drawn again (renderInbox).
const BLUR_REDRAW_MS = 400;
const CLOSED = 2;
const DEBOUNCE_MS = 120;
const RETRY_MS = 4000;

interface OpenThread { id: string; meta: ChatThread | null; rows: LogRow[]; next: number; older: boolean; list: HTMLElement | null }
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
  lastRead: number | null = null;
  inbox: InboxRow[] | null = null;
  projects: Project[] | null = null;
  threads: ChatThread[] = [];
  expanded = new Set<string>();
  thread: OpenThread | null = null;
  tasks: TaskView[] | null = null;
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
  private rendered: Tab | null = null;

  private readonly fetchJson: (path: string) => Promise<Got>;
  private readonly postJson: (path: string, value: unknown) => Promise<Posted>;
  private readonly getCredential: (publicKey: Record<string, unknown>) => Promise<Record<string, unknown> | null>;
  private readonly openStream: (url: string) => StreamLike;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly timers = new Map<string, unknown>();
  private readonly frame: HTMLElement;
  private readonly banner: HTMLElement;
  private readonly conn: HTMLElement;
  private readonly main: HTMLElement;
  private readonly tabs: Record<Tab, HTMLButtonElement>;
  private logBusy = false;
  private logAgain = false;

  constructor(private readonly deps: AppDeps) {
    this.fetchJson = deps.fetchJson ?? getJson;
    this.postJson = deps.postJson ?? postJson;
    this.getCredential = deps.getCredential ?? (async (publicKey) => {
      const credential = await navigator.credentials.get({ publicKey: prepareRequest(publicKey) });
      return credential ? credentialJSON(credential as PublicKeyCredential) : null;
    });
    this.openStream = deps.openStream ?? ((url) => new EventSource(url) as unknown as StreamLike);
    this.now = deps.now ?? Date.now;
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    const frame = el("div", "app");
    frame.dataset.screen = "app";
    frame.dataset.stale = "false";
    const top = el("header", "top");
    top.append(el("span", "brand", "Jarvis"));
    this.conn = el("span", "conn", "connecting");
    this.conn.dataset.testid = "m-conn";
    top.append(this.conn);
    top.append(tap("Sign out", () => void this.signOut(), "quiet", "m-signout"));
    this.banner = el("div", "offline");
    this.banner.dataset.testid = "m-offline";
    this.banner.setAttribute("role", "status");
    this.banner.hidden = true;
    this.main = el("main", "main");
    this.main.dataset.testid = "m-main";
    const nav = el("nav", "tabs");
    const make = (tab: Tab, label: string) => {
      const b = tap(label, () => this.show(tab), "tab", "m-tab-" + tab);
      b.setAttribute("role", "tab");
      nav.append(b);
      return b;
    };
    this.tabs = { inbox: make("inbox", "Inbox"), review: make("review", "Review"), chats: make("chats", "Chats"),
                  tasks: make("tasks", "Tasks") };
    this.tabs.review.hidden = true;
    frame.append(top, this.banner, this.main, nav);
    this.frame = frame;
    this.notify = new Notifications(deps.pushEnv ?? browserEnv, deps.pushSend ?? browserSend,
      () => { if (this.tab === "inbox" && !this.card && !this.stopped) this.renderInbox(); },
      () => { if (!this.stopped) void this.openFromNotification(); });
    this.talk = new Talk(deps.talkEnv ?? browserTalkEnv, {
      insert: (threadId, text) => this.dictated(threadId, text),
      // No recording while this chat's send waits for its answer (its 202 would clear the box).
      busy: (threadId) => this.sending.has(threadId),
      unauthorized: () => void this.recheckSession(),
      online: () => this.online,
    });
  }

  // -- life cycle ----------------------------------------------------------------

  start(): void {
    this.deps.root.className = "app-root";
    this.deps.root.replaceChildren(this.frame);
    // A notification's tap opens /m/#inbox: the Inbox is the first tab anyway.
    if (typeof location !== "undefined" && location.hash === "#inbox") history.replaceState(null, "", "/m/");
    this.render();
    void this.loadInbox();
    // MR-12: a launch opens on the review when the morning push was the last one.
    void this.loadReview();
    void this.loadMe();
    void this.notify.sync();
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.staleTimer) clearTimeout(this.staleTimer);
    this.staleTimer = null;
    this.talk.hidden();
    this.talk.dropRetry();
    this.stream?.close();
    this.stream = null;
  }

  private async signOut(): Promise<void> {
    this.stop();
    await this.deps.signOut();
  }

  private signedOut(): void {
    if (this.stopped) return;
    this.stop();
    this.deps.onSignedOut("Your session ended. Sign in again.");
  }

  // -- reading ---------------------------------------------------------------------

  /** One read: the body, or null after handling offline and signed out. */
  private async read(path: string): Promise<{ body: unknown } | { error: string; status: number } | null> {
    const got = await this.fetchJson(path);
    if (this.stopped) return null;
    if (got.ok) {
      this.lastRead = this.now();
      this.setOnline(true);
      return { body: got.body };
    }
    if (got.why === "signed-out") { this.signedOut(); return null; }
    if (got.why === "offline") { this.setOnline(false); return null; }
    return { error: errorWord(got.body) || `could not be read (${got.status})`, status: got.status };
  }

  async loadInbox(): Promise<void> {
    const r = await this.read("/m/api/inbox");
    if (!r || !("body" in r)) return;
    const body = r.body as { rows?: unknown };
    this.inbox = parseInbox(body?.rows) ?? [];
    this.renderBadge();
    const card = this.card;
    if (card && !card.busy && !this.inbox.some((row) => row.id === card.rowId)) {
      card.refused = "This card is no longer waiting: it was answered, or it timed out.";
      card.rule = null;
    }
    if (this.tab === "inbox") this.renderInbox();
  }

  /** §4.1's last result, to show (`/m/api/me`): never what authorizes. */
  async loadMe(): Promise<void> {
    // Display only: a failure here changes nothing (the reads and the
    // stream decide offline and signed out).
    const got = await this.fetchJson("/m/api/me");
    if (this.stopped || !got.ok) return;
    this.secure = parseSecure((got.body as { secure?: unknown })?.secure);
    if (this.tab === "inbox") this.renderInbox();
  }

  /** MR: the morning review. The first read decides where a launch opens (MR-12). */
  async loadReview(): Promise<void> {
    const r = await this.read("/m/api/review");
    if (!r || !("body" in r)) return;
    const report = parseReview(r.body);
    if (!report) return;
    this.review = report;
    // Under an open confirmation, a re-read that drops or changes any
    // confirmed item closes it: nothing is approved but the set the owner
    // confirmed (review of #329, MED 1).
    this.checkConfirmed(report);
    // A re-read unticks whatever left the review.
    this.reviewSel = keptSelection(report, this.reviewSel);
    const first = this.reviewFirst;
    this.reviewFirst = false;
    this.renderTabs();
    if (first && report.opensHere && this.tab === "inbox" && !this.card) {
      this.show("review");
      return;
    }
    if (this.tab === "review") this.renderKeepingFocus();
  }

  /** A notification's tap on an open app (sw.ts posts "open-inbox"): the review when the
   * morning push was the last one and it has open items, else the Inbox (MR-12). */
  async openFromNotification(): Promise<void> {
    this.card = null;
    const r = await this.read("/m/api/review");
    const report = r && "body" in r ? parseReview(r.body) : null;
    if (this.stopped) return;
    if (report) this.review = report;
    // Opened from a notification: nothing is ticked (review of #329, MED 2).
    this.resetReviewSelection();
    this.renderTabs();
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

  private renderTabs(): void {
    this.tabs.review.hidden = !(this.tab === "review" || reviewTabShown(this.review));
    const n = this.review?.open ?? 0;
    this.tabs.review.textContent = n ? `Review · ${n}` : "Review";
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
    void this.loadInbox();
  }

  /** Leave it on one deferred row (the session only). */
  async leaveInReview(row: InboxRow): Promise<void> {
    const item = this.review ? allItems(this.review).find((i) => i.row.id === row.id) : undefined;
    if (!item) return;
    await this.answerInReview(item, { action: "leave" });
  }

  /** Approve selected, step two: one challenge for exactly the ticked set, the passkey once, then the set. */
  async approveSelected(): Promise<void> {
    const report = this.review;
    if (!report || this.reviewBusy || !this.reviewConfirming || !this.reviewConfirmed) return;
    // Exactly what the confirmation showed, and only while it still holds.
    if (confirmedChanged(report, this.reviewConfirmed).length) { this.checkConfirmed(report); this.render(); return; }
    const body = { items: this.reviewConfirmed.map((c) => ({ id: c.id, tag: c.tag })) };
    this.reviewBusy = true;
    this.reviewFlash = "";
    this.render();
    const done = async (result: { status: number; body: Record<string, unknown> } | null, words?: string) => {
      this.reviewBusy = false;
      this.reviewConfirming = false;
      this.reviewConfirmed = null;
      if (words !== undefined) this.reviewFlash = words;
      else if (result && result.status === 200) {
        const n = typeof result.body.approved === "number" ? result.body.approved : body.items.length;
        this.reviewSel = emptySelection();
        this.reviewFlash = `Approved ${n} once each, for 30 minutes; their chats were told.`;
      } else if (result && result.status === 409 && Array.isArray(result.body.changed)) {
        // All or nothing: what changed is unticked, with why; the rest need a new confirmation.
        const after = afterChanged(this.reviewSel, result.body.changed);
        this.reviewSel = after.selection;
        for (const [id, words2] of after.notes) this.notes.set(id, words2);
        this.reviewFlash = "Nothing was approved: something you selected changed. Look again, then confirm again.";
      } else if (result) {
        this.reviewFlash = "Nothing was approved. " + await this.refusedWith(result.status, result.body);
      }
      this.render();
      void this.loadReview();
      void this.loadInbox();
    };
    const options = await this.post("/m/api/review/approve/options", body);
    if (!options || options.status !== 200) return done(options);
    let assertion: Record<string, unknown> | null = null;
    try {
      const publicKey = options.body.publicKey;
      assertion = publicKey && typeof publicKey === "object" ? await this.getCredential(publicKey as Record<string, unknown>) : null;
    } catch {
      assertion = null;
    }
    if (!assertion) return done(null, "Cancelled, or the passkey was not verified. Nothing was approved.");
    return done(await this.post("/m/api/review/approve", { items: body.items, assertion }));
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
    void this.loadInbox();
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

  /** A write's answer: null after handling offline and signed out. */
  private async post(path: string, value: unknown): Promise<{ status: number; body: Record<string, unknown> } | null> {
    const got = await this.postJson(path, value);
    if (this.stopped) return null;
    if ("offline" in got) { this.setOnline(false); return null; }
    if (OFFLINE.has(got.status)) { this.setOnline(false); return null; }
    this.setOnline(true);
    return got;
  }

  /** A 401 from an answer is a passkey that was not accepted, unless the session ended too. */
  private async refusedWith(status: number, body: Record<string, unknown>): Promise<string> {
    if (status === 401) {
      const me = await this.fetchJson("/m/api/me");
      if (!me.ok && me.why === "signed-out") { this.signedOut(); return ""; }
      return "Not accepted: the passkey was not verified for this answer.";
    }
    const word = typeof body.error === "string" ? body.error : "";
    return word ? word[0].toUpperCase() + word.slice(1) + "." : `Not answered (${status}).`;
  }

  openCard(row: InboxRow): void {
    this.flash = "";
    this.card = { rowId: row.id, row, detail: null, refused: null, busy: false, rule: null, notice: "" };
    this.tab = "inbox";
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
    if (!got.ok && got.why === "signed-out") return this.signedOut();
    if (!got.ok && got.why === "offline") { this.setOnline(false); return; }
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
    this.render();
    if (result.status === 409) void this.loadCard();
  }

  async denyCard(): Promise<void> {
    const card = this.card;
    if (!card || card.busy) return;
    card.busy = true;
    card.notice = "";
    this.render();
    const result = await this.post(this.path(card.rowId, "answer"), { decision: "deny" });
    await this.finish(card, result, "Denied.");
  }

  /** AUTHORIZE: a challenge for this card and this answer, then the passkey, then the answer. */
  async allowCard(): Promise<void> {
    const card = this.card;
    if (!card || card.busy) return;
    card.busy = true;
    card.notice = "";
    this.render();
    const options = await this.post(this.path(card.rowId, "answer/options"), { decision: "allow" });
    if (!options || options.status !== 200) return this.finish(card, options, "");
    await this.withPasskey(card, "allow", options.body.publicKey, "Authorized.");
  }

  /** ALWAYS, step one: the rule it would write, shown before the passkey is asked for. */
  async alwaysCard(): Promise<void> {
    const card = this.card;
    if (!card || card.busy) return;
    card.busy = true;
    card.notice = "";
    this.render();
    const options = await this.post(this.path(card.rowId, "answer/options"), { decision: "always" });
    if (this.card !== card) return;
    if (!options || options.status !== 200) return this.finish(card, options, "");
    const words = typeof options.body.rule_words === "string" ? options.body.rule_words : "";
    const publicKey = options.body.publicKey;
    card.busy = false;
    card.rule = publicKey && typeof publicKey === "object"
      ? { words, entry: options.body.rule ?? null, publicKey: publicKey as Record<string, unknown> } : null;
    this.render();
  }

  /** ALWAYS, step two: the owner read the rule; now the passkey. */
  async confirmAlways(): Promise<void> {
    const card = this.card;
    if (!card || card.busy || !card.rule) return;
    card.busy = true;
    this.render();
    await this.withPasskey(card, "always", card.rule.publicKey, "Authorized, and the rule is written.");
  }

  cancelAlways(): void {
    if (!this.card) return;
    this.card.rule = null;
    this.render();
  }

  private async withPasskey(card: CardState, decision: "allow" | "always", publicKey: unknown, what: string) {
    let assertion: Record<string, unknown> | null = null;
    try {
      assertion = publicKey && typeof publicKey === "object" ? await this.getCredential(publicKey as Record<string, unknown>) : null;
    } catch {
      assertion = null;
    }
    if (this.card !== card) return;
    if (!assertion) {
      card.busy = false;
      card.rule = null;
      card.notice = "Cancelled, or the passkey was not verified. Nothing was answered.";
      this.render();
      return;
    }
    const result = await this.post(this.path(card.rowId, "answer"), { decision, assertion });
    if (result && result.status === 200 && result.body.authority === "limited") what += " (with Discord's authority: limited)";
    await this.finish(card, result, what);
  }

  /**
   * QH, Approve and retry on a deferred row: a challenge bound to this
   * deferral (`answer/options`, `{decision: "allow"}`), the passkey, then the
   * answer with its assertion. Sent once; a cancelled passkey answers nothing.
   */
  async retryDeferred(row: InboxRow): Promise<void> {
    if (this.answering.has(row.id)) return;
    this.flash = "";
    this.answering.add(row.id);
    this.notes.delete(row.id);
    this.render();
    const done = async (result: { status: number; body: Record<string, unknown> } | null, note?: string) => {
      this.answering.delete(row.id);
      if (note !== undefined) this.notes.set(row.id, note);
      else if (result && result.status === 200) {
        this.flash = typeof result.body.note === "string" ? result.body.note : "Approved once.";
      } else if (result) this.notes.set(row.id, await this.refusedWith(result.status, result.body));
      this.render();
      void this.loadInbox();
    };
    const options = await this.post(this.path(row.id, "answer/options"), { decision: "allow" });
    if (!options || options.status !== 200) return done(options);
    let assertion: Record<string, unknown> | null = null;
    try {
      const publicKey = options.body.publicKey;
      assertion = publicKey && typeof publicKey === "object" ? await this.getCredential(publicKey as Record<string, unknown>) : null;
    } catch {
      assertion = null;
    }
    if (!assertion) return done(null, "Cancelled, or the passkey was not verified. Nothing was approved.");
    return done(await this.post(this.path(row.id, "answer"), { action: "retry", assertion }));
  }

  /** A question's or a decision's answer, from its row. Sent once. */
  async sendAnswer(row: InboxRow, body: Record<string, unknown>): Promise<void> {
    if (this.answering.has(row.id)) return;
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
      const note = typeof result.body.note === "string" ? result.body.note
        : row.kind === "proposal" ? "Withdrawn: the task will not start." : "Sent.";
      this.flash = note;
    } else {
      this.notes.set(row.id, await this.refusedWith(result.status, result.body));
    }
    this.render();
    void this.loadInbox();
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
    if ("offline" in got || OFFLINE.has(got.status)) {
      this.setOnline(false);
      return { status: 0, body: {}, ambiguous: true };
    }
    this.setOnline(true);
    const result = got;
    // The write took effect, or may have, but its answer failed (remote_write's UNCERTAIN).
    if (result.body.uncertain === true) return { status: result.status, body: result.body, ambiguous: true };
    if (result.status === 401) {
      const me = await this.fetchJson("/m/api/me");
      if (!me.ok && me.why === "signed-out") { this.signedOut(); return null; }
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
    return composerView({
      draft: this.composeDrafts.get(threadId) ?? "", busy: this.sending.has(threadId),
      note: this.composeNotes.get(threadId) ?? "", online: this.online,
      send: (text) => void this.sendMessage(threadId, text),
      edit: (text) => { this.composeDrafts.set(threadId, text); },
      mic: this.talk.view(threadId),
    });
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

  /** A 401 from the clip upload: is the session over? */
  private async recheckSession(): Promise<void> {
    const me = await this.fetchJson("/m/api/me");
    if (!me.ok && me.why === "signed-out") this.signedOut();
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

  /** A confirmed verb: Start and Resume on the session; Cancel with its own passkey assertion. */
  async confirmVerb(id: string, verb: Verb): Promise<void> {
    const w = this.taskState(id);
    if (w.busy || w.confirming !== verb || !this.online) return;
    w.busy = true;
    w.note = "";
    this.render();
    let result: { status: number; body: Record<string, unknown>; ambiguous?: boolean } | null;
    if (verb === "cancel") {
      const options = await this.write(`/m/api/tasks/${id}/cancel/options`, {});
      if (options && options.status === 200 && options.body.publicKey && typeof options.body.publicKey === "object") {
        let assertion: Record<string, unknown> | null = null;
        try { assertion = await this.getCredential(options.body.publicKey as Record<string, unknown>); } catch { assertion = null; }
        if (!assertion) {
          if (this.taskWrite === w) {
            w.busy = false;
            w.confirming = null;
            w.note = "Cancelled, or the passkey was not verified. The task goes on.";
            this.render();
          }
          return;
        }
        result = await this.write(`/m/api/tasks/${id}/cancel`, { assertion });
      } else if (options?.ambiguous && options.status === 0) {
        // `cancel/options` is a read: nothing was sent that could have cancelled.
        if (this.taskWrite === w) {
          w.busy = false;
          w.confirming = null;
          w.note = "Offline: the task was not cancelled.";
          this.render();
        }
        return;
      } else {
        result = options;
      }
    } else {
      result = await this.write(`/m/api/tasks/${id}/${verb}`, {});
    }
    if (this.taskWrite !== w) return;
    w.busy = false;
    w.confirming = null;
    if (!result) {
      w.note = "";
    } else if (result.ambiguous) {
      w.note = MAYBE_DONE;
    } else if (result.status === 200) {
      w.note = verb === "start" ? "Started." : verb === "resume" ? "Resumed." : "Cancelled.";
      void this.loadTasks();
      if (this.task?.id === id) { void this.openTask(id); return; }
    } else if (result.status === 401 && verb === "cancel") {
      w.note = "Not accepted: the passkey was not verified for this cancel.";
    } else {
      w.note = refusal(result.status, result.body, "Not done");
    }
    this.render();
  }

  async loadChats(): Promise<void> {
    const [p, t] = [await this.read("/m/api/projects"), await this.read("/m/api/threads")];
    if (!p || !("body" in p) || !t || !("body" in t)) return;
    this.projects = parseProjects(p.body);
    this.threads = parseThreads(t.body);
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
    if (this.tab === "chats" && !this.thread && !this.sheet) this.render();
  }

  async openThread(id: string, meta: ChatThread | null = null): Promise<void> {
    this.tab = "chats";
    this.sheet = null;
    this.thread = { id, meta: meta ?? this.threads.find((t) => t.id === id) ?? null, rows: [], next: 0, older: false, list: null };
    this.render();
    const r = await this.read(`/m/api/threads/${id}/log`);
    if (!r || this.thread?.id !== id) return;
    if (!("body" in r)) { this.threadGone(r.error); return; }
    const page = parseLog(r.body);
    if (!page) return;
    this.thread = { id, meta: page.thread ?? this.thread.meta, rows: page.rows, next: page.next, older: page.older, list: null };
    this.render();
    this.scrollToEnd();
  }

  private threadGone(why: string): void {
    if (!this.thread) return;
    this.thread.rows = [];
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
          const page = parseLog(r.body);
          if (!page) return;
          const last = open.rows.length ? open.rows[open.rows.length - 1].seq : -1;
          const fresh = page.rows.filter((row) => row.seq > last);
          open.next = Math.max(open.next, page.next);
          if (page.thread) open.meta = page.thread;
          more = page.more;
          if (fresh.length) this.appendRows(open, fresh);
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
    for (const row of rows) open.list.append(logRow(row));
    if (atEnd) this.scrollToEnd();
  }

  async loadTasks(): Promise<void> {
    const r = await this.read("/m/api/tasks");
    if (!r || !("body" in r)) return;
    this.tasks = parseTasks(r.body);
    if (this.tab === "tasks" && !this.task && !this.sheet) this.render();
  }

  /** Open a task, or read the open one again (its diff too, once shown; its folds kept). */
  async openTask(id: string): Promise<void> {
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
    if (this.tab === "chats") {
      if (this.thread) void this.moreLog();
      void this.loadChats();
    } else if (this.tab === "tasks") {
      if (this.task) void this.openTask(this.task.id);
      void this.loadTasks();
    }
  }

  // -- the stream ----------------------------------------------------------------------

  connect(): void {
    if (this.stopped) return;
    this.stream?.close();
    const stream = this.openStream(STREAM_URL);
    this.stream = stream;
    stream.onopen = () => {
      if (this.stream !== stream) return;
      this.conn.textContent = "live";
      this.conn.dataset.state = "live";
      // Anything published while the stream was down is read now.
      this.refreshVisible();
    };
    stream.onmessage = (ev) => {
      if (this.stream !== stream) return;
      const event = parseEvent(typeof ev.data === "string" ? ev.data : "");
      if (event) this.handle(event);
    };
    stream.onerror = () => {
      if (this.stream !== stream) return;
      this.conn.textContent = "reconnecting";
      this.conn.dataset.state = "down";
      void this.probe(stream);
    };
  }

  /** The stream dropped: is it the network, the session, or a passing break? */
  async probe(stream: StreamLike | null = this.stream): Promise<void> {
    const got = await this.fetchJson("/m/api/me");
    if (this.stopped) return;
    if (!got.ok && got.why === "signed-out") return this.signedOut();
    if (got.ok) this.secure = parseSecure((got.body as { secure?: unknown })?.secure);
    this.setOnline(got.ok);
    if (stream && stream === this.stream && stream.readyState === CLOSED) {
      this.setTimer(() => { if (this.stream === stream) this.connect(); }, RETRY_MS);
    }
  }

  handle(event: StreamEvent): void {
    if (event.kind === "inbox_changed") {
      this.later("inbox", () => void this.loadInbox());
      this.later("review", () => void this.loadReview());
      if (this.card && event.ids.includes(this.card.rowId)) this.later("card", () => void this.loadCard());
      if (this.tasks !== null && event.ids.some((id) => /^(task|prop|tq):/.test(id))) {
        this.later("tasks", () => void this.loadTasks());
      }
    } else if (event.kind === "thread_changed") {
      if (this.thread?.id === event.threadId) this.later("log", () => void this.moreLog());
      if (this.projects !== null) {
        const project = event.projectId;
        if (project) this.later("threads:" + project, () => void this.loadProjectThreads(project));
        else this.later("chats", () => void this.loadChats());
      }
    } else if (event.kind === "task_changed") {
      if (this.tasks !== null) this.later("tasks", () => void this.loadTasks());
      if (this.task?.id === event.taskId) this.later("task", () => void this.openTask(event.taskId));
    } else {
      this.refreshVisible();
    }
  }

  private later(key: string, fn: () => void): void {
    if (this.timers.has(key)) return;
    this.timers.set(key, this.setTimer(() => { this.timers.delete(key); if (!this.stopped) fn(); }, DEBOUNCE_MS));
  }

  // -- online and offline ---------------------------------------------------------------

  setOnline(online: boolean): void {
    if (online === this.online) {
      if (!online) this.renderBanner();
      return;
    }
    this.online = online;
    this.frame.dataset.stale = online ? "false" : "true";
    this.renderBanner();
    // The write controls (P-5) say offline and turn off, or come back.
    if (this.tab !== "inbox" || !this.card) this.renderKeepingFocus();
    if (online) this.refreshVisible();
  }

  private renderBanner(): void {
    this.banner.hidden = this.online;
    if (this.online) {
      const live = this.stream?.readyState === 1;
      this.conn.textContent = live ? "live" : "connecting";
      this.conn.dataset.state = live ? "live" : "down";
      return;
    }
    const at = this.lastRead !== null ? clockOf(this.lastRead) : "";
    this.banner.textContent = at
      ? `Offline. Nothing here is current: it was last read at ${at}.`
      : "Offline. Nothing here could be read yet.";
    this.conn.textContent = "offline";
    this.conn.dataset.state = "offline";
  }

  // -- drawing ---------------------------------------------------------------------------

  show(tab: Tab): void {
    if (tab === "inbox" && this.tab === "inbox" && this.card) this.card = null;
    if (tab === this.tab) {
      // A second tap on the open tab goes back to its list.
      if (tab === "chats") this.thread = null;
      if (tab === "tasks") this.task = null;
      this.sheet = null;
    } else if (this.sheet && this.sheet.kind !== (tab === "chats" ? "chat" : tab === "tasks" ? "task" : "")) {
      this.sheet = null;
    }
    // Entering the Review tab: nothing ticked, nothing confirmed (review of #329, MED 2).
    if (tab === "review" && this.tab !== "review") this.resetReviewSelection();
    this.tab = tab;
    this.render();
    if (tab === "inbox") void this.loadInbox();
    if (tab === "review") void this.loadReview();
    if (tab === "chats" && !this.thread) void this.loadChats();
    if (tab === "tasks" && !this.task) void this.loadTasks();
  }

  private renderBadge(): void {
    const n = this.inbox ? waitingCount(this.inbox) : 0;
    this.tabs.inbox.textContent = n ? `Inbox · ${n}` : "Inbox";
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

  render(): void {
    this.wakeForStale();
    for (const [tab, b] of Object.entries(this.tabs) as [Tab, HTMLButtonElement][]) {
      b.setAttribute("aria-selected", String(tab === this.tab));
    }
    this.renderBadge();
    this.renderTabs();
    let view: HTMLElement;
    if (this.tab === "review") {
      view = reviewView({
        report: this.review, selection: this.reviewSel, notes: this.notes, receipts: this.reviewReceipts,
        busy: this.answering, groupBusy: this.reviewBusy, confirming: this.reviewConfirming, flash: this.reviewFlash,
        drafts: this.drafts, picks: this.reviewPicks, withdrawing: this.withdrawing, online: this.online,
        opened: this.opened, confirmLines: this.reviewConfirmed ? this.reviewConfirmLines : null,
      }, { ...this.reviewActions(), expand: (row, open) => { this.opened.set(row.id, open); this.render(); } }, this.secure && !this.secure.ok ? `limited: ${this.secure.label || "a check failed"}`
        : this.review?.group && !this.review.group.ok ? (this.review.group.why || "limited") : null);
      // The scroll holds where it was: a row answered in place folds where it stood.
      const top = this.rendered === "review" ? this.main.scrollTop : 0;
      this.main.replaceChildren(view);
      this.main.scrollTop = top;
      this.rendered = "review";
      return;
    }
    if (this.tab === "inbox" && this.card) {
      view = cardView(this.card, {
        back: () => this.closeCard(),
        deny: () => void this.denyCard(),
        allow: () => void this.allowCard(),
        always: () => void this.alwaysCard(),
        confirmAlways: () => void this.confirmAlways(),
        cancelAlways: () => this.cancelAlways(),
        // AS: the owner's own open or close of the command, kept across a re-read of the card.
        toggleCommand: (open) => { if (this.card) { this.card.expanded = open; this.render(); } },
      });
    } else if (this.tab === "inbox") {
      const answers: InboxAnswers = {
        openCard: (row) => this.openCard(row),
        send: (row, body) => void this.sendAnswer(row, body),
        retry: (row) => void this.retryDeferred(row),
        busy: this.answering, notes: this.notes, drafts: this.drafts, picks: this.picks,
        expanded: this.opened,
        toggle: (row, open) => { this.opened.set(row.id, open); this.render(); },
        withdraw: {
          confirming: this.withdrawing,
          ask: (row) => { this.withdrawing.add(row.id); this.notes.delete(row.id); this.render(); },
          back: (row) => { this.withdrawing.delete(row.id); this.render(); },
        },
      };
      view = inboxView(this.inbox, (row) => {
        if (row.thread_id) void this.openThread(row.thread_id);
        else if (row.task_id) void this.openTask(row.task_id);
      }, answers, this.secure);
      view.append(this.notify.view());
      if (this.flash) {
        const line = el("p", "m-notice", this.flash);
        line.dataset.testid = "m-flash";
        line.setAttribute("role", "status");
        view.prepend(line);
      }
    } else if (this.tab === "chats") {
      if (this.sheet?.kind === "chat") {
        view = chatSheetView(this.sheet, this.projects ?? [], this.models, this.online, {
          close: () => { this.sheet = null; this.render(); void this.loadChats(); },
          submit: () => void this.createChat(),
          change: () => this.render(),
          openChat: (id) => void this.openThread(id),
        });
      } else if (this.thread) {
        const built = transcriptView(this.thread.meta, this.thread.rows, this.thread.older, () => this.show("chats"),
          this.composer(this.thread.id));
        this.thread.list = built.list;
        view = built.view;
      } else {
        view = chatsView(this.projects, this.threads, this.expanded, {
          open: (t) => void this.openThread(t.id, t),
          toggle: (id) => {
            if (this.expanded.has(id)) this.expanded.delete(id); else this.expanded.add(id);
            this.render();
          },
        });
        view.prepend(tap("New chat", () => void this.openNewChat(), "quiet m-new", "m-new-chat-open"));
      }
    } else if (this.sheet?.kind === "task") {
      view = taskSheetView(this.sheet, this.projects ?? [], this.online, {
        close: () => { this.sheet = null; this.render(); void this.loadTasks(); },
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
        }, allowed);
        if (proposed) {
          box.prepend(el("p", "m-notice", "Proposed: it starts on its own when its grace window ends. Withdraw it from the inbox."));
        }
        return box;
      })() : (this.taskWrite?.id === open.id && this.taskWrite.note
        ? el("p", "m-notice", this.taskWrite.note) : null);
      view = open.view
        ? taskView(open.view, open.diff, { back: () => this.show("tasks"), showDiff: () => void this.loadDiff(),
          folds: open.folds, controls })
        : el("p", "empty", "Loading…");
    } else {
      view = tasksView(this.tasks, (t) => void this.openTask(t.id));
      view.prepend(tap("New task", () => this.openNewTask(), "quiet m-new", "m-new-task-open"));
    }
    this.main.replaceChildren(view);
    this.rendered = this.tab;
  }

  private reviewActions(): ReviewActions {
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
      approveConfirm: () => void this.approveSelected(),
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
        target?.scrollIntoView({ block: "start" });
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
    window.addEventListener("offline", () => app.setOnline(false));
    window.addEventListener("online", () => void app.probe());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !app.stopped) void app.probe();
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
