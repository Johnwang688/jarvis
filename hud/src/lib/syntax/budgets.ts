// Tree-sitter colour's budgets (ED-5, editor plan §2.6). You never see a
// broken file, only a quieter one: past any of these, the document (or the
// part of a request) stays on Monarch. Here, with no imports, so the main
// thread can read them without pulling in the worker's engine.

export const BUDGETS = {
  /** A document longer than this (UTF-16 code units, ~1 MB) stays on Monarch: "syntax: basic (large file)". */
  maxChars: 1 << 20,
  /** The first parse of a document, cancelled past this: "syntax: basic (slow parse)". */
  firstParseMs: 1000,
  /** An incremental reparse, cancelled past this; the last tree is kept and its colour shown a moment longer. */
  reparseMs: 250,
  /** After a cancelled reparse, try again once edits have been idle this long. */
  retryIdleMs: 500,
  /** Cancelled reparses in a row before the document goes back to Monarch: "syntax: basic (slow edits)". */
  maxCancels: 2,
  /** One token request: time, and captures collected, before it stops at the lines it has. */
  queryMs: 8,
  maxCaptures: 20_000,
  /** Injected text parsed per document, in document order; past it a fence keeps its host's colour. */
  injectedChars: 256 * 1024,
  /** Edits closer together than this are one reparse. */
  coalesceMs: 30,
  /** Lines queried at a time, so a request can stop between chunks. */
  chunkLines: 64,
};

export type Budgets = typeof BUDGETS;

export type DocState = "loading" | "ready" | "basic";

/** Why a document is on Monarch: the status corner's words, after "syntax: basic". */
export type BasicReason = "large file" | "slow parse" | "slow edits" | "grammar failed";
