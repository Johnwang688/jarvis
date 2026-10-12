// `.editorconfig` (ED-8, editor plan §2.8), as the EditorConfig specification
// reads it (https://spec.editorconfig.org): pure, so the parser and the globs
// are tested without a window. `state/editorconfig.ts` finds the files (through
// the editor's own owner-only routes) and applies what they say.
//
// **What it sets, and only that**: the indent style and size of a file it
// matches, and the line ending a file with no line break yet takes for its
// new lines. **It never rewrites a file on save** (decisions E-9: no format
// on save): `trim_trailing_whitespace`, `insert_final_newline` and `charset`
// are read and ignored, and a file that already has line endings keeps them.
//
// Where it looks: the file's own folder, then each folder above it up to the
// project's root — **never above the project root** — stopping at a file that
// says `root = true` in its preamble. Nearer files win, and within a file the
// last matching section wins.
//
// **An `.editorconfig` is project content an agent can write**, and it is
// judged on the main thread of the window that gates approvals. So every part
// of it is bounded, and **one resolution has one work budget**
// (`MATCH_BUDGET`, review of PR #59): compiling and matching every section of
// every file on the walk is charged to it, and past it the file being opened
// has no configuration at all (said once, by the caller). Per glob: 64
// alternatives, 512 tokens each and 4,096 in all, braces 16 deep; per file:
// 128 sections; per path: 1,024 characters.

/** Spec limits (0.17): longer names, keys and values are ignored. */
export const SECTION_MAX = 4096;
export const KEY_MAX = 1024;
export const VALUE_MAX = 4096;
/** A file bigger than this is not read as a configuration (it is not one). */
export const FILE_MAX = 64 * 1024;
/** Folders walked up from a file, at most (the stat route takes 100 paths). */
export const DEPTH_MAX = 32;
/** Sections one file may hold: past this the file is no configuration (`tooMany`). */
export const SECTIONS_MAX = 128;
/** Braces nested deeper than this make a glob that matches nothing. */
const BRACE_DEPTH_MAX = 16;

export interface Section {
  glob: string;
  props: Record<string, string>;
}

export interface ConfigFile {
  /** `root = true` in the preamble: no folder above this one is read. */
  root: boolean;
  sections: Section[];
  /** More than `SECTIONS_MAX` sections: it configures nothing. */
  tooMany?: boolean;
}

/** The properties whose values the spec says are case-insensitive. */
const LOWER_VALUES = new Set([
  "indent_style", "indent_size", "tab_width", "end_of_line", "charset",
  "trim_trailing_whitespace", "insert_final_newline", "root",
]);

/**
 * Parse an `.editorconfig`: blank lines, `#`/`;` comment lines, `[glob]`
 * section headers (brackets allowed inside the glob) and `key = value` pairs.
 * Keys are case-insensitive, and so are the values of the spec's own
 * properties. A line that is none of these is ignored, as is a pair in the
 * preamble other than `root`. Past `SECTIONS_MAX` sections it stops and says
 * so (`tooMany`).
 */
export function parseEditorConfig(text: string): ConfigFile {
  const out: ConfigFile = { root: false, sections: [] };
  if (typeof text !== "string") return out;
  let current: Section | null = null;
  const body = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  for (const rawLine of body.split(/\r\n|\r|\n/)) {
    const line = rawLine.trim();
    if (!line || line[0] === "#" || line[0] === ";") continue;
    if (line[0] === "[") {
      if (line[line.length - 1] !== "]") {
        current = null; // not a header: its pairs belong to no section
        continue;
      }
      const glob = line.slice(1, -1);
      if (!glob || glob.length > SECTION_MAX) {
        current = null;
        continue;
      }
      if (out.sections.length >= SECTIONS_MAX) {
        out.tooMany = true;
        break;
      }
      current = { glob, props: {} };
      out.sections.push(current);
      continue;
    }
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim().toLowerCase();
    let value = line.slice(eq + 1).trim();
    if (!key || key.length > KEY_MAX || value.length > VALUE_MAX) continue;
    if (LOWER_VALUES.has(key)) value = value.toLowerCase();
    if (current) current.props[key] = value;
    else if (key === "root" && out.sections.length === 0) out.root = value === "true";
  }
  return out;
}

