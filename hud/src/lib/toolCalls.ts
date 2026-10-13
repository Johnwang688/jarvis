import type { ChatMessage, ToolCall, ToolEntry, ToolOp, ToolOutcome } from "../types";
import { toolName } from "./thinking";

/** Safe display text only; never derive labels from raw args or results in the window. */
export function toolLabel(raw: unknown): string {
  const flat = String(raw ?? "").slice(0, 4096).replace(/[\s\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Co}\p{Cn}]+/gu, " ").trim();
  const chars = Array.from(flat);
  return chars.length <= 120 ? flat : chars.slice(0, 119).join("").trimEnd() + "…";
}
export function outcome(value: unknown, ok = true): ToolOutcome {
  return value === "running" || value === "ok" || value === "failed" || value === "denied" || value === "stopped"
    ? value : ok ? "ok" : "failed";
}
export function cleanCall(raw: ToolCall): ToolCall {
  return { call_id: String(raw.call_id), turn_id: String(raw.turn_id ?? ""), name: toolName(raw.name),
    label: toolLabel(raw.label), outcome: outcome(raw.outcome), at: raw.at,
    ...(raw.finished_at ? { finished_at: raw.finished_at } : {}) };
}
export type TranscriptEvent =
  | { kind: "message"; message: ChatMessage }
  | { kind: "start"; call: ToolCall }
  | { kind: "finish"; call_id: string; turn_id: string; outcome: ToolOutcome; at: string }
  | { kind: "stop"; turn_id?: string; at: string }
  | { kind: "mark"; local?: string; message_id?: string; patch: Partial<ChatMessage> }
  | { kind: "remove"; local: string };

