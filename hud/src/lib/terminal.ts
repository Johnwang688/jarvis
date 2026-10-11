// The owner's terminals, the rules (WP-D; plan docs/plans/2026-10-09-hud-
// workspace-plan.md §2.3–§2.4, decisions W-1, W-2, W-5; contract in
// docs/hud-api.md, "terminal backend"). Pure, like layout.ts and
// workspace.ts, so every rule is tested without a browser or a socket;
// components/Terminal.tsx only applies what this file decides.
//
// **The output is hostile bytes.** Anything a program prints — a title, a
// link, a control message's text — is treated the way a model's reply is:
// a title is text, capped, and never `document.title` (the desktop bridge
// recognises this window by `J.A.R.V.I.S.`, so a program that could retitle
// it would disarm that backstop); a link opens only for http(s); a server
// message is read field by field and anything malformed is ignored.
//
// **A paste never splices.** The daemon drops a frame it has no room for and
// then refuses everything after it on that socket (a lone Ctrl-C excepted),
// so the program sees a prefix of the paste. The window's half: send a
// paste in 16 KiB chunks, paced, from a queue that belongs to *one socket*;
// on `input_dropped` the queue is thrown away at once, and a socket that
// closes takes its queue with it — the rest of a paste is never sent on the
// next socket, where it would land after a hole.

export const CHUNK = 16 * 1024;
export const COLS = { min: 2, max: 1000 } as const;
export const ROWS = { min: 1, max: 500 } as const;
/** A title from the output is capped at this many characters. */
export const TITLE_MAX = 80;
/** The HUD's own font size, which a terminal matches at every zoom. */
export const FONT_PX = 13;

export type Integration = "bash" | "posix" | "none";

/** `GET /terminals`' row (docs/hud-api.md). */
export interface TerminalRow {
  id: string;
  title: string;
  folder: string;
  project_id: string | null;
  created: string;
  cols: number;
  rows: number;
  /** A window is attached (this one or another). */
  shown: boolean;
  exited: boolean;
  exit_code: number | null;
  /** The owner's "Jarvis can read" switch (W-2), on for every new terminal. */
  readable: boolean;
  /** Something other than the shell is in the foreground: ask before closing. */
  busy: boolean;
  /** What was configured. */
  integration: Integration;
  /** The shell has marked a command (bash only). */
  integrated: boolean;
  /** What took: the startup file's first signed prompt mark arrived. */
  marked: boolean;
}

/** Where a new terminal starts: resolved by the daemon from ids, never a path. */
export type InSpec = "home" | { thread: string } | { project: string } | { task: string };

const ID = /^[0-9a-f]{8}$/;

export function isTerminalId(v: unknown): v is string {
  return typeof v === "string" && ID.test(v);
}

function str(v: unknown, cap = 4096): string {
  return typeof v === "string" ? v.slice(0, cap) : "";
}

function int(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) ? v : null;
}

/** One row off the wire, or null when it is not one (an id is eight hex digits). */
export function parseRow(raw: unknown): TerminalRow | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const v = raw as Record<string, unknown>;
  if (!isTerminalId(v.id)) return null;
  const integration: Integration = v.integration === "bash" || v.integration === "posix" ? v.integration : "none";
  return {
    id: v.id,
    title: str(v.title, 200),
    folder: str(v.folder),
    project_id: typeof v.project_id === "string" ? v.project_id : null,
    created: str(v.created, 64),
    cols: int(v.cols) ?? 80,
    rows: int(v.rows) ?? 24,
    shown: v.shown === true,
    exited: v.exited === true,
    exit_code: int(v.exit_code),
    // On unless the daemon says off: the switch is on by default (W-2).
    readable: v.readable !== false,
    busy: v.busy === true,
    integration,
    integrated: v.integrated === true,
    marked: v.marked === true,
  };
}

export function parseRows(raw: unknown): TerminalRow[] {
  if (!Array.isArray(raw)) return [];
  const out: TerminalRow[] = [];
  for (const r of raw) {
    const row = parseRow(r);
    if (row && !out.some((o) => o.id === row.id)) out.push(row);
  }
  return out;
}

/**
 * The socket's URL, from the window's own location — **never a fixed port**:
 * a hard-coded port once sent the test suite to the owner's live daemon.
 * `https:` becomes `wss:`.
 */
