import { afterEach, beforeEach, expect, it, vi } from "vitest";
import css from "../theme.css?raw";
import { DEFAULT_APPEARANCE, saveAppearance } from "./appearance";
import { applyAppearance, installAppearance } from "./appearanceApply";
import { cssPalette, themeData } from "./editorTheme";
import { cssTermTheme } from "./termTheme";
const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
const defaults = new Map([...(/:root\s*\{([\s\S]*?)\n\}/.exec(text)![1]).matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
let uninstall: () => void;
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(window, "getComputedStyle").mockImplementation(el => ({
    getPropertyValue: (name: string) => (el as HTMLElement).style.getPropertyValue(name) || defaults.get(name) || "",
  }) as CSSStyleDeclaration);
  uninstall = installAppearance();
});
afterEach(() => { uninstall(); localStorage.clear(); applyAppearance(DEFAULT_APPEARANCE); vi.restoreAllMocks(); });
it("live accent-only events preserve every editor and terminal theme colour; code events change both", () => {
  const originalEditor = themeData(cssPalette()), originalTerm = cssTermTheme();
  saveAppearance({ accent: "#e8edf3" });
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#e8edf3");
  expect(themeData(cssPalette())).toEqual(originalEditor);
  expect(cssTermTheme()).toEqual(originalTerm);
  saveAppearance({ codeAccent: "#c0aef5" });
  expect(themeData(cssPalette())).not.toEqual(originalEditor);
  expect(cssTermTheme().cyan).toBe("#c0aef5");
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#e8edf3");
});
