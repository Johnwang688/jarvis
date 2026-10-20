// @vitest-environment jsdom
// B-DIRECT-CAPTURE: the owner's switch and probe, with fakes. Not M6 acceptance.
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserApi } from "../api";
import { browserSettings } from "../state/browserSettings";
import { DIRECT_UNTESTED, DirectCaptureSetting } from "./DirectCaptureSetting";
import { PATTERN, PROBE_APP_ONLY, SAMPLE_POINTS } from "../lib/directProbe";
import { fakeEnv, place } from "../lib/directCaptureFake";

vi.mock("../api", () => ({ browserApi: { get: vi.fn(), write: vi.fn() } }));
let host: HTMLDivElement, root: Root, settings: Record<string, unknown>, probe: unknown, posted: unknown[];
beforeEach(async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  settings = { agents_may_drive: true, direct_capture: false }; probe = null; posted = [];
  vi.mocked(browserApi.get).mockReset().mockImplementation(async (path: string) =>
    (path === "/settings" ? settings : { probe }) as never);
  vi.mocked(browserApi.write).mockReset().mockImplementation(async (path: string, body: unknown) => {
    if (path === "/direct-capture/probe") {
      posted.push(body);
      const facts = body as { prompt: string; samples: { got: number[]; want: number[] }[] };
      const ok = facts.prompt === "accepted" && facts.samples.length === 9
        && facts.samples.every(s => s.want.every((w, i) => Math.abs(w - s.got[i]) <= 24));
      probe = { passed: ok, reason: ok ? "" : facts.prompt === "cancelled" ? "cancelled" : "wrong_pixels", at: "2026-10-19T00:00:00Z" };
      return { ...(probe as object), saved: true } as never;
    }
    const b = body as { direct_capture?: boolean };
    if (b.direct_capture === true && !(probe as { passed?: boolean } | null)?.passed)
      throw new Error("run Test Direct capture in Settings first: it has not passed since Jarvis started");
    settings = { ...settings, ...b };
    return settings as never;
  });
  await browserSettings.refresh();
  (document as unknown as { elementFromPoint: (x: number, y: number) => Element | null }).elementFromPoint =
    () => host.querySelector('[data-testid="direct-probe-pattern"]');
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
const settle = () => act(async () => { for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 10)); });
async function render(props: Record<string, unknown>) {
  await act(async () => root.render(React.createElement(DirectCaptureSetting, { blocked: false, ...props } as never)));
  await settle();
}
const box = () => host.querySelector<HTMLInputElement>('[data-testid="browser-direct-capture"]')!;
const testButton = () => host.querySelector<HTMLButtonElement>('[data-testid="browser-direct-probe"]')!;
/** Lay the pattern frame out once React draws it, and fire its load. */
async function pressProbe() {
  await act(async () => testButton().click());
  const frame = host.querySelector<HTMLIFrameElement>('[data-testid="direct-probe-pattern"]');
  if (frame) { place(frame, { left: 10, top: 10, width: 320, height: 200 }); frame.dispatchEvent(new Event("load")); }
  await settle();
}
const pattern = SAMPLE_POINTS.map(([, , c]) => [...c, 255]);

describe("Direct capture switch and owner probe", () => {
  it("is off by default and cannot be turned on before a passing probe", async () => {
    await render({ env: fakeEnv().env, match: () => true });
    expect(box().checked).toBe(false); expect(box().disabled).toBe(true);
    expect(host.textContent).toContain(DIRECT_UNTESTED);
    expect(host.textContent).toContain("no credential check");
  });
  it("refuses to run outside the installed app, with no prompt and no report", async () => {
    const f = fakeEnv({ pixels: pattern });
    await render({ env: f.env, match: (q: string) => q.includes("browser") });
    await pressProbe();
    expect(host.textContent).toContain(PROBE_APP_ONLY);
    expect(f.calls).toEqual([]); expect(posted).toEqual([]);
  });
  it("an untrusted press does nothing", async () => {
    const f = fakeEnv({ trusted: false, pixels: pattern });
    await render({ env: f.env, match: () => true });
    await pressProbe();
    expect(f.calls).toEqual([]); expect(posted).toEqual([]);
  });
  it("a passing probe posts facts, never an image, stops its tracks, and unlocks the switch", async () => {
    const f = fakeEnv({ pixels: pattern, dpr: 1.5, frame: { width: 480, height: 300 } });
    await render({ env: f.env, match: (q: string) => q === "(display-mode: standalone)" });
    await pressProbe();
    expect(posted).toHaveLength(1);
    const facts = posted[0] as Record<string, unknown>;
    expect(facts).toMatchObject({ display_mode: "standalone", prompt: "accepted", crop: { applied: true, error: "" },
      frame: { width: 480, height: 300 }, expected: { width: 480, height: 300 }, tracks_live_after: 0, dpr: 1.5 });
    expect((facts.samples as unknown[]).length).toBe(9);
    expect((facts.samples as { want: number[] }[])[0].want).toEqual([...PATTERN.tl]);
    expect(JSON.stringify(facts)).not.toMatch(/data_b64|base64/);
    expect(f.tracks.every(t => t.readyState === "ended")).toBe(true);
    expect(f.calls).not.toContain("encode");
    expect(host.textContent).toContain("Passed: Chrome shared this tab");
    expect(host.querySelector('[data-testid="direct-probe-pattern"]')).toBeNull();
    expect(box().disabled).toBe(false);
  });
  it("a cancelled probe records cancelled and leaves the switch locked", async () => {
    const f = fakeEnv({ prompt: "NotAllowedError" });
    await render({ env: f.env, match: () => true });
    await pressProbe();
    expect((posted[0] as { prompt: string }).prompt).toBe("cancelled");
    expect(host.textContent).toContain("Cancelled: the share prompt was dismissed");
    expect(box().disabled).toBe(true);
    expect(f.tracks).toEqual([]);
  });
  it("a card during the probe stops its tracks and posts nothing", async () => {
    const f = fakeEnv({ hold: true, pixels: pattern });
    await render({ env: f.env, match: () => true });
    await pressProbe();
    expect(f.calls).toContain("getDisplayMedia");
    await render({ env: f.env, match: () => true, blocked: true });
    f.release(); await settle();
    expect(f.tracks.every(t => t.readyState === "ended")).toBe(true);
    expect(posted).toEqual([]);
  });
});
