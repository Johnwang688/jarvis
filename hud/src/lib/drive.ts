// When an agent drives a Browser pane (BR-2; amendment 1 §B.9, decisions B-9):
// the "driven by…" banner, the list of its actions, the outline of its
// target, and whether the owner's own input takes over. Pure, so the rules
// are tested without a browser; BR-9c draws them. Nothing here is wired in.
//
// The daemon is the authority: it holds the list (at most 50 per tab, in
// memory), stamps the owner's input and pauses the agent before that input is
// delivered. It tells the pane over the tab's own ticketed socket, as
// `{t: "agent", kind, …}` text frames — never the bus, never disk:
//
//   {t:"agent", kind:"driver", thread_id: str|null, provider: "fast"|"claude"|"codex"|null}
//       who holds the tab; `thread_id: null` when it lets go (its turn ended,
//       or 30 s after its last action);
//   {t:"agent", kind:"action", id, at, action, target, text, outcome, rect?}
//       one action, sent when it starts (`outcome: "running"`), while it waits
//       on the owner's card (`"asking"`) and when it ends (`done`, `refused`,
//       `asked_allowed`, `asked_denied`, `denied` or `stopped`), the same `id`
//       each time; `rect` is the target in page px, while it runs;
//   {t:"agent", kind:"paused", paused: bool, by: "owner"|"stop"|null}
//   {t:"agent", kind:"list", actions: [action, …]}
//       the held list, oldest first, when the pane attaches.
//
// Anything else, or a record of the wrong shape, is dropped whole. The page
// chose every `target` and `text`: each is cleaned to one capped line and only
// ever drawn as React text.
//
// **The banner** is HUD chrome in a neutral colour, never amber, never a modal,
// and never says Authorize, Approve or Deny: it is not a card, and must never
// look like one. It shows from an agent's first action until 30 s after its
// last, or until its turn ends; while an action runs; and while the agent is
// paused, which holds across turns until the owner presses Resume Jarvis.
//
// **Taking over.** Any input of the owner's forwarded to the tab pauses the
// agent before the event is delivered (the daemon does it; the pane mirrors
// it at once): a pointer press or release, the wheel, a key, a paste or an
// IME commit, a dialog or file-chooser answer. A pointer move does not — but
// a drag does (a move with a button held changes the page). The HUD's own
// keys never reach the page (`remote.browserTakesKey`), so they never count,
// and neither does a lone modifier — it is as often the first half of one.

import { cleanTitle, isModifierKey, type InputMsg } from "./remote";

export const ACTIONS = ["navigate", "back", "forward", "click", "type", "press", "scroll", "select", "upload"] as const;
export type ActionKind = (typeof ACTIONS)[number];

export const OUTCOMES = ["done", "refused", "asked_allowed", "asked_denied", "denied", "stopped"] as const;
export type Outcome = (typeof OUTCOMES)[number];
/** An action's state: under way, waiting on the owner's card, or ended. */
export type Phase = "running" | "asking" | Outcome;

export type DriverProvider = "fast" | "claude" | "codex";

/** The list the pane shows. The daemon holds 50; the pane shows the newest 20. */
export const LIST_MAX = 20;
/** The banner stays this long after the last action. */
export const LINGER_MS = 30_000;
/** A target and typed text are cleaned and capped at this many characters. */
export const TEXT_CAP = 60;
/**
 * An action still "running" or "asking" this long after its last word is not
 * outlined any more (its end was lost with a dropped socket; the replay on
 * reattach puts the list right). A card waits at most 120 s.
 */
export const RUNNING_STALE_MS = 180_000;

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface AgentAction {
  id: string;
  /** The daemon's time (epoch ms), for the list's clock. */
  at: number;
  action: ActionKind;
  /** What the page calls the target, cleaned. */
  target: string;
  /** What was typed, the address, the key or the direction, cleaned. */
  text: string;
  phase: Phase;
  /** The target in page px, while the action runs. */
  rect: Rect | null;
}

export type AgentMsg =
  | { kind: "driver"; threadId: string | null; provider: DriverProvider | null }
  | { kind: "action"; action: AgentAction }
  | { kind: "paused"; paused: boolean; by: "owner" | "stop" | null }
  | { kind: "list"; actions: AgentAction[] };

const PHASES: readonly Phase[] = ["running", "asking", ...OUTCOMES];
const PROVIDERS: readonly DriverProvider[] = ["fast", "claude", "codex"];

const isId = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(v);

function timeOf(v: unknown, fallback: number): number {
  if (typeof v === "number" && Number.isFinite(v) && v > 0) return v;
  if (typeof v === "string" && v.length <= 64) {
    const t = Date.parse(v);
    if (Number.isFinite(t)) return t;
  }
  return fallback;
}

function rectOf(v: unknown): Rect | null {
  if (!v || typeof v !== "object") return null;
  const r = v as Record<string, unknown>;
  const n = (x: unknown) => typeof x === "number" && Number.isFinite(x) && Math.abs(x) <= 100_000;
  if (!n(r.x) || !n(r.y) || !n(r.width) || !n(r.height)) return null;
  if ((r.width as number) < 0 || (r.height as number) < 0) return null;
  return { x: r.x as number, y: r.y as number, width: r.width as number, height: r.height as number };
}

