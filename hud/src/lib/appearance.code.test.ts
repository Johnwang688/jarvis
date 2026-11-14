// Acceptance tests: the editor and terminal colour is its own setting, not the accent (owner 2026-11-13:
// "make the editor text colors separately adjusted from the accent color"). Written by the orchestrator
// before the build; the implementer may not edit this file.
//
// Contract: a `codeAccent` (`#rrggbb`, default today's cyan) drives `--code-accent`, `--code-accent-hi` and
// `--syn-fn`. The editor's chrome and the terminal read `--code-accent*` where they read `--accent*` before.
// The UI accent no longer moves any of them. Same guards as the accent (checkAccent against the washes).
import { describe, expect, it } from "vitest";
import {
  AMBER, DEFAULT_ACCENT, DEFAULT_CODE_ACCENT, DEFAULT_COLOURS, colourProps, parseAppearance, resolveColours, rgbWashes,
} from "./appearance";
import { applyAppearance } from "./appearanceApply";
import { CHROME, CHROME_TOKENS } from "./editorTheme";
import { TERM_TOKENS, termTheme } from "./termTheme";
import css from "../theme.css?raw";

const LAVENDER = "#c0aef5";

describe("codeAccent in the colour model", () => {
  it("defaults to today's cyan and is part of DEFAULT_COLOURS", () => {
    expect(DEFAULT_CODE_ACCENT).toBe("#6fc3df");
    expect(DEFAULT_CODE_ACCENT).toBe(DEFAULT_ACCENT);
    expect(DEFAULT_COLOURS.codeAccent).toBe(DEFAULT_CODE_ACCENT);
  });

  it("is parsed as #rrggbb only", () => {
    expect(parseAppearance(JSON.stringify({ codeAccent: "#C0AEF5" })).codeAccent).toBe(LAVENDER);
    expect(parseAppearance(JSON.stringify({ codeAccent: "url(x)" })).codeAccent).toBeUndefined();
  });

  it("resolves independently of the accent, through the same guard, and survives RGB mode", () => {
    expect(resolveColours({ accent: "#e8edf3" }).codeAccent).toBe(DEFAULT_CODE_ACCENT);
    expect(resolveColours({ codeAccent: LAVENDER }).codeAccent).toBe(LAVENDER);
    expect(resolveColours({ codeAccent: LAVENDER }).accent).toBe(DEFAULT_ACCENT);
    expect(resolveColours({ codeAccent: AMBER }).codeAccent).toBe(DEFAULT_CODE_ACCENT);
    expect(resolveColours({ codeAccent: "#1a3a4a" }).codeAccent).toBe(DEFAULT_CODE_ACCENT); // too dark
    const rgb = resolveColours({ rgb: true, codeAccent: LAVENDER });
    expect(rgb.codeAccent).toBe(LAVENDER);
    expect(rgb.washes).toEqual(rgbWashes(0));
  });

  it("the default reproduces theme.css: --code-accent, --code-accent-hi, --syn-fn", () => {
    const p = colourProps(DEFAULT_COLOURS);
    expect(p["--code-accent"]).toBe("#6fc3df");
    expect(p["--code-accent-hi"]).toBe("#9ad6ea");
    expect(p["--syn-fn"]).toBe("#8ec5d6");
    expect(css).toMatch(/--code-accent:\s*#6fc3df;/);
    expect(css).toMatch(/--code-accent-hi:\s*#9ad6ea;/);
  });

  it("the accent no longer moves the syntax colour; the code accent does", () => {
    const white = colourProps({ ...DEFAULT_COLOURS, accent: "#e8edf3" });
    expect(white["--syn-fn"]).toBe("#8ec5d6");
    expect(white["--code-accent"]).toBe("#6fc3df");
    const lav = colourProps({ ...DEFAULT_COLOURS, codeAccent: LAVENDER });
    expect(lav["--code-accent"]).toBe(LAVENDER);
    expect(lav["--syn-fn"]).not.toBe("#8ec5d6");
    expect(lav["--code-accent-hi"]).not.toBe("#9ad6ea");
    expect(lav["--accent"]).toBe(DEFAULT_ACCENT);
  });

  it("applyAppearance writes a custom code accent and nothing else changes the accent", () => {
    const root = document.createElement("div");
    applyAppearance({ motion: "fluid", stir: true, activity: true, drift: 1, rgb: false, rgbSpeed: 30, codeAccent: LAVENDER }, root);
    expect(root.style.getPropertyValue("--code-accent")).toBe(LAVENDER);
    expect(root.style.getPropertyValue("--accent")).toBe(DEFAULT_ACCENT);
    applyAppearance({ motion: "fluid", stir: true, activity: true, drift: 1, rgb: false, rgbSpeed: 30 }, root);
    expect(root.style.getPropertyValue("--code-accent")).toBe("");
  });
});

describe("the editor and terminal read the code accent", () => {
  it("the editor chrome reads --code-accent*, never --accent*", () => {
    expect(CHROME_TOKENS).toContain("--code-accent");
    expect(CHROME_TOKENS).toContain("--code-accent-hi");
    expect(CHROME_TOKENS as readonly string[]).not.toContain("--accent");
    expect(CHROME_TOKENS as readonly string[]).not.toContain("--accent-hi");
    expect(CHROME.filter((r) => r.token === ("--accent" as string) || r.token === ("--accent-hi" as string))).toEqual([]);
    expect(CHROME.find((r) => r.key === "editorCursor.foreground")?.token).toBe("--code-accent-hi");
    expect(CHROME.find((r) => r.key === "editor.selectionBackground")?.token).toBe("--code-accent");
  });

  it("the terminal's cyan, cursor and selection follow --code-accent, not --accent", () => {
    expect(TERM_TOKENS as readonly string[]).toContain("--code-accent");
    expect(TERM_TOKENS as readonly string[]).not.toContain("--accent");
    const tokens: Record<string, string> = { "--accent": "#e8edf3", "--accent-hi": "#ffffff", "--code-accent": LAVENDER, "--code-accent-hi": "#d8ccfa" };
    const t = termTheme((n) => tokens[n] ?? "");
    expect(t.cyan).toBe(LAVENDER);
    expect(t.cursor).toBe(LAVENDER);
    expect(t.brightCyan).toBe("#d8ccfa");
    expect(t.selectionBackground.startsWith(LAVENDER)).toBe(true);
  });
});
