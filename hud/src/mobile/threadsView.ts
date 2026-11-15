// The thread list's DOM (PX-2, plan C §3.1 and §4.4): the summary strip, the
// three bands, one card per slot, and the notices that say what the phone does
// not show. Pure from data to elements, so the tests build it in jsdom.
//
// **Every received inbox row is drawn exactly once, in its slot, by
// `inboxRow`** (the same article as before: `data-testid="m-inbox-row"`,
// `data-id`, `data-kind`), and ranks 1 to 4 are never inside a fold. The few
// folds that exist hold rank 6 and below: Recent's "Show all" and rank 8's
// "for your information"; both keep their rows in the page.
//
// **Keyed reuse (Sol, finding 3, and finding 5 of the first review).** A card
// is rebuilt only when its signature changes, and the signature holds
// everything the card renders from: the slot's entry, task fields and rows
// (whole), each row's UI state (sending, notes, progress, withdrawing, the
// command's open state, picks), the global `limited`, the cross-row answer a
// PR fix's question resolves to, the staleness of a pending summary, and the
// age words. A draft is the one thing left out: it lives in the textarea, and
// the app already holds a redraw while one is being typed in.
//
// Untrusted text (titles, origins, briefs) is set with textContent only.

import { oneLine, waitingCount, type InboxRow } from "../lib/inbox";
import { startsCollapsed, staleAt, summaryView } from "../lib/approvalSummary";
import { prRowOf, questionRow } from "../lib/prRow";
import { LABEL_TEXT } from "../lib/working";
import type { TaskView, WorkingItem } from "./model";
import { bands, buildSlots, type Band, type Slot } from "./threads";
import { el, empty, tap, type InboxAnswers } from "./views";
import { buildItems, ItemsUi } from "./items";
import { agoWords } from "./reltime";
import "./threads.css";

/** The strip's chips: a band, or the Tasks list. `null` shows everything. */
export type Filter = "needs" | "running" | "tasks" | null;

export interface ListModel {
  rows: InboxRow[] | null;
  /** null: not read, failed or unavailable. */
  entries: WorkingItem[] | null;
  working: "loading" | "ok" | "unavailable";
  /** The daemon cut the working list: "Showing N of T: the rest are at the desk". */
  workingCut: { more: boolean; total: number };
  tasks: TaskView[] | null;
  tasksMore: boolean;
  inboxMore: boolean;
  filter: Filter;
  recentAll: boolean;
  /** The inbox read failed with these words (the list stays, dimmed by the app). */
  error: string | null;
}

export interface ListActions {
  answers: InboxAnswers;
  openRow: (row: InboxRow) => void;
  openThread: (id: string) => void;
  openTask: (id: string) => void;
  setFilter: (filter: Filter) => void;
  setRecentAll: (all: boolean) => void;
  retry: () => void;
}

/** How many Recent cards show before "Show all". */
export const RECENT_SHOWN = 5;

function testid<T extends HTMLElement>(node: T, id: string): T {
  node.dataset.testid = id;
  return node;
}

const KIND_WORDS: Record<string, string> = {
  approval: "Approval", question: "Question", task_question: "Question", proposal: "Proposal", task: "Task",
  missed: "Missed", decision: "Decision", check: "Check", pr: "Pull request", pr_unavailable: "Pull requests",
  deferred: "Deferred",
};
const kindWord = (row: Pick<InboxRow, "kind">): string => KIND_WORDS[row.kind] || "Item";

// -- the signature ------------------------------------------------------------------------

/** Everything about one row's UI that the card draws from, outside the row itself. */
function rowUi(row: InboxRow, a: InboxAnswers, now: number): string {
  const parts: unknown[] = [
    a.busy.has(row.id), a.notes.get(row.id) ?? "", a.picks.get(row.id) ?? -1, a.expanded?.get(row.id) ?? null,
    a.withdraw?.confirming.has(row.id) ?? null, a.pr?.progress.get(row.id) ?? "",
  ];
  if (row.kind === "approval") {
    // A pending summary goes stale with time and opens the command: the card must follow.
    const stale = staleAt(row.detail);
    parts.push(startsCollapsed(summaryView(row.detail, now)), stale !== null && now >= stale);
  }
  if (row.kind === "pr") {
    // The fix's question is resolved against the OTHER rows (views.ts prBlock): it can change with this row untouched.
    const fix = prRowOf(row)?.fix;
    parts.push(fix && a.pr ? questionRow(fix, a.pr.rows) : "");
  }
  return JSON.stringify(parts);
}

export function slotSignature(slot: Slot, band: Band, a: InboxAnswers, now: number, ui?: ItemsUi): string {
  const task = slot.task;
  return JSON.stringify([
    band, slot.key, slot.title, slot.entry,
    task ? [task.id, task.brief, task.state, task.updated, task.step, task.steps] : null,
    agoWords(slot.entry?.last ?? task?.updated ?? "", now),
    // Globals the rows draw from.
    !!a.pr, a.pr?.limited ?? false, !!a.withdraw, !!a.toggle,
    slot.rows.map((r) => [r, rowUi(r, a, now)]),
    // PX-3b: the fold, Later and their revision.
    ui ? ui.sig(slot) : "",
  ]);
}

