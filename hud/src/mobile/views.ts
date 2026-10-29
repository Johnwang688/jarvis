// The phone app's views (phone design §6, P-3): the Inbox, Chats and Tasks
// tabs, read-only. Each is a function from data to elements, so the tests
// (views.test.ts) build them in jsdom without a server.
//
// **Every string from the daemon is set with textContent** (inbox A1's rule
// for `origin` and every `detail` field; the same for titles, briefs, plans,
// reports, roles and patches). The one exception is an assistant's reply,
// which goes through the sanitized Markdown renderer (./markdown.ts).
// P-4: given `InboxAnswers`, an approval row opens its answering card
// (./card.ts) and a question, a task question or an open decision takes its
// answer here: a text box and Send, or a decision's options and Answer.
// Nothing is answered by a tap on the row itself, by Enter, or without the
// button. Without them (P-3's read-only view) every row says "Answer at the
// desk". P-5: given `withdraw`, a proposal takes Withdraw (a confirmation tap,
// the session only); a task's start, resume and cancel are on its own view
// (./write.ts), and the transcript takes a message box.
// QH: an approval deferred during quiet hours shows the card it would have
// shown, "Approve and retry" (the app asks for the passkey first, as for an
// Allow) where the daemon and the HUD's rules allow it, and "Leave it".

import { clockOf, decisionOf, deferredOf, groupRows, oneLine, questionOf, briefOf, textBlock, type InboxRow } from "../lib/inbox";
import { recentChats, moreLabel } from "../lib/recentChats";
import { AI_LABEL, recordSummary, startsCollapsed, summaryView } from "../lib/approvalSummary";
import { rowCommand, summaryLines } from "./card";
import type { Thread } from "../types";
import { renderPhoneMarkdown } from "./markdown";
import {
  ASSISTANT_ROLES, deskNote, lineKind, orderTasks,
  type ChatThread, type DiffView, type LogRow, type Project, type TaskView,
} from "./model";

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function testid<T extends HTMLElement>(node: T, id: string): T {
  node.dataset.testid = id;
  return node;
}

/** A tappable row or link (at least 44 px tall, mobile.css): navigation only. */
export function tap(label: string, onClick: () => void, cls = "nav", id?: string): HTMLButtonElement {
  const b = el("button", "tap " + cls, label);
  b.type = "button";
  if (id) b.dataset.testid = id;
  b.addEventListener("click", onClick);
  return b;
}

function time(at: string | null | undefined): string {
  return clockOf(at ?? null);
}

