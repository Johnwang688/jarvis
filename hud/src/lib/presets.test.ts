// Acceptance tests for custom colour presets (owner 2026-11-13). Written by the orchestrator before the
// build; the implementer may not edit this file.
//
// Owner decisions: a custom preset remembers the accent, the three washes, the background and the editor &
// terminal colour (codeAccent); motion, drift and RGB stay separate. Only custom presets can be renamed or
// deleted; the built-ins stay fixed.
//
// Contract (hud/src/lib/presets.ts):
//   CUSTOM_PRESETS_KEY = "jarvis.hud.presets" (localStorage, a JSON array), PRESETS_EVENT fired on window
//   after every successful write, MAX_CUSTOM_PRESETS = 24, MAX_PRESET_NAME = 32 (code points).
//   CustomPreset { id: /^c-[a-z0-9]{1,24}$/, name, accent, washes[3], base, codeAccent }.
//   cleanPresetName(raw): string, trimmed, inner whitespace runs collapsed to one space, control, format and
//     bidi characters removed, cut to MAX_PRESET_NAME code points; non-strings give "".
//   checkCustomPreset(p): null, or a refusal when the five colours fail checkPreset or codeAccent fails
//     checkAccent against the preset's own washes.
//   loadCustomPresets(storage?): the valid entries in stored order; drops malformed entries, bad ids,
//     duplicate ids and names (first wins, names case-insensitively, built-in names included), entries that
//     fail the quick per-colour guards (format, checkWash/checkBase unclamped, checkAccent of accent and
//     codeAccent against the washes); keeps at most MAX_CUSTOM_PRESETS; examines every stored entry. Never
//     throws. The whole-palette proof is NOT run on load (too slow, Sol round 2); add, restore and apply run
//     the full checkCustomPreset.
//   addCustomPreset(name, colours, storage?), renameCustomPreset(id, name, storage?),
//   deleteCustomPreset(id, storage?), restoreCustomPreset(preset, index, storage?) each return
//     { ok: true, presets, preset?, index? } or { ok: false, reason }. Refusals change nothing.
import { describe, expect, it, vi } from "vitest";
import { AMBER, COLOUR_PRESETS, DEFAULT_COLOURS, type Colours } from "./appearance";
import {
  CUSTOM_PRESETS_KEY, MAX_CUSTOM_PRESETS, MAX_PRESET_NAME, PRESETS_EVENT, addCustomPreset, checkCustomPreset,
  cleanPresetName, deleteCustomPreset, loadCustomPresets, renameCustomPreset, restoreCustomPreset, type CustomPreset,
} from "./presets";

const mem = (init?: unknown) => {
  const m: Record<string, string> = {};
  if (init !== undefined) m[CUSTOM_PRESETS_KEY] = typeof init === "string" ? init : JSON.stringify(init);
  return { m, getItem: (k: string) => m[k] ?? null, setItem: (k: string, v: string) => void (m[k] = v) };
};
const stored = (s: ReturnType<typeof mem>) => JSON.parse(s.m[CUSTOM_PRESETS_KEY] ?? "null");
const NEBULA = COLOUR_PRESETS.find((p) => p.id === "nebula")!;
const nebula: Colours = { accent: NEBULA.accent, washes: [...NEBULA.washes] as [string, string, string], base: NEBULA.base, codeAccent: "#c0aef5" };
const entry = (o: Partial<CustomPreset> = {}): CustomPreset => ({ id: "c-abc", name: "Mine", ...nebula, ...o });

describe("cleanPresetName", () => {
  it("trims, collapses whitespace, strips control/format/bidi characters, caps at 32 code points", () => {
    expect(cleanPresetName("  Night   drive \t")).toBe("Night drive");
    expect(cleanPresetName("a\u0000b\u0007c\u007f")).toBe("abc");
    expect(cleanPresetName("evil‮egap⁦x​")).toBe("evilegapx");
    expect(cleanPresetName("line\nbreak")).toBe("line break");
    expect([...cleanPresetName("x".repeat(40))].length).toBe(MAX_PRESET_NAME);
    expect([...cleanPresetName("🌈".repeat(40))].length).toBe(MAX_PRESET_NAME);
    for (const bad of [null, undefined, 3, {}, []]) expect(cleanPresetName(bad)).toBe("");
  });
});

