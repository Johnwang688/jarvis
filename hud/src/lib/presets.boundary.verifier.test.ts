import { afterEach, describe, expect, it, vi } from "vitest";
import { AMBER, COLOUR_PRESETS, DEFAULT_COLOURS } from "./appearance";
import {
  MAX_CUSTOM_PRESETS, PRESETS_EVENT, addCustomPreset, cleanPresetName,
  deleteCustomPreset, loadCustomPresets, renameCustomPreset, restoreCustomPreset, type CustomPreset,
} from "./presets";

const entry = (patch: Partial<CustomPreset> = {}): CustomPreset => ({
  ...DEFAULT_COLOURS, washes: [...DEFAULT_COLOURS.washes], id: "c-one", name: "One", ...patch,
});
function memory(value: unknown = []) {
  let raw = typeof value === "string" ? value : JSON.stringify(value);
  return { getItem: () => raw, setItem: vi.fn((_key: string, value: string) => { raw = value; }) };
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("custom preset trust boundary (independent verifier)", () => {
  it.each(["accent", "base", "codeAccent", "wash-0", "wash-1", "wash-2"])("rejects hostile CSS in %s", slot => {
    for (const bad of ["url(https://invalid/)", "var(--red)", "red", "#fff", "#12345678", "#c0aef5; color:red", "#c0aef5\n", {}, [], null, 123]) {
      const p: Record<string, unknown> = { ...entry(), washes: [...DEFAULT_COLOURS.washes] };
      if (slot.startsWith("wash-")) (p.washes as unknown[])[Number(slot.slice(5))] = bad;
      else p[slot] = bad;
      expect(loadCustomPresets(memory([p])), JSON.stringify({ slot, bad })).toEqual([]);
    }
  });

  it("rejects code accents against their own washes, including readable default-only accents", () => {
    const gray: [string, string, string] = ["#838283", "#838283", "#838283"];
    expect(loadCustomPresets(memory([entry({ washes: gray, accent: "#e8edf3", codeAccent: "#ababab" })]))).toEqual([]);
    expect(loadCustomPresets(memory([entry({ codeAccent: AMBER })]))).toEqual([]);
  });

  it("ignores __proto__, constructor, deep unknown fields and non-entry nesting", () => {
    const deep = "[".repeat(20_000) + "0" + "]".repeat(20_000);
    const raw = JSON.stringify(entry()).slice(0, -1)
      + ',"__proto__":{"polluted":true},"constructor":{"prototype":{"polluted":true}},"extra":' + deep + "}";
    expect(loadCustomPresets(memory("[" + raw + "]"))).toEqual([entry()]);
    expect(loadCustomPresets(memory(deep))).toEqual([]);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  it("cleans every Unicode control/format character and counts astral characters once", () => {
    for (const cp of [0, 7, 0x7f, 0x85, 0x200b, 0x200c, 0x200d, 0x200e, 0x202a, 0x202e, 0x2066, 0x2069, 0xfeff, 0xe0001]) {
      expect(cleanPresetName("a" + String.fromCodePoint(cp) + "b")).toMatch(/^a ?b$/);
    }
    expect(cleanPresetName("  🌈".repeat(100))).toBe(Array(16).fill("🌈").join(" "));
    expect(cleanPresetName("🌈".repeat(33))).toBe("🌈".repeat(32));
    expect(cleanPresetName("x".repeat(200_000))).toBe("x".repeat(32));
  });

  it("rejects cleaned and case-folded taken names and allows its own renamed name", () => {
    const s = memory([entry(), entry({ id: "c-two", name: "Two" })]);
    for (const name of ["  TWO ", "T\u202ewo", "Ｔｗｏ", ...COLOUR_PRESETS.map(p => p.name.toUpperCase())]) {
      const before = s.getItem();
      expect(renameCustomPreset("c-one", name, s).ok, name).toBe(false);
      expect(s.getItem()).toBe(before);
    }
    expect(renameCustomPreset("c-one", "  O\u202eNE  ", s).ok).toBe(true);
    expect(loadCustomPresets(s)[0]!.name).toBe("ONE");
  });

  it("refused writes preserve exact bytes and emit no change event", () => {
    const s = memory("[" + JSON.stringify(entry()) + ', {"junk":true}]');
    const before = s.getItem();
    const spy = vi.fn();
    window.addEventListener(PRESETS_EVENT, spy);
    try {
      expect(addCustomPreset("one", DEFAULT_COLOURS, s).ok).toBe(false);
      expect(renameCustomPreset("c-one", "Aurora", s).ok).toBe(false);
      expect(deleteCustomPreset("c-gone", s).ok).toBe(false);
      expect(restoreCustomPreset(entry({ id: "c-two", name: "One" }), 0, s).ok).toBe(false);
      expect(s.getItem()).toBe(before);
      expect(s.setItem).not.toHaveBeenCalled();
      expect(spy).not.toHaveBeenCalled();
    } finally { window.removeEventListener(PRESETS_EVENT, spy); }
  });

  it("enforces 24 on restore without replacing entries another window added", () => {
    const full = Array.from({ length: MAX_CUSTOM_PRESETS }, (_, i) => entry({ id: `c-n${i}`, name: `Preset ${i}` }));
    const s = memory(full);
    const before = s.getItem();
    expect(restoreCustomPreset(entry(), 0, s).ok).toBe(false);
    expect(s.getItem()).toBe(before);
    expect(s.setItem).not.toHaveBeenCalled();
  });

  it("never acknowledges an unpersisted write or emits a change event", () => {
    const s = { getItem: () => "[]", setItem: () => { throw new Error("blocked"); } };
    const listener = vi.fn();
    window.addEventListener(PRESETS_EVENT, listener);
    try {
      expect(addCustomPreset("New", DEFAULT_COLOURS, s).ok).toBe(false);
      expect(listener).not.toHaveBeenCalled();
    } finally { window.removeEventListener(PRESETS_EVENT, listener); }
  });

  it("keeps ids unique when random retries and the timestamp fallback both collide", () => {
    vi.spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
    vi.stubGlobal("crypto", { getRandomValues: (array: Uint32Array) => array.fill(0) });
    const s = memory([entry({ id: "c-000" }), entry({ id: `c-${Date.now().toString(36)}`, name: "Two" })]);
    const before = s.getItem();
    const result = addCustomPreset("Three", DEFAULT_COLOURS, s);
    if (!result.ok) { expect(s.getItem()).toBe(before); return; }
    expect(new Set(result.presets.map(p => p.id)).size).toBe(result.presets.length);
    expect(loadCustomPresets(s).map(p => p.name)).toEqual(["One", "Two", "Three"]);
  });
});
