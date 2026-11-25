import { useId, useState, type FormEvent, type KeyboardEvent, type MouseEvent } from "react";
import type { Brief, FeedWrite } from "./feedApi";

const kindLabels = { decision: "Decision", news: "News", digest: "Digest" };
const resolutionLabels = {
  approved: "Approved", edited: "Edited", other: "Answered",
  dismissed: "Acknowledged", expired: "Expired", done: "Done",
};
const feedback = [
  ["too_long", "Too long"],
  ["missing_context", "Missing context"],
] as const;
const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
});

// A closed row has room for the day only; the open brief shows the full time.
const dayFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

function display(text: string) {
  return text.replace(/\p{Bidi_Control}/gu, "");
}

function timeLabel(value: string, format = dateFormat) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? display(value) : format.format(date);
}

function httpsRef(ref: string): string | undefined {
  if (!/^https:\/\//i.test(ref) || display(ref) !== ref || /[\r\n\t]/.test(ref)) return;
  try {
    const url = new URL(ref);
    if (url.protocol === "https:" && !url.username && !url.password) return url.href;
  } catch {
    // Keep malformed references visible as text.
  }
}

function BriefContext({ sources }: { sources: Brief["context"] }) {
  return (
    <details className="feed-context">
      <summary>Context</summary>
      {sources.length ? (
        <ul>
          {sources.map(({ label, ref }, index) => {
            const href = httpsRef(ref);
            return (
              <li key={index}>
                <bdi>{display(label)}</bdi>
                {href ? <a href={href} target="_blank" rel="noreferrer noopener"><bdi>{display(ref)}</bdi></a>
                  : <bdi>{display(ref)}</bdi>}
              </li>
            );
          })}
        </ul>
      ) : <p>No sources listed.</p>}
    </details>
  );
}

function DefaultOutcome({ fallback, kind }: { fallback: NonNullable<Brief["default"]>; kind: Brief["kind"] }) {
  const when = fallback.at === null ? null : timeLabel(fallback.at);
  if (fallback.action === "none") {
    return when && kind === "decision" ? <>No action. Closes after <bdi>{when}</bdi>.</> : <>No action scheduled.</>;
  }
  return (
    <>
      <bdi>{display(fallback.action)}</bdi>
      {when && kind === "decision" ? <> is recorded as the default. The brief expires after <bdi>{when}</bdi>.</>
        : <> is listed as the default. No action is scheduled.</>}
    </>
  );
}

export type BriefDraft = { action: "edit" | "other"; text: string; brief: Brief };

export function BriefCard({ brief, busy, error, write, draft, setDraft, missing }: {
  brief: Brief;
  busy: boolean;
  error?: string;
  write: (change: FeedWrite) => Promise<boolean>;
  draft?: BriefDraft;
  setDraft: (id: string, draft: BriefDraft | null) => void;
  missing: boolean;
}) {
  const editor = draft?.action;
  const text = draft?.text ?? "";
  const headingId = useId();
  const moreId = useId();
  const answersId = useId();
  const open = !missing && brief.state === "open";
  const decision = brief.kind === "decision";
  const urgent = open && brief.rank <= 2;
  // A brief is a two-line row until the owner opens it. An urgent decision
  // starts open, and a brief with an answer being written stays open.
  const [chosen, setChosen] = useState<boolean | null>(null);
  const expanded = !!draft || (chosen ?? (urgent && decision));
  const Heading = open ? "h3" : "h4";
  const { id } = brief;

  async function answer(body: Extract<FeedWrite, { route: "answer" }>["body"]) {
    if (open && await write({ route: "answer", body })) setDraft(id, null);
  }

  // The second press of a double-click is not an answer: the first may have
  // only just opened the row.
  function pressed(event: MouseEvent<HTMLElement>, body: Extract<FeedWrite, { route: "answer" }>["body"]) {
    if (event.detail > 1) return;
    void answer(body);
  }

  function startEditor(action: BriefDraft["action"]) {
    if (open && action !== editor) setDraft(id, { action, text: "", brief });
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!open || busy || !editor || !text.trim()) return;
    void answer({ id, action: editor, text });
  }

  // A press anywhere on a closed row opens it; only the title closes it again.
  function onRowClick(event: MouseEvent<HTMLElement>) {
    if (expanded || event.defaultPrevented) return;
    const target = event.target;
    if (target instanceof Element && target.closest("button, a, input, textarea, select, summary")) return;
    setChosen(true);
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    // A closed row shows no answers, so its keys give none.
    if (!open || !decision || !expanded || busy || event.defaultPrevented || event.repeat || event.nativeEvent.isComposing
      || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const target = event.target;
    if (!(target instanceof HTMLElement) || target !== document.activeElement
      || !event.currentTarget.contains(target) || target.isContentEditable
      || target.closest("input, textarea, select, [contenteditable]")) return;

    switch (event.key.toLowerCase()) {
      case "a": void answer({ id, action: "approve" }); break;
      case "e": startEditor("edit"); break;
      case "n": void answer({ id, action: "not_now" }); break;
      default: return;
    }
    event.preventDefault();
  }

  return (
    <article className={`feed-card${urgent ? " feed-priority" : ""}`} tabIndex={open && decision ? 0 : undefined}
      aria-labelledby={headingId} data-collapsed={!expanded || undefined} onKeyDown={onKeyDown} onClick={onRowClick}>
      <header className="feed-ask">
        <Heading id={headingId}>
          <button type="button" className="feed-toggle" aria-expanded={expanded} aria-controls={open && decision ? `${answersId} ${moreId}` : moreId}
            onClick={() => setChosen(!expanded)}><bdi>{display(brief.ask)}</bdi></button>
        </Heading>
        <div className="feed-chips">
          <span>{kindLabels[brief.kind]}</span>
          {brief.due !== null && <span>Due <bdi>{timeLabel(brief.due, expanded ? dateFormat : dayFormat)}</bdi></span>}
          <span className={urgent && decision ? "feed-rank feed-waiting" : "feed-rank"}>Rank {brief.rank}</span>
          {brief.state !== "open" && (
            <span>{brief.state === "dismissed" && decision ? "Not now" : resolutionLabels[brief.state]}</span>
          )}
        </div>
      </header>
      {open && (
        <div className="feed-actions" id={answersId} hidden={decision && !expanded}>
          {decision ? (
            <>
              <button type="button" className="feed-primary" aria-keyshortcuts="A" disabled={busy} onClick={(event) => pressed(event, { id, action: "approve" })}>Approve</button>
              <button type="button" aria-keyshortcuts="E" aria-expanded={editor === "edit"} disabled={busy} onClick={() => startEditor("edit")}>Edit</button>
              <button type="button" aria-expanded={editor === "other"} disabled={busy} onClick={() => startEditor("other")}>Other…</button>
              <button type="button" aria-keyshortcuts="N" disabled={busy} onClick={(event) => pressed(event, { id, action: "not_now" })}>Not now</button>
            </>
          ) : <button type="button" disabled={busy} onClick={(event) => pressed(event, { id, action: "not_now" })}>Got it</button>}
        </div>
      )}
      {draft && (
        <form className="feed-editor" onSubmit={submit}>
          <label>
            {editor === "edit" ? "Your edit" : "Your answer"}
            <textarea key={editor} autoFocus={open} rows={2} maxLength={2000}
              readOnly={busy} value={text} onChange={(event) => setDraft(id, { ...draft, text: event.target.value })} />
          </label>
          {!open && <p role="status">This brief is closed.</p>}
          <div className="feed-actions">
            <button type="submit" className="feed-primary" disabled={!open || busy || !text.trim()}>Submit</button>
            <button type="button" disabled={busy} onClick={(event) => {
              event.currentTarget.closest("article")?.focus();
              setDraft(id, null);
            }}>Cancel</button>
          </div>
        </form>
      )}
      {error && <p className="feed-error" role="status">{error}</p>}
      {brief.why_now && <p className="feed-why"><strong>Why now</strong> <bdi>{display(brief.why_now)}</bdi></p>}
      <div className="feed-more" id={moreId} hidden={!expanded}>
      {brief.recommend && (
        <p><strong>Recommend</strong> <bdi>{display(brief.recommend.label)}</bdi> — because <bdi>{display(brief.recommend.because)}</bdi></p>
      )}
      {brief.options.length > 0 && (
        <p><strong>Other options</strong> {brief.options.map(({ label }, index) => (
          <span key={index}>{index > 0 && ", "}<bdi>{display(label)}</bdi></span>
        ))}</p>
      )}
      {brief.default && (
        <p><strong>If you ignore it</strong> <DefaultOutcome fallback={brief.default} kind={brief.kind} /></p>
      )}
      <BriefContext sources={brief.context} />
      <div className="feed-feedback" role="group" aria-label="Brief feedback">
        {feedback.map(([tag, label]) => (
          <button key={tag} type="button" disabled={missing || busy || brief.feedback.includes(tag)}
            aria-pressed={brief.feedback.includes(tag)}
            onClick={() => void write({ route: "feedback", body: { id, tag } })}>{label}</button>
        ))}
      </div>
      </div>
    </article>
  );
}
