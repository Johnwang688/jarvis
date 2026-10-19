// One row for a message one chat sent another (chat-to-chat plan §7, C5):
// the receiving side ("FROM <badge> · ‹title›"), the sending side ("↪ SENT TO
// ‹title›" and its state chip) and the reply ("↩ REPLY FROM <badge> ·
// ‹title›").
//
// Never the owner's: no "YOU", never the `user` class. The peer's title is
// another chat's, so it is shown only through `peerLabel` (one clean line) as
// React text; a received or sent message is plain text, and only a reply —
// another chat's answer, like Jarvis's own — goes through the sanitising
// `Markdown`. Nothing here takes focus on mount; "Open" and "Withdraw" act on
// the owner's click only, and are dead while an approval card is up.

import { memo, useState } from "react";
import type { ChatMessage } from "../types";
import { Markdown } from "./Markdown";
import {
  deliveryWords, peerLabel, providerClass, providerWord, stateTone, stateWords,
} from "../lib/relays";

/** A sent message longer than this, or than three lines, starts collapsed. */
const COLLAPSE_CHARS = 240;
const COLLAPSE_LINES = 3;

function Badge({ provider }: { provider: string | null }) {
  return <span className={`badge ${providerClass(provider)}`}>{providerWord(provider)}</span>;
}

export const RelayRow = memo(function RelayRow({ msg, blocked = false, onOpen, onWithdraw }: {
  msg: ChatMessage;
  /** An approval card is up: every button is disabled, and a click does nothing. */
  blocked?: boolean;
  /** "Open": the peer chat beside this one (the owner's click only). */
  onOpen?: (threadId: string) => void;
  /** The owner's Withdraw on a queued send. */
  onWithdraw?: (messageId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const r = msg.relay;
  if (!r) return null;
  const title = peerLabel(r.peer.title);
  const common = { "data-message-id": r.message_id, "data-state": r.state };

  if (r.dir === "in") {
    const peer = r.peer.thread_id;
    return (
      <div className="msg relay relay-in" data-testid="msg-relay-in" {...common}>
        <div className="who">FROM <Badge provider={r.peer.provider} /> · {title}</div>
        <div className="body user-text">{msg.text}</div>
        <div className="relay-foot">
          <button
            type="button"
            className="relay-open"
            data-testid="relay-open"
            disabled={blocked || !peer}
            title="Open that chat beside this one"
            onClick={() => { if (!blocked && peer && onOpen) onOpen(peer); }}
          >
            Open
          </button>
          {r.state === "answered" ? (
            <div className="relay-answered" data-testid="relay-answered">↩ reply sent to {title}</div>
          ) : null}
        </div>
      </div>
    );
  }

  if (r.dir === "out") {
    const long = msg.text.length > COLLAPSE_CHARS || msg.text.split("\n").length > COLLAPSE_LINES;
    return (
      <div className="msg relay relay-out" data-testid="msg-relay-out" {...common}>
        <div className="who">↪ SENT TO {title} <span className={`relay-state ${stateTone(r.state)}`} data-testid="relay-state">{stateWords(r)}</span></div>
        <div className={`body user-text${long && !open ? " collapsed" : ""}`}>{msg.text}</div>
        {long ? (
          <button
            type="button"
            className="relay-expand linkish"
            data-testid="relay-expand"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "less" : "more"}
          </button>
        ) : null}
        {r.state === "running" && r.progress ? (
          <div className="relay-progress" data-testid="relay-progress">{r.progress}</div>
        ) : null}
        {r.state === "queued" ? (
          <div className="relay-foot">
            <button
              type="button"
              className="relay-withdraw"
              data-testid="relay-withdraw"
              disabled={blocked}
              title="Withdraw this message before it starts"
              onClick={() => { if (!blocked && onWithdraw) onWithdraw(r.message_id); }}
            >
              Withdraw
            </button>
          </div>
        ) : null}
      </div>
    );
  }

  const delivered = deliveryWords(r.delivery, r.unconfirmed);
  return (
    <div className="msg relay relay-reply" data-testid="msg-relay-reply" {...common}>
      <div className="who">↩ REPLY FROM <Badge provider={r.peer.provider} /> · {title}</div>
      <Markdown text={msg.text} />
      {delivered ? <div className="relay-delivery" data-testid="relay-delivery">{delivered}</div> : null}
    </div>
  );
});
