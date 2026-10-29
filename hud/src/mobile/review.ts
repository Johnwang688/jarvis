// The phone's Review tab (MR-2 to MR-5, MR-11, MR-12;
// docs/plans/2026-10-28-morning-review.md): the morning review, built from
// `GET /m/api/review` (jarvis/v2/review.py) with the rules shared with the
// desk (../lib/review.ts). A view function from state to elements, so the
// tests build it in jsdom.
//
//   - a summary line and one jump chip per chat or task (a tap scrolls to
//     its anchor); groups by project, then chat or task, never folded;
//   - a live approval sits alone at the top ("Live now") and opens its card;
//   - a deferred approval shows its whole card and a checkbox, **unticked
//     until its own tap; there is no "select all"**; it also takes the
//     per-row Approve and retry (the only approval a limited phone has, the
//     owner's answer 3) and Leave it, far apart;
//   - questions, decisions and proposals are answered in place, one at a
//     time; once sent a row folds to a one-line receipt where it stood. A
//     decision's default is marked, never chosen;
//   - the sticky footer: "N selected", Leave selected (N) at the left,
//     Approve selected (N) at the right; Approve is off while any ticked
//     item cannot be approved (the footer names which), past 10, or on a
//     limited phone. Approve selected first lists exactly the ticked items,
//     then asks for Face ID once (app.ts).
// Every string from the daemon is set with textContent.

import { clockOf, decisionOf, deferredOf, oneLine, questionOf, briefOf, type InboxRow } from "../lib/inbox";
import {
  allItems, chipsOf, confirmationList, footerState, SECTIONS, groupItems, placeLabel, sectionTitle, summaryLine, whyWords,
  type ConfirmLine, type ReviewItem, type ReviewReport, type Section,
} from "../lib/review";
import { el, tap } from "./views";
import { AI_LABEL, recordSummary } from "../lib/approvalSummary";
import { rowCommand, summaryLines } from "./card";

export interface ReviewState {
  report: ReviewReport | null;
  /** Ticked row ids: empty until the owner's own taps. */
  selection: ReadonlySet<string>;
  /** Per-row words (a refusal, why it was unticked). */
  notes: ReadonlyMap<string, string>;
  /** Rows answered in place: shown as a one-line receipt where they stood. */
  receipts: ReadonlyMap<string, { item: ReviewItem; text: string }>;
  /** Rows whose answer is on its way. */
  busy: ReadonlySet<string>;
  /** Approve selected or Leave selected on its way. */
  groupBusy: boolean;
  /** Approve selected's list is open, waiting for the owner's "Approve with Face ID". */
  confirming: boolean;
  flash: string;
  /** Typed answers and picked options, kept across a redraw. */
  drafts: Map<string, string>;
  picks: Map<string, number>;
  /** Proposals whose Withdraw was tapped once (asked before it is sent). */
  withdrawing: ReadonlySet<string>;
  online: boolean;
  /** AS: items whose command the owner opened (true) or closed (false). */
  opened?: ReadonlyMap<string, boolean>;
  /** What the open confirmation shows, frozen when it opened (review of #330, LOW 4). */
  confirmLines?: ConfirmLine[] | null;
}

export interface ReviewActions {
  toggle: (item: ReviewItem) => void;
  approveAsk: () => void;
  approveConfirm: () => void;
  approveCancel: () => void;
  leaveSelected: () => void;
  retry: (row: InboxRow) => void;
  leaveOne: (row: InboxRow) => void;
  send: (item: ReviewItem, body: Record<string, unknown>) => void;
  withdrawAsk: (row: InboxRow) => void;
  withdrawBack: (row: InboxRow) => void;
  openCard: (row: InboxRow) => void;
  open: (row: InboxRow) => void;
  openInbox: () => void;
  jump: (anchor: string) => void;
  /** AS: open or close an item's command (display only). */
  expand?: (row: InboxRow, open: boolean) => void;
}

function testid<T extends HTMLElement>(node: T, id: string): T {
  node.dataset.testid = id;
  return node;
}

function note(state: ReviewState, id: string, parent: HTMLElement) {
  const words = state.notes.get(id);
  if (!words) return;
  const line = testid(el("p", "m-notice", words), "m-review-note");
  line.setAttribute("role", "status");
  parent.append(line);
}

