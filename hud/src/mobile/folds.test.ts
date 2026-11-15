import { afterEach, describe, expect, it, vi } from "vitest";
import { createProjectFolds, FOLDS_KEY } from "./folds";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("RD-5 project fold state", () => {
  it("opens active projects and folds idle projects without saving defaults", () => {
    const storage = { getItem: vi.fn(() => null), setItem: vi.fn() };
    const folds = createProjectFolds(storage);
    expect(folds.get("needs", true)).toBe(false);
    expect(folds.get("running", true)).toBe(false);
    expect(folds.get("idle")).toBe(true);
    expect(storage.setItem).not.toHaveBeenCalled();
    // An unchosen project's default follows refreshed activity.
    expect(folds.get("idle", true)).toBe(false);
  });

  it("persists independent owner choices across reload, overriding either activity default", () => {
    const folds = createProjectFolds();
    folds.set("active", true);
    folds.set("idle", false);
    expect(JSON.parse(localStorage.getItem(FOLDS_KEY)!)).toEqual({ active: true, idle: false });
    const reloaded = createProjectFolds();
    expect(reloaded.get("active", true)).toBe(true);
    expect(reloaded.get("idle", false)).toBe(false);
    expect(reloaded.get("new", true)).toBe(false);
  });

  it("notifies shared consumers after changes and lets them unsubscribe", () => {
    const folds = createProjectFolds(null);
    const one = vi.fn(() => expect(folds.get("p", true)).toBe(true));
    const two = vi.fn();
    const unsubscribe = folds.subscribe(one);
    folds.subscribe(two);
    folds.set("p", true);
    folds.set("p", true);
    expect(one).toHaveBeenCalledTimes(1);
    expect(two).toHaveBeenCalledTimes(1);
    unsubscribe();
    folds.set("p", false);
    expect(one).toHaveBeenCalledTimes(1);
    expect(two).toHaveBeenCalledTimes(2);
  });

  it.each(["{", "[]", "true", "null", '"text"', "42"])("ignores malformed storage %s", (raw) => {
    const folds = createProjectFolds({ getItem: () => raw, setItem: () => {} });
    expect(folds.get("p", true)).toBe(false);
    expect(folds.get("p", false)).toBe(true);
  });

  it("accepts only boolean choices, including ids that are object property names", () => {
    const folds = createProjectFolds({
      getItem: () => '{"p":true,"q":false,"bad":"false","__proto__":false,"constructor":true}',
      setItem: () => {},
    });
    expect(folds.get("p", true)).toBe(true);
    expect(folds.get("q")).toBe(false);
    expect(folds.get("bad", true)).toBe(false);
    expect(folds.get("__proto__")).toBe(false);
    expect(folds.get("constructor", true)).toBe(true);
  });

  it("keeps choices and subscriptions working when storage reads and writes throw", () => {
    const folds = createProjectFolds({
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("quota"); },
    });
    const changed = vi.fn();
    folds.subscribe(changed);
    expect(folds.get("p", true)).toBe(false);
    expect(() => folds.set("p", true)).not.toThrow();
    expect(folds.get("p", true)).toBe(true);
    expect(changed).toHaveBeenCalledOnce();
  });

  it("works when accessing window.localStorage itself throws", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => { throw new Error("blocked"); });
    const folds = createProjectFolds();
    folds.set("p", false);
    expect(folds.get("p")).toBe(false);
  });

  it("works with no storage", () => {
    const folds = createProjectFolds(null);
    expect(folds.get("p")).toBe(true);
    folds.set("p", false);
    expect(folds.get("p")).toBe(false);
    expect(createProjectFolds(null).get("p")).toBe(true);
  });
});
