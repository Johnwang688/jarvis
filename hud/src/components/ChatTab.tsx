// The chat tab: rendered replies, the live draft, the OPERATIONS ticker, and
// a proposal_reply shown as a system line with a 60-second Cancel.
//
// Two rules carried from v1. The draft is **plain text** while it is being
// written (half a markdown document is not markdown; rendering `**bold`
// mid-word flickers) and is replaced by the rendered reply once, on `text`.
// And the owner's own message goes up **verbatim** — typing `*foo*` means
// `*foo*` — while only his is rendered.

import { memo, useEffect, useRef, useState } from "react";
import type { ChatMessage, Thread, ToolOp } from "../types";
import { chatPlace } from "../lib/discord";
import { Markdown } from "./Markdown";

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

/**
 * One chat pane's transcript and tool ticker. Memoized: typing in any box
 * re-renders the window (the box's words are the store's, WP-B), and a long
 * transcript redrawn per keystroke cost milliseconds per character, per
 * chat pane (re-review of PR #27). Every prop is stable between keystrokes.
 */
export const ChatTab = memo(function ChatTab(props: {
  /** The open chat, for its "On Discord" header (PR C); null while composing. */
  thread?: Thread | null;
  messages: ChatMessage[];
  draft: string;
  ops: ToolOp[];
  onCancelTask: (id: string) => void;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [props.messages, props.draft]);

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
      <div className="scroll chatlog" ref={logRef} data-testid="log">
        {props.messages.map((m, i) =>
          m.proposal ? (
            <Proposal key={i} msg={m} onCancel={props.onCancelTask} />
          ) : (
            <div className={`msg ${m.role}`} key={i} data-testid={`msg-${m.role}`}>
              <div className="who">
                {m.role === "user" ? "YOU" : m.role === "system" ? "SYSTEM" : "JARVIS"}
                {m.role === "user" && m.via ? (
                  <span className="via" data-testid="via-discord"> · via Discord</span>
                ) : null}
                {m.role === "user" && m.mark ? (
                  <span className={`mark ${m.mark.replace(" ", "-")}`} data-testid="msg-mark">
                    {" · "}
                    {MARKS[m.mark]}
                  </span>
                ) : null}
              </div>
              {m.role === "assistant" ? (
                <Markdown text={m.text} />
              ) : (
                <div className="body user-text">{m.text}</div>
              )}
            </div>
          ),
        )}
        {props.draft ? (
          <div className="msg assistant draft" data-testid="draft">
            <div className="who">JARVIS</div>
            {/* plain text while drafting, deliberately */}
            <div className="body">{props.draft}</div>
          </div>
        ) : null}
      </div>
      <div className="chatops" data-testid="ops">
        {props.ops
          .slice(-30)
          .reverse()
          .map((o) => (
            <div className={"op" + (o.finished ? " done" : "")} key={o.call_id} data-testid="op">
              <span className="nm">{o.name}</span>
              <span>{o.finished ? (o.ok === false ? "failed" : "done") : "running"}</span>
              <span className="ms">
                {o.finished ? `${((o.finished - o.started) / 1000).toFixed(1)}s` : ""}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
});
