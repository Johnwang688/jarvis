import { memo, useCallback, useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ChatMessage, ToolCall } from "../types";
import { groupTitle, toolLabel } from "../lib/toolCalls";
import { elapsed, toolName } from "../lib/thinking";
import { scmWindow } from "../lib/scm";
import { noPointerFocus } from "../lib/talkkey";
import { toolGroups } from "../state/toolGroups";

const ROW = 22, VISIBLE = 16, OVER = 4;
function Duration({ call, drawn }: { call: ToolCall; drawn: boolean }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!drawn || call.outcome !== "running") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [drawn, call.outcome]);
  const start = Date.parse(call.at), end = Date.parse(call.finished_at ?? "");
  return <span className="tc-time">{Number.isFinite(start) ? elapsed((Number.isFinite(end) ? end : now) - start) : ""}</span>;
}
const CallRow = memo(function CallRow({ call, drawn }: { call: ToolCall; drawn: boolean }) {
  const label = toolLabel(call.label);
  return <li data-testid="tool-call" data-status={call.outcome} className="tool-call">
    <span className="tc-name">{toolName(call.name)}</span>
    <span className="tc-label" title={label}>{label}</span>
    <span className={`tc-status ${call.outcome}`}>{call.outcome}</span>
    <Duration call={call} drawn={drawn} />
  </li>;
});

export const ToolGroupRow = memo(function ToolGroupRow({ group, threadId, using, drawn }: {
  group: ChatMessage; threadId: string; using: boolean; drawn: boolean;
}) {
  const calls = group.calls!;
  const key = toolGroups.key(threadId, calls[0].call_id, group.turn_id);
  const subscribe = useCallback((fn: () => void) => toolGroups.subscribe(key, fn), [key]);
  const snapshot = useCallback(() => toolGroups.get(key), [key]);
  const open = useSyncExternalStore(subscribe, snapshot);
  const id = useId();
  const row = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const keptScroll = useRef<number | null>(null);
  const [top, setTop] = useState(0);
  const windowed = calls.length > 40;
  const { first, last } = windowed ? scmWindow(calls.length, top, (VISIBLE - 1) * ROW, ROW, OVER) : { first: 0, last: calls.length };
  const title = groupTitle(group, using);
  const toggle = () => {
    const log = row.current?.closest<HTMLElement>("[data-testid=log]");
    keptScroll.current = log?.scrollTop ?? null;
    toolGroups.toggle(key);
  };
  useLayoutEffect(() => {
    const log = row.current?.closest<HTMLElement>("[data-testid=log]");
    if (log && keptScroll.current !== null) log.scrollTop = keptScroll.current;
    keptScroll.current = null;
    if (open && list.current) list.current.scrollTop = top;
  }, [open]);
  return <div className="tool-group" ref={row}>
    <button type="button" data-testid="tool-group" aria-expanded={open} aria-controls={id}
      onMouseDown={noPointerFocus} onClick={toggle}>
      <span aria-hidden="true">{open ? "▾" : "▸"}</span> {title.text}
      {title.failed ? <span className="tc-failed"> · {title.failed} failed</span> : null}
      {title.denied ? <span> · {title.denied} denied</span> : null}
      {title.stopped ? <span> · {title.stopped} stopped</span> : null}
    </button>
    <ul id={id} ref={list} hidden={!open} className={"tool-calls" + (windowed ? " windowed" : "")}
      tabIndex={open && windowed ? 0 : undefined} aria-label="Tool calls"
      onScroll={e => { if (e.target === e.currentTarget) setTop(e.currentTarget.scrollTop); }}
      onKeyDown={e => {
        if (!windowed || (e.key !== "End" && e.key !== "Home")) return;
        e.preventDefault();
        const next = e.key === "End" ? Math.max(0, calls.length * ROW - VISIBLE * ROW) : 0;
        e.currentTarget.scrollTop = next; setTop(next);
      }}>
      {open ? <>
        {first ? <li aria-hidden="true" style={{ height: first * ROW }} /> : null}
        {calls.slice(first, last).map(call => <CallRow key={`${call.turn_id}|${call.call_id}`} call={call} drawn={drawn} />)}
        {last < calls.length ? <li aria-hidden="true" style={{ height: (calls.length - last) * ROW }} /> : null}
      </> : null}
    </ul>
  </div>;
});