/** The cards already built, by slot key: a card is reused while its signature holds. */
export class SlotCache {
  readonly map = new Map<string, { sig: string; node: HTMLElement }>();
  builds = 0;
  /** PX-3b: this page's fold and Later state (memory only: a reload starts empty). */
  readonly ui = new ItemsUi();
  prune(keep: ReadonlySet<string>): void {
    for (const key of [...this.map.keys()]) if (!keep.has(key)) this.map.delete(key);
  }
}

// -- cards ------------------------------------------------------------------------------------

function threadTitle(slot: Slot): string {
  if (slot.entry) return slot.entry.title || "(untitled chat)";
  if (slot.kind === "task") return slot.title || slot.rows.find((r) => r.kind === "task" || r.kind === "proposal")?.title || "Task";
  if (slot.kind === "orphan") return slot.title || "Chat";
  return oneLine(slot.rows[0]?.title, 80) || "Item";
}

function threadMeta(slot: Slot, now: number): string {
  const age = agoWords(slot.entry?.last ?? slot.task?.updated ?? "", now);
  if (slot.entry) return [LABEL_TEXT[slot.entry.status], slot.entry.project, age].filter(Boolean).join(" · ");
  if (slot.kind === "task") {
    const t = slot.task;
    const progress = t && t.steps ? `step ${Math.min(t.step, t.steps)} of ${t.steps}` : "";
    return ["Task", t?.state ?? "", progress, age].filter(Boolean).join(" · ");
  }
  return ["Chat", age].filter(Boolean).join(" · ");
}

function buildCard(slot: Slot, band: Band, a: ListActions, now: number, ui: ItemsUi): HTMLElement {
  const card = testid(el("article", `m-thread m-thread-${band}`), "m-thread");
  card.dataset.key = slot.key;
  card.dataset.band = band;
  card.dataset.kind = slot.kind;
  if (slot.threadId) card.dataset.thread = slot.threadId;
  if (slot.taskId) card.dataset.task = slot.taskId;
  card.dataset.state = slot.entry?.status ?? slot.task?.state ?? "";
  if (slot.kind === "row") {
    // A row alone: its head names the kind; the item below is the row.
    card.append(testid(el("h3", "m-thread-head m-thread-kind", kindWord(slot.rows[0])), "m-thread-kind"));
  } else {
    const open = slot.threadId && slot.kind !== "task" ? () => a.openThread(slot.threadId!) : () => a.openTask(slot.taskId!);
    const head = tap("", open, "m-thread-open", "m-thread-open");
    head.append(el("span", "m-thread-title", threadTitle(slot)), el("span", "m-thread-meta", threadMeta(slot, now)));
    card.append(head);
  }
  if (slot.rows.length) {
    card.append(buildItems(slot, a, ui));
  }
  return card;
}

function cardFor(slot: Slot, band: Band, a: ListActions, cache: SlotCache, now: number): HTMLElement {
  const sig = slotSignature(slot, band, a.answers, now, cache.ui);
  const hit = cache.map.get(slot.key);
  if (hit && hit.sig === sig) return hit.node;
  const node = buildCard(slot, band, a, now, cache.ui);
  cache.builds += 1;
  cache.map.set(slot.key, { sig, node });
  return node;
}

// -- the list ---------------------------------------------------------------------------------

function heading(text: string, id: string): HTMLElement {
  return testid(el("h2", "group m-band-head", text), id);
}

function note(text: string, id: string, cls = "m-band-note"): HTMLElement {
  return testid(el("p", cls, text), id);
}

function strip(model: ListModel, a: ListActions, running: number): HTMLElement {
  const bar = testid(el("div", "m-strip"), "m-strip");
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "Show");
  const waitingN = model.rows ? waitingCount(model.rows) : 0;
  const pending = !!model.rows?.some((r) => r.rank <= 2);
  const chip = (id: Exclude<Filter, null>, label: string, tone = "", count: string | null = null) => {
    const b = tap("", () => a.setFilter(model.filter === id ? null : id), "chip", "m-chip-" + id);
    // The count never shrinks: a long label gives way to an ellipsis, never the number.
    b.append(el("span", "m-chip-label", label));
    if (count !== null) b.append(el("span", "m-chip-count", ` · ${count}`));
    b.setAttribute("aria-pressed", String(model.filter === id));
    if (tone) b.dataset.tone = tone;
    bar.append(b);
  };
  chip("needs", "Needs you", pending ? "pending" : "", model.rows === null && model.error !== null ? "?" : String(waitingN));
  chip("running", "Running", "", String(running));
  chip("tasks", "Tasks");
  return bar;
}

