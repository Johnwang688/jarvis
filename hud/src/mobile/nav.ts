// Navigation for the phone (PX-4, plan C §3.3 to §3.5): the scroll memory, the approval
// sheet's frame, the ⋯ menu and the Needs-you tray. Pure DOM and state, so the tests build
// it in jsdom; app.ts decides what each level is.
//
// **No browser-history integration.** A level the app opens (the approval sheet, an open chat or
// task, a New chat or New task form) makes no history entry and no popstate is listened for: the
// in-app back closes the level directly, one per tap, and the browser's Back and swipe behave as
// they did before. (Deferred to a follow-up after the owner's iPhone test.)
//
// Text from the server is set with textContent only.

import { el } from "./views";
import { oneLine } from "../lib/inbox";
import "./nav.css";

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
