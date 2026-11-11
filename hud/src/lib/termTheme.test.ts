import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import { contrast } from "./editorTheme";
import { TERM_TOKENS, termTheme } from "./termTheme";

const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
const decl = new Map([...(/:root\s*\{([\s\S]*?)\n\}/.exec(text)![1]).matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
const read = (n: string) => decl.get(n) ?? "";

describe("the terminal's theme (AG-1)", () => {
  it("reads its surface, text, accent and meaning colours from the tokens, and is transparent", () => {
    const t = termTheme(read);
    expect(t.foreground).toBe(decl.get("--text"));
    expect(t.cursor).toBe(decl.get("--accent"));
    expect(t.cyan).toBe(decl.get("--accent"));
    expect(t.red).toBe(decl.get("--red"));
    expect(t.green).toBe(decl.get("--green"));
    expect(t.yellow).toBe(decl.get("--amber"));
    expect(t.cursorAccent).toBe(decl.get("--bg-1"));
    expect(t.background).toBe("rgba(0, 0, 0, 0)");
    for (const n of TERM_TOKENS) expect(decl.has(n), n).toBe(true);
  });

  it("a token that cannot be read falls back, never throws", () => {
    expect(termTheme(() => "var(--nope)").foreground).toBe("#eaf2f6");
  });

  it("every ANSI colour is at least 4.5:1 on the dense-text tint over the base, except black", () => {
    const t = termTheme(read);
    const [r, g, b] = [0, 1, 2].map((i) => {
      const m = /rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)/.exec(decl.get("--glass-solid")!)!;
      const base = parseInt(decl.get("--base")!.slice(1 + 2 * i, 3 + 2 * i), 16);
      return Math.round(Number(m[1 + i]) * Number(m[4]) + base * (1 - Number(m[4])));
    });
    const bg = "#" + [r, g, b].map((n) => n.toString(16).padStart(2, "0")).join("");
    for (const [k, v] of Object.entries(t)) {
      if (!/^#[0-9a-f]{6}$/.test(v) || k === "black" || k === "cursorAccent") continue;
      expect(contrast(v, bg), `${k} ${v}`).toBeGreaterThanOrEqual(4.5);
    }
  });
});
