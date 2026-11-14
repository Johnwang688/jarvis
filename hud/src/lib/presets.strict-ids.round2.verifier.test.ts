import { afterEach, expect, it, vi } from "vitest";
import { AMBER, DEFAULT_COLOURS, checkAccent, checkPreset } from "./appearance";
import { addCustomPreset, checkCustomPreset, loadCustomPresets, restoreCustomPreset, type CustomPreset } from "./presets";

const entry = (patch: Partial<CustomPreset> = {}): CustomPreset => ({ ...DEFAULT_COLOURS, id: "c-one", name: "One", ...patch });
function memory(list: unknown = []) {
  let raw = JSON.stringify(list);
  return { getItem: () => raw, setItem: (_k: string, v: string) => { raw = v; } };
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each([
  ["accent", { accent: AMBER }],
  ["dark accent", { accent: "#101418" }],
  ["code accent", { codeAccent: AMBER }],
  ["dark code accent", { codeAccent: "#101418" }],
  ["bright wash", { washes: ["#ffffff", "#3468c4", "#6450c4"] }],
  ["warm wash", { washes: [AMBER, "#3468c4", "#6450c4"] }],
  ["bright base", { base: "#405060" }],
  ["warm base", { base: "#5a1010" }],
  ["blend hit", { washes: ["#fa0397", "#3468c4", "#6450c4"], base: "#021000" }],
  ["proof budget", { washes: ["#0090a1", "#606d42", "#7e7155"], base: "#00181b" }],
] as [string, Partial<CustomPreset>][])("never keeps, adds, restores or caches acceptance for a refused %s", (_slot, patch) => {
  const p = entry(patch);
  expect(checkPreset(p) ?? (checkAccent(p.codeAccent, p.washes).ok ? null : "bad code")).not.toBeNull();
  for (let repeat = 0; repeat < 2; repeat++) {
    expect(checkCustomPreset(p)).not.toBeNull();
    // Orchestrator rewrite (2026-11-14): the whole-palette proof is too slow to run on load (Sol round 2:
    // 24 valid near-band palettes take ~3 s), so load drops what fails the quick per-colour guards and a
    // proof-only refusal is caught where it matters: add, restore and apply (AuroraSetting.custom-apply.test.ts).
    const proofOnly = _slot === "blend hit" || _slot === "proof budget";
    if (!proofOnly) expect(loadCustomPresets(memory([p]))).toEqual([]);
    for (const op of ["add", "restore"]) {
      const s = memory();
      const before = s.getItem();
      expect((op === "add" ? addCustomPreset(p.name, p, s) : restoreCustomPreset(p, 0, s)).ok).toBe(false);
      expect(s.getItem()).toBe(before);
    }
  }
});

it.each(["crypto", "Math.random"])("keeps all 24 ids well formed and unique with a colliding %s and clock", source => {
  vi.spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
  if (source === "crypto") vi.stubGlobal("crypto", { getRandomValues: (a: Uint32Array) => a.fill(0) });
  else { vi.stubGlobal("crypto", undefined); vi.spyOn(Math, "random").mockReturnValue(0); }
  const s = memory();
  const ids = new Set<string>();
  for (let i = 0; i < 24; i++) {
    const result = addCustomPreset("Custom " + i, DEFAULT_COLOURS, s);
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error(result.reason);
    const id = result.preset!.id;
    expect(id).toMatch(/^c-[a-z0-9]{1,24}$/);
    expect(ids.has(id)).toBe(false);
    ids.add(id);
    expect(loadCustomPresets(s)).toHaveLength(i + 1);
  }
});

it("skips an already populated timestamp and counter family", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
  vi.stubGlobal("crypto", { getRandomValues: (a: Uint32Array) => a.fill(0) });
  const stamp = Date.now().toString(36);
  const list = [entry({id:"c-000"}), ...Array.from({length:21}, (_,i) => entry({id:"c-" + stamp + (i ? i.toString(36) : ""), name:"Taken " + i}))];
  const s = memory(list);
  const result = addCustomPreset("New", DEFAULT_COLOURS, s);
  expect(result.ok).toBe(true);
  if (!result.ok) throw new Error(result.reason);
  expect(result.preset!.id).toBe("c-" + stamp + (21).toString(36));
  expect(loadCustomPresets(s)).toHaveLength(23);
});
