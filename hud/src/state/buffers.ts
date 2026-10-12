// The editor's buffers (ED-2, editor plan §2.3–§2.4): one text model per file
// on disk, kept outside React, so a view that unmounts — a pane switched to
// chat, a project re-picked, the workspace's error boundary — never takes an
// edit with it.
//
//  - **Keyed by the file's absolute path** (`abs`, from the read route), so
//    one file opened from two projects (the Inbox is rooted at `~`) is one
//    buffer, never two that can diverge. Two panes showing it share the
//    model: the text, the dirty dot and the undo stack; each keeps its own
//    view state.
//  - **A buffer saves through the project it was read through**, against the
//    etag it was read (or last saved) as: the daemon refuses with 409 when the
//    bytes on disk moved, and the buffer takes the conflict instead of
//    clobbering. There is no force-save anywhere.
//  - **Dirty means "not the version last read or saved"**: the model's
//    alternative version id at that moment is kept, so undoing back to the
//    saved text reads clean. The text itself is read once, when it is saved —
//    never per keystroke (#43's L3).
//  - **Polling** (`poll`, every 2 s from App while the window is visible, and
//    on focus) asks `POST …/files/stat`: a clean buffer whose file moved
//    reloads in place, undoably, with a toast naming the writer when the
//    activity feed saw a running turn touch it; an edited one gets the
//    conflict bar; a gone one, "Save to recreate" or "Close".
//  - **An env file's text** (`kind: "env"`, the owner's `.env*` rule) lives
//    here and in the owner's editor only: never in the bus, localStorage,
//    a log line, add-to-chat or any request but its own read and save.
//  - Unsaved text is never persisted. A model goes when no tab holds it and
//    it is clean; a dirty one no tab holds is kept (and `beforeunload` asks)
//    until it is reopened or discarded.

import type { FileRead } from "../types";
import {
  editableOf, eolOf, minimalEdit, parseKind, pollAction, TOUCHES_MAX, TOUCH_MS, writerOf,
  type Conflict, type Kind, type StatRow, type Touch,
} from "../lib/buffers";

/** An etag as the daemon writes one: 32 lowercase hex digits. */
const ETAG = /^[0-9a-f]{32}$/;

/** The part of a Monaco text model the registry uses (and the fallback's shim). */
export interface TextModel {
  getValue(): string;
  getAlternativeVersionId(): number;
  onDidChangeContent(fn: () => void): { dispose(): void };
  isDisposed(): boolean;
  dispose(): void;
  getLanguageId?(): string;
  getEOL?(): string;
  /** Monaco: replace a range undoably. The shim has `setValue` only. */
  pushEditOperations?(before: any, ops: any[], cursor: () => any): any;
  getPositionAt?(offset: number): { lineNumber: number; column: number };
  pushEOL?(eol: number): void;
  /** Monaco: close the undo element, so a reload is one step of its own. */
  pushStackElement?(): void;
  setValue?(text: string): void;
}

/** A text model with no Monaco behind it: the fallback textarea's. */
export class PlainModel implements TextModel {
  private value: string;
  private version = 1;
  private dead = false;
  private fns = new Set<() => void>();
  constructor(text: string) {
    this.value = text;
  }
  getValue() {
    return this.value;
  }
  getAlternativeVersionId() {
    return this.version;
  }
  setValue(text: string) {
    if (text === this.value) return;
    this.value = text;
    this.version += 1;
    for (const f of Array.from(this.fns)) f();
  }
  onDidChangeContent(fn: () => void) {
    this.fns.add(fn);
    return { dispose: () => void this.fns.delete(fn) };
  }
  isDisposed() {
    return this.dead;
  }
  dispose() {
    this.dead = true;
    this.fns.clear();
  }
  getEOL() {
    return this.value.includes("\r\n") ? "\r\n" : "\n";
  }
}

export interface WriteBody {
  path: string;
  content: string;
  expected_etag: string | null;
}

export interface WriteResult {
  etag: string;
  mtime?: number;
}

