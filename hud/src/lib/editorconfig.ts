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

/** Spec limits (0.17): longer names, keys and values are ignored. */
export const SECTION_MAX = 4096;
export const KEY_MAX = 1024;
export const VALUE_MAX = 4096;
/** A file bigger than this is not read as a configuration (it is not one). */
export const FILE_MAX = 64 * 1024;
/** Folders walked up from a file, at most (the stat route takes 100 paths). */
export const DEPTH_MAX = 32;
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
 * preamble other than `root`.
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
// never a backtracking regular expression. An `.editorconfig` is project
// content an agent can write, and `[*a*a*a*a*a*a*a*a*b]` turned into a regex
// takes minutes against a long file name, on the window that gates
// approvals. Here every glob costs at most tokens × path length per
// alternative, with the tokens, the path and the brace alternatives capped.

/** A glob longer than this (in tokens), or a path longer than this, matches nothing. */
export const GLOB_TOKENS_MAX = 512;
export const PATH_MAX = 1024;
/** Brace alternatives multiply (`{a,b}{c,d}` is four): past this, the glob matches nothing. */
export const ALTERNATIVES_MAX = 64;

type Tok =
  | { k: "lit"; c: string }
  | { k: "star" } // `*`: anything but a slash
  | { k: "globstar" } // `**`: anything
  | { k: "dirstar" } // `**/` at a folder's start: no folder, or any folders
  | { k: "any" } // `?`: one character, not a slash
  | { k: "class"; neg: boolean; test: (c: string) => boolean }
  | { k: "num"; lo: number; hi: number }; // `{lo..hi}`: a whole number in range

/** Where the `]` closing a bracket opened at `i` is, or -1 (then `[` is literal). */
function bracketEnd(g: string, i: number): number {
  let j = i + 1;
  if (g[j] === "!" || g[j] === "^") j++;
  if (g[j] === "]") j++; // a leading `]` is a member
  for (; j < g.length; j++) {
    if (g[j] === "\\") {
      j++;
      continue;
    }
    if (g[j] === "/") return -1; // a slash inside brackets makes them literal
    if (g[j] === "]") return j;
  }
  return -1;
}

/** Where the `}` closing a brace opened at `i` is (nesting and escapes followed), or -1. */
function braceEnd(g: string, i: number): number {
  let depth = 0;
  for (let j = i; j < g.length; j++) {
    const c = g[j];
    if (c === "\\") {
      j++;
      continue;
    }
    if (c === "[") {
      const e = bracketEnd(g, j);
      if (e > 0) j = e;
      continue;
    }
    if (c === "{") depth++;
    else if (c === "}" && --depth === 0) return j;
  }
  return -1;
}

/** The top-level commas of a brace's inside split it into alternatives. */
function splitTop(s: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let j = 0; j < s.length; j++) {
    const c = s[j];
    if (c === "\\") {
      j++;
      continue;
    }
    if (c === "[") {
      const e = bracketEnd(s, j);
      if (e > 0) j = e;
      continue;
    }
    if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === "," && depth === 0) {
      parts.push(s.slice(start, j));
      start = j + 1;
    }
  }
  parts.push(s.slice(start));
  return parts;
}

function classOf(inner: string): Tok {
  let s = inner;
  let neg = false;
  if (s[0] === "!" || s[0] === "^") {
    neg = true;
    s = s.slice(1);
  }
  const singles = new Set<string>();
  const spans: [string, string][] = [];
  const chars: string[] = [];
  for (let j = 0; j < s.length; j++) chars.push(s[j] === "\\" && j + 1 < s.length ? s[++j] : s[j]);
  for (let j = 0; j < chars.length; j++) {
    if (j + 2 < chars.length && chars[j + 1] === "-") {
      spans.push([chars[j], chars[j + 2]]);
      j += 2;
    } else {
      singles.add(chars[j]);
    }
  }
  return { k: "class", neg, test: (c) => singles.has(c) || spans.some(([a, b]) => c >= a && c <= b) };
}

