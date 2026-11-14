// The terminal's colours, from the HUD's tokens (AG-1). They used to be hex
// copied into Terminal.tsx; now the surface, text, accent and the three
// meaning colours are read off `:root`, so the terminal cannot drift from
// theme.css. xterm 6 (DOM renderer) is given `allowTransparency: true` and a
// transparent background: the slot behind it (`.termslot`) paints the
// `--glass-solid` tint, so cell text sits on the same dense-text glass as
// Monaco and the transcript, and a program's own cell backgrounds still draw.
//
// The ANSI colours that have no token (blue, the bright row) are quiet cool
// siblings of the HUD's hues; none is amber or red beyond the real
// `red`/`yellow` slots, which read `--red` and `--amber` exactly.

import { parseToken } from "./editorTheme";

/** The tokens the terminal reads. */
export const TERM_TOKENS = [
  "--bg-0", "--bg-1", "--text", "--code-accent", "--code-accent-hi", "--red", "--amber", "--green", "--badge-claude",
] as const;
type TermToken = (typeof TERM_TOKENS)[number];

/** Used for a token that cannot be read (a window whose tokens are missing still gets a terminal). */
const FALLBACK: Record<TermToken, string> = {
  "--bg-0": "#0a1116", "--bg-1": "#0f181f", "--text": "#eaf2f6", "--code-accent": "#6fc3df", "--code-accent-hi": "#9ad6ea",
  "--red": "#f87171", "--amber": "#fbbf24", "--green": "#6dd08f", "--badge-claude": "#c0aef5",
};

export interface TermTheme {
  background: string; foreground: string; cursor: string; cursorAccent: string;
  selectionBackground: string; selectionInactiveBackground: string;
  black: string; red: string; green: string; yellow: string; blue: string; magenta: string; cyan: string; white: string;
  brightBlack: string; brightRed: string; brightGreen: string; brightYellow: string;
  brightBlue: string; brightMagenta: string; brightCyan: string; brightWhite: string;
}

/** The opaque `#rrggbb` of a token, or its fallback. */
function opaque(read: (name: string) => string, t: TermToken): string {
  const v = parseToken(read(t) ?? "");
  return v ? v.slice(0, 7) : FALLBACK[t];
}

export function termTheme(read: (name: string) => string): TermTheme {
  const c = (t: TermToken) => opaque(read, t);
  const accent = c("--code-accent");
  return {
    // Transparent: the slot behind paints the glass (see the header).
    background: "rgba(0, 0, 0, 0)", foreground: c("--text"), cursor: accent, cursorAccent: c("--bg-1"),
    selectionBackground: `${accent}55`, selectionInactiveBackground: `${accent}2a`,
    black: c("--bg-0"), red: c("--red"), green: c("--green"), yellow: c("--amber"),
    blue: "#7aa7d8", magenta: c("--badge-claude"), cyan: accent, white: "#cfd5d9",
    brightBlack: "#8a979f", brightRed: "#fca5a5", brightGreen: "#9be3b4", brightYellow: "#fcd34d",
    brightBlue: "#9cc2ea", brightMagenta: "#d8ccfa", brightCyan: c("--code-accent-hi"), brightWhite: "#f3f7f9",
  };
}

/** The theme as the window draws it. */
export function cssTermTheme(root: Element = document.documentElement): TermTheme {
  const style = getComputedStyle(root);
  return termTheme((name) => style.getPropertyValue(name));
}
