// The owner's terminals in the window (WP-D; plan docs/plans/2026-10-09-hud-
// workspace-plan.md §2.3–§2.4, decisions W-1, W-2, W-5; contract in
// docs/hud-api.md). lib/terminal.ts decides every rule; this file holds the
// sockets and the xterm views and draws them.
//
// **One window, one manager, one session per terminal.** A session is the
// terminal's xterm and its socket. It is created the first time this window
// draws the terminal, attaches with a fresh ticket, and then stays attached
// while it is hidden — the output keeps landing in its buffer — until the
// terminal is closed, taken by another window, or ends with Jarvis. Its view
// is moved, not rebuilt, between the panel and a pane: one terminal is drawn
// in one place at a time (W-1).
//
// **The agent never gets a lever here.** Nothing in this file is reachable
// from a tool; the routes are owner-only (this window's Origin) and every
// attach needs a ticket fetched right before it.
//
// **Under an authorization card nothing reaches the shell**: input is held
// (xterm's `disableStdin`, a key filter that lets every key bubble to the
// card — Escape denies — and a guard on the bytes themselves), a paste in
// flight waits, and the card takes focus (Approvals.tsx). Output keeps
// drawing, and focus comes back when the card goes.
//
// **Output is hostile bytes.** A title a program sets is text in the pane
// header, capped, and never `document.title`; a link opens only for http(s),
// and only on Ctrl+click; there is no clipboard addon (no OSC 52). xterm
// answers some output — a cursor-position, device or colour query — through
// the owner's own input channel, so **a reattach sends nothing until its
// replay has been parsed** (the ring's old queries are answered into a
// session that is not listening), and an older socket's queued output is
// never parsed into a new one (OutputPipe).
//
// **A paste is inert as a control stream**: ESC and C1 are stripped before
// xterm sees it, so a pasted `ESC[201~` cannot end bracketed paste early.
//
// **A terminal another window shows is never taken unasked**: it is drawn as
// "in another window · Show it here" until the owner says so here.

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import type { IDisposable, Terminal as XTerm } from "@xterm/xterm";
import type { FitAddon } from "@xterm/addon-fit";
import { api } from "../api";
import {
  AttachLedger, InputGate, OutputPipe, attachUrl, cleanPaste, cleanTitle, clampSize, counterZoom, encodeInput,
  fontSizeFor, integrationNote, isTerminalId, judgeLink, loadMine, loadPrefs, pageNonce, parseControl, parseRead,
  readNoteText, saveMine, savePrefs,
  SPECS_KEPT, type InSpec, type ReadNote, type TerminalRow,
} from "../lib/terminal";
import { toCss } from "../lib/layout";
import { loadXterm } from "../lib/xterm";
import type { PaneNo } from "../lib/workspace";
import type { Project } from "../types";

export type SessionState =
  | "idle" | "connecting" | "waiting" | "attached" | "taken" | "refused" | "exited" | "ended" | "closed" | "lost";

/** Between paste chunks: a paste is paced, so `input_dropped` can stop the rest. */
const PACE_MS = 8;
/** A socket this far behind on sending waits before the next chunk. */
const HIGH_WATER = 64 * 1024;
const RECONNECT_MS = [300, 700, 1500, 3000, 6000, 10000];
/** How long a shell has to show its first prompt before "integration inactive" is said. */
const SETTLE_MS = 4000;
const RESUME_RETRY_MS = 1000;
/** RIS, written in band: the replay that follows starts on a clean terminal (see "attached"). */
const RIS = new Uint8Array([0x1b, 0x63]);

/** Graphite, for the terminal: the pane colour, the HUD's text, the accent cursor. */
const THEME = {
  background: "#1a1a19", foreground: "#e2dfd7", cursor: "#6fc3df", cursorAccent: "#1a1a19",
  selectionBackground: "#6fc3df55", selectionInactiveBackground: "#6fc3df2a",
  black: "#141413", red: "#f87171", green: "#6dd08f", yellow: "#fbbf24",
  blue: "#7aa7d8", magenta: "#c0aef5", cyan: "#6fc3df", white: "#cfcbc3",
  brightBlack: "#88847b", brightRed: "#fca5a5", brightGreen: "#9be3b4", brightYellow: "#fcd34d",
  brightBlue: "#9cc2ea", brightMagenta: "#d8ccfa", brightCyan: "#9ad6ea", brightWhite: "#f5f3ee",
};

export interface PasteNotice {
  /**
   * dropped: the program was not reading; lost: the socket closed mid-paste;
   * stopped: a Ctrl-C ended it; unsent: typed or pasted while the terminal
   * was connecting (or not running here), so it went nowhere.
   */
  kind: "dropped" | "lost" | "stopped" | "unsent";
  bytes: number;
  /** For "unsent": still connecting, or not running in this window. */
  why?: "connecting" | "offline";
  /** An `input_resume` is out (or being retried). */
  resuming: boolean;
  /** The daemon's last word on a refused resume. */
  refused: string;
}

let unloading = false;
/**
 * This page load's nonce, in memory only: it marks this tab's list of its own
 * terminals as held by a live page (lib/terminal.ts, MINE_KEY), so a copy of
 * the list in a duplicated or reopened tab is never taken for its own.
 */
const PAGE = pageNonce();
if (typeof window !== "undefined") {
  // Leaving (a reload, the window closing): say goodbye with a normal close,
  // so the daemon detaches at once rather than when TCP notices — and never
  // reattach on the way out. The tab's list is released as it goes: only a
  // reload of this page may take it up again.
  window.addEventListener("pagehide", () => {
    unloading = true;
    for (const s of terminals.sessions.values()) s.leave();
    terminals.release();
  });
  // Back from the back/forward cache: the sessions it closed attach again.
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    unloading = false;
    terminals.hold();
    for (const s of terminals.sessions.values()) s.revive();
  });
}

/** A tab's row for a terminal that no longer exists. */
const GONE_ROW: TerminalRow = {
  id: "", title: "", folder: "", project_id: null, created: "", cols: 80, rows: 24, shown: false,
  exited: false, exit_code: null, readable: true, busy: false, integration: "none", integrated: false,
  marked: false,
};

