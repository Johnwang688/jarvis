// The owner inbox's view (inbox A2; design docs/plans/2026-10-24-owner-inbox-
// design.md §7 and §8): one ranked list of what waits on the owner, under the
// four thin headers ("Waiting now" for ranks 1–2, "Waiting on you" 3–5, "When
// you have time" 6–7, "For your information" 8), empty groups hidden.
//
// What each row offers, and nothing more:
//   - an approval: **its card is the existing one, and only that one.** The
//     card is `ApprovalVeil`'s (components/Approvals.tsx), unchanged: whenever
//     the inbox holds an approval, the window holds its card, the veil is up
//     and this whole view is inert behind it, as the HUD is under every card.
//     The row is never a second mounted card: a second instance would chime
//     again, count down from its own mount, fetch a HUD-only card's private
//     detail outside the veil ("only the mounted top card calls this hook",
//     BrowserApproval.tsx), and put a second `approval-card` where the veil's
//     Tab cycle looks for one. So the row says where its card is (on screen,
//     or queued behind it) and when it denies, from code-built fields only;
//   - a question (a chat's provider question, a `clarifying` task's blocking
//     question): its text, its options as text, and an answer box;
//   - a proposal: its brief and Withdraw (`{"withdraw": true}`) until its
//     `withdraw_until`; the button goes when the window closes;
//   - a blocked or failed task: information only.
// Every row links to its thread or task pane. **Clicking a row never answers
// anything**: only Answer (or Enter in its box) and Withdraw post, each once
// per press, through `POST /inbox/<id>/answer` (api.answerInbox). A 409 is
// "already answered or gone", and the inbox is read again.
//
// Every string here is untrusted text (lib/inbox): React text only, never
// HTML or Markdown. Under a card everything here is inert (the veil), every
// button is disabled too, and the handlers refuse.

import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import type { ApprovalRequest } from "../types";
import {
  ANSWERS_TEXT, PENDING_MAX_RANK, approvalReq, briefOf, clockOf, groupRows, linkOf, questionOf,
  waitingCount, withdrawOpen, withdrawUntil, type InboxRow,
} from "../lib/inbox";
import type { InboxSnapshot } from "../state/inbox";

export const GONE_NOTICE = "Already answered or gone.";

export interface InboxViewProps {
  snapshot: InboxSnapshot;
  /** The window's cards, newest-shown first: `[0]` is on the veil. */
  approvals: readonly Pick<ApprovalRequest, "req_id">[];
  /** An approval card is up: everything here is inert and every control off. */
  blocked: boolean;
  onOpenThread: (threadId: string) => void;
  onOpenTask: (taskId: string) => void;
  onAnswer: (rowId: string, body: { text: string } | { withdraw: true }) => Promise<unknown>;
  onReread: () => Promise<unknown> | void;
  /** The clock (tests pass their own). */
  now?: () => number;
}

function tone(row: InboxRow): "pending" | "failed" | "plain" {
  if (row.rank <= PENDING_MAX_RANK) return "pending";
  if (row.kind === "task" && row.detail.state === "failed") return "failed";
  return "plain";
}

