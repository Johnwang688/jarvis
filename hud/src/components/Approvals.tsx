// The authorization card, with v1's semantics intact
// (tests/face/hud_approval_check.py is the spec):
//
//   - deny is the cheap action (Escape, or the button); authorize takes a
//     deliberate click, and **nothing is keyboard-defaulted** — Enter must do
//     nothing at all on this card;
//   - every string on it is text, never markup: the args are model-written and
//     this is the window that gates approvals;
//   - the buttons come first (AS-12, the owner's request of 2026-10-28),
//     sticky at the top of the card; AUTHORIZE and ALWAYS are armed only
//     ARM_MS after the card appears or its summary changes (AS-13), DENY
//     always;
//   - an **AI summary** (a model's one line, marked as such, AS) and two
//     code lines, WHY ASKED and WHERE, come under the buttons; the whole
//     command and every argument stay **one press away** (collapsed while a
//     summary is pending or there, expanded when there is none) —
//     approving a command is not consent to what it does, so the owner must
//     always be able to read what it does;
//   - PTT and the wake word are inert while a card is up (enforced by the
//     caller, which checks `approvals.length`);
//   - a chime, best-effort: a browser may block audio before the owner has
//     interacted with the window.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ApprovalRequest } from "../types";
import { headlineLine } from "../lib/approval";
import { argText, countdownText, nestedTooDeep, originText, reasonText, TOO_DEEP_WARNING } from "../lib/approvalCard";
import {
  AI_LABEL, ARM_MS, WHERE_LABEL, WHY_LABEL, previewLine, startsCollapsed, summaryView, summaryWords, toggleLabel,
  whereText, type SummaryView,
} from "../lib/approvalSummary";
import { noPointerFocus } from "../lib/talkkey";
import { allowRestore } from "../state/frameguard";
import { BrowserApprovalCard } from "./BrowserApproval";
import { cardLabel, validSid, type SubagentRef } from "../lib/subagents";
import { relayedLine, relayedParts } from "../lib/relays";
import { useRow, useWatch } from "../state/subagents";

export function approvalChime(ctx?: AudioContext | null) {
  try {
    const c = ctx || new AudioContext();
    const now = c.currentTime;
    for (const [offset, frequency] of [[0, 660], [0.12, 880]] as [number, number][]) {
      const osc = c.createOscillator();
      const gain = c.createGain();
      osc.type = "sine";
      osc.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, now + offset);
      gain.gain.exponentialRampToValueAtTime(0.12, now + offset + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.16);
      osc.connect(gain).connect(c.destination);
      osc.start(now + offset);
      osc.stop(now + offset + 0.17);
    }
  } catch {
    /* audio may be blocked until the owner interacts with the window */
  }
}

export function ApprovalCard(props: {
  request: ApprovalRequest;
  onDecide: (reqId: string, allow: boolean, always?: boolean) => void;
  onSubagent?: (ref: SubagentRef, reqId: string) => void;
}) {
  return props.request.hud_only
    ? <BrowserApprovalCard request={props.request} onDecide={props.onDecide} onPresent={approvalChime} />
    : <OrdinaryApprovalCard {...props} />;
}

/**
 * "from sub-agent ‹name›" on a card a native sub-agent's call raised (SV-3):
 * its sid comes with the card, its name from the owner's list route. It
 * decides nothing. The name is the model's: on this authorization surface it
 * is reduced as the daemon reduces a card's label (`cardLabel`, SV-1's
 * `[A-Za-z0-9 _./-]` rule), so it cannot draw ‹ ›, · or a "REQUESTED BY". Like AUTHORIZE, is out of the Tab order and never
 * takes focus from a pointer; a click marks the child's row behind the card,
 * and once the card is answered takes the owner to it (App).
 */
function SubagentLink({ threadId, sid, onOpen }: {
  threadId: string; sid: string; onOpen?: (ref: SubagentRef) => void;
}) {
  useWatch(threadId);
  const row = useRow(threadId, sid);
  return (
    <div className="sa-cardlink">
      <button type="button" className="quiet" data-testid="approval-subagent" data-sid={sid} tabIndex={-1}
              onMouseDown={noPointerFocus} title="Show this sub-agent's row (its pane opens once this card is answered)"
              onClick={() => onOpen?.({ threadId, sid })}>
        from sub-agent ‹{cardLabel(row?.name)}›
      </button>
    </div>
  );
}

/** The AI summary's slot (AS): its space reserved from the first frame, so nothing moves when it lands. */
export function SummarySlot({ view }: { view: SummaryView }) {
  if (!view.slot) return null;
  return (
    <div className={"as-ai as-" + view.state} data-testid="approval-summary" data-state={view.state}>
      <span className="as-tag">{AI_LABEL}</span>
      <span className="as-text" data-testid="approval-summary-text">{summaryWords(view)}</span>
    </div>
  );
}

