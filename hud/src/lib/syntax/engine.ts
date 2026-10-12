// The syntax worker's engine (ED-5, editor plan §2.6): documents, their trees,
// incremental reparse, injections, the budgets, and semantic tokens for a
// range of lines. No messages and no timers here — `worker.ts` is the shell
// that owns those — so the tests drive this directly, under Node, with the
// same web-tree-sitter and the same grammars the window loads.

import { Edit, Parser } from "web-tree-sitter";
import type { Language, Node, Query, QueryMatch, Tree } from "web-tree-sitter";
import { encode, flatten, type Capture } from "./flatten";
import { GRAMMARS, STYLED_AS, injectionGrammar, type GrammarName } from "./grammars";
import { NONE_TYPE, modifierOf, typeOf } from "./legend";
import { applyChange, Lines, queryRange, TsLines, type Change } from "./positions";
import { BUDGETS, type BasicReason, type Budgets, type DocState } from "./budgets";

export { BUDGETS };
export type { BasicReason, Budgets, DocState };

export interface GrammarAssets {
  language: Language;
  highlights: Query;
  injections: Query | null;
}

export interface EngineOptions {
  /** A grammar's language and compiled queries; refused or failed → rejects. */
  load(grammar: GrammarName): Promise<GrammarAssets>;
  now?: () => number;
  budgets?: Partial<Budgets>;
  /** Told when a document's colour may have changed without an edit (a grammar for an injection arrived). */
  changed?: (id: string) => void;
}

interface Injection {
  start: number;
  end: number;
  grammar: GrammarName;
  /** Within the document's injected-text budget, in document order. */
  parsed: boolean;
}

interface Doc {
  id: string;
  grammar: GrammarName;
  modifier: number;
  text: string;
  version: number;
  tree: Tree | null;
  dirty: boolean;
  state: DocState;
  reason?: BasicReason;
  cancels: number;
  lines: Lines | null;
  injections: Injection[] | null;
  /** Injections queries cancelled in a row; at `maxCancels` the document's injections stay off. */
  injectionCancels: number;
}

export interface TokensResult {
  version: number;
  /** null: nothing new to draw (not ready, on Monarch, stale, or a version it never saw). */
  data: Uint32Array | null;
  state: DocState;
  reason?: BasicReason;
  /** The last line tokens were computed for, when a budget stopped short of `last`. */
  truncatedAt?: number;
  stale?: boolean;
}

/** One captured span relative to its injected text. */
interface RelCapture {
  start: number;
  end: number;
  type: number;
  modifier: number;
  priority: number;
  pattern: number;
}

export class SyntaxEngine {
  readonly budgets: Budgets;
  private readonly now: () => number;
  private readonly docs = new Map<string, Doc>();
  private readonly assets = new Map<GrammarName, GrammarAssets>();
  private readonly loading = new Map<GrammarName, Promise<GrammarAssets | null>>();
  private readonly parsers = new Map<GrammarName, Parser>();
  /** Injected text → its captures, least recently used first. */
  private readonly injected = new Map<string, RelCapture[]>();
  private injectedChars = 0;
  private order = 0;

  constructor(private readonly opts: EngineOptions) {
    this.budgets = { ...BUDGETS, ...opts.budgets };
    this.now = opts.now ?? (() => performance.now());
  }

  // ---- grammars ----------------------------------------------------------

  private grammar(name: GrammarName): Promise<GrammarAssets | null> {
    let p = this.loading.get(name);
    if (!p) {
      p = this.opts.load(name).then(
        (a) => {
          this.assets.set(name, a);
          const parser = new Parser();
          parser.setLanguage(a.language);
          this.parsers.set(name, parser);
          return a;
        },
        () => null,
      );
      this.loading.set(name, p);
    }
    return p;
  }

  /** Load a grammar ahead of need (tests; an injection's grammar otherwise loads on first sight). */
  async preload(name: GrammarName): Promise<boolean> {
    return !!(await this.grammar(name));
  }

  // ---- documents ---------------------------------------------------------