/** What the registry needs from the world: the three routes and a model maker. */
export interface BufferDeps {
  read(projectId: string, path: string): Promise<FileRead>;
  /** `signal` aborts a write that has not answered by WRITE_TIMEOUT_MS. */
  write(projectId: string, body: WriteBody, signal?: AbortSignal): Promise<WriteResult>;
  stat(projectId: string, paths: string[]): Promise<{ files: Record<string, StatRow> }>;
  makeModel(text: string, abs: string, kind: Kind): Promise<TextModel>;
  now(): number;
}

interface Buf {
  abs: string;
  projectId: string;
  path: string;
  kind: Kind;
  /** The daemon said the editor may save it (`editableOf`): never the kind alone. */
  editable: boolean;
  readonly: string | null;
  etag: string | null;
  encoding: string | null;
  eol: string | null;
  model: TextModel | null;
  saved: number;
  conflict: Conflict | null;
  saving: boolean;
  /** The save on its way, if any: a second Ctrl+S waits on it. */
  inflight: Promise<SaveOutcome> | null;
  /** Ctrl+S pressed again while `inflight` was out: the one follow-up save. */
  again: Promise<SaveOutcome> | null;
  refs: Set<string>;
  wasDirty: boolean;
  sub: { dispose(): void } | null;
  /** A programmatic change (a reload in place) is under way: not an edit. */
  applying: boolean;
  /**
   * Bumped by every reload in place (Reload, Discard and reload, Take theirs,
   * the poll's): a save sent before it says nothing about the text after it.
   */
  gen: number;
}

/** A buffer as the window and the tests see it: never its text. */
export interface BufferView {
  abs: string;
  projectId: string;
  path: string;
  kind: Kind;
  editable: boolean;
  readonly: string | null;
  etag: string | null;
  encoding: string | null;
  eol: string | null;
  dirty: boolean;
  saving: boolean;
  conflict: Conflict | null;
  refs: string[];
  language: string | null;
  modelEol: string | null;
}

export type SaveOutcome = "saved" | "conflict" | "error" | "skipped";

/**
 * A UTF-8 byte-order mark is the file's, not the text's: it is kept off the
 * model (Monaco would drop it from `getValue()` and a reload would type it in
 * as a character) and put back on save when the file had one.
 */
const BOM = "\ufeff";
const unBOM = (text: string) => (text.startsWith(BOM) ? text.slice(1) : text);

/** A pane's tab, as a registry reference: `<pane>|<path>`. */
export const refOf = (pane: number | string, path: string) => `${pane}|${path}`;
/** The pane a reference belongs to, as a string. */
export const paneOfRef = (ref: string) => ref.slice(0, ref.indexOf("|"));

const SAVE_NOTE = "saved";

/** How long a save's write may stay unanswered before it is given up on. */
export const WRITE_TIMEOUT_MS = 90_000;
const WRITE_TIMEOUT_NOTE = "the save didn't answer; your text is still here, press Ctrl+S to try again";

export class Buffers {
  private deps: BufferDeps | null = null;
  private bufs = new Map<string, Buf>();
  private byKey = new Map<string, string>();
  private pending = new Map<string, Promise<Buf>>();
  /** A buffer being made, by abs: a second read of the same file waits for it. */
  private making = new Map<string, Promise<Buf>>();
  private listeners = new Set<() => void>();
  private noticeFns = new Set<(abs: string, text: string) => void>();
  private touches: Touch[] = [];
  private polling = false;
  private ver = 0;

  configure(deps: BufferDeps) {
    this.deps = deps;
  }

