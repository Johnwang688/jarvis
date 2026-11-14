// Applies the owner's colours to the window (AG-3): sets the custom properties on `:root`'s inline style
// and the orb's hues, from a record that lib/appearance.ts has checked. With every colour at its default
// nothing is set inline, so theme.css alone paints. No reload: Monaco, xterm and the orb re-read on the
// same event (monacoBundle.ts, Terminal.tsx, orbpalette.ts); the aurora re-reads on the event or the
// style change.

import {
  APPEARANCE_EVENT, DEFAULT_ACCENT, DEFAULT_CODE_ACCENT, DEFAULT_COLOURS, colourProps, deriveAccent, loadAppearance, resolveColours,
  type Appearance,
} from "./appearance";
import { DEFAULT_COOL, setCoolHues } from "./orbpalette";

/** Every custom property AG-3 may set; a reset removes exactly these. */
export const MANAGED_PROPS: readonly string[] = Object.keys(colourProps(DEFAULT_COLOURS));

export function applyAppearance(a: Appearance, root: HTMLElement = document.documentElement): void {
  const c = resolveColours(a);
  const isDefault =
    c.accent === DEFAULT_ACCENT && c.codeAccent === DEFAULT_CODE_ACCENT && c.base === DEFAULT_COLOURS.base
    && c.washes.every((w, i) => w === DEFAULT_COLOURS.washes[i]);
  if (isDefault) {
    for (const k of MANAGED_PROPS) root.style.removeProperty(k);
    setCoolHues(DEFAULT_COOL);
    return;
  }
  // Only values that came out of resolveColours (`#rrggbb`, or numbers derived from one) are written.
  for (const [k, v] of Object.entries(colourProps(c))) root.style.setProperty(k, v);
  setCoolHues(deriveAccent(c.accent).orb);
}

/**
 * Applies the saved record now (before React renders) and again on every appearance change. Installed
 * before the aurora, so its listener, registered later, sees the new tokens. Returns the uninstaller.
 */
export function installAppearance(win: Window = window): () => void {
  const apply = () => applyAppearance(loadAppearance());
  apply();
  win.addEventListener(APPEARANCE_EVENT, apply);
  win.addEventListener("storage", apply);
  return () => {
    win.removeEventListener(APPEARANCE_EVENT, apply);
    win.removeEventListener("storage", apply);
  };
}
