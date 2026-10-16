// A native sub-agent's row in its parent's transcript (SV-3, plan §D5), at
// the point it was spawned: name, provider, kind, status (always a word, in
// Graphite: amber live, green done, red failed, muted stopped), the last
// tool, how long it ran, its tool uses, tokens and declined calls. The row
// is one button: Enter, Space or a click opens the child's live transcript
// in a read-only pane. Beside it, for a live child, the owner's Stop (SV-4,
// `SubagentStop`): a sibling button, never nested in the row's.
//
// Every word on it is text the model chose (name, kind, last tool), cleaned
// again by lib/subagents and rendered as text nodes only. More than six rows
// at one spot collapse into one line, "Sub-agents · 8 (5 running)".

import { memo, useEffect, useId, useState } from "react";
import {
  COLLAPSE_OVER, PROVIDER_WORD, blockTitle, displayName, elapsedOf, isLive, isOwnersPress, providerOf, statusMark,
  stopView, tokensText, toolsText, type Subagent,
} from "../lib/subagents";
import { noPointerFocus } from "../lib/talkkey";
import { subagents, useBlockOpen, useRow, useRows, useStopBlocked, useStopUi } from "../state/subagents";

/**
 * The owner's Stop on one live child (SV-4; the owner's decisions of
 * 2026-10-15): one press, then "stopping…" until the CLI's own end. Only a
 * trusted press counts — a pointer click with the primary button, or Enter
 * or Space on the focused button (the VEX and turn Stop rule) — never a
 * script's click. Disabled, and inert, under an approval card. Its hover
 * help says what it does: for Codex, that a command the child is already
 * running may still finish and the orchestrator's model is not told. A
 * child the CLI cannot stop alone shows "Can't stop" with the reason.
 * Nothing is spoken or posted.
 */
export function SubagentStop({ threadId, row }: { threadId: string; row: Subagent }) {
  const ui = useStopUi(threadId, row.sid);
  const blocked = useStopBlocked();
  const noteId = useId();
  const view = stopView(row, ui.pending);
  if (view.kind === "none") return null;
  const error = ui.error ? (
    <span className="sa-stop-error" data-testid="subagent-stop-error" role="status">{ui.error}</span>
  ) : null;
  if (view.kind === "cant" || view.kind === "refused") {
    // Why there is no Stop, reachable by keyboard and screen reader too:
    // a focusable note described by its reason (and the reason as the hover).
    const cant = view.kind === "cant";
    return (
      <>
        <span className="sa-cant" data-testid={cant ? "subagent-cant-stop" : "subagent-stop-refused"}
              title={view.title} tabIndex={0} aria-describedby={noteId}>
          {cant ? "Can't stop" : "Stop refused"}
        </span>
        <span id={noteId} className="sa-sr" data-testid="subagent-stop-why">{view.title}</span>
      </>
    );
  }
  const stopping = view.kind === "stopping";
  return (
    <>
      {/* "stopping…" is aria-disabled, not disabled: a keyboard press keeps
          its focus on the button rather than dropping it to the page. */}
      <button type="button" className={"sa-stop" + (stopping ? " stopping" : "")} data-testid="subagent-stop"
              data-state={stopping ? "stopping" : view.unconfirmed ? "unconfirmed" : "ready"}
              aria-label={`Stop sub-agent ${displayName(row)}`} title={view.title}
              disabled={blocked} aria-disabled={stopping || undefined} onMouseDown={noPointerFocus}
              onClick={(e) => {
                // The owner's own press only: a pointer, or Enter/Space on it — never a script's click.
                if (!isOwnersPress({ isTrusted: e.nativeEvent.isTrusted, button: e.button })) return;
                if (stopping || subagents.isBlocked()) return;
                void subagents.stop(threadId, row.sid);
              }}>
        {stopping ? "stopping…" : view.label}
      </button>
      {stopping ? null : error}
    </>
  );
}

