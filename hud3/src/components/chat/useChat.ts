import { useEffect, useRef, useState } from "react";
import {
  actionError, readChat, readEvents, writeChat,
  type ChatAction, type ChatAuto, type ChatMessage, type ChatSnapshot, type ChatState, type ChatTool,
} from "./chatApi";

const ACTIVE_MS = 700;
const IDLE_MS = 5_000;
const TIMEOUT_MS = 10_000;

export const isActive = (state: ChatState) => state === "running" || state === "stopping";

type MessageRow = ChatMessage & { kind: "message"; steerSeq?: number };
type ToolRow = ChatTool & { kind: "tool"; resultUnavailable?: boolean };
export type ChatRow = MessageRow | ToolRow | ChatAuto;
type ChatView = Omit<ChatSnapshot, "history" | "tools" | "replay"> & {
  rows: ChatRow[];
  notice?: "stopped" | "error";
};
type EventBatch = Awaited<ReturnType<typeof readEvents>>;

const pendingTool = (tool: ToolRow) => tool.ok === undefined && !tool.resultUnavailable;

function closeTools(rows: ChatRow[]): ChatRow[] {
  return rows.map((row) => row.kind === "tool" && pendingTool(row) ? { ...row, resultUnavailable: true } : row);
}

function limitHistory(rows: ChatRow[]): ChatRow[] {
  let messages = 0;
  for (let index = rows.length - 1; index >= 0; index--) {
    if (rows[index].kind === "message" && ++messages === 50) return rows.slice(index);
  }
  return rows;
}

function applyEvents(current: ChatView, batch: EventBatch): ChatView {
  const next = { ...current, state: batch.state, seq: batch.seq, approvals: batch.approvals,
    outside_text: batch.outside_text ?? current.outside_text };
  let rows = [...current.rows];
  let after = current.seq;
  for (const event of batch.events) {
    if (event.seq <= after) continue;
    after = event.seq;
    switch (event.kind) {
      case "user": {
        rows = closeTools(rows);
        next.draft = "";
        next.notice = undefined;
        if (event.inputs) {
          // Carry the exact queued rows forward, including repeated text.
          const carried = new Set(event.inputs.map(({ seq }) => seq));
          rows = rows.filter((row) => row.kind !== "message" || row.steerSeq === undefined || !carried.has(row.steerSeq));
          rows.push(...event.inputs.map(({ seq, text }): MessageRow =>
            ({ kind: "message", role: "user", text, t: event.t, steerSeq: seq })));
        } else {
          rows.push({ kind: "message", role: "user", text: event.text, t: event.t });
        }
        break;
      }
      case "steer":
        rows.push({ kind: "message", role: "user", text: event.text, t: event.t,
          steered: !event.queued, queued: event.queued, steerSeq: event.seq });
        break;
      case "reply":
        next.draft = "";
        rows = closeTools(rows);
        rows.push({ kind: "message", role: "assistant", text: event.text, t: event.t });
        break;
      case "tool_start":
        rows.push({ kind: "tool", name: event.name });
        break;
      case "tool_end": {
        const index = rows.findIndex((row) => row.kind === "tool" && pendingTool(row) && row.name === event.name);
        const finished: ToolRow = { kind: "tool", name: event.name, ok: event.ok, summary: event.summary };
        if (index === -1) rows.push(finished);
        else rows[index] = finished;
        break;
      }
      case "auto": {
        const index = rows.map((row) => row.kind === "tool" ? row.name : undefined).lastIndexOf(event.tool);
        if (index === -1) rows.push(event);
        else rows[index] = event;
        break;
      }
      case "error":
      case "stopped": {
        next.notice = event.kind;
        next.draft = "";
        rows = closeTools(rows);
        const discarded = new Set(event.discarded);
        rows = rows.map((row) => row.kind === "message" && row.steerSeq !== undefined && discarded.has(row.steerSeq)
          ? { ...row, steered: false, queued: false, discarded: true } : row);
        break;
      }
    }
  }
  if (!isActive(next.state)) {
    next.draft = "";
    rows = closeTools(rows);
  }
  return { ...next, rows: limitHistory(rows) };
}