/** WHY ASKED and WHERE (AS-9, AS-8): code's lines, never a model's. */
export function CodeLines({ why, where }: { why: string; where: string }) {
  return (
    <>
      {why ? (
        <div className="reason as-line" data-testid="approval-why"><span className="k">{WHY_LABEL} </span>{why}</div>
      ) : null}
      {where ? (
        <div className="as-line" data-testid="approval-where"><span className="k">{WHERE_LABEL} </span>{where}</div>
      ) : null}
    </>
  );
}

/**
 * The command and every argument: whole, or collapsed to its first line with
 * a control that opens it (AS-15). The control never takes focus (DENY stays
 * the card's only Tab stop) and decides nothing.
 */
export function CommandBlock({ command, args, expanded, onToggle, testid = "approval" }: {
  command: string | null | undefined; args: Record<string, unknown> | null | undefined;
  expanded: boolean; onToggle: () => void; testid?: string;
}) {
  const entries = Object.entries(args || {});
  const cmd = typeof command === "string" ? command : "";
  if (!cmd && !entries.length) return null;
  const preview = cmd ? previewLine(cmd) : entries.length ? `${entries[0][0]} ${previewLine(argText(entries[0][1]))}` : "";
  return (
    <div className="as-cmd" data-testid={`${testid}-command-block`} data-expanded={expanded ? "true" : "false"}>
      {expanded ? (
        <>
          {cmd ? (
            <div className="arg" data-testid={`${testid}-command`}>
              <span className="k">command </span>
              {cmd}
            </div>
          ) : null}
          {entries.map(([k, v]) => (
            <div className="arg" key={k}>
              <span className="k">{k} </span>
              {argText(v)}
            </div>
          ))}
        </>
      ) : (
        <div className="arg as-preview" data-testid={`${testid}-command-preview`}>
          <span className="k">{cmd ? "command " : ""}</span>
          {preview}
        </div>
      )}
      <button type="button" className="quiet as-toggle" data-testid={`${testid}-command-toggle`} tabIndex={-1}
              onMouseDown={noPointerFocus} aria-expanded={expanded} onClick={onToggle}>
        {toggleLabel(expanded, cmd, entries.length)}
      </button>
    </div>
  );
}

/** AS-13: false for ARM_MS after mount and after `key` changes; then true. */
export function useArmed(key: string): boolean {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    setArmed(false);
    const t = setTimeout(() => setArmed(true), ARM_MS);
    return () => clearTimeout(t);
  }, [key]);
  return armed;
}

