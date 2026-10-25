import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import { contrast, hueSat, isWarm } from "./editorTheme";

// The owner inbox row's aurora (owner, 2026-10-25): an owner-approved
// exception to Graphite's one sparing accent. These checks recompute, from
// theme.css itself, what the exception promised: dim, low-saturation hues
// outside the amber/red band, the row's text AA over every blend, never
// brighter than the text, the amber count still amber on an opaque chip, and
// motion that reduced-motion and the approval veil both stop.

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
const opaque = (c: RGBA) => hex([c[0], c[1], c[2], 1]);

const BAND = ["--aurora-teal", "--aurora-green", "--aurora-violet", "--aurora-rose"] as const;
const ALL = [...BAND, "--aurora-spark"] as const;

/** Every background the row's text can sit on: the pane, hovered or
 * selected, under each band hue at full strength (where two stops meet the
 * blend lies between them, so the extremes are the stops themselves). */
function rowBackgrounds(): string[] {
  const pane = token("--bg-1");
  const states = [pane, over(token("--hover"), pane), over(token("--selected"), pane)];
  const out: string[] = [];
  for (const state of states) {
    out.push(hex(state));
    for (const hue of BAND) out.push(hex(over(token(hue), state)));
  }
  return out;
}

describe("the aurora tokens", () => {
  it("are declared on :root as colours, with the drift durations", () => {
    for (const t of ALL) expect(() => token(t), t).not.toThrow();
    expect(decl.get("--aurora-drift")).toMatch(/^\d+(\.\d+)?s$/);
    expect(decl.get("--aurora-drift-calm")).toMatch(/^\d+(\.\d+)?s$/);
    const drift = parseFloat(decl.get("--aurora-drift")!);
    expect(drift).toBeGreaterThanOrEqual(8);   // "slowly shifting": 8-12 s a pass
    expect(drift).toBeLessThanOrEqual(12);
    expect(parseFloat(decl.get("--aurora-drift-calm")!)).toBeGreaterThan(drift);
  });

  it("the band is dim: at most 0.24 alpha", () => {
    for (const t of BAND) expect(token(t)[3], t).toBeLessThanOrEqual(0.24);
  });

  it("the band is low saturation and never amber or red, so pending and error keep their meanings", () => {
    for (const t of BAND) {
      const colour = opaque(token(t));
      expect(hueSat(colour).s, t).toBeLessThanOrEqual(0.5);
      expect(isWarm(colour), `${t} ${colour} is in the amber/red band`).toBe(false);
    }
  });

  it("keeps the row's text AA over every blend, and the band never brighter than the text", () => {
    const ink = decl.get("--text")!;
    for (const bg of rowBackgrounds()) {
      expect(contrast(ink, bg), `--text on ${bg}`).toBeGreaterThanOrEqual(4.5);
      // Brighter than the text would flip the contrast's direction.
      expect(contrast(bg, "#000000"), bg).toBeLessThan(contrast(ink, "#000000"));
    }
  });

  it("leaves the amber count on an opaque --bg-1 chip, as readable as anywhere else", () => {
    expect(text).toMatch(/\.ownerinbox\.aurora \.badge\.inboxcount\s*\{\s*background:\s*var\(--bg-1\);/);
    expect(contrast(decl.get("--amber")!, decl.get("--bg-1")!)).toBeGreaterThanOrEqual(4.5);
  });

  it("colours nothing but the owner inbox row", () => {
    for (const t of ALL) {
      const uses = [...text.matchAll(new RegExp(`var\\(${t}\\)`, "g"))].map((m) => m.index!);
      expect(uses.length, t).toBeGreaterThan(0);
      for (const at of uses) {
        const rule = text.lastIndexOf("}", at);
        const selector = text.slice(rule + 1, text.indexOf("{", rule + 1)).trim();
        expect(selector, `${t} used outside the aurora`).toMatch(/ownerinbox\.aurora/);
      }
    }
  });
});

describe("the aurora's motion", () => {
  const rules = (selector: RegExp) =>
    [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter((m) => selector.test(m[1])).map((m) => m[2]);

  it("animates only transform and opacity (compositor-friendly, no layout)", () => {
    for (const name of ["aurora-drift", "aurora-twinkle"]) {
      const kf = new RegExp(`@keyframes ${name}\\s*\\{([\\s\\S]*?)\\n\\}`).exec(text);
      expect(kf, name).not.toBeNull();
      const props = [...kf![1].matchAll(/([a-z-]+)\s*:/g)].map((m) => m[1]);
      expect(props.length, name).toBeGreaterThan(0);
      for (const p of props) expect(["transform", "opacity"], `${name}: ${p}`).toContain(p);
    }
  });

  it("the moving layers are absolutely placed and clipped by the row", () => {
    const row = rules(/^\s*button\.tree-row\.ownerinbox\.aurora\s*$/).join(";");
    expect(row).toMatch(/position:\s*relative/);
    expect(row).toMatch(/overflow:\s*hidden/);
    for (const pseudo of ["::before", "::after"]) {
      const body = rules(new RegExp(`^\\s*button\\.tree-row\\.ownerinbox\\.aurora${pseudo}\\s*$`)).join(";");
      expect(body, pseudo).toMatch(/position:\s*absolute/);
      expect(body, pseudo).toMatch(/pointer-events:\s*none/);
    }
  });

  it("stops entirely under prefers-reduced-motion: reduce, leaving a static tint", () => {
    const blocks = [...text.matchAll(/@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/g)].map((m) => m[1]);
    const mine = blocks.find((b) => b.includes("ownerinbox.aurora"));
    expect(mine).toBeDefined();
    expect(mine).toMatch(/button\.tree-row\.ownerinbox\.aurora\[data-aurora\]::before\s*\{[^}]*animation:\s*none/);
    expect(mine).toMatch(/button\.tree-row\.ownerinbox\.aurora\[data-aurora\]::after\s*\{[^}]*animation:\s*none[^}]*display:\s*none/);
    expect(mine).not.toMatch(/::before\s*\{[^}]*(display:\s*none|background:\s*none)/);
  });

  it("pauses under the approval card (everything outside the veil is inert)", () => {
    const paused = rules(/\[inert\] button\.tree-row\.ownerinbox\.aurora\[data-aurora\]::before/).join(";");
    expect(paused).toMatch(/animation-play-state:\s*paused/);
    const sparks = rules(/\[inert\] button\.tree-row\.ownerinbox\.aurora\[data-aurora\]::after/).join(";");
    expect(sparks).toMatch(/animation-play-state:\s*paused/);
  });
});
