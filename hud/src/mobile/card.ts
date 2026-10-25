// The phone's answering card (phone design PH-4, PH-7, §6; P-4): an approval
// opened from the inbox, drawn from the HUD's own projection of it
// (`GET /m/api/inbox/<id>/detail`, jarvis/v2/remote_answer.py) with the
// HUD card's own words (lib/approvalCard.ts), and the card's invariants:
//
//   - the whole card shows before any button does anything: the whole
//     command and every argument, never a summary or a cut;
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
import { argText, browserBody, originText, reasonText } from "../lib/approvalCard";
import { parseBrowserPrivateApproval, type BrowserApprovalPublic } from "../lib/browserapproval";
import { clockOf, oneLine, type InboxRow } from "../lib/inbox";
import { relayedLine, relayedParts } from "../lib/relays";

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
}

export interface CardActions {
  back: () => void;
  deny: () => void;
  allow: () => void;
  always: () => void;
  confirmAlways: () => void;
  cancelAlways: () => void;
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

function ordinaryBody(card: Json, box: HTMLElement) {
  const headline = headlineLine(card.headline);
  if (headline) box.append(testid(el("div", "headline", headline), "approval-headline"));
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
  if (typeof card.command === "string" && card.command) {
    const line = testid(el("div", "arg"), "approval-command");
    line.append(el("span", "k", "command "), card.command);
    box.append(line);
  }
  if (isObj(card.args)) {
    for (const [k, v] of Object.entries(card.args)) {
      const line = testid(el("div", "arg"), "approval-arg");
      line.append(el("span", "k", `${k} `), argText(v));
      box.append(line);
    }
  }
  const reason = reasonText(card.reason, card.layer);
  if (reason) box.append(el("div", "reason", reason));
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
    ordinaryBody(d.card, body);
    shown = true;
  }
  card.append(body);
  if (d && d.why) card.append(testid(el("p", "m-why", d.why), "m-card-why"));
  if (state.notice) {
    const note = testid(el("p", "m-notice", state.notice), "m-card-notice");
    note.setAttribute("role", "status");
    card.append(note);
  }
  // Deny first, alone; AUTHORIZE and ALWAYS far below it.
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
    allow.disabled = state.busy || !allowable || !d!.can.allow;
    authorize.append(allow);
    card.append(el("div", "m-gap"), authorize);
  }
  (denyRow.firstChild as HTMLButtonElement).disabled = state.busy;
  const at = d ? deadline(d.card) : "";
  card.append(el("div", "foot", at ? `DENIES AUTOMATICALLY AT ${at}` : "DENIES AUTOMATICALLY WHEN IT TIMES OUT"));
  view.append(card);
  return view;
}