function kib(bytes: number): string {
  return bytes >= 1024 ? `${Math.round(bytes / 1024)} KiB` : `${bytes} byte${bytes === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------------------
// one terminal in this window

export class TermSession {
  readonly id: string;
  state: SessionState = "idle";
  exitCode: number | null = null;
  /** A title the program set (OSC 0/2), cleaned. */
  title = "";
  refused = "";
  waitingUntil = 0;
  /** Another window asks for this terminal. */
  takeover: { id: string; until: number } | null = null;
  paste: PasteNotice | null = null;
  /** Typing was refused while latched: the notice says so again. */
  heldAt = 0;
  marked = false;
  attachedAt = 0;
  /** The row as last seen, kept for a tab after the terminal itself is gone. */
  lastRow: TerminalRow | null = null;
  /** The element xterm lives in; moved, not rebuilt, between the panel and a pane. */
  readonly wrap: HTMLDivElement;
  private slot: HTMLElement | null = null;
  private term: XTerm | null = null;
  private fit: FitAddon | null = null;
  private loading: Promise<void> | null = null;
  private ws: WebSocket | null = null;
  private gate = new InputGate();
  /**
   * The output, in order, tagged with the socket it came from (lib/terminal.ts,
   * OutputPipe): a reattach never parses an older socket's queued output.
   */
  private out = new OutputPipe((data, done) => {
    const term = this.term;
    if (!term) {
      done();
      return;
    }
    term.write(data, done);
  }, { onOverflow: (dropped) => this.overflowed(dropped) });
  /** Output skipped because it came faster than xterm could draw it (bytes; the notice). */
  skipped = 0;
  /** The current socket's generation (OutputPipe). */
  private gen = 0;
  /**
   * From a new socket until its replay has been *parsed*: xterm answers the
   * queries in the replayed output (cursor position, device attributes, a
   * colour, DECRQSS) through the owner's input channel, and none of those
   * answers — nor anything else — may reach the program as if typed.
   */
  private replaying = false;
  /** The owner is typing or pasting right now (set for the length of the event). */
  private gesture = false;
  private gestureTimer: ReturnType<typeof setTimeout> | null = null;
  /** A first attach is waiting on a fresh listing (is it shown in another window?). */
  private checking = false;

  /**
   * The terminal printed faster than this window can draw (OutputPipe's
   * high-water mark): the backlog is already gone, and the socket starts
   * again, so the replay shows the latest 1 MiB rather than output seconds or
   * minutes old. Said, never silent: the scrollback just jumped.
   */
  private overflowed(dropped: number) {
    this.skipped += dropped;
    this.mgr.changed();
    if (!this.disposed && this.ws) this.reattach();
  }

  dismissSkipped() {
    this.skipped = 0;
    this.mgr.changed();
  }

  /** Output queued and not yet drawn (bytes; the test hook). */
  get backlog(): number {
    return this.out.backlog;
  }
  private pumpTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private resumeTimer: ReturnType<typeof setTimeout> | null = null;
  private attempts = 0;
  private wanted = false;
  private replayed = false;
  private sawAttached = false;
  private lastSize: { cols: number; rows: number } | null = null;
  private disposed = false;
  private subs: IDisposable[] = [];

  constructor(id: string, private mgr: TermManager) {
    this.id = id;
    this.wrap = document.createElement("div");
    this.wrap.className = "termhost";
    // Every key typed into a terminal is the shell's: Space is never push-to-
    // talk, Escape never closes a menu behind it. The window's own capture-
    // phase listener has already run (its zoom keys, Ctrl+`). Under a card
    // nothing is stopped, so Escape reaches the card and denies.
    const stop = (e: Event) => {
      if (!this.mgr.blocked) e.stopPropagation();
    };
    for (const kind of ["keydown", "keyup", "keypress"]) this.wrap.addEventListener(kind, stop);
    // What the owner does, as opposed to what xterm answers on its own: a key
    // or text dropped while connecting is said; a query's answer is not.
    for (const kind of ["keydown", "keypress", "input", "compositionend"]) {
      this.wrap.addEventListener(kind, this.markGesture, true);
    }
    // Every paste comes through here, before xterm's own handler (capture
    // phase, on the host): made inert as a control stream, then pasted the
    // way xterm would (bracketed when the program asked for it).
    this.wrap.addEventListener("paste", (e) => this.onPaste(e as ClipboardEvent), true);
  }

  private markGesture = () => {
    this.gesture = true;
    if (this.gestureTimer) return;
    this.gestureTimer = setTimeout(() => {
      this.gestureTimer = null;
      this.gesture = false;
    }, 0);
  };

  private onPaste(e: ClipboardEvent) {
    // xterm never sees the raw event: a paste carrying `ESC[201~` would end
    // bracketed-paste mode early, and the rest would run as typed.
    e.preventDefault();
    e.stopPropagation();
    if (this.mgr.blocked || this.disposed || !this.term) return;
    const text = cleanPaste(e.clipboardData?.getData("text/plain") ?? "");
    if (!text) return;
    this.markGesture();
    this.term.paste(text);
  }

  // -- the view -------------------------------------------------------------

  mount(slot: HTMLElement) {
    this.slot = slot;
    slot.appendChild(this.wrap);
    this.ensure().then(() => {
      if (this.disposed || this.slot !== slot) return;
      this.applyZoom();
      this.term?.refresh(0, Math.max(0, (this.term?.rows ?? 1) - 1));
      this.fitNow();
      if (this.state === "idle") void this.firstAttach();
    }).catch(() => {
      /* xterm did not load: the next mount tries again */
    });
  }

  unmount(slot: HTMLElement) {
    if (this.slot !== slot) return;
    this.slot = null;
    if (this.wrap.parentElement === slot) slot.removeChild(this.wrap);
  }

  private ensure(): Promise<void> {
    if (this.term) return Promise.resolve();
    if (!this.loading) {
      this.loading = loadXterm().then((kit) => {
        if (this.disposed || this.term) return;
        const term = new kit.Terminal({
          fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, monospace',
          fontSize: fontSizeFor(this.mgr.zoom),
          cursorBlink: true,
          scrollback: 10000,
          theme: THEME,
          disableStdin: this.mgr.blocked,
          // OSC 8 hyperlinks: judged like every other link, never the
          // browser's own confirm-and-open.
          linkHandler: {
            activate: (e, uri) => this.mgr.openLink(e, uri),
            hover: (_e, uri) => this.hint(uri),
            leave: () => this.hint(""),
            allowNonHttpProtocols: false,
          },
        });
        const fit = new kit.FitAddon();
        term.loadAddon(fit);
        term.loadAddon(new kit.WebLinksAddon((e, uri) => this.mgr.openLink(e, uri), {
          hover: (_e, uri) => this.hint(uri),
          leave: () => this.hint(""),
        }));
        term.attachCustomKeyEventHandler((e) => this.keyFilter(e));
        this.subs.push(
          term.onData((d) => this.input(encodeInput(d))),
          term.onBinary((d) => {
            const b = new Uint8Array(d.length);
            for (let i = 0; i < d.length; i++) b[i] = d.charCodeAt(i) & 0xff;
            this.input(b);
          }),
          // Never document.title: the desktop bridge's backstop matches on it.
          term.onTitleChange((t) => {
            this.title = cleanTitle(t);
            this.mgr.changed();
          }),
          term.onResize(() => this.sendResize(false)),
        );
        term.open(this.wrap);
        this.term = term;
        this.fit = fit;
      });
      this.loading.catch(() => {
        this.loading = null;
      });
    }
    return this.loading;
  }

  private hint(uri: string) {
    const v = uri ? judgeLink(uri) : null;
    this.wrap.title = v && v.ok ? `Ctrl+click to open ${v.url}` : "";
  }

  /** What xterm may do with a key. False: xterm leaves it alone (and it bubbles). */
  private keyFilter(e: KeyboardEvent): boolean {
    if (this.mgr.blocked) return false;
    if (e.type !== "keydown") return true;
    // The HUD took it in its capture-phase listener (zoom, Ctrl+`, Ctrl+Alt+N).
    if (e.defaultPrevented) return false;
    const ctrl = (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (ctrl && k === "c" && this.term?.hasSelection()) {
      // Copy when there is a selection; otherwise Ctrl+C interrupts.
      const text = this.term.getSelection();
      try {
        void navigator.clipboard?.writeText(text).catch(() => {});
      } catch {
        /* no clipboard: the selection stays */
      }
      this.term.clearSelection();
      e.preventDefault();
      return false;
    }
    // Ctrl+V pastes: the browser's paste event reaches xterm, bracketed.
    if (ctrl && k === "v") return false;
    return true;
  }

  focus() {
    if (!this.mgr.blocked) this.term?.focus();
  }

  applyZoom() {
    // Counter-zoomed host, font scaled instead (lib/terminal.ts, fontSizeFor).
    this.wrap.style.zoom = String(counterZoom(this.mgr.zoom));
    if (this.term) this.term.options.fontSize = fontSizeFor(this.mgr.zoom);
    this.fitNow();
  }

  setBlocked(blocked: boolean) {
    if (this.term) this.term.options.disableStdin = blocked;
    if (!blocked) this.pumpSoon(0);
  }

  fitNow() {
    const term = this.term;
    const fit = this.fit;
    if (!term || !fit || !this.wrap.isConnected) return;
    // Hidden (display: none): nothing to measure, and a zero size is not one.
    if (this.wrap.clientWidth < 20 || this.wrap.clientHeight < 10) return;
    try {
      const d = fit.proposeDimensions();
      if (d && Number.isFinite(d.cols) && Number.isFinite(d.rows) && d.cols >= 2 && d.rows >= 1
          && (d.cols !== term.cols || d.rows !== term.rows)) {
        term.resize(d.cols, d.rows);
      }
    } catch {
      /* not measurable yet */
    }
  }

  /** The buffer as text (the test hook). */
  bufferText(): string {
    const b = this.term?.buffer.active;
    if (!b) return "";
    const lines: string[] = [];
    for (let i = 0; i < b.length; i++) lines.push(b.getLine(i)?.translateToString(true) ?? "");
    return lines.join("\n").replace(/\n+$/, "");
  }

  get size() {
    return this.term ? { cols: this.term.cols, rows: this.term.rows } : null;
  }

  get selection(): string {
    return this.term?.getSelection() ?? "";
  }

  get latched(): boolean {
    return this.gate.latched;
  }

  // -- the socket -----------------------------------------------------------

  private set(state: SessionState) {
    this.state = state;
    this.mgr.changed();
  }

  /**
   * The first attach from this window: straight away for a terminal this tab
   * has shown, else only once a fresh listing says no other window shows it
   * — one that does is drawn as "in another window · Show it here".
   */
  private async firstAttach() {
    if (this.checking) return;
    if (!this.mgr.claimed.has(this.id)) {
      this.checking = true;
      try {
        await this.mgr.freshList();
      } finally {
        this.checking = false;
      }
      if (this.disposed || this.state !== "idle" || !this.slot) return;
      if (this.mgr.elsewhere(this.id)) {
        this.mgr.changed();
        return;
      }
    }
    void this.attach();
  }

  async attach() {
    if (this.disposed || this.ws || this.state === "closed") return;
    this.wanted = true;
    this.clearRetry();
    this.set("connecting");
    let ticket: string;
    try {
      // A fresh ticket before every attach: single use, 30 s, this terminal only.
      ticket = (await api.terminalTicket(this.id)).ticket;
    } catch (e: any) {
      if (this.disposed || !this.wanted) return;
      if (e?.status === 404 || e?.status === 400) {
        this.end("gone");
        return;
      }
      this.scheduleReconnect();
      return;
    }
    if (this.disposed || !this.wanted || this.ws) return;
    let ws: WebSocket;
    try {
      ws = new WebSocket(attachUrl(window.location, this.id, ticket));
    } catch {
      this.scheduleReconnect();
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    // A new generation: whatever an older socket still has queued is never
    // parsed here, and nothing is sent until this socket's replay is parsed.
    const gen = this.out.next();
    this.gen = gen;
    this.replaying = true;
    // A queue of this socket's own, unlatched. Nothing of an older socket's
    // paste is ever offered to it.
    const stale = this.gate.open();
    if (stale) this.notePaste("lost", stale);
    this.replayed = false;
    this.sawAttached = false;
    this.lastSize = null;
    this.mgr.ledger.mine(this.id, Date.now());
    ws.onmessage = (ev) => this.onMessage(ws, gen, ev);
    ws.onclose = () => this.onClose(ws);
    ws.onerror = () => {};
  }

  /** Ask again, after "taken" or "refused" (the window showing it is asked). */
  retry() {
    if (this.disposed || this.ws) return;
    this.attempts = 0;
    this.refused = "";
    void this.attach();
  }

  /** A new socket (the way out of a program that never reads: the latch is per socket). */
  reattach() {
    if (this.disposed) return;
    const ws = this.ws;
    this.attempts = 0;
    if (!ws) {
      void this.attach();
      return;
    }
    this.reattachNow = true;
    try {
      ws.close(1000, "reattach");
    } catch {
      this.onClose(ws);
    }
  }

  private reattachNow = false;

  private onClose(ws: WebSocket) {
    if (ws !== this.ws) return;
    this.ws = null;
    this.stopPump();
    this.clearResume();
    if (!this.sawAttached) this.mgr.ledger.failed(this.id);
    // Never continued onto a new socket: what was not sent dies here.
    const lost = this.gate.close();
    if (lost > 0) this.notePaste("lost", lost);
    else if (this.paste && this.paste.kind === "dropped") this.paste = { ...this.paste, resuming: false };
    if (this.disposed || unloading) return;
    if (this.reattachNow) {
      this.reattachNow = false;
      void this.attach();
      return;
    }
    if (["taken", "refused", "ended", "closed"].includes(this.state)) {
      this.mgr.changed();
      return;
    }
    if (this.wanted) this.scheduleReconnect();
    else this.mgr.changed();
  }

  private scheduleReconnect() {
    this.clearRetry();
    if (this.disposed || unloading) return;
    if (this.attempts >= RECONNECT_MS.length) {
      this.set("lost");
      void this.mgr.refresh();
      return;
    }
    const delay = RECONNECT_MS[this.attempts++];
    this.set("connecting");
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.attach();
    }, delay);
  }

  private clearRetry() {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private end(why: "gone" | "ended") {
    this.wanted = false;
    this.endedWhy = why;
    this.set("ended");
    this.mgr.ended(this.id);
  }

  /** "ended": Jarvis restarted (the socket said so). "gone": the terminal no longer exists. */
  endedWhy: "gone" | "ended" = "ended";

  private onMessage(ws: WebSocket, gen: number, ev: MessageEvent) {
    if (ws !== this.ws) return;
    if (typeof ev.data !== "string") {
      this.out.data(gen, new Uint8Array(ev.data as ArrayBuffer));
      return;
    }
    const msg = parseControl(ev.data);
    if (!msg) return;
    switch (msg.type) {
      case "attached":
        this.sawAttached = true;
        this.attempts = 0;
        this.mgr.claim(this.id);
        // The replay that follows is the terminal's whole recent output: start
        // clean. In band (RIS), so it lands after anything still being parsed
        // and before the replay; a reset() here would let output xterm already
        // holds be drawn again after it. Nothing is sent until "replayed".
        this.replaying = true;
        this.out.data(gen, RIS);
        if (msg.terminal) {
          this.mgr.updateRow(msg.terminal);
          this.lastRow = msg.terminal;
          this.marked = this.marked || msg.terminal.marked;
        }
        this.exitCode = null;
        this.refused = "";
        this.waitingUntil = 0;
        this.attachedAt = Date.now();
        this.set("attached");
        return;
      case "replayed":
        // Once the replay has been parsed — every query in it answered into a
        // session that was not listening — input flows again. Then a resize,
        // so a full-screen program redraws at this window's size.
        this.out.then(gen, () => {
          if (gen !== this.gen || ws !== this.ws) return;
          this.replaying = false;
          this.replayed = true;
          this.fitNow();
          this.sendResize(true);
        });
        return;
      case "exit":
        this.exitCode = msg.code;
        if (msg.reason === "ended") {
          this.end("ended");
        } else if (msg.reason === "closed") {
          this.wanted = false;
          this.set("closed");
          this.mgr.gone(this.id);
        } else {
          this.set("exited");
          this.mgr.exited(this.id);
        }
        return;
      case "takeover_request": {
        const id = msg.id;
        this.takeover = { id, until: Date.now() + msg.timeout_s * 1000 };
        setTimeout(() => {
          if (this.takeover?.id === id) {
            this.takeover = null;
            this.mgr.changed();
          }
        }, msg.timeout_s * 1000 + 250);
        this.mgr.changed();
        return;
      }
      case "waiting":
        this.waitingUntil = Date.now() + msg.timeout_s * 1000;
        this.set("waiting");
        return;
      case "taken":
        this.wanted = false;
        this.takeover = null;
        // Another window has it now: a reload here asks before taking it back.
        this.mgr.unclaim(this.id);
        this.set("taken");
        return;
      case "refused":
        this.wanted = false;
        this.refused = msg.reason;
        this.mgr.unclaim(this.id);
        this.set("refused");
        return;
      case "input_dropped": {
        // Stop the rest of the paste at once. The frames already on their way
        // are refused too (the daemon latched), and counted here, once.
        const rest = this.gate.dropped();
        this.stopPump();
        // One notice per incident: a frame that was already on its way when
        // the first drop was said is refused too, and only adds its bytes —
        // it never undoes a resume the owner has already asked for.
        const prev = this.paste && this.paste.kind === "dropped" ? this.paste : null;
        this.paste = prev
          ? { ...prev, bytes: prev.bytes + rest + msg.bytes }
          : { kind: "dropped", bytes: rest + msg.bytes, resuming: false, refused: "" };
        this.mgr.changed();
        return;
      }
      case "input_resumed":
        this.gate.resumed();
        this.clearResume();
        if (this.paste?.kind === "dropped") this.paste = null;
        this.mgr.changed();
        return;
      case "input_resume_refused":
        if (this.paste) this.paste = { ...this.paste, refused: msg.reason };
        if (this.paste?.resuming) {
          this.clearResume();
          this.resumeTimer = setTimeout(() => this.sendResume(), RESUME_RETRY_MS);
        }
        this.mgr.changed();
        return;
      case "marked":
        this.marked = true;
        this.mgr.markRow(this.id);
        return;
    }
  }

  answerTakeover(allow: boolean) {
    const t = this.takeover;
    if (!t || !this.ws || this.mgr.blocked) return;
    this.takeover = null;
    try {
      this.ws.send(JSON.stringify({ type: "takeover", id: t.id, allow }));
    } catch {
      /* the socket went: the other window is not asked again */
    }
    this.mgr.changed();
  }

  private sendResize(force: boolean) {
    const ws = this.ws;
    const term = this.term;
    if (!ws || !term || ws.readyState !== WebSocket.OPEN || !this.replayed) return;
    const size = clampSize(term.cols, term.rows);
    if (!size) return;
    if (!force && this.lastSize && this.lastSize.cols === size.cols && this.lastSize.rows === size.rows) return;
    this.lastSize = size;
    ws.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
  }

  // -- input ----------------------------------------------------------------

  private input(bytes: Uint8Array) {
    // Held under a card: a `y⏎` typed at the wrong moment goes nowhere.
    if (this.mgr.blocked || this.disposed) return;
    // Not connected, or the replay is still being parsed: nothing is sent —
    // not a query's answer, and not a key. A key or a paste is said.
    if (this.state !== "attached" || this.replaying) {
      if (this.gesture) this.noteUnsent(bytes.length);
      return;
    }
    if (this.gesture && this.paste?.kind === "unsent") {
      this.paste = null;                      // typing reaches the shell again: the notice has done its job
      this.mgr.changed();
    }
    const r = this.gate.input(bytes);
    if (r.send && this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(r.send);
    if (r.dropped) this.notePaste("stopped", r.dropped);
    if (r.held) {
      if (this.gate.latched) {
        // Typing is paused: the notice (and its Resume) is on screen whenever
        // a key is held, never a key swallowed without a word.
        this.heldAt = Date.now();
        if (!this.paste || this.paste.kind !== "dropped") {
          this.paste = { kind: "dropped", bytes: 0, resuming: false, refused: "" };
        }
        this.mgr.changed();
      }
      return;
    }
    this.pump();
  }

  private pump = () => {
    this.pumpTimer = null;
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (this.mgr.blocked) {
      this.pumpSoon(100);                               // a paste waits out a card
      return;
    }
    if (ws.bufferedAmount > HIGH_WATER) {
      this.pumpSoon(PACE_MS);
      return;
    }
    const piece = this.gate.next();
    if (!piece) return;
    ws.send(piece);
    if (this.gate.pending) this.pumpSoon(PACE_MS);
  };

  private pumpSoon(ms: number) {
    if (this.pumpTimer || !this.gate.pending) return;
    this.pumpTimer = setTimeout(this.pump, ms);
  }

  private stopPump() {
    if (this.pumpTimer) clearTimeout(this.pumpTimer);
    this.pumpTimer = null;
  }

  private notePaste(kind: PasteNotice["kind"], bytes: number) {
    this.paste = { kind, bytes, resuming: false, refused: "" };
    this.mgr.changed();
  }

  private noteUnsent(bytes: number) {
    // Never over the notice that holds the only way to resume.
    if (this.paste?.kind === "dropped" && this.gate.latched) return;
    const why = ["connecting", "waiting", "attached"].includes(this.state) ? "connecting" : "offline";
    const prev = this.paste?.kind === "unsent" ? this.paste : null;
    this.paste = { kind: "unsent", bytes: (prev?.bytes ?? 0) + bytes, resuming: false, refused: "", why };
    this.mgr.changed();
  }

  /** The owner read the notice and wants to type again: ask the daemon to resume. */
  resume() {
    if (!this.gate.latched || !this.ws) {
      this.paste = null;
      this.mgr.changed();
      return;
    }
    if (this.paste) this.paste = { ...this.paste, resuming: true };
    this.sendResume();
    this.mgr.changed();
  }

  private sendResume() {
    this.resumeTimer = null;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.gate.latched) return;
    this.ws.send(JSON.stringify({ type: "input_resume" }));
  }

  private clearResume() {
    if (this.resumeTimer) clearTimeout(this.resumeTimer);
    this.resumeTimer = null;
  }

  /** The window came back from the back/forward cache: attach again if it was. */
  revive() {
    if (this.wanted && !this.ws && !this.disposed) this.retry();
  }

  /** The window is going: a normal close, nothing more. */
  leave() {
    const ws = this.ws;
    if (!ws) return;
    try {
      ws.close(1000, "window closed");
    } catch {
      /* already closing */
    }
  }

  dismissPaste() {
    this.clearResume();
    if (this.paste) this.paste = null;
    this.mgr.changed();
  }

  dispose() {
    this.disposed = true;
    this.wanted = false;
    this.stopPump();
    this.clearRetry();
    this.clearResume();
    if (this.gestureTimer) clearTimeout(this.gestureTimer);
    this.gestureTimer = null;
    this.out.clear();
    const ws = this.ws;
    this.ws = null;
    this.gate.close();
    if (ws) {
      try {
        ws.close(1000, "closed");
      } catch {
        /* already gone */
      }
    }
    for (const s of this.subs) s.dispose();
    this.subs = [];
    this.term?.dispose();
    this.term = null;
    this.wrap.remove();
  }
}