export function threadsView(model: ListModel, a: ListActions, cache: SlotCache, now: number,
                            tasksPanel: HTMLElement | null, secure?: { ok: boolean; label: string } | null): HTMLElement {
  const view = testid(el("section", "view m-threads"), "m-inbox");
  const loading = model.rows === null && model.error === null;
  if (loading) view.setAttribute("aria-busy", "true");
  // The inbox read failed: the list stays, dimmed (its own state; the app is not offline).
  if (model.error) view.dataset.error = "true";
  const rows = model.rows ?? [];
  // A first inbox read that failed (rows null, error set) still shows what the other reads brought: the
  // running threads and tasks. It never says Nothing needs you, and never All clear.
  const unread = model.rows === null && model.error !== null;
  const slots = loading ? [] : buildSlots(model.entries, rows, model.tasks);
  const grouped = bands(slots);
  view.append(strip(model, a, grouped.running.length));
  if (secure && !secure.ok) {
    // §4.1 (P-4): the phone answers with Discord's authority only.
    view.append(testid(el("p", "m-limited", `limited: ${secure.label || "a check failed"}`), "m-limited"));
  }
  if (model.error) {
    const banner = testid(el("div", "m-error"), "m-list-error");
    banner.setAttribute("role", "alert");
    banner.append(el("span", "", model.error), tap("Retry", a.retry, "quiet m-retry", "m-retry"));
    view.append(banner);
  }
  if (model.filter === "tasks") {
    if (tasksPanel) view.append(tasksPanel);
    cache.prune(new Set());
    return view;
  }
  if (loading) {
    for (let i = 0; i < 3; i++) view.append(el("div", "m-skeleton"));
    view.append(el("p", "m-sr", "Loading…"));
    return view;
  }
  const keep = new Set<string>();
  const draw = (list: Slot[], band: Band, into: HTMLElement) => {
    for (const slot of list) {
      keep.add(slot.key);
      into.append(cardFor(slot, band, a, cache, now));
    }
  };
  const all = model.filter === null;
  const showNeeds = all || model.filter === "needs";
  const showRunning = all || model.filter === "running";
  const showRecent = all;

  if (showNeeds) {
    const sec = testid(el("section", "m-band"), "m-band-needs");
    sec.dataset.band = "needs";
    sec.append(heading(grouped.needs.length || unread ? "Needs you" : "Nothing needs you", "m-band-needs-head"));
    if (unread) sec.append(note("The Inbox could not be read", "m-inbox-unreadable"));
    draw(grouped.needs, "needs", sec);
    view.append(sec);
  }
  if (showRunning && (grouped.running.length || model.working === "unavailable" || model.filter === "running" || model.tasksMore)) {
    const sec = testid(el("section", "m-band"), "m-band-running");
    sec.dataset.band = "running";
    sec.append(heading("Running", "m-band-running-head"));
    // Route-specific (Sol 3): a failed working read says so here, and nothing else about the app changes.
    if (model.working === "unavailable") sec.append(note("In progress unavailable", "m-working-unavailable", "m-band-note m-unavailable"));
    if (!grouped.running.length && model.working !== "unavailable") sec.append(note("Nothing running", "m-running-none"));
    draw(grouped.running, "running", sec);
    if (model.tasksMore) {
      sec.append(note("Showing the newest 200 tasks: older ones, running ones included, are at the desk", "m-tasks-more"));
    }
    view.append(sec);
  }
  if (showRecent && (grouped.recent.length || grouped.info.length)) {
    const sec = testid(el("section", "m-band"), "m-band-recent");
    sec.dataset.band = "recent";
    sec.append(heading("Recent", "m-band-recent-head"));
    draw(grouped.recent.slice(0, RECENT_SHOWN), "recent", sec);
    if (grouped.recent.length > RECENT_SHOWN) {
      // Rank 6 and below only (a rank 1 to 5 row is Needs you): the cards stay in the page, folded.
      const rest = testid(el("div", "m-recent-rest"), "m-recent-rest");
      rest.hidden = !model.recentAll;
      draw(grouped.recent.slice(RECENT_SHOWN), "recent", rest);
      const toggle = tap(model.recentAll ? "Show fewer" : `Show all (${grouped.recent.length})`,
        () => a.setRecentAll(!model.recentAll), "quiet m-recent-all", "m-recent-all");
      toggle.setAttribute("aria-expanded", String(model.recentAll));
      sec.append(toggle, rest);
    }
    if (grouped.info.length) {
      const n = grouped.info.reduce((sum, s) => sum + s.rows.length, 0);
      const fold = testid(el("details", "m-info"), "m-info");
      fold.append(el("summary", "m-info-summary", `${n} for your information`));
      draw(grouped.info, "recent", fold);
      sec.append(fold);
    }
    view.append(sec);
  }
  if (all && !slots.length && !unread) view.append(empty("All clear"));
  // What the phone does not show.
  if (all || model.filter === "needs") {
    if (model.workingCut.more && model.working === "ok") {
      view.append(note(`Showing ${model.entries?.length ?? 0} of ${model.workingCut.total}: the rest are at the desk`, "m-working-more"));
    }
    if (model.inboxMore) {
      view.append(note("More waiting than the phone shows: open the Inbox at the desk", "m-inbox-more"));
    }
  }
  cache.prune(keep);
  return view;
}
