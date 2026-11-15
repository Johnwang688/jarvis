import { describe, expect, it, vi } from "vitest";
import { brandButton, orbState, renderOrb, type OrbSignals } from "./orb";
import * as icons from "./icons";
import css from "./mobile.css?raw";

const base: OrbSignals = { signedOut: false, online: true, failStatus: null, streamOpen: true,
  everOpened: true, firstOpenTimedOut: false, secure: { ok: true }, working: 0, lastRead: 0, now: 180_000 };
const cases: [string, Partial<OrbSignals>, string, string, string][] = [
  ["signed out", { signedOut: true }, "error", "err", "Signed out"],
  ...[502, 503, 504].map((failStatus): [string, Partial<OrbSignals>, string, string, string] =>
    [`proxy ${failStatus}`, { online: false, failStatus }, "error", "err", "Jarvis unreachable"]),
  ["limited", { secure: { ok: false } }, "error", "err", "Limited connection"],
  ["offline", { online: false, failStatus: 0 }, "offline", "wait", "Offline · read 3 min ago"],
  ["a limited phone in airplane mode", { online: false, failStatus: 0, secure: { ok: false } }, "offline", "wait", "Offline · read 3 min ago"],
  ["a limited phone whose Jarvis is down", { online: false, failStatus: 502, secure: { ok: false } }, "error", "err", "Jarvis unreachable"],
  ["limited while reconnecting", { streamOpen: false, secure: { ok: false } }, "error", "err", "Limited connection"],
  ["offline before a read", { online: false, lastRead: null }, "offline", "wait", "Offline · not read yet"],
  ["reconnecting", { streamOpen: false }, "reconnecting", "wait", "Reconnecting…"],
  ["connecting", { streamOpen: false, everOpened: false }, "connecting", "ok", "Connecting…"],
  ["first open timed out", { streamOpen: false, everOpened: false, firstOpenTimedOut: true }, "reconnecting", "wait", "Reconnecting…"],
  ["offline after first-open timeout", { online: false, everOpened: false, firstOpenTimedOut: true }, "offline", "wait", "Offline · read 3 min ago"],
  ["working", { working: 2 }, "working", "ok", "Live · 2 working"],
  ["live", {}, "live", "ok", "Live"],
];

describe("phone orb state table", () => {
  it.each(cases)("%s always carries a visible state word and an accessible name", (_name, signals, state, tone, word) => {
    const input = Object.freeze({ ...base, ...signals });
    const result = orbState(input);
    expect(result).toEqual({ state, tone, word });
    expect(orbState(input)).toEqual(result); // no clock read, mutation or retained state
    const brand = brandButton();
    renderOrb(brand, result);
    const conn = brand.querySelector<HTMLElement>('[data-testid="m-conn"]')!;
    expect(conn.textContent).toBe(word);
    expect(conn.hidden).toBe(false);
    expect(conn.dataset.state).toBe(state);
    expect(conn.dataset.tone).toBe(tone);
    expect(brand.getAttribute("aria-label")).toBe(`Jarvis, ${word}. Open navigation`);
    expect(brand.querySelector(".m-orb")?.getAttribute("data-state")).toBe(state);
    expect(brand.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(brand.querySelectorAll("svg path")).toHaveLength(3);
    expect(brand.querySelector(".m-o-outer")?.getAttribute("d")?.match(/M/g)).toHaveLength(24);
  });

  it("uses the required precedence even when every lower-priority signal disagrees", () => {
    const signals = { ...base, signedOut: true, online: false, failStatus: 503, secure: { ok: false }, streamOpen: false, working: 4 };
    expect(orbState(signals).word).toBe("Signed out");
    signals.signedOut = false;
    expect(orbState(signals).word).toBe("Jarvis unreachable");
    signals.failStatus = 0;
    expect(orbState(signals).state).toBe("offline");
    signals.online = true;
    expect(orbState(signals).word).toBe("Limited connection");
    signals.secure.ok = true;
    expect(orbState(signals).state).toBe("reconnecting");
    signals.streamOpen = true;
    expect(orbState(signals).state).toBe("working");
    signals.working = 0;
    expect(orbState(signals).state).toBe("live");
  });

  it("an old proxy failure does not colour a recovered connection red", () => {
    expect(orbState({ ...base, failStatus: 503 }).state).toBe("live");
  });

  it("has a 48px brand target and calls the navigation hook", () => {
    const onBrand = vi.fn();
    const brand = brandButton(onBrand);
    renderOrb(brand, orbState(base));
    brand.click();
    expect(onBrand).toHaveBeenCalledOnce();
    expect(brand.type).toBe("button");
    expect(css).toMatch(/\.tap\.m-brand \{[^}]*min-height: var\(--m-tap\)/);
    expect(css).toMatch(/--m-tap: 3rem/);
  });

  it("turns while working, pulses while reconnecting, has a hollow offline core, and disables all motion when reduced", () => {
    expect(css).toMatch(/\.m-orb\[data-state="working"\] \.m-o-outer[^}]*animation: m-o-spin/);
    expect(css).toMatch(/\.m-orb\[data-state="reconnecting"\]::before[^}]*animation: m-o-pulse/);
    expect(css).toMatch(/\.m-orb\[data-state="offline"\] \.m-o-core \{ fill: none; stroke:/);
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\) \{ \.m-orb \*, \.m-orb::before \{ animation: none !important;/);
  });
});

describe("phone outline icons", () => {
  it.each(Object.entries(icons).filter(([name]) => name.endsWith("Icon")) as [string, () => SVGSVGElement][])("%s creates a fresh, decorative SVG in the SVG namespace", (_name, build) => {
    const svg = build();
    expect(svg.namespaceURI).toBe("http://www.w3.org/2000/svg");
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    expect(svg.getAttribute("focusable")).toBe("false");
    expect(svg.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(svg.getAttribute("stroke-width")).toBe("1.75");
    expect(svg.querySelectorAll("path").length).toBeGreaterThan(0);
    expect(svg.querySelectorAll("script, image, foreignObject, [onload], [onclick]")).toHaveLength(0);
    expect(build()).not.toBe(svg);
    expect(icons.CODE_SVG.has(svg)).toBe(true);
  });
  it("provides every requested icon", () => {
    expect(Object.keys(icons).filter((k) => k.endsWith("Icon")).sort()).toEqual(["inboxIcon", "chatsIcon", "settingsIcon", "lockIcon", "unlockIcon",
      "chevronIcon", "backIcon", "moreIcon", "micIcon", "sendIcon", "copyIcon", "terminalIcon", "sunIcon"].sort());
  });
});
