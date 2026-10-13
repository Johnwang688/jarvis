// Math is parsed as Markdown tokens, never by rewriting HTML or code spans.
// Only a per-render nonce placeholder crosses the unchanged prose sanitizer.
import MarkdownIt from "markdown-it";
import katex from "katex";
import DOMPurify from "dompurify";

export const MAX_FORMULA = 8192;
export const MAX_MATH_SOURCE = 32768;
export const MAX_FORMULAS = 64;
type Formula = { source: string; display: boolean; raw: string };
export type MathRender = { nonce: string; formulas: Map<string, Formula>; used: number };
export function mathRender(): MathRender {
  const random = crypto.getRandomValues(new Uint32Array(4));
  return { nonce: Array.from(random, (n) => n.toString(16).padStart(8, "0")).join(""),
    formulas: new Map(), used: 0 };
}
function escaped(src: string, pos: number): boolean {
  let slashes = 0;
  while (pos > 0 && src[--pos] === "\\") slashes++;
  return slashes % 2 === 1;
}
function closing(src: string, from: number, end: number, close: string, dollar: boolean): number {
  const limit = Math.min(end, from + MAX_FORMULA + close.length);
  const bounded = src.slice(from, limit);
  let next = 0, found: number;
  while ((found = bounded.indexOf(close, next)) >= 0) {
    const at = from + found;
    if (!escaped(src, at) && (!dollar || (
      !/\s/.test(src[at - 1] || "") && !/\d/.test(src[at + 1] || "")
      && src[at + 1] !== "$" && src[at - 1] !== "$"))) return at;
    next = found + close.length;
  }
  return -1;
}
// Index each inline source once: a flood of unpairable "$1" delimiters must
// never buy a fresh 8 KiB lookahead for each dollar. StateInline is ephemeral.
const closers = new WeakMap<object, Map<string, number[]>>();
function inlineClose(state: { src: string; posMax: number }, from: number, close: string): number {
  let index = closers.get(state);
  if (!index) {
    index = new Map([["$", []], ["$$", []], ["\\)", []], ["\\]", []]]);
    const src = state.src;
    for (let i = 0; i < src.length; i++) {
      if (src[i] === "$" && !escaped(src, i)) {
        if (src[i + 1] === "$") index.get("$$")!.push(i);
        if (!/\s/.test(src[i - 1] || "") && !/\d/.test(src[i + 1] || "")
            && src[i - 1] !== "$" && src[i + 1] !== "$") index.get("$")!.push(i);
      } else if (src[i] === "\\" && (src[i + 1] === ")" || src[i + 1] === "]")
                 && !escaped(src, i)) index.get(src.slice(i, i + 2))!.push(i);
    }
    closers.set(state, index);
  }
  const positions = index.get(close)!;
  let low = 0, high = positions.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (positions[mid] < from) low = mid + 1;
    else high = mid;
  }
  const at = positions[low] ?? -1;
  return at >= 0 && at + close.length <= Math.min(state.posMax, from + MAX_FORMULA + close.length) ? at : -1;
}
export function installMath(md: MarkdownIt) {
  md.inline.ruler.before("escape", "math", (state, silent) => {
    const pos = state.pos, src = state.src;
    const open = src.startsWith("$$", pos) ? "$$" : src.startsWith("\\(", pos) ? "\\("
      : src.startsWith("\\[", pos) ? "\\[" : src[pos] === "$" ? "$" : null;
    if (!open) return false;
    const close = open === "\\(" ? "\\)" : open === "\\[" ? "\\]" : open;
    const display = open === "$$" || open === "\\[";
    const from = pos + open.length;
    if (open === "$" && /\s/.test(src[from] || "")) return false;
    const end = inlineClose(state, from, close);
    const source = end >= 0 ? src.slice(from, end) : "";
    if (end < 0 || !source.trim() || (!display && source.includes("\n"))) {
      // Markdown's escape rule otherwise eats an incomplete \(... or \[...
      // Consume a double dollar too, so its second $ never becomes an opener.
      if (open.length === 2) {
        if (!silent) state.pending += open;
        state.pos += open.length;
        return true;
      }
      return false;
    }
    if (!silent) {
      const token = state.push("math", "", 0);
      token.meta = { source, display, raw: src.slice(pos, end + close.length) } satisfies Formula;
    }
    state.pos = end + close.length;
    return true;
  });
  // A display block can span blank lines; its closing delimiter ends the block.
  md.block.ruler.before("fence", "math_block", (state, start, end, silent) => {
    if (state.sCount[start] - state.blkIndent >= 4) return false;
    const begin = state.bMarks[start] + state.tShift[start];
    const line = state.src.slice(begin, state.eMarks[start]);
    const open = line.startsWith("$$") ? "$$" : line.startsWith("\\[") ? "\\[" : null;
    if (!open) return false;
    const close = open === "$$" ? "$$" : "\\]";
    const at = closing(state.src, begin + open.length, state.eMarks[end - 1], close, false);
    if (at < 0) return false;
    let last = start;
    while (last < end && state.eMarks[last] < at + close.length) last++;
    if (last >= end || state.src.slice(at + close.length, state.eMarks[last]).trim()) return false;
    const source = state.src.slice(begin + open.length, at);
    if (!source.trim()) return false;
    if (silent) return true;
    const token = state.push("math", "", 0);
    token.block = true;
    token.map = [start, last + 1];
    token.meta = { source, display: true, raw: state.src.slice(begin, at + close.length) } satisfies Formula;
    state.line = last + 1;
    return true;
  }, { alt: ["paragraph", "reference", "blockquote", "list"] });
  md.renderer.rules.math = (tokens, idx, _options, env) => {
    const formula = tokens[idx].meta as Formula, render = env as MathRender;
    if (formula.source.length > MAX_FORMULA || render.formulas.size >= MAX_FORMULAS
        || render.used + formula.source.length > MAX_MATH_SOURCE)
      return md.utils.escapeHtml(formula.raw);
    const key = "jarvis-math-" + render.nonce + "-" + render.formulas.size;
    render.formulas.set(key, formula);
    render.used += formula.source.length;
    return "<code>" + key + "</code>";
  };
}

