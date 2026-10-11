// The orb's state palette, shared by the big orb (components/Orb.tsx, a
// canvas) and each chat's mini orb (components/MiniOrb.tsx, an inline SVG) —
// WP-H, decisions S-4. One function, so amber and red can never take the
// avatar's accent in one orb and not the other.
//
// The avatar's accent recolours the cyan family only. Amber (a tool running,
// an authorization pending) and red (an error) are *meanings*, not
// decoration — an avatar that could repaint them could make a running command
// look idle.

import type { OrbState } from "../state/store";

export interface OrbPalette {
  /** "r,g,b": `rgba(${hue},a)` on the canvas, `rgb(var(--mo-hue))` in CSS. */
  hue: string;
  boost: number;
  spin: number;
}

/** The states an avatar's accent may recolour. Never tool, approval or error. */
export const ACCENTABLE: ReadonlySet<OrbState> = new Set<OrbState>([
  "idle", "listening", "transcribing", "thinking", "composing", "speaking",
]);

/** The meanings: amber for a tool or an authorization, red for an error. */
export const AMBER = "251,191,36";
export const RED = "248,113,113";

/** An accent as the avatars API sends it ("r,g,b", each 0–255), else null. */
export function accentHue(accent: string | null | undefined): string | null {
  if (typeof accent !== "string") return null;
  const m = /^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*$/.exec(accent);
  if (!m) return null;
  const parts = [m[1], m[2], m[3]].map(Number);
  return parts.every((n) => n <= 255) ? parts.join(",") : null;
}

export function palette(state: OrbState, accent?: string | null): OrbPalette {
  const p: OrbPalette = (() => {
    switch (state) {
      // Graphite: the cool states are the glacier accent (111,195,223) and its
      // lighter step (154,214,234). Idle is a little dimmer and slower, so the
      // orb is present without glowing at you all session.
      case "listening": return { hue: "154,214,234", boost: 1.35, spin: 1.6 };
      case "transcribing": return { hue: "154,214,234", boost: 1.2, spin: 2.2 };
      case "thinking": return { hue: "111,195,223", boost: 1.15, spin: 3.4 };
      case "composing": return { hue: "125,205,200", boost: 1.3, spin: 1.8 };
      case "tool": return { hue: AMBER, boost: 1.2, spin: 3.4 };
      case "approval": return { hue: AMBER, boost: 1.5, spin: 0.35 };
      case "speaking": return { hue: "154,214,234", boost: 1.5, spin: 1.2 };
      case "error": return { hue: RED, boost: 1.3, spin: 0.6 };
      default: return { hue: "111,195,223", boost: 0.85, spin: 0.8 };
    }
  })();
  // An accent that is not "r,g,b" is ignored rather than handed to a canvas
  // or a CSS property as it came.
  const hue = accentHue(accent);
  if (hue && ACCENTABLE.has(state)) p.hue = hue;
  return p;
}
