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
// judged on the main thread of the window that gates approvals. So the guard
// is **a real clock, not a cost model** (the lead's decision on PR #59's
// second review: a model can always be out-counted by a shape nobody thought
// of). The parser, the compiler and the matcher all spend their work through
// one `Deadline`, which reads `performance.now()` every `CHECK_STEPS` steps
// and throws `OutOfTime` past `RESOLVE_MS` (5 ms); the caller then gives the
// file no configuration at all — never a partial one — and poisons the config
// file that spent the time (`state/editorconfig.ts`). The caps below stay, as
// limits on what a real config needs: per glob 64 alternatives, 512 tokens each
// and 4,096 in all, braces 16 deep; per file 128 sections; per path 1,024
// characters. A config over one of them configures nothing.

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

// ---- the clock --------------------------------------------------------------

/** One resolution's time: compile, match and parse together, at most. */
export const RESOLVE_MS = 5;
/** Steps of work between two looks at the clock (a step is a few nanoseconds). */
export const CHECK_STEPS = 256;

/** Raised from inside the parser, the compiler or the matcher when the time is up. */
export class OutOfTime extends Error {
  constructor() {
    super("out of time");
  }
}

export interface Deadline {
  now(): number;
  /** The clock reading past which `tick` throws. */
  until: number;
  /** Steps since the clock was last read. */
  steps: number;
  /** Every step spent through it (tests: "did no work"). */
  total: number;
}

/** A deadline `ms` from now (`Infinity`: none), read through `now` (tests pass a fake clock). */
export function deadline(ms: number = RESOLVE_MS, now: () => number = () => performance.now()): Deadline {
  return { now, until: ms === Infinity ? Infinity : now() + ms, steps: 0, total: 0 };
}

/** Spend `n` steps of work; every `CHECK_STEPS`, look at the clock. */
function tick(d: Deadline, n: number) {
  d.total += n;
  d.steps += n;
  if (d.steps < CHECK_STEPS) return;
  d.steps = 0;
  if (d.until !== Infinity && d.now() > d.until) throw new OutOfTime();
}

const unbounded = () => deadline(Infinity);

// ---- parsing ----------------------------------------------------------------

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
  /**
   * Each section's glob as compiled, kept with the file (the caller keeps the
   * file by its etag), filled as resolutions need them: a cache that holds
   * exactly one file's globs, so it can never thrash (review of PR #59).
   */
  compiled?: (Compiled | null | undefined)[];
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
 * so (`tooMany`). Its work goes through `d`.
 */
