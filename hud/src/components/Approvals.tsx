// The authorization card, with v1's semantics intact
// (tests/face/hud_approval_check.py is the spec):
//
//   - deny is the cheap action (Escape, or the button); authorize takes a
//     deliberate click, and **nothing is keyboard-defaulted** — Enter must do
//     nothing at all on this card;
//   - every string on it is text, never markup: the args are model-written and
//     this is the window that gates approvals;
//   - the **whole command** is shown, never a summary (§6.1) — approving a
//     command is not consent to what it does, so the owner has to be able to
//     read what it does;
//   - PTT and the wake word are inert while a card is up (enforced by the
//     caller, which checks `approvals.length`);
//   - a chime, best-effort: a browser may block audio before the owner has
//     interacted with the window.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ApprovalRequest } from "../types";
import { headlineLine } from "../lib/approval";

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
}) {
  const r = props.request;
  const [left, setLeft] = useState(r.timeout_s || 120);
  const [busy, setBusy] = useState(false);
  const chimed = useRef(false);

  useEffect(() => {
    if (!chimed.current) {
      chimed.current = true;
      approvalChime();
    }
    const t = setInterval(() => setLeft((n) => n - 1), 1000);
    return () => clearInterval(t);
  }, []);

  const decide = (allow: boolean, always = false) => {
    setBusy(true);
    props.onDecide(r.req_id, allow, always);
  };

  const shown = left > 120 ? `${Math.ceil(left / 60)}m` : `${Math.max(0, left)}s`;
  const also = r.remote ? " · ALSO ASKED ON DISCORD" : "";
  // `args` is a dict of model-written strings; each is rendered as text.
  const args = Object.entries(r.args || {});

  return (
    // Focusable (tabIndex -1) so the veil can take focus off a frame or an
    // editor behind it — but no button on it is ever focused for the owner.
    <div className="auth" data-testid="approval-card" data-req={r.req_id} tabIndex={-1}
         role="alertdialog" aria-modal="true" aria-label="Authorization required">
      <h3>AUTHORIZATION REQUIRED</h3>
      {headlineLine(r.headline) ? (
        <div className="headline" data-testid="approval-headline">{headlineLine(r.headline)}</div>
      ) : null}
      {r.origin ? (
        <div className="origin" data-testid="approval-origin">REQUESTED BY {r.origin}</div>
      ) : null}
      <div className="tool" data-testid="approval-tool">{r.tool}</div>
      {/* The entire command, never a summary. */}
      {r.command ? (
        <div className="arg" data-testid="approval-command">
          <span className="k">command </span>
          {r.command}
        </div>
      ) : null}
      {args.map(([k, v]) => (
        <div className="arg" key={k}>
          <span className="k">{k} </span>
          {typeof v === "string" ? v : JSON.stringify(v)}
        </div>
      ))}
      {r.reason ? <div className="reason">{r.layer ? `[${r.layer}] ` : ""}{r.reason}</div> : null}
      <div className="btns">
        {/* type="button" on every one: nothing on this card is a form default,
            so Enter submits nothing. */}
        <button type="button" className="deny" disabled={busy} onClick={() => decide(false)} data-testid="approval-deny">
          DENY
        </button>
        {/* AUTHORIZE and ALWAYS are out of the Tab order (tabIndex -1): they
            take a deliberate click, so no Tab or Shift+Tab ever puts one under
            an Enter (re-review of PR #27). DENY, the cheap action, is the only
            stop for Tab. */}
        {r.allowlistable === false ? null : (
          <button
            type="button"
            className="always"
            disabled={busy}
            tabIndex={-1}
            data-testid="approval-always"
            title="authorize AND allowlist this, so it stops asking (persists across restarts)"
            onClick={() => decide(true, true)}
          >
            ALWAYS
          </button>
        )}
        <button type="button" disabled={busy} tabIndex={-1} onClick={() => decide(true)} data-testid="approval-allow">
          AUTHORIZE
        </button>
      </div>
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
    for (const el of Array.from(veil?.parentElement?.children ?? [])) {
      if (el === veil || el.hasAttribute("inert")) continue;
      el.setAttribute("inert", "");
      made.push(el);
    }
    return () => {
      for (const el of made) el.removeAttribute("inert");
      const back = restore.current;
      restore.current = null;
      if (back && back.isConnected) back.focus({ preventScroll: true });
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
      <ApprovalCard key={top.req_id} request={top} onDecide={props.onDecide} />
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
          {r.origin ? `${r.origin} · ` : ""}
          {r.tool}
          {r.command ? ` · ${r.command}` : ""}
        </div>
      ))}
    </div>
  );
}
