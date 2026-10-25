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
// There is no offline queue: the app writes nothing.

import { parseInbox, waitingCount, clockOf, type InboxRow } from "../lib/inbox";
import { getJson, errorWord, type Got } from "./net";
import {
  parseDiff, parseEvent, parseLog, parseProjects, parseTask, parseTasks, parseThreads,
  type ChatThread, type DiffView, type LogRow, type Project, type StreamEvent, type TaskView,
} from "./model";
import { chatsView, el, inboxView, logRow, taskView, tasksView, transcriptView, tap } from "./views";

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
  openStream?: (url: string) => StreamLike;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
}

export const STREAM_URL = "/m/api/events";
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

  private readonly fetchJson: (path: string) => Promise<Got>;
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
  }

  // -- life cycle ----------------------------------------------------------------

  start(): void {
    this.deps.root.className = "app-root";
    this.deps.root.replaceChildren(this.frame);
    this.render();
    void this.loadInbox();
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
    if (this.tab === "inbox") this.render();
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
    this.setOnline(got.ok);
    if (stream && stream === this.stream && stream.readyState === CLOSED) {
      this.setTimer(() => { if (this.stream === stream) this.connect(); }, RETRY_MS);
    }
  }

  handle(event: StreamEvent): void {
    if (event.kind === "inbox_changed") {
      this.later("inbox", () => void this.loadInbox());
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
    if (this.tab === "inbox") {
      view = inboxView(this.inbox, (row) => {
        if (row.thread_id) void this.openThread(row.thread_id);
        else if (row.task_id) void this.openTask(row.task_id);
      });
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
