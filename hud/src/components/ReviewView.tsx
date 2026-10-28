// The morning review at the desk (MR-10; docs/plans/2026-10-28-morning-review.md):
// one screen of what waited overnight, read top to bottom once, grouped by
// project, then chat or task (lib/review.ts `groupItems`), with a summary
// line and one jump chip per chat (no group folds: a folded group could hide
// a ticked item; the chips replace scrolling).
//
//   - A live approval (rank 1) sits alone at the top as "Live now": its card
//     is the veil's, as in the inbox; never selectable (IN-3 for live cards).
//   - A deferred approval shows its whole QH card (tool, leading line,
//     command, why, arguments, "asked N times", why it cannot be approved if
//     not) and a checkbox, **unticked when the view opens; nothing is ticked
//     but by its own click; there is no "select all"**.
//   - A question, a decision and a proposal are answered one at a time, in
//     place, through the inbox's own routes; once sent the row folds to a
//     one-line receipt where it stood. A decision's default is marked, never
//     chosen; a decision also takes a per-row Dismiss (B4's route). No group
//     answer, no group dismiss (the owner's answer 1).
//   - The sticky footer: "N selected", Leave selected (N) at the left and
//     Approve selected (N) at the right, far apart. Approve is off while any
//     ticked item cannot be approved, and the footer names which; at most 10.
//     It opens a confirmation listing each selected item (chat, tool, the
//     command's first line) with **Cancel focused; Enter and Escape mean
//     Cancel**; only a click on its Approve posts. No passkey at the desk.
//     All or nothing: a 409 unticks what changed, says why, and asks for a
//     new confirmation.
// Under a live approval card the whole view is inert and every control off.
// Every string from the daemon is React text (lib/inbox's untrusted rule).

import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { ApiError } from "../api";
import { clockOf, decisionOf, deferredOf, questionOf, briefOf } from "../lib/inbox";
import {
  afterChanged, allItems, approveBody, chipsOf, confirmationList, emptySelection, footerState, groupItems,
  keptSelection, leaveBody, placeLabel, sectionTitle, summaryLine, toggled, type ReviewItem, type ReviewReport,
  type Section, whyWords, confirmedChanged, UNTICKED_GONE, SECTIONS,
} from "../lib/review";
import type { ReviewSnapshot } from "../state/review";
import { AI_LABEL, WHERE_LABEL, WHY_LABEL, previewLine, recordSummary, toggleLabel } from "../lib/approvalSummary";
import { reasonText } from "../lib/approvalCard";

export interface ReviewViewProps {
  snapshot: ReviewSnapshot;
  /** An approval card is up: everything here is inert and every control off. */
  blocked: boolean;
  onReread: () => Promise<unknown> | void;
  onApprove: (body: { items: { id: string; tag: string }[] }) => Promise<unknown>;
  onLeave: (body: { items: { id: string }[] }) => Promise<unknown>;
  onAnswer: (rowId: string, body: { text: string } | { withdraw: true }) => Promise<unknown>;
  onDecide: (decisionId: string, body: { choice: number } | { text: string }) => Promise<unknown>;
  onDismiss: (rowId: string) => Promise<unknown>;
  onOpenThread: (threadId: string) => void;
  onOpenTask: (taskId: string) => void;
  onOpenInbox: () => void;
}

const message = (error: unknown) => (error instanceof Error && error.message ? error.message : "the daemon could not be reached");