function reconcileSnapshot({ history, tools, replay, ...snapshot }: ChatSnapshot, current?: ChatView): ChatView {
  if (replay) history = history.slice(0, replay.history_prefix);
  const rows: MessageRow[] = history.map((message) => ({ ...message, kind: "message" }));
  const active = isActive(snapshot.state);
  const snapshotTools = active ? [...tools] : tools.filter((tool) => tool.ok !== undefined);
  // Snapshot tools have no event position. Place them in the latest turn, before its reply.
  const toolsBefore = !active && rows.at(-1)?.role === "assistant" ? rows.length - 1 : rows.length;
  const knownTools = new Map<number, ToolRow[]>();
  if (current && current.session_id === snapshot.session_id && snapshot.seq >= current.seq) {
    let anchor = -1;
    let searchFrom = 0;
    let recentAnchor = false;
    const lastReply = history.slice(0, toolsBefore).map((message) => message.role).lastIndexOf("assistant");
    const turnStart = current.rows.reduce((start, row, index) =>
      row.kind === "message" && row.role === "user" && !row.steered && !row.queued ? index : start, -1);
    for (const [index, row] of current.rows.entries()) {
      if (row.kind === "message") {
        // Snapshot history is authoritative. Match timestamped messages in order
        // so a repeated prompt or a different session cannot inherit a steer label.
        anchor = history.findIndex((message, index) => index >= searchFrom && row.t !== undefined
          && row.t === message.t && row.role === message.role && row.text === message.text);
        if (anchor !== -1) {
          searchFrom = anchor + 1;
          rows[anchor].steered = row.steered;
        }
        recentAnchor = index >= turnStart && anchor > lastReply && anchor < toolsBefore;
      } else if (row.kind === "tool" && anchor !== -1) {
        let tool = pendingTool(row) ? { ...row, resultUnavailable: true } : row;
        if (recentAnchor && !row.resultUnavailable) {
          const index = snapshotTools.findIndex((entry) => entry.name === row.name
            && (pendingTool(row) || (entry.ok === row.ok && entry.summary === row.summary)));
          if (index !== -1) {
            const [updated] = snapshotTools.splice(index, 1);
            tool = { ...updated, kind: "tool" };
          }
        }
        // Keep each known tool directly after the same message.
        const group = knownTools.get(anchor);
        if (group) group.push(tool);
        else knownTools.set(anchor, [tool]);
      }
    }
  }
  const saved = new Map<number, ChatAuto[]>();
  for (const event of replay?.saved ?? []) {
    const group = saved.get(event.after);
    if (group) group.push(event);
    else saved.set(event.after, [event]);
  }
  const atMessage = (row: MessageRow, index: number): ChatRow[] =>
    [row, ...(knownTools.get(index) ?? []), ...(saved.get(index) ?? [])];
  const transcript = rows.slice(0, toolsBefore).flatMap(atMessage);
  transcript.push(...snapshotTools.map((tool): ToolRow => ({ ...tool, kind: "tool" })));
  transcript.push(...rows.slice(toolsBefore).flatMap((row, index) => atMessage(row, index + toolsBefore)));
  const view = { ...snapshot, draft: active ? snapshot.draft : "", rows: transcript };
  if (!replay?.events.length) return view;
  const replayed = applyEvents({ ...view, seq: replay.events[0].seq - 1 }, {
    state: view.state, seq: view.seq, gap: false, events: replay.events, approvals: view.approvals,
  });
  return { ...replayed, draft: view.draft };
}

