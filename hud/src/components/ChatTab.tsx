// The chat tab: rendered replies, the live draft, the OPERATIONS ticker, and
// a proposal_reply shown as a system line with a 60-second Cancel.
//
// Two rules carried from v1. The draft is **plain text** while it is being
// written (half a markdown document is not markdown; rendering `**bold`
// mid-word flickers) and is replaced by the rendered reply once, on `text`.
// And the owner's own message goes up **verbatim** — typing `*foo*` means
// `*foo*` — while only his is rendered.

import { useEffect, useRef, useState } from "react";
import type { ChatMessage, ToolOp } from "../types";
import { Markdown } from "./Markdown";

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

export function ChatTab(props: {
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

  return (
    <div className="tabbody">
      <div className="scroll" id="log" ref={logRef} data-testid="log">
        {props.messages.map((m, i) =>
          m.proposal ? (
            <Proposal key={i} msg={m} onCancel={props.onCancelTask} />
          ) : (
            <div className={`msg ${m.role}`} key={i} data-testid={`msg-${m.role}`}>
              <div className="who">{m.role === "user" ? "YOU" : m.role === "system" ? "SYSTEM" : "JARVIS"}</div>
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
      <div id="ops" data-testid="ops">
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
}