// A separate allowlist, only for KaTeX-generated output. No source HTML, links,
// images, handlers, ids or data attributes can enter either sanitizer.
const MATH_TAGS = [
  "span", "svg", "path", "line", "math", "semantics", "annotation", "mrow",
  "mi", "mo", "mn", "mtext", "mspace", "mstyle", "mfrac", "msqrt", "mroot",
  "msub", "msup", "msubsup", "munder", "mover", "munderover", "mtable", "mtr",
  "mtd", "menclose", "mpadded", "mmultiscripts", "mprescripts", "none",
];
const MATH_ATTRS = [
  "class", "style", "aria-hidden", "xmlns", "width", "height", "viewBox",
  "preserveAspectRatio", "d", "x1", "x2", "y1", "y2", "stroke", "stroke-width",
  "fill", "encoding", "display", "mathvariant", "mathsize", "mathcolor",
  "stretchy", "symmetric", "fence", "separator", "lspace", "rspace",
  "accent", "accentunder", "linethickness", "columnalign", "rowalign",
  "columnspacing", "rowspacing", "scriptlevel", "displaystyle", "notation",
  "voffset", "depth", "minsize", "maxsize", "columnlines", "rowlines",
];
// maxSize caps positive TeX lengths only. Reject generated negative lengths
// and positioning offsets that can escape the message; natural positive
// widths (including KaTeX's stretchy SVGs) still scroll normally.
function boundedGeometry(math: DocumentFragment): boolean {
  for (const element of math.querySelectorAll("[style]")) {
    for (const declaration of (element.getAttribute("style") || "").split(";")) {
      const colon = declaration.indexOf(":");
      if (colon < 0) continue;
      const property = declaration.slice(0, colon).trim();
      const value = declaration.slice(colon + 1);
      const positioned = /^(top|bottom|left|right|vertical-align|margin(?:-.+)?)$/.test(property);
      for (const match of value.matchAll(/([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|[+-]?(?:Infinity|NaN))em\b/gi)) {
        const size = Number(match[1]);
        if (!Number.isFinite(size) || size < -20 || (positioned && size > 20)) return false;
      }
    }
  }
  return true;
}
export function restoreMath(clean: DocumentFragment, render: MathRender) {
  for (const placeholder of Array.from(clean.querySelectorAll("code"))) {
    const formula = render.formulas.get(placeholder.textContent || "");
    if (!formula || placeholder.children.length) continue;
    try {
      const html = katex.renderToString(formula.source, {
        displayMode: formula.display, output: "htmlAndMathml", trust: false,
        strict: "error", throwOnError: true, maxExpand: 100, maxSize: 20,
        macros: {}, // \gdef never reaches another equation/message
      });
      const math = DOMPurify.sanitize(html, {
        ALLOWED_TAGS: MATH_TAGS, ALLOWED_ATTR: MATH_ATTRS, ALLOW_DATA_ATTR: false,
        RETURN_DOM_FRAGMENT: true,
      }) as unknown as DocumentFragment;
      if (!boundedGeometry(math)) throw new Error("math geometry exceeds bounds");
      const wrapper = document.createElement("span");
      wrapper.className = formula.display ? "math-display" : "math-inline";
      wrapper.appendChild(math);
      placeholder.replaceWith(wrapper);
    } catch {
      // Unsupported/incomplete/oversized math stays readable. Never interpolate
      // KaTeX's exception text (it can contain unescaped source).
      placeholder.replaceWith(document.createTextNode(formula.raw));
    }
  }
}