  /**
   * Open a document: load its grammar, then its first parse, within the
   * budget. Resolves to its state (`basic` with the reason when it is staying
   * on Monarch).
   */
  async open(id: string, grammar: GrammarName, text: string, version: number, styledAs: string): Promise<{ state: DocState; reason?: BasicReason }> {
    this.close(id);
    const doc: Doc = {
      id, grammar, modifier: modifierOf(styledAs), text, version, tree: null, dirty: false,
      state: "loading", cancels: 0, lines: null, injections: null, injectionCancels: 0,
    };
    this.docs.set(id, doc);
    if (text.length > this.budgets.maxChars) return this.basic(doc, "large file");
    const assets = await this.grammar(grammar);
    if (this.docs.get(id) !== doc) return { state: "basic" }; // closed or reopened meanwhile
    if (!assets) return this.basic(doc, "grammar failed");
    if (doc.text.length > this.budgets.maxChars) return this.basic(doc, "large file");
    const tree = this.parse(grammar, doc.text, null, this.budgets.firstParseMs);
    if (!tree) return this.basic(doc, "slow parse");
    doc.tree = tree;
    doc.dirty = false;
    doc.state = "ready";
    return { state: "ready" };
  }

  private basic(doc: Doc, reason: BasicReason) {
    doc.state = "basic";
    doc.reason = reason;
    doc.tree?.delete();
    doc.tree = null;
    doc.injections = null;
    return { state: "basic" as const, reason };
  }

  close(id: string) {
    const doc = this.docs.get(id);
    if (!doc) return;
    doc.tree?.delete();
    doc.tree = null;
    this.docs.delete(id);
  }

  state(id: string): { state: DocState; reason?: BasicReason; version: number } | null {
    const doc = this.docs.get(id);
    return doc ? { state: doc.state, reason: doc.reason, version: doc.version } : null;
  }

  /** The document's text as the worker holds it (tests). */
  text(id: string): string | null {
    return this.docs.get(id)?.text ?? null;
  }

  /**
   * Monaco's changes for one version, in the order Monaco lists them (from the
   * end of the document back, each offset in the text before the event). The
   * tree is edited at once; the reparse waits (`reparse`).
   */
  change(id: string, version: number, changes: readonly Change[]): { state: DocState; reason?: BasicReason } | null {
    const doc = this.docs.get(id);
    if (!doc) return null;
    let text = doc.text;
    for (const c of changes) {
      const { text: next, edit } = applyChange(text, c, doc.tree ? new TsLines(text) : undefined);
      if (doc.tree) doc.tree.edit(new Edit(edit));
      text = next;
    }
    doc.text = text;
    doc.version = version;
    doc.lines = null;
    doc.injections = null;
    if (doc.state === "basic") return { state: "basic", reason: doc.reason };
    if (text.length > this.budgets.maxChars) return this.basic(doc, "large file");
    doc.dirty = true;
    return { state: doc.state };
  }

  /**
   * Bring the tree up to the text, within the reparse budget. `ok`: fresh.
   * `stale`: cancelled, the last tree kept (its ranges moved with the edits);
   * try again later. `basic`: cancelled `maxCancels` times in a row, so the
   * document is back on Monarch.
   */
  reparse(id: string): "ok" | "stale" | "basic" | "absent" {
    const doc = this.docs.get(id);
    if (!doc || doc.state === "loading") return "absent";
    if (doc.state === "basic") return "basic";
    if (!doc.dirty) return "ok";
    const tree = this.parse(doc.grammar, doc.text, doc.tree, this.budgets.reparseMs);
    if (!tree) {
      doc.cancels++;
      if (doc.cancels >= this.budgets.maxCancels) {
        this.basic(doc, "slow edits");
        return "basic";
      }
      return "stale";
    }
    doc.tree?.delete();
    doc.tree = tree;
    doc.dirty = false;
    doc.cancels = 0;
    doc.injections = null;
    return "ok";
  }

