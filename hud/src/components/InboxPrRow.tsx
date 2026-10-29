// A PR row's body on the HUD inbox (PM-F of docs/plans/2026-10-28-pr-inbox-
// merge.md: PM-24, PM-25, Q3, Q4; the rules in lib/prRow.ts, shared with the
// phone). Top to bottom:
//   1. the PR's own title (the author's words, labelled, in a <bdi>);
//   2. the buttons (AS-12): Merge, Merge as is, Fix, Update as the row's
//      `actions` allow, or code's `why` when there are none; "Hold…" at the
//      end of the row; squash named before the OK (Q4);
//   3. the row's one notice (a result or a refusal), its line held so what
//      it says never moves a button; then the hold (Q3) and the fix (PM-21
//      to PM-23), in code's words;
//   4. code's facts parts and the amber safety-file flags (PM-9, PM-10);
//   5. the AI summary, labelled and set apart, its two lines held from the
//      first frame (AS-14), then code's note on it;
//   6. the GitHub link, small (PM-25, the HUD only).
// Arming (AS-13): every action button is off for ARM_MS after the row
// appears and after anything an OK answers changes (`prArmKey`), and the
// click handler checks again. No button is focused or defaulted. Each press
// sends the head and card this render drew (`actionBody`), never a value read
// again at click time. Every string is React text.

import { useState } from "react";
import type { InboxRow } from "../lib/inbox";
import { useArmed } from "../lib/armed";
import {
  AI_TAG, HOLD_MAX, actionLabel, fixCardLines, fixSummaryWords, methodWords, prArmKey, prButtons, prFixWords,
  prSummaryWords, questionRow, type PrAction, type PrFact, type PrRowView,
} from "../lib/prRow";

export interface PrNotice {
  text: string;
  tone: "plain" | "amber";
}

export interface InboxPrRowProps {
  row: InboxRow;
  view: PrRowView;
  /** Every row in the inbox, to find the fix task's question row. */
  rows: readonly InboxRow[];
  /** The PR's validated GitHub URL (lib/inbox `prOf`), or null: no link. */
  url: string | null;
  /** An approval card is up (everything inert) or an action on this row is on its way. */
  blocked: boolean;
  busy: boolean;
  notice: PrNotice | null;
  /** An action, with exactly the head and card this render drew. */
  onAct: (action: PrAction, view: PrRowView) => void;
  /** Q3: set (`note`) or clear (null) the hold. */
  onHold: (note: string | null) => void;
  onCancelFix: () => void;
  /** The fix's question: its `tq:` row, or (none in the inbox) its task. */
  onQuestion: (rowId: string | null, taskId: string | null) => void;
  isBlocked: () => boolean;
}

/** Code's facts parts, each its own span; names in their own boxed <bdi>, scanners labelled by code. */
export function FactsLine({ facts, testid }: { facts: PrFact[]; testid: string }) {
  return (
    <div className="ir-pr-facts" data-testid={testid}>
      {facts.map((part, i) => (
        <span className="ir-pr-part" key={i} data-key={part.key}>
          {i ? <span className="ir-pr-sep" aria-hidden="true"> · </span> : null}
          <span className="ir-pr-text">{part.text}</span>
          {part.items.map((n, j) => (
            <span className="ir-pr-name" key={j}>
              {" "}<bdi>{n.name}</bdi>
              {n.scanner ? <span className="ir-pr-tag" data-testid="inbox-pr-scanner"> [secret scanner]</span> : null}
            </span>
          ))}
          {part.more ? <span className="ir-pr-text">{` +${part.more} more`}</span> : null}
        </span>
      ))}
    </div>
  );
}