/** Every alternative's tokens (braces expanded), or null: too many, too deep, too long. */
function expand(g: string, depth: number): Tok[][] | null {
  if (depth > BRACE_DEPTH_MAX) return null;
  let seqs: Tok[][] = [[]];
  const push = (t: Tok) => {
    for (const s of seqs) s.push(t);
  };
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === "\\") {
      push({ k: "lit", c: i + 1 < g.length ? g[++i] : "\\" });
    } else if (c === "*") {
      if (g[i + 1] === "*") {
        const atDir = i === 0 || g[i - 1] === "/";
        while (g[i + 1] === "*") i++;
        if (atDir && g[i + 1] === "/") {
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
      const e = bracketEnd(g, i);
      if (e < 0) push({ k: "lit", c: "[" });
      else {
        push(classOf(g.slice(i + 1, e)));
        i = e;
      }
    } else if (c === "{") {
      const e = braceEnd(g, i);
      if (e < 0) {
        push({ k: "lit", c: "{" });
        continue;
      }
      const inner = g.slice(i + 1, e);
      const range = /^([+-]?\d{1,9})\.\.([+-]?\d{1,9})$/.exec(inner);
      const alts = splitTop(inner);
      if (range) {
        const a = parseInt(range[1], 10);
        const b = parseInt(range[2], 10);
        push({ k: "num", lo: Math.min(a, b), hi: Math.max(a, b) });
      } else if (alts.length < 2) {
        for (const ch of g.slice(i, e + 1)) push({ k: "lit", c: ch }); // `{single}` is literal
      } else {
        const options: Tok[][] = [];
        for (const a of alts) {
          const sub = expand(a, depth + 1);
          if (!sub) return null;
          options.push(...sub);
          if (options.length > ALTERNATIVES_MAX) return null;
        }
        if (seqs.length * options.length > ALTERNATIVES_MAX) return null;
        seqs = seqs.flatMap((s) => options.map((o) => [...s, ...o]));
      }
      i = e;
    } else {
      push({ k: "lit", c });
    }
    if (seqs[0].length > GLOB_TOKENS_MAX) return null;
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

const compiled = new Map<string, Tok[][] | null>();
const COMPILED_MAX = 512;

/** A section's glob as token lists, relative to its file's folder; null if unusable. */
function compileGlob(glob: string): Tok[][] | null {
  if (compiled.has(glob)) return compiled.get(glob)!;
  let g = glob;
  // A glob with a slash (outside brackets) is relative to the file's folder;
  // one without matches at any depth below it.
  let hasSlash = false;
  for (let j = 0; j < g.length; j++) {
    if (g[j] === "\\") {
      j++;
      continue;
    }
    if (g[j] === "[") {
      const e = bracketEnd(g, j);
      if (e > 0) {
        j = e;
        continue;
      }
    }
    if (g[j] === "/") {
      hasSlash = true;
      break;
    }
  }
  if (!hasSlash) g = `**/${g}`;
  else if (g[0] === "/") g = g.slice(1);
  const result = expand(g, 0);
  if (compiled.size >= COMPILED_MAX) compiled.clear();
  compiled.set(glob, result);
  return result;
}

/** Does `glob` (a section of an `.editorconfig`) match `rel`, a path relative to that file's folder? */
export function matchGlob(glob: string, rel: string): boolean {
  if (rel.length > PATH_MAX) return false;
  const alts = compileGlob(glob);
  if (!alts) return false;
  return alts.some((toks) => matchTokens(toks, rel));
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

/**
 * The properties for `path` (relative to the project) from the files found,
 * **outermost first** (each with its folder, relative to the project): later
 * files and later sections win; `unset` removes a property; and the spec's
 * indent rules fill `indent_size` and `tab_width` from each other.
 */
export function resolveProps(files: { dir: string; file: ConfigFile }[], path: string): Record<string, string> {
  const props: Record<string, string> = {};
  for (const { dir, file } of files) {
    const prefix = dir ? `${dir}/` : "";
    if (prefix && !path.startsWith(prefix)) continue;
    const rel = path.slice(prefix.length);
    for (const s of file.sections) {
      if (!matchGlob(s.glob, rel)) continue;
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
  return props;
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
