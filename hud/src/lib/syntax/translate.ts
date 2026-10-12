// nvim-treesitter's queries, made to run on web-tree-sitter (ED-5, editor
// plan §2.6). Used by the dev-run vendoring script (`scripts/vendor-queries.mjs`),
// never at run time: what the HUD bundles is this module's output, so the
// syntax worker only ever compiles text that already works.
//
// What it does to a query file:
//  - splits it into top-level patterns (comments and strings understood), so a
//    pattern can be dropped alone;
//  - `#lua-match?` (a Lua pattern) becomes `#match?` (a JS regular
//    expression), and `#not-lua-match?` / `#any-lua-match?` likewise, for the
//    pattern classes these queries use (`luaToRegex`);
//  - keeps what web-tree-sitter evaluates itself (`#eq?`, `#match?`,
//    `#any-of?` and their `not-`/`any-` forms, `#set!`) and what the syntax
//    worker evaluates (`#has-ancestor?`, `#has-parent?` and their `not-`
//    forms, `#offset!` on an injection's content, `#trim!` on a fold);
//  - takes out `#set! @capture key value` (nvim's per-capture metadata —
//    `url`, `bo.commentstring` — which web-tree-sitter refuses to compile and
//    nothing here reads), keeping the pattern;
//  - drops every pattern that uses anything else — an nvim-only predicate or
//    directive such as `#gsub!`, or a Lua pattern with no JS equivalent
//    (`%b`, `%f`) — and lists each one with its reason.
//
// `#set! priority N` needs no translation: web-tree-sitter hands it to the
// worker as the pattern's `setProperties`, which `flatten` sorts by.

export interface Pattern {
  /** The pattern's text, from its first token to its last (trailing comments excluded). */
  text: string;
  /** The text before it since the previous pattern: comments and blank lines, kept as they were. */
  lead: string;
  /** 1-based line of its first character in the source. */
  line: number;
  /** The query file it came from (`<nvim dir>/<kind>.scm`), once translated. */
  file?: string;
}

export interface Dropped {
  file: string;
  line: number;
  reason: string;
  pattern: string;
}

export interface Translated {
  text: string;
  dropped: Dropped[];
  /** Directives taken out of a pattern that stays: nvim-only `#set! @capture …` metadata. */
  stripped: Dropped[];
  /** How many patterns came in. */
  patterns: number;
  /** The patterns kept, translated. */
  kept: Pattern[];
}

/** Predicates and directives web-tree-sitter evaluates itself. */
export const NATIVE = new Set([
  "eq?", "not-eq?", "any-eq?", "match?", "not-match?", "any-match?", "any-of?", "not-any-of?",
  "is?", "is-not?", "set!",
]);

/**
 * Predicates and directives the syntax worker evaluates (lib/syntax/engine.ts;
 * `#trim!` in lib/syntax/folds.ts, ED-6).
 */
export const WORKER = new Set([
  "has-ancestor?", "not-has-ancestor?", "has-parent?", "not-has-parent?", "offset!", "trim!",
]);

/** The nvim query files the HUD bundles per language: colour and injections (ED-5), folds (ED-6). */
export type QueryKind = "highlights" | "injections" | "folds";

/** Lua-pattern predicates, and what each becomes. */
export const LUA = new Map([
  ["lua-match?", "match?"],
  ["not-lua-match?", "not-match?"],
  ["any-lua-match?", "any-match?"],
]);

// ---- splitting a query into patterns ----------------------------------------

/**
 * The top-level patterns of a query source. A pattern is one primary — a
 * parenthesised node, a `[...]` alternation, a string, or `_` — with the
 * quantifiers and captures that follow it at the top level.
 */
