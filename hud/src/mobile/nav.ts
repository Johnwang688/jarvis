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

export class Nav {
  /** How many of our entries sit above the base entry in the real history. */
  hist = 0;
  private off: (() => void) | null = null;
  /** The app is closing levels for a pop: `sync` waits, so it never pushes a forward entry mid-way. */
  private closing = false;
  /**
   * Traversals this class asked for (`go`) whose popstate has not arrived. The browser runs them later, so
   * until they land the real position is unknown: nothing is pushed or closed from a stale answer. When the
   * last one lands, the history is reconciled against what the app shows NOW (a sheet closed and reopened in
   * between is not dismissed by the traversal that followed the close).
   */
  private pending = 0;
  private watchdog: ReturnType<typeof setTimeout> | null = null;

  /**
   * `depth`: how many levels the app shows now. `close`: close levels until the app shows at
   * most this many (the app's own back, run for a pop).
   */
  constructor(private readonly env: HistoryEnv, private readonly depth: () => number,
              private readonly close: (depth: number) => void) {}

  start(): void {
    // The entry the app was opened on is level 0, whatever state a reload left on it.
    this.env.replace({ jv: 0 });
    this.hist = 0;
    this.off = this.env.onPop((state) => {
      const landed = depthOf(state);
      this.hist = landed;
      if (this.pending > 0) {
        // The answer to a `go` of ours, not the owner's back: it closes nothing.
        this.pending -= 1;
        if (this.pending === 0) { this.clearWatchdog(); this.sync(); }
        return;
      }
      this.closing = true;
      try { this.close(landed); } finally { this.closing = false; }
      this.sync();
    });
  }

  stop(): void {
    this.off?.();
    this.off = null;
    this.clearWatchdog();
  }

  private clearWatchdog(): void {
    if (this.watchdog !== null) clearTimeout(this.watchdog);
    this.watchdog = null;
  }

  /** Make the real history match the levels the app shows: push for each new one, go back for each closed one. */
  sync(): void {
    if (this.closing || this.pending > 0) return;
    const want = this.depth();
    if (want > this.hist) {
      for (let i = this.hist + 1; i <= want; i++) this.env.push({ jv: i });
      this.hist = want;
    } else if (want < this.hist) {
      const delta = want - this.hist;
      this.hist = want;
      this.pending += 1;
      this.env.go(delta);
      // A traversal that never reports (the browser had nowhere to go) must not hold the history for good.
      this.clearWatchdog();
      this.watchdog = setTimeout(() => { this.watchdog = null; this.pending = 0; this.sync(); }, 1500);
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