function OrdinaryApprovalCard(props: {
  request: ApprovalRequest;
  onDecide: (reqId: string, allow: boolean, always?: boolean) => void;
  onSubagent?: (ref: SubagentRef, reqId: string) => void;
}) {
  const r = props.request;
  const [left, setLeft] = useState(r.timeout_s || 120);
  const [busy, setBusy] = useState(false);
  const chimed = useRef(false);
  const view = summaryView(r);
  // The owner's own toggle wins; until then, collapsed only with a summary pending or there.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const expanded = toggled ?? !startsCollapsed(view);
  const armed = useArmed(`${view.state}:${view.text}`);

  useEffect(() => {
    if (!chimed.current) {
      chimed.current = true;
      approvalChime();
    }
    const t = setInterval(() => setLeft((n) => n - 1), 1000);
    return () => clearInterval(t);
  }, []);

  const decide = (allow: boolean, always = false) => {
    if (allow && !armed) return;
    setBusy(true);
    props.onDecide(r.req_id, allow, always);
  };

  const shown = countdownText(left);
  const also = r.remote ? " · ALSO ASKED ON DISCORD" : "";
  // Chat-to-chat C6: which chat's message started the asking call's turn.
  const relay = relayedParts(r.relayed_from);
  // An argument cut at the redaction's depth was never judged or shown:
  // DENY only (review of PR #313, a desk behaviour change that fails closed).
  const tooDeep = nestedTooDeep(r.args);

  return (
    // Focusable (tabIndex -1) so the veil can take focus off a frame or an
    // editor behind it — but no button on it is ever focused for the owner.
    <div className="auth" data-testid="approval-card" data-req={r.req_id} tabIndex={-1}
         role="alertdialog" aria-modal="true" aria-label="Authorization required">
      <h3>AUTHORIZATION REQUIRED</h3>
      {headlineLine(r.headline) ? (
        <div className="headline" data-testid="approval-headline">{headlineLine(r.headline)}</div>
      ) : null}
      <div className="btns" data-testid="approval-buttons">
        {/* type="button" on every one: nothing on this card is a form default,
            so Enter submits nothing. First on the card and sticky (AS-12). */}
        <button type="button" className="deny" disabled={busy} onClick={() => decide(false)} data-testid="approval-deny">
          DENY
        </button>
        {/* AUTHORIZE and ALWAYS are out of the Tab order (tabIndex -1): they
            take a deliberate click, so no Tab or Shift+Tab ever puts one under
            an Enter (re-review of PR #27). DENY, the cheap action, is the only
            stop for Tab. Nor does a pointer ever focus them (`noPointerFocus`): a
            press that slid off before its release used to leave focus on the
            button, and the next Space or Enter authorized (review of PR #40 —
            Space was push-to-talk until then, the key the owner's thumb is
            trained to hold). The click itself still works, once armed (AS-13). */}
        {r.allowlistable === false ? null : (
          <button
            type="button"
            className="always"
            disabled={busy || tooDeep || !armed}
            tabIndex={-1}
            onMouseDown={noPointerFocus}
            data-testid="approval-always"
            title="authorize AND allowlist this, so it stops asking (persists across restarts)"
            onClick={() => decide(true, true)}
          >
            ALWAYS
          </button>
        )}
        <button type="button" disabled={busy || tooDeep || !armed} tabIndex={-1} onMouseDown={noPointerFocus}
                onClick={() => decide(true)} data-testid="approval-allow">
          AUTHORIZE
        </button>
      </div>
      {/* AS: the model's line, marked as the model's; then code's two lines. */}
      <SummarySlot view={view} />
      <CodeLines why={reasonText(r.reason, r.layer)} where={whereText(r.where)} />
      {/* The card's words are lib/approvalCard.ts's, shared with the phone's
          answering card (mobile/card.ts, P-4). */}
      {originText(r.origin) ? (
        <div className="origin" data-testid="approval-origin">{originText(r.origin)}</div>
      ) : null}
      {/* Chat-to-chat C6: the relayed turn behind this call (or running). Display
          only, one clipped line of plain text; it changes no button here. */}
      {relay ? (
        <div className="relayed" data-testid="approval-relayed" title={relayedLine(r.relayed_from)}>
          {relay.started ? "" : "while a "}turn started by a message from chat ‹<bdi>{relay.title}</bdi>› ({relay.who})
          {relay.started ? "" : " runs"}
        </div>
      ) : null}
      {validSid(r.subagent) && r.thread_id ? (
        <SubagentLink threadId={r.thread_id} sid={r.subagent}
                      onOpen={(ref) => props.onSubagent?.(ref, r.req_id)} />
      ) : null}
      <div className="tool" data-testid="approval-tool">{r.tool}</div>
      {/* The entire command and every argument, one press away (AS-15). */}
      <CommandBlock command={r.command} args={r.args} expanded={expanded} onToggle={() => setToggled(!expanded)} />
      {tooDeep ? <div className="reason" role="alert" data-testid="approval-too-deep">{TOO_DEEP_WARNING}</div> : null}
      <div className="foot">
        DENIES AUTOMATICALLY IN {shown} · ESC TO DENY{also}
      </div>
    </div>
  );
}

/**
 * Where keys were going when a card came up, if that is anywhere but the card:
 * a frame (a preview iframe gets its own key events, so Escape never reached
 * the card, and keys went on into the framed page), Monaco (it kept typing
 * into the buffer behind the card), or **any** focused control behind the
 * veil — a sidebar button, a pane's tab, a chip — which the veil stops the
 * pointer from reaching but Enter or Space still pressed (review of PR #26).
 * Only the page itself (`body`, nothing focused) is not behind the card.
 */
export function behindTheCard(el: Element | null, card: Element | null = null): boolean {
  if (!el || el === document.body || el === document.documentElement) return false;
  return !(card && card.contains(el));
}