  // ---- subscribing (useSyncExternalStore) ------------------------------------

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => void this.listeners.delete(fn);
  };

  version = () => this.ver;

  private emit() {
    this.ver += 1;
    for (const f of Array.from(this.listeners)) f();
  }

  /** A sentence for every pane showing `abs` (a reload, a save, a refusal). */
  onNotice(fn: (abs: string, text: string) => void) {
    this.noticeFns.add(fn);
    return () => void this.noticeFns.delete(fn);
  }

  private notice(abs: string, text: string) {
    for (const f of Array.from(this.noticeFns)) f(abs, text);
  }

  // ---- reading ---------------------------------------------------------------

  get(abs: string): BufferView | null {
    const b = this.bufs.get(abs);
    return b ? this.view(b) : null;
  }

  /** The model a pane shows for `abs` (null: withheld, or not open). */
  model(abs: string): TextModel | null {
    return this.bufs.get(abs)?.model ?? null;
  }

  /** The buffer's text: for a markdown preview, a save, "Add to chat" — never per keystroke. */
  text(abs: string): string | null {
    const m = this.bufs.get(abs)?.model;
    return m && !m.isDisposed() ? m.getValue() : null;
  }

  isDirty(abs: string): boolean {
    const b = this.bufs.get(abs);
    return !!b && this.dirtyOf(b);
  }

  /** Any buffer a tab of pane `pane` holds is dirty: the pane may not let go of it. */
  paneDirty(pane: number | string): boolean {
    const key = String(pane);
    for (const b of this.bufs.values()) {
      if (this.dirtyOf(b) && [...b.refs].some((r) => paneOfRef(r) === key)) return true;
    }
    return false;
  }

  /**
   * The unsaved edits only pane `pane` holds: what closing it would lose. A
   * dirty buffer another pane also holds is not lost when this one closes.
   */
  paneLosing(pane: number | string): BufferView[] {
    const key = String(pane);
    return [...this.bufs.values()]
      .filter((b) => this.dirtyOf(b) && b.refs.size > 0 && [...b.refs].every((r) => paneOfRef(r) === key))
      .map((b) => this.view(b));
  }

  /**
   * Pane `pane` holds an edit no other pane does: letting go of it — a view
   * switch, another project, ×, a split taking the pane back — would lose it
   * (split-panes plan G-3: ask only when closing a buffer's last view).
   */
  paneLoses(pane: number | string): boolean {
    const key = String(pane);
    for (const b of this.bufs.values()) {
      if (this.dirtyOf(b) && b.refs.size > 0 && [...b.refs].every((r) => paneOfRef(r) === key)) return true;
    }
    return false;
  }

  /**
   * The unsaved edits the panes of `panes` hold together and no pane outside
   * them does: what letting go of all of them at once would lose. A project
   * going away unpins every pane on it in one step, so the last-view rule is
   * asked of the set, never pane by pane — two panes sharing an edit each
   * read "another pane holds it" and both let go (review of PR #49, H2).
   */
  panesLose(panes: readonly (number | string)[]): BufferView[] {
    const keys = new Set(panes.map(String));
    return [...this.bufs.values()]
      .filter((b) => this.dirtyOf(b) && b.refs.size > 0 && [...b.refs].every((r) => keys.has(paneOfRef(r))))
      .map((b) => this.view(b));
  }

  /** The panes of `panes` that hold one of the edits `panesLose(panes)` names: the ones to keep. */
  panesHolding(panes: readonly (number | string)[]): string[] {
    const held = new Set<string>();
    for (const v of this.panesLose(panes)) for (const r of v.refs) held.add(paneOfRef(r));
    return panes.map(String).filter((p) => held.has(p));
  }

  /** Save every edit only pane `pane` holds (closing it, "Save"): true when all saved. */
  async savePane(pane: number | string): Promise<boolean> {
    let ok = true;
    for (const b of this.paneLosing(pane)) ok = (await this.save(b.abs)) === "saved" && ok;
    return ok && this.paneLosing(pane).length === 0;
  }

  /** How many buffers hold an unsaved edit, whether or not a tab holds them. */
  dirtyCount(): number {
    let n = 0;
    for (const b of this.bufs.values()) if (this.dirtyOf(b)) n += 1;
    return n;
  }

  /**
   * Save every unsaved edit, a buffer no tab holds included: the crash
   * prompt's way out, since the registry outlives the workspace (review of PR
   * #49, L3). True when nothing is left unsaved.
   */
  async saveAll(): Promise<boolean> {
    for (const b of [...this.bufs.values()]) if (this.dirtyOf(b)) await this.save(b.abs);
    return this.dirtyCount() === 0;
  }

  anyDirty(): boolean {
    for (const b of this.bufs.values()) if (this.dirtyOf(b)) return true;
    return false;
  }

  /** Every buffer, for `window.__hud.buffers()` (tests only): never any text. */
  snapshot(): BufferView[] {
    return [...this.bufs.values()].map((b) => this.view(b));
  }

  private view(b: Buf): BufferView {
    return {
      abs: b.abs, projectId: b.projectId, path: b.path, kind: b.kind, editable: b.editable,
      readonly: b.readonly, etag: b.etag,
      // Saving while a write is out *or* a second Ctrl+S waits for one: the
      // tab and the status say "saving…" until the last of them answers.
      encoding: b.encoding, eol: b.eol, dirty: this.dirtyOf(b), saving: b.saving || !!b.again,
      conflict: b.conflict,
      refs: [...b.refs], language: b.model?.getLanguageId?.() ?? null,
      modelEol: b.model?.getEOL ? (b.model.getEOL() === "\r\n" ? "CRLF" : "LF") : null,
    };
  }

  private dirtyOf(b: Buf): boolean {
    return b.editable && !!b.model && !b.model.isDisposed()
      && b.model.getAlternativeVersionId() !== b.saved;
  }

  // ---- opening and letting go ------------------------------------------------

  /**
   * The buffer for `path` in project `projectId`, read once and shared: held
   * by `ref` (a pane's tab, `refOf`) until released. Rejects with the read's
   * error (404 gone, 403 out of scope).
   */
  async open(projectId: string, path: string, ref: string): Promise<BufferView> {
    const key = `${projectId}\u0000${path}`;
    let buf: Buf | undefined;
    const known = this.byKey.get(key);
    if (known) buf = this.bufs.get(known);
    if (!buf) {
      let p = this.pending.get(key);
      if (!p) {
        p = this.load(projectId, path, key);
        this.pending.set(key, p);
        p.finally(() => this.pending.delete(key)).catch(() => {});
      }
      buf = await p;
    }
    buf.refs.add(ref);
    this.emit();
    return this.view(buf);
  }

  private async load(projectId: string, path: string, key: string): Promise<Buf> {
    const deps = this.need();
    const r = await deps.read(projectId, path);
    const abs = typeof r.abs === "string" && r.abs ? r.abs : `${projectId}:${r.path || path}`;
    // The same file through another project or another name: one buffer —
    // whether it is made already or still being made. Two reads of one file
    // under two keys (a restored Inbox tab on `src/main.py` and a project's on
    // `main.py`) both used to make a model at the same URI; the second
    // replaced the first's, and the tab on it died with "Model is disposed!"
    // (review of PR #49, L1).
    const existing = this.bufs.get(abs);
    if (existing) {
      this.byKey.set(key, abs);
      return existing;
    }
    const making = this.making.get(abs);
    if (making) {
      const buf = await making;
      this.byKey.set(key, abs);
      return buf;
    }
    const made = this.make(r, projectId, path, abs);
    this.making.set(abs, made);
    try {
      const buf = await made;
      this.byKey.set(key, abs);
      return buf;
    } finally {
      this.making.delete(abs);
    }
  }

  private async make(r: FileRead, projectId: string, path: string, abs: string): Promise<Buf> {
    const deps = this.need();
    const kind = r.protected ? "withheld" : parseKind(r.kind);
    const model = kind === "withheld" ? null : await deps.makeModel(unBOM(r.content ?? ""), abs, kind);
    const raced = this.bufs.get(abs);
    if (raced) {
      // Cannot happen while `making` dedupes by abs; if it ever does, the
      // live buffer's model is kept and only the new one goes.
      if (model && model !== raced.model) model.dispose();
      return raced;
    }
    const buf: Buf = {
      abs, projectId, path: r.path || path, kind, editable: editableOf(kind, r.editable),
      readonly: typeof r.readonly === "string" ? r.readonly : kind === "withheld" ? "withheld" : null,
      etag: typeof r.etag === "string" ? r.etag : null,
      encoding: r.encoding ?? null, eol: r.eol ?? null,
      model, saved: model ? model.getAlternativeVersionId() : 0, conflict: null, saving: false,
      inflight: null, again: null,
      refs: new Set(), wasDirty: false, sub: null, applying: false, gen: 0,
    };
    if (model) {
      // Said the moment it changes (App's guards read it in the same event
      // as the keystroke: #43's M1), and only when dirtiness moved.
      // A reload in place (`apply`) is not an edit: while it runs nothing is
      // said, so it never reads dirty for a moment — which pinned a preview
      // tab and flashed the dirty dot (review of PR #49, L4). The reload says
      // what it leaves once it is done.
      buf.sub = model.onDidChangeContent(() => {
        if (buf.applying) return;
        const now = this.dirtyOf(buf);
        if (now !== buf.wasDirty) {
          buf.wasDirty = now;
          this.emit();
        }
      });
    }
    this.bufs.set(abs, buf);
    return buf;
  }

  /**
   * A tab lets go of its buffer. The model goes once no tab holds it and it
   * is clean — or `discard` says the owner threw the edit away.
   */
  release(abs: string, ref: string, opts: { discard?: boolean } = {}) {
    const b = this.bufs.get(abs);
    if (!b || !b.refs.delete(ref)) return;
    if (!b.refs.size && (!this.dirtyOf(b) || opts.discard)) this.drop(b);
    this.emit();
  }

  /**
   * Every tab of pane `pane` lets go. Only the buffers this pane held are
   * touched: one goes once no tab holds it and it is clean, or `discard`
   * says the owner threw this pane's edit away. A buffer no tab held before
   * the call — another pane's edit kept after its pane unmounted — is never
   * this pane's to discard: closing any pane used to drop every such edit
   * (review of PR #49, H1).
   */
  releasePane(pane: number | string, opts: { discard?: boolean } = {}) {
    const key = String(pane);
    for (const b of [...this.bufs.values()]) {
      let held = false;
      for (const r of [...b.refs]) {
        if (paneOfRef(r) === key) {
          b.refs.delete(r);
          held = true;
        }
      }
      if (held && !b.refs.size && (!this.dirtyOf(b) || opts.discard)) this.drop(b);
    }
    this.emit();
  }

  private drop(b: Buf) {
    b.sub?.dispose();
    b.model?.dispose();
    this.bufs.delete(b.abs);
    for (const [k, v] of [...this.byKey]) if (v === b.abs) this.byKey.delete(k);
  }

  // ---- saving ----------------------------------------------------------------

  /**
   * Save `abs` against the etag it was read or last saved as (or `expected`:
   * the disk's, for Keep mine; null, to create a file that is gone). A 409 is
   * the conflict bar, never a retry; nothing is ever forced.
   *
   * One write at a time per buffer: a second PUT while the first is on its
   * way would carry the same stale etag, and its 409 would raise a conflict
   * bar against the owner's own write. But a second Ctrl+S is never dropped
   * (review of PR #49, LOW-2 — on a slow disk, `/mnt/c`, "type A, save, type
   * B, save" used to put only A on disk while the owner believed both were
   * there): it waits for the first, and once that answers, **exactly one**
   * follow-up save runs against the etag *that answer returned*. Any further
   * presses join the same follow-up. The first one's 409 or error runs none
   * (the bar, or the notice, says why), and a buffer with nothing new since
   * the first was sent needs none — a double Ctrl+S is still one PUT. A
   * follow-up saves with the buffer's own etag, whatever `expected` it was
   * pressed with. Nor does one run once the buffer was reloaded in place
   * since it was queued: Discard and reload (or Take theirs) threw that text
   * away, and saving it then wrote the reload's stale text back over the
   * first save (delta review of PR #49, MED-1).
   */
  save(abs: string, opts: { expected?: string | null } = {}): Promise<SaveOutcome> {
    const b = this.bufs.get(abs);
    if (!b || !b.model || !b.editable) return Promise.resolve("skipped");
    if (b.inflight) {
      // A follow-up drops `expected`. Keep mine and Save to recreate pass one
      // and rely on the conflict bar disabling them while a save is out
      // (`busy`, the view's `saving`); enabled then, a press would save
      // against the buffer's etag instead of the disk the owner was shown.
      if (!b.again) {
        b.again = this.followUp(b, b.inflight, b.gen);
        this.emit();
      }
      return b.again;
    }
    const sent = this.send(b, abs, opts).finally(() => {
      if (b.inflight === sent) b.inflight = null;
    });
    b.inflight = sent;
    return sent;
  }

  /** The one follow-up: after `first` answers (its `inflight` already cleared). */
  private async followUp(b: Buf, first: Promise<SaveOutcome>, gen: number): Promise<SaveOutcome> {
    let outcome: SaveOutcome;
    try {
      outcome = await first;
    } catch {
      outcome = "error";
    }
    b.again = null;
    const still = this.bufs.get(b.abs) === b && b.gen === gen;
    if (outcome !== "saved" || !still || !this.dirtyOf(b)) {
      this.emit();
      return outcome;
    }
    return this.save(b.abs);
  }

  private async send(b: Buf, abs: string, opts: { expected?: string | null }): Promise<SaveOutcome> {
    if (!b.model) return "skipped";
    const deps = this.need();
    const version = b.model.getAlternativeVersionId();
    // Read once, here; the file's byte-order mark, if it had one, put back.
    const content = (b.encoding === "utf-8-bom" ? BOM : "") + b.model.getValue();
    const expected = "expected" in opts ? opts.expected ?? null : b.etag;
    const gen = b.gen;
    b.saving = true;
    this.emit();
    // A write that never answers would hold `saving` — and with it the poll
    // for this file — forever (delta review of PR #49, LOW-1): it is aborted
    // after WRITE_TIMEOUT_MS and ends as an error, the text kept. The race
    // ends it even if the transport ignores the signal.
    const ctl = new AbortController();
    let timedOut = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        ctl.abort();
        reject(new Error("timeout"));
      }, WRITE_TIMEOUT_MS);
    });
    try {
      const r = await Promise.race([
        deps.write(b.projectId, { path: b.path, content, expected_etag: expected }, ctl.signal),
        timeout,
      ]);
      b.saving = false;
      // A late answer lands only on the buffer it was sent from: one dropped
      // and read again meanwhile is another buffer, never marked saved by it.
      if (this.bufs.get(abs) !== b) return "saved";
      // Nor on text it never carried: a reload in place meanwhile (Discard
      // and reload, Take theirs) holds the disk as it was read, and its etag.
      // The poll then sees the write that landed and reloads it; marking this
      // version saved instead left the reload's text reading dirty, saved by
      // the follow-up over the write (delta review of PR #49, MED-1).
      if (b.gen !== gen) {
        this.emit();
        return "saved";
      }
      b.etag = r.etag;
      b.saved = version;
      b.wasDirty = this.dirtyOf(b);
      b.conflict = null;
      b.eol = eolOf(content) ?? b.eol;
      this.emit();
      this.notice(abs, SAVE_NOTE);
      return "saved";
    } catch (e: any) {
      b.saving = false;
      if (timedOut) {
        this.emit();
        this.notice(abs, WRITE_TIMEOUT_NOTE);
        return "error";
      }
      if (e?.status === 409) {
        // The daemon says what is on disk now: an etag, or null for gone. An
        // answer without one (a daemon from before ED-2), or with one that is
        // not an etag, is asked for it; still unknown, the owner is told.
        const said = e?.body && typeof e.body === "object" && "etag" in e.body ? e.body.etag : undefined;
        let now: string | null | undefined = said === null || (typeof said === "string" && ETAG.test(said))
          ? said : undefined;
        if (now === undefined) {
          try {
            const row = (await deps.stat(b.projectId, [b.path])).files?.[b.path];
            now = row?.gone ? null : typeof row?.etag === "string" && ETAG.test(row.etag) ? row.etag : undefined;
          } catch {
            now = undefined;
          }
        }
        if (this.bufs.get(abs) !== b) return "conflict";
        if (typeof now === "string") b.conflict = this.changed(b, now);
        else if (now === null) b.conflict = { state: "gone" };
        this.emit();
        if (now === undefined) this.notice(abs, "changed on disk since it was read");
        return "conflict";
      }
      this.emit();
      this.notice(abs, e?.message || "save failed");
      return "error";
    } finally {
      clearTimeout(timer);
    }
  }

  /** The bar for a newer write: "again" once it had already been shown for another. */
  private changed(b: Buf, etag: string): Conflict {
    const was = b.conflict;
    return { state: "changed", etag, again: !!was && (was.state === "gone" || was.etag !== etag) };
  }

  /**
   * Keep mine: save against the disk the owner was shown — the etag the bar
   * (or Compare's left side) was drawn for — so a write that lands after it
   * is a 409 and the bar again, never overwritten unseen. Gone: recreate.
   */
  keepMine(abs: string, shown?: string | null): Promise<SaveOutcome> {
    const b = this.bufs.get(abs);
    if (shown !== undefined) return this.save(abs, { expected: shown });
    if (!b?.conflict) return this.save(abs);
    return this.save(abs, { expected: b.conflict.state === "changed" ? b.conflict.etag : null });
  }

  /**
   * Load what is on disk into the buffer, undoably (Take theirs, Reload, or a
   * clean buffer the poll saw move). With `onlyIfClean`, an edit made while
   * the file was being read wins: the buffer gets the conflict bar instead.
   */
  async reload(abs: string, opts: { onlyIfClean?: boolean; toast?: boolean } = {}): Promise<boolean> {
    const b = this.bufs.get(abs);
    if (!b || !b.model) return false;
    const deps = this.need();
    let r: FileRead;
    try {
      r = await deps.read(b.projectId, b.path);
    } catch (e: any) {
      if (e?.status === 404) {
        b.conflict = { state: "gone" };
        this.emit();
      } else this.notice(abs, e?.message || "reload failed");
      return false;
    }
    if (this.bufs.get(abs) !== b || !b.model || b.model.isDisposed()) return false;
    if (r.protected || parseKind(r.kind) === "withheld") return false;
    if (opts.onlyIfClean && this.dirtyOf(b)) {
      if (r.etag && r.etag !== b.etag) b.conflict = this.changed(b, r.etag);
      this.emit();
      return false;
    }
    this.apply(b, unBOM(r.content ?? ""), r.eol ?? null);
    b.gen += 1;
    b.etag = typeof r.etag === "string" ? r.etag : null;
    b.kind = parseKind(r.kind);
    b.editable = editableOf(b.kind, r.editable);
    b.readonly = typeof r.readonly === "string" ? r.readonly : null;
    b.encoding = r.encoding ?? null;
    b.eol = r.eol ?? null;
    b.saved = b.model.getAlternativeVersionId();
    b.wasDirty = false;
    b.conflict = null;
    this.emit();
    if (opts.toast) {
      const who = writerOf(this.touches, abs, this.need().now());
      this.notice(abs, who ? `changed on disk by ${who} — reloaded` : "changed on disk — reloaded");
    }
    return true;
  }

  /** What is on disk now, and its etag, for Compare: read through the buffer's own project. */
  async readDisk(abs: string): Promise<{ text: string; etag: string | null } | null> {
    const b = this.bufs.get(abs);
    if (!b) return null;
    try {
      const r = await this.need().read(b.projectId, b.path);
      return r.protected ? null : { text: unBOM(r.content ?? ""), etag: typeof r.etag === "string" ? r.etag : null };
    } catch {
      return null;
    }
  }

  /** Take theirs: the disk's text, undoably (Ctrl+Z brings the edit back). */
  takeTheirs(abs: string): Promise<boolean> {
    return this.reload(abs);
  }

  /** Replace the model's text, keeping what did not change, as one undo step. */
  private apply(b: Buf, next: string, eol: string | null) {
    const m = b.model!;
    b.applying = true;
    try {
      if (m.pushEditOperations && m.getPositionAt && m.getEOL) {
        if (m.pushEOL && (eol === "CRLF" || eol === "LF")) {
          const want = eol === "CRLF" ? "\r\n" : "\n";
          if (m.getEOL() !== want) m.pushEOL(want === "\r\n" ? 1 : 0);
        }
        const modelEol = m.getEOL();
        const normal = next.replace(/\r\n|\r|\n/g, modelEol);
        const edit = minimalEdit(m.getValue(), normal);
        if (edit) {
          const a = m.getPositionAt(edit.start);
          const z = m.getPositionAt(edit.end);
          // An undo step of its own, on both sides: Ctrl+Z steps back over
          // the reload alone, never the reload and the typing before it.
          m.pushStackElement?.();
          m.pushEditOperations([], [{
            range: { startLineNumber: a.lineNumber, startColumn: a.column, endLineNumber: z.lineNumber, endColumn: z.column },
            text: edit.text, forceMoveMarkers: true,
          }], () => null);
          m.pushStackElement?.();
        }
      } else {
        m.setValue?.(next);
      }
    } finally {
      b.applying = false;
    }
  }

  // ---- the change poll ---------------------------------------------------------

  /** Ask where every open file stands; act on what moved (plan §2.4's table). */
  async poll(): Promise<void> {
    if (this.polling || !this.deps) return;
    this.polling = true;
    try {
      const groups = new Map<string, Buf[]>();
      for (const b of this.bufs.values()) {
        if (!b.refs.size || b.kind === "withheld" || !b.model) continue;
        groups.set(b.projectId, [...(groups.get(b.projectId) || []), b]);
      }
      for (const [projectId, bufs] of groups) {
        for (let i = 0; i < bufs.length; i += 100) {
          const chunk = bufs.slice(i, i + 100);
          // A stat that crossed a save says nothing about the buffer: it may
          // have read the disk before the write landed, and once the save's
          // answer moved the etag that reads as someone else's write — a
          // conflict bar against the owner's own save on a slow disk.
          const asked = new Map(chunk.map((b) => [b, { etag: b.etag, saving: b.saving || !!b.again }]));
          let rows: Record<string, StatRow>;
          try {
            rows = (await this.deps.stat(projectId, chunk.map((b) => b.path))).files || {};
          } catch {
            continue;
          }
          for (const b of chunk) {
            if (this.bufs.get(b.abs) !== b) continue;
            const was = asked.get(b)!;
            if (was.saving || was.etag !== b.etag) continue;
            const row = rows[b.path];
            const saving = b.saving || !!b.again;
            switch (pollAction({ etag: b.etag, dirty: this.dirtyOf(b), saving, conflict: b.conflict }, row)) {
              case "reload":
                await this.reload(b.abs, { onlyIfClean: true, toast: true });
                break;
              case "conflict":
                b.conflict = this.changed(b, row!.etag as string);
                this.emit();
                break;
              case "gone":
                b.conflict = { state: "gone" };
                this.emit();
                break;
              case "clear":
                b.conflict = null;
                this.emit();
                break;
              default:
                break;
            }
          }
        }
      }
    } finally {
      this.polling = false;
    }
  }

  /**
   * A running turn's write tool named a file (any thread): the toast can say
   * who. `path` is absolute (`writtenPaths`): a relative one is ignored.
   */
  noteTouch(path: string, writer: string) {
    if (!path.startsWith("/")) return;
    const now = this.deps?.now() ?? Date.now();
    this.touches.push({ path, writer, at: now });
    if (this.touches.length > TOUCHES_MAX || now - this.touches[0].at > TOUCH_MS) {
      this.touches = this.touches.filter((t) => now - t.at <= TOUCH_MS).slice(-TOUCHES_MAX);
    }
  }

  private need(): BufferDeps {
    if (!this.deps) throw new Error("buffers are not configured");
    return this.deps;
  }

  /** Tests only: forget everything (models disposed). */
  reset() {
    for (const b of [...this.bufs.values()]) this.drop(b);
    this.byKey.clear();
    this.pending.clear();
    this.making.clear();
    this.touches = [];
    this.emit();
  }
}

/** The window's one registry. */
export const buffers = new Buffers();