// ---------------------------------------------------------------------------
// every terminal in this window

export interface AttachNotice {
  key: number;
  terminalId: string;
  title: string;
  at: string;
}

/** HH:MM, local, from the event's ISO time (else now). */
function clock(iso: string): string {
  const d = new Date(iso);
  const t = Number.isFinite(d.getTime()) ? d : new Date();
  return t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export class TermManager {
  rows: TerminalRow[] = [];
  loaded = false;
  listError = "";
  /** A refused create or close, in the daemon's words. */
  error = "";
  sessions = new Map<string, TermSession>();
  /** Attaches this window did not make (`terminal_attached`): quiet, dismissible. */
  notices: AttachNotice[] = [];
  /** The last `terminal_read` per terminal (WP-F): "Jarvis read 200 lines · 15:42" in its bar. */
  reads = new Map<string, ReadNote>();
  /** A busy terminal the owner asked to close: asked once more. `unsure`: the listing failed. */
  confirm: { id: string; title: string; unsure: boolean } | null = null;
  /**
   * The terminals this tab has shown (sessionStorage; lib/terminal.ts,
   * MINE_KEY): inherited only by a reload of the page that released them,
   * then held under this load's nonce at once, so a copy made from now on is
   * a copy.
   */
  claimed: Set<string> = loadMine();

  constructor() {
    if (typeof window !== "undefined") saveMine(this.claimed, PAGE);
  }

  /** The page is going: the list is released, for a reload of this page to take up. */
  release() {
    saveMine(this.claimed, null);
  }

  /** The page came back (back/forward cache): the list is held again. */
  hold() {
    saveMine(this.claimed, PAGE);
  }
  blocked = false;
  zoom = 100;
  panelOpen = false;
  ledger = new AttachLedger();
  prefs = loadPrefs();
  /** Which drawn pane shows which terminal (lib/terminal.ts, placeTerminals). */
  place = new Map<string, number>();
  /** Exited while nobody could see it: the dot on ⬓. */
  private unseenExits = new Set<string>();
  private noticeSeq = 0;
  private version = 0;
  private listeners = new Set<() => void>();
  private refreshing: Promise<void> | null = null;

  // Set by the workspace (Workspace.tsx, useTerminalBridge).
  defaultIn: () => InSpec = () => "home";
  setPaneTerminal: (pane: PaneNo, id: string | null) => void = () => {};
  forgetTerminal: (id: string) => void = () => {};
  focusPane: (pane: PaneNo) => void = () => {};
  showPanel: () => void = () => {};

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  getVersion = () => this.version;

  changed() {
    this.version++;
    for (const fn of Array.from(this.listeners)) fn();
  }

  session(id: string): TermSession {
    let s = this.sessions.get(id);
    if (!s) {
      s = new TermSession(id, this);
      this.sessions.set(id, s);
    }
    return s;
  }

  row(id: string): TerminalRow | null {
    return this.rows.find((r) => r.id === id) ?? null;
  }

  updateRow(row: TerminalRow) {
    this.rows = this.rows.some((r) => r.id === row.id)
      ? this.rows.map((r) => (r.id === row.id ? row : r))
      : [...this.rows, row];
  }

  markRow(id: string) {
    this.rows = this.rows.map((r) => (r.id === id ? { ...r, marked: true } : r));
    this.changed();
  }

  /** Whether a terminal is in view: a drawn pane holds it, or it is the open panel's tab. */
  visible(id: string): boolean {
    return this.place.has(id) || (this.panelOpen && this.panelActive() === id);
  }

  /**
   * The panel's tabs: every terminal the daemon lists, and — until the owner
   * acts on it — one this window was showing that ended with Jarvis or was
   * lost, so its "ended" strip and "New terminal here" stay where they were.
   */
  tabs(): TerminalRow[] {
    const out = this.rows.slice();
    for (const s of this.sessions.values()) {
      if ((s.state === "ended" || s.state === "lost") && !out.some((r) => r.id === s.id)) {
        out.push(s.lastRow ?? { ...GONE_ROW, id: s.id, title: `terminal ${s.id}` });
      }
    }
    return out;
  }

  /**
   * The panel's tab: the stored one while it exists, else the first not shown
   * in a pane — one of this window's own before one another window shows.
   */
  panelActive(): string | null {
    const tabs = this.tabs();
    const a = this.prefs.active;
    if (a && tabs.some((r) => r.id === a)) return a;
    const free = tabs.find((r) => !this.place.has(r.id) && !this.elsewhere(r.id))
      ?? tabs.find((r) => !this.place.has(r.id));
    return (free ?? tabs[0])?.id ?? null;
  }

  claim(id: string) {
    if (this.claimed.has(id)) return;
    this.claimed.add(id);
    saveMine(this.claimed, PAGE);
    this.changed();
  }

  unclaim(id: string) {
    if (!this.claimed.delete(id)) return;
    saveMine(this.claimed, PAGE);
    this.changed();
  }

  /**
   * Listed as shown, and not by this tab: another window has it. Never
   * attached to unasked — taking it would put a takeover question in front
   * of the owner there that nothing they did here caused.
   */
  elsewhere(id: string): boolean {
    const r = this.row(id);
    if (!r || !r.shown || this.claimed.has(id)) return false;
    const s = this.sessions.get(id);
    return !s || s.state === "idle";
  }

  /** "Show it here": the owner's choice. The window showing it is asked. */
  showHere(id: string) {
    if (this.blocked) return;
    this.claim(id);
  }

  selectTab(id: string) {
    this.prefs = { ...this.prefs, active: id };
    savePrefs(this.prefs);
    this.changed();
  }

  get dot(): boolean {
    return !this.panelOpen && this.unseenExits.size > 0;
  }

  // -- the workspace's settings, from an effect (never during a render) ----

  setBlocked(blocked: boolean) {
    if (this.blocked === blocked) return;
    this.blocked = blocked;
    if (blocked) this.confirm = null;
    for (const s of this.sessions.values()) s.setBlocked(blocked);
    this.changed();
  }

  setZoom(zoom: number) {
    if (this.zoom === zoom) return;
    this.zoom = zoom;
    for (const s of this.sessions.values()) s.applyZoom();
  }

  setPanelOpen(open: boolean) {
    if (this.panelOpen === open) return;
    this.panelOpen = open;
    if (open) {
      this.unseenExits.clear();
      void this.refresh();
    }
    this.changed();
  }

  setPlace(place: Map<string, number>) {
    const same = place.size === this.place.size && [...place].every(([k, v]) => this.place.get(k) === v);
    if (same) return;
    this.place = place;
    for (const id of place.keys()) this.unseenExits.delete(id);
    this.changed();
  }

  // -- the daemon -----------------------------------------------------------

  refresh(): Promise<void> {
    if (!this.refreshing) {
      this.refreshing = (async () => {
        try {
          this.rows = await api.terminals();
          this.loaded = true;
          this.listError = "";
          const live = new Set(this.rows.map((r) => r.id));
          // A terminal this window has never drawn that ended while it was not looking.
          for (const r of this.rows) {
            if (r.exited && !this.sessions.has(r.id) && !this.visible(r.id) && !this.panelOpen) {
              this.unseenExits.add(r.id);
            }
          }
          for (const id of Array.from(this.unseenExits)) if (!live.has(id)) this.unseenExits.delete(id);
        } catch (e: any) {
          this.listError = e?.message || "the terminals could not be listed";
        } finally {
          this.refreshing = null;
          this.changed();
        }
      })();
    }
    return this.refreshing;
  }

  /**
   * A listing asked for now — never one already on its way, which may have
   * left before the change the caller is about to act on. True if it came.
   */
  async freshList(): Promise<boolean> {
    const before = this.refreshing;
    if (before) await before;
    if (this.refreshing && this.refreshing !== before) await this.refreshing;    // left after the ask
    else await this.refresh();
    return !this.listError;
  }

  /** Where it was opened, so Restart and "New terminal here" reopen it there. */
  specOf(id: string): InSpec {
    const kept = this.prefs.specs[id];
    if (kept) return kept;
    const row = this.row(id);
    return row?.project_id ? { project: row.project_id } : "home";
  }

  private remember(id: string, spec: InSpec) {
    const specs = { ...this.prefs.specs, [id]: spec };
    const ids = Object.keys(specs);
    for (const old of ids.slice(0, Math.max(0, ids.length - SPECS_KEPT))) delete specs[old];
    this.prefs = { ...this.prefs, specs };
    savePrefs(this.prefs);
  }

  /** A new terminal, shown in the panel or in a pane. Null when refused (said in `error`). */
  async create(spec: InSpec, where: "panel" | PaneNo): Promise<string | null> {
    if (this.blocked) return null;
    this.error = "";
    let row: TerminalRow;
    try {
      row = await api.createTerminal({ in: spec });
    } catch (e: any) {
      this.error = e?.message || "the terminal could not be opened";
      this.changed();
      return null;
    }
    this.updateRow(row);
    this.claimed.add(row.id);
    saveMine(this.claimed, PAGE);
    this.remember(row.id, spec);
    if (where === "panel") {
      this.prefs = { ...this.prefs, active: row.id };
      savePrefs(this.prefs);
      this.showPanel();
    } else {
      this.setPaneTerminal(where, row.id);
    }
    this.changed();
    return row.id;
  }

  /** Ctrl+` opened the panel: like VS Code, a terminal if there is none. */
  async keyOpened() {
    if (this.blocked) return;
    await this.refresh();
    if (this.loaded && !this.listError && this.rows.length === 0) await this.create(this.defaultIn(), "panel");
  }

  /**
   * ×: closed at once, unless something is running in it — then asked first.
   * `busy` is read from a listing fetched now (a stale one could skip the
   * question); if none comes, it asks anyway, saying it could not check.
   */
  async requestClose(id: string) {
    if (this.blocked) return;
    const listed = await this.freshList();
    if (this.blocked) return;
    const row = this.row(id);
    const s = this.sessions.get(id);
    const gone = s && (s.state === "ended" || s.state === "closed");
    if (row && !row.exited && !gone && (row.busy || !listed)) {
      this.confirm = { id, title: row.title || id, unsure: !listed };
      this.changed();
      return;
    }
    await this.close(id);
  }

  async close(id: string) {
    if (this.blocked) return;
    this.confirm = null;
    this.error = "";
    const s = this.sessions.get(id);
    const known = this.rows.some((r) => r.id === id);
    if (known || (s && s.state !== "ended" && s.state !== "closed")) {
      try {
        await api.closeTerminal(id);
      } catch (e: any) {
        if (e?.status !== 404) {
          this.error = e?.message || "the terminal could not be closed";
          this.changed();
          return;
        }
      }
    }
    this.gone(id);
  }

  keep() {
    this.confirm = null;
    this.changed();
  }

  /** Closed, here or by another window: its views go, and its panes choose again. */
  gone(id: string) {
    const s = this.sessions.get(id);
    this.sessions.delete(id);
    s?.dispose();
    this.rows = this.rows.filter((r) => r.id !== id);
    this.unseenExits.delete(id);
    if (this.claimed.delete(id)) saveMine(this.claimed, PAGE);
    const specs = { ...this.prefs.specs };
    delete specs[id];
    this.prefs = { active: this.prefs.active === id ? null : this.prefs.active, specs };
    savePrefs(this.prefs);
    this.forgetTerminal(id);
    this.changed();
  }

  exited(id: string) {
    if (!this.visible(id)) this.unseenExits.add(id);
    void this.refresh();
    this.changed();
  }

  ended(id: string) {
    this.unseenExits.delete(id);
    const row = this.row(id);
    const s = this.sessions.get(id);
    if (s && row) s.lastRow = row;
    this.rows = this.rows.filter((r) => r.id !== id);
    this.changed();
  }

  /** Exited: the same folder again, in the same place. */
  async restart(id: string) {
    if (this.blocked) return;
    const spec = this.specOf(id);
    const pane = this.place.get(id) as PaneNo | undefined;
    await this.close(id);
    await this.create(spec, pane ?? "panel");
  }

  /** Ended with Jarvis (or gone): a new one where it was. */
  async newHere(id: string) {
    if (this.blocked) return;
    const spec = this.specOf(id);
    const pane = this.place.get(id) as PaneNo | undefined;
    this.gone(id);
    await this.create(spec, pane ?? "panel");
  }

  async setReadable(id: string, readable: boolean) {
    if (this.blocked) return;
    try {
      const row = await api.setTerminalReadable(id, readable);
      if (row) this.updateRow(row);
      this.error = "";
    } catch (e: any) {
      this.error = e?.message || "the switch could not be changed";
    }
    this.changed();
  }

  /** `terminal_attached` on the bus: said quietly when it was not this window's own. */
  heardAttached(data: unknown) {
    const d = (data ?? {}) as Record<string, unknown>;
    const id = d.terminal_id;
    if (!isTerminalId(id)) return;
    if (this.ledger.heard(id, Date.now())) return;
    const title = this.row(id)?.title || `terminal ${id}`;
    const at = clock(typeof d.at === "string" ? d.at : "");
    this.notices = [...this.notices.filter((n) => n.terminalId !== id),
      { key: ++this.noticeSeq, terminalId: id, title, at }].slice(-3);
    void this.refresh();
    this.changed();
  }

  /**
   * `terminal_read` on the bus: Jarvis read this terminal (or was refused).
   * HUD chrome only — the note never touches the terminal's stream.
   */
  heardRead(data: unknown) {
    const note = parseRead(data);
    if (!note) return;
    this.reads = new Map(this.reads).set(note.terminalId, note);
    this.changed();
  }

  dismissNotice(key: number) {
    this.notices = this.notices.filter((n) => n.key !== key);
    this.changed();
  }

  /** A link in a terminal: http(s) only, on Ctrl+click. Everything else is inert. */
  openLink(e: MouseEvent, uri: string) {
    const v = judgeLink(uri);
    if (!v.ok || this.blocked) return;
    if (!(e.ctrlKey || e.metaKey)) return;
    window.open(v.url, "_blank", "noopener,noreferrer");
  }
}

/** This window's terminals. */
export const terminals = new TermManager();

/** The SSE `terminal_attached` record (App's one event stream). */
export function terminalAttached(data: unknown) {
  terminals.heardAttached(data);
}

/** The SSE `terminal_read` record (App's one event stream). */
export function terminalRead(data: unknown) {
  terminals.heardRead(data);
}

if (typeof window !== "undefined") {
  // Read-only hooks for the headless checks: what a terminal shows and its size.
  (window as any).__hudTerminals = {
    ids: () => Array.from(terminals.sessions.keys()),
    state: (id: string) => terminals.sessions.get(id)?.state ?? null,
    text: (id: string) => terminals.sessions.get(id)?.bufferText() ?? "",
    selection: (id: string) => terminals.sessions.get(id)?.selection ?? "",
    size: (id: string) => terminals.sessions.get(id)?.size ?? null,
    latched: (id: string) => terminals.sessions.get(id)?.latched ?? null,
    backlog: (id: string) => terminals.sessions.get(id)?.backlog ?? null,
  };
}

export function useTerminals(): TermManager {
  useSyncExternalStore(terminals.subscribe, terminals.getVersion);
  return terminals;
}

/** The dot on ⬓: a terminal in the hidden panel has exited. */
export function usePanelDot(): boolean {
  useSyncExternalStore(terminals.subscribe, terminals.getVersion);
  return terminals.dot;
}

// ---------------------------------------------------------------------------
// views

function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [active, ms]);
  return now;
}

