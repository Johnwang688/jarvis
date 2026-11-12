// The HUD's fonts (AG-1, owner D-9): Plus Jakarta Sans for the UI, Geist Mono for code, both bundled
// from @fontsource and served from 'self' (the CSP's `font-src 'self' data:` is unchanged).
//
// Canvas-like consumers measure glyphs when they open (xterm's cell size, Monaco's char width), so
// they wait for `fontsReady()` first, or their cells would be sized for the fallback face and stay
// wrong until the next refit.

/** The mono stack: `--mono` in theme.css, kept in step with it by fonts.test.ts. */
export const MONO_STACK = '"Geist Mono", ui-monospace, "Cascadia Mono", Consolas, monospace';

/** `--mono` as the window has it, else the constant (tests, a window whose tokens cannot be read). */
export function monoFamily(): string {
  try {
    if (typeof document !== "undefined") {
      const v = getComputedStyle(document.documentElement).getPropertyValue("--mono").trim();
      if (v) return v;
    }
  } catch { /* fall through */ }
  return MONO_STACK;
}

const FACES = ['400 13px "Plus Jakarta Sans"', '600 13px "Plus Jakarta Sans"', '400 13px "Geist Mono"', '600 13px "Geist Mono"'];
let ready: Promise<void> | null = null;

/** Resolves once the bundled faces are loaded (or failed: a missing font must never block a terminal or an editor). */
export function fontsReady(): Promise<void> {
  if (!ready) {
    ready = (async () => {
      try {
        const fonts = typeof document !== "undefined" ? document.fonts : undefined;
        if (!fonts) return;
        await Promise.all(FACES.map((f) => fonts.load(f).catch(() => [])));
        await fonts.ready;
      } catch { /* a font that did not load is the fallback face, not an error */ }
    })();
  }
  return ready;
}