export function splitPatterns(source: string): Pattern[] {
  const out: Pattern[] = [];
  let i = 0;
  let leadStart = 0;
  const n = source.length;
  const lineAt = (k: number) => {
    let line = 1;
    for (let j = 0; j < k; j++) if (source.charCodeAt(j) === 10) line++;
    return line;
  };
  const skipString = (k: number) => {
    // k at the opening quote; returns the index after the closing one.
    k++;
    while (k < n && source[k] !== '"') k += source[k] === "\\" ? 2 : 1;
    if (k >= n) throw new Error(`unterminated string at line ${lineAt(k)}`);
    return k + 1;
  };
  const skipComment = (k: number) => {
    while (k < n && source[k] !== "\n") k++;
    return k;
  };
  const skipGroup = (k: number) => {
    // k at `(` or `[`; returns the index after the matching close.
    let depth = 0;
    while (k < n) {
      const c = source[k];
      if (c === '"') { k = skipString(k); continue; }
      if (c === ";") { k = skipComment(k); continue; }
      if (c === "(" || c === "[") depth++;
      else if (c === ")" || c === "]") {
        depth--;
        if (depth === 0) return k + 1;
      }
      k++;
    }
    throw new Error(`unbalanced brackets from line ${lineAt(k)}`);
  };
  const isSpace = (c: string) => c === " " || c === "\t" || c === "\n" || c === "\r";
  while (i < n) {
    const c = source[i];
    if (isSpace(c)) { i++; continue; }
    if (c === ";") { i = skipComment(i); continue; }
    const start = i;
    if (c === "(" || c === "[") i = skipGroup(i);
    else if (c === '"') i = skipString(i);
    else if (c === "_") i++;
    else throw new Error(`unexpected ${JSON.stringify(c)} at line ${lineAt(i)}`);
    // Quantifiers and captures that belong to this primary.
    for (;;) {
      let k = i;
      while (k < n && isSpace(source[k])) k++;
      if (source[k] === "?" || source[k] === "*" || source[k] === "+") { i = k + 1; continue; }
      if (source[k] === "@") {
        k++;
        while (k < n && /[\w.\-]/.test(source[k])) k++;
        i = k;
        continue;
      }
      break;
    }
    out.push({ text: source.slice(start, i), lead: source.slice(leadStart, start), line: lineAt(start) });
    leadStart = i;
  }
  return out;
}