/**
 * One terminal: its bar (the name — a title the program set, as text — the
 * integration note, the "Jarvis can read" switch, ×), a notice when a paste
 * was stopped, the xterm itself, and a strip when it is not running.
 */
export function TerminalView(props: { id: string; where: string; pane?: PaneNo; onChoose?: () => void }) {
  const mgr = useTerminals();
  const s = mgr.session(props.id);
  const slot = useRef<HTMLDivElement>(null);
  // Another window shows it: drawn as a choice, never attached to unasked.
  const away = mgr.elsewhere(props.id);

  useLayoutEffect(() => {
    const el = slot.current;
    if (!el) return;
    s.mount(el);
    let frame = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => s.fitNow());
    });
    ro.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      s.unmount(el);
    };
  }, [s, away]);

  const now = useNow(s.state === "waiting" || (s.attachedAt > 0 && Date.now() - s.attachedAt < SETTLE_MS + 1000));
  // The daemon's row while the terminal exists; its last one only for a name after.
  const live = mgr.row(props.id);
  const row = live ?? s.lastRow;
  const settled = s.attachedAt > 0 && now - s.attachedAt >= SETTLE_MS;
  const note = integrationNote(live ? { integration: live.integration, marked: s.marked || live.marked } : null,
                               settled);
  const readable = row ? row.readable : true;
  const read = mgr.reads.get(props.id);
  const name = s.title || row?.title || `terminal ${props.id}`;
  const blocked = mgr.blocked;

  return (
    <div className="termview" data-testid={`terminal-${props.id}`} data-state={s.state} data-where={props.where}>
      <div className="termbar">
        <span className="termname" data-testid="term-name" title={row?.folder ? `${name} — ${row.folder}` : name}>
          {name}
        </span>
        {note ? (
          <span className={"termbadge " + note.kind} data-testid="term-integration" data-kind={note.kind}
                title={note.title}>
            {note.badge}
          </span>
        ) : null}
        {read ? (
          <span className={"termreadnote" + (read.refused ? " refused" : "")} data-testid="term-read-note"
                data-refused={read.refused ? "true" : "false"} role="status"
                title={read.refused
                  ? "Jarvis asked to read this terminal and was refused: reading is off, or the output may hold a credential"
                  : "Jarvis read this terminal's recent output (text only; it never types here)"}>
            {readNoteText(read, clock(read.at))}
          </span>
        ) : null}
        <button
          type="button"
          className={"termread" + (readable ? " on" : "")}
          data-testid="term-readable"
          aria-pressed={readable}
          disabled={blocked || !live}
          title={readable
            ? "Jarvis can read this terminal's recent output (a read never includes a recognisable secret). Click to stop."
            : "Jarvis cannot read this terminal. Click to allow."}
          onClick={() => void mgr.setReadable(props.id, !readable)}
        >
          {readable ? "Jarvis can read" : "Jarvis can't read"}
        </button>
        <button
          type="button"
          className="collapse"
          data-testid="term-close"
          title="Close this terminal"
          aria-label="Close this terminal"
          disabled={blocked}
          onClick={() => void mgr.requestClose(props.id)}
        >
          ×
        </button>
      </div>
      {away ? (
        <div className="termstrip" data-testid="term-elsewhere">
          <span>This terminal is in another window.</span>
          <button type="button" data-testid="term-show-here" disabled={blocked} onClick={() => mgr.showHere(props.id)}
                  title="Show it here; the window showing it is asked first">
            Show it here
          </button>
        </div>
      ) : (
        <>
          {s.paste ? <PasteStrip s={s} blocked={blocked} /> : null}
          {s.skipped ? (
            <div className="termnote" data-testid="term-output-skipped" role="status">
              <span className="termnote-text">
                Output came faster than this window could draw it, so it skipped ahead ({kib(s.skipped)} not
                drawn): what is shown is the terminal's latest output.
              </span>
              <button type="button" className="quiet" data-testid="term-output-skipped-dismiss" aria-label="Dismiss"
                      onClick={() => s.dismissSkipped()}>
                ×
              </button>
            </div>
          ) : null}
          <div className="termslot" ref={slot} onMouseDown={() => setTimeout(() => s.focus(), 0)} />
          <StateStrip s={s} now={now} blocked={blocked} onChoose={props.onChoose} />
        </>
      )}
    </div>
  );
}

