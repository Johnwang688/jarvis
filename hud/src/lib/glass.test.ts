import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import indexHtml from "../../index.html?raw";
import { contrast } from "./editorTheme";

// Aurora glass (AG-1, plan §3 "legibility rule"): every claim the glass makes
// about legibility is recomputed here from theme.css itself, with alpha
// compositing, so a token that drifts too bright (or too clear) fails here and
// not in the owner's eyes.

const text = css.replace(/\/\*[\s\S]*?\*\//g, "");

function rootDeclarations(): Map<string, string> {
  const block = /:root\s*\{([\s\S]*?)\n\}/.exec(text);
  if (!block) throw new Error("theme.css has no :root block");
  return new Map([...block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}
const decl = rootDeclarations();

type RGBA = [number, number, number, number];
function rgba(value: string): RGBA {
  const v = value.trim().toLowerCase();
  const hex = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/.exec(v);
  if (hex) return [parseInt(hex[1], 16), parseInt(hex[2], 16), parseInt(hex[3], 16), 1];
  const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(v);
  if (!m) throw new Error(`not a colour: ${value}`);
  return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])];
}
const token = (name: string): RGBA => {
  const value = decl.get(name);
  if (!value) throw new Error(`${name} is not on :root`);
  return rgba(value);
};
/** `top` composited over an opaque `under`. */
function over(top: RGBA, under: RGBA): RGBA {
  const a = top[3];
  return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(1) as RGBA;
}
const hex = (c: RGBA) => "#" + c.slice(0, 3).map((n) => Math.round(n).toString(16).padStart(2, "0")).join("");
const lum = (c: RGBA) => {
  const [r, g, b] = c.slice(0, 3).map((n) => { const s = n / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const WASHES = ["--wash-1", "--wash-2", "--wash-3"] as const;
/** The brightest wash at full strength, then the base: the two ends of what a pane can sit over. */
const brightest = WASHES.map((w) => ({ w, c: token(w) })).sort((a, b) => lum(b.c) - lum(a.c))[0];
const UNDER: [string, RGBA][] = [[`brightest wash ${brightest.w}`, brightest.c], ...WASHES.map((w) => [w, token(w)] as [string, RGBA]), ["--base", token("--base")]];

/** The sheen's strongest white (its first stop), as a layer over the tint. */
const sheenPeak = (): RGBA => {
  const m = /rgba\(255,\s*255,\s*255,\s*([\d.]+)\)/.exec(decl.get("--glass-sheen") ?? "");
  if (!m) throw new Error("--glass-sheen has no white stop");
  return [255, 255, 255, Number(m[1])];
};

/** A pane as drawn: the tint over the wash, then the sheen at its peak. */
const pane = (under: RGBA) => over(sheenPeak(), over(token("--glass-pane"), under));

describe("the glass tokens", () => {
  it("are declared, with the wash and base as opaque hex", () => {
    for (const t of ["--base", "--wash-1", "--wash-2", "--wash-3", "--glass-pane", "--glass-solid", "--glass-sheen", "--rim", "--glass-shadow", "--blur"]) {
      expect(decl.has(t), t).toBe(true);
    }
    for (const t of ["--base", ...WASHES]) expect(decl.get(t), t).toMatch(/^#[0-9a-f]{6}$/);
    expect(decl.get("--blur")).toMatch(/^blur\(\d+px\)/);
  });

  it("keep the accent family and the meanings exactly, and the Inbox row's own aurora names", () => {
    expect(decl.get("--accent")).toBe("#6fc3df");
    expect(decl.get("--accent-hi")).toBe("#9ad6ea");
    expect(decl.get("--accent-soft")).toBe("rgba(111, 195, 223, 0.14)");
    expect(decl.get("--accent-line")).toBe("rgba(111, 195, 223, 0.45)");
    expect(decl.get("--amber")).toBe("#fbbf24");
    expect(decl.get("--amber-soft")).toBe("rgba(251, 191, 36, 0.10)");
    expect(decl.get("--amber-line")).toBe("rgba(251, 191, 36, 0.55)");
    expect(decl.get("--red")).toBe("#f87171");
    expect(decl.get("--green")).toBe("#6dd08f");
    for (const t of ["--aurora-teal", "--aurora-green", "--aurora-violet", "--aurora-rose", "--aurora-spark", "--aurora-drift"]) {
      expect(decl.has(t), t).toBe(true);
    }
    expect(decl.get("--radius-btn")).toBe("4px");
  });

  it("the tint is translucent (so the wash shows) and the dense-text tint is the more opaque", () => {
    expect(token("--glass-pane")[3]).toBeGreaterThan(0.3);
    expect(token("--glass-pane")[3]).toBeLessThan(0.8);
    expect(token("--glass-solid")[3]).toBeGreaterThan(token("--glass-pane")[3]);
  });

  it("the wash is dim and cool: no wash colour is brighter than a quarter of the text, or in the amber/red band", () => {
    for (const w of WASHES) {
      expect(lum(token(w)), w).toBeLessThan(0.1);
      const [r, , b] = token(w);
      expect(b, `${w} is warm`).toBeGreaterThanOrEqual(r);
    }
  });
});

describe("legibility over the brightest wash and over the base (plan §3)", () => {
  it("--text and --text-muted are at least 4.5:1 on a pane, over every wash and the base", () => {
    for (const [name, under] of UNDER) {
      const bg = hex(pane(under));
      expect(contrast(hex(token("--text")), bg), `--text on pane over ${name}`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(hex(token("--text-2")), bg), `--text-2 on pane over ${name}`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(hex(token("--text-muted")), bg), `--text-muted on pane over ${name}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("they hold on a raised control (--raise-hi on the pane) and on the selected row", () => {
    for (const [name, under] of UNDER) {
      for (const layer of ["--raise-hi", "--selected"]) {
        const bg = hex(over(token(layer), pane(under)));
        expect(contrast(hex(token("--text")), bg), `--text on ${layer} over ${name}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(hex(token("--text-muted")), bg), `--text-muted on ${layer} over ${name}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the dense-text tint and a floating layer clear it too", () => {
    for (const [name, under] of UNDER) {
      for (const tint of ["--glass-solid", "--glass-float"]) {
        const bg = hex(over(token(tint), under));
        expect(contrast(hex(token("--text")), bg), `--text on ${tint} over ${name}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(hex(token("--text-muted")), bg), `--text-muted on ${tint} over ${name}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the accent is at least 3:1 on a pane (focus, links, the selection bar)", () => {
    for (const [name, under] of UNDER) {
      expect(contrast(hex(token("--accent")), hex(pane(under))), `--accent on pane over ${name}`).toBeGreaterThanOrEqual(3);
    }
  });

  it("amber, red and green still read on a pane and on a floating layer (pending, error, done)", () => {
    for (const [name, under] of UNDER) {
      for (const bg of [hex(pane(under)), hex(over(token("--glass-float"), under))]) {
        for (const t of ["--amber", "--red", "--green"]) {
          expect(contrast(hex(token(t)), bg), `${t} over ${name}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it("the opaque solids (what a floater falls back to) are as legible", () => {
    for (const bgName of ["--bg-0", "--bg-1", "--bg-2", "--bg-3"]) {
      const bg = hex(token(bgName));
      expect(contrast(hex(token("--text")), bg), bgName).toBeGreaterThanOrEqual(4.5);
      expect(contrast(hex(token("--text-muted")), bg), bgName).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("the check is a real one: a wash brighter than the plan allows fails it", () => {
    const bright: RGBA = [60, 140, 200, 1];
    expect(contrast(hex(token("--text-muted")), hex(pane(bright)))).toBeLessThan(4.5);
  });
});

describe("where the glass is painted", () => {
  /** Every rule: [selectors, body]. */
  const rules = [...text.matchAll(/([^{}@]+)\{([^{}]*)\}/g)].map((m) => [m[1].trim(), m[2]] as const);
  const sel = (s: string) => s.split(",").map((x) => x.trim());

  it("no pane carries a backdrop-filter: real blur is only on floating layers", () => {
    const blurred = rules.filter(([, body]) => /backdrop-filter/.test(body)).flatMap(([s]) => sel(s));
    expect(blurred.length).toBeGreaterThan(10);
    for (const s of blurred) expect(s, s).not.toMatch(/^(\.pane|\.wpane|\.rail|\.panel|#sidebar|#right|#main|#titlebar|\.tabs|\.chatlog|\.termslot|body|#root|#aurora)\b/);
    for (const s of [".picker", ".auth", ".threadmenu", ".mpop", ".laymenu", ".explorermenu", ".termtoast", ".settings-page"]) {
      expect(blurred, s).toContain(s);
    }
  });

  it("every blurred layer has an opaque fallback where backdrop-filter is missing", () => {
    const m = /@supports not \(\(backdrop-filter: blur\(1px\)\) or \(-webkit-backdrop-filter: blur\(1px\)\)\)\s*\{([\s\S]*?\})\s*\}/.exec(text);
    expect(m, "an @supports not block").not.toBeNull();
    const fallback = /([^{}]+)\{\s*background:\s*var\(--bg-2\);\s*\}/.exec(m![1]);
    expect(fallback, "the fallback sets --bg-2").not.toBeNull();
    const blurred = rules.filter(([, body]) => /backdrop-filter/.test(body)).flatMap(([s]) => sel(s));
    for (const s of blurred) expect(sel(fallback![1]), s).toContain(s);
  });

  it("the aurora wash is one fixed, inert layer behind #root, and the page has it", () => {
    const body = rules.find(([s]) => s === "#aurora")?.[1] ?? "";
    expect(body).toMatch(/position:\s*fixed/);
    expect(body).toMatch(/pointer-events:\s*none/);
    expect(body).toMatch(/z-index:\s*-1/);
    for (const w of WASHES) expect(body).toContain(`var(${w})`);
    expect(body).toContain("var(--base)");
    expect(body).not.toMatch(/animation/);
    expect(indexHtml).toMatch(/<div id="aurora" aria-hidden="true"><\/div>\s*<div id="root">/);
  });

  it("panes, the title bar and the strips are the glass tint; dense text is the solid one", () => {
    const body = (s: string) => rules.find(([x]) => sel(x).includes(s))?.[1] ?? "";
    for (const s of [".pane", ".wpane", ".panel", ".rail"]) expect(body(s), s).toContain("var(--glass-pane)");
    expect(body(".pane")).toContain("var(--glass-sheen)");
    expect(body("#titlebar")).toContain("var(--glass-pane)");
    expect(body(".chatlog")).toContain("var(--glass-solid)");
    expect(body(".termslot")).toContain("var(--glass-solid)");
    // The 1px gap between split panes shows the wash, not a grey slab.
    expect(body("#workspace")).toMatch(/background:\s*transparent/);
  });

  it("xterm's own solid-black viewport is made transparent, so the terminal sits on the dense-text glass", () => {
    const hit = rules.find(([x]) => sel(x).includes(".termhost .xterm .xterm-viewport"));
    expect(hit?.[1] ?? "").toMatch(/background-color:\s*transparent\s*!important/);
  });
});