  private parse(grammar: GrammarName, text: string, old: Tree | null, budgetMs: number): Tree | null {
    const parser = this.parsers.get(grammar);
    if (!parser) return null;
    const deadline = this.now() + budgetMs;
    // Returning true from the progress callback cancels the parse.
    const tree = parser.parse(text, old, { progressCallback: () => this.now() > deadline });
    if (!tree) {
      // A cancelled parse resumes on the next call unless reset — and the
      // next call is for different text.
      parser.reset();
      return null;
    }
    return tree;
  }

  // ---- tokens ------------------------------------------------------------

  /**
   * Semantic tokens for Monaco lines `[first, last]` (0-based) of version
   * `version`. Within the query budget: it stops at the end of the chunk
   * where time or captures ran out, and says so (`truncatedAt`).
   */
  tokens(id: string, version: number, first: number, last: number): TokensResult {
    const doc = this.docs.get(id);
    if (!doc) return { version: -1, data: null, state: "basic" };
    const base = { version: doc.version, state: doc.state, reason: doc.reason };
    if (doc.state !== "ready" || version !== doc.version) return { ...base, data: null };
    const r = this.reparse(id);
    if (r === "basic") return { version: doc.version, data: null, state: "basic", reason: doc.reason };
    if (r === "stale") return { ...base, data: null, stale: true };
    const lines = (doc.lines ??= new Lines(doc.text));
    first = Math.max(0, first);
    last = Math.min(last, lines.count - 1);
    if (last < first) return { ...base, data: new Uint32Array(0) };
    const deadline = this.now() + this.budgets.queryMs;
    const captures: Capture[] = [];
    let done = first - 1;
    let truncatedAt: number | undefined;
    for (let l = first; l <= last; l += this.budgets.chunkLines) {
      const end = Math.min(l + this.budgets.chunkLines - 1, last);
      const from = lines.start(l), to = lines.end(end);
      const had = captures.length;
      if (!this.collect(doc, from, to, captures)) {
        // A query run past `matchMs`: its matches are partial, so this chunk
        // is dropped and the answer stops at the lines before it.
        captures.length = had;
        truncatedAt = done;
        break;
      }
      this.collectInjected(doc, from, to, captures);
      done = end;
      if (end < last && (this.now() > deadline || captures.length > this.budgets.maxCaptures)) {
        truncatedAt = end;
        break;
      }
    }
    const data = encode(flatten(captures), lines, doc.text, first, done, NONE_TYPE);
    return { ...base, data, ...(truncatedAt !== undefined ? { truncatedAt } : {}) };
  }

  /** The document's own captures over `[from, to)`; false when the query ran past its budget. */
  private collect(doc: Doc, from: number, to: number, out: Capture[]): boolean {
    const a = this.assets.get(doc.grammar);
    if (!a || !doc.tree) return true;
    const { matches, cancelled } = this.matches(a.highlights, doc.tree.rootNode, queryRange(from, to));
    if (cancelled) return false;
    for (const m of matches) this.push(a.highlights, m, 0, doc.modifier, 0, out);
    return true;
  }

  /**
   * `query.matches(node, range)` within `matchMs` (web-tree-sitter's
   * `progressCallback`: returning true cancels the run). `cancelled`: the
   * budget stopped it, and its matches are partial — never used as colour.
   */
  private matches(query: Query, node: Node, range: { startIndex: number; endIndex: number } | null = null) {
    const deadline = this.now() + this.budgets.matchMs;
    let cancelled = false;
    const matches = query.matches(node, {
      ...(range ?? {}),
      progressCallback: () => {
        if (!cancelled && this.now() > deadline) cancelled = true;
        return cancelled;
      },
    });
    return { matches, cancelled };
  }

  private push(query: Query, m: QueryMatch, offset: number, modifier: number, depth: number, out: Capture[]) {
    if (!workerPredicates(query, m)) return;
    const priority = priorityOf(m);
    for (const c of m.captures) {
      const type = typeOf(c.name);
      if (type < 0) continue;
      const start = c.node.startIndex, end = c.node.endIndex;
      if (end <= start) continue;
      out.push({
        start: start + offset, end: end + offset, type, modifier, priority, depth, pattern: m.patternIndex,
        order: this.order++,
      });
    }
  }

