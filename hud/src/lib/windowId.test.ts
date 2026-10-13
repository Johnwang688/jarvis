import { describe, expect, it, vi } from "vitest";
import { WINDOW_ID, isWindowId, pageNonce } from "./windowId";

describe("one live page identity", () => {
  it("matches the daemon's 128-bit lowercase-hex contract and each new page gets another", () => {
    expect(isWindowId(WINDOW_ID)).toBe(true);
    expect(isWindowId(pageNonce())).toBe(true);
    expect(pageNonce()).not.toBe(WINDOW_ID);
    for (const bad of ["a".repeat(16), "A".repeat(32), "a".repeat(33), "", null, 123]) {
      expect(isWindowId(bad)).toBe(false);
    }
  });

  it("fails closed when secure random bytes are unavailable", () => {
    const random = vi.spyOn(crypto, "getRandomValues").mockImplementationOnce(() => {
      throw new Error("unavailable");
    });
    try {
      expect(pageNonce()).toBe("");
      expect(isWindowId(pageNonce())).toBe(true);
      expect(isWindowId(WINDOW_ID)).toBe(true);
    } finally { random.mockRestore(); }
  });
});
