import { expect, it } from "vitest";
import { DEFAULT_COLOURS } from "./appearance";
import { addCustomPreset, deleteCustomPreset, loadCustomPresets, MAX_CUSTOM_PRESETS, renameCustomPreset } from "./presets";

const survivor = { ...DEFAULT_COLOURS, id: "c-survivor", name: "Survivor" };
function memory() {
  // Invalid records must be dropped without consuming the quota of valid presets.
  let raw = JSON.stringify([...Array(MAX_CUSTOM_PRESETS * 2).fill(null), survivor]);
  return { getItem: () => raw, setItem: (_k: string, v: string) => { raw = v; } };
}
it("keeps a valid preset after 48 malformed records", () => {
  expect(loadCustomPresets(memory())).toEqual([survivor]);
});
it("does not erase a valid stored preset when saving after malformed records", () => {
  const s = memory();
  expect(addCustomPreset("New", DEFAULT_COLOURS, s).ok).toBe(true);
  expect(loadCustomPresets(s).map(p => p.name)).toEqual(["Survivor", "New"]);
});
it.each(["rename", "delete"])("can %s a valid id following malformed records", action => {
  const s = memory();
  const result = action === "rename" ? renameCustomPreset(survivor.id, "Renamed", s) : deleteCustomPreset(survivor.id, s);
  expect(result.ok).toBe(true);
});
