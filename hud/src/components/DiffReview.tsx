import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DiffReviewView, ReviewFinding } from "../types";
import {
  CANCEL_HELP, HARNESS_NAMES, QUICK_REVIEW_HELP, costLine, countLine, headerLine, location, scopeLine, target,
} from "../lib/diffreview";

/** How often a running review is read again. The review publishes nothing on
 *  the bus (D2: never spoken, never on Discord), so the panel polls. */
export const POLL_MS = 1500;

export interface QuickReview {
  view: DiffReviewView | null;
  error: string;
  pending: boolean;
  running: boolean;
  press: () => void;
}

/** One task's Quick review: what the daemon holds, and the button's action
 *  (start, or Cancel while one runs). */
export function useQuickReview(taskId: string | null): QuickReview {
  const [view, setView] = useState<DiffReviewView | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const current = useRef(taskId);
  current.current = taskId;

  const take = useCallback((id: string, next: DiffReviewView) => {
    if (current.current === id) setView(next);
  }, []);

  useEffect(() => {
    setView(null);
    setError("");
    setPending(false);
    if (!taskId) return;
    api.diffReview(taskId).then((v) => take(taskId, v)).catch(() => {});
  }, [taskId, take]);

  const running = view?.review?.state === "running";
  useEffect(() => {
    if (!taskId || !running) return;
    const timer = setTimeout(() => {
      api.diffReview(taskId).then((v) => take(taskId, v)).catch((e) => setError(e.message));
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [taskId, running, view, take]);

  const press = useCallback(() => {
    if (!taskId || pending) return;
    setPending(true);
    setError("");
    (running ? api.cancelDiffReview(taskId) : api.startDiffReview(taskId))
      .then((v) => take(taskId, v))
      .catch((e) => {
        if (current.current === taskId) setError(e.message);
        // A 409 says another window's review is running: show it.
        return api.diffReview(taskId).then((v) => take(taskId, v)).catch(() => {});
      })
      .finally(() => {
        if (current.current === taskId) setPending(false);
      });
  }, [taskId, pending, running, take]);

  return { view, error, pending, running, press };
}

/** The Diff tab header's one button (D8): "Quick review", or, while one runs,
 *  a spinner, the harness and Cancel. A plain `button`, so PR #210's 4px
 *  rectangle and the Graphite colours. */
export function QuickReviewButton(props: { review: QuickReview }) {
  const { view, pending, running, press } = props.review;
  const trying = view?.review?.trying;
  return (
    <button
      type="button"
      className={"qr-btn" + (running ? " running" : "")}
      data-testid="quick-review"
      data-state={running ? "running" : "idle"}
      aria-busy={running || undefined}
      disabled={pending}
      title={running ? CANCEL_HELP : QUICK_REVIEW_HELP}
      onClick={press}
    >
      {running ? (
        <>
          <span className="vexspinner" aria-hidden="true" />
          <span className="qr-who" data-testid="quick-review-harness">{HARNESS_NAMES[trying || ""] || "…"}</span>
          <span className="qr-cancel">Cancel</span>
        </>
      ) : (
        "Quick review"
      )}
    </button>
  );
}

function Finding(props: { f: ReviewFinding; paths: string[]; onOpen: (file: string, line: number | null) => void }) {
  const { f } = props;
  const to = target(f, props.paths);
  const body = (
    <>
      <span className={"qr-sev " + f.severity}>{f.severity}</span>
      <span className="qr-loc">{location(f)}</span>
      <span className="qr-why">{f.why}</span>
    </>
  );
  if (!to) {
    return (
      <div className="qr-item off" data-testid="qr-finding" title="Not a file in this diff">
        {body}
      </div>
    );
  }
  return (
    <button
      type="button"
      className="qr-item"
      data-testid="qr-finding"
      title={`Open ${location(f)}`}
      onClick={() => props.onOpen(to.file, to.line)}
    >
      {body}
    </button>
  );
}

/** The findings panel, inline above the diff editor (D2). Every string is
 *  rendered as React text: no Markdown, no HTML, no links (D5). */
export function QuickReviewPanel(props: {
  review: QuickReview;
  paths: string[];
  onOpen: (file: string, line: number | null) => void;
}) {
  const { view, error } = props.review;
  const [folded, setFolded] = useState(false);
  const review = view?.review;
  if (!review) {
    if (error) return <div className="qreview" data-testid="qreview"><div className="qr-error">{error}</div></div>;
    if (view?.outdated) {
      return (
        <div className="qreview" data-testid="qreview" data-state="outdated">
          <div className="qr-head muted">The diff changed since the last quick review.</div>
        </div>
      );
    }
    return null;
  }
  const done = review.state === "done";
  const scope = scopeLine(review);
  const cost = costLine(review);
  return (
    <div className="qreview" data-testid="qreview" data-state={review.state}>
      <div className="qr-head">
        <span className="qr-label" data-testid="qr-header">{headerLine(review)}</span>
        <span className="qr-meta">
          {done ? countLine(review.findings) : ""}
          {done && cost ? " · " : ""}
          {cost}
        </span>
        {done && review.findings.length ? (
          <button type="button" className="qr-fold" aria-expanded={!folded} onClick={() => setFolded((f) => !f)}>
            {folded ? "Show" : "Hide"}
          </button>
        ) : null}
      </div>
      {error ? <div className="qr-error">{error}</div> : null}
      {done && review.summary && !folded ? <div className="qr-summary">{review.summary}</div> : null}
      {scope && !folded ? <div className="qr-scope" data-testid="qr-scope">{scope}</div> : null}
      {done && !folded && review.findings.length ? (
        <div className="qr-list">
          {review.findings.map((f, i) => (
            <Finding key={i} f={f} paths={props.paths} onOpen={props.onOpen} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
