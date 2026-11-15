// UX-1 / UX-2 / UX-12 (owner, 2026-11-15): the phone's own aurora-glass skin, rounder and bigger,
// sized in rem, and none of it on the desk HUD. Phone only: hud/src/mobile/ and its own CSS.
import { describe, expect, it } from "vitest";
import theme from "../theme.css?raw";
import mobile from "./mobile.css?raw";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import page from "../../mobile.html?raw";

const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");
const MOBILE = strip(mobile);
// talk.css and write.css are read from disk: vitest does not process them as ?raw modules.
const TALK = strip(readFileSync(join(__dirname, "talk.css"), "utf8"));
const WRITE = strip(readFileSync(join(__dirname, "write.css"), "utf8"));
const ITEMS = strip(readFileSync(join(__dirname, "items.css"), "utf8"));
/** PX-4: every stylesheet of the phone, read from the folder (a new one cannot escape the blur rule). */
const PHONE_CSS: Record<string, string> = Object.fromEntries(readdirSync(__dirname).filter((f) => f.endsWith(".css"))
  .map((f) => [f, strip(readFileSync(join(__dirname, f), "utf8"))]));

/** Every rule that sets a backdrop filter (prefixed or not), as [selector list, file]. */
function blurRules(css: string): string[][] {
  const out: string[][] = [];
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (/(^|[\s;])(-webkit-)?backdrop-filter\s*:/.test(m[2])) out.push(m[1].split(",").map((x) => x.trim()));
  }
  return out;
}
/** Overlay-only blur: every selector must be one of the three approved surfaces. */
const strayBlur = (css: string) => blurRules(css).filter((sels) => !sels.every((x) => [".m-sheet", ".m-drawer", ".m-actsheet"].includes(x)));

/** The custom properties declared on a stylesheet's `:root`. */
function rootTokens(css: string): Map<string, string> {
  const block = strip(css).match(/:root\s*\{([^}]*)\}/);
  if (!block) throw new Error("no :root block");
  const out = new Map<string, string>();
  for (const m of block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out.set(m[1], m[2].trim());
  return out;
}