function deferredItem(item: ReviewItem, state: ReviewState, a: ReviewActions, box: HTMLElement) {
  const d = deferredOf(item.row)!;
  const id = item.row.id;
  if (d.headline) box.append(el("p", "headline", d.headline));
  const busy = state.busy.has(id) || state.groupBusy;
  // AS-12: the item's controls first (its Select box, then Approve and
  // retry and Leave it, far apart), then the record's AI summary and code's
  // WHY ASKED and WHERE, then the tool and the command, collapsed only when
  // there is a summary and one tap away. Display only: the tag binds the
  // card and the digest (MR-6), never the summary.
  if (item.approve || item.leave) {
    const label = testid(el("label", "m-tick"), "m-review-tick-label");
    const box2 = testid(el("input", ""), "m-review-tick");
    box2.type = "checkbox";
    box2.dataset.id = id;
    box2.checked = state.selection.has(id);
    box2.disabled = busy;
    // Only its own tap ticks it (a change on this box), never anything else.
    box2.addEventListener("change", () => a.toggle(item));
    // M13 (phone audit): VoiceOver names which item it selects, not only "Select".
    box2.setAttribute("aria-label", "Select: " + (item.row.title || item.row.id));
    label.append(box2, el("span", "", "Select"));
    box.append(label);
  }
  // Per row: Approve and retry (the passkey first) and Leave it, far apart.
  const row = el("div", "m-review-rowacts");
  const leave = tap("Leave it", () => a.leaveOne(item.row), "quiet m-send", "m-review-leave-one");
  leave.disabled = busy || !state.online;
  row.append(leave);
  if (d.grantable && item.approve) {
    const retry = tap("Approve and retry", () => a.retry(item.row), "quiet m-send", "m-review-retry");
    retry.disabled = busy || !state.online;
    row.append(retry);
  }
  box.append(row);
  if (!item.approve) box.append(testid(el("p", "muted small", `Not approvable here: ${whyWords(item.why, item.row)}.`), "m-review-why"));
  const view = recordSummary(d.summary);
  summaryLines({ summary: view.text, summary_state: view.slot ? "ready" : undefined, reason: d.reason, layer: d.layer,
                 where: d.where }, box, "m-review-as");
  if (d.tool) box.append(el("div", "label", "Tool"), el("p", "", d.tool));
  if (d.count > 1) box.append(el("p", "muted small", `Asked ${d.count} times while it waited.`));
  // Every argument, beside the command too (timeout, run_in_background, a
  // description...): a grant never covers what was not shown (#329, LOW 6).
  // Collapsed behind its toggle when there is a summary (AS, the owner's
  // answer 4), never cut.
  const toggle = a.expand ? (open: boolean) => a.expand!(item.row, open) : undefined;
  const open = !toggle || (state.opened?.get(id) ?? !view.slot);
  rowCommand(d.command, d.args, open, toggle, box,
    { command: "m-review-command", args: "m-review-args", prefix: "m-review-command" });
  if (d.more) box.append(el("p", "muted small", `${d.more} more asks from this chat were denied overnight, too many to list one by one.`));
}

function questionItem(item: ReviewItem, state: ReviewState, a: ReviewActions, box: HTMLElement) {
  const id = item.row.id;
  const q = questionOf(item.row);
  if (q.text) box.append(testid(el("p", "question", q.text), "m-question"));
  if (q.options.length) box.append(el("p", "muted small", "Options: " + q.options.join(" / ")));
  const text = testid(el("textarea", "m-text"), "m-review-answer");
  text.setAttribute("aria-label", "Your answer");
  text.placeholder = "Your answer";
  text.rows = 2;
  text.value = state.drafts.get(id) ?? "";
  text.readOnly = state.busy.has(id);
  const send = tap("Send", () => { const v = text.value.trim(); if (v) a.send(item, { text: v }); }, "quiet m-send", "m-review-send");
  const sync = () => { state.drafts.set(id, text.value); send.disabled = state.busy.has(id) || !text.value.trim() || !state.online; };
  text.addEventListener("input", sync);
  sync();
  box.append(text, send);
}

function decisionItem(item: ReviewItem, state: ReviewState, a: ReviewActions, box: HTMLElement) {
  const d = decisionOf(item.row);
  if (!d || !d.open) return;
  const id = item.row.id;
  if (d.question) box.append(testid(el("p", "question", d.question), "m-question"));
  if (d.why) box.append(el("div", "label", "Why"), el("p", "", d.why));
  const group = testid(el("div", "m-choices"), "m-review-options");
  group.setAttribute("role", "radiogroup");
  const text = testid(el("textarea", "m-text"), "m-review-decision-text");
  // MR-3: the default is marked, never chosen: nothing is picked until the owner picks.
  const picked = () => state.picks.get(id) ?? -1;
  const send = tap("Send", () => {
    const words = text.value.trim();
    if (words) a.send(item, { text: words });
    else if (picked() >= 0) a.send(item, { choice: picked() });
  }, "quiet m-send", "m-review-decision-send");
  const sync = () => { state.drafts.set(id, text.value); send.disabled = state.busy.has(id) || !state.online || (!text.value.trim() && picked() < 0); };
  d.options.forEach((option, i) => {
    const label = el("label", "m-choice");
    const radio = testid(el("input", ""), "m-review-option");
    radio.type = "radio";
    radio.name = "review-" + id;
    radio.value = String(i);
    radio.checked = picked() === i;
    radio.disabled = state.busy.has(id);
    radio.addEventListener("change", () => { if (radio.checked) { state.picks.set(id, i); sync(); } });
    label.append(radio, " ");
    if (i === d.suggested) label.append(testid(el("span", "suggested", "default"), "m-review-default"), " ");
    label.append(el("span", "option-text", option));
    group.append(label);
  });
  text.setAttribute("aria-label", "Or answer in your own words");
  text.placeholder = "Or answer in your own words";
  text.rows = 2;
  text.value = state.drafts.get(id) ?? "";
  text.readOnly = state.busy.has(id);
  text.addEventListener("input", sync);
  sync();
  box.append(group, text, send);
}