function PasteStrip(props: { s: TermSession; blocked: boolean }) {
  const { s, blocked } = props;
  const p = s.paste!;
  const what =
    p.kind === "dropped"
      ? (p.bytes > 0
        ? `Paste stopped: the program in this terminal is not reading its input, so the rest of it (${kib(p.bytes)}) `
          + "was not sent. Typing is paused until you resume."
        : "The program in this terminal is not reading its input: typing is paused until you resume.")
      : p.kind === "lost"
        ? `The connection closed during a paste: the rest of it (${kib(p.bytes)}) was not sent, and will not be.`
        : p.kind === "unsent"
          ? `This terminal ${p.why === "offline" ? "is not running here" : "was still connecting"}, so what you `
            + `typed or pasted (${kib(p.bytes)}) was not sent.`
          : `Ctrl-C stopped the paste: the rest of it (${kib(p.bytes)}) was not sent.`;
  return (
    <div className="termnote" data-testid="term-paste-notice" data-kind={p.kind} data-held={s.heldAt ? "true" : undefined}
         role="status">
      <span className="termnote-text">
        {what}
        {p.kind === "dropped" && p.resuming
          ? " Waiting for the program to read what was already sent… If it never does, reattach or close the terminal."
          : ""}
      </span>
      {p.kind === "dropped" && s.latched ? (
        <>
          <button type="button" data-testid="term-resume" disabled={blocked || p.resuming} onClick={() => s.resume()}>
            Resume typing
          </button>
          <button type="button" className="quiet" data-testid="term-reattach" disabled={blocked}
                  onClick={() => s.reattach()}>
            Reattach
          </button>
        </>
      ) : null}
      {/* While typing is paused the notice stays: it holds the only way to resume. */}
      {p.kind === "dropped" && s.latched ? null : (
        <button type="button" className="quiet" data-testid="term-paste-dismiss" aria-label="Dismiss"
                onClick={() => s.dismissPaste()}>
          ×
        </button>
      )}
    </div>
  );
}