// ---- globs ------------------------------------------------------------------
//
// A section's glob is matched by a table over (glob token, path position) —
// never a backtracking regular expression: `[*a*a*a*a*a*a*a*a*b]` as a regex
// takes minutes against a long file name. The glob is read in one linear
// pass (each bracket's and brace's end found once, `scan`), and its cost —
// the characters read, the tokens made, tokens × path for the match — is
// counted, so the caller can hold all of a file open to one budget.

/** A brace alternative longer than this (in tokens), or a path longer than this, matches nothing. */
export const GLOB_TOKENS_MAX = 512;
/** All of a glob's alternatives together, in tokens: past this, the glob matches nothing. */
export const GLOB_TOTAL_MAX = 4096;
export const PATH_MAX = 1024;
/** Brace alternatives multiply (`{a,b}{c,d}` is four): past this, the glob matches nothing. */
export const ALTERNATIVES_MAX = 64;
/**
 * The work one resolution may do (`resolveProps`): characters read and tokens
 * made while compiling, plus each alternative's weight × (path + 1) to match.
 * About 10 ms on the owner's machine; a real `.editorconfig` spends a few
 * thousand.
 */
export const MATCH_BUDGET = 2_000_000;

type Tok =
  | { k: "lit"; c: string }
  | { k: "star" } // `*`: anything but a slash
  | { k: "globstar" } // `**`: anything
  | { k: "dirstar" } // `**/` at a folder's start: no folder, or any folders
  | { k: "any" } // `?`: one character, not a slash
  | { k: "class"; neg: boolean; spans: number; test: (c: string) => boolean }
  | { k: "num"; lo: number; hi: number }; // `{lo..hi}`: a whole number in range

/** A glob read once: which characters are escaped, and where each bracket and brace ends. */
interface Scan {
  g: string;
  /** 1 where the character is escaped by the backslash before it. */
  esc: Uint8Array;
  /** For an unescaped `[`: its closing `]`, or -1 (then `[` is literal). */
  bracket: Int32Array;
  /** For an unescaped `{`: its matching `}`, or -1. */
  brace: Int32Array;
}

function scan(g: string): Scan {
  const n = g.length;
  const esc = new Uint8Array(n);
  for (let i = 0; i < n; i++) if (g[i] === "\\" && !esc[i] && i + 1 < n) esc[i + 1] = 1;
  // The first unescaped `]` and `/` at or after each position, right to left.
  const close = new Int32Array(n + 1).fill(-1);
  const slash = new Int32Array(n + 1).fill(-1);
  for (let i = n - 1; i >= 0; i--) {
    close[i] = g[i] === "]" && !esc[i] ? i : close[i + 1];
    slash[i] = g[i] === "/" && !esc[i] ? i : slash[i + 1];
  }
  const bracket = new Int32Array(n).fill(-1);
  for (let i = 0; i < n; i++) {
    if (g[i] !== "[" || esc[i]) continue;
    let j = i + 1;
    if (j < n && (g[j] === "!" || g[j] === "^") && !esc[j]) j++;
    if (j < n && g[j] === "]" && !esc[j]) j++; // a leading `]` is a member
    if (j >= n) continue;
    const k = close[j];
    // A slash inside brackets makes them literal.
    if (k >= 0 && !(slash[j] >= 0 && slash[j] < k)) bracket[i] = k;
  }
  const brace = new Int32Array(n).fill(-1);
  const open: number[] = [];
  for (let i = 0; i < n; i++) {
    if (esc[i]) continue;
    const c = g[i];
    if (c === "[" && bracket[i] > 0) i = bracket[i];
    else if (c === "{") open.push(i);
    else if (c === "}" && open.length) brace[open.pop()!] = i;
  }
  return { g, esc, bracket, brace };
}

function classOf(g: string, start: number, end: number, esc: Uint8Array): Tok {
  let i = start;
  let neg = false;
  if (i < end && (g[i] === "!" || g[i] === "^")) {
    neg = true;
    i++;
  }
  const chars: string[] = [];
  for (; i < end; i++) {
    if (g[i] === "\\" && !esc[i] && i + 1 < end) continue; // the escaped character follows
    chars.push(g[i]);
  }
  const singles = new Set<string>();
  const spans: [string, string][] = [];
  for (let j = 0; j < chars.length; j++) {
    if (j + 2 < chars.length && chars[j + 1] === "-") {
      spans.push([chars[j], chars[j + 2]]);
      j += 2;
    } else {
      singles.add(chars[j]);
    }
  }
  return {
    k: "class", neg, spans: spans.length,
    test: (c) => singles.has(c) || spans.some(([a, b]) => c >= a && c <= b),
  };
}

