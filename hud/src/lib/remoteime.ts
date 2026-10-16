// IME composition for a Remote Browser pane (B-IME; Browser plan BR-7).
//
// The owner composes in the pane's own hidden textarea (RemoteView's input
// proxy), never in the remote page: the operating system's IME draws its
// candidate window beside that box, and the page sees **only committed text**,
// sent as `{t:"text"}` (`Input.insertText`). The page never sees half-typed
// pinyin or kana, and no remote composition state exists that a lost socket,
// a card or a navigation could strand or commit later.
//
// What Chrome 155 does, measured (and what this class does about it):
//   - a commit (Enter, Space, a candidate) ends with `compositionend` whose
//     `data` is the committed text → sent;
//   - Escape ends it with `data` "" → nothing is sent;
//   - a focus change mid-composition **commits** what was typed: Chrome fires
//     `compositionend` with the raw text (`ni`) *before* the box's `blur`, in
//     the same task. Chrome on Windows also commits on any mouse press before
//     the press reaches the page. So a commit is held for `COMMIT_HOLD_MS` and
//     dropped if a blur or a pointer press follows; any later owner keystroke
//     or insertion sends it first, so order is never inverted;
//   - setting the box's value mid-composition ends Blink's composition (no
//     `compositionend`), which is how an interruption here cancels it
//     (`interrupt`). Whether the operating system's IME is told is not
//     something a test here can see, so an IME that still holds the cancelled
//     composition and commits it later — that commit arrives as a plain
//     `insertText` — is not trusted: after a cancel, the first `insertText`
//     before a new composition or a forwarded key is dropped (`stale`);
//   - text inserted with no composition at all (the Windows emoji panel, an
//     IME's direct commit of punctuation, a key whose keydown was not
//     forwarded) arrives as an `input` of type `insertText` → sent, once.
//
// An interrupted composition — blur, a pointer press, a card or hold, a
// reconnect, a navigation (`owner_reset`), an auth prompt — sends nothing,
// and its box is cleared so no stale composition lingers in the IME.

/** How long a `compositionend` waits for a blur or pointer press that would make it an interruption. */
export const COMMIT_HOLD_MS = 40;

export interface ImeSink {
  /** Whether owner input may reach the page right now (no card, hold, auth; the current socket). */
  canInput(): boolean;
  /** The input epoch: bumped by a card, a reconnect, a navigation or an auth prompt. */
  epoch(): number;
  /** Send committed text to the page (as `{t:"text"}`). */
  send(text: string): void;
  /** Empty the input proxy; mid-composition this cancels the IME's composition. */
  clear(): void;
  /** The proxy shows (or stops showing) the composition. */
  showing?(composing: boolean): void;
}

type Timer = (fn: () => void, ms: number) => unknown;
type Cancel = (handle: unknown) => void;

export class RemoteIme {
  /** The DOM's composition is in progress (start seen, end not yet). */
  private active = false;
  /** The epoch a composition started in, while it may still commit; null when it may not. */
  private session: number | null = null;
  /** A commit waiting out `COMMIT_HOLD_MS`. */
  private pending: { text: string; epoch: number; timer: unknown } | null = null;
  /** A composition was cancelled under the IME: its late commit may still arrive as `insertText`. */
  private stale = false;

  constructor(
    private sink: ImeSink,
    private timer: Timer = (fn, ms) => setTimeout(fn, ms),
    private cancelTimer: Cancel = (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  ) {}

  /** A composition is in progress in the proxy. */
  get composing(): boolean {
    return this.active;
  }

  /** A commit is waiting to be sent. */
  get waiting(): boolean {
    return this.pending !== null;
  }

  private valid(epoch: number): boolean {
    return this.sink.canInput() && this.sink.epoch() === epoch;
  }

  /** `compositionstart`. A composition begun while input is held can never commit. */
  start() {
    this.flush();
    this.stale = false;
    this.active = true;
    this.session = this.sink.canInput() ? this.sink.epoch() : null;
    this.sink.showing?.(this.session !== null);
  }

  /** `compositionupdate` / an `input` mid-composition: a card or hold since it began cancels it. */
  update() {
    if (!this.active) return;
    if (this.session === null || !this.valid(this.session)) this.interrupt();
  }

  /** `compositionend`. Escape's empty end sends nothing; a commit waits out the hold. */
  end(data: string) {
    const epoch = this.session;
    this.active = false;
    this.session = null;
    this.sink.showing?.(false);
    this.sink.clear();
    if (epoch === null || !data || !this.valid(epoch)) return;
    this.drop();
    const pending = { text: data, epoch, timer: null as unknown };
    pending.timer = this.timer(() => {
      if (this.pending === pending) this.flush();
    }, COMMIT_HOLD_MS);
    this.pending = pending;
  }

  /**
   * An `input` event. Composition text is the composition's (sent at its end,
   * never here); an `insertText` outside one is sent once. Anything else the
   * proxy received is cleared. Returns whether the proxy should be emptied.
   */
  input(inputType: string, data: string | null, isComposing: boolean): boolean {
    if (isComposing || this.active) {
      this.update();
      return false;
    }
    this.flush();
    if (inputType !== "insertText" || !data) return true;
    if (this.stale) {
      this.stale = false;
      return true;
    }
    if (this.sink.canInput()) this.sink.send(data);
    return true;
  }

  /** An ordinary key reached the page: the owner has moved on from any cancelled composition. */
  forwarded() {
    this.stale = false;
  }

  /** Before any other owner input reaches the page: a waiting commit goes first. */
  flush() {
    const p = this.pending;
    if (!p) return;
    this.pending = null;
    this.cancelTimer(p.timer);
    if (this.valid(p.epoch)) this.sink.send(p.text);
  }

  private drop() {
    if (this.pending) this.cancelTimer(this.pending.timer);
    this.pending = null;
  }

  /**
   * A blur, a pointer press, a card or hold, a reconnect, a navigation or an
   * auth prompt: nothing waiting is sent, and a composition in progress is
   * cancelled (its proxy emptied, which cancels the IME's composition too).
   */
  interrupt() {
    this.drop();
    const was = this.active;
    this.active = false;
    this.session = null;
    if (was) this.stale = true;
    if (was) this.sink.showing?.(false);
    this.sink.clear();
  }
}