/** The veil holds the newest card; the rest are listed in the right pane. */
export function ApprovalVeil(props: {
  requests: ApprovalRequest[];
  onDecide: (reqId: string, allow: boolean, always?: boolean) => void;
  /** A card's "from sub-agent ‹name›" (SV-3), with that card's request id. */
  onSubagent?: (ref: SubagentRef, reqId: string) => void;
}) {
  const top = props.requests[0];
  const up = !!top;
  // A card coming up takes focus off whatever had it — a frame, Monaco, a
  // sidebar button — onto the card itself (never a button: nothing here is
  // keyboard-defaulted), and **everything outside the veil goes inert**: the
  // title bar, the shell with its panes and panel, the orb, any picker. The
  // veil stops the pointer; inert stops Tab and Shift+Tab, which used to walk
  // focus back onto a Stop behind the card for Enter to press (review of
  // PR #27). On the way out inert comes off first, then focus goes back to
  // where it was (WP-A's restore). A layout effect, so nothing behind the
  // card can take a key between the card drawing and this.
  const restore = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!up) return;
    const veil = document.getElementById("authveil");
    const card = veil?.querySelector<HTMLElement>('[data-testid="approval-card"]') ?? null;
    const was = document.activeElement as HTMLElement | null;
    if (behindTheCard(was, card)) restore.current = was;
    card?.focus({ preventScroll: true });
    const made: Element[] = [];
    const hold = (el: Element) => {
      if (el === veil || el.hasAttribute("inert")) return;
      el.setAttribute("inert", "");
      made.push(el);
    };
    const parent = veil?.parentElement ?? null;
    for (const el of Array.from(parent?.children ?? [])) hold(el);
    // And whatever appears beside the card while it is up: the Reload prompt
    // a render error draws in place of the window (components/Boundary.tsx)
    // came after this pass and stayed reachable behind the card (review of
    // PR #32). Its records arrive as a microtask, before the next event.
    const watch = new MutationObserver((records) => {
      for (const r of records) {
        for (const n of Array.from(r.addedNodes)) if (n instanceof Element) hold(n);
      }
    });
    if (parent) watch.observe(parent, { childList: true });
    return () => {
      watch.disconnect();
      for (const el of made) el.removeAttribute("inert");
      const back = restore.current;
      restore.current = null;
      if (back && back.isConnected) {
        // Giving the keyboard back to a Browser page the owner had it in is
        // the HUD's act, not the page's (state/frameguard.ts): said first.
        allowRestore(back);
        back.focus({ preventScroll: true });
      }
    };
  }, [up]);
  // The next queued card is a new card (it is keyed by its request): it takes
  // focus as the first one did, so Escape and the Tab cycle reach it — the
  // answered card's button that had focus is gone (re-review of PR #27).
  // After the effect above, so the first card's restore target is recorded
  // before anything here moves focus.
  const topId = top?.req_id ?? null;
  useLayoutEffect(() => {
    if (!topId) return;
    const card = document.querySelector<HTMLElement>('#authveil [data-testid="approval-card"]');
    if (card && !card.contains(document.activeElement)) card.focus({ preventScroll: true });
  }, [topId]);
  useEffect(() => {
    if (!top) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        props.onDecide(top.req_id, false);
      }
      // Tab and Shift+Tab go round the card's own tabbable buttons and never
      // leave it (everything else is inert; this keeps focus off the
      // browser's own UI too). AUTHORIZE and ALWAYS are tabIndex -1, so they
      // are never in the round: DENY is (re-review of PR #27). The card
      // itself is not in the order: nothing is focused for the owner, a key
      // only moves focus.
      if (e.key === "Tab") {
        e.preventDefault();
        const card = document.querySelector<HTMLElement>('[data-testid="approval-card"]');
        const items = Array.from(card?.querySelectorAll<HTMLButtonElement>("button") ?? [])
          .filter((b) => !b.disabled && b.tabIndex >= 0);
        if (!items.length) {
          card?.focus({ preventScroll: true });
          return;
        }
        const at = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = e.shiftKey
          ? (at <= 0 ? items.length - 1 : at - 1)
          : (at < 0 || at === items.length - 1 ? 0 : at + 1);
        items[next].focus({ preventScroll: true });
      }
      // Enter is deliberately not handled: authorize takes a click.
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [top, props]);

  if (!top) return null;
  return (
    <div id="authveil" data-testid="authveil">
      {/* Keyed by its request: the next queued card is a new card, with its
          own countdown and live buttons — not the answered card's busy state
          (re-review of PR #27; main had the same bug). */}
      <ApprovalCard key={top.req_id} request={top} onDecide={props.onDecide} onSubagent={props.onSubagent} />
    </div>
  );
}

export function ApprovalQueue(props: {
  requests: ApprovalRequest[];
}) {
  if (props.requests.length < 2) return null;
  return (
    <div className="block" data-testid="approval-queue">
      <h3>Approvals queue ({props.requests.length})</h3>
      {props.requests.map((r) => (
        <div className="queue-row" key={r.req_id}>
          {r.hud_only ? `Browser action · ${r.code}` : <>{r.origin ? `${r.origin} · ` : ""}{r.tool}{
            summaryView(r).state === "ready" ? ` · AI: ${summaryView(r).text}` : r.command ? ` · ${r.command}` : ""}</>}
        </div>
      ))}
    </div>
  );
}
