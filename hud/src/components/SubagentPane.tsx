// A native sub-agent's live transcript, read-only, in a pane of its own
// (SV-3, plan §D5 "The pane"). It is the owner's transcript route as served —
// the child's texts, its tool calls by label and outcome (never arguments
// or results), the CLI's closing word, a reviewer's decline by the tool's
// name — and, at the top and collapsed, the brief the orchestrator gave it
// (owner, 2026-10-15). No input box and no Stop (SV-4): steering stays with
// the orchestrator. It follows the bottom only when the reader is there.
//
// A child's prose is untrusted data: Markdown's own sanitising, text nodes
// everywhere else, and the brief as plain text.

import { Fragment, memo, useEffect, useMemo, useRef, useState } from "react";
import {
  PROVIDER_WORD, childrenOf, displayName, elapsedOf, isLive, placeRows, placeableItem, providerOf,
  statusMark, tokensText, toolsText, type PaneItem, type Subagent, type SubagentRef,
} from "../lib/subagents";
import { noPointerFocus } from "../lib/talkkey";
import { useAnchors, usePane, useRow, useWatch } from "../state/subagents";
import { Markdown } from "./Markdown";
import { SubagentRows } from "./SubagentRow";
import { ToolGroupRow } from "./ToolGroupRow";

const FAILED: Record<string, string> = {
  gone: "This sub-agent's record is gone.",
  old: "Update Jarvis to see sub-agents.",
  refused: "Only this window's owner can read a sub-agent's transcript.",
  error: "Could not read this sub-agent's transcript. It is read again when Jarvis says more.",
};

function Item({ item, threadId, sid, drawn }: { item: PaneItem; threadId: string; sid: string; drawn: boolean }) {
  switch (item.kind) {
    case "text":
      return (
        <div className="msg assistant" data-testid="sa-msg-text">
          <div className="who">SUB-AGENT</div>
          <Markdown text={item.text} />
        </div>
      );
    case "tools":
      return <ToolGroupRow group={item.group} threadId={`subagent:${threadId}|${sid}`} using={false} drawn={drawn} />;
    case "result":
      return (
        <div className="msg system sa-result" data-testid="sa-msg-result" data-status={item.status ?? ""}>
          <div className="who">RESULT{item.status ? ` · ${statusMark(item.status).word.toUpperCase()}` : ""}</div>
          <Markdown text={item.text} />
        </div>
      );
    case "declined":
      return (
        <div className="msg system" data-testid="sa-msg-declined">
          <div className="body user-text">The reviewer declined a {item.name} call.</div>
        </div>
      );
  }
}

function Header({ row, live, drawn, threadTitle, onShowInChat }: {
  row: Subagent; live: boolean; drawn: boolean; threadTitle: string; onShowInChat: (() => void) | null;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!drawn || !live) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [drawn, live]);
  const provider = providerOf(row.sid);
  const mark = statusMark(row.status);
  const time = elapsedOf(row, now);
  return (
    <div className="sa-head" data-testid="sa-head">
      <span className="sa-mark" aria-hidden="true">◇</span>
      <span className="sa-name" data-testid="sa-head-name">{displayName(row)}</span>
      <span className={`badge ${provider}`}>{PROVIDER_WORD[provider]}</span>
      {row.kind ? <span className="sa-kind">{row.kind}</span> : null}
      <span className={`sa-status ${mark.tone}`} data-testid="sa-head-status" title={row.stop_reason || undefined}>
        {mark.word}{row.stop_reason && !isLive(row.status) ? ` · ${row.stop_reason}` : ""}
      </span>
      {time ? <span className="sa-time">{time}</span> : null}
      {row.tool_uses !== undefined ? <span className="sa-count">{toolsText(row.tool_uses)}</span> : null}
      {row.tokens !== undefined ? <span className="sa-count">{tokensText(row.tokens)}</span> : null}
      {row.declined ? <span className="sa-count">{row.declined} declined</span> : null}
      <span className="sa-of muted" title={threadTitle}>of {threadTitle || "its chat"}</span>
      <button type="button" className="quiet" data-testid="sa-show-in-chat" disabled={!onShowInChat}
              onMouseDown={noPointerFocus}
              title={onShowInChat ? "Show this sub-agent's row in its chat" : "Its chat is not open in a pane"}
              onClick={() => onShowInChat?.()}>
        Show in chat
      </button>
    </div>
  );
}