  // ---- injections --------------------------------------------------------

  /** Every injection in the document, in order, with the injected-text budget applied. */
  private injectionsOf(doc: Doc): Injection[] {
    if (doc.injections) return doc.injections;
    const a = this.assets.get(doc.grammar);
    const list: Injection[] = [];
    if (a?.injections && doc.tree && doc.injectionCancels < this.budgets.maxCancels) {
      // The whole document, after every edit: within `matchMs`, or none at
      // all for this version (fences keep their host's colour) — and twice
      // running, none for this document from then on.
      const { matches, cancelled } = this.matches(a.injections, doc.tree.rootNode);
      if (cancelled) {
        doc.injectionCancels++;
        doc.injections = [];
        return doc.injections;
      }
      doc.injectionCancels = 0;
      const ts = new TsLines(doc.text);
      for (const m of matches) {
        if (!workerPredicates(a.injections, m)) continue;
        const props = m.setProperties ?? {};
        let name: string | null = null;
        const langCapture = m.captures.find((c) => c.name === "injection.language");
        if (langCapture) name = langCapture.node.text;
        else if (typeof props["injection.language"] === "string") name = props["injection.language"];
        else if (Object.hasOwn(props, "injection.self") || Object.hasOwn(props, "injection.parent")) name = doc.grammar;
        // Own keys only: a fence tagged `constructor` or `__proto__` names no grammar.
        const grammar = name ? (Object.hasOwn(GRAMMARS, name) ? (name as GrammarName) : injectionGrammar(name)) : null;
        if (!grammar) continue;
        const offsets = offsetsOf(a.injections, m);
        for (const c of m.captures) {
          if (c.name !== "injection.content") continue;
          let start = c.node.startIndex, end = c.node.endIndex;
          if (offsets) {
            const sp = c.node.startPosition, ep = c.node.endPosition;
            start = ts.offset({ row: sp.row + offsets[0], column: sp.column + offsets[1] });
            end = ts.offset({ row: ep.row + offsets[2], column: ep.column + offsets[3] });
          }
          if (end > start) list.push({ start, end, grammar, parsed: false });
        }
      }
    }
    list.sort((x, y) => x.start - y.start || y.end - x.end);
    // One level, no overlaps: an injection inside an earlier one is left out.
    const kept: Injection[] = [];
    let budget = this.budgets.injectedChars;
    for (const inj of list) {
      const prev = kept[kept.length - 1];
      if (prev && inj.start < prev.end) continue;
      const size = inj.end - inj.start;
      inj.parsed = size <= budget;
      if (inj.parsed) budget -= size;
      else budget = 0; // in document order: once over, every later one is too
      kept.push(inj);
    }
    doc.injections = kept;
    return kept;
  }

