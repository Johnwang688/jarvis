import { afterEach, describe, expect, it, vi } from "vitest";
import { readKey, writeKey } from "./storage";
import { loadLastProject, saveLastProject } from "./compose";
import { loadNoiseSuppression, saveNoiseSuppression } from "./listen/pref";
import { loadSparkle, saveSparkle } from "./sparkle";
import { loadReadAloud, saveReadAloud, loadSelfVoiceGate, saveSelfVoiceGate, loadDuckBrowser, saveDuckBrowser } from "./speech";
import { loadEndPause, saveEndPause } from "./voiceTiming";
import { loadSensitivity, saveSensitivity, loadCardCommands, saveCardCommands, DEFAULT_SENSITIVITY, DEFAULT_CARD_COMMANDS } from "./wake/wakeSettings";
import { END_PAUSE_MS } from "./vad";

const mem = (init: Record<string, string> = {}) => {
  const m = { ...init };
  return { getItem: (k: string) => (k in m ? m[k] : null), setItem: (k: string, v: string) => { m[k] = v; }, m };
};
const boom = () => { throw new Error("blocked"); };
const blocked = { getItem: boom, setItem: boom };

afterEach(() => vi.unstubAllGlobals());

describe("readKey", () => {
  it("returns the stored string, or null for an unset key", () => {
    const s = mem({ k: "v", e: "" });
    expect(readKey("k", s)).toBe("v");
    expect(readKey("e", s)).toBe("");
    expect(readKey("missing", s)).toBeNull();
  });
  it("returns null when the storage throws", () => {
    expect(readKey("k", blocked)).toBeNull();
  });
  it("defaults to window.localStorage, also for null", () => {
    vi.stubGlobal("window", { localStorage: mem({ k: "w" }) });
    expect(readKey("k")).toBe("w");
    expect(readKey("k", null)).toBe("w");
  });
  it("returns null when there is no window or window.localStorage throws on access", () => {
    vi.stubGlobal("window", { get localStorage() { throw new Error("SecurityError"); } });
    expect(readKey("k")).toBeNull();
    vi.stubGlobal("window", undefined);
    expect(readKey("k")).toBeNull();
  });
  it("takes a function so a global whose access throws is still guarded", () => {
    expect(readKey("k", () => mem({ k: "s" }))).toBe("s");
    expect(readKey("k", boom)).toBeNull();
  });
});

describe("writeKey", () => {
  it("stores the value", () => {
    const s = mem();
    writeKey("k", "v", s);
    expect(s.m).toEqual({ k: "v" });
  });
  it("ignores a storage that throws, on call or on access", () => {
    expect(() => writeKey("k", "v", blocked)).not.toThrow();
    expect(() => writeKey("k", "v", boom)).not.toThrow();
  });
  it("defaults to window.localStorage, also for null, and survives its absence", () => {
    const ls = mem();
    vi.stubGlobal("window", { localStorage: ls });
    writeKey("a", "1");
    writeKey("b", "2", null);
    expect(ls.m).toEqual({ a: "1", b: "2" });
    vi.stubGlobal("window", { get localStorage() { throw new Error("SecurityError"); } });
    expect(() => writeKey("a", "1")).not.toThrow();
  });
});

describe("the sites that inject a storage survive one that throws, with their default", () => {
  it("loads give the default", () => {
    expect(loadLastProject(blocked as unknown as Storage)).toBeNull();
    expect(loadNoiseSuppression(blocked)).toBe(false);
    expect(loadSparkle(blocked)).toBe(true);
    expect(loadReadAloud(blocked)).toBe(true);
    expect(loadSelfVoiceGate(blocked)).toBe(true);
    expect(loadDuckBrowser(blocked)).toBe(true);
    expect(loadEndPause(blocked)).toBe(END_PAUSE_MS);
    expect(loadSensitivity(blocked)).toBe(DEFAULT_SENSITIVITY);
    expect(loadCardCommands(blocked)).toBe(DEFAULT_CARD_COMMANDS);
  });
  it("saves do not throw", () => {
    const b = blocked as unknown as Storage;
    expect(() => {
      saveLastProject("p", b);
      saveNoiseSuppression(true, blocked);
      saveSparkle(false, blocked);
      saveReadAloud(false, blocked);
      saveSelfVoiceGate(false, blocked);
      saveDuckBrowser(false, blocked);
      saveEndPause(2000, blocked);
      saveSensitivity(DEFAULT_SENSITIVITY, blocked);
      saveCardCommands(false, blocked);
    }).not.toThrow();
  });
});
