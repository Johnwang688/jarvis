// The chat tab: rendered replies, the live draft, the collapsed tool groups, and
// a proposal_reply shown as a system line with a 60-second Cancel.
//
// Two rules carried from v1. The draft is **plain text** while it is being
// written (half a markdown document is not markdown; rendering `**bold`
// mid-word flickers) and is replaced by the rendered reply once, on `text`.
// And the owner's own message goes up **verbatim** — typing `*foo*` means
// `*foo*` — while only his is rendered.
//
// While this chat's turn runs, its foot says so (WP-H, decisions S-4): the
// thinking line — mini orb, word, elapsed time — right after the transcript's
// scroller and above the input bar. It is outside `[data-testid=log]`, so what
// the log says is the conversation and nothing else.

import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage, Thread } from "../types";
import { chatPlace } from "../lib/discord";
import { ToolGroupRow } from "./ToolGroupRow";
import { Markdown } from "./Markdown";
import { ThinkingRow } from "./MiniOrb";
import type { ThinkingLine } from "../lib/thinking";
import { childrenOf, placeRows, placeableOf } from "../lib/subagents";
import { useAnchors, useWatch } from "../state/subagents";
import { SubagentRows } from "./SubagentRow";

/** What became of a message sent while a turn ran (2026-10-08). */
const MARKS: Record<string, string> = {
  steering: "steering",
  queued: "queued · will run when this turn ends",
  // A Stop, almost always; a thread that could not take its turn otherwise.
  "not sent": "not sent",
};

function Proposal({ msg, onCancel }: { msg: ChatMessage; onCancel: (id: string) => void }) {
  const [left, setLeft] = useState(() => Math.max(0, Math.round((msg.proposal!.until - Date.now()) / 1000)));
  useEffect(() => {
    const t = setInterval(
      () => setLeft(Math.max(0, Math.round((msg.proposal!.until - Date.now()) / 1000))),
      500,
    );
    return () => clearInterval(t);
  }, [msg]);
  return (
    <div className="msg system" data-testid="proposal">
      <div className="who">PROPOSED</div>
      <div className="user-text">{msg.text}</div>
      {left > 0 ? (
        <button
          type="button"
          data-testid="proposal-cancel"
          style={{ marginTop: 6 }}
          onClick={() => onCancel(msg.proposal!.task_id)}
        >
          Cancel ({left}s)
        </button>
      ) : (
        <span className="muted small"> · window closed</span>
      )}
    </div>
  );
}

export const MessageRow = memo(function MessageRow({ msg: m, onCancel }: { msg: ChatMessage; onCancel: (id: string) => void }) {
  if (m.proposal) return <Proposal msg={m} onCancel={onCancel} />;
  return <div className={`msg ${m.role}`} data-testid={`msg-${m.role}`}>
    <div className="who">
      {m.role === "user" ? "YOU" : m.role === "system" ? "SYSTEM" : "JARVIS"}
      {m.role === "user" && m.via ? <span className="via" data-testid="via-discord"> · via Discord</span> : null}
      {m.role === "user" && m.mark ? <span className={`mark ${m.mark.replace(" ", "-")}`} data-testid="msg-mark"> · {MARKS[m.mark]}</span> : null}
    </div>
    {m.role === "assistant" ? <Markdown text={m.text} /> : <div className="body user-text">{m.text}</div>}
  </div>;
});

/**
 * One chat pane's transcript and grouped tool calls. Memoized: typing in any box
 * re-renders the window (the box's words are the store's, WP-B), and a long
 * transcript redrawn per keystroke cost milliseconds per character, per
 * chat pane (re-review of PR #27). Every prop is stable between keystrokes.
 */
export const ChatTab = memo(function ChatTab(props: {
  /** The open chat, for its "On Discord" header (PR C); null while composing. */
  thread?: Thread | null;
  messages: ChatMessage[];
  draft: string;
  toolTurnId?: string | null;
  busy?: boolean;
  onCancelTask: (id: string) => void;
  /** The thinking line (lib/thinking `thinkingLine`), or null while nothing runs. */
  thinking?: ThinkingLine | null;
  /** The avatar's accent ("r,g,b"): it recolours "Thinking…" only. */
  accent?: string | null;
  /** The pane is on screen: the line's time ticks. */
  drawn?: boolean;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  const follows = useRef(true);
  const shown = useRef(props.thread?.id);
  // The chat's own native sub-agents (SV-3): one row each, where it was
  // spawned — the parent's Agent call (Claude) or the time of its start
  // (Codex). Only the top level here; a nested one is in its parent's pane.
  const tid = props.thread?.id ?? null;
  useWatch(tid);
  const anchors = useAnchors(tid);
  const placed = useMemo(
    () => (tid && anchors.length ? placeRows(props.messages.map(placeableOf), childrenOf(anchors, null)) : null),
    [tid, anchors, props.messages],
  );
  const drawn = props.drawn !== false;
  // Markdown fills each new body in a passive effect; follow after those
  // child effects, when the transcript has its final height.
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    if (shown.current !== props.thread?.id) { follows.current = true; shown.current = props.thread?.id; }
    if (follows.current) el.scrollTop = el.scrollHeight;
  }, [props.messages, props.draft, props.thread?.id, placed]);

  const place = chatPlace(props.thread);
  return (
    <div className="tabbody">
      {place ? (
        <div className="chat-place muted small" data-testid="chat-discord">
          {place.url ? (
            <a href={place.url} target="_blank" rel="noopener noreferrer">{place.text}</a>
          ) : (
            place.text
          )}
        </div>
      ) : null}
      <div className="scroll chatlog" ref={logRef} data-testid="log" onScroll={e => {
        if (e.target !== e.currentTarget) return;
        const el = e.currentTarget;
        follows.current = el.scrollHeight - el.clientHeight - el.scrollTop <= 24;
      }}>
        {tid && placed?.get(-1) ? <SubagentRows threadId={tid} rows={placed.get(-1)!} drawn={drawn} /> : null}
        {props.messages.map((m, i) => {
          const rows = tid ? placed?.get(i) : undefined;
          const body = m.calls ? (
            <ToolGroupRow group={m}
              threadId={props.thread?.id ?? ""} using={!!props.busy && i === props.messages.length - 1 && m.turn_id === props.toolTurnId}
              drawn={drawn} />
          ) : <MessageRow msg={m} onCancel={props.onCancelTask} />;
          return <Fragment key={m.calls ? `tool:${m.turn_id}|${m.calls[0].call_id}` : `msg:${i}`}>
            {body}
            {rows ? <SubagentRows threadId={tid!} rows={rows} drawn={drawn} /> : null}
          </Fragment>;
        })}
        {props.draft ? (
          <div className="msg assistant draft" data-testid="draft">
            <div className="who">JARVIS</div>
            {/* plain text while drafting, deliberately */}
            <div className="body">{props.draft}</div>
          </div>
        ) : null}
      </div>
      {props.thinking ? <ThinkingRow line={props.thinking} accent={props.accent} drawn={!!props.drawn} /> : null}
    </div>
  );
});
