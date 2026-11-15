// Navigation for the phone (PX-4, plan C §3.3 to §3.5): the history, the scroll
// memory, the approval sheet's frame, the ⋯ menu and the Needs-you tray. Pure
// DOM and state, so the tests build it in jsdom; app.ts decides what each level is.
//
// **History is state only.** A level the app opens (an approval sheet, an open
// chat, a task, a New chat or New task form) pushes ONE history entry whose state is
// `{ jv: <depth> }` and whose URL is the one already shown: `pushState` is never
// given a URL, so no thread, task or row id ever reaches the address bar, the
// history list or a shared link. The depth is derived from what the app shows
// (`sync`), never counted by hand, so every way of leaving a level (the in-app
// back, a tap on the dim area, a swipe-back, the Android back button) lands on the
// same state. A home-screen app may offer no swipe-back at all: the in-app back
// closes the level itself and then only trims the history to match.
//
// Text from the server is set with textContent only.

import { el } from "./views";
import { oneLine } from "../lib/inbox";
import "./nav.css";

// -- history ---------------------------------------------------------------------------

/** What `Nav` needs of the browser (a fake in the tests). */
export interface HistoryEnv {
  state(): unknown;
  /** `pushState(state, "")`: no URL argument, so the address never changes. */
  push(state: unknown): void;
  replace(state: unknown): void;
  go(delta: number): void;
  onPop(listener: (state: unknown) => void): () => void;
}

export const browserHistory: HistoryEnv = {
  state: () => history.state,
  push: (state) => history.pushState(state, ""),
  replace: (state) => history.replaceState(state, ""),
  go: (delta) => history.go(delta),
  onPop: (listener) => {
    const h = (e: PopStateEvent) => listener(e.state);
    window.addEventListener("popstate", h);
    return () => window.removeEventListener("popstate", h);
  },
};

/** The depth a history entry's state names: a whole number, else 0 (an entry we did not make). */
export function depthOf(state: unknown): number {
  const v = state && typeof state === "object" ? (state as { jv?: unknown }).jv : undefined;
  return typeof v === "number" && Number.isInteger(v) && v > 0 && v < 100 ? v : 0;
}

/** How long a traversal we asked for may stay unanswered before the queued opens are let through. */
const PENDING_MS = 4000;

/**
 * The history, with ONE path and no attribution.
 *
 * - **Only a popstate closes a level.** The handler reads the depth the browser landed on (`state.jv`, else 0)
 *   and closes every level deeper than it. Every popstate gets this, ours or the owner's, so a real Back is
 *   never swallowed.
 * - **Closing goes through the browser.** `back()` (the in-app back, the dim area) asks `history.go(-1)` and
 *   closes nothing itself. (When the browser has no entry of ours to leave, `back()` closes the level at once.)
 * - **Opening while a traversal we asked for is on its way is queued** (`open`) and run, with its push, right
 *   after the next popstate lands, so a late popstate can never close a level opened after the request: the
 *   level does not exist yet when it lands. If none arrives within `PENDING_MS` the queue is let through; the
 *   timeout never pushes by itself and never closes anything, and what is missing is pushed from the state
 *   `history.state` shows then.
 * - `sync` pushes the entries the app's levels are missing and walks back over entries whose levels the app
 *   closed on its own (a tab switch, an answered card).
 */
export class Nav {
  private off: (() => void) | null = null;
  /** The app is closing levels for a pop: `sync` waits, so it never pushes a forward entry mid-way. */
  private closing = false;
  /** A `go` of ours has not been answered yet. */
  private pending = false;
  private queue: (() => void)[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;

  /**
   * `depth`: how many levels the app shows now. `close`: close levels until the app shows at
   * most this many (run only for a popstate).
   */
  constructor(private readonly env: HistoryEnv, private readonly depth: () => number,
              private readonly close: (depth: number) => void) {}

  /** How many of our entries sit above the base entry in the real history, as the browser says. */
  get hist(): number {
    return depthOf(this.env.state());
  }

  start(): void {
    // The entry the app was opened on is level 0, whatever state a reload left on it.
    this.env.replace({ jv: 0 });
    this.off = this.env.onPop((state) => this.popped(depthOf(state)));
  }

  stop(): void {
    this.off?.();
    this.off = null;
    this.settle();
    this.queue = [];
  }

  private settle(): void {
    this.pending = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private request(delta: number): void {
    this.pending = true;
    this.env.go(delta);
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.pending = false;
      this.flush();
      this.sync();
    }, PENDING_MS);
  }