function actionOf(v: unknown, now: number): AgentAction | null {
  if (!v || typeof v !== "object") return null;
  const a = v as Record<string, unknown>;
  if (!isId(a.id)) return null;
  if (!(ACTIONS as readonly unknown[]).includes(a.action)) return null;
  if (!(PHASES as readonly unknown[]).includes(a.outcome)) return null;
  const phase = a.outcome as Phase;
  const live = phase === "running" || phase === "asking";
  return {
    id: a.id,
    at: timeOf(a.at, now),
    action: a.action as ActionKind,
    target: cleanTitle(typeof a.target === "string" ? a.target.slice(0, 4096) : "", TEXT_CAP),
    text: cleanTitle(typeof a.text === "string" ? a.text.slice(0, 4096) : "", TEXT_CAP),
    phase,
    rect: live ? rectOf(a.rect) : null,
  };
}

/** A `{t:"agent"}` text frame, or null for anything else or the wrong shape. */
export function parseAgent(raw: unknown, now: number): AgentMsg | null {
  if (!raw || typeof raw !== "object") return null;
  const m = raw as Record<string, unknown>;
  if (m.t !== "agent") return null;
  switch (m.kind) {
    case "driver": {
      if (m.thread_id !== null && !isId(m.thread_id)) return null;
      const provider = (PROVIDERS as readonly unknown[]).includes(m.provider) ? (m.provider as DriverProvider) : null;
      return { kind: "driver", threadId: m.thread_id as string | null, provider };
    }
    case "action": {
      const action = actionOf(m, now);
      return action ? { kind: "action", action } : null;
    }
    case "paused": {
      if (typeof m.paused !== "boolean") return null;
      const by = m.by === "owner" || m.by === "stop" ? m.by : null;
      return { kind: "paused", paused: m.paused, by: m.paused ? by : null };
    }
    case "list": {
      if (!Array.isArray(m.actions)) return null;
      const actions = m.actions.slice(-50).map((a) => actionOf(a, now)).filter((a): a is AgentAction => a !== null);
      return { kind: "list", actions };
    }
    default:
      return null;
  }
}

export interface DriveState {
  threadId: string | null;
  provider: DriverProvider | null;
  /** An agent holds the tab: from its first action until it lets go. */
  driving: boolean;
  /** When (the pane's clock) the last action was heard. */
  lastAt: number | null;
  paused: boolean;
  pausedBy: "owner" | "stop" | null;
  /** Oldest first, at most LIST_MAX. */
  actions: AgentAction[];
}

export const DRIVE_IDLE: DriveState = {
  threadId: null, provider: null, driving: false, lastAt: null, paused: false, pausedBy: null, actions: [],
};

export type DriveEvent =
  | { type: "agent"; msg: AgentMsg; now: number }
  /** The owner's input went to the tab (`isTakeover` said so). */
  | { type: "owner"; now: number }
  /** The banner's Stop. */
  | { type: "stop" }
  /** Resume Jarvis: refused under a card, as the button is disabled and inert there. */
  | { type: "resume"; blocked: boolean }
  /** The tab closed: everything goes. */
  | { type: "closed" };

const keepLast = (list: AgentAction[]) => (list.length > LIST_MAX ? list.slice(-LIST_MAX) : list);

export function driveReducer(s: DriveState, ev: DriveEvent): DriveState {
  switch (ev.type) {
    case "agent": {
      const m = ev.msg;
      switch (m.kind) {
        // A driver, or a replayed list with an action still under way, starts
        // the banner's clock when none runs (review of PR #66): after a reload
        // mid-drive the pane hears `driver` and `list` but no `action`, and
        // the owner must still see the banner and its Stop.
        case "driver":
          if (m.threadId === null) return { ...s, driving: false };
          return { ...s, threadId: m.threadId, provider: m.provider, driving: true, lastAt: s.lastAt ?? ev.now };
        case "action": {
          const at = s.actions.findIndex((a) => a.id === m.action.id);
          const actions = at >= 0
            ? s.actions.map((a, i) => (i === at ? m.action : a))
            : keepLast([...s.actions, m.action]);
          return { ...s, actions, driving: true, lastAt: ev.now };
        }
        case "paused":
          return { ...s, paused: m.paused, pausedBy: m.paused ? m.by : null };
        case "list": {
          const live = m.actions.some((a) => a.phase === "running" || a.phase === "asking");
          return { ...s, actions: keepLast(m.actions.slice()), lastAt: live ? s.lastAt ?? ev.now : s.lastAt };
        }
      }
      return s;
    }
    case "owner":
      // The daemon pauses the agent; the pane says so at once. With no agent
      // at work there is nothing to take over, and nothing is shown.
      if (s.paused || !agentActive(s, ev.now)) return s;
      return { ...s, paused: true, pausedBy: "owner" };
    case "stop":
      return s.paused ? s : { ...s, paused: true, pausedBy: "stop" };
    case "resume":
      if (ev.blocked || !s.paused) return s;
      return { ...s, paused: false, pausedBy: null };
    case "closed":
      return DRIVE_IDLE;
  }
}

