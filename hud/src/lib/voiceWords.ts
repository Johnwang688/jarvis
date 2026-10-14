// Send and cancel words (voice rework V10, plan §3.3 phase 1, decision V-1,
// owner's answer to Q1).
//
// A message that ends with a send word ("over") goes out at once, the word
// stripped, even in REVIEW (it sends what is in the box). One that ends with a
// cancel word ("cancel") is discarded, with everything dictated since the last
// send, and held for ten seconds so Undo can put it back in the box.
//
// The word is read from the TRANSCRIPT, at the message's end, and counts only
// when
//   - it is the last word of the message (an "over" in the middle of a sentence
//     is prose),
//   - the recording ended with at least SEND_WORD_SILENCE_MS of quiet after it
//     (`tailMs`, measured by Capture on the ring clock: "the game is over" cut
//     off by a card, a mute or a talk key released mid-word has none), and
//   - the message is whole: no chunk failed (`late`), it is not the product of
//     the owner's Retry, and no authorization card is waiting. A card is
//     answered by hand, never by what the microphone heard; with one up the
//     words are inert and the text is placed verbatim.
//
// Everything here is pure and window-local; App.tsx only carries the plan out.
// Two pieces of memory support it, both in RAM and neither persisted: the
// `DictationLedger` (which words in a REVIEW box came from dictation, so a
// cancel removes those and not what the owner typed) and the `UndoHold`.

import type { SttCapture, SttLanding } from "./stt";

/** Quiet that must follow the word (owner's answer to Q1). */
export const SEND_WORD_SILENCE_MS = 500;
/** Longest list, longest word: a bound on what the settings box accepts and storage returns. */
export const MAX_WORDS = 5;
export const MAX_WORD_LENGTH = 20;
export const UNDO_MS = 10_000;
/** The line under the orb while Undo is offered (plan §3.12). */
export const CANCELLED_STATUS = "CANCELLED · UNDO";

export interface Words {
  /** The whole feature, as one toggle (plan §3.3, "an on/off toggle"). */
  enabled: boolean;
  send: string[];
  cancel: string[];
}

export const DEFAULT_WORDS: Readonly<Words> = Object.freeze({ enabled: true, send: ["over"], cancel: ["cancel"] });
const KEY = "jarvis.hud.voice.words";

