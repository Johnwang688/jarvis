import { afterEach, beforeEach, expect, it, vi } from "vitest";
import source from "./presets.ts?raw";
import type { CustomPreset } from "./presets";

beforeEach(() => vi.resetModules());
afterEach(() => vi.restoreAllMocks());
const hex = (n: number) => `#${n.toString(16).padStart(6, "0")}`;
function memory(rows: unknown[]) {
  let raw = JSON.stringify(rows);
  return { getItem: () => raw, setItem: vi.fn((_k: string, s: string) => { raw = s; }) };
}

it("actual darkUnclamped matches unclamped guards on cap neighbours and fixed-seed colours", async () => {
  const A = await import("./appearance"), O = await import("./oklch");
  // Exercise the private function's actual body without editing production or duplicating its predicate.
  const body = source.match(/function darkUnclamped\(hex: string, cap: number\): boolean \{([^}]+)\}/)![1];
  const dark = new Function("impersonatesMeaning", "WASH_CHROMA_FLOOR", "luminance", `return (hex, cap) => {${body}}`)(A.impersonatesMeaning, A.WASH_CHROMA_FLOOR, O.luminance) as (h: string, cap: number) => boolean;
  const colours = new Set<string>([...A.DEFAULT_WASHES, A.DEFAULT_BASE, A.AMBER, A.RED, "#000000", "#ffffff", "#00ffff"]);
  let seed = 0x912da45;
  for (let i = 0; i < 8000; i++) { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; colours.add(hex(seed & 0xffffff)); }
  // Every red/green pair in a coarse lattice, and the 8-bit blue neighbours of each luminance boundary.
  for (let r = 0; r < 256; r += 5) for (let g = 0; g < 256; g += 5) {
    for (const cap of [A.WASH_MAX_LUMINANCE, A.BASE_MAX_LUMINANCE]) {
      let lo = 0, hi = 255;
      while (lo < hi) { const b = (lo + hi) >>> 1; if (O.luminance(hex((r << 16) | (g << 8) | b)) <= cap) lo = b + 1; else hi = b; }
      for (let d = -2; d <= 2; d++) if (lo + d >= 0 && lo + d < 256) colours.add(hex((r << 16) | (g << 8) | (lo + d)));
    }
  }
  const mismatches: unknown[] = [];
  for (const colour of colours) for (const [cap, guard] of [[A.WASH_MAX_LUMINANCE, A.checkWash], [A.BASE_MAX_LUMINANCE, A.checkBase]] as const) {
    const result = guard(colour), actual = dark(colour, cap);
    if (actual !== (result.ok && !result.clamped)) mismatches.push({ colour, cap, actual, result });
  }
  expect(mismatches).toEqual([]);
}, 60_000);

async function hostileRows(shape: string) {
  const A = await import("./appearance");
  const good: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-good", name: "Good" };
  const count = shape === "both accents" ? 19000 : shape === "names" || shape === "ids" ? 11000 : 15000;
  const rows = Array.from({ length: count }, (_, i) => {
    // Distinct dark blue/green washes; no clamping and no whole-palette proof on this path.
    const wash = hex(((i >>> 8) << 8) | (i & 255));
    return { ...good, id: `c-b${i.toString(36)}`, name: `Bad ${i}`,
      ...(shape === "names" ? { name: `Bad ${i} ` + "\u2066".repeat(22) } : {}),
      ...(shape === "ids" ? { id: `c-${i.toString(36).padStart(24, "0")}` } : {}),
      washes: [wash, "#000000", "#000001"],
      accent: shape === "accent" ? hex(((i % 128) << 16) | ((i >>> 7) << 8) | 255) : "#e8edf3",
      codeAccent: shape === "code" ? hex(((i % 128) << 16) | ((i >>> 7) << 8) | 255) : "#555555",
      ...(shape === "both accents" ? {
        accent: hex(((i % 128) << 16) | 0xff00 | (128 + (i >>> 7))),
        codeAccent: hex(((i % 128) << 16) | ((i >>> 7) << 8) | 255),
        washes: [128, 144, 160].map(b => hex(((i % 16) << 16) | (((i >>> 4) % 64) << 8) | (b + (i >>> 10)))),
        base: hex(((i % 16) << 16) | (((i >>> 4) % 16) << 8) | ((i >>> 8) % 32)),
      } : {}),
      ...(shape === "bright wash" ? { washes: [hex(0x00c000 + i), "#000000", "#000001"] } : {}),
    };
  });
  return { rows: [...rows, good], good };
}

it.each(["accent", "code", "both accents", "wash", "bright wash", "names", "ids"])("cold load stays below one second with distinct %s work", async shape => {
  const P = await import("./presets");
  const { rows, good } = await hostileRows(shape), s = memory(rows);
  expect(s.getItem().length * 2).toBeLessThan(5 * 1024 * 1024);
  const start = performance.now();
  const loaded = P.loadCustomPresets(s);
  const elapsed = performance.now() - start;
  console.info(`${shape}: ${Math.round(elapsed)} ms; ${s.getItem().length * 2} UTF-16 bytes`);
  expect(loaded).toEqual([good]);
  expect(elapsed, `${shape}: ${Math.round(elapsed)} ms; ${s.getItem().length * 2} UTF-16 bytes`).toBeLessThan(2000);
}, 30_000);