export function ReviewView(props: ReviewViewProps) {
  const { report, status } = props.snapshot;
  // MR-4: the boxes start unticked each time the view opens.
  const [selection, setSelection] = useState<Set<string>>(emptySelection);
  const [notes, setNotes] = useState<Record<string, string>>({});
  // AS: items whose command the owner opened (or closed): row id -> open.
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const [receipts, setReceipts] = useState<Record<string, { item: ReviewItem; text: string }>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [picks, setPicks] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState<Record<string, true>>({});
  const [confirming, setConfirming] = useState(false);
  // The exact set the open confirmation shows: only it is ever sent (review of #329, MED 1).
  const [confirmed, setConfirmed] = useState<{ id: string; tag: string }[] | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const sending = useRef(new Set<string>());
  const blockedNow = useRef(props.blocked);
  blockedNow.current = props.blocked;
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => { void props.onReread(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  // A re-read under the open confirmation that drops or changes a confirmed
  // item closes it, says why on that row, and asks for a new confirmation;
  // any re-read unticks whatever left the report.
  useEffect(() => {
    if (!report) return;
    if (confirming && confirmed) {
      const changed = confirmedChanged(report, confirmed);
      if (changed.length) {
        setConfirming(false);
        setConfirmed(null);
        setSelection((sel) => { const next = new Set(sel); changed.forEach((id) => next.delete(id)); return next; });
        setNotes((all) => ({ ...all, ...Object.fromEntries(changed.map((id) => [id, UNTICKED_GONE])) }));
        setFlash("Something you selected changed: nothing was approved. Look again, then confirm again.");
      }
    }
    setSelection((sel) => (sel.size ? keptSelection(report, sel) : sel));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report]);
  // Under a card the confirmation goes too (the veil's own keys win).
  useEffect(() => { if (props.blocked) { setConfirming(false); setConfirmed(null); } }, [props.blocked]);
  useEffect(() => { if (confirming) cancelRef.current?.focus(); }, [confirming]);

  const run = async (key: string, call: () => Promise<unknown>, done: (result: unknown) => void,
                     failed: (error: unknown) => void) => {
    if (blockedNow.current || sending.current.has(key)) return;
    sending.current.add(key);
    setBusy((all) => ({ ...all, [key]: true }));
    try {
      done(await call());
    } catch (error) {
      failed(error);
    } finally {
      sending.current.delete(key);
      setBusy((all) => { const { [key]: _gone, ...rest } = all; return rest; });
      void props.onReread();
    }
  };

  // One question, decision or proposal, in place; once sent it folds to a receipt where it stood.
  const answerRow = (item: ReviewItem, call: () => Promise<unknown>, receipt: string) =>
    void run(item.row.id, call, (result) => {
      const note = result && typeof result === "object" ? (result as { note?: unknown }).note : undefined;
      setReceipts((all) => ({ ...all, [item.row.id]: { item, text: typeof note === "string" && note ? note.slice(0, 200) : receipt } }));
    }, (error) => {
      setNotes((all) => ({ ...all, [item.row.id]: error instanceof ApiError && error.status === 409
        ? "Already answered or gone." : `Not sent: ${message(error)}` }));
    });

  const approve = () => {
    if (!report || !confirmed) return;
    setConfirming(false);
    setConfirmed(null);
    // Exactly what the dialog listed, and only while it still holds.
    if (confirmedChanged(report, confirmed).length) return;
    const body = { items: confirmed.map((c) => ({ id: c.id, tag: c.tag })) };
    void run("approve", () => props.onApprove(body), (result) => {
      const n = result && typeof result === "object" ? (result as { approved?: unknown }).approved : 0;
      setSelection(emptySelection());
      setFlash(`Approved ${typeof n === "number" ? n : body.items.length} once each, for 30 minutes; their chats were told.`);
    }, (error) => {
      if (error instanceof ApiError && error.status === 409 && error.body && Array.isArray(error.body.changed)) {
        const after = afterChanged(selection, error.body.changed);
        setSelection(after.selection);
        setNotes((all) => ({ ...all, ...Object.fromEntries(after.notes) }));
        setFlash("Nothing was approved: something you selected changed. Look again, then confirm again.");
      } else {
        setFlash(`Nothing was approved: ${message(error)}`);
      }
    });
  };

  const leave = () => {
    if (!report) return;
    const body = leaveBody(report, selection);
    if (!body) return;
    void run("leave", () => props.onLeave(body), (result) => {
      const results = result && typeof result === "object" ? (result as { results?: unknown }).results : null;
      const left = Array.isArray(results) ? results.filter((r) => r && typeof r === "object" && (r as { ok?: unknown }).ok === true).length : 0;
      setSelection(emptySelection());
      setFlash(`Left ${left} of ${body.items.length}: nothing was sent.`);
    }, (error) => setFlash(`Not left: ${message(error)}`));
  };

  const keepKeys = (e: ReactKeyboardEvent) => { if (e.key !== "Escape") e.stopPropagation(); };

  const deferredBody = (item: ReviewItem) => {
    const d = deferredOf(item.row)!;
    const id = item.row.id;
    const ticked = selection.has(id);
    // AS: the item's control first (its Select box); the record's AI summary
    // and code's WHY ASKED and WHERE; the command and arguments collapsed
    // only when there is a summary, one press away. Display only: the tag
    // binds the card and the digest (MR-6), never the summary.
    const view = recordSummary(d.summary);
    const open = opened[id] ?? !view.slot;
    const why = reasonText(d.reason, d.layer);
    const hasBlock = !!(d.command || d.args);
    return (
      <div className="ir-body" data-testid="review-deferred">
        {d.headline ? <div className="ir-text" data-testid="review-headline">{d.headline}</div> : null}
        {item.approve || item.leave ? (
          <label className="review-tick">
            <input type="checkbox" data-testid="review-tick" data-id={id} checked={ticked}
                   disabled={props.blocked || !!busy.approve || !!busy.leave}
                   onChange={() => { if (!blockedNow.current) setSelection((sel) => toggled(sel, id, item)); }}
                   onKeyDown={keepKeys} />
            <span>Select</span>
          </label>
        ) : null}
        {!item.approve ? (
          <div className="ir-when muted" data-testid="review-why">Not approvable here: {whyWords(item.why, item.row)}.</div>
        ) : null}
        {view.slot ? (
          <div className="as-ai as-ready" data-testid="review-summary">
            <span className="as-tag">{AI_LABEL}</span><span className="as-text">{view.text}</span>
          </div>
        ) : null}
        {why ? <div className="as-line" data-testid="review-reason"><span className="k">{WHY_LABEL} </span>{why}</div> : null}
        {d.where ? <div className="as-line" data-testid="review-where"><span className="k">{WHERE_LABEL} </span>{d.where}</div> : null}
        {d.tool ? <div className="ir-text" data-testid="review-tool">Tool: {d.tool}</div> : null}
        {open ? (
          <>
            {d.command ? <pre className="ir-text ir-command" data-testid="review-command">{d.command}</pre> : null}
            {d.args ? <pre className="ir-text ir-args" data-testid="review-args">{d.args}</pre> : null}
          </>
        ) : hasBlock ? (
          <pre className="ir-text ir-command as-preview" data-testid="review-command-preview">
            {d.command ? previewLine(d.command) : previewLine(d.args)}
          </pre>
        ) : null}
        {hasBlock && view.slot ? (
          <button type="button" className="quiet as-toggle" data-testid="review-command-toggle" aria-expanded={open}
                  disabled={props.blocked} onClick={() => setOpened((o) => ({ ...o, [id]: !open }))}>
            {toggleLabel(open, d.command, d.args ? 1 : 0)}
          </button>
        ) : null}
        {d.count > 1 ? <div className="ir-when muted" data-testid="review-count">Asked {d.count} times while it waited.</div> : null}
        {d.more ? <div className="ir-when muted">{d.more} more asks from this chat were denied overnight, too many to list one by one.</div> : null}
      </div>
    );
  };

  const questionBody = (item: ReviewItem) => {
    const q = questionOf(item.row);
    const id = item.row.id;
    const text = drafts[id] || "";
    const send = () => { if (text.trim()) answerRow(item, () => props.onAnswer(id, { text: text.trim() }), "Sent."); };
    return (
      <div className="ir-body">
        {q.text ? <div className="ir-text" data-testid="review-question">{q.text}</div> : null}
        {q.options.length ? <div className="ir-options muted">Options: {q.options.join(" / ")}</div> : null}
        <div className="ir-answer">
          <input data-testid="review-answer" aria-label="Your answer" placeholder="Your answer" value={text}
                 readOnly={!!busy[id]}
                 onChange={(e) => { const v = e.target.value; setDrafts((all) => ({ ...all, [id]: v })); }}
                 onKeyDown={(e) => {
                   keepKeys(e);
                   if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
                 }} />
          <button type="button" data-testid="review-answer-send" disabled={props.blocked || !!busy[id] || !text.trim()}
                  onClick={send}>Send</button>
        </div>
      </div>
    );
  };

  const decisionBodyView = (item: ReviewItem) => {
    const d = decisionOf(item.row);
    if (!d || !d.open) return null;
    const id = item.row.id;
    const typed = drafts[id] || "";
    // MR-3: the default is marked, never chosen: only the owner's own pick or words send.
    const picked = id in picks ? picks[id] : null;
    const send = () => {
      const body = typed.trim() ? { text: typed.trim() } : picked !== null ? { choice: picked } : null;
      if (body) answerRow(item, () => props.onDecide(d.id, body), "Sent.");
    };
    return (
      <div className="ir-body">
        {d.question ? <div className="ir-text" data-testid="review-decision-question">{d.question}</div> : null}
        {d.why ? <div className="ir-why">{d.why}</div> : null}
        <div className="ir-choices" role="radiogroup" aria-label="Options">
          {d.options.map((option, i) => (
            <label className="ir-choice" key={i}>
              <input type="radio" name={`review-${id}`} data-testid="review-decision-option" data-index={i}
                     checked={picked === i}
                     onChange={() => { if (!blockedNow.current) setPicks((all) => ({ ...all, [id]: i })); }}
                     onKeyDown={(e) => { keepKeys(e); if (e.key === "Enter") e.preventDefault(); }} />
              {i === d.suggested ? <span className="ir-suggested" data-testid="review-suggested">default</span> : null}
              <span className="ir-option-text">{option}</span>
            </label>
          ))}
        </div>
        <div className="ir-answer">
          <input data-testid="review-decision-text" aria-label="Or answer in your own words"
                 placeholder="Or answer in your own words" value={typed} readOnly={!!busy[id]}
                 onChange={(e) => { const v = e.target.value; setDrafts((all) => ({ ...all, [id]: v })); }}
                 onKeyDown={keepKeys} />
          <button type="button" data-testid="review-decision-send"
                  disabled={props.blocked || !!busy[id] || (!typed.trim() && picked === null)} onClick={send}>Send</button>
          <button type="button" className="quiet" data-testid="review-dismiss" disabled={props.blocked || !!busy[id]}
                  onClick={() => answerRow(item, () => props.onDismiss(id), "Dismissed.")}>Dismiss</button>
        </div>
      </div>
    );
  };

  const itemView = (item: ReviewItem) => {
    const row = item.row;
    const receipt = receipts[row.id];
    if (receipt) {
      return (
        <div className="inbox-row review-receipt" key={row.id} data-testid="review-receipt" data-id={row.id}>
          <span className="ir-when">{receipt.text}</span>
        </div>
      );
    }
    let body = null;
    if (deferredOf(row)) body = deferredBody(item);
    else if (row.kind === "question" || row.kind === "task_question") body = questionBody(item);
    else if (row.kind === "decision") body = decisionBodyView(item);
    else if (row.kind === "proposal") {
      body = (
        <div className="ir-body">
          {briefOf(row) ? <div className="ir-text">{briefOf(row)}</div> : null}
          <div className="ir-answer">
            <button type="button" data-testid="review-withdraw" disabled={props.blocked || !!busy[row.id]}
                    onClick={() => answerRow(item, () => props.onAnswer(row.id, { withdraw: true }), "Withdrawn.")}>
              Withdraw
            </button>
          </div>
        </div>
      );
    } else if (row.kind === "task") {
      body = (
        <div className="ir-body">
          {briefOf(row) ? <div className="ir-text">{briefOf(row)}</div> : null}
          {row.task_id ? (
            <div className="ir-answer">
              <button type="button" className="quiet" disabled={props.blocked}
                      onClick={() => { if (!blockedNow.current && row.task_id) props.onOpenTask(row.task_id); }}>
                Open the task to resume or cancel it
              </button>
            </div>
          ) : null}
        </div>
      );
    }
    return (
      <div className="inbox-row" key={row.id} data-testid="review-item" data-id={row.id} data-kind={row.kind}
           data-approve={item.approve ? "1" : "0"} data-tone={row.rank <= 2 ? "pending" : "plain"}>
        <div className="ir-head">
          <span className="ir-title">{row.title}</span>
          <span className="ir-time muted">{clockOf(row.asked_at).slice(0, 5)}</span>
        </div>
        {row.origin ? <div className="ir-origin">{row.origin}</div> : null}
        {body}
        {notes[row.id] ? <div className="ir-when" role="status" data-testid="review-note">{notes[row.id]}</div> : null}
      </div>
    );
  };

  const section = (r: ReviewReport, which: Section) => {
    // Answered rows stay where they stood (as receipts) until the view closes.
    const kept = Object.values(receipts).map((x) => x.item)
      .filter((i) => i.section === which && !r[which].some((j) => j.row.id === i.row.id));
    const items = [...r[which], ...kept];
    if (!items.length) return null;
    return (
      <section className="review-part" data-testid={`review-${which}`}>
        <h3 className="inbox-gh">{sectionTitle(r, which)}</h3>
        {groupItems(r, items).map((g) => (
          <div className="review-project" key={g.projectId ?? "none"}>
            <h4 className="review-project-name" data-testid="review-project">{g.name}</h4>
            {g.places.map((p) => {
              const label = placeLabel(p.place);
              return (
                <div className="review-place" id={p.anchor} key={p.anchor} data-testid="review-place" data-key={p.place.key}>
                  <div className="review-place-name">
                    <span className="review-kind">{label.kind}</span> <span>{label.title}</span>
                    {p.place.id && (p.place.kind === "chat" || p.place.kind === "task") ? (
                      <button type="button" className="quiet ir-link" disabled={props.blocked}
                              onClick={() => {
                                if (blockedNow.current || !p.place.id) return;
                                if (p.place.kind === "chat") props.onOpenThread(p.place.id); else props.onOpenTask(p.place.id);
                              }}>open</button>
                    ) : null}
                  </div>
                  {p.items.map(itemView)}
                </div>
              );
            })}
          </div>
        ))}
      </section>
    );
  };

  if (!report) {
    return (
      <div className="inbox review" data-testid="review-view" data-status={status}>
        <div className="inbox-head"><h2 className="bar">Morning review</h2></div>
        <div className="inbox-notice">{status === "loading" ? "Reading…" : status === "unavailable"
          ? "The morning review is not available from this daemon." : `Not read: ${props.snapshot.message}`}</div>
      </div>
    );
  }

  const foot = footerState(report, selection);
  const confirm = confirmed ? confirmationList(report, new Set(confirmed.map((c) => c.id))) : [];
  // While the dialog is open everything behind it in this view is inert (#329, NIT 8).
  const behind = confirming ? { inert: "" } as Record<string, string> : {};
  const picked = allItems(report).filter((i) => selection.has(i.row.id)).length;
  return (
    <div className="inbox review" data-testid="review-view" data-status={status}
         {...(props.blocked ? { inert: "" } as Record<string, string> : {})}>
      <div className="inbox-head" {...behind}><h2 className="bar">Morning review</h2></div>
      <div className="review-scroll" {...behind}>
        <div className="review-summary" data-testid="review-summary">{summaryLine(report)}</div>
        {flash ? <div className="inbox-notice" role="status" data-testid="review-flash">{flash}</div> : null}
        <nav className="review-chips" aria-label="Jump to a chat">
          {chipsOf(report).map((c) => (
            <button type="button" key={c.key} className="review-chip" data-testid="review-chip" data-anchor={c.anchor}
                    disabled={props.blocked}
                    onClick={() => document.getElementById(c.anchor)?.scrollIntoView({ block: "start" })}>
              <span className="review-kind">{c.kind}</span> {c.title}
            </button>
          ))}
        </nav>
        {report.live.length ? (
          <section className="review-part" data-testid="review-live">
            <h3 className="inbox-gh">Live now</h3>
            {report.live.map((row) => (
              <div className="inbox-row" data-tone="pending" key={row.id} data-testid="review-live-row">
                <div className="ir-head"><span className="ir-title">{row.title}</span></div>
                <div className="ir-cardnote">Its card is on screen: answer it there.</div>
              </div>
            ))}
          </section>
        ) : null}
        {SECTIONS.map((which) => <div key={which}>{section(report, which)}</div>)}
        {report.more ? <div className="inbox-notice">And {report.more} more in the Inbox.</div> : null}
        {report.fyi ? (
          <div className="review-fyi" data-testid="review-fyi">
            For your information: {report.fyi} more {report.fyi === 1 ? "row" : "rows"} came in overnight.{" "}
            <button type="button" className="quiet ir-link" disabled={props.blocked} onClick={props.onOpenInbox}>Open the Inbox</button>
          </div>
        ) : null}
        {!report.open && !report.live.length ? <div className="inbox-notice" data-testid="review-empty">Nothing waited overnight.</div> : null}
      </div>
      <div className="review-footer" data-testid="review-footer" {...behind}>
        <button type="button" className="quiet" data-testid="review-leave"
                disabled={props.blocked || !foot.leave || !!busy.leave} onClick={leave}>
          Leave selected ({picked})
        </button>
        <span className="review-count" data-testid="review-selected">{picked} selected</span>
        <button type="button" data-testid="review-approve" disabled={props.blocked || !foot.approve || !!busy.approve}
                onClick={() => {
                  if (blockedNow.current || !foot.approve) return;
                  const body = approveBody(report, selection);
                  if (!body) return;
                  setConfirmed(body.items);
                  setConfirming(true);
                }}>
          Approve selected ({picked})
        </button>
        {picked && !foot.approve ? (
          <div className="review-blockers" data-testid="review-blockers">
            {foot.approveWhy.map((line, i) => <div key={i}>{line}</div>)}
          </div>
        ) : null}
      </div>
      {confirming ? (
        <div className="review-confirm-veil">
          <div className="review-confirm" role="dialog" aria-modal="true" aria-label="Approve these calls"
               data-testid="review-confirm" ref={dialogRef}
               onKeyDownCapture={(e) => {
                 // Enter and Escape mean Cancel, whatever has focus here.
                 if (e.key === "Enter" || e.key === "Escape") {
                   e.preventDefault();
                   e.stopPropagation();
                   setConfirming(false);
                   setConfirmed(null);
                   return;
                 }
                 // A focus trap (#329, NIT 8): Tab cycles inside the dialog only.
                 if (e.key === "Tab") {
                   const stops = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled)") ?? []);
                   if (!stops.length) return;
                   e.preventDefault();
                   const at = stops.indexOf(document.activeElement as HTMLElement);
                   const next = e.shiftKey ? (at <= 0 ? stops.length - 1 : at - 1) : (at + 1) % stops.length;
                   stops[next].focus();
                 }
               }}>
            <div className="review-confirm-head">Approve these {confirm.length} {confirm.length === 1 ? "call" : "calls"} once each, for 30 minutes?</div>
            <ol className="review-confirm-list" data-testid="review-confirm-list">
              {confirm.map((line) => (
                <li key={line.id} data-testid="review-confirm-item" data-id={line.id}>
                  <div className="review-where">{line.where}</div>
                  {line.summary ? (
                    <div className="as-ai as-ready" data-testid="review-confirm-summary">
                      <span className="as-tag">{AI_LABEL}</span><span className="as-text">{line.summary}</span>
                    </div>
                  ) : null}
                  <div>Tool: {line.tool}</div>
                  <pre className="ir-command">{line.firstLine}</pre>
                </li>
              ))}
            </ol>
            <div className="review-confirm-actions">
              <button type="button" ref={cancelRef} data-testid="review-confirm-cancel" autoFocus
                      onClick={() => { setConfirming(false); setConfirmed(null); }}>Cancel</button>
              <button type="button" className="quiet" data-testid="review-confirm-approve" disabled={props.blocked}
                      onClick={approve}>Approve {confirm.length}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