/** Every stylesheet and source file of the desk HUD: anything outside src/mobile/. */
const DESK_CSS = import.meta.glob(["../*.css", "../components/**/*.css", "../lib/**/*.css", "../aurora/**/*.css"],
  { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const DESK_SRC = import.meta.glob(["../*.{ts,tsx}", "../components/**/*.{ts,tsx}", "../lib/**/*.{ts,tsx}", "../state/**/*.{ts,tsx}"],
  { query: "?raw", import: "default", eager: true }) as Record<string, string>;

const px = (css: string, name: string) => Number(new RegExp(`${name}:\\s*([\\d.]+)rem`).exec(css)![1]) * 16;

describe("the phone's skin is the phone's alone", () => {
  it("keeps every phone-only token (--m-*) in the phone's stylesheet and out of the desk HUD's", () => {
    const own = [...MOBILE.matchAll(/(--m-[\w-]+)\s*:/g)].map((m) => m[1]);
    expect(new Set(own).size).toBeGreaterThan(12);
    const files = Object.keys(DESK_CSS);
    expect(files.some((f) => f.endsWith("theme.css")), "the desk's theme.css is among the files scanned").toBe(true);
    expect(files.length).toBeGreaterThan(5);
    for (const [file, css] of Object.entries(DESK_CSS)) {
      expect([file, strip(css).match(/--m-[\w-]+/g) ?? []]).toEqual([file, []]);
    }
    for (const name of own) expect(rootTokens(theme).has(name), name).toBe(false);
  });

  it("is imported by the phone entry only, never by a desk source file", () => {
    const sources = Object.entries(DESK_SRC);
    expect(sources.length).toBeGreaterThan(20);
    for (const [file, text] of sources) {
      expect([file, /mobile\/(mobile|write|talk)\.css|from ["']\.\/mobile\/|from ["']\.\.\/mobile\//.test(text)]).toEqual([file, false]);
    }
  });

  it("keeps the meaning colours fixed: amber is pending, red is an error, whatever the glass", () => {
    const hud = rootTokens(theme);
    const phone = rootTokens(mobile);
    for (const name of ["--amber", "--amber-soft", "--amber-line", "--red", "--red-soft", "--accent", "--green"]) {
      expect([name, phone.get(name)]).toEqual([name, hud.get(name)]);
    }
    // No phone token paints a pending or error colour; the card's amber and the limited state's red stay.
    expect(MOBILE).not.toMatch(/--m-[\w-]+\s*:[^;]*(#fbbf24|#f87171|251, 191, 36|248, 113, 113)/i);
    expect(MOBILE).toMatch(/\.m-limited \{[^}]*color: var\(--red\)/);
    expect(MOBILE).toMatch(/\.m-card h3 \{[^}]*color: var\(--amber\)/);
    expect(MOBILE).toMatch(/\.m-card \{[^}]*border: 1px solid var\(--amber-line\)/);
  });
});

describe("Dynamic Type: sized in rem", () => {
  it("has no px size past a hairline in any phone stylesheet", () => {
    for (const [name, css] of Object.entries(PHONE_CSS)) {
      const bad = [...css.matchAll(/(?<![\w.-])(\d+(?:\.\d+)?)px\b/g)].filter((m) => Number(m[1]) > 2).map((m) => m[0]);
      expect([name, bad]).toEqual([name, []]);
    }
  });

  it("keeps the one-tap chips on the phone tokens at 48 px, scoped under .m-chips", () => {
    expect(ITEMS).toMatch(/\.m-chips > \.tap\.m-chip \{[^}]*min-height: 3rem[^}]*var\(--m-r-btn\)/);
    expect(ITEMS).not.toMatch(/var\(--radius-btn\)/);
  });

  it("takes its text size from the reader on iOS, and never pins zoom", () => {
    expect(MOBILE).toMatch(/body \{[^}]*font: 1\.0625rem\/1\.45/);
    expect(MOBILE).toMatch(/@supports \(font: -apple-system-body\) \{\s*html \{ font: -apple-system-body; \}/);
    expect(MOBILE).toMatch(/html \{ font-size: 100%; \}/);
    expect(page).not.toMatch(/maximum-scale|user-scalable=no/);
  });
});

describe("rounder and bigger", () => {
  it("has card radius 16-20 px, buttons 12 px, a 48 px least target", () => {
    expect(px(MOBILE, "--m-r-card")).toBeGreaterThanOrEqual(16);
    expect(px(MOBILE, "--m-r-card")).toBeLessThanOrEqual(20);
    expect(px(MOBILE, "--m-r-btn")).toBe(12);
    expect(px(MOBILE, "--m-tap")).toBe(48);
    expect(MOBILE).toMatch(/\.tap\.m-btn \{[^}]*border-radius: var\(--m-r-btn\)/);
    expect(MOBILE).toMatch(/\.row \{[^}]*border-radius: var\(--m-r-card\)/);
    expect(MOBILE).toMatch(/\.chip \{[^}]*border-radius: var\(--m-r-pill\)/);
  });

  it("makes every control at least 48 px: buttons, rows, ticks, the mic and Send", () => {
    for (const sel of [".tap {", ".tap.m-btn {", ".btn {", ".m-text {", ".tap.m-chip {"]) {
      const at = MOBILE.indexOf("\n" + sel) + 1; // a rule that starts a line (not ".top .tap {")
      expect(at, sel).toBeGreaterThanOrEqual(0);
      const body = MOBILE.slice(at, MOBILE.indexOf("}", at));
      expect(Number(/min-height:\s*([\d.]+)rem/.exec(body)![1]) * 16, sel).toBeGreaterThanOrEqual(48);
    }
    // The audit's 24 px Review tick is a 48 px target now.
    const tick = MOBILE.slice(MOBILE.indexOf(".m-tick input {"), MOBILE.indexOf("}", MOBILE.indexOf(".m-tick input {")));
    expect(px(tick, "width")).toBeGreaterThanOrEqual(48);
    expect(px(tick, "height")).toBeGreaterThanOrEqual(48);
    const mic = TALK.match(/\.tap\.m-mic \{[^}]*\}/)![0];
    const send = WRITE.match(/\.tap\.m-send-round \{[^}]*\}/)![0];
    for (const rule of [mic, send]) {
      expect(px(rule, "width")).toBeGreaterThanOrEqual(48);
      expect(px(rule, "(?:min-)?height")).toBeGreaterThanOrEqual(48);
    }
  });

  it("has no text below 11 px (a mono tag) and a 17 px body", () => {
    const sizes = [...MOBILE.matchAll(/font(?:-size)?:\s*(?:\d+\s+)?([\d.]+)rem/g)].map((m) => Number(m[1]) * 16);
    expect(sizes.length).toBeGreaterThan(30);
    expect(sizes.filter((n) => n < 11)).toEqual([]);
    expect(Number(/body \{[^}]*font: ([\d.]+)rem/.exec(MOBILE)![1]) * 16).toBe(17);
  });
});

describe("aurora glass, with no blur per card", () => {
  it("loads no images or external resources in any phone stylesheet", () => {
    for (const [file, css] of Object.entries(PHONE_CSS)) expect([file, /url\s*\(/i.test(css)]).toEqual([file, false]);
  });
  it("keeps the two actions of a pair 48 px apart when they wrap or stack", () => {
    expect(MOBILE).toMatch(/\.m-pr-acts \{[^}]*gap: 3rem 3rem/);
    expect(MOBILE).toMatch(/\.m-review-rowacts \{[^}]*gap: 3rem 3rem/);
  });

  it("allows backdrop-filter only on sheets and the drawer, in every phone stylesheet", () => {
    // The guard sees every file in the folder, not a list of three.
    expect(Object.keys(PHONE_CSS).sort()).toEqual(expect.arrayContaining(["items.css", "mobile.css", "nav.css", "talk.css", "threads.css", "write.css"]));
    for (const [file, css] of Object.entries(PHONE_CSS)) expect([file, strayBlur(css)]).toEqual([file, []]);
    // The existing sheet exception exists, in the sheet's own rule, prefixed and plain.
    const sheet = blurRules(PHONE_CSS["nav.css"]);
    expect(sheet).toContainEqual([".m-sheet"]);
    expect(PHONE_CSS["nav.css"]).toMatch(/\.m-sheet \{[^}]*-webkit-backdrop-filter:[^}]*\n  backdrop-filter:/);
    // The checker itself: a shared selector list, a sibling class, a card or a nested rule are all stray.
    expect(strayBlur(".m-sheet, .row { backdrop-filter: blur(1px); }").length).toBe(1);
    expect(strayBlur(".m-sheet-dim { -webkit-backdrop-filter: blur(1px); }").length).toBe(1);
    expect(strayBlur(".m-card { color: red; backdrop-filter: blur(1px) }").length).toBe(1);
    expect(strayBlur("@media (min-width: 1px) { .m-tray { backdrop-filter: blur(1px); } }").length).toBe(1);
    expect(strayBlur(".m-drawer, .m-actsheet { backdrop-filter: blur(1px); }")).toEqual([]);
    expect(strayBlur(".m-drawer .row { backdrop-filter: blur(1px); }").length).toBe(1);
    expect(strayBlur(".m-actsheet, .row { backdrop-filter: blur(1px); }").length).toBe(1);
    expect(strayBlur(".m-sheet { backdrop-filter: blur(1px); -webkit-backdrop-filter: blur(1px); }")).toEqual([]);
  });

  it("paints a tint, a sheen and a rim", () => {
    expect(MOBILE).toMatch(/\.row \{[^}]*var\(--m-sheen\), var\(--m-glass\)/);
    expect(MOBILE).toMatch(/\.m-card \{[^}]*var\(--m-sheen\), var\(--m-glass\)/);
    expect(MOBILE).toMatch(/\.tabs \{[^}]*var\(--m-glass-bar\)/);
    expect(MOBILE).toMatch(/\.top \{[^}]*var\(--m-glass-bar\)/);
    // What scrolls beneath it (the compose bar, the sub-bar, the review footer) is near-opaque, so no text shows through.
    for (const css of [WRITE, MOBILE]) expect(css).toMatch(/var\(--m-glass-sticky\)/);
    expect(WRITE).toMatch(/\.m-compose \{[^}]*var\(--m-glass-sticky\)/);
    expect(MOBILE).toMatch(/\.subbar \{[^}]*var\(--m-glass-sticky\)/);
    expect(MOBILE).toMatch(/\.m-review-footer \{[^}]*var\(--m-glass-sticky\)/);
    expect(MOBILE).toMatch(/\nhtml \{[^}]*radial-gradient/);
  });

  it("uses the HUD's cool surfaces and washes, so the HUD's legibility rule carries over", () => {
    const hud = rootTokens(theme);
    const phone = rootTokens(mobile);
    for (const name of ["--bg-0", "--bg-1", "--bg-2", "--bg-3", "--bg-inset", "--border", "--border-strong", "--text", "--text-2", "--text-muted"]) {
      expect([name, phone.get(name)]).toEqual([name, hud.get(name)]);
    }
    const wash = (n: string) => new RegExp(`--wash-${n}:\\s*(#[0-9a-f]{6})`, "i").exec(theme)![1].toLowerCase();
    for (const n of ["1", "2", "3"]) expect(MOBILE).toContain(`--m-wash-${n}: ${wash(n)}`);
    expect(MOBILE).toContain("--m-glass: rgba(11, 19, 25, 0.50)");
  });
});