/** Every `(#name ...)` predicate in a pattern: its name and its raw argument text. */
export function predicatesOf(pattern: string): { name: string; start: number; end: number }[] {
  const out: { name: string; start: number; end: number }[] = [];
  let k = 0;
  while (k < pattern.length) {
    const c = pattern[k];
    if (c === '"') {
      k++;
      while (k < pattern.length && pattern[k] !== '"') k += pattern[k] === "\\" ? 2 : 1;
      k++;
      continue;
    }
    if (c === ";") {
      while (k < pattern.length && pattern[k] !== "\n") k++;
      continue;
    }
    if (c === "(" && pattern[k + 1] === "#") {
      const m = /^\(#([\w\-]+[?!])/.exec(pattern.slice(k));
      if (m) {
        // Find the predicate's own close paren.
        let j = k + 1;
        let depth = 1;
        while (j < pattern.length && depth > 0) {
          const d = pattern[j];
          if (d === '"') {
            j++;
            while (j < pattern.length && pattern[j] !== '"') j += pattern[j] === "\\" ? 2 : 1;
          } else if (d === "(") depth++;
          else if (d === ")") depth--;
          j++;
        }
        out.push({ name: m[1], start: k, end: j });
        k = j;
        continue;
      }
    }
    k++;
  }
  return out;
}

// ---- Lua patterns -> JS regular expressions ---------------------------------

const LUA_CLASS: Record<string, string> = {
  a: "A-Za-z",
  d: "0-9",
  l: "a-z",
  u: "A-Z",
  s: " \\t\\n\\r\\f\\v",
  w: "A-Za-z0-9",
  x: "0-9A-Fa-f",
  p: "!-\\/:-@\\[-`{-~",
  c: "\\x00-\\x1f\\x7f",
  g: "!-~",
};

/** A character as a literal inside a JS character class. */
const setLiteral = (c: string) => (/[\\\]\[^\-]/.test(c) ? `\\${c}` : c);
/** A character as a literal outside a class. */
const literal = (c: string) => (/[\\^$.*+?()[\]{}|\/]/.test(c) ? `\\${c}` : c);

export class Untranslatable extends Error {}

/**
 * A Lua pattern (as `string.find` reads it, in the C locale) as the source of
 * an equivalent JS regular expression. Covers anchors, `.`, the `%` classes
 * and their complements, `%` escapes, sets (with classes and ranges inside),
 * and the quantifiers `* + ? -`. `%b` (balanced), `%f` (frontier) and
 * back-references have no JS equivalent here and throw `Untranslatable`.
 */
export function luaToRegex(lua: string): string {
  let out = "";
  let i = 0;
  const n = lua.length;
  // Whether the previous emitted item can take a quantifier.
  let quantifiable = false;
  const classOf = (c: string): string => {
    const lower = c.toLowerCase();
    if (lower === "b" || lower === "f") throw new Untranslatable(`Lua's %${c} has no JS equivalent`);
    if (/[1-9]/.test(c)) throw new Untranslatable(`Lua back-reference %${c}`);
    const body = LUA_CLASS[lower];
    if (!body) return literal(c);
    return c === lower ? `[${body}]` : `[^${body}]`;
  };
  if (lua[0] === "^") {
    out += "^";
    i = 1;
  }
  while (i < n) {
    const c = lua[i];
    if (c === "$" && i === n - 1) {
      out += "$";
      i++;
      quantifiable = false;
      continue;
    }
    if (c === "%") {
      if (i + 1 >= n) throw new Untranslatable("a pattern ending in %");
      out += classOf(lua[i + 1]);
      i += 2;
      quantifiable = true;
      continue;
    }
    if (c === "[") {
      // A set: [^...], with %classes, ranges and literals; `]` first is literal.
      let j = i + 1;
      let body = "";
      let negate = false;
      if (lua[j] === "^") {
        negate = true;
        j++;
      }
      let first = true;
      while (j < n && (lua[j] !== "]" || first)) {
        const d = lua[j];
        first = false;
        if (d === "%") {
          if (j + 1 >= n) throw new Untranslatable("a set ending in %");
          const e = lua[j + 1];
          const lower = e.toLowerCase();
          const cls = LUA_CLASS[lower];
          if (cls) {
            if (e !== lower) throw new Untranslatable(`a complemented class %${e} inside a set`);
            body += cls;
          } else body += setLiteral(e);
          j += 2;
          continue;
        }
        if (lua[j + 1] === "-" && j + 2 < n && lua[j + 2] !== "]") {
          body += `${setLiteral(d)}-${setLiteral(lua[j + 2])}`;
          j += 3;
          continue;
        }
        body += setLiteral(d);
        j++;
      }
      if (j >= n) throw new Untranslatable("an unterminated set");
      out += `[${negate ? "^" : ""}${body}]`;
      i = j + 1;
      quantifiable = true;
      continue;
    }
    if ((c === "*" || c === "+" || c === "?" || c === "-") && quantifiable) {
      out += c === "-" ? "*?" : c;
      i++;
      quantifiable = false;
      continue;
    }
    if (c === "(" || c === ")") {
      // Lua captures group like JS ones; `()` (a position capture) does not.
      if (c === "(" && lua[i + 1] === ")") throw new Untranslatable("a Lua position capture ()");
      out += c;
      i++;
      quantifiable = c === ")" ? false : false;
      continue;
    }
    // Lua's `.` is any character, a line break included; JS's is not.
    out += c === "." ? "[\\s\\S]" : literal(c);
    i++;
    quantifiable = true;
  }
  // The result must compile, and mean the same thing to `RegExp` as to tree-sitter.
  new RegExp(out);
  return out;
}

/** A query string literal's value (tree-sitter's escapes: \n \t \r \0 \\ \"). */
export function unquote(literalText: string): string {
  const body = literalText.slice(1, -1);
  return body.replace(/\\(.)/g, (_, c: string) => ({ n: "\n", t: "\t", r: "\r", "0": "\0" })[c] ?? c);
}

/** A value as a query string literal. */
export function quote(value: string): string {
  return `"${value.replace(/[\\"]/g, (c) => `\\${c}`).replace(/\n/g, "\\n").replace(/\t/g, "\\t").replace(/\r/g, "\\r")}"`;
}

// ---- translating one query file --------------------------------------------

/**
 * One query file made web-tree-sitter-ready. `file` names it in the dropped
 * list. A pattern whose predicates cannot all be kept or translated is left
 * out whole, with its reason.
 */
export function translateQuery(source: string, file: string): Translated {
  const dropped: Dropped[] = [];
  const stripped: Dropped[] = [];
  const kept: Pattern[] = [];
  const patterns = splitPatterns(source);
  for (const p of patterns) {
    let text = p.text;
    let reason = "";
    // Right to left, so earlier offsets stay valid as later ones change.
    const preds = predicatesOf(text).reverse();
    for (const pred of preds) {
      if (pred.name === "set!" && /^\(#set!\s+@/.test(text.slice(pred.start, pred.end))) {
        stripped.push({ file, line: p.line, reason: "#set! on a capture is nvim-only metadata", pattern: text.slice(pred.start, pred.end) });
        text = text.slice(0, pred.start).replace(/[ \t]*\n?[ \t]*$/, "") + text.slice(pred.end);
        continue;
      }
      if (NATIVE.has(pred.name) || WORKER.has(pred.name)) continue;
      const to = LUA.get(pred.name);
      if (!to) {
        reason = `#${pred.name} is not supported`;
        break;
      }
      const inner = text.slice(pred.start, pred.end);
      const m = /^\(#[\w\-]+[?!]\s+(@[\w.\-]+)\s+("(?:[^"\\]|\\.)*")\s*\)$/s.exec(inner);
      if (!m) {
        reason = `#${pred.name} takes one capture and one pattern`;
        break;
      }
      try {
        const regex = luaToRegex(unquote(m[2]));
        text = `${text.slice(0, pred.start)}(#${to} ${m[1]} ${quote(regex)})${text.slice(pred.end)}`;
      } catch (e) {
        reason = e instanceof Error ? e.message : String(e);
        break;
      }
    }
    if (reason) {
      dropped.push({ file, line: p.line, reason, pattern: p.text });
      continue;
    }
    kept.push({ ...p, text, file });
  }
  return { text: joinPatterns(kept), dropped, stripped, patterns: patterns.length, kept };
}

const joinPatterns = (kept: readonly Pattern[]) => `${kept.map((p) => p.lead + p.text).join("").trim()}\n`;

/** `; inherits: a,b` on a query's first lines: the languages it builds on, in order. */
export function inheritsOf(source: string): string[] {
  const m = /^;+\s*inherits\s*:?\s*([^\n]*)$/m.exec(source.split("\n").slice(0, 3).join("\n"));
  if (!m) return [];
  return m[1].split(",").map((s) => s.trim().replace(/^\((.*)\)$/, "$1")).filter(Boolean);
}

/**
 * The query files a language's query is made of, as nvim resolves `inherits`:
 * each parent's (itself resolved) first, in the order named, then its own;
 * a file reached twice is read once.
 */
export function inheritanceOrder(lang: string, read: (lang: string) => string | null, seen: string[] = []): string[] {
  if (seen.includes(lang)) throw new Error(`an inherits cycle: ${[...seen, lang].join(" -> ")}`);
  const own = read(lang);
  if (own === null) return [];
  const out: string[] = [];
  for (const parent of inheritsOf(own)) {
    for (const l of inheritanceOrder(parent, read, [...seen, lang])) if (!out.includes(l)) out.push(l);
  }
  if (!out.includes(lang)) out.push(lang);
  return out;
}

export interface Built extends Omit<Translated, "kept"> {
  /** The nvim query files it was built from, in order. */
  files: string[];
}

/**
 * One bundled query: the language's nvim query files, `inherits` resolved by
 * concatenation, each translated (`translateQuery`), then compiled against
 * the grammar (`compiles` returns the runtime's error, or null). A pattern the
 * grammar refuses — a node type or field this grammar version does not have —
 * is dropped alone and listed; the query that is left must compile whole.
 */
export function buildQuery(
  lang: string,
  kind: QueryKind,
  read: (lang: string, kind: string) => string | null,
  compiles: (text: string) => string | null,
): Built {
  const files = inheritanceOrder(lang, (l) => read(l, kind));
  const dropped: Dropped[] = [];
  const stripped: Dropped[] = [];
  const kept: Pattern[] = [];
  let patterns = 0;
  for (const l of files) {
    const source = (read(l, kind) ?? "").replace(/^;+\s*inherits\s*:?[^\n]*\n/m, "");
    const t = translateQuery(source, `${l}/${kind}.scm`);
    dropped.push(...t.dropped);
    stripped.push(...t.stripped);
    patterns += t.patterns;
    // A file boundary starts a new line, as concatenation would.
    t.kept.forEach((p, i) => kept.push(i === 0 && kept.length ? { ...p, lead: `\n${p.lead}` } : p));
  }
  let text = joinPatterns(kept);
  if (compiles(text) !== null) {
    const ok: Pattern[] = [];
    for (const p of kept) {
      const why = compiles(p.text);
      if (why === null) ok.push(p);
      else dropped.push({ file: p.file ?? `${lang}/${kind}.scm`, line: p.line, reason: `does not compile against the grammar: ${why}`, pattern: p.text });
    }
    text = joinPatterns(ok);
    const still = compiles(text);
    if (still !== null) throw new Error(`${lang}/${kind}: the query does not compile even pattern by pattern: ${still}`);
  }
  return { text, dropped, stripped, patterns, files };
}
