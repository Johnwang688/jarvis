import { apiFetch } from "../../api";

export type ChatState = "idle" | "running" | "stopping" | "error";

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  t?: number;
  steered?: boolean;
  queued?: boolean;
  discarded?: boolean;
}

export interface ChatTool {
  name: string;
  ok?: boolean;
  summary?: string;
}

export interface ChatAuto {
  kind: "auto";
  tool: string;
  title: string;
  target: string;
  level: "L0" | "L1";
  why: string;
}

export interface ChatSnapshot {
  state: ChatState;
  session_id: string | null;
  seq: number;
  draft: string;
  tools: ChatTool[];
  history: ChatMessage[];
  approvals: number;
  outside_text: boolean;
  replay?: { history_prefix: number; events: ChatEvent[]; saved?: (ChatAuto & { after: number })[] };
}

type SteerInput = { seq: number; text: string };
type ChatEvent = { seq: number; t?: number } & (
  | { kind: "user"; text: string; inputs?: SteerInput[] }
  | { kind: "steer"; text: string; queued?: boolean }
  | { kind: "reply"; text: string }
  | { kind: "tool_start"; name: string }
  | ({ kind: "tool_end"; ok: boolean; summary: string } & ChatTool)
  | ChatAuto
  | { kind: "error" | "stopped"; discarded?: number[] }
);

interface ChatEvents {
  state: ChatState;
  seq: number;
  session_id?: string | null;
  gap: boolean;
  events: ChatEvent[];
  approvals: number;
  outside_text?: boolean;
}

export type ChatAction = { route: "send"; text: string } | { route: "stop" | "new" };

const actionRoutes = { send: "chatSend", stop: "chatStop", new: "chatNew" } as const;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isState(value: unknown): value is ChatState {
  return value === "idle" || value === "running" || value === "stopping" || value === "error";
}

