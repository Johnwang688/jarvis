import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { CustomPreset } from "./presets";

// Independent round 3 probes. All modules (and both verdict caches) start cold.
const rows = [
  ["#0090a1", "#606d42", "#7e7155", "#00181b"],
  ["#1f2e00", "#009189", "#6d760f", "#080100"],
  ["#444a00", "#765c6d", "#708972", "#000202"],
  ["#e13eb1", "#de1aa3", "#b51459", "#000000"],
  ["#bc6892", "#313a24", "#3a3b28", "#110e18"],
  ["#707100", "#6a6d4c", "#788865", "#070100"],
  ["#3e3d00", "#514e00", "#7d8300", "#000000"],
  ["#708a6a", "#4e4e00", "#606200", "#040003"],
];
const proofOnly = (n = 23): CustomPreset[] => Array.from({ length: n }, (_, i) => {
  const [a, b, c, base] = rows[i % rows.length];
  return { id: `c-q${i.toString(36)}`, name: `Stored ${i}`, accent: "#e8edf3", codeAccent: "#e8edf3", washes: [a, b, c], base };
});
function memory(list: unknown) {
  let raw = JSON.stringify(list);
  return { getItem: () => raw, setItem: vi.fn((_k: string, v: string) => { raw = v; }) };
}
beforeEach(() => vi.resetModules());
afterEach(() => vi.restoreAllMocks());

it.each(["load", "add", "rename", "delete", "restore"])("cold %s never proves stored palettes", async action => {
  const A = await import("./appearance");
  const proof = vi.spyOn(A, "checkPreset");
  const P = await import("./presets");
  const list = proofOnly();
  const s = memory(list);
  const start = performance.now();
  if (action === "load") expect(P.loadCustomPresets(s)).toEqual(list);
  if (action === "add") expect(P.addCustomPreset("Added", A.DEFAULT_COLOURS, s).ok).toBe(true);
  if (action === "rename") expect(P.renameCustomPreset(list[0].id, "Renamed", s).ok).toBe(true);
  if (action === "delete") expect(P.deleteCustomPreset(list[0].id, s).ok).toBe(true);
  if (action === "restore") expect(P.restoreCustomPreset({ ...A.DEFAULT_COLOURS, id: "c-back", name: "Back" }, 3, s).ok).toBe(true);
  const elapsed = performance.now() - start;
  const newProofs = action === "add" || action === "restore" ? 1 : 0;
  expect(proof.mock.calls.map(([p]) => p.washes), "only the new preset may be proved").toEqual(Array.from({ length: newProofs }, () => A.DEFAULT_COLOURS.washes));
  expect(elapsed, `${action} blocked for ${Math.round(elapsed)} ms`).toBeLessThan(1000);
});

it.each(["duplicate", "clamped wash", "clamped base", "gamut-clamped wash", "gamut-clamped base", "late code refusal"])("bounds cold load of quota-sized %s records and keeps a late survivor", async shape => {
  const A = await import("./appearance");
  const P = await import("./presets");
  const good: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-good", name: "Good" };
  const bad = { ...good, id: "c-bad", name: "Bad",
    ...(shape === "clamped wash" ? { washes: ["#ffffff", ...good.washes.slice(1)] } : {}),
    ...(shape === "clamped base" ? { base: "#ffffff" } : {}),
    ...(shape === "gamut-clamped wash" ? { washes: ["#00ffff", ...good.washes.slice(1)] } : {}),
    ...(shape === "gamut-clamped base" ? { base: "#00ffff" } : {}),
    ...(shape === "late code refusal" ? { codeAccent: A.AMBER } : {}),
  };
  const s = memory([...Array(shape.startsWith("gamut") ? 20_000 : 12_000).fill(bad), good]);
  // < 5 MiB even in UTF-16, inside an ordinary localStorage quota.
  expect(s.getItem().length * 2).toBeLessThan(5 * 1024 * 1024);
  const start = performance.now();
  expect(P.loadCustomPresets(s).map(p => p.id)).toEqual(shape === "duplicate" ? ["c-bad", "c-good"] : ["c-good"]);
  const elapsed = performance.now() - start;
  expect(elapsed, `${shape}: blocked for ${Math.round(elapsed)} ms on ${s.getItem().length} chars`).toBeLessThan(1000);
}, 30_000);

