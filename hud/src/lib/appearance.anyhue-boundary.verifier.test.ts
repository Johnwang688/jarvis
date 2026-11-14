import { afterEach, beforeEach, expect, it, vi } from "vitest";
import css from "../theme.css?raw";
import {
  AMBER, APPEARANCE_KEY, BASE_MAX_LUMINANCE, DEFAULT_APPEARANCE, DEFAULT_COLOURS,
  WASH_MAX_LUMINANCE, checkAccent, checkBase, checkWash, resolveColours, saveAppearance,
} from "./appearance";
import { applyAppearance, installAppearance } from "./appearanceApply";
import { cssPalette, themeData } from "./editorTheme";
import { cssTermTheme } from "./termTheme";
import { palette } from "./orbpalette";
import { sparkLavender } from "./boot";
import { parseColour, readPalette } from "../aurora/palette";
import { luminance } from "./oklch";

const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
const defaults = new Map([...(/:root\s*\{([\s\S]*?)\n\}/.exec(text)![1])
  .matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
let uninstall: () => void;
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(window, "getComputedStyle").mockImplementation(el => ({
    getPropertyValue: (name: string) => (el as HTMLElement).style.getPropertyValue(name) || defaults.get(name) || "",
  }) as CSSStyleDeclaration);
  uninstall = installAppearance();
});
afterEach(() => {
  uninstall();
  localStorage.clear();
  applyAppearance(DEFAULT_APPEARANCE);
  vi.restoreAllMocks();
});

const warm = {
  accent: AMBER, codeAccent: "#ffa3a3", base: "#140806",
  washes: ["#8a2a10", "#7a4a00", "#6a1a2a"],
};
const opaque = ["--accent", "--accent-hi", "--code-accent", "--code-accent-hi", "--syn-fn",
  "--wash-1", "--wash-2", "--wash-3", "--base", "--bg-0", "--badge-fast"];

it.each(["save", "storage"])("%s propagates a warm palette to CSS, orb, boot, Monaco and xterm without recolouring meanings", path => {
  const oldEditor = themeData(cssPalette());
  const oldTerm = cssTermTheme();
  if (path === "save") saveAppearance(warm);
  else {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(warm));
    window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY }));
  }
  const root = document.documentElement;
  for (const name of opaque) expect(root.style.getPropertyValue(name), name).toMatch(/^#[0-9a-f]{6}$/);
  expect(root.style.getPropertyValue("--accent")).toBe(warm.accent);
  expect(root.style.getPropertyValue("--code-accent")).toBe(warm.codeAccent);
  expect(root.style.getPropertyValue("--wash-3")).toBe(warm.washes[2]);
  expect(cssPalette()["--code-accent"]).toBe(warm.codeAccent);
  expect(themeData(cssPalette())).not.toEqual(oldEditor);
  expect(cssTermTheme().cursor).toBe(warm.codeAccent);
  expect(cssTermTheme().yellow).toBe(oldTerm.yellow);
  expect(cssTermTheme().red).toBe(oldTerm.red);
  expect(palette("idle").hue).toBe("251,191,36");
  expect(palette("tool").hue).toBe("251,191,36");
  expect(palette("approval").hue).toBe("251,191,36");
  expect(palette("error").hue).toBe("248,113,113");
  const boot = readPalette();
  expect(boot.colours).toEqual(warm.washes.map(w => parseColour(w)));
  expect(sparkLavender(boot.colours[2], parseColour(warm.accent)!))
    .toEqual(boot.colours[2].map(v => v + (1 - v) * 0.5));
  expect(root.style.getPropertyValue("--amber")).toBe("");
  expect(root.style.getPropertyValue("--red")).toBe("");
});

it.each(["url(//invalid)", "var(--red)", "#abc", "#abcdef00", "#FFA3A3\n", "#FFA3A3;--evil:red"])(
  "every owner colour rejects malformed storage-event values %j with RGB both on and off", bad => {
    for (const rgb of [false, true]) {
      for (const slot of ["accent", "codeAccent", "base", "washes"]) {
        const record = { ...warm, rgb, [slot]: slot === "washes" ? [bad, bad, bad] : bad };
        localStorage.setItem(APPEARANCE_KEY, JSON.stringify(record));
        window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY }));
        for (const name of opaque) {
          const value = document.documentElement.style.getPropertyValue(name);
          expect(value, `${slot}/${rgb}/${name}`).toMatch(/^#[0-9a-f]{6}$/);
          expect(value).not.toContain(bad);
        }
        expect(document.documentElement.getAttribute("style")).not.toMatch(/url\(|var\(|--evil/);
      }
    }
  },
);

it("resolves both accents against the shown washes after the removed blend fallback", () => {
  // Restores the surviving contrast intent of appearancePalette.test.ts's deleted shown-washes test.
  const washes = ["#e70075", "#858260", "#ffffff"];
  const shown = washes.map(w => {
    const r = checkWash(w);
    expect(r.ok).toBe(true);
    if (!r.ok) throw new Error(r.reason);
    return r.value;
  });
  expect(checkAccent("#ababab").ok).toBe(true);
  expect(checkAccent("#ababab", shown).ok).toBe(false);
  const actual = resolveColours({ accent: "#ababab", codeAccent: "#ababab", washes });
  expect(actual.washes).toEqual(shown);
  expect(actual.accent).toBe(DEFAULT_COLOURS.accent);
  expect(actual.codeAccent).toBe(DEFAULT_COLOURS.codeAccent);
});

it.each(["#ffffff", AMBER, "#ff9900", "#ffa3a3"])("bright warm picks %s retain both caps and their darkening notes", hex => {
  for (const [check, cap, label] of [[checkWash, WASH_MAX_LUMINANCE, "aurora"], [checkBase, BASE_MAX_LUMINANCE, "background"]] as const) {
    const r = check(hex);
    expect(r.ok).toBe(true);
    if (!r.ok) throw new Error(r.reason);
    expect(r.value).toMatch(/^#[0-9a-f]{6}$/);
    expect(luminance(r.value)).toBeLessThanOrEqual(cap);
    if (luminance(hex) > cap) {
      expect(r.clamped).toBe(true);
      expect(r.note).toBe(`Darkened to ${r.value} so text stays readable over the ${label}.`);
    }
  }
});
