// @vitest-environment node
// The VEX view's colours (VX-UI-2, plan §3.1, §8): **amber is never a VEX
// result**. In Graphite amber means an approval card is pending; in the VEX
// view only the status line's "Held" says that. Read from the stylesheet the
// window loads, so a mark, badge, help number or stale note that took amber
// fails here, not only in a browser check.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(decodeURIComponent(new URL("../theme.css", import.meta.url).pathname), "utf8");

/** Every rule whose selector names a VEX class: [selector, body]. */
function vexRules(): [string, string][] {
  const out: [string, string][] = [];
  const body = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const m of body.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sel = m[1].trim();
    if (/\.vex[a-z-]*/.test(sel)) out.push([sel, m[2]]);
  }
  return out;
}

describe("VEX colours", () => {
  it("finds the VEX rules (the check reads something)", () => {
    const sels = vexRules().map(([s]) => s).join("\n");
    for (const c of [".vexmark.ok", ".vexbadgemark", ".vexhelpnum", ".vexstalenote", ".vexheld"]) expect(sels).toContain(c);
  });

  it("amber appears only where the view is held by an approval card", () => {
    const amber = vexRules().filter(([, b]) => /--amber/.test(b)).map(([s]) => s);
    expect(amber.sort()).toEqual([".vexheld", ".vexstatus.held"]);
  });

  it("each mark state has its Graphite colour, and stale is the ok colour dimmed", () => {
    const rule = (sel: string) => vexRules().find(([s]) => s.split(",").map((x) => x.trim()).includes(sel))?.[1] ?? "";
    expect(rule(".vexmark.busy")).toMatch(/var\(--accent\)/);
    expect(rule(".vexmark.ok")).toMatch(/var\(--green\)/);
    expect(rule(".vexmark.failed")).toMatch(/var\(--red\)/);
    expect(rule(".vexmark.stale")).toMatch(/var\(--green\)[\s\S]*opacity: 0\.6/);
    expect(rule(".vexmark.unknown")).toMatch(/var\(--text-muted\)/);
  });
});