/** What a glob compiled to: its alternatives, their match weight, and the work it took. */
interface Compiled {
  alts: Tok[][];
  /** Σ over alternatives of each token's per-position cost (a class's spans, a number's digits). */
  weight: number;
  /** Characters read and tokens made compiling it. */
  work: number;
}

const tokWeight = (t: Tok) => (t.k === "num" ? 13 : t.k === "class" ? 1 + t.spans : 1);

/**
 * Every alternative's tokens for `g[start, end)` (braces expanded), or null:
 * too many alternatives, too many tokens, too deep. `work` counts what it read.
 */
function expand(sc: Scan, start: number, end: number, depth: number, work: { n: number }): Tok[][] | null {
  if (depth > BRACE_DEPTH_MAX) return null;
  const { g, esc } = sc;
  let seqs: Tok[][] = [[]];
  let longest = 0;
  let total = 0;
  const push = (t: Tok) => {
    for (const s of seqs) s.push(t);
    longest += 1;
    total += seqs.length;
    work.n += seqs.length;
  };
  for (let i = start; i < end; i++) {
    work.n += 1;
    const c = g[i];
    if (esc[i]) {
      push({ k: "lit", c });
    } else if (c === "\\" && i + 1 < g.length) {
      // the escaped character is the next one
    } else if (c === "*") {
      if (i + 1 < end && g[i + 1] === "*" && !esc[i + 1]) {
        const atDir = i === start || (g[i - 1] === "/" && !esc[i - 1]);
        while (i + 1 < end && g[i + 1] === "*" && !esc[i + 1]) i++;
        if (atDir && i + 1 < end && g[i + 1] === "/" && !esc[i + 1]) {
          i++;
          push({ k: "dirstar" });
        } else {
          push({ k: "globstar" });
        }
      } else {
        push({ k: "star" });
      }
    } else if (c === "?") {
      push({ k: "any" });
    } else if (c === "[") {
      const e = sc.bracket[i];
      if (e < 0 || e >= end) push({ k: "lit", c: "[" });
      else {
        work.n += e - i;
        push(classOf(g, i + 1, e, esc));
        i = e;
      }
    } else if (c === "{") {
      const e = sc.brace[i];
      if (e < 0 || e >= end) {
        push({ k: "lit", c: "{" });
        continue;
      }
      // A range is at most `-999999999..-999999999`: anything longer is never sliced.
      const range = e - i - 1 <= 22 ? /^([+-]?\d{1,9})\.\.([+-]?\d{1,9})$/.exec(g.slice(i + 1, e)) : null;
      if (range) {
        const a = parseInt(range[1], 10);
        const b = parseInt(range[2], 10);
        push({ k: "num", lo: Math.min(a, b), hi: Math.max(a, b) });
        i = e;
      } else {
        // The top-level commas split it; a nested brace or a bracket is jumped over.
        const cuts: number[] = [i];
        for (let j = i + 1; j < e; j++) {
          work.n += 1;
          if (esc[j]) continue;
          if (g[j] === "[" && sc.bracket[j] > 0 && sc.bracket[j] < e) j = sc.bracket[j];
          else if (g[j] === "{" && sc.brace[j] > 0 && sc.brace[j] < e) j = sc.brace[j];
          else if (g[j] === ",") cuts.push(j);
        }
        cuts.push(e);
        if (cuts.length < 3) {
          for (let j = i; j <= e; j++) push({ k: "lit", c: g[j] }); // `{single}` is literal
          i = e;
        } else {
          const options: Tok[][] = [];
          for (let a = 0; a + 1 < cuts.length; a++) {
            const sub = expand(sc, cuts[a] + 1, cuts[a + 1], depth + 1, work);
            if (!sub) return null;
            options.push(...sub);
            if (options.length > ALTERNATIVES_MAX) return null;
          }
          if (seqs.length * options.length > ALTERNATIVES_MAX) return null;
          let optTotal = 0;
          let optLongest = 0;
          for (const o of options) {
            optTotal += o.length;
            optLongest = Math.max(optLongest, o.length);
          }
          // Checked before a token is copied: every alternative, never the first alone.
          const nextTotal = total * options.length + optTotal * seqs.length;
          if (nextTotal > GLOB_TOTAL_MAX || longest + optLongest > GLOB_TOKENS_MAX) return null;
          seqs = seqs.flatMap((s) => options.map((o) => [...s, ...o]));
          work.n += nextTotal;
          total = nextTotal;
          longest += optLongest;
          i = e;
        }
      }
    } else {
      push({ k: "lit", c });
    }
    if (longest > GLOB_TOKENS_MAX || total > GLOB_TOTAL_MAX) return null;
  }
  return seqs;
}