  private flush(): void {
    const run = this.queue.splice(0);
    for (const fn of run) fn();
  }

  private popped(landed: number): void {
    this.settle();
    this.closing = true;
    try { this.close(landed); } finally { this.closing = false; }
    this.flush();
    this.sync();
  }

  /** Run an action that opens a level: now, or right after the pending traversal lands. */
  open(fn: () => void): void {
    if (this.pending) { this.queue.push(fn); return; }
    fn();
    this.sync();
  }

  /** The top level goes back through the browser; with no entry of ours to leave it is closed at once. */
  back(): void {
    const want = this.depth();
    if (want <= 0 || this.pending || this.closing) return;
    if (this.hist <= 0) {
      this.closing = true;
      try { this.close(want - 1); } finally { this.closing = false; }
      this.sync();
      return;
    }
    this.request(-1);
  }

  /** Make the real history match the levels the app shows: push for each new one, go back over each closed one. */
  sync(): void {
    if (this.closing || this.pending) return;
    const want = this.depth();
    const actual = this.hist;
    if (want > actual) {
      for (let i = actual + 1; i <= want; i++) this.env.push({ jv: i });
    } else if (want < actual) {
      this.request(want - actual);
    }
  }
}

// -- scroll memory ---------------------------------------------------------------------

/** Where a view's scroll stood, and the row the owner left it from, per view. */
export class ScrollMemory {
  private readonly tops = new Map<string, number>();
  private readonly from = new Map<string, { attr: string; value: string }>();

  save(key: string, top: number): void {
    this.tops.set(key, Math.max(0, Math.round(top)));
  }
  top(key: string): number {
    return this.tops.get(key) ?? 0;
  }
  /** The row opened from this view: matched later by attribute and value, never spliced into a selector. */
  leftFrom(key: string, attr: string, value: string): void {
    this.from.set(key, { attr, value });
  }
  row(key: string): { attr: string; value: string } | null {
    return this.from.get(key) ?? null;
  }
  clearRow(key: string): void {
    this.from.delete(key);
  }
}

/** The element carrying `attr="value"` inside `root`. */
export function byAttr(root: ParentNode, attr: string, value: string): HTMLElement | undefined {
  return Array.from(root.querySelectorAll<HTMLElement>(`[${attr}]`)).find((n) => n.getAttribute(attr) === value);
}

// -- the sheet ---------------------------------------------------------------------------

/**
 * The approval sheet's frame (§3.3): the dimmed area that closes it and the sheet that holds the
 * card. The only element in the phone that blurs what is behind it (nav.css). No drag, no swipe.
 */
export function sheetView(content: HTMLElement, dismiss: () => void): HTMLElement[] {
  const dim = el("div", "m-sheet-dim");
  dim.dataset.testid = "m-sheet-dim";
  dim.addEventListener("click", dismiss);
  const sheet = el("div", "m-sheet");
  sheet.dataset.testid = "m-sheet";
  sheet.append(content);
  return [dim, sheet];
}

// -- the ⋯ menu ----------------------------------------------------------------------------

export interface MenuItem {
  id: string;
  /** The row's `data-testid`, when it is not `<menu id>-<id>`. */
  testid?: string;
  /** Plain label (a row), or a node drawn as the row's content (Notifications). */
  label?: string;
  node?: HTMLElement;
  onSelect?: () => void;
  disabled?: boolean;
  /** A line of text, not an action ("Last read 2 min ago"). */
  info?: boolean;
}