function itemView(item: ReviewItem, state: ReviewState, a: ReviewActions): HTMLElement {
  const row = item.row;
  const receipt = state.receipts.get(row.id);
  if (receipt) {
    const line = testid(el("p", "m-review-receipt", receipt.text), "m-review-receipt");
    line.dataset.id = row.id;
    return line;
  }
  const card = testid(el("article", `row rank-${row.rank}`), "m-review-item");
  card.dataset.id = row.id;
  card.dataset.kind = oneLine(row.kind, 40);
  card.dataset.approve = item.approve ? "1" : "0";
  card.append(el("h3", "row-title", row.title || row.id));
  if (row.origin) card.append(el("p", "row-origin", row.origin));
  if (row.asked_at) card.append(el("p", "row-meta", `asked ${clockOf(row.asked_at).slice(0, 5)}`));
  const box = el("div", "row-detail");
  if (deferredOf(row)) deferredItem(item, state, a, box);
  else if (row.kind === "question" || row.kind === "task_question") questionItem(item, state, a, box);
  else if (row.kind === "decision") decisionItem(item, state, a, box);
  else if (row.kind === "proposal") {
    if (briefOf(row)) box.append(el("p", "brief", briefOf(row)));
    if (state.withdrawing.has(row.id)) {
      box.append(el("p", "m-ask", "Withdraw this proposal? The task will not start."));
      const keep = tap("Keep it", () => a.withdrawBack(row), "quiet m-send", "m-review-withdraw-back");
      const go = tap("Withdraw it", () => a.send(item, { withdraw: true }), "quiet m-send", "m-review-withdraw-send");
      keep.disabled = go.disabled = state.busy.has(row.id) || !state.online;
      box.append(keep, go);
    } else {
      const ask = tap("Withdraw", () => a.withdrawAsk(row), "quiet m-send", "m-review-withdraw");
      ask.disabled = state.busy.has(row.id) || !state.online;
      box.append(ask);
    }
  } else if (row.kind === "task") {
    if (briefOf(row)) box.append(el("p", "brief", briefOf(row)));
  }
  card.append(box);
  note(state, row.id, card);
  if (row.thread_id || row.task_id) {
    card.append(tap(row.task_id && !row.thread_id ? "Open the task" : "Open the chat", () => a.open(row), "link", "m-open"));
  }
  return card;
}

function part(report: ReviewReport, which: Section, state: ReviewState, a: ReviewActions): HTMLElement | null {
  const kept = Array.from(state.receipts.values()).map((x) => x.item)
    .filter((i) => i.section === which && !report[which].some((j) => j.row.id === i.row.id));
  const items = [...report[which], ...kept];
  if (!items.length) return null;
  const section = testid(el("section", "m-review-part"), "m-review-" + which);
  section.append(el("h2", "group", sectionTitle(report, which)));
  for (const g of groupItems(report, items)) {
    section.append(testid(el("h3", "m-review-project", g.name), "m-review-project"));
    for (const p of g.places) {
      const place = testid(el("div", "m-review-place"), "m-review-place");
      place.id = p.anchor;
      place.dataset.key = p.place.key;
      const label = placeLabel(p.place);
      const head = el("p", "m-review-placename");
      head.append(el("span", "m-review-kind", label.kind), " ", el("span", "", label.title));
      place.append(head);
      for (const item of p.items) place.append(itemView(item, state, a));
      section.append(place);
    }
  }
  return section;
}