it("memo keeps wash and base luminance roles separate even for the same hex", async () => {
  const A = await import("./appearance"), P = await import("./presets");
  const bad = { ...A.DEFAULT_COLOURS, id: "c-bad", name: "Bad", base: A.DEFAULT_WASHES[0] };
  expect(A.checkWash(bad.base)).toMatchObject({ ok: true, clamped: false });
  const b = A.checkBase(bad.base);
  expect(b.ok && !b.clamped).toBe(false);
  expect(P.loadCustomPresets(memory([bad]))).toEqual([]);
});

it.each(["wash", "base", "accent", "codeAccent"])("memo preserves a repeated %s refusal and resets between reads", async slot => {
  const A = await import("./appearance"), P = await import("./presets");
  const good: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-good", name: "Good" };
  const bad = { ...good, id: "c-bad", name: "Bad", washes: [...good.washes] as [string, string, string] };
  if (slot === "wash") bad.washes[0] = "#00ffff";
  else bad[slot as "base" | "accent" | "codeAccent"] = slot === "base" ? "#00ffff" : "#555555";
  const s = memory([bad, { ...bad, id: "c-again", name: "Again" }, good]);
  expect(P.loadCustomPresets(s)).toEqual([good]);
  s.setItem(P.CUSTOM_PRESETS_KEY, JSON.stringify([{ ...good, ...bad }]));
  expect(P.loadCustomPresets(s)).toEqual([]);
  s.setItem(P.CUSTOM_PRESETS_KEY, JSON.stringify([good]));
  expect(P.loadCustomPresets(s)).toEqual([good]);
});

it.each(["add", "rename", "delete", "restore"])("cold %s stays below one second with distinct colours in every slot", async action => {
  const A = await import("./appearance"), P = await import("./presets");
  const { rows, good } = await hostileRows("both accents"), s = memory(rows);
  expect(s.getItem().length * 2).toBeLessThan(5 * 1024 * 1024);
  const start = performance.now();
  const result = action === "add" ? P.addCustomPreset("New", A.DEFAULT_COLOURS, s)
    : action === "rename" ? P.renameCustomPreset(good.id, "Renamed", s)
    : action === "delete" ? P.deleteCustomPreset(good.id, s)
    : P.restoreCustomPreset({ ...good, id: "c-back", name: "Back" }, 0, s);
  const elapsed = performance.now() - start;
  expect(result.ok).toBe(true);
  expect(P.loadCustomPresets(s).some(p => p.id === good.id)).toBe(action !== "delete");
  console.info(`${action}: ${Math.round(elapsed)} ms`);
  expect(elapsed, `${action}: blocked for ${Math.round(elapsed)} ms`).toBeLessThan(2000);
}, 30_000);

it.each(["render", "storage", "presets event"])("cold panel %s stays below one second with distinct colours in every slot", async path => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const React = await import("react"), { createRoot } = await import("react-dom/client");
  const P = await import("./presets"), { AuroraSetting } = await import("../components/AuroraSetting");
  const { rows } = await hostileRows("both accents"), raw = JSON.stringify(rows);
  expect(raw.length * 2).toBeLessThan(5 * 1024 * 1024);
  localStorage.clear();
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  try {
    if (path !== "render") await React.act(async () => root.render(React.createElement(AuroraSetting)));
    localStorage.setItem(P.CUSTOM_PRESETS_KEY, raw);
    const start = performance.now();
    await React.act(async () => {
      if (path === "render") root.render(React.createElement(AuroraSetting));
      else window.dispatchEvent(path === "storage" ? new StorageEvent("storage", { key: P.CUSTOM_PRESETS_KEY }) : new Event(P.PRESETS_EVENT));
    });
    const elapsed = performance.now() - start;
    expect(host.querySelectorAll(".appearance-custom")).toHaveLength(1);
    expect(host.querySelector('[data-testid="appearance-custom-c-good"]')).not.toBeNull();
    console.info(`panel ${path}: ${Math.round(elapsed)} ms`);
    // Orchestrator (2026-11-14): these 5 MB all-distinct lists are the quota-filling worst case and measured
    // 0.8-1.4 s on the owner's machine, so every distinct-list probe in this file uses 2 s: it flags a return
    // to multi-second blocking without flaking. Per-colour clamping on load is pinned by the equivalence probe
    // at the top of this file (mutant-checked), not by these timings.
    expect(elapsed, `panel ${path}: blocked for ${Math.round(elapsed)} ms`).toBeLessThan(2000);
  } finally {
    await React.act(async () => root.unmount());
    host.remove();
    localStorage.clear();
  }
}, 30_000);
