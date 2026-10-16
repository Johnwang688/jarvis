import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { SparkleSetting } from "./SparkleSetting";
import { GeneralSettings } from "./Pickers";
import { SPARKLE_KEY } from "../lib/sparkle";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => { try { localStorage.removeItem(SPARKLE_KEY); } catch { /* none */ } });

describe("Settings -> General: click sparkle", () => {
  it("is a checkbox, on by default, that flips and remembers the preference", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      act(() => root.render(createElement(SparkleSetting)));
      const box = container.querySelector('[data-testid="sparkle-toggle"]') as HTMLInputElement;
      expect(box.type).toBe("checkbox");
      expect(box.checked).toBe(true);
      act(() => box.click());
      expect(box.checked).toBe(false);
      expect(localStorage.getItem(SPARKLE_KEY)).toBe("0");
      act(() => box.click());
      expect(box.checked).toBe(true);
      expect(localStorage.getItem(SPARKLE_KEY)).toBe("1");
    } finally { act(() => root.unmount()); }
  });
  it("starts unchecked when the stored choice is off", () => {
    localStorage.setItem(SPARKLE_KEY, "0");
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      act(() => root.render(createElement(SparkleSetting)));
      expect((container.querySelector('[data-testid="sparkle-toggle"]') as HTMLInputElement).checked).toBe(false);
    } finally { act(() => root.unmount()); }
  });
  it("sits in the General section beside the install row", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      act(() => root.render(createElement(GeneralSettings, { blocked: false, installStatus: "unavailable", onInstall: async () => {} })));
      expect(container.querySelector('[data-testid="install-app"]')).not.toBeNull();
      expect(container.querySelector('[data-testid="sparkle-toggle"]')).not.toBeNull();
    } finally { act(() => root.unmount()); }
  });
});