function StateStrip(props: { s: TermSession; now: number; blocked: boolean; onChoose?: () => void }) {
  const { s, now, blocked } = props;
  const mgr = terminals;
  switch (s.state) {
    case "waiting":
      return (
        <div className="termstrip" data-testid="term-waiting">
          Asking the window that shows this terminal… {Math.max(0, Math.ceil((s.waitingUntil - now) / 1000))}s
        </div>
      );
    case "refused":
      return (
        <div className="termstrip" data-testid="term-refused">
          <span>{s.refused || "The window showing this terminal kept it."}</span>
          <button type="button" data-testid="term-retry" disabled={blocked} onClick={() => s.retry()}>Ask again</button>
        </div>
      );
    case "taken":
      return (
        <div className="termstrip" data-testid="term-taken">
          <span>Another window took this terminal.</span>
          <button type="button" data-testid="term-take-back" disabled={blocked} onClick={() => s.retry()}>
            Take it back
          </button>
        </div>
      );
    case "exited":
      return (
        <div className="termstrip" data-testid="term-exited">
          <span>exited (code {s.exitCode ?? "?"})</span>
          <button type="button" data-testid="term-restart" disabled={blocked} onClick={() => void mgr.restart(s.id)}>
            Restart
          </button>
          <button type="button" data-testid="term-exit-close" disabled={blocked} onClick={() => void mgr.close(s.id)}>
            Close
          </button>
        </div>
      );
    case "ended":
      return (
        <div className="termstrip" data-testid="term-ended">
          <span>
            {s.endedWhy === "ended"
              ? "Jarvis restarted; this terminal ended."
              : "This terminal has ended (Jarvis restarted, or it was closed)."}
          </span>
          <button type="button" data-testid="term-new-here" disabled={blocked} onClick={() => void mgr.newHere(s.id)}>
            New terminal here
          </button>
          {props.onChoose ? (
            <button type="button" className="quiet" data-testid="term-choose" disabled={blocked} onClick={props.onChoose}>
              Choose another
            </button>
          ) : null}
        </div>
      );
    case "lost":
      return (
        <div className="termstrip" data-testid="term-lost">
          <span>The connection to this terminal was lost.</span>
          <button type="button" data-testid="term-retry" disabled={blocked} onClick={() => s.retry()}>Reconnect</button>
        </div>
      );
    case "connecting":
      return <div className="termstrip quiet" data-testid="term-connecting">connecting…</div>;
    default:
      return null;
  }
}