export function InboxView(props: InboxViewProps) {
  const clock = props.now ?? Date.now;
  const [now, setNow] = useState(clock);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, true>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const sending = useRef(new Set<string>());
  const blockedNow = useRef(props.blocked);
  blockedNow.current = props.blocked;
  const { rows, status } = props.snapshot;

  // Read again when the view is shown: the rows are the daemon's, never kept here.
  useEffect(() => { void props.onReread(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  // Withdraw goes when its window closes: wake up then (and no sooner).
  useEffect(() => {
    const t = clock();
    const next = rows.map(withdrawUntil).filter((u): u is number => u !== null && u > t).sort((a, b) => a - b)[0];
    if (next === undefined) return;
    const timer = setTimeout(() => setNow(clock()), Math.min(2 ** 31 - 1, next - t + 50));
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, now]);
  useEffect(() => { setNow(clock()); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [rows]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(timer);
  }, [notice]);

  const drop = <V,>(all: Record<string, V>, key: string) => {
    if (!(key in all)) return all;
    const { [key]: _gone, ...rest } = all;
    return rest;
  };

  const send = async (row: InboxRow, body: { text: string } | { withdraw: true }) => {
    if (blockedNow.current || sending.current.has(row.id)) return;
    sending.current.add(row.id);
    setBusy((all) => ({ ...all, [row.id]: true }));
    setErrors((all) => drop(all, row.id));
    try {
      await props.onAnswer(row.id, body);
      setDrafts((all) => drop(all, row.id));
      setNotice(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setNotice(GONE_NOTICE);
      } else {
        const why = error instanceof Error && error.message ? error.message : "the daemon could not be reached";
        setErrors((all) => ({ ...all, [row.id]: `Not sent: ${why}` }));
      }
    } finally {
      sending.current.delete(row.id);
      setBusy((all) => drop(all, row.id));
      // The daemon's projection is the truth: read it again either way.
      void props.onReread();
    }
  };

  const answerText = (row: InboxRow) => {
    const text = (drafts[row.id] || "").trim();
    if (!text) return;
    void send(row, { text });
  };

  const open = (row: InboxRow) => {
    if (blockedNow.current) return;
    const link = linkOf(row);
    if (!link) return;
    if (link.kind === "thread") props.onOpenThread(link.id);
    else props.onOpenTask(link.id);
  };

  const approvalNote = (row: InboxRow): string => {
    const req = approvalReq(row);
    const at = req ? props.approvals.findIndex((r) => r.req_id === req) : -1;
    if (at === 0) return "On the card now: answer it there.";
    if (at > 0) return "Queued: its card comes up after the one on screen.";
    return "Its card is not up in this window: reload to see it.";
  };

  const body = (row: InboxRow) => {
    const off = props.blocked || !!busy[row.id];
    if (row.kind === "approval") {
      const denies = clockOf(row.expires_at);
      return (
        <div className="ir-body">
          <div className="ir-cardnote" data-testid="inbox-card-note">{approvalNote(row)}</div>
          {denies ? <div className="ir-when muted" data-testid="inbox-denies">Denies automatically at {denies}</div> : null}
        </div>
      );
    }
    if (ANSWERS_TEXT.has(row.kind)) {
      const q = questionOf(row);
      return (
        <div className="ir-body">
          {q.text ? <div className="ir-text" data-testid="inbox-question">{q.text}</div> : null}
          {q.options.length ? (
            <div className="ir-options muted" data-testid="inbox-options">Options: {q.options.join(" / ")}</div>
          ) : null}
          <div className="ir-answer">
            <input
              data-testid="inbox-answer"
              aria-label="Your answer"
              placeholder="Your answer"
              value={drafts[row.id] || ""}
              // Not `disabled` under a card (inert covers it): a disabled box
              // drops focus before the card can record it (hud-workspace notes, WP-B).
              onChange={(e) => {
                const value = e.target.value;
                setDrafts((all) => ({ ...all, [row.id]: value }));
              }}
              onKeyDown={(e) => {
                // The window's keys are not for this box; Escape still reaches the card.
                if (e.key !== "Escape") e.stopPropagation();
                if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  answerText(row);
                }
              }}
            />
            <button type="button" data-testid="inbox-answer-send" disabled={off}
                    onClick={() => answerText(row)}>
              Answer
            </button>
          </div>
        </div>
      );
    }
    if (row.kind === "proposal") {
      const until = withdrawUntil(row);
      const openNow = withdrawOpen(row, now);
      return (
        <div className="ir-body">
          {briefOf(row) ? <div className="ir-text" data-testid="inbox-brief">{briefOf(row)}</div> : null}
          {openNow && until !== null ? (
            <div className="ir-answer">
              <span className="ir-when muted">Starts on its own at {clockOf(until)}</span>
              <button type="button" data-testid="inbox-withdraw" disabled={off}
                      onClick={() => void send(row, { withdraw: true })}>
                Withdraw
              </button>
            </div>
          ) : (
            <div className="ir-when muted" data-testid="inbox-withdraw-closed">Its window to withdraw has closed.</div>
          )}
        </div>
      );
    }
    // A blocked or failed task: information only.
    const brief = briefOf(row);
    return brief ? <div className="ir-body"><div className="ir-text" data-testid="inbox-brief">{brief}</div></div> : null;
  };

  const groups = groupRows(rows);
  const count = waitingCount(rows);

  return (
    <div className="scroll inbox" data-testid="inbox-view" data-status={status} data-reads={props.snapshot.reads}>
      <div className="inbox-head">
        <h2 className="bar">Inbox</h2>
        <span className="muted small" data-testid="inbox-summary">
          {status === "ready" ? (count ? `${count} waiting on you` : "Nothing waiting on you") : ""}
        </span>
      </div>
      {notice ? <div className="inbox-notice" role="status" data-testid="inbox-notice">{notice}</div> : null}
      {status === "loading" && !rows.length ? <div className="pad muted" data-testid="inbox-loading">Reading the inbox…</div> : null}
      {status === "unavailable" ? (
        <div className="pad muted" data-testid="inbox-unavailable">The inbox is not available here: {props.snapshot.message}</div>
      ) : null}
      {status === "error" ? (
        <div className="pad err" data-testid="inbox-error">Could not read the inbox: {props.snapshot.message}</div>
      ) : null}
      {status === "ready" && !rows.length ? (
        <div className="pad muted" data-testid="inbox-empty">Nothing is waiting on you.</div>
      ) : null}
      {groups.map(({ group, rows: mine }) => (
        <section className="inbox-group" key={group.key} data-testid={`inbox-group-${group.key}`}>
          <h3 className="inbox-gh" data-testid="inbox-group-head">{group.label}</h3>
          {mine.map((row) => {
            const link = linkOf(row);
            const asked = clockOf(row.asked_at);
            return (
              // No handler on the row itself: a click on it answers nothing.
              <div className="inbox-row" key={row.id} data-testid="inbox-row" data-id={row.id} data-kind={row.kind}
                   data-rank={row.rank} data-tone={tone(row)}>
                <div className="ir-head">
                  <span className="ir-title" data-testid="inbox-title">{row.title}</span>
                  {asked ? <span className="ir-time muted">{asked}</span> : null}
                </div>
                {row.blocks ? <div className="ir-blocks muted" data-testid="inbox-blocks">{row.blocks}</div> : null}
                {row.origin ? <div className="ir-origin" data-testid="inbox-origin">{row.origin}</div> : null}
                {body(row)}
                {errors[row.id] ? <div className="err small" role="alert" data-testid="inbox-row-error">{errors[row.id]}</div> : null}
                {link ? (
                  <div className="ir-foot">
                    <button type="button" className="quiet ir-link" data-testid="inbox-link" disabled={props.blocked}
                            onClick={() => open(row)}>
                      {link.kind === "thread" ? "Open its chat" : "Open its task"}
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </section>
      ))}
    </div>
  );
}
