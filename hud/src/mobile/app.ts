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

export type Tab = "inbox" | "chats" | "tasks";

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
  private deferred = false;
  /** One line shown once above the inbox after an answer ("Denied.", where a decision's answer went). */
  private flash = "";
  /** P-6: "Turn on notifications", under the Inbox (./notify.ts). */
  readonly notify: Notifications;

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
    this.tabs = { inbox: make("inbox", "Inbox"), chats: make("chats", "Chats"), tasks: make("tasks", "Tasks") };
    frame.append(top, this.banner, this.main, nav);
    this.frame = frame;
    this.notify = new Notifications(deps.pushEnv ?? browserEnv, deps.pushSend ?? browserSend,
      () => { if (this.tab === "inbox" && !this.card && !this.stopped) this.renderInbox(); },
      () => { if (!this.stopped) { this.card = null; this.show("inbox"); } });
  }

  // -- life cycle ----------------------------------------------------------------

  start(): void {
    this.deps.root.className = "app-root";
    this.deps.root.replaceChildren(this.frame);
    // A notification's tap opens /m/#inbox: the Inbox is the first tab anyway.
    if (typeof location !== "undefined" && location.hash === "#inbox") history.replaceState(null, "", "/m/");
    this.render();
    void this.loadInbox();
    void this.loadMe();
    void this.notify.sync();
    this.connect();
  }

  stop(): void {
    this.stopped = true;
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
      const note = typeof result.body.note === "string" ? result.body.note : "Sent.";
      this.flash = note;
    } else {
      this.notes.set(row.id, await this.refusedWith(result.status, result.body));
    }
    this.render();
    void this.loadInbox();
  }

  async loadChats(): Promise<void> {
    const [p, t] = [await this.read("/m/api/projects"), await this.read("/m/api/threads")];
    if (!p || !("body" in p) || !t || !("body" in t)) return;
    this.projects = parseProjects(p.body);
    this.threads = parseThreads(t.body);
    if (this.tab === "chats" && !this.thread) this.render();
  }

  async loadProjectThreads(projectId: string): Promise<void> {
    const r = await this.read(`/m/api/threads?project=${encodeURIComponent(projectId)}`);
    if (!r) return;
    if (!("body" in r)) {
      if (r.status === 404) return void this.loadChats();     // archived meanwhile
      return;
    }
    this.threads = this.threads.filter((t) => t.project_id !== projectId).concat(parseThreads(r.body));
    if (this.tab === "chats" && !this.thread) this.render();
  }

  async openThread(id: string, meta: ChatThread | null = null): Promise<void> {
    this.tab = "chats";
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
    if (this.tab === "tasks" && !this.task) this.render();
  }

  /** Open a task, or read the open one again (its diff too, once shown; its folds kept). */
  async openTask(id: string): Promise<void> {
    this.tab = "tasks";
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
    }
    this.tab = tab;
    this.render();
    if (tab === "inbox") void this.loadInbox();
    if (tab === "chats" && !this.thread) void this.loadChats();
    if (tab === "tasks" && !this.task) void this.loadTasks();
  }

  private renderBadge(): void {
    const n = this.inbox ? waitingCount(this.inbox) : 0;
    this.tabs.inbox.textContent = n ? `Inbox · ${n}` : "Inbox";
  }

  render(): void {
    for (const [tab, b] of Object.entries(this.tabs) as [Tab, HTMLButtonElement][]) {
      b.setAttribute("aria-selected", String(tab === this.tab));
    }
    this.renderBadge();
    let view: HTMLElement;
    if (this.tab === "inbox" && this.card) {
      view = cardView(this.card, {
        back: () => this.closeCard(),
        deny: () => void this.denyCard(),
        allow: () => void this.allowCard(),
        always: () => void this.alwaysCard(),
        confirmAlways: () => void this.confirmAlways(),
        cancelAlways: () => this.cancelAlways(),
      });
    } else if (this.tab === "inbox") {
      const answers: InboxAnswers = {
        openCard: (row) => this.openCard(row),
        send: (row, body) => void this.sendAnswer(row, body),
        busy: this.answering, notes: this.notes, drafts: this.drafts, picks: this.picks,
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
      if (this.thread) {
        const built = transcriptView(this.thread.meta, this.thread.rows, this.thread.older, () => this.show("chats"));
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
      }
    } else if (this.task) {
      view = this.task.view
        ? taskView(this.task.view, this.task.diff, { back: () => this.show("tasks"), showDiff: () => void this.loadDiff(),
          folds: this.task.folds })
        : el("p", "empty", "Loading…");
    } else {
      view = tasksView(this.tasks, (t) => void this.openTask(t.id));
    }
    this.main.replaceChildren(view);
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
  }
  return app;
}
