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
  it("copies the HUD's meaning and accent tokens from theme.css exactly", () => {
    // AG-1: the desk HUD moved to aurora glass with cool surfaces and text; the phone keeps its
    // Graphite surfaces until the later phone port (AG-5, owner D-6). What must never differ is
    // what the colours MEAN: the accent, amber, red and green, and their tints and lines.
    const hud = rootTokens(theme);
    const phone = rootTokens(mobile);
    expect(phone.size).toBeGreaterThan(5);
    const SURFACES = new Set([
      "--bg-0", "--bg-1", "--bg-2", "--bg-3", "--bg-inset", "--border", "--border-strong",
      "--text", "--text-2", "--text-muted", "--radius",
    ]);
    for (const [name, value] of phone) {
      if (SURFACES.has(name)) continue;
      expect([name, hud.get(name)]).toEqual([name, value]);
    }
    for (const name of ["--accent", "--amber", "--red", "--green"]) expect(phone.has(name), name).toBe(true);
  });

  it("uses no url(), which would point outside /m/", () => {
    expect(mobile.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/url\s*\(/i);
  });

  it("names itself, with no inline script or style", () => {
    expect(page).toContain("Jarvis · phone");
    expect(page).toContain('id="app"');
    expect(page).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/i);
    expect(page).not.toMatch(/<style|\sstyle=/i);
    expect(page).toContain('href="/m/manifest.webmanifest"');
  });
});