function isSequence(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isTimestamp(value: unknown): boolean {
  return value === undefined || (typeof value === "number" && Number.isFinite(value));
}

function isMessage(value: unknown): value is ChatMessage {
  return record(value) && (value.role === "user" || value.role === "assistant")
    && typeof value.text === "string" && isTimestamp(value.t);
}

function isTool(value: unknown): value is ChatTool {
  return record(value) && typeof value.name === "string"
    && (value.ok == null || typeof value.ok === "boolean")
    && (value.summary == null || typeof value.summary === "string");
}

function isAuto(value: unknown): value is ChatAuto {
  return record(value) && value.kind === "auto" && typeof value.tool === "string"
    && typeof value.title === "string" && typeof value.target === "string"
    && (value.level === "L0" || value.level === "L1") && typeof value.why === "string";
}

function isEvent(value: unknown): value is ChatEvent {
  if (!record(value) || !isSequence(value.seq) || !isTimestamp(value.t)) return false;
  switch (value.kind) {
    case "user":
      return typeof value.text === "string" && (value.inputs === undefined ||
        (Array.isArray(value.inputs) && value.inputs.every((input) =>
          record(input) && isSequence(input.seq) && typeof input.text === "string")));
    case "steer":
      return typeof value.text === "string" && (value.queued === undefined || typeof value.queued === "boolean");
    case "reply":
      return typeof value.text === "string";
    case "tool_start":
      return typeof value.name === "string";
    case "tool_end":
      return typeof value.name === "string" && typeof value.ok === "boolean" && typeof value.summary === "string";
    case "auto":
      return isAuto(value);
    case "error":
    case "stopped":
      return value.discarded === undefined || (Array.isArray(value.discarded) && value.discarded.every(isSequence));
    default:
      return false;
  }
}

class ChatHttpError extends Error {
  constructor(readonly status: number) {
    super("Chat request failed");
  }
}

export function actionError(action: ChatAction, failure: unknown): string {
  const status = failure instanceof ChatHttpError ? failure.status : undefined;
  switch (action.route) {
    case "send":
      if (status === 400) return "Chat could not accept this message. Check the text and try again.";
      if (status === 409) return "Jarvis is stopping. Try sending again when it stops.";
      if (status === 429) return "Too many messages are queued. Try again shortly.";
      return "Could not confirm the send. Your message is still here.";
    case "stop":
      return "Could not stop the turn. Try again.";
    case "new":
      return status === 409 ? "Wait for the turn to finish before starting a new chat." : "Could not start a new chat. Try again.";
  }
}

async function request(signal: AbortSignal, route?: "chatEvents" | typeof actionRoutes[keyof typeof actionRoutes], body?: object): Promise<unknown> {
  const options: RequestInit = { signal, cache: "no-store", redirect: "error" };
  if (route) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await apiFetch(route ?? "chat", options);
  if (!response.ok) throw new ChatHttpError(response.status);
  return response.json();
}

export async function readChat(signal: AbortSignal): Promise<ChatSnapshot> {
  const value = await request(signal);
  // Older chat responses predate approvals.
  if (!record(value) || !isState(value.state) || !isSequence(value.seq)
    || (value.approvals !== undefined && !isSequence(value.approvals))
    || (value.outside_text !== undefined && typeof value.outside_text !== "boolean")
    || (value.session_id !== null && typeof value.session_id !== "string")
    || typeof value.draft !== "string" || !Array.isArray(value.history) || !value.history.every(isMessage)
    || !Array.isArray(value.tools) || !value.tools.every((tool) => typeof tool === "string" || isTool(tool))) {
    throw new Error("Invalid chat snapshot");
  }
  // String entries are the daemon's available tool names, not observed calls.
  const tools = value.tools.filter(isTool).map(({ name, ok, summary }) => ({
    name, ok: ok ?? undefined, summary: summary ?? undefined,
  }));
  const history = value.history.map(({ role, text, t }: ChatMessage) => ({ role, text, t }));
  let replay: ChatSnapshot["replay"];
  if (value.replay !== undefined) {
    const { replay: candidate } = value;
    if (!record(candidate) || !isSequence(candidate.history_prefix) || candidate.history_prefix > history.length
      || !Array.isArray(candidate.events) || !candidate.events.every(isEvent)) {
      throw new Error("Invalid chat replay");
    }
    const prefix = candidate.history_prefix;
    const saved = candidate.saved;
    if (saved !== undefined && (!Array.isArray(saved) || !saved.every((event) =>
      record(event) && isSequence(event.after) && event.after < prefix && isAuto(event)))) {
      throw new Error("Invalid saved chat replay");
    }
    replay = { history_prefix: prefix, events: candidate.events, saved };
  }
  return { state: value.state, seq: value.seq, session_id: value.session_id, draft: value.draft,
    history, tools, replay, approvals: value.approvals ?? 0, outside_text: value.outside_text ?? false };
}

export async function readEvents(after: number, signal: AbortSignal): Promise<ChatEvents> {
  const value = await request(signal, "chatEvents", { after });
  if (!record(value) || !isState(value.state) || !isSequence(value.seq) || typeof value.gap !== "boolean"
    || (value.session_id != null && typeof value.session_id !== "string")
    || (value.approvals !== undefined && !isSequence(value.approvals))
    || (value.outside_text !== undefined && typeof value.outside_text !== "boolean")
    || !Array.isArray(value.events) || (!value.gap && !value.events.every(isEvent))) {
    throw new Error("Invalid chat events");
  }
  return { state: value.state, seq: value.seq, session_id: value.session_id as string | null | undefined,
    gap: value.gap, events: value.gap ? [] : value.events,
    approvals: value.approvals ?? 0, outside_text: value.outside_text };
}

export async function writeChat(action: ChatAction, signal: AbortSignal):
  Promise<{ sessionId: string } | { state: ChatState; steered: boolean }> {
  const value = await request(signal, actionRoutes[action.route], action.route === "send" ? { text: action.text } : {});
  if (record(value)) {
    if (action.route === "new" && typeof value.session_id === "string") {
      return { sessionId: value.session_id };
    }
    if (isState(value.state)) {
      if (action.route === "stop") return { state: value.state, steered: false };
      if (action.route === "send" && typeof value.steered === "boolean") {
        return { state: value.state, steered: value.steered };
      }
    }
  }
  throw new Error("Invalid chat action response");
}
