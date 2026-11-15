import { describe, expect, it } from "vitest";
import theme from "../theme.css?raw";
import mobile from "./mobile.css?raw";
import { AMBER, RED, DEFAULT_COOL } from "../lib/orbpalette";
import { readFileSync } from "node:fs";
import { join } from "node:path";
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
      "--text", "--text-2", "--text-muted", "--radius", "--mono",
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


describe("RD-0 phone tokens", () => {
  const html = mobile.match(/html \{([^}]+)\}/)![1];
  const token = (name: string) => new RegExp(`${name}:\\s*([^;]+);`).exec(html)?.[1].trim();
  it("matches the desk orb hues, including the fixed meaning colours", () => {
    expect(token("--m-hue-ok")).toBe(DEFAULT_COOL.base);
    expect(token("--m-hue-wait")).toBe(AMBER);
    expect(token("--m-hue-err")).toBe(RED);
  });
  it("declares all redesign tokens on html, leaving the shared theme alone", () => {
    const values = {
      "--m-orb": "1.875rem", "--m-orb-sm": "1.25rem", "--m-icon": "1.5rem", "--m-icon-sm": "1.125rem",
      "--m-top-h": "3.25rem", "--m-tab-h": "3.25rem", "--m-scrim": "rgba(3,8,12,0.62)",
      "--m-drawer-w": "min(19rem, 84vw)", "--m-seg": "rgba(255,255,255,.06)", "--m-seg-on": "rgba(111,195,223,.18)",
      "--m-bubble": "color-mix(in srgb, var(--accent) 16%, var(--bg-3))", "--m-r-bubble": "1.25rem",
      "--m-badge-ink": "#1a1203", "--m-dot": "0.625rem", "--m-ease": "cubic-bezier(.2,.8,.2,1)",
      "--m-dur-1": "120ms", "--m-dur-2": "220ms", "--m-dur-3": "320ms",
    };
    for (const [name, value] of Object.entries(values)) {
      expect(token(name), name).toBe(value);
      expect(theme).not.toContain(name);
    }
  });
  it("allows DOM-built SVG icons while forbidding image loads in talk.css", () => {
    const talk = readFileSync(join(__dirname, "talk.css"), "utf8");
    expect(talk).toContain("Code-built inline SVG icons are allowed");
    expect(talk).not.toContain("no svg");
    expect(talk.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/url\s*\(|@import/i);
  });
});
