// Chats that message each other (chat-to-chat plan §7, C5): the rows for a
// message one chat sent another, its state and its reply, folded from the
// transcript route (`hud_api._relay_message`) and the bus records
// (`ChatRelay._record`). C3's merged code wins over the plan where they
// differ: a transcript row carries no queue `position`, `relay_delivered` is a
// sixth bus kind, rows carry the `reason` an unrun message was not delivered,
// and `relay_out` names only the target (the sender is the record's thread).
//
// Everything here is plain data and plain words. A peer's title is another
// chat's — set by a model or by whatever that chat read — so it reaches the
// window only through `peerLabel`: one line, no format characters, capped,
// and rendered by React as text. A relay row is never the owner's: its role is
// `relay`, never `user`, and nothing here opens, selects or focuses anything.

import type { ChatMessage, RelayInfo } from "../types";

/** One bus record, as `ChatRelay._record` publishes it. */
export interface RelayEvent {
  kind: string;
  thread_id?: string;
  project_id?: string;
  turn_id?: string | null;
  at?: string;
  data?: Record<string, unknown>;
  relayed?: unknown;
}

/** Exactly the bus kinds the window folds into relay rows. */
export const RELAY_KINDS: ReadonlySet<string> = new Set([
  "relay_out", "relay_in", "relay_state", "relay_progress", "relay_reply", "relay_delivered",
]);

/** The longest peer title a row shows, in code points (the "…" counted). */
export const PEER_CHARS = 60;
/** The longest reason a chip shows. */
const REASON_CHARS = 120;

/** One clean line: format characters gone, every other control character
 * and run of white space one space, trimmed, capped with "…". */
function oneLine(raw: unknown, cap: number): string {
  if (typeof raw !== "string") return "";
  const flat = raw.replace(/\p{Cf}/gu, "").replace(/[\s\p{Cc}\p{Zl}\p{Zp}]+/gu, " ").trim();
  const chars = Array.from(flat);
  return chars.length <= cap ? flat : chars.slice(0, cap - 1).join("").trimEnd() + "…";
}

/** Another chat's title as one plain line of at most `PEER_CHARS`. */
export function peerLabel(title: unknown): string {
  return oneLine(title, PEER_CHARS) || "(untitled)";
}

const PROVIDER_WORDS: Record<string, string> = { fast: "Jarvis", claude: "Claude", codex: "Codex" };

/** The provider in words: Jarvis, Claude, Codex, or "chat". */
export function providerWord(p: unknown): string {
  return typeof p === "string" && Object.hasOwn(PROVIDER_WORDS, p) ? PROVIDER_WORDS[p] : "chat";
}

/** The only words a provider badge's class may take. */
export function providerClass(p: unknown): "fast" | "claude" | "codex" | "unknown" {
  return p === "fast" || p === "claude" || p === "codex" ? p : "unknown";
}

/** 1st 2nd 3rd 4th … 11th 12th 13th … 21st. */
export function ordinal(n: number): string {
  const teen = Math.abs(n) % 100;
  const unit = Math.abs(n) % 10;
  const suffix = teen >= 11 && teen <= 13 ? "th" : unit === 1 ? "st" : unit === 2 ? "nd" : unit === 3 ? "rd" : "th";
  return `${n}${suffix}`;
}

/** A state chip's words. */
export function stateWords(r: RelayInfo): string {
  switch (r.state) {
    case "queued":
      return typeof r.position === "number" && Number.isInteger(r.position) && r.position > 0
        ? `queued · ${ordinal(r.position)}` : "queued";
    case "running": return "working";
    case "answered": return "answered";
    case "failed": return "failed";
    case "stopped": return "stopped";
    default: {
      // withdrawn, dropped, expired, "not delivered", and anything unknown.
      const reason = oneLine(r.reason, REASON_CHARS);
      return reason ? `not delivered · ${reason}` : "not delivered";
    }
  }
}

export type RelayTone = "pending" | "working" | "done" | "muted" | "error";

/** Graphite: amber only for pending, red only for an error. */
export function stateTone(state: string): RelayTone {
  return state === "queued" ? "pending" : state === "running" ? "working" : state === "answered" ? "done"
    : state === "failed" ? "error" : "muted";
}

/** How a reply reached the chat that asked, in words; null when not known. */
export function deliveryWords(delivery: unknown, unconfirmed?: boolean): string | null {
  switch (delivery) {
    case "wait": return "Jarvis read it while waiting";
    case "step": return unconfirmed ? "passed to Jarvis at its next step (may not have reached it)"
      : "passed to Jarvis at its next step";
    case "turn": return "started a turn to report it";
    // `shown`: the owner stopped the turn that asked, or the reply was no
    // answer and nothing was running to take it (§5.3 d) — on screen only.
    case "shown": return "not passed on: shown here only";
    case "dropped": return "not passed on: this chat could not take it";
    default: return null;
  }
}

/** ChatTab's queued indicator. */
export function waitingLine(n: number, from?: string | null): string {
  if (n === 1) {
    const who = oneLine(from, PEER_CHARS);
    return `1 message from ${who || "another chat"} waits for this turn`;
  }
  return `${n} messages from other chats wait for this turn`;
}

/** The sidebar badge's title. */
export function waitingTitle(n: number): string {
  return n === 1 ? "1 message from another chat waits for this chat"
    : `${n} messages from other chats wait for this chat`;
}

// ---------------------------------------------------------------------------

const DIRS = new Set(["in", "out", "reply"]);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const peerOf = (id: unknown, title: unknown, provider: unknown): RelayInfo["peer"] =>
  ({ thread_id: str(id), title: str(title), provider: str(provider) });