describe("checkCustomPreset", () => {
  it("passes a preset built from a built-in's colours and a readable code accent", () => {
    expect(checkCustomPreset(entry())).toBeNull();
  });
  it("refuses an amber accent, an amber code accent, a wash that would need darkening, a bad hex", () => {
    expect(checkCustomPreset(entry({ accent: AMBER }))).not.toBeNull();
    expect(checkCustomPreset(entry({ codeAccent: AMBER }))).not.toBeNull();
    expect(checkCustomPreset(entry({ codeAccent: "#1a3a4a" }))).not.toBeNull();
    expect(checkCustomPreset(entry({ washes: ["#9fe8ff", NEBULA.washes[1], NEBULA.washes[2]] }))).not.toBeNull();
    expect(checkCustomPreset(entry({ base: "url(x)" }))).not.toBeNull();
  });
});

describe("loadCustomPresets", () => {
  it("is empty for nothing stored, corrupt JSON or a non-array", () => {
    expect(loadCustomPresets(mem())).toEqual([]);
    expect(loadCustomPresets(mem("{nope"))).toEqual([]);
    expect(loadCustomPresets(mem({ a: 1 }))).toEqual([]);
  });
  it("keeps valid entries in order and drops bad ones", () => {
    const good2 = entry({ id: "c-def", name: "Second" });
    const s = mem([
      entry(),
      { ...entry({ id: "c-bad1", name: "Amber" }), accent: AMBER },
      { ...entry({ name: "Same id" }) },
      entry({ id: "C-UPPER", name: "Bad id" }),
      entry({ id: "c-ghi", name: "mine" }),
      entry({ id: "c-jkl", name: "Moonlight" }),
      entry({ id: "c-mno", name: "   " }),
      "string",
      null,
      good2,
    ]);
    expect(loadCustomPresets(s).map((p) => p.id)).toEqual(["c-abc", "c-def"]);
  });
  it("cleans stored names and caps the list", () => {
    const many = Array.from({ length: MAX_CUSTOM_PRESETS + 5 }, (_, i) => entry({ id: `c-n${i}`, name: `P ${i}` }));
    expect(loadCustomPresets(mem(many))).toHaveLength(MAX_CUSTOM_PRESETS);
    expect(loadCustomPresets(mem([entry({ name: "  Tidy ‮ me " })]))[0]!.name).toBe("Tidy me");
  });
  it("never throws when storage throws", () => {
    const bad = { getItem: () => { throw new Error("x"); }, setItem: () => { throw new Error("x"); } };
    expect(loadCustomPresets(bad)).toEqual([]);
  });
});