function when(at: string | null | undefined): string {
  const ms = typeof at === "string" ? Date.parse(at) : NaN;
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return clockOf(ms).slice(0, 5);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function empty(text: string): HTMLElement {
  return testid(el("p", "empty", text), "m-empty");
}

// -- Inbox ---------------------------------------------------------------------------

function lines(parent: HTMLElement, label: string, items: string[], cls = "") {
  if (!items.length) return;
  parent.append(el("div", "label", label));
  const list = el("ul", "plain " + cls);
  for (const item of items) list.append(el("li", "", item));
  parent.append(list);
}

function approvalDetail(row: InboxRow, box: HTMLElement, a?: InboxAnswers) {
  const d = row.detail;
  if (d.hud_only === true) {
    box.append(testid(el("p", "muted", `${oneLine(d.tool, 60) || "A tool"}: a private card. Its detail is shown only at the desk.`), "m-private"));
    return;
  }
  const headline = textBlock(d.headline, 400);
  if (headline) box.append(el("p", "headline", headline));
  // AS: the AI summary and code's WHY ASKED and WHERE, then the tool and the
  // command, collapsed only with a summary pending or there (and a toggle).
  summaryLines(d, box, "m-row");
  box.append(el("div", "label", "Tool"), el("p", "", oneLine(d.tool, 80)));
  const command = textBlock(d.command, 4000);
  let shown = "";
  if (d.args && typeof d.args === "object" && !command) {
    try { shown = JSON.stringify(d.args, null, 1).slice(0, 4000); } catch { shown = ""; }
    if (shown === "{}") shown = "";
  }
  const toggle = a?.toggle ? (open: boolean) => a.toggle!(row, open) : undefined;
  const open = !toggle || (a?.expanded?.get(row.id) ?? !startsCollapsed(summaryView(d)));
  rowCommand(command, shown, open, toggle, box, { command: "m-command", args: "m-args", prefix: "m-command" });
}

/** Any other kind's detail (one P-3 does not know, such as A3's missed rows): its text fields, as text. */
function deferredDetail(row: InboxRow, box: HTMLElement, a?: InboxAnswers) {
  const d = deferredOf(row)!;
  if (d.headline) box.append(el("p", "headline", d.headline));
  // AS: the record's AI summary and code's lines; the command collapsed only when there is a summary.
  const view = recordSummary(d.summary);
  summaryLines({ summary: view.text, summary_state: view.slot ? "ready" : undefined, reason: d.reason, layer: d.layer,
                 where: d.where }, box, "m-deferred");
  if (d.tool) box.append(el("div", "label", "Tool"), el("p", "", d.tool));
  if (d.count > 1) box.append(testid(el("p", "muted small", `Asked ${d.count} times while it waited.`), "m-deferred-count"));
  const toggle = a?.toggle ? (open: boolean) => a.toggle!(row, open) : undefined;
  const open = !toggle || (a?.expanded?.get(row.id) ?? !view.slot);
  rowCommand(d.command, d.args, open, toggle, box, { command: "m-command", args: "m-deferred-args", prefix: "m-command" });
  box.append(testid(el("p", "muted small", d.more
    ? `${d.more} more asks from this chat were denied overnight, too many to list one by one.`
    : d.expired ? "Over a day old: it can only be left."
      : d.whyNot ? d.whyNot
        : "Denied while you slept. Approve and retry allows exactly this call once, for 30 minutes."), "m-deferred-note"));
}

function genericDetail(row: InboxRow, box: HTMLElement) {
  for (const [key, value] of Object.entries(row.detail).slice(0, 12)) {
    if (typeof value !== "string" && typeof value !== "number") continue;
    if (/(^|_)id$/.test(key)) continue;
    const text = textBlock(String(value), 2000);
    if (text) box.append(el("div", "label", oneLine(key.replace(/_/g, " "), 40)), el("p", "", text));
  }
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** A list of untrusted names (check names, paths), each in its own <bdi>, then "+N more". */
function names(parent: HTMLElement, items: unknown, more: unknown) {
  const list = Array.isArray(items) ? items.map((x) => oneLine(x, 120)).filter(Boolean).slice(0, 20) : [];
  list.forEach((name, i) => parent.append(i ? ", " : " ", el("bdi", "", name)));
  const n = typeof more === "number" && more > 0 ? Math.floor(more) : 0;
  if (n) parent.append(` +${n} more`);
}

/**
 * H4 (phone audit), until PM-F: a PR row's title, code's facts line and the
 * AI summary's Does and Blocked lines, labelled as the model's. Never the
 * head SHA, never a link, never the raw keys. Each facts part is code's own
 * words (pr_facts.facts_parts), drawn apart with the phone's own separators;
 * a part's names are untrusted, each in its own <bdi>.
 */
function prDetail(row: InboxRow, box: HTMLElement) {
  const d = row.detail;
  const title = oneLine(d.title, 300);
  if (title) box.append(testid(el("p", "pr-title", title), "m-pr-title"));
  const facts = testid(el("p", "pr-facts"), "m-pr-facts");
  if (Array.isArray(d.facts)) {
    d.facts.filter(isObj).slice(0, 16).forEach((part) => {
      const words = oneLine(part.text, 200);
      if (!words) return;
      const span = el("span", "pr-fact", words);
      names(span, part.items, part.more);
      if (facts.childNodes.length) facts.append(" · ");
      facts.append(span);
    });
  } else if (typeof d.facts === "string") {
    facts.append(el("span", "pr-fact", oneLine(d.facts, 400)));
  }
  if (facts.childNodes.length) box.append(facts);
  const flags = isObj(d.flags) ? d.flags : null;
  if (flags && Array.isArray(flags.items) && flags.items.length) {
    const line = testid(el("p", "pr-flags", "Touches safety files:"), "m-pr-flags");
    names(line, flags.items.map((f) => (isObj(f) ? f.name : "")), flags.more);
    box.append(line);
  }
  // The model's words, set apart and labelled (the AS slot's look); only when there.
  const sum = d.summary;
  const does = isObj(sum) ? textBlock(sum.does, 400) : typeof sum === "string" ? textBlock(sum, 400) : "";
  const blocked = isObj(sum) ? textBlock(sum.blocked, 400) : "";
  if (does || blocked) {
    const slot = testid(el("div", "as-ai as-ready pr-summary"), "m-pr-summary");
    slot.append(el("span", "as-tag", AI_LABEL));
    if (does) slot.append(testid(el("div", "as-text", (isObj(sum) ? "Does: " : "") + does), "m-pr-does"));
    if (blocked) slot.append(testid(el("div", "as-text", "Blocked: " + blocked), "m-pr-blocked"));
    box.append(slot);
  }
}

/** What the inbox may answer (P-4), and the app's state for it. */
export interface InboxAnswers {
  openCard: (row: InboxRow) => void;
  send: (row: InboxRow, body: Record<string, unknown>) => void;
  /** QH: Approve and retry on a deferred row; the app asks for the passkey first. */
  retry: (row: InboxRow) => void;
  /** Rows whose answer is on its way. */
  busy: ReadonlySet<string>;
  /** A row's answer note ("Sent.", a refusal's words). */
  notes: ReadonlyMap<string, string>;
  /** Typed answers and picked options, kept across a read again. */
  drafts: Map<string, string>;
  picks: Map<string, number>;
  /** AS: rows whose command the owner opened (true) or closed (false), and the toggle. */
  expanded?: ReadonlyMap<string, boolean>;
  toggle?: (row: InboxRow, open: boolean) => void;
  /** P-5: a proposal's Withdraw, asked once before it is sent. */
  withdraw?: { confirming: ReadonlySet<string>; ask: (row: InboxRow) => void; back: (row: InboxRow) => void };
}

/** The kinds the phone answers itself (P-4). */
export function answersHere(row: InboxRow, a?: InboxAnswers): boolean {
  if (row.kind === "approval" || row.kind === "question" || row.kind === "task_question") return true;
  if (deferredOf(row)) return true;
  if (row.kind === "proposal") return !!a?.withdraw;
  return row.kind === "decision" && !!decisionOf(row)?.open;
}

function sendButton(label: string, id: string, onClick: () => void): HTMLButtonElement {
  const b = tap(label, onClick, "quiet m-send", id);
  return b;
}

function answerBox(row: InboxRow, a: InboxAnswers): HTMLElement {
  const box = testid(el("div", "m-answer"), "m-answer-box");
  const busy = a.busy.has(row.id);
  const deferred = deferredOf(row);
  if (row.kind === "approval") {
    // The row's one action, edged in the accent (M7 of the phone audit).
    box.append(tap("Open the card to answer", () => a.openCard(row), "quiet m-send primary", "m-open-card"));
  } else if (deferred) {
    // Plain buttons, neither defaulted; the retry asks for the passkey first.
    // H6 (phone audit): as the Review tab lays them out, Leave it at the left
    // and Approve and retry at the right, far apart, never stacked.
    const acts = testid(el("div", "m-review-rowacts"), "m-deferred-acts");
    const leave = tap("Leave it", () => a.send(row, { action: "leave" }), "quiet m-send", "m-deferred-leave");
    leave.disabled = busy;
    acts.append(leave);
    if (deferred.grantable) {
      const retry = tap("Approve and retry", () => a.retry(row), "quiet m-send", "m-deferred-retry");
      retry.disabled = busy;
      acts.append(retry);
    }
    box.append(acts);
  } else if (row.kind === "proposal" && a.withdraw) {
    const w = a.withdraw;
    if (w.confirming.has(row.id)) {
      box.append(testid(el("p", "m-ask", "Withdraw this proposal? The task will not start."), "m-withdraw-ask"));
      const keep = tap("Keep it", () => w.back(row), "quiet m-send", "m-withdraw-back");
      const go = sendButton("Withdraw it", "m-withdraw-send", () => a.send(row, { withdraw: true }));
      keep.disabled = busy;
      go.disabled = busy;
      box.append(keep, go);
    } else {
      const ask = tap("Withdraw", () => w.ask(row), "quiet m-send", "m-withdraw");
      ask.disabled = busy;
      box.append(ask);
    }
  } else if (row.kind === "question" || row.kind === "task_question") {
    const text = testid(el("textarea", "m-text"), "m-answer-text");
    text.setAttribute("aria-label", "Your answer");
    text.placeholder = "Your answer";
    text.rows = 2;
    text.value = a.drafts.get(row.id) ?? "";
    text.readOnly = busy;
    const send = sendButton("Send answer", "m-answer-send", () => {
      const value = text.value.trim();
      if (value) a.send(row, { text: value });
    });
    const sync = () => { a.drafts.set(row.id, text.value); send.disabled = busy || !text.value.trim(); };
    text.addEventListener("input", sync);
    sync();
    box.append(text, send);
  } else {
    const d = decisionOf(row)!;
    const group = testid(el("div", "m-choices"), "m-decision-options");
    group.setAttribute("role", "radiogroup");
    const text = testid(el("textarea", "m-text"), "m-decision-text");
    // M4 (phone audit), as the Review tab (MR-3): the suggestion is marked, never chosen.
    const picked = () => a.picks.get(row.id) ?? -1;
    const send = sendButton("Answer", "m-decision-send", () => {
      const words = text.value.trim();
      if (words) a.send(row, { text: words });
      else if (picked() >= 0) a.send(row, { choice: picked() });
    });
    const sync = () => {
      a.drafts.set(row.id, text.value);
      send.disabled = busy || (!text.value.trim() && picked() < 0);
    };
    d.options.forEach((option, i) => {
      const label = el("label", "m-choice");
      const radio = testid(el("input", ""), "m-decision-option");
      radio.type = "radio";
      radio.name = "decision-" + row.id;
      radio.value = String(i);
      radio.checked = picked() === i;
      radio.disabled = busy;
      radio.addEventListener("change", () => { if (radio.checked) { a.picks.set(row.id, i); sync(); } });
      // The suggestion's marker is the phone's own element before the option's
      // words, never text an option could write (as the HUD's and P-3's).
      label.append(radio, " ");
      if (i === d.suggested) label.append(testid(el("span", "suggested", "suggested"), "m-suggested"), " ");
      label.append(el("span", "option-text", option));
      group.append(label);
    });
    text.setAttribute("aria-label", "Or answer in your own words");
    text.placeholder = "Or answer in your own words";
    text.rows = 2;
    text.value = a.drafts.get(row.id) ?? "";
    text.readOnly = busy;
    text.addEventListener("input", sync);
    sync();
    box.append(group, text, send);
  }
  const note = a.notes.get(row.id);
  if (note) {
    const line = testid(el("p", "m-notice", note), "m-answer-note");
    line.setAttribute("role", "status");
    box.append(line);
  }
  return box;
}

export function inboxRow(row: InboxRow, open: (row: InboxRow) => void, answers?: InboxAnswers): HTMLElement {
  const card = testid(el("article", `row rank-${row.rank}`), "m-inbox-row");
  card.dataset.kind = oneLine(row.kind, 40);
  card.dataset.id = row.id;
  card.append(el("h3", "row-title", row.title || row.id));
  if (row.blocks) card.append(el("p", "row-blocks", row.blocks));
  if (row.origin) card.append(testid(el("p", "row-origin", row.origin), "m-origin"));
  const meta = [row.asked_at ? `asked ${time(row.asked_at)}` : "", row.expires_at ? `expires ${time(row.expires_at)}` : ""]
    .filter(Boolean).join(" · ");
  if (meta) card.append(el("p", "row-meta", meta));
  const box = testid(el("div", "row-detail"), "m-detail");
  // AS-12: an approval's and a deferral's buttons come before its detail.
  const first = (row.kind === "approval" || !!deferredOf(row)) && !!answers && answersHere(row, answers);
  if (first) card.append(answerBox(row, answers!));
  if (row.kind === "approval") approvalDetail(row, box, answers);
  else if (deferredOf(row)) deferredDetail(row, box, answers);
  else if (row.kind === "question" || row.kind === "task_question") {
    const q = questionOf(row);
    if (q.text) box.append(testid(el("p", "question", q.text), "m-question"));
    lines(box, "Options", q.options, "options");
  } else if (row.kind === "proposal" || row.kind === "task") {
    const brief = briefOf(row);
    if (brief) box.append(testid(el("p", "brief", brief), "m-brief"));
  } else if (row.kind === "decision" && decisionOf(row)) {
    // Inbox B1: the agent's question, options and why, read-only (P-3).
    const d = decisionOf(row)!;
    if (d.question) box.append(testid(el("p", "question", d.question), "m-question"));
    // The suggestion's marker is the phone's own element, before the option's
    // words, never text an option could also write (review of PR #309). When
    // the row answers here its radios list the options (M4 of the phone
    // audit: not twice).
    if (!(answers && answersHere(row, answers))) {
      box.append(el("div", "label", "Options"));
      const list = el("ul", "plain options");
      d.options.forEach((option, i) => {
        const item = el("li", "");
        if (i === d.suggested) item.append(testid(el("span", "suggested", "suggested"), "m-suggested"), " ");
        item.append(el("span", "option-text", option));
        list.append(item);
      });
      box.append(list);
    }
    if (d.why) box.append(el("div", "label", "Why"), testid(el("p", "", d.why), "m-why"));
    if (!d.open) box.append(el("div", "label", "Your answer"), testid(el("p", "", d.answer), "m-answer"));
  } else if (row.kind === "pr") prDetail(row, box);
  else genericDetail(row, box);
  if (box.childNodes.length) card.append(box);
  if (first) {
    // The buttons are already above.
  } else if (answers && answersHere(row, answers)) {
    card.append(answerBox(row, answers));
  } else {
    // A decision already answered (rank 8, recorded for a closed chat) takes no answer.
    const note = row.kind === "decision" && !decisionOf(row)?.open ? null : deskNote(row.kind);
    if (note) card.append(testid(el("p", "desk-only", note), "m-desk-only"));
  }
  if (row.thread_id || row.task_id) {
    card.append(tap(row.thread_id ? "Open the chat" : "Open the task", () => open(row), "link", "m-open"));
  }
  return card;
}

export function inboxView(rows: InboxRow[] | null, open: (row: InboxRow) => void, answers?: InboxAnswers,
                          secure?: { ok: boolean; label: string } | null): HTMLElement {
  const view = testid(el("section", "view"), "m-inbox");
  if (secure && !secure.ok) {
    // §4.1 (P-4): the phone answers with Discord's authority only.
    view.append(testid(el("p", "m-limited", `limited: ${secure.label || "a check failed"}`), "m-limited"));
  }
  if (rows === null) { view.append(empty("Loading…")); return view; }
  if (!rows.length) { view.append(empty("Nothing is waiting on you.")); return view; }
  for (const { group, rows: mine } of groupRows(rows)) {
    view.append(el("h2", "group", group.label));
    for (const row of mine) view.append(inboxRow(row, open, answers));
  }
  return view;
}

// -- Chats ---------------------------------------------------------------------------

function asThread(t: ChatThread): Thread {
  return { id: t.id, project_id: t.project_id, updated: t.updated, running: t.running } as unknown as Thread;
}

export function chatsView(projects: Project[] | null, threads: ChatThread[], expanded: ReadonlySet<string>,
                          actions: { open: (t: ChatThread) => void; toggle: (projectId: string) => void }): HTMLElement {
  const view = testid(el("section", "view"), "m-chats");
  if (projects === null) { view.append(empty("Loading…")); return view; }
  if (!projects.length) { view.append(empty("No projects.")); return view; }
  for (const project of projects) {
    const chats = threads.filter((t) => t.project_id === project.id && !t.task_id);
    const section = testid(el("section", "project"), "m-project");
    section.dataset.id = project.id;
    section.append(el("h2", "group", project.name));
    const byId = new Map(chats.map((t) => [t.id, t]));
    const recent = recentChats(chats.map(asThread), { expanded: expanded.has(project.id) });
    if (!recent.shown.length) section.append(el("p", "muted small", "No chats."));
    for (const shown of recent.shown) {
      const t = byId.get(shown.id);
      if (!t) continue;
      const row = tap("", () => actions.open(t), "chat", "m-chat");
      row.dataset.id = t.id;
      row.append(el("span", "chat-title", t.title || "(untitled)"));
      const meta = el("span", "chat-meta", [t.provider, t.running ? "working" : "", when(t.updated)].filter(Boolean).join(" · "));
      if (t.running) row.classList.add("running");
      row.append(meta);
      section.append(row);
    }
    if (recent.more) {
      section.append(tap(moreLabel(recent.hidden, expanded.has(project.id)), () => actions.toggle(project.id),
        "link", "m-more"));
    }
    view.append(section);
  }
  return view;
}

/** The marks (chat_reads `_owner_row`) of words the owner typed into this chat:
 * only an owner row whose every mark is one of these is drawn as the owner's
 * bubble. Anything else ("via the inbox (not typed in this chat)", a phone
 * decision's answer, "steering", "not sent", a mark this list does not know)
 * is a flat row with its marks above the words (review of #331, MED 1). */
export const TYPED_HERE_MARKS: ReadonlySet<string> = new Set(["via the phone", "via Discord", "via DM", "spoken"]);
/** A mark shown in amber: the message never ran. */
const WARN_MARKS: ReadonlySet<string> = new Set(["not sent"]);

/** How a transcript row is drawn (Claude/Codex style, the owner's choice
 * 2026-10-28): the owner's own words typed in this chat in a right-aligned
 * bubble; an owner row with any other provenance flat, its marks above it; a
 * reply as plain full-width Markdown; anything else (the daemon's own lines,
 * another chat's relay) as one muted line that a tap opens to the whole text.
 * A peer or a relay is never a bubble: it is not the owner's words. */
export function rowStyle(role: string, marks: readonly string[] = [], ownerChat = true): "owner" | "flat" | "reply" | "step" {
  if (role === "owner") return ownerChat && marks.every((m) => TYPED_HERE_MARKS.has(m)) ? "owner" : "flat";
  return ASSISTANT_ROLES.has(role) ? "reply" : "step";
}

/** A step's role split so the folded line can cut its middle, never its end:
 * "chat Docs (d7278e2a), not the owner" keeps " (d7278e2a), not the owner" whole. */
export function roleParts(role: string): [string, string] {
  const at = role.lastIndexOf(" (");
  if (at > 0) return [role.slice(0, at), role.slice(at)];
  const not = ", not the owner";
  if (role.endsWith(not) && role.length > not.length) return [role.slice(0, -not.length), not];
  return [role, ""];
}

/** An owner's own chat, known to be one: only there is an unmarked owner row the owner's typing. A
 * task's threads take the runner's prompts as "owner" rows (review of #331), and an unknown thread
 * is not assumed to be a chat. */
export function ownerChat(meta: ChatThread | null | undefined): boolean {
  return !!meta && meta.role === "chat" && !meta.task_id;
}

/** The classes only code puts on an owner row (C1 of the phone audit): no
 * reply's Markdown can carry any of them (the sanitizer keeps no `class`). */
export const OWNER_ROW_CLASSES: readonly string[] = ["msg-owner", "msg-flat", "msg-bubble", "msg-meta", "who-owner"];

/** The owner's mark (C1): a code-built "You" chip, drawn on every owner row
 * (bubble and flat) before its marks. Hidden from screen readers, which hear
 * the row's "You: " instead (M13). */
function ownerMark(): HTMLElement {
  const mark = testid(el("span", "who-owner", "You"), "m-owner-mark");
  mark.setAttribute("aria-hidden", "true");
  return mark;
}

export function logRow(row: LogRow, inOwnerChat = true): HTMLElement {
  const style = rowStyle(row.role, row.marks, inOwnerChat);
  const item = testid(el("article", "msg msg-" + style), "m-msg");
  item.dataset.role = row.role;
  item.dataset.seq = String(row.seq);
  const cut = row.cut ? el("p", "muted small", "The rest of this message is shown at the desk.") : null;
  if (style === "owner") {
    // M13: who said it, first, for VoiceOver (the chip below is hidden from it).
    item.append(el("span", "m-sr", "You: "), el("div", "msg-bubble plain-text", row.text));
    const meta = testid(el("div", "msg-meta"), "m-msg-meta");
    meta.append(ownerMark());
    for (const part of [...row.marks, time(row.at).slice(0, 5)].filter(Boolean)) meta.append(" · ", part);
    item.append(meta);
    if (cut) item.append(cut);
    return item;
  }
  if (style === "flat") {
    // Where the words came from first, then the words, never as a bubble.
    item.append(el("span", "m-sr", "You: "));
    const head = testid(el("div", "msg-head"), "m-msg-marks");
    const parts: HTMLElement[] = [ownerMark()];
    for (const mark of row.marks) parts.push(el("span", WARN_MARKS.has(mark) ? "mark mark-warn" : "mark", mark));
    const at = time(row.at).slice(0, 5);
    if (at) parts.push(el("span", "", at));
    parts.forEach((part, i) => { if (i) head.append(" · "); head.append(part); });
    item.append(head, el("div", "msg-body plain-text", row.text));
    if (cut) item.append(cut);
    return item;
  }
  if (style === "reply") {
    item.append(el("div", "msg-head", [row.role, ...row.marks, time(row.at).slice(0, 5)].filter(Boolean).join(" · ")));
    const body = el("div", "msg-body md");
    renderPhoneMarkdown(body, row.text);
    item.append(body);
    if (cut) item.append(cut);
    return item;
  }
  // A step: folded to one line; the whole text (all the detail there is) inside,
  // after the whole role, so "…, not the owner" is always readable (review of #331, MED 2).
  const who = [row.role, ...row.marks].filter(Boolean).join(" · ");
  const fold = testid(el("details", "step"), "m-step");
  const summary = el("summary", "step-line");
  const first = row.text.split("\n", 1)[0];
  summary.title = who;
  summary.setAttribute("aria-label", first ? `${who}: ${first}` : who);
  const [head, tail] = roleParts(row.role);
  const role = el("span", "step-role");
  role.append(el("span", "step-role-head", head), el("span", "step-role-tail", tail));
  if (row.marks.length) role.append(el("span", "step-role-marks", " · " + row.marks.join(" · ")));
  summary.append(role, el("span", "step-text", first), el("span", "step-time", time(row.at).slice(0, 5)));
  fold.append(summary, testid(el("div", "step-who", who), "m-step-who"), el("div", "step-body plain-text", row.text));
  if (cut) fold.append(cut);
  item.append(fold);
  return item;
}

/** H5 (phone audit): what an empty transcript says. "No messages yet." only
 * once the log was read; before that "Loading…", or, offline, that it was
 * never read (never a false "empty"). */
export function emptyLog(loaded: boolean, online: boolean): string {
  return loaded ? "No messages yet." : online ? "Loading…" : "Not read yet: you're offline.";
}

export function transcriptView(thread: ChatThread | null, rows: LogRow[], older: boolean,
                               back: () => void, composer?: HTMLElement | null,
                               read: { loaded: boolean; online: boolean } = { loaded: true, online: true },
): { view: HTMLElement; list: HTMLElement } {
  const view = testid(el("section", "view transcript-view"), "m-thread");
  const bar = el("div", "subbar");
  bar.append(tap("‹ Chats", back, "back", "m-back"));
  bar.append(testid(el("h2", "subtitle", thread?.title || "(untitled)"), "m-thread-title"));
  view.append(bar);
  if (older) view.append(el("p", "muted small center", "Older messages are at the desk."));
  const list = testid(el("div", "transcript"), "m-transcript");
  for (const row of rows) list.append(logRow(row, ownerChat(thread)));
  if (!rows.length) list.append(empty(emptyLog(read.loaded, read.online)));
  view.append(list);
  // P-5: the message box (./write.ts) for an owner's chat, or the app's
  // "read only" line for any other; nothing until the chat is known.
  if (composer) view.append(composer);
  return { view, list };
}

// -- Tasks ---------------------------------------------------------------------------

function chip(state: string): HTMLElement {
  const c = testid(el("span", "chip state-" + state.replace(/[^a-z]/g, ""), state), "m-state");
  return c;
}

export function tasksView(tasks: TaskView[] | null, open: (t: TaskView) => void): HTMLElement {
  const view = testid(el("section", "view"), "m-tasks");
  if (tasks === null) { view.append(empty("Loading…")); return view; }
  if (!tasks.length) { view.append(empty("No tasks.")); return view; }
  for (const group of orderTasks(tasks)) {
    view.append(testid(el("h2", "group", group.label), "m-task-group"));
    for (const t of group.tasks) {
      const row = tap("", () => open(t), "task", "m-task");
      row.dataset.id = t.id;
      row.dataset.state = t.state;
      row.append(el("span", "task-brief", oneLine(t.brief, 140) || "(no brief)"));
      const meta = el("span", "task-meta");
      meta.append(chip(t.state));
      meta.append(el("span", "muted", " " + when(t.updated)));
      row.append(meta);
      view.append(row);
    }
  }
  return view;
}

/** The diff, one fold per file: open when there are three files or fewer,
 * else folded; `folds` remembers what the owner opened or folded. */
export function diffView(diff: DiffView, folds: Map<string, boolean> = new Map()): HTMLElement {
  const box = testid(el("div", "diff"), "m-diff");
  if (diff.truncated) box.append(testid(el("p", "desk-only", diff.note || "Too large to show here: open it at the desk."), "m-diff-note"));
  if (!diff.files.length) box.append(empty("No changes."));
  const fold = diff.files.length > 3;
  for (const file of diff.files) {
    const item = testid(el("details", "diff-file"), "m-diff-file");
    item.dataset.path = file.path;
    item.open = folds.get(file.path) ?? !fold;
    item.addEventListener("toggle", () => folds.set(file.path, item.open));
    const summary = el("summary", "diff-summary");
    summary.append(el("span", "diff-status", file.status), el("span", "diff-path", file.path),
      el("span", "diff-churn", `+${file.additions} −${file.deletions}`));
    item.append(summary);
    if (file.patch === null) {
      item.append(el("p", "desk-only", file.tooLarge ? "Too large to show here: open it at the desk." : "Not shown."));
    } else {
      const pre = el("pre", "patch");
      for (const line of file.patch.split("\n")) {
        pre.append(el("span", "pl " + lineKind(line), line + "\n"));
      }
      item.append(pre);
    }
    box.append(item);
  }
  return box;
}

export function taskView(task: TaskView, diff: DiffView | null | "loading" | string,
                         actions: { back: () => void; showDiff: () => void; folds?: Map<string, boolean>;
                                    controls?: HTMLElement | null;
                                    /** M5 (phone audit): the question's Inbox row, where the phone answers it. */
                                    answerInInbox?: (index: number) => void }): HTMLElement {
  const view = testid(el("section", "view"), "m-task-view");
  view.dataset.id = task.id;
  const bar = el("div", "subbar");
  bar.append(tap("‹ Tasks", actions.back, "back", "m-back"));
  const title = el("h2", "subtitle");
  title.append(chip(task.state));
  view.append(bar);
  bar.append(title);
  view.append(testid(el("p", "brief", task.brief || "(no brief)"), "m-task-brief"));
  const status = [task.steps ? `step ${task.step} of ${task.steps}` : "", task.branch].filter(Boolean).join(" · ");
  if (status) view.append(el("p", "muted small", status));
  // P-5: Start, Resume and Cancel (./write.ts), each asked first.
  if (actions.controls) view.append(actions.controls);
  if (task.openQuestion) view.append(el("div", "label", "Waiting on"), el("p", "", task.openQuestion));
  const open = task.questions.filter((q) => q.blocking && q.answer === null);
  for (const q of open) {
    view.append(el("div", "label", "Question"), testid(el("p", "question", q.text), "m-question"));
    lines(view, "Options", q.options, "options");
    const go = actions.answerInInbox;
    if (go) view.append(tap("Answer it in the Inbox", () => go(q.index), "quiet m-send primary", "m-answer-in-inbox"));
  }
  if (open.length && !actions.answerInInbox) view.append(testid(el("p", "desk-only", "Answer at the desk"), "m-desk-only"));
  if (task.plan.length) {
    view.append(el("div", "label", "Plan"));
    const ol = testid(el("ol", "plain plan"), "m-plan");
    for (const step of task.plan) ol.append(el("li", "", step));
    view.append(ol);
  }
  const r = task.report;
  if (r) {
    const box = testid(el("div", "report"), "m-report");
    box.append(el("h3", "group", "Last report"));
    lines(box, "Done", r.done);
    lines(box, "Changed", r.changed);
    if (r.verified) box.append(el("div", "label", "Verified"), el("p", "", r.verified));
    lines(box, "Open", r.open);
    if (r.next) box.append(el("div", "label", "Next"), el("p", "", r.next));
    view.append(box);
  }
  view.append(el("h3", "group", "Diff"));
  if (diff === null) view.append(tap("Show the diff", actions.showDiff, "link", "m-show-diff"));
  else if (diff === "loading") view.append(empty("Loading…"));
  else if (typeof diff === "string") view.append(testid(el("p", "muted", diff), "m-diff-error"));
  else view.append(diffView(diff, actions.folds));
  return view;
}