export function applyStart(entries: ChatMessage[], input: ToolCall): ChatMessage[] {
  const call = cleanCall(input);
  if (entries.some(e => e.calls?.some(c => c.call_id === call.call_id && c.turn_id === call.turn_id))) return entries;
  const last = entries.at(-1);
  if (last?.calls && last.turn_id === call.turn_id) {
    return [...entries.slice(0, -1), { ...last, calls: [...last.calls, call] }];
  }
  return [...entries, { role: "tool", text: "", turn_id: call.turn_id, calls: [call] }];
}
export function applyFinish(entries: ChatMessage[], event: Extract<TranscriptEvent, {kind: "finish"}>): ChatMessage[] {
  const index = entries.findIndex(e => e.calls?.some(c => c.call_id === event.call_id && c.turn_id === event.turn_id));
  if (index < 0) return entries;
  const group = entries[index];
  const calls = group.calls!.map(c => c.call_id === event.call_id && c.turn_id === event.turn_id
    && !(Date.parse(c.finished_at ?? "") > Date.parse(event.at))
    ? { ...c, outcome: event.outcome, finished_at: event.at } : c);
  return entries.map((e, i) => i === index ? { ...group, calls } : e);
}
export function stopTurn(entries: ChatMessage[], turn: string | undefined, at: string): ChatMessage[] {
  let changed = false;
  const next = entries.map(e => {
    if (!e.calls || (turn !== undefined && e.turn_id !== turn)) return e;
    if (!e.calls.some(c => c.outcome === "running")) return e;
    changed = true;
    return { ...e, calls: e.calls.map(c => c.outcome === "running"
      ? { ...c, outcome: "stopped" as const, finished_at: at } : c) };
  });
  return changed ? next : entries;
}
function messageIdentity(m: ChatMessage): string | undefined {
  if (m.message_id) return `message:${m.message_id}`;
  // Only an initial owner message lacks a message id. Steers/queued owner
  // messages have their own id, even if their turn or text is the same.
  if (m.role === "user" && m.turn_id) return `initial:${m.turn_id}`;
}
function sameMessage(a: ChatMessage, b: ChatMessage): boolean {
  if (a.role === "tool" || a.role !== b.role) return false;
  const id = messageIdentity(b);
  return id ? messageIdentity(a) === id : !!b.at && a.at === b.at && a.text === b.text;
}
export function applyEvent(entries: ChatMessage[], event: TranscriptEvent): ChatMessage[] {
  switch (event.kind) {
    case "start": return applyStart(entries, event.call);
    case "finish": return applyFinish(entries, event);
    case "stop": return stopTurn(entries, event.turn_id, event.at);
    case "remove": return entries.filter(m => m.local !== event.local);
    case "mark": {
      const next = entries.map(m => (event.message_id && m.message_id === event.message_id)
        || (event.local && m.local === event.local) ? { ...m, ...event.patch } : m);
      // A send can learn its server id after history has already landed.
      // Keep the history position and transfer its optimistic local handle.
      const first = new Map<string, number>();
      const merged: ChatMessage[] = [];
      for (const m of next) {
        const id = messageIdentity(m);
        const at = id ? first.get(id) : undefined;
        if (at === undefined) {
          if (id) first.set(id, merged.length);
          merged.push(m);
        } else merged[at] = { ...merged[at], ...m, at: merged[at].at ?? m.at };
      }
      return merged;
    }
    case "message": {
      const at = entries.findIndex(e => sameMessage(e, event.message));
      if (at < 0) return [...entries, event.message];
      // Retain the local handle so queued/dropped marks still find this row.
      return event.message.local ? entries.map((m, i) => i === at ? { ...m, local: event.message.local } : m) : entries;
    }
  }
}
/** History is folded once, then events heard during the load are replayed by stable ids. */
export function fold(rows: (ChatMessage | ToolEntry)[], replay: TranscriptEvent[] = []): ChatMessage[] {
  let entries: ChatMessage[] = [];
  for (const row of rows) {
    entries = row.role === "tool" && "call_id" in row
      ? applyStart(entries, row) : applyEvent(entries, { kind: "message", message: row as ChatMessage });
  }
  // An optimistic send receives its stable id/initial turn in the response.
  // Bind it before replaying so the message retains its history position.
  const identities = new Map<string, Partial<ChatMessage>>();
  const removed = new Set<string>();
  for (const event of replay) {
    if (event.kind === "mark" && event.local && (event.patch.message_id || event.patch.turn_id)) {
      identities.set(event.local, { ...identities.get(event.local),
        ...(event.patch.message_id ? { message_id: event.patch.message_id } : {}),
        ...(event.patch.turn_id ? { turn_id: event.patch.turn_id } : {}) });
    }
    if (event.kind === "remove") removed.add(event.local);
  }
  for (const event of replay) {
    if (event.kind === "message" && event.message.local) {
      if (removed.has(event.message.local)) continue;
      const identity = identities.get(event.message.local);
      entries = applyEvent(entries, identity ? { ...event, message: { ...event.message, ...identity } } : event);
    } else entries = applyEvent(entries, event);
  }
  return entries;
}
export function groupTitle(group: ChatMessage, using: boolean): { text: string; failed: number; denied: number; stopped: number } {
  const calls = group.calls ?? [];
  return { text: using ? `Using tools… ${calls.length}` : `Used ${calls.length} ${calls.length === 1 ? "tool" : "tools"}`,
    failed: calls.filter(c => c.outcome === "failed").length, denied: calls.filter(c => c.outcome === "denied").length,
    stopped: calls.filter(c => c.outcome === "stopped").length };
}
export function historyOps(entries: ChatMessage[], now: number): ToolOp[] {
  return entries.flatMap(e => e.calls ?? []).slice(-61).map(c => ({ call_id: c.call_id, turn_id: c.turn_id,
    name: c.name, label: c.label, at: c.at, started: c.outcome === "running" ? now : Date.parse(c.at) || now,
    ...(c.outcome === "running" ? {} : { finished: Date.parse(c.finished_at ?? "") || now, ok: c.outcome === "ok" }) }));
}