/** A pane showing the terminal view: its terminal, or the choice of one. */
export function TerminalPane(props: { pane: PaneNo; terminalId: string | null; drawn: boolean }) {
  const mgr = useTerminals();
  const id = props.terminalId;
  useEffect(() => {
    if (props.drawn && !mgr.loaded) void mgr.refresh();
  }, [props.drawn, mgr]);
  if (!props.drawn) return <div className="tabbody" />;
  if (id) {
    const at = mgr.place.get(id);
    if (at && at !== props.pane) {
      return (
        <div className="tabbody pad muted" data-testid={`pane-${props.pane}-terminal-elsewhere`}>
          This terminal is shown in pane {at}.{" "}
          <button type="button" className="quiet" onClick={() => mgr.focusPane(at as PaneNo)}>Go there</button>
        </div>
      );
    }
    return (
      <div className="tabbody">
        <TerminalView id={id} where={`pane-${props.pane}`} pane={props.pane}
                      onChoose={() => mgr.setPaneTerminal(props.pane, null)} />
      </div>
    );
  }
  const where = (r: TerminalRow) => {
    const n = mgr.place.get(r.id);
    if (n) return `in pane ${n}`;
    if (r.exited) return "exited";
    if (mgr.elsewhere(r.id)) return "in another window";
    return "in the panel";
  };
  return (
    <div className="tabbody termpick" data-testid={`pane-${props.pane}-terminal-pick`}>
      <div className="pad">
        <button type="button" data-testid={`pane-${props.pane}-terminal-new`} disabled={mgr.blocked}
                onClick={() => void mgr.create(mgr.defaultIn(), props.pane)}>
          + New terminal here
        </button>
      </div>
      {mgr.rows.length ? (
        <div className="termpick-list">
          {mgr.rows.map((r) => (
            <button
              type="button"
              key={r.id}
              className="termpick-row"
              data-testid={`pane-${props.pane}-pick-${r.id}`}
              disabled={mgr.blocked}
              onClick={() => mgr.setPaneTerminal(props.pane, r.id)}
            >
              <span className="termpick-name">{r.title || r.id}</span>
              <span className="muted">{where(r)}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="pad muted">No terminals are open.</div>
      )}
      {mgr.error ? <div className="pad err" data-testid="terminal-error">{mgr.error}</div> : null}
    </div>
  );
}

/** The panel's body: a tab per terminal, `+ ▾`, and the selected terminal. */
export function TerminalPanel(props: {
  open: boolean; blocked: boolean; projects: Project[]; zoom: number; onHide: () => void;
}) {
  const mgr = useTerminals();
  const [menu, setMenu] = useState<{ right: number; bottom: number } | null>(null);
  const menuBtn = useRef<HTMLButtonElement>(null);
  const active = mgr.panelActive();
  const tabs = mgr.tabs();
  useEffect(() => {
    if (props.blocked) setMenu(null);
  }, [props.blocked]);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setMenu(null);
      menuBtn.current?.focus();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc, true);
    };
  }, [menu]);

  const openIn = (spec: InSpec) => {
    setMenu(null);
    void mgr.create(spec, "panel");
  };

  return (
    <>
      <div className="panelhead">
        <span className="paneltitle">Terminal</span>
        <div className="paneltabs" role="tablist" aria-label="Terminals" data-testid="panel-tabs">
          {tabs.map((r) => {
            const pane = mgr.place.get(r.id);
            const s = mgr.sessions.get(r.id);
            const exited = r.exited || s?.state === "exited";
            const ended = s?.state === "ended" || s?.state === "lost";
            const away = mgr.elsewhere(r.id);
            return (
              <span key={r.id} className={"paneltab" + (r.id === active && !pane ? " on" : "")}>
                <button
                  type="button"
                  role="tab"
                  aria-selected={r.id === active && !pane}
                  data-testid={`panel-tab-${r.id}`}
                  data-place={pane ? `pane-${pane}` : "panel"}
                  title={pane ? `${r.title} — shown in pane ${pane}; click to go there` : r.title}
                  disabled={props.blocked}
                  onClick={() => (pane ? mgr.focusPane(pane as PaneNo) : mgr.selectTab(r.id))}
                >
                  <span className="paneltab-name">{r.title || r.id}</span>
                  {pane ? <span className="paneltab-where">in pane {pane}</span> : null}
                  {ended ? <span className="paneltab-where">ended</span>
                    : exited ? <span className="paneltab-where">exited</span>
                      : away ? <span className="paneltab-where">in another window</span> : null}
                </button>
                <button
                  type="button"
                  className="paneltab-x"
                  data-testid={`panel-close-${r.id}`}
                  title="Close this terminal"
                  aria-label={`Close ${r.title || r.id}`}
                  disabled={props.blocked}
                  onClick={() => void mgr.requestClose(r.id)}
                >
                  ×
                </button>
              </span>
            );
          })}
        </div>
        <button
          type="button"
          className="collapse"
          data-testid="panel-new"
          title="New terminal in the folder of what the focused pane shows"
          aria-label="New terminal"
          disabled={props.blocked}
          onClick={() => void mgr.create(mgr.defaultIn(), "panel")}
        >
          +
        </button>
        <button
          type="button"
          ref={menuBtn}
          className="collapse"
          data-testid="panel-new-menu"
          title="New terminal in…"
          aria-label="New terminal in a folder"
          aria-haspopup="menu"
          aria-expanded={!!menu}
          disabled={props.blocked}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            if (menu) {
              setMenu(null);
              return;
            }
            const r = e.currentTarget.getBoundingClientRect();
            setMenu({
              right: Math.max(4, Math.round(toCss(window.innerWidth - r.right, props.zoom))),
              bottom: Math.round(toCss(window.innerHeight - r.top, props.zoom)) + 2,
            });
          }}
        >
          ▾
        </button>
        <button
          type="button"
          className="collapse"
          data-testid="panel-hide"
          title="Hide the panel (Ctrl+`)"
          aria-label="Hide the panel"
          disabled={props.blocked}
          onClick={props.onHide}
        >
          ⌄
        </button>
      </div>
      {menu ? (
        <div
          className="laymenu termmenu"
          role="menu"
          aria-label="New terminal in"
          data-testid="panel-new-menu-list"
          style={{ right: menu.right, bottom: menu.bottom }}
          onMouseDown={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            if (e.code === "Space" || e.key === " ") e.stopPropagation();
          }}
          onKeyUp={(e) => {
            if (e.code === "Space" || e.key === " ") e.stopPropagation();
          }}
        >
          <div className="lm-head">New terminal in</div>
          <button type="button" role="menuitem" className="lm-row" data-testid="panel-new-home" onClick={() => openIn("home")}>
            <span className="lm-label">Home</span>
            <span className="lm-key">~</span>
          </button>
          {props.projects.map((p) => (
            <button
              type="button"
              key={p.id}
              role="menuitem"
              className="lm-row"
              data-testid={`panel-new-project-${p.id}`}
              onClick={() => openIn({ project: p.id })}
            >
              <span className="lm-label">{p.name}</span>
            </button>
          ))}
        </div>
      ) : null}
      {mgr.error ? <div className="pad err" data-testid="terminal-error">{mgr.error}</div> : null}
      <div className="panelbody termpanel">
        {!mgr.loaded && !mgr.listError ? (
          <div className="pad muted">…</div>
        ) : mgr.listError && !tabs.length ? (
          <div className="pad err" data-testid="panel-list-error">{mgr.listError}</div>
        ) : !tabs.length || !active ? (
          <div className="pad muted" data-testid="panel-empty">
            No terminals are open.{" "}
            <button type="button" className="quiet" data-testid="panel-empty-new" disabled={props.blocked}
                    onClick={() => void mgr.create(mgr.defaultIn(), "panel")}>
              New terminal
            </button>
          </div>
        ) : mgr.place.has(active) ? (
          <div className="pad muted" data-testid="panel-elsewhere">
            This terminal is shown in pane {mgr.place.get(active)}.{" "}
            <button type="button" className="quiet" data-testid="panel-jump"
                    onClick={() => mgr.focusPane(mgr.place.get(active) as PaneNo)}>
              Go there
            </button>
          </div>
        ) : props.open ? (
          // Only while the panel is open: a hidden panel attaches nothing
          // (a reload must not ask another window for a terminal nobody is
          // looking at here). Once attached, a session stays attached hidden.
          <TerminalView key={active} id={active} where="panel" />
        ) : null}
      </div>
    </>
  );
}