/** The Review tab: the report, its chips and parts, and the sticky footer. */
export function reviewView(state: ReviewState, a: ReviewActions, limited: string | null): HTMLElement {
  const view = testid(el("section", "view m-review"), "m-review");
  const report = state.report;
  if (!report) { view.append(testid(el("p", "empty", "Loading…"), "m-empty")); return view; }
  if (limited) view.append(testid(el("p", "m-limited", limited + ": Approve selected is off; approve one at a time."), "m-limited"));
  view.append(testid(el("p", "m-review-summary", summaryLine(report)), "m-review-summary"));
  if (state.flash) {
    const line = testid(el("p", "m-notice", state.flash), "m-review-flash");
    line.setAttribute("role", "status");
    view.append(line);
  }
  const chips = testid(el("nav", "m-review-chips"), "m-review-chips");
  chips.setAttribute("aria-label", "Jump to a chat");
  for (const c of chipsOf(report)) {
    const chip = tap("", () => a.jump(c.anchor), "quiet m-chip", "m-review-chip");
    chip.dataset.anchor = c.anchor;
    chip.append(el("span", "m-review-kind", c.kind), " ", el("span", "", c.title));
    chips.append(chip);
  }
  if (chips.childNodes.length) view.append(chips);
  if (report.live.length) {
    const live = testid(el("section", "m-review-part"), "m-review-live");
    live.append(el("h2", "group", "Live now"));
    for (const row of report.live) {
      const card = testid(el("article", "row rank-1"), "m-review-live-row");
      card.append(el("h3", "row-title", row.title || row.id));
      card.append(tap("Open the card to answer", () => a.openCard(row), "quiet m-send primary", "m-review-open-card"));
      live.append(card);
    }
    view.append(live);
  }
  for (const which of SECTIONS) {
    const section = part(report, which, state, a);
    if (section) view.append(section);
  }
  if (report.more) view.append(el("p", "muted small center", `And ${report.more} more in the Inbox.`));
  if (report.fyi) {
    const fyi = testid(el("p", "muted small", `For your information: ${report.fyi} more came in overnight. `), "m-review-fyi");
    fyi.append(tap("Open the Inbox", a.openInbox, "link", "m-review-open-inbox"));
    view.append(fyi);
  }
  if (!report.open && !report.live.length && !state.receipts.size) view.append(testid(el("p", "empty", "Nothing waited overnight."), "m-empty"));
  view.append(footer(report, state, a, limited));
  return view;
}

function footer(report: ReviewReport, state: ReviewState, a: ReviewActions, limited: string | null): HTMLElement {
  const foot = testid(el("div", "m-review-footer"), "m-review-footer");
  const n = Array.from(state.selection).filter((id) => allItems(report).some((i) => i.row.id === id)).length;
  const fs = footerState(report, state.selection, { limited });
  if (state.confirming && fs.approve) {
    // Exactly the ticked items, before the one passkey prompt.
    foot.classList.add("confirming");
    foot.append(el("p", "m-ask", `Approve these ${n} once each, for 30 minutes?`));
    const list = testid(el("ol", "plain m-review-confirm"), "m-review-confirm");
    // Frozen when it opened: a summary landing under it changes nothing shown.
    for (const line of state.confirmLines ?? confirmationList(report, state.selection)) {
      const li = testid(el("li", ""), "m-review-confirm-item");
      li.dataset.id = line.id;
      li.append(el("div", "m-review-where", line.where));
      if (line.summary) {
        const ai = testid(el("div", "as-ai as-ready"), "m-review-confirm-summary");
        ai.append(el("span", "as-tag", AI_LABEL), el("span", "as-text", line.summary));
        li.append(ai);
      }
      li.append(el("div", "", "Tool: " + line.tool), el("pre", "code", line.firstLine));
      list.append(li);
    }
    foot.append(list);
    const acts = el("div", "m-review-actions");
    const back = tap("Back", a.approveCancel, "quiet m-btn", "m-review-confirm-back");
    const go = tap("Approve with Face ID", a.approveConfirm, "quiet m-btn allow", "m-review-confirm-go");
    back.disabled = state.groupBusy;
    go.disabled = state.groupBusy || !state.online;
    acts.append(back, go);
    foot.append(acts);
    return foot;
  }
  foot.append(testid(el("p", "m-review-count", `${n} selected`), "m-review-count"));
  if (n && !fs.approve) {
    const why = testid(el("div", "m-review-blockers"), "m-review-blockers");
    for (const line of fs.approveWhy) why.append(el("p", "small", line));
    foot.append(why);
  }
  const acts = el("div", "m-review-actions");
  const leave = tap(`Leave selected (${n})`, a.leaveSelected, "quiet m-btn", "m-review-leave");
  const approve = tap(`Approve selected (${n})`, a.approveAsk, "quiet m-btn allow", "m-review-approve");
  leave.disabled = !fs.leave || state.groupBusy || !state.online;
  approve.disabled = !fs.approve || state.groupBusy || !state.online;
  acts.append(leave, approve);
  foot.append(acts);
  return foot;
}

/** Whether the Review tab shows (MR-12): the review has open items and the next night has not begun. */
export function reviewTabShown(report: ReviewReport | null): boolean {
  return !!report && report.open > 0 && !report.quiet.now;
}
