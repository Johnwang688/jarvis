// The phone app's views (phone design §6, P-3): the Inbox, Chats and Tasks
// tabs, read-only. Each is a function from data to elements, so the tests
// (views.test.ts) build them in jsdom without a server.
//
// **Every string from the daemon is set with textContent** (inbox A1's rule
// for `origin` and every `detail` field; the same for titles, briefs, plans,
// reports, roles and patches). The one exception is an assistant's reply,
// which goes through the sanitized Markdown renderer (./markdown.ts).
// Nothing here can answer, send, withdraw, resume or cancel: approvals and
// questions say "Answer at the desk" until P-4 builds answering.

import { clockOf, decisionOf, groupRows, oneLine, questionOf, briefOf, textBlock, type InboxRow } from "../lib/inbox";
import { recentChats, moreLabel } from "../lib/recentChats";
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

function approvalDetail(row: InboxRow, box: HTMLElement) {
  const d = row.detail;
  if (d.hud_only === true) {
    box.append(testid(el("p", "muted", `${oneLine(d.tool, 60) || "A tool"}: a private card. Its detail is shown only at the desk.`), "m-private"));
    return;
  }
  const headline = textBlock(d.headline, 400);
  if (headline) box.append(el("p", "headline", headline));
  box.append(el("div", "label", "Tool"), el("p", "", oneLine(d.tool, 80)));
  const command = textBlock(d.command, 4000);
  if (command) box.append(el("div", "label", "Command"), testid(el("pre", "code", command), "m-command"));
  const reason = textBlock(d.reason, 2000);
  if (reason) box.append(el("div", "label", "Why"), el("p", "", reason));
  if (d.args && typeof d.args === "object" && !command) {
    let shown = "";
    try { shown = JSON.stringify(d.args, null, 1).slice(0, 4000); } catch { shown = ""; }
    if (shown && shown !== "{}") box.append(el("div", "label", "Arguments"), el("pre", "code", shown));
  }
}

/** Any other kind's detail (one P-3 does not know, such as A3's missed rows): its text fields, as text. */
function genericDetail(row: InboxRow, box: HTMLElement) {
  for (const [key, value] of Object.entries(row.detail).slice(0, 12)) {
    if (typeof value !== "string" && typeof value !== "number") continue;
    if (/(^|_)id$/.test(key)) continue;
    const text = textBlock(String(value), 2000);
    if (text) box.append(el("div", "label", oneLine(key.replace(/_/g, " "), 40)), el("p", "", text));
  }
}

export function inboxRow(row: InboxRow, open: (row: InboxRow) => void): HTMLElement {
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
  if (row.kind === "approval") approvalDetail(row, box);
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
    // words, never text an option could also write (review of PR #309).
    box.append(el("div", "label", "Options"));
    const list = el("ul", "plain options");
    d.options.forEach((option, i) => {
      const item = el("li", "");
      if (i === d.suggested) item.append(testid(el("span", "suggested", "suggested"), "m-suggested"), " ");
      item.append(el("span", "option-text", option));
      list.append(item);
    });
    box.append(list);
    if (d.why) box.append(el("div", "label", "Why"), testid(el("p", "", d.why), "m-why"));
    if (!d.open) box.append(el("div", "label", "Your answer"), testid(el("p", "", d.answer), "m-answer"));
  } else genericDetail(row, box);
  if (box.childNodes.length) card.append(box);
  // A decision already answered (rank 8, recorded for a closed chat) takes no answer.
  const note = row.kind === "decision" && !decisionOf(row)?.open ? null : deskNote(row.kind);
  if (note) card.append(testid(el("p", "desk-only", note), "m-desk-only"));
  if (row.thread_id || row.task_id) {
    card.append(tap(row.thread_id ? "Open the chat" : "Open the task", () => open(row), "link", "m-open"));
  }
  return card;
}

export function inboxView(rows: InboxRow[] | null, open: (row: InboxRow) => void): HTMLElement {
  const view = testid(el("section", "view"), "m-inbox");
  if (rows === null) { view.append(empty("Loading…")); return view; }
  if (!rows.length) { view.append(empty("Nothing is waiting on you.")); return view; }
  for (const { group, rows: mine } of groupRows(rows)) {
    view.append(el("h2", "group", group.label));
    for (const row of mine) view.append(inboxRow(row, open));
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

export function logRow(row: LogRow): HTMLElement {
  const item = testid(el("article", "msg"), "m-msg");
  item.dataset.role = row.role;
  item.dataset.seq = String(row.seq);
  const head = [row.role, ...row.marks, time(row.at)].filter(Boolean).join(" · ");
  item.append(el("div", "msg-head", head));
  const body = el("div", "msg-body");
  if (ASSISTANT_ROLES.has(row.role)) {
    body.classList.add("md");
    renderPhoneMarkdown(body, row.text);
  } else {
    body.classList.add("plain-text");
    body.textContent = row.text;
  }
  item.append(body);
  if (row.cut) item.append(el("p", "muted small", "The rest of this message is shown at the desk."));
  return item;
}

export function transcriptView(thread: ChatThread | null, rows: LogRow[], older: boolean,
                               back: () => void): { view: HTMLElement; list: HTMLElement } {
  const view = testid(el("section", "view transcript-view"), "m-thread");
  const bar = el("div", "subbar");
  bar.append(tap("‹ Chats", back, "back", "m-back"));
  bar.append(testid(el("h2", "subtitle", thread?.title || "(untitled)"), "m-thread-title"));
  view.append(bar);
  if (older) view.append(el("p", "muted small center", "Older messages are at the desk."));
  const list = testid(el("div", "transcript"), "m-transcript");
  for (const row of rows) list.append(logRow(row));
  if (!rows.length) list.append(empty("No messages yet."));
  view.append(list);
  view.append(el("p", "muted small center", "Reading only: sending from the phone comes later."));
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
                         actions: { back: () => void; showDiff: () => void; folds?: Map<string, boolean> }): HTMLElement {
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
  if (task.openQuestion) view.append(el("div", "label", "Waiting on"), el("p", "", task.openQuestion));
  const open = task.questions.filter((q) => q.blocking && q.answer === null);
  for (const q of open) {
    view.append(el("div", "label", "Question"), testid(el("p", "question", q.text), "m-question"));
    lines(view, "Options", q.options, "options");
  }
  if (open.length) view.append(testid(el("p", "desk-only", "Answer at the desk"), "m-desk-only"));
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