/** A single word: letters, digits, an inner apostrophe or hyphen. Lower-cased. Null if it is anything else. */
export function parseWord(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const w = raw.normalize("NFKC").trim().toLowerCase().replace(/[’]/g, "'");
  if (w.length < 2 || w.length > MAX_WORD_LENGTH) return null;
  return /^[\p{L}\p{N}]+(?:['-][\p{L}\p{N}]+)*$/u.test(w) ? w : null;
}

/** Why an entry was refused, in words the settings box can show; null when it is fine. */
export function refusal(raw: string, own: readonly string[], other: readonly string[]): string | null {
  const text = typeof raw === "string" ? raw.trim() : "";
  if (!text) return "Type a word.";
  if (/\s/.test(text)) return "One word only.";
  const w = parseWord(text);
  if (!w) return `Use a single word of 2 to ${MAX_WORD_LENGTH} letters.`;
  if (own.includes(w)) return "Already in this list.";
  if (other.includes(w)) return "Already in the other list.";
  if (own.length >= MAX_WORDS) return `At most ${MAX_WORDS} words.`;
  return null;
}

/** A list as storage or a form hands it back: valid words only, no repeats, bounded. */
export function normalizeList(list: unknown, taken: readonly string[] = []): string[] {
  if (!Array.isArray(list)) return [];
  const out: string[] = [];
  for (const raw of list) {
    const w = parseWord(raw);
    if (w && !out.includes(w) && !taken.includes(w)) out.push(w);
    if (out.length >= MAX_WORDS) break;
  }
  return out;
}

export function normalizeWords(value: unknown): Words {
  if (!value || typeof value !== "object" || Array.isArray(value)) return { ...DEFAULT_WORDS, send: [...DEFAULT_WORDS.send], cancel: [...DEFAULT_WORDS.cancel] };
  const v = value as Record<string, unknown>;
  // A list that is present stays as it is, even empty (that function is then off);
  // a missing or malformed one is the default. A word in both lists belongs to send.
  const send = Array.isArray(v.send) ? normalizeList(v.send) : [...DEFAULT_WORDS.send];
  const cancel = Array.isArray(v.cancel) ? normalizeList(v.cancel, send) : DEFAULT_WORDS.cancel.filter(w => !send.includes(w));
  return { enabled: typeof v.enabled === "boolean" ? v.enabled : true, send, cancel };
}

export function loadWords(storage?: Pick<Storage, "getItem">): Words {
  try {
    const raw = (storage ?? window.localStorage).getItem(KEY);
    return normalizeWords(raw === null ? null : JSON.parse(raw));
  } catch {
    return normalizeWords(null);
  }
}

export function saveWords(words: Words, storage?: Pick<Storage, "setItem">): void {
  try {
    (storage ?? window.localStorage).setItem(KEY, JSON.stringify(normalizeWords(words)));
  } catch {
    /* forgets, still works */
  }
}

export type SpokenWord = "send" | "cancel";

/** The last word of `text` (punctuation aside) and where it starts, or null. */
function lastWord(text: string): { word: string; at: number } | null {
  const body = text.replace(/[\s.,;:!?…"“”‘’')\]*_–—-]+$/u, "");
  const m = /(?:^|[^\p{L}\p{N}'’-])([\p{L}\p{N}][\p{L}\p{N}'’-]*)$/u.exec(body);
  if (!m) return null;
  return { word: m[1].toLowerCase().replace(/’/g, "'"), at: body.length - m[1].length };
}

/**
 * Read the trailing word of a finished message. `tailMs` is the quiet that
 * followed the last word; without enough of it nothing is read and the text is
 * returned untouched ("the game is over" stays a sentence).
 */
export function readWord(text: string, words: Words, tailMs: number): { text: string; word: SpokenWord | null } {
  const none = { text, word: null as SpokenWord | null };
  if (!words.enabled || !(tailMs >= SEND_WORD_SILENCE_MS)) return none;
  const last = lastWord(text);
  if (!last) return none;
  const word: SpokenWord | null = words.send.includes(last.word) ? "send" : words.cancel.includes(last.word) ? "cancel" : null;
  if (!word) return none;
  return { text: text.slice(0, last.at).replace(/[\s,;:"“‘–—-]+$/u, ""), word };
}

// ---- planning a batch -------------------------------------------------------

export interface WordItem {
  text: string;
  late: boolean;
  retried: boolean;
  meta: Pick<SttCapture, "tailMs">;
}

export type WordStep<T> =
  /** No word: placed as before (AUTO sends, REVIEW boxes). */
  | { kind: "run"; items: T[] }
  /** `items` (word stripped) go out now; `word` is the item that said it. */
  | { kind: "send"; items: T[]; word: T }
  /** `items` (word stripped) are discarded; `word` is the item that said it. */
  | { kind: "cancel"; items: T[]; word: T };

/**
 * Split a batch of finished messages, in the order spoken, at each message
 * that ends with a word. Messages before a cancel are discarded with it; those
 * before a send word go out with it. With no word anywhere the result is one
 * plain run, which is what keeps every V2-V8 behaviour as it was.
 */
export function planWords<T extends WordItem>(batch: readonly T[], ctx: { words: Words; card: boolean }): WordStep<T>[] {
  const steps: WordStep<T>[] = [];
  let run: T[] = [];
  for (const item of batch) {
    const eligible = !ctx.card && !item.late && !item.retried && typeof item.meta.tailMs === "number";
    const read = eligible ? readWord(item.text, ctx.words, item.meta.tailMs!) : { text: item.text, word: null };
    if (!read.word) { run.push(item); continue; }
    const stripped = { ...item, text: read.text };
    steps.push({ kind: read.word, items: [...run, stripped], word: stripped });
    run = [];
  }
  if (run.length) steps.push({ kind: "run", items: run });
  return steps;
}

/**
 * May a send word actually send these words where they landed? The same
 * conditions as `placementFor`, except that the mode is no obstacle either
 * way: "over" sends in REVIEW too. Anything moved since the words were said
 * (another chat, a pane that is gone, a turn that is not this chat's own) and
 * they wait in the box instead.
 */
export function wordMaySend(captured: SttCapture, landing: SttLanding, item: { late: boolean; retried: boolean }): boolean {
  const same = captured.chat !== null && captured.chat === landing.chat;
  return captured.mode !== "off" && landing.mode !== "off" && same && !captured.boxOnly
    && (!landing.busy || landing.steers) && !landing.card && !item.late && !item.retried;
}

// ---- what a cancel removes ---------------------------------------------------

const MAX_PIECES = 50;
const MAX_KEYS = 20;

/**
 * Which words in a chat's box came from dictation since the last send. A cancel
 * takes those out and leaves what the owner typed; a piece the owner edited no
 * longer matches and stays. Memory only.
 */
export class DictationLedger {
  private pieces = new Map<string, string[]>();

  add(key: string, text: string) {
    const t = text.trim();
    if (!t) return;
    const list = this.pieces.get(key) ?? [];
    this.pieces.delete(key);
    list.push(t);
    this.pieces.set(key, list.slice(-MAX_PIECES));
    while (this.pieces.size > MAX_KEYS) this.pieces.delete(this.pieces.keys().next().value as string);
  }

  /** The box was sent or emptied by a send: nothing in it is dictation any more. */
  forget(key: string) { this.pieces.delete(key); }

  has(key: string): boolean { return (this.pieces.get(key)?.length ?? 0) > 0; }

  /**
   * Remove the dictated pieces from `input` (the box) and the not-yet-taken
   * `pending` text. Returns what is left in the box, what was removed in the
   * order it was spoken, and how many pieces were edited past recognition and
   * so stay.
   */
  take(key: string, input: string, pending = ""): { input: string; removed: string[]; kept: number } {
    const list = [...(this.pieces.get(key) ?? [])];
    const pend = pending.trim();
    if (pend) { const i = list.lastIndexOf(pend); if (i >= 0) list.splice(i, 1); }
    let rest = input;
    const removed: string[] = [];
    let kept = 0;
    for (const piece of list) {
      const at = rest.lastIndexOf(piece);
      if (at < 0) { kept++; continue; }
      let before = rest.slice(0, at);
      let after = rest.slice(at + piece.length);
      if (before.endsWith(" ")) before = before.slice(0, -1);
      else if (after.startsWith(" ")) after = after.slice(1);
      rest = before + after;
      removed.push(piece);
    }
    if (pend) removed.push(pend);
    this.pieces.delete(key);
    return { input: rest, removed, kept };
  }
}

// ---- Undo ---------------------------------------------------------------------

export interface HeldCancel { key: string; text: string; until: number }

/**
 * What a cancel discarded, for UNDO_MS. One at a time: a second cancel that
 * discards something replaces it; one that discards nothing leaves it alone.
 * Memory only; nothing about it is stored or uploaded.
 */
export class UndoHold {
  private value: HeldCancel | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<() => void>();

  constructor(private ms = UNDO_MS, private now: () => number = () => Date.now()) {}

  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  snapshot = (): HeldCancel | null => this.value;
  private publish() { for (const fn of this.listeners) { try { fn(); } catch { /* observers cannot affect it */ } } }

  hold(key: string, text: string) {
    const t = text.trim();
    if (!t) return;
    if (this.timer) clearTimeout(this.timer);
    this.value = { key, text: t, until: this.now() + this.ms };
    this.timer = setTimeout(() => this.clear(), this.ms);
    this.publish();
  }

  /** The held text for `key`, once: Undo. Null if there is none, it is another chat's, or it expired. */
  take(key: string): string | null {
    const v = this.value;
    if (!v || v.key !== key || this.now() >= v.until) return null;
    this.clear();
    return v.text;
  }

  clear() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.value === null) return;
    this.value = null;
    this.publish();
  }
}
