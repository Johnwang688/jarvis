import { describe, expect, it } from "vitest";
import { MODIFIER_KEYS, isModifierKey } from "./modifierkeys";
import { isModifierKey as frameguardIsModifier } from "./frameguard";
import { isModifierKey as remoteIsModifier } from "./remote";
import { isTakeover } from "./drive";

describe("one modifier-key list", () => {
  it("is exactly the keys both old copies agreed on", () => {
    expect([...MODIFIER_KEYS].sort()).toEqual(["Alt", "AltGraph", "Control", "Meta", "OS", "Shift"]);
  });

  it("is the list frameguard and remote both use", () => {
    for (const key of ["Shift", "Control", "Alt", "Meta", "AltGraph", "OS", "CapsLock", "Fn", "Super", "Hyper", "FnLock", "a", "Tab"]) {
      expect(frameguardIsModifier(key)).toBe(isModifierKey(key));
      expect(remoteIsModifier(key)).toBe(isModifierKey(key));
    }
  });

  it("exempts none of the keys the two copies disagreed on", () => {
    for (const key of ["CapsLock", "Fn", "Super", "Hyper", "FnLock"]) expect(isModifierKey(key)).toBe(false);
  });

  it("takes non-strings as not a modifier", () => {
    for (const key of [undefined, null, 1, {}, ["Shift"]]) expect(isModifierKey(key)).toBe(false);
  });

  it("a lone disputed key now counts as the owner taking over", () => {
    for (const key of ["CapsLock", "Fn", "Super", "Hyper", "FnLock"]) {
      expect(isTakeover({ t: "key", e: { key } } as never)).toBe(true);
    }
    expect(isTakeover({ t: "key", e: { key: "Control" } } as never)).toBe(false);
  });
});