  private collectInjected(doc: Doc, from: number, to: number, out: Capture[]) {
    const list = this.injectionsOf(doc);
    // First injection that ends after `from`.
    let lo = 0, hi = list.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (list[mid].end <= from) lo = mid + 1;
      else hi = mid;
    }
    for (let k = lo; k < list.length && list[k].start < to; k++) {
      const inj = list[k];
      if (!inj.parsed) continue;
      const rel = this.capturesOfInjected(doc, inj);
      if (!rel) continue;
      for (const c of rel) {
        const s = c.start + inj.start, e = c.end + inj.start;
        if (e <= from || s >= to) continue;
        out.push({ start: s, end: e, type: c.type, modifier: c.modifier, priority: c.priority, depth: 1, pattern: c.pattern, order: this.order++ });
      }
    }
  }

  /** The captures of one injected range, cached by its grammar and text; null until its grammar is loaded. */
  private capturesOfInjected(doc: Doc, inj: Injection): RelCapture[] | null {
    const text = doc.text.slice(inj.start, inj.end);
    const key = `${inj.grammar}\u0000${text}`;
    const hit = this.injected.get(key);
    if (hit) {
      this.injected.delete(key);
      this.injected.set(key, hit);
      return hit;
    }
    const a = this.assets.get(inj.grammar);
    if (!a) {
      // Load it, and ask for colour again once it is here.
      void this.grammar(inj.grammar).then((g) => {
        if (g && this.docs.get(doc.id) === doc) this.opts.changed?.(doc.id);
      });
      return null;
    }
    const tree = this.parse(inj.grammar, text, null, this.budgets.reparseMs);
    const rel: RelCapture[] = [];
    if (tree) {
      const modifier = modifierOf(STYLED_AS[inj.grammar]);
      const tmp: Capture[] = [];
      // Past `matchMs` the range keeps its host's colour (an empty answer, cached).
      const { matches, cancelled } = this.matches(a.highlights, tree.rootNode);
      if (!cancelled) for (const m of matches) this.push(a.highlights, m, 0, modifier, 1, tmp);
      for (const c of tmp) rel.push({ start: c.start, end: c.end, type: c.type, modifier: c.modifier, priority: c.priority, pattern: c.pattern });
      tree.delete();
    }
    this.injected.set(key, rel);
    this.injectedChars += text.length;
    // Least recently used out, past 4 MB of text or 20k ranges.
    for (const [k] of this.injected) {
      if (this.injectedChars <= 4 << 20 && this.injected.size <= 20_000) break;
      this.injected.delete(k);
      this.injectedChars -= k.length - k.indexOf("\u0000") - 1;
    }
    return rel;
  }

  /** How many documents and cached injected ranges are held (tests). */
  stats() {
    return { docs: this.docs.size, injected: this.injected.size, grammars: this.assets.size };
  }

  /** The grammars compiled and ready, by name (tests: every grammar loads under the CSP). */
  loaded(): GrammarName[] {
    return [...this.assets.keys()].sort();
  }
}

// ---- predicates the worker evaluates -------------------------------------

function nodesOf(m: QueryMatch, capture: string): Node[] {
  return m.captures.filter((c) => c.name === capture).map((c) => c.node);
}

/**
 * `#has-ancestor?`, `#has-parent?` and their `not-` forms (structural, so
 * evaluated here; web-tree-sitter leaves them to its caller): true when any
 * node of the capture has an ancestor (or parent) of one of the types.
 * Anything else in `predicates` (`#offset!`) is not a filter.
 */
export function workerPredicates(query: Query, m: QueryMatch): boolean {
  for (const p of query.predicates[m.patternIndex] ?? []) {
    const op = p.operator;
    const parentOnly = op === "has-parent?" || op === "not-has-parent?";
    if (!parentOnly && op !== "has-ancestor?" && op !== "not-has-ancestor?") continue;
    const [cap, ...rest] = p.operands;
    if (!cap || cap.type !== "capture") return false;
    const types = new Set(rest.filter((s) => s.type === "string").map((s) => (s as { value: string }).value));
    let found = false;
    for (const node of nodesOf(m, cap.name)) {
      for (let a = node.parent; a; a = parentOnly ? null : a.parent) {
        if (types.has(a.type)) {
          found = true;
          break;
        }
      }
      if (found) break;
    }
    if (found === op.startsWith("not-")) return false;
  }
  return true;
}

/** `#offset! @injection.content r0 c0 r1 c1`, if the pattern has one. */
function offsetsOf(query: Query, m: QueryMatch): [number, number, number, number] | null {
  for (const p of query.predicates[m.patternIndex] ?? []) {
    if (p.operator !== "offset!") continue;
    const [cap, ...nums] = p.operands;
    if (!cap || cap.type !== "capture" || cap.name !== "injection.content") continue;
    const v = nums.map((s) => Number(s.type === "string" ? s.value : NaN));
    if (v.length === 4 && v.every(Number.isFinite)) return v as [number, number, number, number];
  }
  return null;
}

function priorityOf(m: QueryMatch): number {
  const p = Number(m.setProperties?.priority);
  return Number.isFinite(p) ? p : 100;
}
