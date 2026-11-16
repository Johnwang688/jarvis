// RD-6: navigation consumes the app's already-read state. No requests or authorization here.
import { el, tap } from "./views";
import { brandButton, renderOrb, type PhoneOrbState } from "./orb";
import { inboxIcon, chatsIcon, settingsIcon, sunIcon, terminalIcon, chevronIcon, lockIcon, unlockIcon } from "./icons";
import type { ProjectFolds } from "./folds";
import type { ChatThread, Project } from "./model";
import "./nav.css";

export type DrawerSection = "inbox" | "chats" | "review" | "tasks" | "settings";
export interface DrawerState {
  section: DrawerSection;
  connection: PhoneOrbState;
  lastRead: string;
  projects: Project[] | null;
  threads: ChatThread[];
  needs: ReadonlySet<string>;
  running: ReadonlySet<string>;
  count: number;
  review: number;
  unlock: string;
  unlocked: boolean;
  lockNote: string;
}
interface DrawerActions {
  section: (section: DrawerSection) => void;
  thread: (thread: ChatThread) => void;
  lock: () => void;
}
const recent = (threads: ChatThread[]) => threads.map((t, i) => ({ t, i, at: Date.parse(t.updated) || 0 }))
  .sort((a, b) => b.at - a.at || a.i - b.i).slice(0, 5).map(({ t }) => t);

/** Direction locks once: a vertical or multi-touch gesture can never become navigation. The first
 * eight CSS pixels stay with the browser's native back gesture. We never cancel a pending touch. */
export function drawerSwipe(host: HTMLElement, direction: "open" | "close", enabled: () => boolean,
  actions: { begin: () => void; move: (distance: number) => void; end: (commit: boolean) => void }): () => void {
  let start: { x: number; y: number; id: number } | null = null;
  let dragging = false;
  const finish = (commit = false) => {
    if (dragging) actions.end(commit);
    start = null;
    dragging = false;
  };
  const down = (event: TouchEvent) => {
    finish();
    if (!enabled() || event.touches.length !== 1) return;
    const t = event.touches[0];
    if (direction === "open" && (t.clientX < 8 || t.clientX > 20)) return;
    start = { x: t.clientX, y: t.clientY, id: t.identifier };
  };
  const distance = (t: Touch) => (t.clientX - start!.x) * (direction === "open" ? 1 : -1);
  const move = (event: TouchEvent) => {
    if (!start) return;
    const t = event.touches[0];
    if (event.touches.length !== 1 || t.identifier !== start.id) { finish(); return; }
    const dx = distance(t), dy = Math.abs(t.clientY - start.y);
    if (!dragging) {
      if (dy > 8 && dy >= Math.abs(dx)) { start = null; return; }
      if (dx < -8) { start = null; return; }
      if (dx < 8 || dx <= dy * 1.5) return;
      // If the browser owns this gesture, leave it alone (e.g. native iOS back).
      if (!event.cancelable) { start = null; return; }
      dragging = true;
      actions.begin();
    }
    event.preventDefault();
    actions.move(Math.max(0, dx));
  };
  const up = (event: TouchEvent) => {
    const t = Array.from(event.changedTouches).find((t) => t.identifier === start?.id);
    finish(!!t && event.touches.length === 0 && distance(t) >= 40);
  };
  const cancel = () => finish();
  host.addEventListener("touchstart", down, { passive: true });
  host.addEventListener("touchmove", move, { passive: false });
  host.addEventListener("touchend", up);
  host.addEventListener("touchcancel", cancel);
  return () => {
    finish();
    host.removeEventListener("touchstart", down);
    host.removeEventListener("touchmove", move);
    host.removeEventListener("touchend", up);
    host.removeEventListener("touchcancel", cancel);
  };
}