export function useChat() {
  const [chat, setChat] = useState<ChatView>();
  const [pollError, setPollError] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean }>();
  const [pending, setPending] = useState<ChatAction["route"]>();
  const submit = useRef<(action: ChatAction) => Promise<boolean>>();

  useEffect(() => {
    let current: ChatView | undefined;
    let pollRequest: AbortController | undefined;
    let actionRequest: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let backoff = 0;
    let retryAt = 0;
    let disposed = false;
    let needsSnapshot = true;
    let readStopped = false;

    function publish(next: ChatView) {
      current = next;
      setChat(next);
    }

    function adoptSnapshot(snapshot: ChatSnapshot) {
      if (current && current.seq === snapshot.seq && current.session_id === snapshot.session_id) {
        // A matching cursor lets us keep event-only labels and finished tools.
        publish({
          ...current,
          session_id: snapshot.session_id,
          state: snapshot.state,
          draft: isActive(snapshot.state) ? snapshot.draft : "",
          approvals: snapshot.approvals,
          outside_text: snapshot.outside_text,
        });
      } else {
        if (current && current.session_id !== snapshot.session_id) setFeedback(undefined);
        publish(reconcileSnapshot(snapshot, current));
      }
      needsSnapshot = false;
    }

    function interval() {
      return needsSnapshot || readStopped || (current && isActive(current.state)) ? ACTIVE_MS : IDLE_MS;
    }

    function schedule(delay: number) {
      clearTimeout(timer);
      if (!disposed && !document.hidden && !pollRequest && !actionRequest) {
        timer = setTimeout(poll, Math.max(delay, retryAt - Date.now()));
      }
    }

    async function poll() {
      if (disposed || document.hidden || pollRequest || actionRequest) return;
      const controller = new AbortController();
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(TIMEOUT_MS)]);
      pollRequest = controller;
      const canPublish = () => !controller.signal.aborted && !signal.aborted;
      try {
        if (!needsSnapshot && current && (readStopped || isActive(current.state))) {
          const batch = await readEvents(current.seq, signal);
          if (!canPublish()) return;
          readStopped = false;
          if (batch.gap || batch.seq < current.seq
            || (batch.session_id !== undefined && batch.session_id !== current.session_id)) {
            needsSnapshot = true;
            const snapshot = await readChat(signal);
            if (canPublish()) adoptSnapshot(snapshot);
          } else {
            publish(applyEvents(current, batch));
            if (!isActive(batch.state) || batch.events.some((event) => event.kind === "reply")) {
              setFeedback((previous) => previous?.error ? previous : undefined);
            }
            if (isActive(batch.state)) {
              const snapshot = await readChat(signal);
              if (!canPublish()) return;
              if (snapshot.seq === current.seq
                || snapshot.session_id !== current.session_id) {
                adoptSnapshot(snapshot);
              } else if (snapshot.approvals !== current.approvals) {
                // A newer snapshot can already include a reply. Let events catch up.
                publish({ ...current, approvals: snapshot.approvals });
              }
            }
          }
        } else {
          const snapshot = await readChat(signal);
          if (canPublish()) adoptSnapshot(snapshot);
        }
        if (canPublish()) {
          backoff = 0;
          retryAt = 0;
          setPollError(false);
        }
      } catch {
        if (!controller.signal.aborted) {
          backoff = Math.min(backoff ? backoff * 2 : IDLE_MS, 30_000);
          retryAt = Date.now() + backoff;
          setPollError(true);
        }
      } finally {
        pollRequest = undefined;
        schedule(interval());
      }
    }

    submit.current = async (action) => {
      if (!current || disposed || actionRequest) return false;
      if (action.route === "new" && current.state !== "idle") return false;
      if (action.route === "stop" && current.state !== "running") return false;
      if (action.route === "send" && current.state === "stopping") return false;
      clearTimeout(timer);
      pollRequest?.abort();
      const controller = new AbortController();
      actionRequest = controller;
      setPending(action.route);
      setFeedback(undefined);

      try {
        const result = await writeChat(action, AbortSignal.any([controller.signal, AbortSignal.timeout(TIMEOUT_MS)]));
        if (disposed) return false;
        if ("sessionId" in result) {
          publish({ session_id: result.sessionId, state: "idle", seq: current.seq, draft: "", rows: [], approvals: 0,
            outside_text: false });
          needsSnapshot = true;
        } else {
          publish({ ...current, state: result.state, notice: undefined,
            draft: isActive(result.state) ? current.draft : "",
            approvals: action.route === "stop" ? 0 : current.approvals });
          if (result.steered) setFeedback({ text: "Message added to this turn." });
          if (action.route === "stop") {
            // Read the terminal event even if Stop has already returned idle.
            readStopped = true;
          }
        }
        backoff = 0;
        retryAt = 0;
        return true;
      } catch (failure) {
        if (!disposed) setFeedback({ text: actionError(action, failure), error: true });
        return false;
      } finally {
        actionRequest = undefined;
        if (!disposed) setPending(undefined);
        schedule(needsSnapshot ? 0 : interval());
      }
    };

    function onVisibilityChange() {
      if (document.hidden) {
        clearTimeout(timer);
        pollRequest?.abort();
      } else {
        schedule(0);
      }
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    if (!document.hidden) void poll();
    return () => {
      disposed = true;
      submit.current = undefined;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearTimeout(timer);
      pollRequest?.abort();
      actionRequest?.abort();
    };
  }, []);

  return { chat, pollError, feedback, pending, submit: (action: ChatAction) => submit.current?.(action) };
}
