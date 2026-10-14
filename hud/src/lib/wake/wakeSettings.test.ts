import { describe, expect, it } from "vitest";
import { DEFAULT_CARD_COMMANDS, DEFAULT_SENSITIVITY, loadCardCommands, loadSensitivity, saveCardCommands, saveSensitivity } from "./wakeSettings";

const store = (init: Record<string, string> = {}) => {
  const m = new Map(Object.entries(init));
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => { m.set(k, v); }, m };
};
const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };

describe("the owner's wake-word choices", () => {
  it("default to Normal and to hearing the four commands under a card", () => {
    expect(loadSensitivity(store())).toBe(DEFAULT_SENSITIVITY);
    expect(DEFAULT_SENSITIVITY).toBe("normal");
    expect(loadCardCommands(store())).toBe(DEFAULT_CARD_COMMANDS);
    expect(DEFAULT_CARD_COMMANDS).toBe(true);
  });

  it("round-trip, and a value nobody wrote is the default", () => {
    const s = store();
    saveSensitivity("high", s); saveCardCommands(false, s);
    expect(loadSensitivity(s)).toBe("high");
    expect(loadCardCommands(s)).toBe(false);
    saveCardCommands(true, s);
    expect(loadCardCommands(s)).toBe(true);
    expect(loadSensitivity(store({ "jarvis.wakeSensitivity": "loudest" }))).toBe("normal");
    expect(loadCardCommands(store({ "jarvis.wakeCardCommands": "maybe" }))).toBe(true);
  });

  it("storage that throws leaves the defaults and the window working", () => {
    expect(loadSensitivity(throwing)).toBe("normal");
    expect(loadCardCommands(throwing)).toBe(true);
    expect(() => saveSensitivity("low", throwing)).not.toThrow();
    expect(() => saveCardCommands(false, throwing)).not.toThrow();
  });
});
