import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import main from "../main.tsx?raw";
import { MONO_STACK, monoFamily } from "./fonts";

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

  it("the faces are bundled from @fontsource (latin only), never a CDN, for every weight theme.css uses", () => {
    for (const w of [400, 600, 700]) expect(main).toContain(`@fontsource/plus-jakarta-sans/latin-${w}.css`);
    for (const w of [400, 500, 600]) expect(main).toContain(`@fontsource/geist-mono/latin-${w}.css`);
    expect(main).not.toMatch(/https?:\/\//);
    expect(text).not.toMatch(/fonts\.(googleapis|gstatic)\.com|@import\s+url\(/);
    // Every weight the stylesheet asks for is one of the bundled ones.
    const used = new Set([...text.matchAll(/font(?:-weight)?:\s*(\d{3})\b/g)].map((m) => Number(m[1])));
    for (const w of used) expect([400, 500, 600, 700], `weight ${w}`).toContain(w);
  });

  it("word spacing is 0.05em on body and reset to normal in every rule that sets --mono (code keeps its grid)", () => {
    expect(text).toMatch(/body\s*\{[^}]*word-spacing:\s*0\.05em/);
    const rules = [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => [m[1].trim(), m[2]] as const);
    const mono = rules.filter(([sel, b]) => /var\(--mono\)/.test(b) && !/^:root/.test(sel));
    expect(mono.length).toBeGreaterThan(50);
    for (const [sel, b] of mono) expect(b, sel).toMatch(/word-spacing:\s*normal/);
    const reset = rules.find(([sel]) => /\.monaco-editor/.test(sel) && /\.xterm/.test(sel));
    expect(reset?.[1]).toMatch(/word-spacing:\s*normal/);
  });
});
