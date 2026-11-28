import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { countdownText } from "@hud/lib/approvalCard";
import { useArmed } from "@hud/lib/armed";
import { noPointerFocus } from "@hud/lib/talkkey";
import { useUnlock } from "../UnlockContext";
import type { ApprovalRow, Decision } from "./approvalApi";
import { useApprovals, type ShownApproval } from "./useApprovals";
import "./approvals.css";

const visibleText = (text: string) => text.replace(/\p{Bidi_Control}/gu, "");
const invisible = /[\p{Default_Ignorable_Code_Point}\p{Cc}\p{Cf}\u2800\u{1D159}]/gu;
const comparableText = (text: string) => text.replace(/\p{White_Space}+/gu, " ")
  .replace(invisible, "").normalize("NFKC").replace(/\s+/gu, " ").trim();
const labelKey = (label: string) => comparableText(label).toLowerCase();
// CardPass withholds spans and redact_command can redact inside an identifier.
const redactedTarget = /\[(?:withheld:|redacted(?=[:\]]))/i;

// The action titles and target labels in jarvis/v3/approval_card.py.
const actions = new Map([
  ["vault_add", { title: "Add a vault entry", target: "Entry" }],
  ["vault_patch", { title: "Patch a vault entry", target: "Entry" }],
  ["vault_archive", { title: "Archive a vault entry", target: "Entry" }],
  ["calendar_create", { title: "Create a calendar event", target: "Title" }],
  ["calendar_update", { title: "Update a calendar event", target: "Event" }],
  ["calendar_delete", { title: "Delete a calendar event", target: "Event" }],
  ["work_start", { title: "Start a coding thread", target: "Project", required: ["Folder", "Agent", "Runs in"] }],
  ["delegate", { title: "Hand off to Sol", target: "Project", required: ["Folder", "Model"] }],
  ["work_send", { title: "Send to a coding thread", target: "Thread", required: ["Thread id", "In project"] }],
]);
const browserFollow = { title: "Follow a link on the browser page", target: "Link number", required: ["Address", "From"] };
const browserPressTitles = new Set(["Press a browser control", "Submit a browser form", browserFollow.title]);
const browserActions = new Map<string, { title: string; target: string; required?: string[] }>([
  ["browser_open", { title: "Open a browser address", target: "URL" }],
  ["browser_follow", browserFollow],
  ["browser_sign_in", { title: "Sign in to a saved site", target: "Saved login", required: ["Site", "Browser"] }],
]);
const identityLabels = new Set(["entry", "event", "title", "target", "tool", "action", "project", "thread"]);

function safeIdentity(value: string) {
  const text = comparableText(value);
  return !!text && value === visibleText(value) && !redactedTarget.test(text);
}

function preview(text: string) {
  const flat = visibleText(text).replace(/\s+/gu, " ").trim();
  const chars = Array.from(flat);
  return chars.length <= 120 ? flat : chars.slice(0, 119).join("") + "…";
}

function conflictingLabels(rows: ApprovalRow[]) {
  const values = new Map<string, string>();
  for (const { label, value } of rows) {
    // Check both the collapsed preview and the expanded labels.
    for (const key of [labelKey(preview(label)), labelKey(label)]) {
      const previous = values.get(key);
      if (previous !== undefined && previous !== value) return true;
      values.set(key, value);
    }
  }
  return false;
}

function Field({ row, full = false }: { row: ApprovalRow; full?: boolean }) {
  const format = full ? visibleText : preview;
  return (
    <div className="approval-field">
      <dt><bdi>{format(row.label)}</bdi>{row.source === "page" && " (text from the page)"}</dt>
      <dd><bdi>{format(row.value)}</bdi></dd>
    </div>
  );
}

type Press =
  | { kind: "pointer"; id: number; released: boolean }
  | { kind: "key"; key: string };

const activationKey = (key: string) => key === "Enter" || key === " ";

function plainPress(event: KeyboardEvent<HTMLButtonElement>) {
  return !event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey
    && !event.repeat && !event.nativeEvent.isComposing
    && document.activeElement === event.currentTarget;
}

