import { describe, expect, it } from "vitest";
import theme from "../theme.css?raw";
import mobile from "./mobile.css?raw";
import page from "../../mobile.html?raw";

/** The custom properties declared on a stylesheet's `:root`, comments stripped. */
function rootTokens(css: string): Map<string, string> {
  const block = css.replace(/\/\*[\s\S]*?\*\//g, "").match(/:root\s*\{([^}]*)\}/);
  if (!block) throw new Error("no :root block");
  const out = new Map<string, string>();
  for (const m of block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out.set(m[1], m[2].trim());
  return out;
}

describe("the phone shell (PH-9)", () => {
  it("copies Graphite's tokens from theme.css exactly", () => {
    const hud = rootTokens(theme);
    const phone = rootTokens(mobile);
    expect(phone.size).toBeGreaterThan(5);
    for (const [name, value] of phone) expect([name, hud.get(name)]).toEqual([name, value]);
  });

  it("uses no url(), which would point outside /m/", () => {
    expect(mobile.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/url\s*\(/i);
  });

  it("says it is not paired, with no inline script or style", () => {
    expect(page).toContain("Jarvis · phone, not paired yet");
    expect(page).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/i);
    expect(page).not.toMatch(/<style|\sstyle=/i);
    expect(page).toContain('href="/m/manifest.webmanifest"');
  });
});