it.each(["add", "rename", "delete", "restore"])("bounds cold %s with a quota-sized prefix of per-colour refusals", async action => {
  const A = await import("./appearance");
  const P = await import("./presets");
  const good: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-good", name: "Good" };
  const bad = { ...good, base: "#00ffff" };
  const s = memory([...Array(20_000).fill(bad), good]);
  expect(s.getItem().length * 2).toBeLessThan(5 * 1024 * 1024);
  const start = performance.now();
  const result = action === "add" ? P.addCustomPreset("New", A.DEFAULT_COLOURS, s)
    : action === "rename" ? P.renameCustomPreset(good.id, "Renamed", s)
    : action === "delete" ? P.deleteCustomPreset(good.id, s)
    : P.restoreCustomPreset({ ...good, id: "c-back", name: "Back" }, 0, s);
  const elapsed = performance.now() - start;
  expect(result.ok).toBe(true);
  expect(elapsed, `${action}: blocked for ${Math.round(elapsed)} ms on ${20_000} quick refusals`).toBeLessThan(1000);
}, 30_000);

it.each(["add", "restore"])("a quick-only load cannot cache a safe verdict for a proof refusal before %s", async action => {
  const P = await import("./presets");
  const unsafe = proofOnly(1)[0];
  const source = memory([unsafe]);
  expect(P.loadCustomPresets(source)).toEqual([unsafe]);
  const s = memory([]), before = s.getItem();
  const result = action === "add" ? P.addCustomPreset("Offered", unsafe, s) : P.restoreCustomPreset(unsafe, 0, s);
  expect(result.ok).toBe(false);
  expect(P.checkCustomPreset(unsafe)).not.toBeNull();
  expect(s.getItem()).toBe(before);
  expect(s.setItem).not.toHaveBeenCalled();
});

it("a cached safe verdict cannot exempt a changed code accent or wash", async () => {
  const A = await import("./appearance");
  const P = await import("./presets");
  const p: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-safe", name: "Safe" };
  expect(P.checkCustomPreset(p)).toBeNull();
  expect(P.checkCustomPreset({ ...p, codeAccent: A.AMBER })).not.toBeNull();
  expect(P.checkCustomPreset({ ...p, washes: [A.AMBER, p.washes[1], p.washes[2]] })).not.toBeNull();
});

it.each(["add", "restore"])("%s refuses every quick guard without changing storage", async action => {
  const A = await import("./appearance");
  const P = await import("./presets");
  const p: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-offered", name: "Offered" };
  for (const slot of ["accent", "codeAccent", "base", "wash0", "wash1", "wash2"]) {
    const values = [A.AMBER, "#010101", "#00ffff", "url(x)", "#abcdef\n", "#abc", "#12345678"];
    for (const value of values) {
      const bad = { ...p, washes: [...p.washes] as [string, string, string] };
      if (slot.startsWith("wash")) bad.washes[Number(slot.slice(4))] = value;
      else bad[slot as "accent" | "codeAccent" | "base"] = value;
      const guard = slot === "base" ? A.checkBase(value) : slot.startsWith("wash") ? A.checkWash(value) : A.checkAccent(value, bad.washes);
      if (guard.ok && !guard.clamped) continue;
      const s = memory([]), before = s.getItem();
      const result = action === "add" ? P.addCustomPreset(p.name, bad, s) : P.restoreCustomPreset(bad, 0, s);
      expect(result.ok, `${action} ${slot}=${value}`).toBe(false);
      expect(s.getItem()).toBe(before);
      expect(s.setItem).not.toHaveBeenCalled();
    }
  }
});

it.each(["add", "rename", "delete", "restore"])("%s retains all valid rows behind mixed malformed/duplicate prefixes and respects 24", async action => {
  const A = await import("./appearance");
  const P = await import("./presets");
  const list = Array.from({ length: 23 }, (_, i) => ({ ...A.DEFAULT_COLOURS, id: `c-v${i.toString(36)}`, name: `Valid ${i}` }));
  const noise = Array.from({ length: 160 }, (_, i) => i % 2 ? { ...list[0], id: "bad" } : { ...list[0], codeAccent: A.AMBER });
  const s = memory([...noise, ...list.flatMap(p => [p, p])]);
  const fresh = { ...A.DEFAULT_COLOURS, id: "c-new", name: "New" };
  const result = action === "add" ? P.addCustomPreset(fresh.name, fresh, s)
    : action === "rename" ? P.renameCustomPreset(list[0].id, "Renamed", s)
    : action === "delete" ? P.deleteCustomPreset(list[0].id, s)
    : P.restoreCustomPreset(fresh, 7, s);
  expect(result.ok).toBe(true);
  const kept = P.loadCustomPresets(s);
  expect(kept.filter(p => p.id.startsWith("c-v")).map(p => p.id)).toEqual((action === "delete" ? list.slice(1) : list).map(p => p.id));
  expect(kept.length).toBeLessThanOrEqual(24);
  if (action === "add" || action === "restore") {
    expect(kept).toHaveLength(24);
    const before = s.getItem();
    expect(P.addCustomPreset("Overflow", A.DEFAULT_COLOURS, s).ok).toBe(false);
    expect(P.restoreCustomPreset({ ...fresh, id: "c-more", name: "More" }, 0, s).ok).toBe(false);
    expect(s.getItem()).toBe(before);
  }
});
