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
//   - a proposal: its brief and Withdraw (`{"withdraw": true}`) for as long
//     as the daemon's projection holds the row. `withdraw_until` is the
//     daemon's clock, so this window's clock only colours a "closing" hint;
//     a press after the window closed is the daemon's 409;
//   - a blocked or failed task: information only;
//   - "denied while you were away" (inbox A3, IN-3): information only, why it
//     was denied in code's words (`missedNote`), and its chat link; no "ask
//     again" (design §10, question 3);
//   - a decision (inbox B1, IN-5): its question and why as text, its options
//     as radio buttons with the agent's suggestion **preselected but never
//     submitted** (only Answer posts), a box for the owner's own words (which
//     win over the picked option when there are any), and Answer, posting
//     `{choice}` or `{text}` once to `POST /inbox/decisions/<id>`. Enter
//     answers only from the focused box; on a radio it does nothing. Once
//     answered for a chat that was closed (rank 8) it shows the answer only.
//   - a live check (inbox B2, IN-6): its title and its steps as a numbered
//     list, each with what to do and what to expect (text only: **nothing
//     here runs anything**; "Copy" puts the step's words on the clipboard and
//     nothing more), Pass, Fail and Skip and a note box per step, each press
//     posting once to `POST /inbox/checks/<id>/steps/<n>`. The result is the
//     owner's alone. While the daemon's deployed test does not confirm the
//     check's PRs are in the running build, its line shows and the marks are
//     off (the daemon refuses them too, 409). Done (rank 8), the steps show
//     their verdicts, and "Copy receipt" puts the Markdown receipt (made here
//     from the row, `checkReceipt`) on the clipboard.
// The head's "Import a runbook" reads a runbook under a project's
// docs/plans/ into unsaved drafts (`POST /inbox/checks/import`); only the
// owner's Save on one draft stores it as a check.
// Marks (inbox B4, IN-7, IN-8): a row of rank 5 to 8 the daemon calls
// `markable` offers Snooze 1 h, Until tomorrow, 1 week and Dismiss, each a
// plain button posting once to `POST /inbox/<id>/mark`; ranks 1 to 4 and "PR
// list unavailable" never do (lib/inbox `canMark`, and the daemon refuses
// them, 409). Hidden rows leave the list and the count; "Show hidden (n)"
// shows them, muted, each saying why in code's words with Undo
// (`{clear: true}`). Nothing here is keyboard-defaulted, and clicking a row
// never marks it.
// Every row links to its thread or task pane (a missed row, its chat only). **Clicking a row never answers
// anything**: only Answer (or Enter in its box) and Withdraw post, each once
// per press, through `POST /inbox/<id>/answer` (api.answerInbox). A 409 is
// "already answered or gone", and the inbox is read again.
//
// Every string here is untrusted text (lib/inbox): React text only, never
// HTML or Markdown. Under a card everything here is inert (the veil), every
// button is disabled too, and the handlers refuse.

import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { ApiError } from "../api";
import type { ApprovalRequest } from "../types";
import {
  ANSWERS_TEXT, CLOSING_MS, NOTE_MAX, PENDING_MAX_RANK, SNOOZES, VERDICTS, approvalReq, briefOf, canMark, checkOf,
  checkReceipt, clockOf, decisionBody, decisionOf, groupRows, hiddenNote, hiddenRows, linkOf, missedNote, parseDrafts,
  questionOf, shownRows, snoozeUntil, waitingCount, withdrawHint, withdrawUntil, type CheckDraft, type InboxRow,
  type MarkBody, type SnoozeFor, type Verdict,
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
  /** A decision's answer (B1): its own id, `{choice}` or `{text}`. */
  onDecide: (decisionId: string, body: { choice: number } | { text: string }) => Promise<unknown>;
  onReread: () => Promise<unknown> | void;
  /** A live check's step (B2): the owner's verdict and note. */
  onMarkCheck: (checkId: string, step: number, body: { verdict: Verdict; note: string }) => Promise<unknown>;
  /** The runbook import (B2): `{path}` reads drafts, `{path, digest, save}` saves one. */
  onImportChecks: (body: { path: string } | { path: string; digest: string; save: number }) => Promise<unknown>;
  /** A snooze, dismissal or Undo of one row (B4, IN-7): the daemon judges the row's rank itself. */
  onMark: (rowId: string, body: MarkBody) => Promise<unknown>;
  /** Puts text on the clipboard (tests pass their own). */
  copyText?: (text: string) => Promise<void>;
  /** The clock (tests pass their own). */
  now?: () => number;
}