export function attachUrl(loc: { protocol: string; host: string }, id: string, ticket: string): string {
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}/terminals/${encodeURIComponent(id)}/attach?ticket=${encodeURIComponent(ticket)}`;
}

const encoder = new TextEncoder();

export function encodeInput(data: string): Uint8Array {
  return encoder.encode(data);
}

/** `bytes` in pieces of at most `size`, in order. */
export function chunk(bytes: Uint8Array, size = CHUNK): Uint8Array[] {
  const out: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += size) out.push(bytes.subarray(i, Math.min(bytes.length, i + size)));
  return out;
}

/** Exactly one Ctrl-C: the one input a latched socket still takes. */
export function isCtrlC(bytes: Uint8Array): boolean {
  return bytes.length === 1 && bytes[0] === 0x03;
}

/**
 * The input waiting to go out on **one socket**, in 16 KiB chunks. It is
 * never handed to another socket: when its socket closes, or the daemon says
 * it dropped a frame, `abort()` throws the rest away and says how much.
 */
export class InputQueue {
  private parts: Uint8Array[] = [];
  private bytes = 0;
  private dead = false;

  push(data: Uint8Array): boolean {
    if (this.dead || !data.length) return false;
    for (const piece of chunk(data)) {
      this.parts.push(piece);
      this.bytes += piece.length;
    }
    return true;
  }

  /** The next chunk to send, or null. */
  next(): Uint8Array | null {
    if (this.dead) return null;
    const piece = this.parts.shift() ?? null;
    if (piece) this.bytes -= piece.length;
    return piece;
  }

  /** Throw away what is left; the number of bytes that will never be sent. */
  abort(): number {
    const dropped = this.bytes;
    this.parts = [];
    this.bytes = 0;
    return dropped;
  }

  /** This queue's socket is gone: nothing more is ever taken or given. */
  close(): number {
    this.dead = true;
    return this.abort();
  }

  get pending(): number {
    return this.bytes;
  }

  get closed(): boolean {
    return this.dead;
  }
}

/**
 * The owner's input for one terminal, socket by socket (the policy the
 * daemon's latch asks for, docs/hud-api.md):
 *
 *   - each socket gets a queue of its own (`open`); when it closes, what was
 *     not sent is dropped and never offered to the next socket (`close`);
 *   - `dropped` (the daemon's `input_dropped`) latches the gate and throws
 *     the rest of the paste away at once;
 *   - while latched nothing is queued — the owner is told and must
 *     acknowledge (`resumed`, after `input_resumed`) — except a lone Ctrl-C,
 *     which the daemon still takes;
 *   - a Ctrl-C jumps the queue and ends any paste still waiting in it.
 */
export class InputGate {
  private q: InputQueue | null = null;
  latched = false;

  /** A new socket: a fresh queue, unlatched. Whatever an older one held is dropped. */
  open(): number {
    const dropped = this.q ? this.q.close() : 0;
    this.q = new InputQueue();
    this.latched = false;
    return dropped;
  }

  /** The socket closed: the rest of a paste dies with it. */
  close(): number {
    const dropped = this.q ? this.q.close() : 0;
    this.q = null;
    this.latched = false;
    return dropped;
  }

  /**
   * Owner input. `send` goes out at once (a Ctrl-C), `held` was refused
   * (no socket, or latched), `dropped` is a paste a Ctrl-C ended.
   * Anything else is queued for `next()`.
   */
  input(bytes: Uint8Array): { send: Uint8Array | null; held: boolean; dropped: number } {
    if (!this.q || !bytes.length) return { send: null, held: true, dropped: 0 };
    if (isCtrlC(bytes)) return { send: bytes, held: false, dropped: this.q.abort() };
    if (this.latched) return { send: null, held: true, dropped: 0 };
    this.q.push(bytes);
    return { send: null, held: false, dropped: 0 };
  }

  next(): Uint8Array | null {
    return this.latched || !this.q ? null : this.q.next();
  }

  /** `input_dropped`: latched, and the rest of the paste thrown away. */
  dropped(): number {
    this.latched = true;
    return this.q ? this.q.abort() : 0;
  }

  resumed() {
    this.latched = false;
  }

  get pending(): number {
    return this.q ? this.q.pending : 0;
  }

  get connected(): boolean {
    return !!this.q;
  }
}

/**
 * The terminal's output, written into xterm in order, **one write in flight
 * at a time, each tagged with the socket it came from** (PR #28 review).
 *
 * xterm parses what it is given later, in its own time, and *answers* some of
 * it — a cursor-position query, a device-attributes query, an OSC colour
 * query — through the same channel the owner's keys use. Two things follow:
 *
 *   - a reattach must not parse an older socket's output into the new
 *     session: `next()` starts a generation, and everything an older one
 *     queued is dropped unparsed (at most the one write already handed to
 *     xterm finishes, before anything of the new generation runs);
 *   - `then(fn)` runs `fn` only once everything written before it has been
 *     parsed — which is how the replay's end is known (its queries have all
 *     been answered, into a session that was not listening).
 *
 * **The backlog is bounded** (PR #28 re-review). xterm parses some 14 MB/s;
 * output that arrives faster used to pile up here without limit (a reviewer
 * drove the renderer to ~1.5 GB, the display ~40 s behind). Past
 * `OUTPUT_HIGH_WATER` queued bytes everything queued is dropped, the
 * generation ends (the rest of this socket's output is never drawn) and
 * `onOverflow` says so — the session reattaches, so the replay shows the
 * terminal's latest 1 MiB instead of a backlog seconds or minutes old.
 *
 * **A write that throws never stalls it**: its callback will never come, so
 * the in-flight flag is cleared in a `finally` and the rest goes on.
 */
export const OUTPUT_HIGH_WATER = 8 * 1024 * 1024;

export class OutputPipe {
  private q: { gen: number; data?: Uint8Array; run?: () => void }[] = [];
  private inFlight = false;
  private current = 0;
  private queued = 0;

  constructor(
    private write: (data: Uint8Array, done: () => void) => void,
    private opts: { cap?: number; onOverflow?: (dropped: number) => void } = {},
  ) {}

  get gen(): number {
    return this.current;
  }

  /** A new socket: a new generation, and every older queued write dropped. */
  next(): number {
    this.current += 1;
    this.q = [];
    this.queued = 0;
    return this.current;
  }

  data(gen: number, bytes: Uint8Array) {
    if (gen !== this.current || !bytes.length) return;
    this.q.push({ gen, data: bytes });
    this.queued += bytes.length;
    if (this.queued > (this.opts.cap ?? OUTPUT_HIGH_WATER)) {
      const dropped = this.queued;
      this.current += 1;                             // this socket's output is not drawn any more
      this.q = [];
      this.queued = 0;
      this.opts.onOverflow?.(dropped);
      return;
    }
    this.pump();
  }

  /** `fn`, once everything written before it (this generation's and older) has been parsed. */
  then(gen: number, fn: () => void) {
    if (gen !== this.current) return;
    this.q.push({ gen, run: fn });
    this.pump();
  }

  /** Nothing more: the session is gone. */
  clear() {
    this.current += 1;
    this.q = [];
    this.queued = 0;
  }

  private pump() {
    while (!this.inFlight && this.q.length) {
      const item = this.q.shift()!;
      if (item.data) this.queued -= item.data.length;
      if (item.gen !== this.current) continue;      // an older socket's: never parsed here
      if (item.run) {
        item.run();
        continue;
      }
      this.inFlight = true;
      let handed = false;
      try {
        this.write(item.data!, () => {
          this.inFlight = false;
          this.pump();
        });
        handed = true;
      } finally {
        if (!handed) {
          // It threw: its callback will never come. Never wait on it — the
          // chunk is lost, the rest goes on once the throw has been reported.
          this.inFlight = false;
          queueMicrotask(() => this.pump());
        }
      }
    }
  }

  get pending(): number {
    return this.q.length;
  }

  /** Bytes queued and not yet handed to xterm. */
  get backlog(): number {
    return this.queued;
  }
}

/**
 * Pasted text, made inert as a control stream: ESC and the C1 controls are
 * removed, so a paste carrying `ESC[201~` cannot end bracketed-paste mode
 * early and have the rest run as typed (PR #28 review), nor carry any other
 * escape sequence into the program. Tabs and line ends stay.
 */
export function cleanPaste(text: string): string {
  // eslint-disable-next-line no-control-regex
  return (text || "").replace(/[\u001b\u0080-\u009f]/g, "");
}

/** The daemon's text frames (docs/hud-api.md). Anything else is ignored. */
export type Control =
  | { type: "attached"; terminal: TerminalRow | null; replay: number }
  | { type: "replayed" }
  | { type: "exit"; code: number | null; reason: "" | "closed" | "ended" }
  | { type: "takeover_request"; id: string; timeout_s: number }
  | { type: "waiting"; timeout_s: number }
  | { type: "taken" }
  | { type: "refused"; reason: string }
  | { type: "input_dropped"; bytes: number; latched: boolean; reason: string }
  | { type: "input_resumed" }
  | { type: "input_resume_refused"; reason: string }
  | { type: "marked" };

function seconds(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) && v > 0 && v <= 600 ? v : fallback;
}

export function parseControl(text: string): Control | null {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    return null;
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const m = v as Record<string, unknown>;
  switch (m.type) {
    case "attached": {
      const replay = int(m.replay);
      return { type: "attached", terminal: parseRow(m.terminal), replay: replay !== null && replay > 0 ? replay : 0 };
    }
    case "replayed":
    case "taken":
    case "input_resumed":
    case "marked":
      return { type: m.type };
    case "exit": {
      const reason = m.reason === "closed" || m.reason === "ended" ? m.reason : "";
      return { type: "exit", code: int(m.code), reason };
    }
    case "takeover_request":
      if (typeof m.id !== "string" || !m.id || m.id.length > 64) return null;
      return { type: "takeover_request", id: m.id, timeout_s: seconds(m.timeout_s, 20) };
    case "waiting":
      return { type: "waiting", timeout_s: seconds(m.timeout_s, 20) };
    case "refused":
      return { type: "refused", reason: cleanText(str(m.reason, 400), 200) };
    case "input_dropped": {
      const bytes = int(m.bytes);
      return {
        type: "input_dropped", bytes: bytes !== null && bytes > 0 ? bytes : 0,
        latched: m.latched === true, reason: cleanText(str(m.reason, 400), 300),
      };
    }
    case "input_resume_refused":
      return { type: "input_resume_refused", reason: cleanText(str(m.reason, 400), 300) };
    default:
      return null;
  }
}

/** Any text from the output or the socket, made safe to show: no control characters, one line, capped. */
export function cleanText(raw: string, cap: number): string {
  // eslint-disable-next-line no-control-regex
  const flat = (raw || "").replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]+/g, " ");
  const one = flat.replace(/\s+/g, " ").trim();
  return one.length > cap ? one.slice(0, cap - 1) + "…" : one;
}

/**
 * A title a program set (OSC 0/2): for the pane header only, as text,
 * capped. It never reaches `document.title` — nothing here writes that.
 */
export function cleanTitle(raw: string): string {
  return cleanText(raw, TITLE_MAX);
}

function isLoopbackHost(host: string): boolean {
  const h = host.toLowerCase().replace(/^\[|\]$/g, "");
  return h === "localhost" || h.endsWith(".localhost") || h === "::1" || h === "0.0.0.0"
    || /^127\.\d+\.\d+\.\d+$/.test(h);
}

export interface LinkVerdict {
  ok: boolean;
  url: string;
  /** A server on this machine (a dev server): WP-E offers it to Preview. */
  loopback: boolean;
}

/**
 * Whether a link printed in a terminal may be opened: http and https only —
 * the markdown rule. `javascript:`, `data:`, `file:`, `vbscript:` and every
 * other scheme are inert, however the link arrived (plain text or an OSC 8
 * hyperlink, whose target the program chose and the owner never saw).
 */
export function judgeLink(uri: string): LinkVerdict {
  const text = (uri || "").trim();
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return { ok: false, url: "", loopback: false };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return { ok: false, url: "", loopback: false };
  if (url.username || url.password) return { ok: false, url: "", loopback: false };
  return { ok: true, url: url.href, loopback: isLoopbackHost(url.hostname) };
}

/** A size the daemon accepts (cols 2–1000, rows 1–500), or null. */
export function clampSize(cols: number, rows: number): { cols: number; rows: number } | null {
  if (!Number.isFinite(cols) || !Number.isFinite(rows) || cols < 1 || rows < 1) return null;
  return {
    cols: Math.min(COLS.max, Math.max(COLS.min, Math.floor(cols))),
    rows: Math.min(ROWS.max, Math.max(ROWS.min, Math.floor(rows))),
  };
}

/**
 * xterm under CSS `zoom` (plan §2.4, the main risk): the terminal's host is
 * counter-zoomed (`zoom: 1 / level`), so xterm measures cells and mouse
 * positions in unzoomed space, and its own font is scaled by the zoom
 * instead, so the text matches the HUD's at every level.
 */
export function fontSizeFor(zoomPct: number, base = FONT_PX): number {
  const z = Number.isFinite(zoomPct) && zoomPct > 0 ? zoomPct / 100 : 1;
  return Math.round(base * z * 2) / 2;
}

export function counterZoom(zoomPct: number): number {
  const z = Number.isFinite(zoomPct) && zoomPct > 0 ? zoomPct / 100 : 1;
  return 1 / z;
}

/** Whether keys are going into a terminal. */
export function inTerminal(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof (el as any).closest !== "function") return false;
  return !!el.closest(".termslot, .xterm");
}

/**
 * Which of the HUD's own keys stand aside in a terminal, so the shell gets
 * them (plan §2.2's key table): Ctrl+B and Ctrl+Alt+B (readline back-char,
 * page up in less and vim) and Ctrl+_ (readline undo). Everything else —
 * the zoom keys, Ctrl+`, Ctrl+Alt+1…4 — stays the HUD's even there.
 */
export function terminalTakesKey(shortcut: string | null, key: string): boolean {
  if (shortcut === "toggleLeft" || shortcut === "toggleRight") return true;
  return shortcut === "zoomOut" && key === "_";
}

/**
 * The note a terminal's bar carries about its shell integration: none for a
 * shell whose startup file ran (`marked`); "no integration" for a shell that
 * reads none (zsh, fish: no `sudo -k`, no marks); and, once the shell has
 * had time to show a prompt (`settled`), "inactive" for a configured one
 * that never marked — a profile that `exec`s another shell, say.
 */
export function integrationNote(
  row: { integration: Integration; marked: boolean } | null, settled: boolean,
): { kind: "none" | "inactive"; badge: string; title: string } | null {
  if (!row) return null;
  if (row.integration === "none") {
    return {
      kind: "none",
      badge: "no integration",
      title: "This shell reads no startup file: sudo may cache your password here, "
        + "and commands are not marked for Jarvis's reader.",
    };
  }
  if (!row.marked && settled) {
    return {
      kind: "inactive",
      badge: "integration inactive",
      title: "The startup file has not run (a profile may have started another shell): "
        + "sudo may cache your password here, and commands are not marked.",
    };
  }
  return null;
}

/**
 * Which drawn pane shows which terminal: one terminal is drawn in one place
 * at a time (W-1). The first drawn pane (in drawn order) holding an id wins;
 * any later pane holding the same one says where it is instead. A terminal
 * no drawn pane holds belongs to the panel.
 */
export function placeTerminals(
  panes: readonly { view: string; terminalId: string | null }[], drawn: readonly number[],
): Map<string, number> {
  const out = new Map<string, number>();
  for (const n of drawn) {
    const p = panes[n - 1];
    if (p && p.view === "terminal" && p.terminalId && !out.has(p.terminalId)) out.set(p.terminalId, n);
  }
  return out;
}

/**
 * `terminal_read` on the bus (WP-F, decisions W-2): Jarvis read this terminal,
 * or asked and was refused. The record is exactly `{terminal_id, lines, at,
 * refused}` — never output, a command or a reason — and a record that does not
 * have that shape is dropped whole; any other key is ignored, never drawn.
 */
export interface ReadNote {
  terminalId: string;
  lines: number;
  at: string;
  refused: boolean;
}

export function parseRead(data: unknown): ReadNote | null {
  const d = (data ?? {}) as Record<string, unknown>;
  if (!isTerminalId(d.terminal_id)) return null;
  const lines = d.lines;
  if (typeof lines !== "number" || !Number.isInteger(lines) || lines < 0 || lines > 100_000) return null;
  if (typeof d.refused !== "boolean") return null;
  return { terminalId: d.terminal_id, lines, at: typeof d.at === "string" ? d.at.slice(0, 64) : "",
           refused: d.refused };
}

/** The terminal bar's note: "Jarvis read 200 lines · 15:42", or "refused a read · 15:42". */
export function readNoteText(note: ReadNote, time: string): string {
  if (note.refused) return `refused a read · ${time}`;
  return `Jarvis read ${note.lines} line${note.lines === 1 ? "" : "s"} · ${time}`;
}

/**
 * Whose attach was it? Every attach publishes `terminal_attached` with only
 * `{terminal_id, at}` — a same-uid program can mint a ticket and attach to a
 * terminal no window shows, and over HTTP that looks like the HUD. The window
 * notes each socket it opens (`mine`) and matches the events against them;
 * one it cannot match was not its own (`heard` → false), and the owner is
 * told, quietly. A note expires after `ttlMs` (an attach that never happened
 * — a refused takeover, a dead socket — must not excuse a later stranger).
 */
export class AttachLedger {
  private own: { id: string; at: number }[] = [];

  constructor(private ttlMs = 40_000) {}

  mine(id: string, now: number) {
    this.prune(now);
    this.own.push({ id, at: now });
  }

  /** This window's socket for `id` never attached: forget its note. */
  failed(id: string) {
    const i = this.own.findIndex((o) => o.id === id);
    if (i >= 0) this.own.splice(i, 1);
  }

  /** A `terminal_attached` event: true when it was this window's own. */
  heard(id: string, now: number): boolean {
    this.prune(now);
    const i = this.own.findIndex((o) => o.id === id);
    if (i < 0) return false;
    this.own.splice(i, 1);
    return true;
  }

  private prune(now: number) {
    this.own = this.own.filter((o) => now - o.at <= this.ttlMs);
  }

  get size(): number {
    return this.own.length;
  }
}

/**
 * Where `+` opens a terminal (plan §2.4): the folder of what the focused
 * pane shows — a chat's own folder (its thread; a compose row's project), a
 * File or Preview pane's project, a task's worktree — else home. Always as an
 * id the daemon resolves, never a path. A terminal pane defers to the chat.
 */
export function terminalSpecFor(view: string, at: {
  thread: string | null; compose: string | null; project: string | null; task: string | null;
}): InSpec {
  const chat = (): InSpec =>
    at.thread ? { thread: at.thread } : at.compose ? { project: at.compose } : "home";
  switch (view) {
    case "file":
    case "preview":
      return at.project ? { project: at.project } : chat();
    case "task":
    case "diff":
      return at.task ? { task: at.task } : chat();
    default:
      return chat();
  }
}

/** Where a terminal was opened, so Restart and "New terminal here" reopen it there. */
export function parseSpec(v: unknown): InSpec | null {
  if (v === "home") return "home";
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const keys = Object.keys(v);
  if (keys.length !== 1) return null;
  const [k] = keys;
  const id = (v as Record<string, unknown>)[k];
  if ((k === "thread" || k === "project" || k === "task") && typeof id === "string" && id && id.length <= 200) {
    return { [k]: id } as InSpec;
  }
  return null;
}

export const TERMINALS_KEY = "jarvis.hud.terminals";

export interface TerminalPrefs {
  /** The panel's selected tab. */
  active: string | null;
  /** id → where it was opened (at most `SPECS_KEPT`). */
  specs: Record<string, InSpec>;
}

export const SPECS_KEPT = 24;

export function parsePrefs(raw: unknown): TerminalPrefs {
  let v: any = null;
  if (typeof raw === "string") {
    try {
      v = JSON.parse(raw);
    } catch {
      v = null;
    }
  }
  const prefs: TerminalPrefs = { active: null, specs: {} };
  if (!v || typeof v !== "object" || Array.isArray(v)) return prefs;
  if (isTerminalId(v.active)) prefs.active = v.active;
  if (v.specs && typeof v.specs === "object" && !Array.isArray(v.specs)) {
    for (const [id, spec] of Object.entries(v.specs).slice(-SPECS_KEPT)) {
      const s = parseSpec(spec);
      if (isTerminalId(id) && s) prefs.specs[id] = s;
    }
  }
  return prefs;
}

export function loadPrefs(storage?: Pick<Storage, "getItem">): TerminalPrefs {
  try {
    return parsePrefs((storage ?? window.localStorage).getItem(TERMINALS_KEY));
  } catch {
    return { active: null, specs: {} };
  }
}

export function savePrefs(prefs: TerminalPrefs, storage?: Pick<Storage, "setItem">) {
  try {
    const clean = parsePrefs(JSON.stringify(prefs));
    (storage ?? window.localStorage).setItem(TERMINALS_KEY, JSON.stringify(clean));
  } catch {
    /* storage blocked: the terminals still work, they just forget */
  }
}

/**
 * The terminals this tab has shown (sessionStorage: per tab, and it survives
 * a reload). A terminal the daemon lists as `shown` that is **not** one of
 * these is in another window, and this window never attaches to it on its own
 * — the owner says "Show it here" first (PR #28 review: a takeover prompt the
 * owner did not cause is one they learn to wave through). A reload finds its
 * own terminals here, so it attaches straight back even while the daemon still
 * counts the old page's socket.
 *
 * **A copy of the list is not the list** (PR #28 re-review). Chrome copies
 * sessionStorage into a duplicated tab and into a reopened closed one, and a
 * copy must not skip "Show it here". So the list carries its `holder`: the
 * per-page-load nonce of the live page that wrote it (minted in memory, never
 * stored anywhere else), set to null by that page's `pagehide` as it goes. A
 * new page inherits the list only when **both** say it is a reload of the page
 * that released it: the holder is null, and this load's navigation type is
 * `reload`. A duplicate copies a list whose holder is its still-live original
 * (never null), whatever Chrome calls the navigation; a reopened closed tab
 * has a released list but is a restore, not a reload. Anything else — a
 * legacy array, garbage, storage that throws — inherits nothing (the safe
 * direction: "Show it here" once more).
 */
export const MINE_KEY = "jarvis.hud.terminals.mine";
export const MINE_KEPT = 32;

export interface MineRecord {
  ids: string[];
  /** The live page that wrote it; null once that page has gone (pagehide). */
  holder: string | null;
}

export function parseMine(raw: unknown): MineRecord {
  let v: any = null;
  if (typeof raw === "string") {
    try {
      v = JSON.parse(raw);
    } catch {
      v = null;
    }
  }
  if (!v || typeof v !== "object" || Array.isArray(v) || !Array.isArray(v.ids)) return { ids: [], holder: "" };
  const ids: string[] = [];
  for (const id of v.ids) if (isTerminalId(id) && !ids.includes(id)) ids.push(id);
  const holder = v.holder === null ? null : typeof v.holder === "string" ? v.holder.slice(0, 64) : "";
  return { ids: ids.slice(-MINE_KEPT), holder };
}

/** What a page load inherits: the list only on a reload of the page that released it. */
export function inheritMine(raw: unknown, navigation: string): Set<string> {
  const rec = parseMine(raw);
  return navigation === "reload" && rec.holder === null ? new Set(rec.ids) : new Set();
}

/** This load's navigation type ("navigate", "reload", "back_forward", …), or "" if unknown. */
export function navigationType(): string {
  try {
    const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
    return nav?.type ?? "";
  } catch {
    return "";
  }
}

export function loadMine(storage?: Pick<Storage, "getItem">, navigation?: string): Set<string> {
  try {
    return inheritMine((storage ?? window.sessionStorage).getItem(MINE_KEY), navigation ?? navigationType());
  } catch {
    return new Set();
  }
}

/** `holder`: this page's nonce while it lives; null as it goes (pagehide). */
export function saveMine(mine: Set<string>, holder: string | null, storage?: Pick<Storage, "setItem">) {
  try {
    (storage ?? window.sessionStorage).setItem(
      MINE_KEY, JSON.stringify({ ids: Array.from(mine).slice(-MINE_KEPT), holder }));
  } catch {
    /* storage blocked: a reload asks "Show it here" once more */
  }
}

/** A per-page-load nonce, in memory only. */
export function pageNonce(): string {
  try {
    const b = new Uint8Array(8);
    crypto.getRandomValues(b);
    return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  } catch {
    return `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
  }
}