// The parent mounts a fresh button when the displayed approval or list order changes.
function ApproveButton({ disabled, approve, label }: { disabled: boolean; approve: () => void; label: string }) {
  const press = useRef<Press>();
  const submittedClick = useRef(false);
  const cancel = () => { press.current = undefined; };

  useLayoutEffect(() => {
    press.current = undefined;
    const cancel = () => { press.current = undefined; };
    window.addEventListener("blur", cancel);
    return () => window.removeEventListener("blur", cancel);
  }, [disabled]);

  function cancelUnreleasedPointer() {
    if (press.current?.kind === "pointer" && !press.current.released) cancel();
  }

  return (
    <button type="button" className="approval-approve" onMouseDown={noPointerFocus}
      disabled={disabled} tabIndex={disabled ? -1 : undefined}
      onPointerDown={(event) => {
        cancel();
        if (!disabled && event.button === 0 && event.isPrimary !== false) {
          press.current = { kind: "pointer", id: event.pointerId, released: false };
        }
      }}
      onPointerUp={(event) => {
        const started = press.current;
        cancel();
        if (disabled || started?.kind !== "pointer" || started.released
          || started.id !== event.pointerId || event.button !== 0) return;
        const button = event.currentTarget;
        // Touch can retain implicit pointer capture even after the finger slides away.
        if (button.hasPointerCapture?.(event.pointerId)
          && !button.contains(document.elementFromPoint(event.clientX, event.clientY))) return;
        press.current = { ...started, released: true };
      }}
      onPointerCancel={cancel} onPointerLeave={cancelUnreleasedPointer}
      onLostPointerCapture={cancelUnreleasedPointer} onBlur={cancel}
      onClick={(event) => {
        const started = press.current;
        cancel();
        // An ignored press can still count toward the browser's double click.
        if (event.detail === 1) submittedClick.current = false;
        if (disabled || started?.kind !== "pointer" || !started.released
          || event.detail < 1 || submittedClick.current) return;
        const native = event.nativeEvent;
        if ("pointerId" in native && native.pointerId !== started.id) return;
        submittedClick.current = true;
        approve();
      }}
      onKeyDown={(event) => {
        if (!activationKey(event.key)) {
          if (press.current?.kind === "key") cancel();
          return;
        }
        // Native Enter clicks on keydown. Defer both keys until a matching keyup.
        event.preventDefault();
        cancel();
        if (!disabled && plainPress(event)) {
          press.current = { kind: "key", key: event.key };
        }
      }}
      onKeyUp={(event) => {
        if (!activationKey(event.key)) return;
        event.preventDefault();
        const started = press.current;
        cancel();
        if (!disabled && started?.kind === "key" && started.key === event.key
          && plainPress(event)) approve();
      }}>{label}</button>
  );
}

