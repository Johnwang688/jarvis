import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import main from "../main.tsx?raw";
import { MONO_STACK, monoFamily } from "./fonts";
import { SCHEME } from "./editorTheme";

const UI_WEIGHTS = [400, 600, 700] as const;
const MONO_WEIGHTS = [400, 500, 600, 700] as const;

// Every desktop stylesheet (the phone's mobile.css has its own build and fonts).
const desktop = Object.fromEntries(
  Object.entries(import.meta.glob("../**/*.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>)
    .filter(([file]) => !file.includes("/mobile/"))
    .map(([file, c]) => [file, c.replace(/\/\*[\s\S]*?\*\//g, "")]),
);
for (const [file, c] of Object.entries(desktop)) if (!c.trim()) throw new Error(`${file} read as empty: vite.config.ts test.css.include must cover it`);
const rulesOf = (c: string) => [...c.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => [m[1].trim(), m[2]] as const);
const isMono = (body: string) => /var\(--mono\)|Geist Mono|\bmonospace\b/.test(body);
/** The weights a rule body names: `font-weight: N|bold`, or a `font:` shorthand's number. */
function weightsIn(body: string): number[] {
  const out: number[] = [];
  for (const m of body.matchAll(/font-weight:\s*(\w+)/g)) {
    const v = m[1];
    if (/^\d+$/.test(v)) out.push(Number(v)); else if (v === "bold" || v === "bolder") out.push(700); else if (v === "normal") out.push(400);
  }
  for (const m of body.matchAll(/(?:^|[;\s])font:\s*(?:italic\s+)?(\d{3})\b/g)) out.push(Number(m[1]));
  return out;
}

const text = css.replace(/\/\*[\s\S]*?\*\//g, "");

describe("the fonts (owner D-9)", () => {
  it("--mono in theme.css is the constant the code falls back to", () => {
    const m = /--mono:\s*([^;]+);/.exec(text)!;
    expect(m[1].trim()).toBe(MONO_STACK);
    expect(monoFamily()).toBe(MONO_STACK);   // no window: the constant
  });

  it("the UI font is Plus Jakarta Sans with Segoe UI behind it", () => {
    expect(text).toMatch(/body\s*\{[^}]*font-family:\s*"Plus Jakarta Sans",\s*"Segoe UI",\s*system-ui,\s*sans-serif/);
  });

  it("the faces are bundled from @fontsource (latin only), never a CDN", () => {
    for (const w of UI_WEIGHTS) expect(main).toContain(`@fontsource/plus-jakarta-sans/latin-${w}.css`);
    for (const w of MONO_WEIGHTS) expect(main).toContain(`@fontsource/geist-mono/latin-${w}.css`);
    expect(main).not.toMatch(/https?:\/\//);
    for (const [file, c] of Object.entries(desktop)) {
      expect(c, file).not.toMatch(/fonts\.(googleapis|gstatic)\.com|@import\s+url\(/);
    }
  });

  it("the bundled weights are exactly the ones in main.tsx (no face imported that nothing asks for)", () => {
    const bundled = (family: string) => [...main.matchAll(new RegExp(`@fontsource/${family}/latin-(\\d+)\\.css`, "g"))]
      .map((m) => Number(m[1]));
    expect(bundled("plus-jakarta-sans")).toEqual([...UI_WEIGHTS]);
    expect(bundled("geist-mono")).toEqual([...MONO_WEIGHTS]);
  });

  it("every weight a desktop stylesheet asks for exists in the family it asks it for", () => {
    const seen = { ui: new Set<number>(), mono: new Set<number>() };
    for (const [file, c] of Object.entries(desktop)) {
      for (const [sel, body] of rulesOf(c)) {
        const family = isMono(body) ? "mono" : "ui";
        for (const w of weightsIn(body)) {
          seen[family].add(w);
          expect(family === "mono" ? MONO_WEIGHTS : UI_WEIGHTS, `${file}: ${sel} asks ${w} of the ${family} font`).toContain(w);
        }
      }
    }
    expect(seen.ui.size).toBeGreaterThan(0);
    expect(seen.mono.size).toBeGreaterThan(0);
  });

  it("Monaco's token styles ask only for weights Geist Mono is bundled in (bold is 700)", () => {
    const styles = new Set(SCHEME.map((r) => r.style));
    const weights = [...styles].filter((s) => s.includes("bold")).map(() => 700);
    expect(weights.length).toBeGreaterThan(0);                // the scheme does use bold
    for (const w of weights) expect(MONO_WEIGHTS).toContain(w);
  });

  it("word spacing is 0.05em on body and reset to normal in every desktop rule that uses the mono font (code keeps its grid)", () => {
    expect(text).toMatch(/body\s*\{[^}]*word-spacing:\s*0\.05em/);
    let count = 0;
    for (const [file, c] of Object.entries(desktop)) {
      for (const [sel, body] of rulesOf(c)) {
        if (/^:root/.test(sel) || !isMono(body)) continue;
        count++;
        expect(body, `${file}: ${sel}`).toMatch(/word-spacing:\s*normal/);
      }
    }
    expect(count).toBeGreaterThan(50);
    const reset = rulesOf(css).find(([sel]) => /\.monaco-editor/.test(sel) && /\.xterm/.test(sel));
    expect(reset?.[1]).toMatch(/word-spacing:\s*normal/);
  });
});