function Elapsed({ row, drawn }: { row: Subagent; drawn: boolean }) {
  const [now, setNow] = useState(Date.now);
  const live = isLive(row.status);
  useEffect(() => {
    if (!drawn || !live) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [drawn, live]);
  const text = elapsedOf(row, now);
  return text ? <span className="sa-time">{text}</span> : null;
}

/** One child's row, redrawn only when that child changes. */
export const SubagentRow = memo(function SubagentRow({ threadId, sid, drawn }: {
  threadId: string; sid: string; drawn: boolean;
}) {
  const row = useRow(threadId, sid);
  if (!row) return null;
  const provider = providerOf(row.sid);
  const mark = statusMark(row.status);
  const name = displayName(row);
  return (
    <div className="subagent-row" data-testid="subagent-row" data-sid={row.sid} data-status={row.status}
         data-provider={provider}>
      <button type="button" className="sa-open" aria-label={`Open sub-agent ${name} in a pane`}
              onMouseDown={noPointerFocus} onClick={() => subagents.open({ threadId, sid: row.sid })}>
        {/* Name and status stay together on the first line; the rest may wrap under them. */}
        <span className="sa-first">
          <span className="sa-mark" aria-hidden="true">◇</span>
          <span className="sa-name" data-testid="subagent-name">{name}</span>
          <span className={`sa-status ${mark.tone}`} data-testid="subagent-status"
                title={row.stop_reason || undefined}>{mark.word}</span>
        </span>
        <span className={`badge ${provider}`}>{PROVIDER_WORD[provider]}</span>
        {row.kind ? <span className="sa-kind">{row.kind}</span> : null}
        {row.last_tool ? <span className="sa-tool" data-testid="subagent-tool">{row.last_tool}</span> : null}
        <Elapsed row={row} drawn={drawn} />
        {row.tool_uses !== undefined ? <span className="sa-count">{toolsText(row.tool_uses)}</span> : null}
        {row.tokens !== undefined ? <span className="sa-count">{tokensText(row.tokens)}</span> : null}
        {row.declined ? <span className="sa-count">{row.declined} declined</span> : null}
      </button>
      <SubagentStop threadId={threadId} row={row} />
    </div>
  );
});

/** The rows at one spot: drawn as they are up to six, else one collapsed line that opens to all of them. */
export const SubagentRows = memo(function SubagentRows({ threadId, rows, drawn }: {
  threadId: string; rows: readonly Subagent[]; drawn: boolean;
}) {
  const key = `${threadId}|${rows[0]?.sid ?? ""}`;
  const open = useBlockOpen(key);
  if (!rows.length) return null;
  if (rows.length <= COLLAPSE_OVER) {
    return (
      <div className="subagent-rows" data-testid="subagent-rows">
        {rows.map((r) => <SubagentRow key={r.sid} threadId={threadId} sid={r.sid} drawn={drawn} />)}
      </div>
    );
  }
  return (
    <div className="subagent-rows subagent-block" data-testid="subagent-block">
      <BlockTitle threadId={threadId} sids={rows.map((r) => r.sid)} open={open}
                  onToggle={() => subagents.toggleBlock(key)} />
      {open ? (
        <div className="sa-list" data-testid="subagent-list">
          {rows.map((r) => <SubagentRow key={r.sid} threadId={threadId} sid={r.sid} drawn={drawn} />)}
        </div>
      ) : null}
    </div>
  );
});

function BlockTitle({ threadId, sids, open, onToggle }: {
  threadId: string; sids: string[]; open: boolean; onToggle: () => void;
}) {
  // The live count moves with every status: read from all rows, drawn here alone.
  const all = useRows(threadId);
  const rows = all.filter((r) => sids.includes(r.sid));
  return (
    <button type="button" className="sa-toggle" data-testid="subagent-block-toggle" aria-expanded={open}
            onMouseDown={noPointerFocus} onClick={onToggle}>
      <span aria-hidden="true">{open ? "▾" : "▸"}</span> {blockTitle(rows)}
    </button>
  );
}