/** The length of each whole number (`[+-]?\d+`) starting at `i` whose value is in range. */
function numberEnds(path: string, i: number, lo: number, hi: number): number[] {
  let j = i;
  if (path[j] === "+" || path[j] === "-") j++;
  const digits = j;
  const ends: number[] = [];
  while (j < path.length && path[j] >= "0" && path[j] <= "9" && j - digits < 12) {
    j++;
    const n = parseInt(path.slice(i, j), 10);
    if (n >= lo && n <= hi) ends.push(j);
  }
  return ends;
}

/** One alternative against the path: a table, last token first, one row at a time. */
function matchTokens(toks: Tok[], path: string): boolean {
  const P = path.length;
  let next = new Uint8Array(P + 1);
  next[P] = 1; // no tokens left matches the end of the path
  for (let t = toks.length - 1; t >= 0; t--) {
    const tok = toks[t];
    const row = new Uint8Array(P + 1);
    if (tok.k === "dirstar") {
      // nothing, or anything ending at a slash
      let after = 0;
      for (let i = P; i >= 0; i--) {
        if (i < P && path[i] === "/" && next[i + 1]) after = 1;
        row[i] = next[i] || after;
      }
    } else {
      for (let i = P; i >= 0; i--) {
        const ch = path[i];
        switch (tok.k) {
          case "lit":
            row[i] = i < P && ch === tok.c ? next[i + 1] : 0;
            break;
          case "any":
            row[i] = i < P && ch !== "/" ? next[i + 1] : 0;
            break;
          case "class":
            row[i] = i < P && ch !== "/" && tok.test(ch) !== tok.neg ? next[i + 1] : 0;
            break;
          case "star":
            row[i] = next[i] || (i < P && ch !== "/" ? row[i + 1] : 0);
            break;
          case "globstar":
            row[i] = next[i] || (i < P ? row[i + 1] : 0);
            break;
          case "num":
            row[i] = numberEnds(path, i, tok.lo, tok.hi).some((j) => next[j]) ? 1 : 0;
            break;
        }
      }
    }
    next = row;
  }
  return next[0] === 1;
}

const compiled = new Map<string, Compiled | null>();
const COMPILED_MAX = 512;

/** A section's glob, relative to its file's folder; null if unusable. */
function compileGlob(glob: string): Compiled | null {
  if (compiled.has(glob)) return compiled.get(glob)!;
  // A glob with a slash (outside brackets) is relative to the file's folder;
  // one without matches at any depth below it.
  const first = scan(glob);
  let hasSlash = false;
  for (let j = 0; j < glob.length; j++) {
    if (first.esc[j]) continue;
    if (glob[j] === "[" && first.bracket[j] > 0) j = first.bracket[j];
    else if (glob[j] === "/") {
      hasSlash = true;
      break;
    }
  }
  const g = !hasSlash ? `**/${glob}` : glob[0] === "/" ? glob.slice(1) : glob;
  const work = { n: 2 * glob.length };
  const alts = expand(scan(g), 0, g.length, 0, work);
  let result: Compiled | null = null;
  if (alts) {
    let weight = 0;
    for (const a of alts) for (const t of a) weight += tokWeight(t);
    result = { alts, weight, work: work.n };
  }
  if (compiled.size >= COMPILED_MAX) compiled.clear();
  compiled.set(glob, result);
  return result;
}

/** Tests: what a glob compiles to — its alternatives, match weight and compile work — or null. */
export function globCost(glob: string): { alts: number; weight: number; work: number } | null {
  const c = compileGlob(glob);
  return c ? { alts: c.alts.length, weight: c.weight, work: c.work } : null;
}

/** Does `glob` (a section of an `.editorconfig`) match `rel`, a path relative to that file's folder? */
export function matchGlob(glob: string, rel: string): boolean {
  if (rel.length > PATH_MAX) return false;
  const c = compileGlob(glob);
  if (!c) return false;
  return c.alts.some((toks) => matchTokens(toks, rel));
}