const VERDICT_WORDS: Record<Verdict, string> = { pass: "Passed", fail: "Failed", skip: "Skipped" };
const VERDICT_BUTTONS: Record<Verdict, string> = { pass: "Pass", fail: "Fail", skip: "Skip" };

function defaultCopy(text: string): Promise<void> {
  const clip = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
  if (!clip || typeof clip.writeText !== "function") return Promise.reject(new Error("the clipboard is not available here"));
  return clip.writeText(text);
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
  // A decision's picked option, by row: absent means "the suggestion", which
  // is only preselected — nothing posts until Answer is pressed.
  const [picks, setPicks] = useState<Record<string, number | null>>({});
  const [notice, setNotice] = useState<string | null>(null);
  // The runbook import (B2): the path box, and the drafts the daemon read (unsaved).
  const [importOpen, setImportOpen] = useState(false);
  const [importPath, setImportPath] = useState("");
  const [importErr, setImportErr] = useState<string | null>(null);
  const [runbook, setRunbook] = useState<{ path: string; digest: string; drafts: CheckDraft[] } | null>(null);
  const [saved, setSaved] = useState<Record<number, string>>({});
  // B4: the hidden rows are listed only while the owner asks to see them.
  const [showHidden, setShowHidden] = useState(false);
  const sending = useRef(new Set<string>());
  const blockedNow = useRef(props.blocked);
  blockedNow.current = props.blocked;
  const { rows, status } = props.snapshot;

  // "Show hidden" ends when nothing is hidden, so the next row hidden leaves the list as it should.
  const hiddenCount = rows.filter((r) => r.hidden).length;
  useEffect(() => { if (!hiddenCount) setShowHidden(false); }, [hiddenCount]);

  // Read again when the view is shown: the rows are the daemon's, never kept here.
  useEffect(() => { void props.onReread(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  // The "closing" hint turns on `CLOSING_MS` before a window's end by this
  // window's clock: wake up then (and no sooner). Withdraw itself stays while
  // the row does: only the daemon's projection says the window has closed.
  useEffect(() => {
    const t = clock();
    const next = rows.map(withdrawUntil).map((u) => (u === null ? null : u - CLOSING_MS))
      .filter((u): u is number => u !== null && u > t).sort((a, b) => a - b)[0];
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

  const send = async (row: InboxRow, body: { text: string } | { withdraw: true }) =>
    post(row, () => props.onAnswer(row.id, body));

  const post = async (row: InboxRow, call: () => Promise<unknown>, key: string = row.id) => {
    if (blockedNow.current || sending.current.has(key)) return;
    sending.current.add(key);
    setBusy((all) => ({ ...all, [key]: true }));
    setErrors((all) => drop(all, row.id));
    try {
      const result = await call();
      setDrafts((all) => drop(all, key));
      setPicks((all) => drop(all, key));
      // A decision's reply says where the answer went, in the daemon's fixed words.
      const note = result && typeof result === "object" ? (result as { note?: unknown }).note : undefined;
      setNotice(typeof note === "string" && note ? note.slice(0, 200) : null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // A check's 409 is the deployed test's own line; anything else is "gone".
        setNotice(key !== row.id && error.message ? error.message.slice(0, 300) : GONE_NOTICE);
      } else {
        const why = error instanceof Error && error.message ? error.message : "the daemon could not be reached";
        setErrors((all) => ({ ...all, [row.id]: `Not sent: ${why}` }));
      }
    } finally {
      sending.current.delete(key);
      setBusy((all) => drop(all, key));
      // The daemon's projection is the truth: read it again either way.
      void props.onReread();
    }
  };

  const answerText = (row: InboxRow) => {
    const text = (drafts[row.id] || "").trim();
    if (!text) return;
    void send(row, { text });
  };

  const pickedOf = (row: InboxRow, suggested: number | null) => (row.id in picks ? picks[row.id] : suggested);

  const decide = (row: InboxRow) => {
    const d = decisionOf(row);
    if (!d || !d.open) return;
    const body = decisionBody(pickedOf(row, d.suggested), drafts[row.id] || "", d);
    if (!body) return;
    void post(row, () => props.onDecide(d.id, body));
  };

  const copy = (text: string, done: string) => {
    if (blockedNow.current) return;
    void (props.copyText ?? defaultCopy)(text).then(() => setNotice(done), () => setNotice("Could not copy: the clipboard is not available."));
  };

  const markStep = (row: InboxRow, step: number, verdict: Verdict) => {
    const c = checkOf(row);
    if (!c || !c.markable || step < 0 || step >= c.steps.length) return;
    const key = `${row.id}#${step}`;
    const note = (drafts[key] || "").trim().slice(0, NOTE_MAX);
    void post(row, () => props.onMarkCheck(c.id, step, { verdict, note }), key);
  };

  const readRunbook = async () => {
    const path = importPath.trim();
    if (blockedNow.current || !path || sending.current.has("import")) return;
    sending.current.add("import");
    setImportErr(null);
    setRunbook(null);
    setSaved({});
    try {
      const got = parseDrafts(await props.onImportChecks({ path }));
      if (!got) throw new Error("the daemon's reply was not a list of drafts");
      setRunbook(got);
    } catch (error) {
      setImportErr(error instanceof Error && error.message ? error.message : "the daemon could not be reached");
    } finally {
      sending.current.delete("import");
    }
  };

  const saveDraft = async (index: number) => {
    if (blockedNow.current || !runbook || sending.current.has("import")) return;
    sending.current.add("import");
    setImportErr(null);
    try {
      const got = await props.onImportChecks({ path: runbook.path, digest: runbook.digest, save: index });
      const id = got && typeof got === "object" ? (got as { check_id?: unknown }).check_id : undefined;
      setSaved((all) => ({ ...all, [index]: typeof id === "string" ? id.slice(0, 20) : "saved" }));
      void props.onReread();
    } catch (error) {
      setImportErr(error instanceof Error && error.message ? error.message : "the daemon could not be reached");
    } finally {
      sending.current.delete("import");
    }
  };

  const markKey = (row: InboxRow) => `${row.id}#mark`;

  const mark = (row: InboxRow, body: MarkBody) => {
    // Undo only ever shows a row; a snooze or dismiss only one the rule allows.
    if (!("clear" in body) && !canMark(row)) return;
    if ("clear" in body && !row.hidden) return;
    void post(row, () => props.onMark(row.id, body), markKey(row));
  };

  const snooze = (row: InboxRow, choice: SnoozeFor) => mark(row, { snooze_until: snoozeUntil(choice, clock()) });

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
              // Read-only while its answer is on its way, so nothing typed
              // then is lost when the sent words clear (review of PR #305).
              readOnly={!!busy[row.id]}
              aria-busy={busy[row.id] ? "true" : undefined}
              onChange={(e) => {
                if (sending.current.has(row.id)) return;
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
      // Offered while the row is here, whatever this window's clock says;
      // the hint never claims the window has closed (lib/inbox `withdrawHint`).
      const hint = withdrawHint(row, now);
      return (
        <div className="ir-body">
          {briefOf(row) ? <div className="ir-text" data-testid="inbox-brief">{briefOf(row)}</div> : null}
          <div className="ir-answer">
            <span className="ir-when muted" data-testid="inbox-withdraw-hint" data-hint={hint}>
              {hint === "closing"
                ? "Its window is closing: a Withdraw now may come too late."
                : "Starts on its own when its window closes."}
            </span>
            <button type="button" data-testid="inbox-withdraw" data-hint={hint}
                    className={hint === "closing" ? "closing" : undefined} disabled={off}
                    onClick={() => void send(row, { withdraw: true })}>
              Withdraw
            </button>
          </div>
        </div>
      );
    }
    if (row.kind === "decision") {
      const d = decisionOf(row);
      if (!d) return null;      // not a decision the HUD can read: shown, never answerable
      const head = (
        <>
          {d.question ? <div className="ir-text" data-testid="inbox-decision-question">{d.question}</div> : null}
          {d.why ? <div className="ir-why" data-testid="inbox-decision-why">{d.why}</div> : null}
        </>
      );
      if (!d.open) {
        return (
          <div className="ir-body">
            {head}
            <div className="ir-when muted" data-testid="inbox-decision-answer">Your answer: {d.answer}</div>
          </div>
        );
      }
      const picked = pickedOf(row, d.suggested);
      const typed = drafts[row.id] || "";
      const ready = decisionBody(picked, typed, d) !== null;
      // Keys on a radio stay here (arrows still move the pick); Enter answers
      // nothing from it, and nothing here is a <form>. Escape still reaches the card.
      const keepKeys = (e: ReactKeyboardEvent) => {
        if (e.key !== "Escape") e.stopPropagation();
        if (e.key === "Enter") e.preventDefault();
      };
      return (
        <div className="ir-body">
          {head}
          <div className="ir-choices" role="radiogroup" aria-label="Options" data-testid="inbox-decision-options">
            {d.options.map((option, i) => (
              <label className="ir-choice" key={i}>
                <input
                  type="radio" name={`decision-${row.id}`} value={String(i)} data-testid="inbox-decision-option"
                  data-index={i} checked={picked === i}
                  // Not `disabled` under a card (inert covers it), as the answer box.
                  onChange={() => {
                    if (blockedNow.current || sending.current.has(row.id)) return;
                    setPicks((all) => ({ ...all, [row.id]: i }));
                  }}
                  onKeyDown={keepKeys}
                />
                {/* The HUD's own marker: a separate element, before the option's
                    words, which can never draw it (the daemon refuses an option
                    saying "(suggested)", and so does `decisionOf`). */}
                {i === d.suggested ? <span className="ir-suggested" data-testid="inbox-decision-suggested">suggested</span> : null}
                <span className="ir-option-text">{option}</span>
              </label>
            ))}
          </div>
          <div className="ir-answer">
            <input
              data-testid="inbox-decision-text"
              aria-label="Or answer in your own words"
              placeholder="Or answer in your own words"
              value={typed}
              readOnly={!!busy[row.id]}
              aria-busy={busy[row.id] ? "true" : undefined}
              onChange={(e) => {
                if (sending.current.has(row.id)) return;
                const value = e.target.value;
                setDrafts((all) => ({ ...all, [row.id]: value }));
              }}
              onKeyDown={(e) => {
                if (e.key !== "Escape") e.stopPropagation();
                // Only the focused box answers on Enter, and only with words in it.
                if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  if (typed.trim()) decide(row);
                }
              }}
            />
            <button type="button" data-testid="inbox-decision-send" disabled={off || !ready}
                    onClick={() => decide(row)}>
              Answer
            </button>
          </div>
          {typed.trim() ? <div className="ir-when muted" data-testid="inbox-decision-hint">Your words are sent instead of the option.</div> : null}
        </div>
      );
    }
    if (row.kind === "check") {
      const c = checkOf(row);
      if (!c) return null;      // not a check the HUD can read: shown, never markable
      return (
        <div className="ir-body">
          <div className="ir-text" data-testid="inbox-check-title">{c.title}</div>
          {!c.done && c.deploy && !c.markable ? (
            <div className="ir-when ir-deploy" data-testid="inbox-check-deploy">{c.deploy}</div>
          ) : null}
          <ol className="ir-steps" data-testid="inbox-check-steps">
            {c.steps.map((s, i) => {
              const key = `${row.id}#${i}`;
              const stepOff = props.blocked || !!busy[key] || !c.markable;
              return (
                <li className="ir-step" key={i} data-testid="inbox-check-step" data-verdict={s.verdict ?? ""}>
                  <div className="ir-step-do" data-testid="inbox-check-do">{s.do}</div>
                  {s.expect ? <div className="ir-step-expect" data-testid="inbox-check-expect">Expect: {s.expect}</div> : null}
                  {s.verdict ? (
                    <div className="ir-step-result" data-verdict={s.verdict} data-testid="inbox-check-result">
                      {VERDICT_WORDS[s.verdict]}{s.note ? `: ${s.note}` : ""}
                    </div>
                  ) : null}
                  <div className="ir-answer">
                    <button type="button" className="quiet" data-testid="inbox-check-copy" disabled={props.blocked}
                            onClick={() => copy(s.do, `Copied step ${i + 1}'s words.`)}>
                      Copy
                    </button>
                    {!c.done ? (
                      <>
                        <input
                          data-testid="inbox-check-note"
                          aria-label={`Note on step ${i + 1}`}
                          placeholder="Note (optional)"
                          maxLength={NOTE_MAX}
                          value={drafts[key] || ""}
                          readOnly={!!busy[key]}
                          onChange={(e) => {
                            if (blockedNow.current || sending.current.has(key)) return;
                            const value = e.target.value;
                            setDrafts((all) => ({ ...all, [key]: value }));
                          }}
                          // Enter marks nothing: only a verdict button records one.
                          onKeyDown={(e) => {
                            if (e.key !== "Escape") e.stopPropagation();
                            if (e.key === "Enter") e.preventDefault();
                          }}
                        />
                        {VERDICTS.map((v) => (
                          <button type="button" key={v} data-testid={`inbox-check-${v}`} data-verdict={v}
                                  disabled={stepOff} onClick={() => markStep(row, i, v)}>
                            {VERDICT_BUTTONS[v]}
                          </button>
                        ))}
                      </>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
          {c.steps.some((s) => s.verdict) ? (
            <div className="ir-answer">
              <button type="button" data-testid="inbox-check-receipt" disabled={props.blocked}
                      onClick={() => copy(checkReceipt(c, row.origin), "Copied the receipt as Markdown.")}>
                Copy receipt (Markdown)
              </button>
            </div>
          ) : null}
        </div>
      );
    }
    // "Denied while you were away" (A3, IN-3): information only. Its words are
    // code's (`missedNote`, from `resolution`); nothing here asks again
    // (design §10, question 3): the row's only control is its chat link.
    const missed = missedNote(row);
    if (missed) {
      return (
        <div className="ir-body">
          <div className="ir-text" data-testid="inbox-missed-why">{missed.why}</div>
          <div className="ir-when muted" data-testid="inbox-missed-next">{missed.next}</div>
        </div>
      );
    }
    // A blocked or failed task: information only.
    const brief = briefOf(row);
    return brief ? <div className="ir-body"><div className="ir-text" data-testid="inbox-brief">{brief}</div></div> : null;
  };

  const hidden = hiddenRows(rows);
  const groups = groupRows(showHidden ? rows : shownRows(rows));
  const count = waitingCount(rows);
  const visible = shownRows(rows).length;

  // B4: Snooze and Dismiss for a row of rank 5 to 8 (`canMark`), or, for a
  // hidden row, why it is hidden and Undo. Plain buttons, none defaulted.
  const marks = (row: InboxRow) => {
    const off = props.blocked || !!busy[markKey(row)];
    if (row.hidden) {
      return (
        <div className="ir-marks" data-testid="inbox-marks">
          <span className="ir-when muted" data-testid="inbox-hidden-note">{hiddenNote(row, now)}</span>
          <button type="button" className="quiet" data-testid="inbox-mark-undo" disabled={off}
                  onClick={() => mark(row, { clear: true })}>
            Undo
          </button>
        </div>
      );
    }
    if (!canMark(row)) return null;
    return (
      <div className="ir-marks" data-testid="inbox-marks">
        {SNOOZES.map((s) => (
          <button type="button" key={s.key} className="quiet" data-testid={`inbox-snooze-${s.key}`} disabled={off}
                  onClick={() => snooze(row, s.key)}>
            {s.label}
          </button>
        ))}
        <button type="button" className="quiet" data-testid="inbox-dismiss" disabled={off}
                onClick={() => mark(row, { dismiss: true })}>
          Dismiss
        </button>
      </div>
    );
  };

  return (
    <div className="scroll inbox" data-testid="inbox-view" data-status={status} data-reads={props.snapshot.reads}>
      <div className="inbox-head">
        <h2 className="bar">Inbox</h2>
        <span className="muted small" data-testid="inbox-summary">
          {status === "ready" ? (count ? `${count} waiting on you` : "Nothing waiting on you") : ""}
        </span>
        {hidden.length ? (
          <button type="button" className="quiet" data-testid="inbox-show-hidden" disabled={props.blocked}
                  aria-pressed={showHidden} onClick={() => { if (!blockedNow.current) setShowHidden((v) => !v); }}>
            {showHidden ? `Hide hidden (${hidden.length})` : `Show hidden (${hidden.length})`}
          </button>
        ) : null}
        <button type="button" className="quiet" data-testid="inbox-import-toggle" disabled={props.blocked}
                aria-expanded={importOpen} onClick={() => setImportOpen((v) => !v)}>
          Import a runbook
        </button>
      </div>
      {importOpen ? (
        <div className="inbox-import" data-testid="inbox-import">
          <div className="ir-answer">
            <input
              data-testid="inbox-import-path"
              aria-label="Runbook path"
              placeholder="/home/you/projects/Jarvis/docs/plans/….md"
              value={importPath}
              onChange={(e) => { if (!blockedNow.current) setImportPath(e.target.value); }}
              onKeyDown={(e) => {
                if (e.key !== "Escape") e.stopPropagation();
                if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void readRunbook();
                }
              }}
            />
            <button type="button" data-testid="inbox-import-read" disabled={props.blocked || !importPath.trim()}
                    onClick={() => void readRunbook()}>
              Read
            </button>
          </div>
          <div className="muted small">A runbook directly inside a project's docs/plans/. Nothing is saved until you save a draft.</div>
          {importErr ? <div className="err small" role="alert" data-testid="inbox-import-error">Not imported: {importErr}</div> : null}
          {runbook ? (
            <ol className="ir-drafts" data-testid="inbox-import-drafts">
              {runbook.drafts.map((d) => (
                <li key={d.index} className="ir-draft" data-testid="inbox-import-draft" data-savable={d.savable ? "1" : "0"}>
                  <span className="ir-draft-title">{d.title}</span>
                  <span className="muted small">
                    {` ${d.steps.length} step${d.steps.length === 1 ? "" : "s"}`}
                    {d.prs.length ? `, ${d.prs.map((n) => `#${n}`).join(", ")}` : ""}
                  </span>
                  {saved[d.index] ? (
                    <span className="muted small" data-testid="inbox-import-saved">{` Saved as ${saved[d.index]}.`}</span>
                  ) : d.savable ? (
                    <button type="button" data-testid="inbox-import-save" disabled={props.blocked}
                            onClick={() => void saveDraft(d.index)}>
                      Save
                    </button>
                  ) : (
                    <span className="muted small" data-testid="inbox-import-reason">{` Cannot be saved: ${d.reason ?? "unknown"}.`}</span>
                  )}
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
      {notice ? <div className="inbox-notice" role="status" data-testid="inbox-notice">{notice}</div> : null}
      {status === "loading" && !rows.length ? <div className="pad muted" data-testid="inbox-loading">Reading the inbox…</div> : null}
      {status === "unavailable" ? (
        <div className="pad muted" data-testid="inbox-unavailable">The inbox is not available here: {props.snapshot.message}</div>
      ) : null}
      {status === "error" ? (
        <div className="pad err" data-testid="inbox-error">Could not read the inbox: {props.snapshot.message}</div>
      ) : null}
      {status === "ready" && !visible ? (
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
                   data-rank={row.rank} data-tone={tone(row)} data-hidden={row.hidden ? "1" : undefined}>
                <div className="ir-head">
                  <span className="ir-title" data-testid="inbox-title">{row.title}</span>
                  {asked ? <span className="ir-time muted">{asked}</span> : null}
                </div>
                {row.blocks ? <div className="ir-blocks muted" data-testid="inbox-blocks">{row.blocks}</div> : null}
                {row.origin ? <div className="ir-origin" data-testid="inbox-origin">{row.origin}</div> : null}
                {body(row)}
                {marks(row)}
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