/** The newest action still under way or waiting on the owner, if it is not stale. */
export function runningOf(s: DriveState, now: number): AgentAction | null {
  if (s.lastAt === null || now - s.lastAt > RUNNING_STALE_MS) return null;
  for (let i = s.actions.length - 1; i >= 0; i--) {
    const a = s.actions[i];
    if (a.phase === "running" || a.phase === "asking") return a;
  }
  return null;
}

/** An agent is at work here: an action is under way, or it acted in the last 30 s and still holds the tab. */
export function agentActive(s: DriveState, now: number): boolean {
  if (runningOf(s, now)) return true;
  return s.driving && s.lastAt !== null && now - s.lastAt <= LINGER_MS;
}

/** The outline the pane draws around the target, in page px, while an action runs. */
export function outlineOf(s: DriveState, now: number): Rect | null {
  return runningOf(s, now)?.rect ?? null;
}

export const PROVIDER_WORDS: Record<DriverProvider, string> = { fast: "fast path", claude: "Claude", codex: "Codex" };

export interface Banner {
  text: string;
  paused: boolean;
  /** The banner's Stop (while an agent is at work and not paused). */
  canStop: boolean;
  /** Resume Jarvis (while paused; the pane disables it under a card). */
  canResume: boolean;
}

/**
 * The banner, or null when there is none. `slotOf` is the window's: the pane
 * number the owner reads for the chat showing that thread, or null when no
 * drawn pane shows it.
 */
export function bannerOf(s: DriveState, now: number, slotOf: (threadId: string) => number | null): Banner | null {
  const active = agentActive(s, now);
  if (!active && !s.paused) return null;
  const slot = s.threadId ? slotOf(s.threadId) : null;
  // Panes run 1..12 (decision FS-1); anything else is not a pane on screen.
  const where = s.threadId === null ? "a chat"
    : slot !== null && Number.isInteger(slot) && slot >= 1 && slot <= 12 ? `the chat in pane ${slot}`
    : "a chat not on screen";
  const who = s.provider ? `${where} (${PROVIDER_WORDS[s.provider]})` : where;
  if (s.paused) {
    const why = s.pausedBy === "stop" ? "you stopped" : "you took over from";
    const text = s.threadId === null && !active ? "Paused: you took over this pane" : `Paused: ${why} ${who}`;
    return { text, paused: true, canStop: false, canResume: true };
  }
  return { text: `Driven by ${who}`, paused: false, canStop: true, canResume: false };
}

const PHASE_WORDS: Record<Phase, string> = {
  running: "running",
  asking: "asking you",
  done: "done",
  refused: "refused",
  asked_allowed: "asked · you allowed",
  asked_denied: "asked · you said no",
  denied: "turned down",
  stopped: "stopped",
};

/** A local clock, `15:42:03`. */
export function clockOf(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "--:--:--";
  const d = new Date(ms);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

/** One row of the list: "15:42:03 · click · 'Sign in' · asked · you allowed". */
export function actionLine(a: AgentAction, clock: string = clockOf(a.at)): string {
  const parts = [clock, a.action];
  if (a.target) parts.push(`'${a.target}'`);
  if (a.text) parts.push(`"${a.text}"`);
  parts.push(PHASE_WORDS[a.phase]);
  return parts.join(" · ");
}

/** The socket's messages that are not the owner's input. */
const CONTROL: ReadonlySet<string> = new Set(["ack", "resize", "resume", "stop", "focus", "blur", "visible", "ping", "pong"]);

/**
 * Whether a message the pane is about to send is the owner's input to the
 * page — which pauses a driving agent before it is delivered. Keys, text
 * (a paste, an IME commit), the wheel, a pointer press or release, a dialog
 * or file-chooser answer: yes. A pointer move: no, unless a button is held (a
 * drag). The socket's own messages (acks, resizes, Resume, Stop): no.
 * Anything else the pane sends the page counts — a new kind of input that
 * this list does not know pauses the agent rather than slipping past it.
 */
export function isTakeover(msg: InputMsg | { t: string; [k: string]: unknown } | null | undefined): boolean {
  if (!msg || typeof msg !== "object" || typeof msg.t !== "string") return false;
  if (CONTROL.has(msg.t)) return false;
  // A lone modifier is never the owner taking over (review of PR #66): it is
  // as often the first half of a HUD key — push-to-talk's Ctrl, Ctrl+Alt+2's
  // Ctrl and Alt — and `routeKeyDown` never sends one anyway.
  if (msg.t === "key" && isModifierKey((msg as { e?: { key?: unknown } }).e?.key)) return false;
  if (msg.t === "mouse") {
    const e = (msg as { e?: { type?: unknown; buttons?: unknown } }).e;
    if (e && e.type === "mouseMoved") return typeof e.buttons === "number" && e.buttons !== 0;
  }
  return true;
}