export function parseEditorConfig(text: string, d: Deadline = unbounded()): ConfigFile {
  const out: ConfigFile = { root: false, sections: [] };
  if (typeof text !== "string") return out;
  let current: Section | null = null;
  const body = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  tick(d, body.length >> 4);
  for (const rawLine of body.split(/\r\n|\r|\n/)) {
    tick(d, 1 + (rawLine.length >> 4));
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
// takes minutes against a long file name. The glob is read in one linear pass
// (each bracket's and brace's end found once, `scan`), and every loop spends
// through the deadline.

/** A brace alternative longer than this (in tokens), or a path longer than this, matches nothing. */
export const GLOB_TOKENS_MAX = 512;
/** All of a glob's alternatives together, in tokens: past this, the glob matches nothing. */
export const GLOB_TOTAL_MAX = 4096;
export const PATH_MAX = 1024;
/** Brace alternatives multiply (`{a,b}{c,d}` is four): past this, the glob matches nothing. */
export const ALTERNATIVES_MAX = 64;

interface ClassTok {
  k: "class";
  neg: boolean;
  singles: Set<string>;
  spans: [string, string][];
  /** Each character's answer, once worked out: a class of many spans costs its spans once per character. */
  memo: Map<string, boolean>;
}

type Tok =
  | { k: "lit"; c: string }
  | { k: "star" } // `*`: anything but a slash
  | { k: "globstar" } // `**`: anything
  | { k: "dirstar" } // `**/` at a folder's start: no folder, or any folders
  | { k: "any" } // `?`: one character, not a slash
  | ClassTok
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

function scan(g: string, d: Deadline): Scan {
  const n = g.length;
  tick(d, 4 * n);
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

function classOf(g: string, start: number, end: number, esc: Uint8Array, d: Deadline): ClassTok {
  tick(d, end - start + 1);
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
  return { k: "class", neg, singles, spans, memo: new Map() };
}

/** What a glob compiled to: its alternatives, and their match weight per path position. */
export interface Compiled {
  alts: Tok[][];
  /** Σ over alternatives of tokens (a class counts its spans, a number its digits). */
  weight: number;
}

const tokWeight = (t: Tok) => (t.k === "num" ? 13 : t.k === "class" ? 1 + t.spans.length : 1);

/**
 * Every alternative's tokens for `g[start, end)` (braces expanded), or null:
 * too many alternatives, too many tokens, too deep.
 */
function expand(sc: Scan, start: number, end: number, depth: number, d: Deadline): Tok[][] | null {
  if (depth > BRACE_DEPTH_MAX) return null;
  const { g, esc } = sc;
  let seqs: Tok[][] = [[]];
  let longest = 0;
  let total = 0;
  const push = (t: Tok) => {
    for (const s of seqs) s.push(t);
    longest += 1;
    total += seqs.length;
    tick(d, seqs.length);
  };
  for (let i = start; i < end; i++) {
    tick(d, 1);
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
        push(classOf(g, i + 1, e, esc, d));
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
          tick(d, 1);
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
            const sub = expand(sc, cuts[a] + 1, cuts[a + 1], depth + 1, d);
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
          tick(d, nextTotal);
          seqs = seqs.flatMap((s) => options.map((o) => [...s, ...o]));
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

/** One alternative against the path: a table, last token first, one row at a time. */
function matchTokens(toks: Tok[], path: string, d: Deadline): boolean {
  const P = path.length;
  let next = new Uint8Array(P + 1);
  next[P] = 1; // no tokens left matches the end of the path
  for (let t = toks.length - 1; t >= 0; t--) {
    const tok = toks[t];
    tick(d, P + 1);
    const row = new Uint8Array(P + 1);
    if (tok.k === "dirstar") {
      // nothing, or anything ending at a slash
      let after = 0;
      for (let i = P; i >= 0; i--) {
        if (i < P && path[i] === "/" && next[i + 1]) after = 1;
        row[i] = next[i] || after;
      }
    } else if (tok.k === "num") {
      // A whole number from `i`, counted digit by digit (no slicing, no parsing).
      tick(d, 12 * (P + 1));
      for (let i = P; i >= 0; i--) {
        let j = i;
        let sign = 1;
        if (j < P && (path[j] === "+" || path[j] === "-")) {
          if (path[j] === "-") sign = -1;
          j++;
        }
        const first = j;
        let n = 0;
        let ok = 0;
        while (j < P && j - first < 12) {
          const digit = path.charCodeAt(j) - 48;
          if (digit < 0 || digit > 9) break;
          n = n * 10 + digit;
          j++;
          const v = sign * n;
          if (v >= tok.lo && v <= tok.hi && next[j]) {
            ok = 1;
            break;
          }
        }
        row[i] = ok;
      }
    } else if (tok.k === "class") {
      for (let i = P; i >= 0; i--) {
        if (i >= P || path[i] === "/") continue;
        const ch = path[i];
        let hit = tok.memo.get(ch);
        if (hit === undefined) {
          tick(d, 1 + tok.spans.length);
          hit = tok.singles.has(ch) || tok.spans.some(([a, b]) => ch >= a && ch <= b);
          tok.memo.set(ch, hit);
        }
        row[i] = hit !== tok.neg ? next[i + 1] : 0;
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
          case "star":
            row[i] = next[i] || (i < P && ch !== "/" ? row[i + 1] : 0);
            break;
          case "globstar":
            row[i] = next[i] || (i < P ? row[i + 1] : 0);
            break;
        }
      }
    }
    next = row;
  }
  return next[0] === 1;
}

/** A section's glob, relative to its file's folder; null if over a cap. Its work goes through `d`. */
export function compileGlob(glob: string, d: Deadline = unbounded()): Compiled | null {
  // A glob with a slash (outside brackets) is relative to the file's folder;
  // one without matches at any depth below it.
  const first = scan(glob, d);
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
  const alts = expand(scan(g, d), 0, g.length, 0, d);
  if (!alts) return null;
  let weight = 0;
  for (const a of alts) for (const t of a) weight += tokWeight(t);
  return { alts, weight };
}

/** Tests: what a glob compiles to — its alternatives and match weight — or null. */
export function globCost(glob: string): { alts: number; weight: number } | null {
  const c = compileGlob(glob);
  return c ? { alts: c.alts.length, weight: c.weight } : null;
}

/** Does `glob` (a section of an `.editorconfig`) match `rel`, a path relative to that file's folder? */
export function matchGlob(glob: string, rel: string, d: Deadline = unbounded()): boolean {
  if (rel.length > PATH_MAX) return false;
  const c = compileGlob(glob, d);
  if (!c) return false;
  return c.alts.some((toks) => matchTokens(toks, rel, d));
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
  /**
   * Null, or why the file has no configuration: a config file of too many
   * sections, a glob over a cap, the time spent, or a config file poisoned
   * by an earlier resolution (`state/editorconfig.ts`).
   */
  over: null | "sections" | "cap" | "time" | "poisoned";
  /** When `over`: which of the files (by index) is to blame — the one over a cap, or the one that spent the most time. */
  culprit: number | null;
  /** The steps of work it did (tests: "did no work"). */
  steps: number;
}

/**
 * The properties for `path` (relative to the project) from the files found,
 * **outermost first** (each with its folder, relative to the project): later
 * files and later sections win; `unset` removes a property; and the spec's
 * indent rules fill `indent_size` and `tab_width` from each other.
 *
 * Every compile and match spends through `d` (`RESOLVE_MS` by default); out of
 * time, or with a file over a cap, the file has **no configuration at all —
 * never a partial one** (later sections win, so a cut-off answer would be a
 * wrong one), and `culprit` names the file to blame. A section's compiled glob
 * is kept on its file (`ConfigFile.compiled`).
 */
export function resolveProps(
  files: { dir: string; file: ConfigFile }[], path: string, d: Deadline = deadline(),
): Resolved {
  const props: Record<string, string> = {};
  const start = d.total;
  const spent = files.map(() => 0);
  const none = (over: Exclude<Resolved["over"], null>, culprit: number): Resolved =>
    ({ props: {}, over, culprit, steps: d.total - start });
  let f = 0;
  let mark = d.now();
  try {
    for (; f < files.length; f++) {
      const { dir, file } = files[f];
      if (file.tooMany) return none("sections", f);
      const prefix = dir ? `${dir}/` : "";
      if (!prefix || path.startsWith(prefix)) {
        const rel = path.slice(prefix.length);
        if (rel.length <= PATH_MAX) {
          const compiled = (file.compiled ??= []);
          for (let i = 0; i < file.sections.length; i++) {
            const s = file.sections[i];
            let c = compiled[i];
            if (c === undefined) {
              c = compileGlob(s.glob, d);
              compiled[i] = c;
            }
            if (!c) return none("cap", f);
            if (!c.alts.some((toks) => matchTokens(toks, rel, d))) continue;
            for (const [k, v] of Object.entries(s.props)) {
              if (v === "unset") delete props[k];
              else props[k] = v;
            }
          }
        }
      }
      const now = d.now();
      spent[f] += now - mark;
      mark = now;
    }
  } catch (e) {
    if (!(e instanceof OutOfTime)) throw e;
    spent[Math.min(f, files.length - 1)] += d.now() - mark;
    let culprit = 0;
    for (let i = 1; i < spent.length; i++) if (spent[i] > spent[culprit]) culprit = i;
    return none("time", culprit);
  }
  if (props.indent_style === "tab" && props.indent_size === undefined) props.indent_size = "tab";
  if (props.indent_size !== undefined && props.indent_size !== "tab" && props.tab_width === undefined) {
    props.tab_width = props.indent_size;
  }
  if (props.indent_size === "tab" && props.tab_width !== undefined) props.indent_size = props.tab_width;
  return { props, over: null, culprit: null, steps: d.total - start };
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