/** One transcript row (`{role: "relay", relay, text, at, turn_id?}`) checked
 * and copied; null for anything that is not a relay row. */
export function relayMessage(raw: unknown): ChatMessage | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  if (row.role !== "relay") return null;
  const r = row.relay as Record<string, unknown> | undefined;
  if (!r || typeof r !== "object") return null;
  if (typeof r.dir !== "string" || !DIRS.has(r.dir)) return null;
  if (typeof r.message_id !== "string" || !r.message_id) return null;
  const peer = (r.peer && typeof r.peer === "object" ? r.peer : {}) as Record<string, unknown>;
  const relay: RelayInfo = {
    dir: r.dir as RelayInfo["dir"],
    message_id: r.message_id,
    peer: peerOf(peer.thread_id, peer.title, peer.provider),
    state: typeof r.state === "string" && r.state ? r.state : "not delivered",
    status: str(r.status),
    delivery: str(r.delivery),
  };
  if (typeof r.reason === "string") relay.reason = r.reason;
  if (typeof r.position === "number" && Number.isFinite(r.position)) relay.position = r.position;
  if (typeof r.progress === "string") relay.progress = r.progress;
  if (r.unconfirmed === true) relay.unconfirmed = true;
  const message: ChatMessage = { role: "relay", text: str(row.text) ?? "", relay };
  if (typeof row.at === "string") message.at = row.at;
  if (typeof row.turn_id === "string") message.turn_id = row.turn_id;
  return message;
}

const REPLY_STATE: Record<string, string> = { answered: "answered", empty: "answered", stopped: "stopped", failed: "failed" };

function has(messages: ChatMessage[], dir: RelayInfo["dir"], mid: string): boolean {
  return messages.some((m) => m.role === "relay" && m.relay?.dir === dir && m.relay.message_id === mid);
}

/** `fn` on every relay row with this id (and direction, if given); the same
 * array back when no row changed. */
function patch(messages: ChatMessage[], mid: string, dir: RelayInfo["dir"] | null,
  fn: (r: RelayInfo) => RelayInfo): ChatMessage[] {
  let changed = false;
  const next = messages.map((m) => {
    if (m.role !== "relay" || !m.relay || m.relay.message_id !== mid || (dir && m.relay.dir !== dir)) return m;
    const r = fn(m.relay);
    if (r === m.relay) return m;
    const keys = new Set([...Object.keys(r), ...Object.keys(m.relay)]) as Set<keyof RelayInfo>;
    if ([...keys].every((k) => r[k] === m.relay![k])) return m;
    changed = true;
    return { ...m, relay: r };
  });
  return changed ? next : messages;
}

function appended(e: RelayEvent, relay: RelayInfo, text: unknown): ChatMessage {
  const message: ChatMessage = { role: "relay", text: str(text) ?? "", relay };
  if (typeof e.at === "string") message.at = e.at;
  if (typeof e.turn_id === "string") message.turn_id = e.turn_id;
  return message;
}

/** One bus record folded into one pane's transcript of `e.thread_id`, by
 * message id (never by text); the same array back when nothing changes. */
export function applyRelay(messages: ChatMessage[], e: RelayEvent): ChatMessage[] {
  if (!RELAY_KINDS.has(e.kind)) return messages;
  const data = (e.data && typeof e.data === "object" ? e.data : {}) as Record<string, unknown>;
  const mid = data.message_id;
  if (typeof mid !== "string" || !mid) return messages;
  switch (e.kind) {
    case "relay_out":
      if (has(messages, "out", mid)) return messages;
      return [...messages, appended(e, { dir: "out", message_id: mid, peer: peerOf(data.to, data.title, data.provider),
        state: "queued", status: null, delivery: null }, data.text)];
    case "relay_in":
      if (has(messages, "in", mid)) return messages;
      return [...messages, appended(e, { dir: "in", message_id: mid, peer: peerOf(data.from, data.title, data.provider),
        state: "running", status: null, delivery: null }, data.text)];
    case "relay_state": {
      if (typeof data.state !== "string" || !data.state) return messages;
      const state = data.state;
      const reason = str(data.reason);
      return patch(messages, mid, null, (r) => ({ ...r, state, ...(reason !== null ? { reason } : {}) }));
    }
    case "relay_progress": {
      const text = str(data.text);
      if (text === null) return messages;
      return patch(messages, mid, "out", (r) => ({ ...r, progress: text, state: r.state === "queued" ? "running" : r.state }));
    }
    case "relay_reply": {
      const status = str(data.status);
      const state = status !== null && Object.hasOwn(REPLY_STATE, status) ? REPLY_STATE[status] : null;
      const out = patch(messages, mid, "out", (r) => {
        const { progress: _p, ...rest } = r;
        return { ...rest, status, ...(state ? { state } : {}) };
      });
      if (has(out, "reply", mid)) return out;
      return [...out, appended({ ...e, turn_id: null }, { dir: "reply", message_id: mid,
        peer: peerOf(data.from, data.title, data.provider), state: state ?? "answered", status, delivery: null },
      data.text)];
    }
    case "relay_delivered": {
      const delivery = str(data.delivery);
      if (delivery === null) return messages;
      const unconfirmed = data.unconfirmed === true;
      return patch(messages, mid, null, (r) => {
        const { unconfirmed: _u, ...rest } = r;
        return unconfirmed ? { ...rest, delivery, unconfirmed: true } : { ...rest, delivery };
      });
    }
  }
  return messages;
}
