import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Root } from "react-dom/client";
import type { CustomPreset } from "../lib/presets";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root | undefined, uninstall: (() => void) | undefined;
beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  document.documentElement.removeAttribute("style");
  host = document.createElement("div");
  document.body.append(host);
});
afterEach(async () => {
  if (root) {
    const { act } = await import("react");
    await act(async () => root!.unmount());
  }
  root = undefined;
  uninstall?.();
  uninstall = undefined;
  host.remove();
  localStorage.clear();
  document.documentElement.removeAttribute("style");
  vi.restoreAllMocks();
});
const q = (id: string) => host.querySelector<HTMLButtonElement>(`[data-testid="appearance-${id}"]`);
async function setup() {
  const React = await import("react");
  const { createRoot } = await import("react-dom/client");
  const A = await import("../lib/appearance");
  const proof = vi.spyOn(A, "checkPreset");
  const P = await import("../lib/presets");
  const full = vi.spyOn(P, "checkCustomPreset");
  const { AuroraSetting } = await import("./AuroraSetting");
  return { ...React, A, P, proof, full, mount: async () => {
    root = createRoot(host);
    await React.act(async () => root!.render(React.createElement(AuroraSetting)));
  } };
}
function blend(i = 0): CustomPreset {
  return { id: `c-blend${i}`, name: `Blend ${i}`, accent: "#e8edf3", codeAccent: "#e8edf3",
    washes: ["#0090a1", "#606d42", "#7e7155"], base: "#00181b" };
}

it.each(["render", "storage", "presets event", "pressed state"])("cold %s does not prove the stored list or block for a second", async path => {
  const { act, A, P, proof, full, mount } = await setup();
  const list = Array.from({ length: 24 }, (_, i) => blend(i));
  // Stored appearance may need a proof/fallback of its own; the list never does.
  if (path === "pressed state") localStorage.setItem(A.APPEARANCE_KEY, JSON.stringify(list[0]));
  if (path === "render" || path === "pressed state") localStorage.setItem(P.CUSTOM_PRESETS_KEY, JSON.stringify(list));
  const start = performance.now();
  await mount();
  if (path === "storage" || path === "presets event") {
    await act(async () => {
      localStorage.setItem(P.CUSTOM_PRESETS_KEY, JSON.stringify(list));
      window.dispatchEvent(path === "storage" ? new StorageEvent("storage", { key: P.CUSTOM_PRESETS_KEY }) : new Event(P.PRESETS_EVENT));
    });
  }
  const elapsed = performance.now() - start;
  expect(full).not.toHaveBeenCalled();
  expect(proof).not.toHaveBeenCalled();
  expect(host.querySelectorAll(".appearance-custom")).toHaveLength(24);
  for (const p of list) expect(q(`custom-${p.id}`)?.getAttribute("aria-pressed")).toBe("false");
  expect(elapsed, `${path} blocked for ${Math.round(elapsed)} ms`).toBeLessThan(1000);
});

it.each(["render", "storage", "presets event"])("bounds cold panel %s with a quota-sized prefix of per-colour refusals", async path => {
  const { act, A, P, full, mount } = await setup();
  const good: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-good", name: "Good" };
  const raw = JSON.stringify([...Array(20_000).fill({ ...good, base: "#00ffff" }), good]);
  expect(raw.length * 2).toBeLessThan(5 * 1024 * 1024);
  if (path === "render") localStorage.setItem(P.CUSTOM_PRESETS_KEY, raw);
  else await mount();
  const start = performance.now();
  if (path === "render") await mount();
  else await act(async () => {
    localStorage.setItem(P.CUSTOM_PRESETS_KEY, raw);
    window.dispatchEvent(path === "storage" ? new StorageEvent("storage", { key: P.CUSTOM_PRESETS_KEY }) : new Event(P.PRESETS_EVENT));
  });
  const elapsed = performance.now() - start;
  expect(full).not.toHaveBeenCalled();
  expect(q("custom-c-good")).not.toBeNull();
  expect(elapsed, `panel ${path}: blocked for ${Math.round(elapsed)} ms`).toBeLessThan(1000);
}, 30_000);

it("cold click after quick-only load refuses a blend without applying it or marking it pressed", async () => {
  const { act, A, P, proof, full, mount } = await setup();
  localStorage.setItem(P.CUSTOM_PRESETS_KEY, JSON.stringify([blend()]));
  const { installAppearance } = await import("../lib/appearanceApply");
  uninstall = installAppearance();
  await mount();
  expect(full).not.toHaveBeenCalled();
  const before = { appearance: localStorage.getItem(A.APPEARANCE_KEY), css: document.documentElement.getAttribute("style") };
  await act(async () => q("custom-c-blend0")!.click());
  expect(full).toHaveBeenCalledTimes(1);
  expect(proof).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem(A.APPEARANCE_KEY)).toBe(before.appearance);
  expect(document.documentElement.getAttribute("style")).toBe(before.css);
  expect(q("custom-note")?.textContent).toMatch(/./);
  expect(q("custom-c-blend0")!.getAttribute("aria-pressed")).toBe("false");
});

it("unsafe formats and quick refusals never become swatches or CSS while uppercase safe colours apply normalized", async () => {
  const { act, A, P, mount } = await setup();
  const base: CustomPreset = { ...A.DEFAULT_COLOURS, id: "c-safe", name: "Safe", accent: "#E8EDF3", codeAccent: "#C4A8FF" };
  const bad: CustomPreset[] = [];
  const values = ["url(x)", "#aabbcc;--evil:red", "#abcdef\n", "#abc", "#ffffff", A.AMBER];
  for (const value of values) {
    for (const slot of ["accent", "base", "codeAccent", "wash0", "wash1", "wash2"]) {
      // White accents are safe; white backgrounds/washes require clamping and must be refused as presets.
      if (value === "#ffffff" && (slot === "accent" || slot === "codeAccent")) continue;
      const p = { ...base, id: `c-b${bad.length.toString(36)}`, name: `Bad ${bad.length}`, washes: [...base.washes] as [string, string, string] };
      if (slot.startsWith("wash")) p.washes[Number(slot.slice(4))] = value;
      else p[slot as "accent" | "base" | "codeAccent"] = value;
      bad.push(p);
    }
  }
  localStorage.setItem(P.CUSTOM_PRESETS_KEY, JSON.stringify([...bad, base]));
  const { installAppearance } = await import("../lib/appearanceApply");
  uninstall = installAppearance();
  await mount();
  expect(host.querySelectorAll(".appearance-custom")).toHaveLength(1);
  const swatchValues = Array.from(q("custom-c-safe")!.querySelectorAll<HTMLElement>("[style]")).map(el => el.style.background);
  expect(swatchValues).toHaveLength(5);
  await act(async () => q("custom-c-safe")!.click());
  expect(q("custom-c-safe")!.getAttribute("aria-pressed")).toBe("true");
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#e8edf3");
  expect(document.documentElement.style.getPropertyValue("--code-accent")).toBe("#c4a8ff");
  expect(document.documentElement.style.getPropertyValue("--evil")).toBe("");
  for (const key of ["--accent", "--code-accent", "--wash-1", "--wash-2", "--wash-3", "--base", "--bg-0"]) {
    const value = document.documentElement.style.getPropertyValue(key);
    if (value) expect(value).toMatch(/^#[0-9a-f]{6}$/);
  }
});
