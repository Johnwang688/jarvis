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
});