describe("add / rename / delete / restore", () => {
  it("add saves the six colours under a cleaned name with a fresh c- id, and fires PRESETS_EVENT", () => {
    const s = mem();
    const spy = vi.fn();
    window.addEventListener(PRESETS_EVENT, spy);
    const r = addCustomPreset("  Night drive ", nebula, s);
    window.removeEventListener(PRESETS_EVENT, spy);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.preset).toMatchObject({ name: "Night drive", ...nebula });
    expect(r.preset!.id).toMatch(/^c-[a-z0-9]{1,24}$/);
    expect(stored(s)).toEqual([r.preset]);
    expect(spy).toHaveBeenCalledTimes(1);
    const r2 = addCustomPreset("Other", DEFAULT_COLOURS, s);
    expect(r2.ok && r2.preset!.id !== r.preset!.id).toBe(true);
    expect(loadCustomPresets(s).map((p) => p.name)).toEqual(["Night drive", "Other"]);
  });

  it("add refuses an empty name, a taken name (custom or built-in, any case), a full list and unsafe colours, changing nothing", () => {
    const s = mem([entry()]);
    const before = s.m[CUSTOM_PRESETS_KEY];
    for (const [name, colours] of [
      ["   ", nebula], ["MINE", nebula], ["aurora", nebula], ["Northern Lights", nebula],
      ["Hot", { ...nebula, accent: AMBER }], ["Hot2", { ...nebula, codeAccent: AMBER }],
    ] as [string, Colours][]) {
      const r = addCustomPreset(name, colours, s);
      expect(r.ok, name).toBe(false);
      expect(s.m[CUSTOM_PRESETS_KEY]).toBe(before);
    }
    const full = mem(Array.from({ length: MAX_CUSTOM_PRESETS }, (_, i) => entry({ id: `c-n${i}`, name: `P ${i}` })));
    expect(addCustomPreset("One more", nebula, full).ok).toBe(false);
  });

  it("rename cleans the name, allows a case change of its own name, refuses empty/taken/unknown id", () => {
    const s = mem([entry(), entry({ id: "c-def", name: "Second" })]);
    expect(renameCustomPreset("c-abc", "  Dusk ", s).ok).toBe(true);
    expect(loadCustomPresets(s)[0]!.name).toBe("Dusk");
    expect(renameCustomPreset("c-abc", "DUSK", s).ok).toBe(true);
    expect(loadCustomPresets(s)[0]!.name).toBe("DUSK");
    expect(renameCustomPreset("c-abc", "second", s).ok).toBe(false);
    expect(renameCustomPreset("c-abc", "Graphite", s).ok).toBe(false);
    expect(renameCustomPreset("c-abc", "", s).ok).toBe(false);
    expect(renameCustomPreset("c-zzz", "New", s).ok).toBe(false);
    expect(renameCustomPreset("aurora", "Mine now", s).ok).toBe(false);
    expect(loadCustomPresets(s).map((p) => p.name)).toEqual(["DUSK", "Second"]);
  });

  it("delete returns the preset and its index; restore puts it back at that index", () => {
    const s = mem([entry(), entry({ id: "c-def", name: "Second" }), entry({ id: "c-ghi", name: "Third" })]);
    const d = deleteCustomPreset("c-def", s);
    expect(d.ok).toBe(true);
    if (!d.ok) return;
    expect(d.preset!.name).toBe("Second");
    expect(d.index).toBe(1);
    expect(loadCustomPresets(s).map((p) => p.id)).toEqual(["c-abc", "c-ghi"]);
    expect(restoreCustomPreset(d.preset!, d.index!, s).ok).toBe(true);
    expect(loadCustomPresets(s).map((p) => p.id)).toEqual(["c-abc", "c-def", "c-ghi"]);
    expect(deleteCustomPreset("c-zzz", s).ok).toBe(false);
    expect(deleteCustomPreset("moonlight", s).ok).toBe(false);
  });

  it("restore refuses a duplicate id or name and an unsafe preset; clamps the index", () => {
    const s = mem([entry()]);
    expect(restoreCustomPreset(entry(), 0, s).ok).toBe(false);
    expect(restoreCustomPreset(entry({ id: "c-new", name: "mine" }), 0, s).ok).toBe(false);
    expect(restoreCustomPreset(entry({ id: "c-new", name: "Hot", accent: AMBER }), 0, s).ok).toBe(false);
    expect(restoreCustomPreset(entry({ id: "c-new", name: "Late" }), 99, s).ok).toBe(true);
    expect(loadCustomPresets(s).map((p) => p.id)).toEqual(["c-abc", "c-new"]);
  });

  it("writes keep entries this HUD does not understand out (the stored list is the cleaned list)", () => {
    const s = mem([entry(), { junk: true }]);
    addCustomPreset("Two", DEFAULT_COLOURS, s);
    expect((stored(s) as unknown[]).length).toBe(2);
    expect((stored(s) as CustomPreset[]).every((p) => typeof p.id === "string")).toBe(true);
  });
});
