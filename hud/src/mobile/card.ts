// The phone's answering card (phone design PH-4, PH-7, §6; P-4): an approval
// opened from the inbox, drawn from the HUD's own projection of it
// (`GET /m/api/inbox/<id>/detail`, jarvis/v2/remote_answer.py) with the
// HUD card's own words (lib/approvalCard.ts), and the card's invariants:
//
//   - the buttons come first (AS-12, the owner's request of 2026-10-28),
//     then the AI summary (a model's one line, marked as such) and code's
//     WHY ASKED and WHERE; the whole command and every argument stay one tap
//     away: collapsed while a summary is pending or there, expanded when
//     there is none (lib/approvalSummary.ts, the HUD card's own words);
//   - every string is set with textContent; a Browser card's page text sits
//     in its own <bdi>;
//   - Deny is cheap (one tap, the session only) and first; AUTHORIZE and
//     ALWAYS are far below it, each takes the passkey (Face ID or the
//     passcode) on its own challenge, and ALWAYS shows the rule it would
//     write before the passkey is asked for;
//   - nothing is keyboard-defaulted: no form, no autofocus, AUTHORIZE and
//     ALWAYS out of the Tab order and never focused by a tap, and Enter or
//     Space on the card does nothing unless Deny itself has focus;
//   - when the daemon says the card was withheld (the phone's scrub would
//     change it), or limited (§4.1), the buttons it may not use are off and
//     the card says why ("answer it at the desk").
//
// Pure DOM building from data, so the tests build it in jsdom.

import { headlineLine } from "../lib/approval";
import { argText, browserBody, nestedTooDeep, originText, reasonText, TOO_DEEP_WARNING } from "../lib/approvalCard";
import { parseBrowserPrivateApproval, type BrowserApprovalPublic } from "../lib/browserapproval";
import { clockOf, oneLine, type InboxRow } from "../lib/inbox";
import { relayedLine, relayedParts } from "../lib/relays";
import {
  AI_LABEL, WHERE_LABEL, WHY_LABEL, previewLine, startsCollapsed, summaryView, summaryWords, toggleLabel, whereFields,
  whereParts,
} from "../lib/approvalSummary";

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => !!v && typeof v === "object" && !Array.isArray(v);

export interface Secure { ok: boolean; failed: string | null; label: string; reason: string }

export interface CardDetail {
  card: Json;
  private: boolean;
  withheld: boolean;
  secure: Secure;
  can: { deny: boolean; allow: boolean; always: boolean };
  why: string;
}

export function parseSecure(raw: unknown): Secure {
  const s = isObj(raw) ? raw : {};
  return {
    ok: s.ok === true,
    failed: typeof s.failed === "string" ? oneLine(s.failed, 40) : null,
    label: oneLine(s.label, 160),
    reason: oneLine(s.reason, 160),
  };
}

/** `GET .../detail`'s body, or null for anything that is not one. */
export function parseDetail(raw: unknown): CardDetail | null {
  if (!isObj(raw) || !isObj(raw.card) || !isObj(raw.can)) return null;
  return {
    card: raw.card,
    private: raw.private === true,
    withheld: raw.withheld === true,
    secure: parseSecure(raw.secure),
    can: { deny: true, allow: raw.can.allow === true, always: raw.can.always === true },
    why: oneLine(raw.why, 200),
  };
}

export interface Rule { words: string; entry: unknown; publicKey: Json }

export interface CardState {
  rowId: string;
  row: InboxRow | null;
  /** null while loading. */
  detail: CardDetail | null;
  /** The daemon refused the detail (a HUD-only card while limited: "answer at the desk"). */
  refused: string | null;
  busy: boolean;
  rule: Rule | null;
  notice: string;
  /** AS: the owner's own open (true) or close (false) of the command; undefined until then. */
  expanded?: boolean;
}