// ---- resolving --------------------------------------------------------------

/** The folders to look in for `path` (relative to the project), nearest first, the root ("") last. */
export function candidateDirs(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  parts.pop(); // the file itself
  const dirs: string[] = [];
  for (let n = parts.length; n >= 0 && dirs.length < DEPTH_MAX; n--) dirs.push(parts.slice(0, n).join("/"));
  if (dirs[dirs.length - 1] !== "") dirs[dirs.length - 1] = ""; // past the cap, the root is still read
  return dirs;
}

export const configPath = (dir: string) => (dir ? `${dir}/.editorconfig` : ".editorconfig");

/** What a resolution came to: the properties, or why there are none. */
export interface Resolved {
  props: Record<string, string>;
  /** Null, or why the file has no configuration: a file of too many sections, or the budget spent. */
  over: null | "sections" | "budget";
  /** The work it did (for tests and the caller's note). */
  work: number;
}

/**
 * The properties for `path` (relative to the project) from the files found,
 * **outermost first** (each with its folder, relative to the project): later
 * files and later sections win; `unset` removes a property; and the spec's
 * indent rules fill `indent_size` and `tab_width` from each other.
 *
 * **One budget for all of it** (`budget`, review of PR #59): every section's
 * compile and match is charged before it runs, and the first that would pass
 * the budget ends the resolution with no configuration — never a partial one
 * (later sections win, so a cut-off answer would be a wrong one).
 */
export function resolveProps(
  files: { dir: string; file: ConfigFile }[], path: string, budget = MATCH_BUDGET,
): Resolved {
  const props: Record<string, string> = {};
  let work = 0;
  const none = (over: "sections" | "budget"): Resolved => ({ props: {}, over, work });
  for (const { dir, file } of files) {
    if (file.tooMany) return none("sections");
    const prefix = dir ? `${dir}/` : "";
    if (prefix && !path.startsWith(prefix)) continue;
    const rel = path.slice(prefix.length);
    if (rel.length > PATH_MAX) continue;
    for (const s of file.sections) {
      // A compile is bounded by the glob's caps; it is charged as it is done.
      const c = compileGlob(s.glob);
      work += c ? c.work : 2 * s.glob.length;
      if (work > budget) return none("budget");
      if (!c) continue;
      const cost = (c.weight + c.alts.length) * (rel.length + 1);
      if (work + cost > budget) return none("budget");
      work += cost;
      if (!c.alts.some((toks) => matchTokens(toks, rel))) continue;
      for (const [k, v] of Object.entries(s.props)) {
        if (v === "unset") delete props[k];
        else props[k] = v;
      }
    }
  }
  if (props.indent_style === "tab" && props.indent_size === undefined) props.indent_size = "tab";
  if (props.indent_size !== undefined && props.indent_size !== "tab" && props.tab_width === undefined) {
    props.tab_width = props.indent_size;
  }
  if (props.indent_size === "tab" && props.tab_width !== undefined) props.indent_size = props.tab_width;
  return { props, over: null, work };
}

/** What the editor takes from the properties: a model's indentation, and a new file's line ending. */
export interface Override {
  insertSpaces?: boolean;
  tabSize?: number;
  indentSize?: number;
  /** For a file with no line break yet: the ending its new lines get. */
  eol?: "LF" | "CRLF";
}

const size = (v: string | undefined): number | undefined => {
  if (v === undefined || !/^\d{1,3}$/.test(v)) return undefined;
  const n = parseInt(v, 10);
  return n >= 1 && n <= 32 ? n : undefined;
};

/** The override the properties make, or null when they say nothing the editor uses. */
export function overrideOf(props: Record<string, string>): Override | null {
  const o: Override = {};
  if (props.indent_style === "space") o.insertSpaces = true;
  else if (props.indent_style === "tab") o.insertSpaces = false;
  const tab = size(props.tab_width);
  if (tab !== undefined) o.tabSize = tab;
  const indent = props.indent_size === "tab" ? tab : size(props.indent_size);
  if (indent !== undefined) o.indentSize = indent;
  // `cr` has no Monaco model to match it (one line ending per model, LF or
  // CRLF): it is ignored, and the file's own endings stand.
  if (props.end_of_line === "lf") o.eol = "LF";
  else if (props.end_of_line === "crlf") o.eol = "CRLF";
  return Object.keys(o).length ? o : null;
}
