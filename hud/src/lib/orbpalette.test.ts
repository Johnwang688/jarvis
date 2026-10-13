import { describe, expect, it } from "vitest";
import { ACCENTABLE, AMBER, RED, accentHue, palette } from "./orbpalette";
import type { OrbState } from "../state/store";

const ALL: OrbState[] = [
  "idle", "listening", "transcribing", "thinking", "composing", "tool", "approval", "speaking", "error",
];
const HOOT = "168,85,247";

describe("the orb palette (WP-H)", () => {
  it("never gives a tool, an approval or an error the avatar's accent", () => {
    // Amber and red are meanings: an avatar that could repaint them could make
    // a running command, or an authorization waiting on the owner, look idle.
    expect(palette("tool", HOOT).hue).toBe(AMBER);
    expect(palette("approval", HOOT).hue).toBe(AMBER);
    expect(palette("error", HOOT).hue).toBe(RED);
    for (const s of ["tool", "approval", "error"] as OrbState[]) expect(ACCENTABLE.has(s)).toBe(false);
  });

  it("gives the accent to every cyan state, thinking included", () => {
    for (const s of ALL) {
      if (s === "tool" || s === "approval" || s === "error") continue;
      expect(palette(s, HOOT).hue, s).toBe(HOOT);
      expect(palette(s).hue, s).not.toBe(HOOT);
    }
  });

  it("is the big orb's palette, unchanged without an accent", () => {
    expect(palette("thinking")).toEqual({ hue: "111,195,223", boost: 1.15, spin: 3.4 });
    expect(palette("tool")).toEqual({ hue: AMBER, boost: 1.2, spin: 3.4 });
    expect(palette("approval")).toEqual({ hue: AMBER, boost: 1.5, spin: 0.35 });
    expect(palette("error")).toEqual({ hue: RED, boost: 1.3, spin: 0.6 });
    expect(palette("idle")).toEqual({ hue: "111,195,223", boost: 0.85, spin: 0.8 });
  });

  it("ignores an accent that is not r,g,b, rather than handing it to a canvas or a CSS property", () => {
    for (const bad of ["red", "#a855f7", "168,85", "300,0,0", "1,2,3);background:url(x)", "", null, undefined]) {
      expect(palette("thinking", bad as any).hue, String(bad)).toBe("111,195,223");
      expect(accentHue(bad as any)).toBeNull();
    }
    expect(accentHue(" 168, 85 ,247 ")).toBe(HOOT);
  });
});

/** Every source file under src/, tests excluded, by path ("../components/Orb.tsx"). */
const SOURCES = import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../**/*.test.ts", "!../**/*.test.tsx"], {
  query: "?raw", import: "default", eager: true,
}) as Record<string, string>;
const source = (path: string) => {
  const hit = Object.entries(SOURCES).find(([k]) => k.endsWith(path));
  if (!hit) throw new Error(`no source ${path}`);
  return hit[1];
};

describe("one palette for both orbs (a grep, like terminal_check's no-tool test)", () => {
  const importsPalette = /import\s*\{[^}]*\bpalette\b[^}]*\}\s*from\s*"\.\.\/lib\/orbpalette"/;

  it("reads the real sources", () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(20);
    expect(source("components/Orb.tsx")).toContain("export function Orb(");
  });

  it("Orb.tsx and MiniOrb.tsx both import palette() from lib/orbpalette", () => {
    expect(source("components/Orb.tsx")).toMatch(importsPalette);
    expect(source("components/MiniOrb.tsx")).toMatch(importsPalette);
  });

  it("and nothing else defines a palette, the accent rule, or the amber and red hues", () => {
    const offenders = Object.entries(SOURCES)
      .filter(([path]) => !/(^|\/)orbpalette\.ts$/.test(path))
      .filter(([, text]) =>
        /function\s+palette\s*\(|\bACCENTABLE\s*=|251\s*,\s*191\s*,\s*36|248\s*,\s*113\s*,\s*113/.test(text))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});