export function InboxPrRow(props: InboxPrRowProps) {
  const { view: v } = props;
  const buttons = prButtons(v);
  const armed = useArmed(prArmKey(v));
  const [holdOpen, setHoldOpen] = useState(false);
  const [holdText, setHoldText] = useState("");
  const off = props.blocked || props.busy;
  const method = methodWords(v, buttons.actions);
  const summary = prSummaryWords(v);
  const fix = v.fix ? prFixWords(v.fix) : null;

  const act = (action: PrAction) => {
    // Checked again here: a press that lands before the row armed, or under a card, does nothing.
    if (!armed || props.isBlocked() || props.busy) return;
    props.onAct(action, v);
  };
  const setHold = () => {
    const note = holdText.trim().slice(0, HOLD_MAX);
    if (!note || props.isBlocked() || props.busy) return;
    props.onHold(note);
    setHoldOpen(false);
    setHoldText("");
  };

  return (
    <div className="ir-body ir-pr" data-testid="inbox-pr" data-head={v.head ?? ""} data-card={v.card ?? ""}>
      {v.title ? (
        <div className="ir-pr-title" data-testid="inbox-pr-title">
          <span className="k">title </span><bdi>{v.title}</bdi>
        </div>
      ) : null}
      <div className="ir-pr-acts" data-testid="inbox-pr-actions" data-armed={armed ? "true" : "false"}>
        {buttons.actions.map((a) => (
          <button type="button" key={a} data-testid={`inbox-pr-act-${a}`} data-action={a}
                  className={a === "merge" || a === "merge_as_is" ? "ir-pr-merge" : undefined}
                  disabled={off || !armed} onClick={() => act(a)}>
            {actionLabel(v, a)}
          </button>
        ))}
        {!buttons.actions.length && v.why && v.hold === null ? (
          <span className="ir-pr-why" data-testid="inbox-pr-why">{v.why}</span>
        ) : null}
        <span className="ir-pr-spacer" />
        {v.hold === null ? (
          <button type="button" className="quiet" data-testid="inbox-pr-hold-open" disabled={off}
                  aria-expanded={holdOpen} onClick={() => { if (!props.isBlocked()) setHoldOpen((o) => !o); }}>
            Hold…
          </button>
        ) : null}
      </div>
      <div className="ir-pr-notice" role="status" data-testid="inbox-pr-notice" data-tone={props.notice?.tone ?? ""}>
        {props.busy ? "Sending…" : props.notice?.text ?? ""}
      </div>
      {buttons.actions.length && v.why ? <div className="ir-pr-why muted" data-testid="inbox-pr-why">{v.why}</div> : null}
      {method ? <div className="ir-pr-method" data-testid="inbox-pr-method">{method}</div> : null}
      {holdOpen && v.hold === null ? (
        <div className="ir-answer" data-testid="inbox-pr-hold-box">
          <input data-testid="inbox-pr-hold-note" aria-label="Why it is on hold" placeholder="Why it is on hold"
                 maxLength={HOLD_MAX} value={holdText}
                 onChange={(e) => { if (!props.isBlocked()) setHoldText(e.target.value); }}
                 onKeyDown={(e) => {
                   if (e.key !== "Escape") e.stopPropagation();
                   if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); setHold(); }
                 }} />
          <button type="button" data-testid="inbox-pr-hold-set" disabled={off || !holdText.trim()} onClick={setHold}>
            Set hold
          </button>
          <button type="button" className="quiet" data-testid="inbox-pr-hold-cancel"
                  onClick={() => { setHoldOpen(false); setHoldText(""); }}>
            Cancel
          </button>
        </div>
      ) : null}
      {v.hold !== null ? (
        <div className="ir-pr-hold" data-testid="inbox-pr-hold">
          <span className="k">On hold:</span>{" "}<bdi>{v.hold || "no note"}</bdi>
          <button type="button" className="quiet" data-testid="inbox-pr-hold-clear" disabled={off}
                  onClick={() => { if (!props.isBlocked()) props.onHold(null); }}>
            Clear hold
          </button>
        </div>
      ) : null}
      {v.fix && fix ? (
        <div className="ir-pr-fix" data-testid="inbox-pr-fix" data-state={v.fix.state} data-tone={fix.tone}>
          <div className="ir-pr-fixline">
            <span data-testid="inbox-pr-fix-text">{fix.text}</span>
            {v.fix.max ? <span className="muted">{` · fix ${v.fix.count} of ${v.fix.max}`}</span> : null}
            {fix.question ? (
              <button type="button" className="quiet ir-link" data-testid="inbox-pr-fix-question" disabled={props.blocked}
                      onClick={() => props.onQuestion(questionRow(v.fix!, props.rows), v.fix!.taskId)}>
                {questionRow(v.fix, props.rows) ? "Answer its question" : "Open its task"}
              </button>
            ) : null}
            {fix.cancel ? (
              <button type="button" data-testid="inbox-pr-fix-cancel" disabled={off}
                      onClick={() => { if (!props.isBlocked()) props.onCancelFix(); }}>
                Cancel the fix
              </button>
            ) : null}
          </div>
          {fix.card ? <FixCard view={v} /> : null}
        </div>
      ) : null}
      {v.facts ? <FactsLine facts={v.facts} testid="inbox-pr-facts" /> : null}
      {v.flags && (v.flags.items.length || v.flags.incomplete) ? (
        <div className="ir-pr-flags" data-testid="inbox-pr-flags">
          <span className="ir-pr-text">touches safety files:</span>
          {v.flags.items.map((f, i) => (
            <span className="ir-pr-name" key={i}>
              {i ? "; " : " "}<bdi>{f.name}</bdi>{f.families.length ? ` (${f.families.join(", ")})` : ""}
            </span>
          ))}
          {v.flags.more ? <span>{`; and ${v.flags.more} more`}</span> : null}
          {v.flags.incomplete ? <span>{"; not every changed file could be checked"}</span> : null}
        </div>
      ) : null}
      <div className={"as-ai ir-pr-summary as-" + summary.state} data-testid="inbox-pr-summary" data-state={summary.state}>
        <span className="as-tag">{AI_TAG}</span>
        {summary.lines.length ? summary.lines.map((l) => (
          <div className="as-text" key={l.label} data-testid={`inbox-pr-${l.label.toLowerCase()}`}>
            <span className="ir-pr-label">{l.label}: </span>{l.text}
          </div>
        )) : <span className="as-text">{summary.text}</span>}
      </div>
      {v.summaryNote ? <div className="ir-pr-note muted" data-testid="inbox-pr-summary-note">{`summary: ${v.summaryNote}`}</div> : null}
      {props.url ? (
        <div className="ir-pr-link">
          <a className="ir-link" data-testid="inbox-pr-link" href={props.url} target="_blank" rel="noopener noreferrer"
             aria-disabled={props.blocked ? "true" : undefined}
             // Under a card nothing opens: a click, a middle click (auxclick)
             // or Enter on the focused link (as BrowserTabStrip and EditorTabs).
             onClick={(e) => { if (props.isBlocked()) e.preventDefault(); }}
             onAuxClick={(e) => { if (props.isBlocked()) e.preventDefault(); }}
             onMouseDown={(e) => { if (props.isBlocked() && e.button === 1) e.preventDefault(); }}
             onKeyDown={(e) => { if (props.isBlocked() && e.key === "Enter") e.preventDefault(); }}>
            Open on GitHub
          </a>
        </div>
      ) : null}
    </div>
  );
}

/** The fix card (PM-22): the fix's AI summary in its own slot, then code's counts and the CI amber. */
function FixCard({ view }: { view: PrRowView }) {
  const fix = view.fix!;
  const words = fixSummaryWords(fix);
  const lines = fixCardLines(fix);
  return (
    <>
      <div className={"as-ai ir-pr-summary as-" + words.state} data-testid="inbox-pr-fix-summary" data-state={words.state}>
        <span className="as-tag">{AI_TAG}</span><span className="ir-pr-label">the fix: </span>
        <span className="as-text">{words.text}</span>
      </div>
      {lines.commits ? <div className="ir-pr-note" data-testid="inbox-pr-fix-commits">{lines.commits}</div> : null}
      {lines.ci ? <div className="ir-pr-amber" data-testid="inbox-pr-fix-ci">{lines.ci}</div> : null}
    </>
  );
}
