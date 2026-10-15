// The real wiring: one lease token per window, and one switch for every window (`storage`).
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("createSpeech's duck wiring", () => {
  it("a window token is 32 lowercase hex characters and differs per window", async () => {
    const { newWindowToken } = await import("./index");
    const a = newWindowToken(), b = newWindowToken();
    expect(a).toMatch(/^[0-9a-f]{32}$/);
    expect(a).not.toBe(b);
  });

  it("requests carry this window's token, and another window's switch change applies here", async () => {
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      if (String(url) === "/speech/duck") {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ on: true, ducked: 0, lease_s: 3 }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({ threads: {}, pool: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const { createSpeech, saveDuckBrowser } = await import("./index");
    const one = createSpeech(), two = createSpeech();
    try {
      expect(one.duck.isEnabled()).toBe(true);
      saveDuckBrowser(false);
      window.dispatchEvent(new StorageEvent("storage", { key: "jarvis.duckBrowser", newValue: "off" }));
      expect(one.duck.isEnabled()).toBe(false);
      expect(two.duck.isEnabled()).toBe(false);
      saveDuckBrowser(true);
      window.dispatchEvent(new StorageEvent("storage", { key: "jarvis.duckBrowser", newValue: "on" }));
      expect(one.duck.isEnabled() && two.duck.isEnabled()).toBe(true);
      one.duck.setSpeaking(true);
      two.duck.setSpeaking(true);
      for (let i = 0; i < 20 && bodies.length < 2; i++) await new Promise((r) => setTimeout(r, 0));
      const tokens = bodies.map((b) => b.window);
      expect(new Set(tokens).size).toBe(2);
      expect(bodies.every((b) => b.on === true && /^[0-9a-f]{32}$/.test(String(b.window)) && Object.keys(b).sort().join() === "on,window")).toBe(true);
    } finally {
      one.dispose();
      two.dispose();
    }
  });
});
