// Compact items inside a thread card (PX-3b, plan C §3.2): the approval item with its one Review button,
// the PR item (today's `prBlock`), "+N for later" and "Later". Everything else (a question's chips, a
// decision, a deferral, a proposal, a task) is the row `inboxRow` already draws.
//
// **IN-8 holds here.** The fold covers only ranks ABOVE 5: a row of rank 1 to 5 is always drawn in the card,
// and "Later" is offered only to rank 5 and up, so a rank 1 to 4 row can never be hidden by either. A
// folded row stays in the page (`hidden`), a "Later" row is a one-line stub with a Show button: every row
// the phone received can be expanded and nothing here leaves a row unreachable.
//
// **"Later" is this page's memory and nothing else.** It sends no request, marks nothing on the server and
// is gone on reload (an owner-accepted audit default): the set lives in `ItemsUi`, owned by the card cache.
//
// **Redraws.** Folding and Later change what a card draws, so they are part of its signature (`ui.sig`)
// and bump `ui.rev`; the control also rebuilds the card's items in place, so it works without the app
// having to know about it.

import { deferredOf, oneLine, type InboxRow } from "../lib/inbox";
import { summaryLines } from "./card";
import { deskNote } from "./model";
import { timeLabel } from "./reltime";
import { answersHere, el, inboxRow, prBlock, tap, type InboxAnswers } from "./views";
import type { Slot } from "./threads";
import "./items.css";

/** Ranks 1 to 5 never fold: this is the highest rank that stays in the card. */
export const FOLD_AFTER_RANK = 5;
/** The lowest rank "Later" is offered for (IN-8: kinds 5 to 8). */
export const LATER_MIN_RANK = 5;

/** Folded: a rank above 5 (a rank that is not a number never folds: never lose a row to a typo). */
export const folds = (row: InboxRow): boolean => row.rank > FOLD_AFTER_RANK;
/** Later is offered: rank 5 and up, never ranks 1 to 4. */
export const laterOk = (row: InboxRow): boolean => row.rank >= LATER_MIN_RANK;

/** This session's fold and Later state. Memory only: a reload starts empty. */
export class ItemsUi {
  /** Bumped by every expand, collapse, Later and Show: the cards' signatures read it. */
  rev = 0;
  /** Slot keys whose "+N for later" is open. */
  readonly open = new Set<string>();
  /** Row ids put off for now. */
  readonly later = new Set<string>();

  /** What a slot's card draws from this state. */
  sig(slot: Slot): string {
    return JSON.stringify([this.rev, this.open.has(slot.key), slot.rows.filter((r) => this.later.has(r.id)).map((r) => r.id)]);
  }
}

export interface ItemsEnv {
  answers: InboxAnswers;
  openRow: (row: InboxRow) => void;
}

function testid<T extends HTMLElement>(node: T, id: string): T {
  node.dataset.testid = id;
  return node;
}

/** The article every item shares: title, blocks, origin and the asked/expires line. */
function shell(row: InboxRow): HTMLElement {
  const card = testid(el("article", `row rank-${row.rank} m-item`), "m-inbox-row");
  card.dataset.kind = oneLine(row.kind, 40);
  card.dataset.id = row.id;
  card.append(el("h3", "row-title", row.title || row.id));
  if (row.blocks) card.append(el("p", "row-blocks", row.blocks));
  if (row.origin) card.append(testid(el("p", "row-origin", row.origin), "m-origin"));
  const labels = [timeLabel("ago", row.asked_at, "asked ", Date.now(), true), timeLabel("in", row.expires_at, "expires ", Date.now(), true)]
    .filter((n): n is HTMLElement => n !== null);
  if (labels.length) {
    const meta = el("p", "row-meta");
    labels.forEach((n, i) => { if (i) meta.append(" · "); meta.append(n); });
    card.append(meta);
  }
  return card;
}

/** The approval item: headline, the AI summary, Where and the time left, then one Review button that opens the card. */
function approvalItem(row: InboxRow, env: ItemsEnv): HTMLElement {
  const card = shell(row);
  card.classList.add("m-item-approval");
  const d = row.detail;
  const box = testid(el("div", "row-detail m-item-detail"), "m-detail");
  if (d.hud_only === true) {
    box.append(testid(el("p", "muted", `${oneLine(d.tool, 60) || "A tool"}: a private card. Its detail is shown only at the desk.`), "m-private"));
  } else {
    const headline = oneLine(d.headline, 400);
    if (headline) box.append(el("p", "headline m-clamp2", headline));
    summaryLines(d, box, "m-row");
  }
  card.append(box);
  if (answersHere(row, env.answers)) {
    const review = tap("Review", () => env.answers.openCard(row), "quiet m-send primary m-review", "m-item-review");
    review.setAttribute("aria-label", `Review: ${oneLine(row.title, 120) || "this approval"}`);
    review.disabled = env.answers.busy.has(row.id);
    card.append(review);
  } else {
    const note = deskNote(row.kind);
    if (note) card.append(testid(el("p", "desk-only", note), "m-desk-only"));
  }
  return card;
}