export interface CardActions {
  back: () => void;
  deny: () => void;
  allow: () => void;
  always: () => void;
  confirmAlways: () => void;
  cancelAlways: () => void;
  /** AS: open or close the command (display only). */
  toggleCommand?: (open: boolean) => void;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function testid<T extends HTMLElement>(node: T, id: string): T {
  node.dataset.testid = id;
  return node;
}

function button(label: string, cls: string, id: string, onClick: () => void, deliberate: boolean): HTMLButtonElement {
  const b = testid(el("button", "tap m-btn " + cls, label), id);
  b.type = "button";
  if (deliberate) {
    // Out of the Tab order, and a tap never leaves focus on it: no Enter or
    // Space can ever press it (the HUD card's `noPointerFocus` rule).
    b.tabIndex = -1;
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("pointerdown", (e) => e.preventDefault());
  }
  b.addEventListener("click", () => { if (!b.disabled) onClick(); });
  return b;
}

/** A Browser card's private detail as the HUD's parser accepts it, or null. */
function browserDetail(state: CardState) {
  const d = state.detail;
  const row = state.row?.detail ?? {};
  if (!d || !d.private || !isObj(d.card)) return null;
  // The phone is sent no 4-character code (an answer handle on Discord):
  // both sides are matched without it.
  const expected = {
    req_id: String(row.req_id ?? d.card.req_id ?? ""), tool: String(d.card.tool ?? ""), layer: String(d.card.layer ?? ""),
    hud_only: true, code: "", thread_id: (d.card.thread_id ?? null) as string | null,
    asked_at: String(d.card.asked_at ?? ""), timeout_s: d.card.timeout_s as number,
  } as BrowserApprovalPublic;
  return parseBrowserPrivateApproval({ ...d.card, code: "" }, expected);
}

/** The AI summary's slot and code's two lines (AS), as text. */
export function summaryLines(card: Json, box: HTMLElement, idPrefix = "approval") {
  const view = summaryView(card);
  if (view.slot) {
    const slot = testid(el("div", "as-ai as-" + view.state), `${idPrefix}-summary`);
    slot.dataset.state = view.state;
    slot.append(el("span", "as-tag", AI_LABEL), testid(el("span", "as-text", summaryWords(view)), `${idPrefix}-summary-text`));
    box.append(slot);
  }
  const why = reasonText(card.reason, card.layer);
  if (why) {
    const line = testid(el("div", "reason as-line"), `${idPrefix}-why`);
    line.append(el("span", "k", WHY_LABEL + " "), why);
    box.append(line);
  }
  const fields = whereFields(whereParts(card.where));
  if (fields.length) {
    // Each field in its own labelled box, its value isolated (review of #330, MED 1).
    const line = testid(el("div", "as-line as-where"), `${idPrefix}-where`);
    line.append(el("span", "k", WHERE_LABEL));
    for (const [key, label, value] of fields) {
      const part = testid(el("span", "as-wpart"), `${idPrefix}-where-${key}`);
      part.dataset.field = key;
      part.append(el("span", "as-wk", label), " ", el("bdi", "as-wv", value));
      line.append(" ", part);
    }
    box.append(line);
  }
}

/** The command and every argument, whole or collapsed to its first line, with its toggle (AS-15). */
export function commandBlock(command: unknown, args: unknown, open: boolean, toggle: ((open: boolean) => void) | undefined,
                             box: HTMLElement, idPrefix = "approval") {
  const cmd = typeof command === "string" ? command : "";
  const entries = isObj(args) ? Object.entries(args) : [];
  if (!cmd && !entries.length) return;
  const block = testid(el("div", "as-cmd"), `${idPrefix}-command-block`);
  block.dataset.expanded = open ? "true" : "false";
  if (open) {
    if (cmd) {
      const line = testid(el("div", "arg"), `${idPrefix}-command`);
      line.append(el("span", "k", "command "), cmd);
      block.append(line);
    }
    for (const [k, v] of entries) {
      const line = testid(el("div", "arg"), `${idPrefix}-arg`);
      line.append(el("span", "k", `${k} `), argText(v));
      block.append(line);
    }
  } else {
    const line = testid(el("div", "arg as-preview"), `${idPrefix}-command-preview`);
    line.append(el("span", "k", cmd ? "command " : ""),
      cmd ? previewLine(cmd) : `${entries[0][0]} ${previewLine(argText(entries[0][1]))}`);
    block.append(line);
  }
  if (toggle) {
    const b = button(toggleLabel(open, cmd, entries.length), "quiet as-toggle", `${idPrefix}-command-toggle`,
      () => toggle(!open), true);
    b.className = "tap quiet as-toggle";
    b.setAttribute("aria-expanded", String(open));
    block.append(b);
  }
  box.append(block);
}

/**
 * A row's command and arguments (the inbox's and the review's rows, AS-15):
 * labelled blocks as the rows always drew them, whole when `open`, else the
 * command's (or the arguments') first line; the toggle, when given, below.
 */
export function rowCommand(command: string, argsText: string, open: boolean,
                           toggle: ((open: boolean) => void) | undefined, box: HTMLElement,
                           ids: { command: string; args: string; prefix: string }) {
  if (!command && !argsText) return;
  if (open) {
    if (command) box.append(el("div", "label", "Command"), testid(el("pre", "code", command), ids.command));
    if (argsText) box.append(el("div", "label", "Arguments"), testid(el("pre", "code", argsText), ids.args));
  } else {
    box.append(el("div", "label", command ? "Command" : "Arguments"),
      testid(el("pre", "code as-preview", previewLine(command || argsText)), `${ids.prefix}-preview`));
  }
  if (toggle) {
    const b = button(toggleLabel(open, command, argsText ? 1 : 0), "quiet as-toggle", `${ids.prefix}-toggle`,
      () => toggle(!open), true);
    b.className = "tap quiet as-toggle";
    b.setAttribute("aria-expanded", String(open));
    box.append(b);
  }
}

function ordinaryBody(state: CardState, card: Json, box: HTMLElement, toggle?: (open: boolean) => void) {
  summaryLines(card, box);
  const origin = originText(card.origin);
  if (origin) box.append(testid(el("div", "origin", origin), "approval-origin"));
  const relay = relayedParts(card.relayed_from);
  if (relay) {
    const line = testid(el("div", "relayed"), "approval-relayed");
    line.title = relayedLine(card.relayed_from);
    const bdi = el("bdi", "", relay.title);
    line.append(`${relay.started ? "" : "while a "}turn started by a message from chat ‹`, bdi,
      `› (${relay.who})${relay.started ? "" : " runs"}`);
    box.append(line);
  }
  if (typeof card.subagent === "string" && card.subagent) box.append(el("div", "muted small", "from a sub-agent"));
  box.append(testid(el("div", "tool", typeof card.tool === "string" ? card.tool : ""), "approval-tool"));
  const open = state.expanded ?? !startsCollapsed(summaryView(card));
  commandBlock(card.command, card.args, open, toggle, box);
  if (nestedTooDeep(card.args)) {
    const warn = testid(el("div", "reason m-why", TOO_DEEP_WARNING), "approval-too-deep");
    warn.setAttribute("role", "alert");
    box.append(warn);
  }
}

function privateBody(state: CardState, box: HTMLElement): boolean {
  const parsed = browserDetail(state);
  if (!parsed) {
    box.append(testid(el("p", "", "The Browser action details are unavailable. Deny this request."), "m-private-missing"));
    return false;
  }
  const body = browserBody(parsed);
  const wrap = testid(el("div", "browserapproval"), body.testid);
  wrap.append(el("div", "headline", body.headline));
  for (const p of body.paras) {
    const para = el("p", p.muted ? "muted" : "");
    if (p.testid) para.dataset.testid = p.testid;
    for (const s of p.segs) para.append(s.bdi ? el("bdi", "", s.text) : s.text);
    wrap.append(para);
  }
  if (body.command !== null) {
    const pre = testid(el("pre", "arg"), "approval-command");
    pre.append(el("bdi", "", body.command));
    wrap.append(pre);
  }
  box.append(wrap);
  return true;
}

function deadline(card: Json): string {
  const at = typeof card.asked_at === "string" ? Date.parse(card.asked_at) : NaN;
  const t = typeof card.timeout_s === "number" ? card.timeout_s : NaN;
  return Number.isFinite(at) && Number.isFinite(t) ? clockOf(at + t * 1000) : "";
}

export function cardView(state: CardState, actions: CardActions): HTMLElement {
  const view = testid(el("section", "view"), "m-card-view");
  const bar = el("div", "subbar");
  const back = button("‹ Inbox", "back", "m-back", actions.back, false);
  back.className = "tap back";
  bar.append(back);
  view.append(bar);
  const card = testid(el("div", "auth m-card"), "approval-card");
  card.dataset.req = state.rowId;
  card.setAttribute("role", "alertdialog");
  card.setAttribute("aria-label", "Authorization required");
  card.tabIndex = -1;
  // Nothing on the card is a keyboard default: Enter and Space do nothing
  // unless Deny has focus.
  card.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ")
        && !(e.target instanceof Element && e.target.closest('[data-testid="approval-deny"]'))) e.preventDefault();
  }, true);
  card.append(el("h3", "", "AUTHORIZATION REQUIRED"));
  const d = state.detail;
  const row = state.row;
  if (d && !d.secure.ok) {
    card.append(testid(el("p", "m-limited", `limited: ${d.secure.label || d.secure.failed || "a check failed"}`),
      "m-limited"));
  }
  // AS-12: a card's leading line (a widening, the merge gate's), then the buttons, then the rest.
  if (d && !d.private && !state.refused) {
    const headline = headlineLine(d.card.headline);
    if (headline) card.append(testid(el("div", "headline", headline), "approval-headline"));
  }
  const body = el("div", "m-card-body");
  let shown = false;
  if (state.refused) {
    // A HUD-only card while limited: its private projection only.
    const tool = row && typeof row.detail.tool === "string" ? row.detail.tool : "";
    body.append(testid(el("div", "tool", tool === "hud_browser_act" ? "Browser action" : oneLine(tool, 80) || "A tool"),
      "approval-tool"));
    body.append(testid(el("p", "m-desk", state.refused === "answer at the desk"
      ? "A private card: answer at the desk." : state.refused), "m-card-refused"));
  } else if (!d) {
    body.append(el("p", "muted", "Loading the card…"));
  } else if (d.private) {
    shown = privateBody(state, body);
  } else {
    ordinaryBody(state, d.card, body, actions.toggleCommand);
    shown = true;
  }
  if (d && d.why) card.append(testid(el("p", "m-why", d.why), "m-card-why"));
  if (state.notice) {
    const note = testid(el("p", "m-notice", state.notice), "m-card-notice");
    note.setAttribute("role", "status");
    card.append(note);
  }
  // Deny first, alone; AUTHORIZE and ALWAYS far below it; all of it above the card's body (AS-12).
  const denyRow = el("div", "m-deny-row");
  denyRow.append(button("DENY", "deny", "approval-deny", actions.deny, false));
  card.append(denyRow);
  const allowable = !!d && shown && !state.refused;
  if (state.rule) {
    const rule = testid(el("div", "m-rule"), "m-rule");
    rule.append(el("div", "label", "ALWAYS writes this standing rule:"),
      testid(el("p", "", state.rule.words), "m-rule-words"));
    if (state.rule.entry !== null && state.rule.entry !== undefined) {
      rule.append(testid(el("pre", "code", JSON.stringify(state.rule.entry)), "m-rule-entry"));
    }
    const confirm = button("Confirm ALWAYS with Face ID or passcode", "always", "m-rule-confirm",
      actions.confirmAlways, true);
    const cancel = button("Not this", "quiet", "m-rule-cancel", actions.cancelAlways, false);
    confirm.disabled = state.busy;
    cancel.disabled = state.busy;
    rule.append(confirm, cancel);
    card.append(el("div", "m-gap"), rule);
  } else {
    const authorize = el("div", "m-allow-row");
    // ALWAYS only on a card that takes one (the HUD's rule: not a
    // HUD-only card, not one marked not allowlistable).
    if (d && !d.private && !state.refused && d.card.allowlistable !== false) {
      const always = button("ALWAYS", "always", "approval-always", actions.always, true);
      always.disabled = state.busy || !allowable || !d.can.always;
      authorize.append(always);
    }
    const allow = button("AUTHORIZE", "allow", "approval-allow", actions.allow, true);
    allow.disabled = state.busy || !allowable || !d!.can.allow || (!!d && nestedTooDeep(d.card.args));
    authorize.append(allow);
    card.append(el("div", "m-gap"), authorize);
  }
  (denyRow.firstChild as HTMLButtonElement).disabled = state.busy;
  card.append(el("div", "m-gap-after"), body);
  const at = d ? deadline(d.card) : "";
  card.append(el("div", "foot", at ? `DENIES AUTOMATICALLY AT ${at}` : "DENIES AUTOMATICALLY WHEN IT TIMES OUT"));
  view.append(card);
  return view;
}