export class Drawer {
  private overlay: HTMLElement | null = null;
  private panel: HTMLElement | null = null;
  private background: { node: HTMLElement; inert: boolean }[] = [];
  private unsubscribe: () => void;
  private unswipe: (() => void) | null = null;
  private animation: Animation | null = null;
  private drawing = false;
  constructor(private host: HTMLElement, private opener: HTMLButtonElement, private folds: ProjectFolds,
    private state: () => DrawerState, private actions: DrawerActions) {
    this.unsubscribe = folds.subscribe(() => this.refresh());
    opener.setAttribute("aria-haspopup", "dialog");
    opener.setAttribute("aria-expanded", "false");
    opener.setAttribute("aria-controls", "m-navigation");
  }
  get isOpen(): boolean { return !!this.overlay; }
  open(): void {
    if (this.overlay) return;
    this.background = Array.from(this.host.children).filter((n): n is HTMLElement => n instanceof HTMLElement)
      .map((node) => ({ node, inert: node.hasAttribute("inert") }));
    this.overlay = el("div", "m-drawer-host");
    const dim = el("div", "m-drawer-dim");
    dim.dataset.testid = "m-drawer-dim";
    dim.addEventListener("click", () => this.close());
    this.panel = el("aside", "m-drawer");
    this.panel.id = "m-navigation";
    this.panel.dataset.testid = "m-drawer";
    this.panel.setAttribute("role", "dialog");
    this.panel.setAttribute("aria-modal", "true");
    this.panel.setAttribute("aria-label", "Navigation");
    this.overlay.append(dim, this.panel);
    this.host.append(this.overlay);
    this.opener.setAttribute("aria-expanded", "true");
    this.host.dataset.drawer = "open";
    this.refresh();
    document.addEventListener("keydown", this.keys, true);
    document.addEventListener("focusin", this.focus, true);
    this.unswipe = drawerSwipe(this.panel, "close", () => true, {
      begin: () => {}, move: (dx) => this.follow(dx, false), end: (commit) => {
        this.animation?.cancel(); this.animation = null;
        if (commit) this.close();
      },
    });
    this.first()?.focus({ preventScroll: true });
  }
  /** WAAPI follows the finger without style attributes (the phone's CSP refuses inline styles). */
  follow(distance: number, opening = true): void {
    if (!this.panel || !this.panel.animate || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    if (!this.animation) {
      this.animation = this.panel.animate([{ transform: "translateX(-100%)" }, { transform: "translateX(0)" }],
        { duration: 1000, fill: "both" });
      this.animation.pause();
    }
    const fraction = Math.min(1, distance / Math.max(1, this.panel.getBoundingClientRect().width));
    this.animation.currentTime = 1000 * (opening ? fraction : 1 - fraction);
  }
  finishOpening(commit: boolean): void {
    this.animation?.cancel(); this.animation = null;
    if (!commit) this.close();
  }
  close(): void {
    if (!this.overlay) return;
    document.removeEventListener("keydown", this.keys, true);
    document.removeEventListener("focusin", this.focus, true);
    this.unswipe?.(); this.unswipe = null;
    this.animation?.cancel(); this.animation = null;
    this.overlay.remove(); this.overlay = null; this.panel = null;
    this.background.forEach(({ node, inert }) => { if (!inert) node.removeAttribute("inert"); });
    this.background = [];
    delete this.host.dataset.drawer;
    this.opener.setAttribute("aria-expanded", "false");
    this.opener.focus({ preventScroll: true });
  }
  stop(): void { this.close(); this.unsubscribe(); }
  private buttons = () => Array.from(this.panel?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])
    .filter((b) => !b.closest("[hidden]"));
  private first = () => this.panel?.querySelector<HTMLButtonElement>('[data-testid="m-drawer-inbox"]');
  private keys = (event: KeyboardEvent) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); this.close(); }
    if (event.key !== "Tab") return;
    const buttons = this.buttons(), first = buttons[0], last = buttons.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  };
  private focus = (event: FocusEvent) => {
    if (!this.drawing && this.panel && !this.panel.contains(event.target as Node)) this.first()?.focus({ preventScroll: true });
  };
  refresh(): void {
    if (!this.panel) return;
    // App redraws may remove inert: reassert it on the same refresh, including the header and sheet host.
    this.background.forEach(({ node }) => node.setAttribute("inert", ""));
    const oldFocus = document.activeElement as HTMLElement | null;
    const key = this.panel.contains(oldFocus) ? oldFocus?.dataset.drawerKey : null;
    const top = this.panel.querySelector(".m-drawer-body")?.scrollTop ?? 0;
    const s = this.state();
    this.drawing = true;
    const head = el("div", "m-drawer-head");
    const brand = brandButton();
    renderOrb(brand, s.connection);
    // A display-only brand, using the existing code-owned orb geometry.
    const identity = el("div", "m-drawer-identity");
    identity.append(...Array.from(brand.childNodes));
    identity.querySelector(".conn")!.textContent = `${s.connection.word} · last read ${s.lastRead}`;
    const close = tap("✕", () => this.close(), "m-drawer-close", "m-drawer-close");
    close.setAttribute("aria-label", "Close navigation");
    close.dataset.drawerKey = "close";
    head.append(identity, close);
    const body = el("div", "m-drawer-body");
    const row = (label: string, key: string, act: () => void, icon?: () => SVGSVGElement) => {
      const b = tap("", act, "m-drawer-row", "m-drawer-" + key);
      b.dataset.drawerKey = key;
      if (icon) b.append(icon());
      b.append(el("span", "m-drawer-label", label));
      return b;
    };
    const count = (n: number, pending = false) => el("span", pending ? "m-drawer-count m-drawer-pending" : "m-drawer-count", String(n));
    const sections: [DrawerSection, string, () => SVGSVGElement, number][] = [
      ["inbox", "Inbox", inboxIcon, s.count], ["chats", "Chats", chatsIcon, 0],
      ["review", "Morning review", sunIcon, s.review], ["tasks", "Tasks", terminalIcon, 0],
      ["settings", "Settings", settingsIcon, 0],
    ];
    for (const [section, label, icon, n] of sections) {
      if (section === "review" && !n) continue;
      const b = row(label, section, () => { this.close(); this.actions.section(section); }, icon);
      if (s.section === section) b.setAttribute("aria-current", "page");
      if (n) b.append(count(n, section === "inbox"));
      body.append(b);
    }
    const chat = (t: ChatThread, key: string) => {
      const b = row(t.title || "(untitled)", key, () => { this.close(); this.actions.thread(t); });
      b.classList.add("m-drawer-chat");
      b.dataset.id = t.id;
      b.dataset.status = s.needs.has(t.id) ? "needs" : t.running || s.running.has(t.id) ? "running" : "idle";
      const dot = el("span", "m-drawer-dot"); dot.setAttribute("aria-hidden", "true"); b.prepend(dot);
      return b;
    };
    const chats = s.threads.filter((t) => !t.task_id);
    body.append(el("h2", "m-drawer-heading", "Projects"));
    if (s.projects === null) body.append(el("p", "m-drawer-empty", "Open Chats to read projects."));
    else if (!s.projects.length) body.append(el("p", "m-drawer-empty", "No projects."));
    for (const p of s.projects ?? []) {
      const threads = chats.filter((t) => t.project_id === p.id);
      const needs = threads.filter((t) => s.needs.has(t.id)).length;
      const active = !!needs || threads.some((t) => t.running || s.running.has(t.id));
      const folded = this.folds.get(p.id, active);
      const b = row(p.name, "project-" + p.id, () => this.folds.set(p.id, !folded), chevronIcon);
      b.dataset.testid = "m-drawer-project";
      b.dataset.id = p.id;
      const sub = el("div", "m-drawer-project-body");
      sub.id = "m-drawer-project-" + encodeURIComponent(p.id);
      sub.hidden = folded;
      b.setAttribute("aria-expanded", String(!folded)); b.setAttribute("aria-controls", sub.id);
      if (needs) b.append(count(needs, true));
      b.append(count(threads.length));
      for (const t of recent(threads)) sub.append(chat(t, "project-chat-" + t.id));
      if (!threads.length) sub.append(el("p", "m-drawer-empty", "No chats."));
      body.append(b, sub);
    }
    body.append(el("h2", "m-drawer-heading", "Recent"));
    for (const t of recent(chats)) body.append(chat(t, "recent-" + t.id));
    const foot = el("div", "m-drawer-foot");
    const lock = el("div", "m-drawer-lock");
    lock.append(s.unlocked ? unlockIcon() : lockIcon(), el("span", "m-drawer-lock-label", s.unlock));
    lock.dataset.testid = "m-drawer-lock";
    lock.dataset.state = s.unlocked ? "unlocked" : "locked";
    const lockNow = tap("Lock now", () => { this.close(); this.actions.lock(); }, "quiet", "m-drawer-lock-now");
    lockNow.dataset.drawerKey = "lock";
    foot.append(lock, lockNow);
    if (s.lockNote) { const note = el("p", "m-notice", s.lockNote); note.setAttribute("role", "status"); foot.append(note); }
    this.panel.replaceChildren(head, body, foot);
    body.scrollTop = top;
    this.drawing = false;
    if (key) (this.buttons().find((b) => b.dataset.drawerKey === key) ?? this.first())?.focus({ preventScroll: true });
  }
}
