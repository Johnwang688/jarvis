import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { surface } from "../../api";
import { ApprovalPanel } from "../approvals/ApprovalPanel";
import { isActive, useChat, type ChatRow } from "./useChat";
import type { ChatMessage } from "./chatApi";

const TEXT_LIMIT = 16_000;
const HINT_AT = 15_000;
// How long after the owner's input a scroll still counts as theirs.
const OWNER_SCROLL_MS = 400;

function Message({ message, draft = false }: { message: ChatMessage; draft?: boolean }) {
  return (
    <article className="chat-message" data-role={message.role} data-draft={draft || undefined}>
      <div className="chat-byline">
        {message.role === "user" ? "You" : "Jarvis"}
        {message.steered && <span> · Added to this turn</span>}
        {message.queued && <span> · Queued for the next turn</span>}
        {message.discarded && <span> · Discarded before delivery</span>}
        {draft && <span> · Draft</span>}
      </div>
      <p className="chat-text" dir="auto">{message.text}</p>
    </article>
  );
}

function Transcript({ rows }: { rows: ChatRow[] }) {
  type ToolRow = Exclude<ChatRow, { kind: "message" }>;
  const groups: (Extract<ChatRow, { kind: "message" }> | ToolRow[])[] = [];
  for (const row of rows) {
    const last = groups.at(-1);
    if (row.kind === "message") groups.push(row);
    else if (Array.isArray(last)) last.push(row);
    else groups.push([row]);
  }
  return groups.map((group, index) => Array.isArray(group) ? (
    <ul key={index} className="chat-tools" aria-label="Tools">
      {group.map((tool, toolIndex) => tool.kind === "auto" ? (
        <li key={toolIndex} className="chat-auto">
          <details>
            <summary>Ran without asking · <bdi>{tool.title}</bdi> · <bdi>{tool.target}</bdi></summary>
            <div>Policy level {tool.level} · <bdi>{tool.why}</bdi></div>
          </details>
        </li>
      ) : (
        <li key={toolIndex}>
          <bdi>{tool.name}</bdi>
          <span className={tool.ok === false ? "chat-tool-failed" : undefined}>
            {" · "}{tool.resultUnavailable ? "Result unavailable" : tool.ok === undefined ? "Running" : tool.ok ? "OK" : "Failed"}
          </span>
          {tool.summary && <>{" · "}<bdi>{tool.summary}</bdi></>}
        </li>
      ))}
    </ul>
  ) : <Message key={index} message={group} />);
}

export function ChatPanel({ onApprovalsChange }: { onApprovalsChange?: (count: number) => void } = {}) {
  const { chat, pollError, feedback, pending, submit } = useChat();
  const [text, setText] = useState("");
  const composing = useRef(false);
  const log = useRef<HTMLDivElement>(null);
  const follows = useRef(true);
  const box = useRef("");
  const touched = useRef(-Infinity);
  const touch = () => { touched.current = performance.now(); };
  const session = useRef<string | null>();
  const count = Array.from(text).length;
  const showHint = count >= HINT_AT;
  const active = chat && isActive(chat.state);
  const canSend = chat && chat.state !== "stopping" && !pending && !!text.trim() && count <= TEXT_LIMIT;
  const approvalCount = chat?.approvals ?? 0;

  useEffect(() => {
    onApprovalsChange?.(approvalCount);
  }, [approvalCount, onApprovalsChange]);

  useLayoutEffect(() => {
    if (session.current !== chat?.session_id) {
      session.current = chat?.session_id;
      follows.current = true;
    }
    if (log.current && follows.current) log.current.scrollTop = log.current.scrollHeight;
  }, [chat]);

  // The transcript's height follows the window and whatever shares its column
  // (an approval card, a taller composer). While it follows the end, a change
  // of height keeps the newest message in view.
  const shown = !!chat;
  useEffect(() => {
    const el = log.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      box.current = `${el.clientWidth}x${el.clientHeight}`;
      if (follows.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [shown]);

  async function send() {
    if (!canSend || composing.current) return;
    if (await submit({ route: "send", text })) {
      setText((current) => current === text ? "" : current);
    }
  }

  return (
    <section className="agent-panel agent-chat" data-panel="chat" aria-labelledby="panel-chat">
      <header className="chat-header">
        <h2 id="panel-chat">Chat</h2>
        {chat?.outside_text && <span className="chat-outside">Outside text read: sensitive actions ask</span>}
        {chat && (
          <button type="button" disabled={chat.state !== "idle" || !!pending}
            onClick={() => void submit({ route: "new" })}>New chat</button>
        )}
      </header>
      {pollError && <p className="chat-error" role="status">Chat unavailable. Retrying…</p>}
      {!chat && !pollError && <p role="status">Loading chat…</p>}
      {chat && (
        <>
          <div className="chat-log" ref={log} aria-label="Chat transcript" role="region"
            onWheel={touch} onTouchMove={touch} onPointerDown={touch} onKeyDown={touch} onScroll={(event) => {
            const el = event.currentTarget;
            // A scroll that comes with a change of size is the layout moving
            // the text, not the owner reading back: it does not stop following,
            // unless the owner's own wheel, touch or key came just before it.
            if (box.current && box.current !== `${el.clientWidth}x${el.clientHeight}`
              && performance.now() - touched.current > OWNER_SCROLL_MS) return;
            follows.current = el.scrollHeight - el.clientHeight - el.scrollTop <= 24;
          }}>
            {!chat.rows.length && !(active && chat.draft) && <p className="chat-empty">No messages yet.</p>}
            <Transcript rows={chat.rows} />
            {active && chat.draft && <Message message={{ role: "assistant", text: chat.draft }} draft />}
          </div>
          {active && (
            <div className="chat-working">
              <p className="agent-running" role="status">
                {chat.state === "stopping" ? "Jarvis is stopping…" : "Jarvis is working…"}
              </p>
              <button type="button" disabled={chat.state !== "running" || !!pending}
                onClick={() => void submit({ route: "stop" })}>Stop</button>
            </div>
          )}
          {(chat.state === "error" || chat.notice === "error") &&
            <p className="chat-error" role="status">The turn failed. Try sending a message again.</p>}
          {chat.notice === "stopped" && <p role="status">Turn stopped.</p>}
          <ApprovalPanel key={chat.session_id} sessionId={chat.session_id}
            count={chat.approvals} stopping={pending === "stop"} />
          <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <textarea aria-label="Message" aria-describedby={showHint ? "chat-length" : undefined}
              aria-invalid={count > TEXT_LIMIT || undefined} dir="auto" rows={3}
              placeholder={chat.state !== "running" ? "Message Jarvis"
                : surface() === "phone" ? "Queued for the next turn" : "Add to this turn"}
              value={text} onChange={(event) => setText(event.currentTarget.value)}
              onCompositionStart={() => { composing.current = true; }}
              onCompositionEnd={() => { composing.current = false; }}
              onKeyDown={(event) => {
                if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey
                  || composing.current
                  || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
                event.preventDefault();
                if (!event.repeat) void send();
              }} />
            <div className="chat-composer-actions">
              {showHint && <span id="chat-length">{count.toLocaleString("en-US")} / 16,000 characters</span>}
              <button type="submit" className="chat-send" disabled={!canSend}>Send</button>
            </div>
          </form>
          {feedback && <p className={feedback.error ? "chat-error" : undefined} role="status">{feedback.text}</p>}
        </>
      )}
    </section>
  );
}