/**
 * The window-wide terminal prompts, bottom right of the centre: another
 * window asking for one of this window's terminals (Let it / Keep it), an
 * attach this window did not make, and "close a busy terminal?". Under a
 * card every button here is disabled; a takeover nobody answers is kept.
 */
export function TerminalToasts(props: { blocked: boolean }) {
  const mgr = useTerminals();
  const asks = Array.from(mgr.sessions.values()).filter((s) => s.takeover);
  const now = useNow(asks.length > 0);
  if (!asks.length && !mgr.notices.length && !mgr.confirm) return null;
  return (
    <div className="termtoasts" data-testid="term-toasts">
      {asks.map((s) => (
        <div key={s.id} className="termtoast ask" data-testid="term-takeover" data-terminal={s.id} role="alertdialog"
             aria-label="Another window wants this terminal">
          <span>
            Another window wants <b>{mgr.row(s.id)?.title || `terminal ${s.id}`}</b>
            {" · "}{Math.max(0, Math.ceil((s.takeover!.until - now) / 1000))}s
          </span>
          <button type="button" data-testid="term-let" disabled={props.blocked} onClick={() => s.answerTakeover(true)}>
            Let it
          </button>
          <button type="button" data-testid="term-keep" disabled={props.blocked} onClick={() => s.answerTakeover(false)}>
            Keep it
          </button>
        </div>
      ))}
      {mgr.confirm ? (
        <div className="termtoast" data-testid="term-close-confirm" role="alertdialog" aria-label="Close a busy terminal">
          <span>
            {mgr.confirm.unsure
              ? <>Jarvis could not check whether something is running in <b>{mgr.confirm.title}</b>. Close it anyway?</>
              : <>Something is running in <b>{mgr.confirm.title}</b>. Close it anyway?</>}
          </span>
          <button type="button" data-testid="term-close-yes" disabled={props.blocked}
                  onClick={() => void mgr.close(mgr.confirm!.id)}>
            Close it
          </button>
          <button type="button" data-testid="term-close-no" onClick={() => mgr.keep()}>Keep it</button>
        </div>
      ) : null}
      {mgr.notices.map((n) => (
        <div key={n.key} className="termtoast quiet" data-testid="term-attached-notice" data-terminal={n.terminalId}
             role="status">
          <span>
            <b>{n.title}</b> was attached by something other than this window · {n.at}
          </span>
          <button type="button" className="quiet" data-testid="term-attached-dismiss" aria-label="Dismiss"
                  onClick={() => mgr.dismissNotice(n.key)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