function ApprovalCard({ shown, ready, busy, answer }: {
  shown: ShownApproval;
  ready: boolean;
  busy: boolean;
  answer: (shown: ShownApproval, decision: Decision) => void;
}) {
  const { request, key, deadline } = shown;
  const { locked, busy: unlocking, unlock } = useUnlock();
  const [, tick] = useState(0);
  const armed = useArmed(key);
  const remaining = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
  const expired = remaining === 0;
  const disabled = !ready || busy;
  const { card, tool } = request;
  const rows = card?.rows ?? [];
  const browserAction = tool === "browser_fill"
    ? { title: "Fill a browser field", target: "Site", required: ["Field", "Text", "Then"] }
    : tool === "browser_press" && card && browserPressTitles.has(card.title)
      ? card.title === browserFollow.title
        ? browserFollow
        : { title: card.title, target: "Site", required: ["Button", "Kind"] }
      : browserActions.get(tool);
  // Reading the current page takes no arguments and has no target row.
  const browserRead = tool === "browser_read" && card?.title === "Read the browser page" && rows.length === 0;
  const action = browserAction ?? actions.get(tool);
  const targets = rows.filter((row) => identityLabels.has(labelKey(row.label))
    || (browserAction && labelKey(row.label) === labelKey(browserAction.target)));
  const target = targets.length === 1 && targets[0].label === action?.target ? targets[0] : undefined;
  const policy = [
    { label: "Policy level", value: request.level },
    { label: "Why asked", value: request.why },
  ];
  const safe = (browserRead || (!!target && card?.title === action?.title && safeIdentity(target.value)))
    && (action?.required ?? []).every((label) => {
      const matches = rows.filter((row) => labelKey(row.label) === labelKey(label));
      return matches.length === 1 && matches[0].label === label
        && (label === "Text" && browserAction ? matches[0].value.length > 0
          && matches[0].value === visibleText(matches[0].value) : safeIdentity(matches[0].value));
    })
    && !conflictingLabels([...rows, ...policy]);
  const title = card?.title ?? "";

  useEffect(() => {
    if (expired) return;
    const interval = setInterval(() => tick((n) => n + 1), 1000);
    const expiry = setTimeout(() => tick((n) => n + 1), Math.max(0, Math.ceil(deadline - performance.now())));
    return () => {
      clearInterval(interval);
      clearTimeout(expiry);
    };
  }, [deadline, expired]);

  function decide(decision: Decision, event?: MouseEvent<HTMLButtonElement>) {
    if (disabled || expired || (decision === "approve" && (!armed || !safe)) || (event && event.detail > 1)) return;
    if (locked && decision === "approve") {
      unlock();
      return;
    }
    answer(shown, decision);
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    const focused = document.activeElement;
    if (event.key === "Escape" && event.currentTarget.contains(focused) && event.target === focused) {
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat && !event.nativeEvent.isComposing) decide("deny");
    } else if (event.repeat && (event.key === "Enter" || event.key === " ") && event.target instanceof HTMLButtonElement) {
      event.preventDefault();
    }
  }

  return (
    <section className="approval-card" aria-label="Tool approval" tabIndex={0} onKeyDown={onKeyDown}
      data-expired={expired || undefined}>
      <header className="approval-header">
        {!expired && (
          <div className="approval-actions">
            <button type="button" onMouseDown={noPointerFocus} disabled={disabled}
              onClick={(event) => decide("deny", event)}>Deny</button>
            {safe && <ApproveButton disabled={disabled || !armed || unlocking}
              approve={() => decide("approve")} label={locked ? "Unlock to approve" : "Approve"} />}
          </div>
        )}
        {expired ? <p role="status">Approval expired.</p> : (
          <div className="approval-wait">
            <span>Waiting on you</span>
            <span role="timer">{countdownText(remaining)}</span>
          </div>
        )}
        {!safe && <p className="approval-error" role="status">This request cannot be shown safely</p>}
      </header>
      <h3><bdi>{comparableText(title) ? preview(title) : action?.title ?? "Action unavailable"}</bdi></h3>
      <dl className="approval-target">
        {target && <Field row={target} full />}
        <Field row={{ label: "Tool", value: comparableText(tool) ? tool : "Unavailable" }} />
      </dl>
      <dl className="approval-summary" data-browser={!!browserAction || undefined}>
        {rows.map((row, index) =>
          row === target || row.label === "Revision" ? null : <Field key={index} row={row}
            full={!!browserAction} />)}
      </dl>
      <dl className="approval-policy">
        {policy.map((row) => <Field key={row.label} row={row} />)}
      </dl>
      {card && <details className="approval-details">
        <summary>Details</summary>
        <dl>{rows.map((row, index) => <Field row={row} key={index} full />)}</dl>
      </details>}
    </section>
  );
}

export function ApprovalPanel({ count, stopping, sessionId }: {
  count: number; stopping: boolean; sessionId?: string | null;
}) {
  const { shown, ready, busy, readError, notice, answer } = useApprovals(count, sessionId);
  return (
    <>
      {shown && <ApprovalCard key={shown.key} shown={shown} ready={ready} busy={busy || stopping} answer={answer} />}
      {readError && <p className="approval-error" role="status">Could not refresh approvals. Retrying…</p>}
      {notice === "gone" && <p className="approval-notice" role="status">This request is no longer waiting</p>}
      {notice === "failed" && <p className="approval-error" role="status">The answer was not recorded.</p>}
    </>
  );
}