export interface MenuOptions {
  /** `data-testid` of the ⋯ button; the panel's is `<id>-panel` and its rows `<id>-<item id>`. */
  id: string;
  label: string;
  items: MenuItem[];
  open: boolean;
  toggle: (open: boolean) => void;
}

/** A ⋯ button (48 by 48) and, while open, its panel: 48 px rows in the order given. */
export function menuView(o: MenuOptions): HTMLElement {
  const wrap = el("div", "m-menu");
  wrap.dataset.testid = o.id + "-wrap";
  const button = el("button", "tap m-more", "⋯");
  button.type = "button";
  button.dataset.testid = o.id;
  button.setAttribute("aria-label", o.label);
  button.setAttribute("aria-haspopup", "true");
  button.setAttribute("aria-expanded", String(o.open));
  button.addEventListener("click", () => o.toggle(!o.open));
  wrap.append(button);
  if (!o.open) return wrap;
  const panel = el("div", "m-menu-panel");
  panel.dataset.testid = o.id + "-panel";
  panel.setAttribute("role", "group");
  panel.setAttribute("aria-label", o.label);
  panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); o.toggle(false); button.focus(); }
  });
  for (const item of o.items) {
    if (item.node) {
      item.node.classList.add("m-menu-row");
      item.node.dataset.menuItem = item.id;
      panel.append(item.node);
      continue;
    }
    if (item.info) {
      const line = el("p", "m-menu-info", item.label ?? "");
      line.dataset.testid = item.testid ?? `${o.id}-${item.id}`;
      panel.append(line);
      continue;
    }
    const row = el("button", "tap m-menu-row", item.label ?? "");
    row.type = "button";
    row.dataset.testid = item.testid ?? `${o.id}-${item.id}`;
    row.disabled = !!item.disabled;
    row.addEventListener("click", () => { o.toggle(false); item.onSelect?.(); });
    panel.append(row);
  }
  wrap.append(panel);
  return wrap;
}

// -- the Needs-you tray ------------------------------------------------------------------------

/** A thread's rows that need the owner: ranks 1 to 5 (the tray's rule). */
export const TRAY_MAX_RANK = 5;

export interface TrayOptions<R extends { id: string; rank: number; title: string }> {
  rows: R[];
  /** One row, drawn by the app (the same article the list draws). */
  draw: (row: R) => HTMLElement;
  /** The owner folded it to its bar. */
  folded: boolean;
  fold: (folded: boolean) => void;
}

/**
 * Fixed under the open chat's header (§3.4): open whenever the chat has a row of rank 1 to 5, up to
 * 45dvh. It can shrink to a 48 px bar, and the rows stay in the page: a rank 1 to 4 row is never
 * dropped, only hidden while the owner has the bar folded (the bar still counts it).
 */
export function trayView<R extends { id: string; rank: number; title: string }>(o: TrayOptions<R>): HTMLElement | null {
  const rows = o.rows.filter((r) => r.rank <= TRAY_MAX_RANK);
  if (!rows.length) return null;
  const tray = el("section", "m-tray");
  tray.dataset.testid = "m-tray";
  tray.dataset.folded = String(o.folded);
  const urgent = rows.some((r) => r.rank <= 2);
  const bar = el("button", "tap m-tray-bar");
  bar.type = "button";
  bar.dataset.testid = "m-tray-bar";
  if (urgent) bar.dataset.tone = "pending";
  bar.setAttribute("aria-expanded", String(!o.folded));
  bar.append(el("span", "m-tray-label", "Needs you"), el("span", "m-tray-count", ` · ${rows.length}`));
  if (o.folded) bar.append(el("span", "m-tray-peek", " " + oneLine(rows[0].title, 40)));
  bar.addEventListener("click", () => o.fold(!o.folded));
  tray.append(bar);
  const body = el("div", "m-tray-body");
  body.dataset.testid = "m-tray-body";
  body.hidden = o.folded;
  for (const row of rows) body.append(o.draw(row));
  tray.append(body);
  return tray;
}
