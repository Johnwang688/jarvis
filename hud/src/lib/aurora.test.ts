import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import { contrast, hueSat } from "./editorTheme";

// The owner inbox row's RGB wave (owner, 2026-10-25; replaced by a rainbow wave
// 2026-11-14). An owner-approved exception to the Graphite look's one sparing
// accent. These checks recompute, from theme.css itself, what the exception
// promises: the whole hue wheel covered (amber and red included, this row
// only), always moving (calm too), the row's text AA over every blend, never
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

const BAND = ["--aurora-red", "--aurora-amber", "--aurora-yellow", "--aurora-green", "--aurora-cyan", "--aurora-blue", "--aurora-magenta"] as const;
const ALL = BAND;

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
  it("are declared on :root as colours, with the cycle duration", () => {
    for (const t of ALL) expect(() => token(t), t).not.toThrow();
    expect(decl.get("--aurora-drift")).toMatch(/^\d+(\.\d+)?s$/);
    const drift = parseFloat(decl.get("--aurora-drift")!);
    expect(drift).toBeGreaterThanOrEqual(3);   // a keyboard wave, not a strobe
    expect(drift).toBeLessThanOrEqual(12);     // and not a crawl
  });

  it("the band is a band, not a fill: at most 0.5 alpha", () => {
    for (const t of BAND) expect(token(t)[3], t).toBeLessThanOrEqual(0.5);
  });

  it("is saturated and covers the whole wheel, amber and red included (owner 2026-11-14)", () => {
    const hues: number[] = [];
    for (const t of BAND) {
      const { h, s } = hueSat(opaque(token(t)));
      expect(s, t).toBeGreaterThanOrEqual(0.8);
      hues.push(h);
    }
    // Every third of the wheel holds a stop, so no hue family is skipped.
    for (let from = 0; from < 360; from += 120) {
      expect(hues.some((h) => h >= from && h < from + 120), `no stop in ${from}-${from + 120}`).toBe(true);
    }
    expect(hues.some((h) => h <= 20 || h >= 340), "red").toBe(true);
    expect(hues.some((h) => h >= 25 && h <= 65), "amber/yellow").toBe(true);
    // No gap between neighbouring stops wider than 90 degrees.
    const sorted = [...hues].sort((a, b) => a - b);
    for (let i = 0; i < sorted.length; i++) {
      const next = i + 1 < sorted.length ? sorted[i + 1] : sorted[0] + 360;
      expect(next - sorted[i], "hue gap").toBeLessThanOrEqual(90);
    }
  });

  it("the gradient uses every stop, in two identical periods that both start and end on red", () => {
    const body = /button\.tree-row\.ownerinbox\.aurora::before\s*\{([^}]*)\}/.exec(text)![1];
    const grad = /linear-gradient\(([\s\S]*?)\);/.exec(body)![1];
    const stops = [...grad.matchAll(/var\((--aurora-[a-z]+)\)\s*([\d.]+)%/g)].map((m) => [m[1], Number(m[2])] as const);
    for (const t of BAND) expect(stops.some(([n]) => n === t), t).toBe(true);
    expect(stops[0][0]).toBe("--aurora-red");
    expect(stops[stops.length - 1][0]).toBe("--aurora-red");
    const half = stops.findIndex(([, pos]) => Math.abs(pos - 50) < 0.01);
    expect(half, "a stop at 50%").toBeGreaterThan(0);
    const first = stops.slice(0, half).map(([n]) => n), second = stops.slice(half, -1).map(([n]) => n);
    expect(second).toEqual(first);
    expect(body).toMatch(/width:\s*200%/);
    // Every stop is opaque in the sense of never fully transparent: no on/off.
    expect(grad).not.toMatch(/transparent/);
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

  it("animates only transform: a pure sweep, no brightness pulse, no layout", () => {
    for (const name of ["aurora-drift"]) {
      const kf = new RegExp(`@keyframes ${name}\\s*\\{([\\s\\S]*?)\\n\\}`).exec(text);
      expect(kf, name).not.toBeNull();
      const props = [...kf![1].matchAll(/([a-z-]+)\s*:/g)].map((m) => m[1]);
      expect(props.length, name).toBeGreaterThan(0);
      // The wave is a pure translation: no opacity (no brightness pulse, no on/off).
      for (const p of props) expect(["transform"], `${name}: ${p}`).toContain(p);
    }
  });

  it("the moving layer is absolutely placed and clipped by the row", () => {
    const row = rules(/^\s*button\.tree-row\.ownerinbox\.aurora\s*$/).join(";");
    expect(row).toMatch(/position:\s*relative/);
    expect(row).toMatch(/overflow:\s*hidden/);
    for (const pseudo of ["::before"]) {
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
    expect(mine).not.toMatch(/::before\s*\{[^}]*(display:\s*none|background:\s*none)/);
  });

  it("always animates, the calm state included (owner 2026-11-14), with no sparks and no pulsing", () => {
    expect(text).not.toMatch(/data-aurora="calm"/);
    const band = rules(/^\s*button\.tree-row\.ownerinbox\.aurora::before\s*$/).join(";");
    expect(band).toMatch(/animation:\s*aurora-drift\s+var\(--aurora-drift\)\s+linear\s+infinite/);
    // Only the pending state touches the band's own rules, and only to dim it.
    const states = [...text.matchAll(/button\.tree-row\.ownerinbox\.aurora\[data-aurora="([a-z]+)"\]::before\s*\{([^}]*)\}/g)];
    for (const [, name, body] of states) {
      expect(name).toBe("pending");
      expect(body).not.toMatch(/animation/);
    }
    // No ::after layer, no twinkle: a flash is not a wave.
    expect(text).not.toMatch(/ownerinbox\.aurora(?:\.sel)?::after/);
    expect(text).not.toMatch(/aurora-twinkle/);
    expect(text).not.toMatch(/--aurora-spark/);
  });

  it("leaves the selection bar and the focus ring above the layers it paints", () => {
    const sel = rules(/button\.tree-row\.ownerinbox\.aurora\.sel::before/).join(";");
    expect(sel).toMatch(/left:\s*2px/);
    const focus = rules(/^\s*button\.tree-row\.ownerinbox\.aurora:focus-visible\s*$/).join(";");
    expect(focus).toMatch(/outline:\s*1px solid var\(--accent\)/);
    expect(focus).toMatch(/outline-offset:\s*-1px/);
  });

  it("pauses under the approval card (everything outside the veil is inert)", () => {
    const paused = rules(/\[inert\] button\.tree-row\.ownerinbox\.aurora\[data-aurora\]::before/).join(";");
    expect(paused).toMatch(/animation-play-state:\s*paused/);
  });
});