/** The PR item: today's `prBlock`, unchanged, under the shell. */
function prItem(row: InboxRow, env: ItemsEnv): HTMLElement {
  const card = shell(row);
  const box = testid(el("div", "row-detail"), "m-detail");
  prBlock(row, box, env.answers);
  if (box.childNodes.length) card.append(box);
  if (!env.answers.pr) {
    const note = deskNote(row.kind);
    if (note) card.append(testid(el("p", "desk-only", note), "m-desk-only"));
  }
  if (row.thread_id || row.task_id) {
    card.append(tap(row.thread_id ? "Open the chat" : "Open the task", () => env.openRow(row), "link", "m-open"));
  }
  return card;
}

/** One row in its compact form (no Later control yet). */
export function compactItem(row: InboxRow, env: ItemsEnv): HTMLElement {
  if (row.kind === "approval" && !deferredOf(row)) return approvalItem(row, env);
  if (row.kind === "pr") return prItem(row, env);
  return inboxRow(row, env.openRow, env.answers);
}

/** A row put off: one line and a Show button. It keeps the item's test id and ids so it stays findable. */
function laterStub(row: InboxRow, show: () => void): HTMLElement {
  const card = testid(el("article", `row rank-${row.rank} m-item m-later-stub`), "m-inbox-row");
  card.dataset.kind = oneLine(row.kind, 40);
  card.dataset.id = row.id;
  card.dataset.later = "true";
  card.append(el("span", "row-title m-later-title", row.title || row.id));
  card.append(tap("Show", show, "quiet m-send m-unlater", "m-item-unlater"));
  return card;
}

/** All of a slot's rows: ranks 1 to 5 in place, rank 6 and below behind "+N for later" (kept in the page). */
export function buildItems(slot: Slot, env: ItemsEnv, ui: ItemsUi): HTMLElement {
  let current: HTMLElement = testid(el("div", "m-thread-items"), "m-thread-items");
  // Redraw this container alone, keeping the focus on the control that was used.
  const redraw = (focus: string, id: string | null) => {
    ui.rev += 1;
    const next = buildItems(slot, env, ui);
    const hadFocus = current.contains(document.activeElement);
    current.replaceWith(next);
    current = next;
    if (hadFocus) {
      const mine = id === null ? next
        : Array.from(next.querySelectorAll<HTMLElement>("[data-id]")).find((n) => n.dataset.id === id) ?? next;
      (mine.querySelector<HTMLElement>(`[data-testid="${focus}"]`) ?? next.querySelector<HTMLElement>("button"))
        ?.focus({ preventScroll: true });
    }
  };
  const drawRow = (row: InboxRow): HTMLElement => {
    if (ui.later.has(row.id) && laterOk(row)) {
      return laterStub(row, () => { ui.later.delete(row.id); redraw("m-item-later", row.id); });
    }
    const node = compactItem(row, env);
    if (laterOk(row)) {
      node.append(tap("Later", () => { ui.later.add(row.id); redraw("m-item-unlater", row.id); }, "quiet m-later", "m-item-later"));
    }
    return node;
  };
  // A card with no head of its own (a row alone) never folds: its one row is the card.
  const canFold = slot.kind !== "row";
  const shown = slot.rows.filter((r) => !canFold || !folds(r));
  const rest = canFold ? slot.rows.filter(folds) : [];
  for (const row of shown) current.append(drawRow(row));
  if (rest.length) {
    const open = ui.open.has(slot.key);
    const body = testid(el("div", "m-fold-rest"), "m-fold-rest");
    body.hidden = !open;
    for (const row of rest) body.append(drawRow(row));
    const toggle = tap(open ? `Hide ${rest.length} for later` : `+${rest.length} for later`, () => {
      if (open) ui.open.delete(slot.key); else ui.open.add(slot.key);
      redraw("m-item-fold", null);
    }, "quiet m-fold", "m-item-fold");
    toggle.setAttribute("aria-expanded", String(open));
    current.append(toggle, body);
  }
  return current;
}