function Brief({ text, provider }: { text: string; provider: "claude" | "codex" }) {
  const [open, setOpen] = useState(false);
  if (!text) {
    return (
      <div className="sa-brief muted small" data-testid="sa-brief-none">
        {provider === "codex" ? "Codex does not send a sub-agent's brief." : "No brief was recorded."}
      </div>
    );
  }
  return (
    <div className="sa-brief" data-testid="sa-brief">
      <button type="button" className="sa-toggle" data-testid="sa-brief-toggle" aria-expanded={open}
              onMouseDown={noPointerFocus} onClick={() => setOpen((o) => !o)}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span> Brief
      </button>
      {open ? <div className="sa-brief-text user-text" data-testid="sa-brief-text">{text}</div> : null}
    </div>
  );
}

/** The `subagent` view's body. `onShowInChat` is null when no drawn chat pane shows the thread. */
export const SubagentPane = memo(function SubagentPane(props: {
  sub: SubagentRef;
  drawn: boolean;
  threadTitle: string;
  onShowInChat: (() => void) | null;
  onClose: () => void;
}) {
  const { threadId, sid } = props.sub;
  useWatch(threadId);
  const live = useRow(threadId, sid);
  const pane = usePane(threadId, sid);
  const anchors = useAnchors(threadId);
  const row = live ?? pane.data?.subagent ?? null;
  const items = pane.data?.items;
  const placed = useMemo(
    () => (items ? placeRows(items.map(placeableItem), childrenOf(anchors, sid)) : null),
    [items, anchors, sid],
  );
  const logRef = useRef<HTMLDivElement>(null);
  const follows = useRef(true);
  useEffect(() => {
    const el = logRef.current;
    if (el && follows.current) el.scrollTop = el.scrollHeight;
  }, [items, placed]);

  if (pane.failed && !pane.data) {
    return (
      <div className="tabbody sa-pane" data-testid="sa-pane" data-sid={sid}>
        <div className="pad" data-testid="sa-failed" data-why={pane.failed}>
          <p className="muted">{FAILED[pane.failed]}</p>
          {pane.failed === "gone" ? (
            <button type="button" data-testid="sa-close" onClick={props.onClose}>Close</button>
          ) : null}
        </div>
      </div>
    );
  }
  return (
    <div className="tabbody sa-pane" data-testid="sa-pane" data-sid={sid}>
      {row ? <Header row={row} live={isLive(row.status)} drawn={props.drawn} threadTitle={props.threadTitle}
                     onShowInChat={props.onShowInChat} /> : null}
      {pane.data ? <Brief text={pane.data.brief} provider={providerOf(sid)} /> : null}
      <div className="scroll chatlog" ref={logRef} data-testid="sa-log" onScroll={(e) => {
        if (e.target !== e.currentTarget) return;
        const el = e.currentTarget;
        follows.current = el.scrollHeight - el.clientHeight - el.scrollTop <= 24;
      }}>
        {pane.loading ? <div className="pad muted small" data-testid="sa-loading">Reading…</div> : null}
        {pane.data?.truncated ? (
          <div className="pad muted small" data-testid="sa-truncated">Older entries are no longer kept.</div>
        ) : null}
        {placed?.get(-1) ? <SubagentRows threadId={threadId} rows={placed.get(-1)!} drawn={props.drawn} /> : null}
        {items?.map((item, i) => (
          <Fragment key={item.kind === "tools" ? `t:${item.group.calls![0].call_id}` : `${item.kind}:${item.seq}`}>
            <Item item={item} threadId={threadId} sid={sid} drawn={props.drawn} />
            {placed?.get(i) ? <SubagentRows threadId={threadId} rows={placed.get(i)!} drawn={props.drawn} /> : null}
          </Fragment>
        ))}
        {items && !items.length && !placed?.size ? (
          <div className="pad muted small" data-testid="sa-empty">
            {row && isLive(row.status) ? "Nothing yet." : "This sub-agent left nothing to show."}
          </div>
        ) : null}
        {pane.failed && pane.data ? <div className="pad muted small">{FAILED[pane.failed]}</div> : null}
      </div>
    </div>
  );
});
