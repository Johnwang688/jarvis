// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { BrowserCapture } from "./BrowserCapture";
import { CAPTURE_MIME, CAPTURE_NO_CHAT, captureAttachments, type CaptureIdentity, type CaptureStill } from "../lib/pageCapture";
import { fakeEnv, place } from "../lib/directCaptureFake";
vi.mock("../api", () => ({ api: { captureBrowser: vi.fn(), directCapture: vi.fn() } }));
vi.mock("../lib/pageCapture", async original => ({
  ...await original<typeof import("../lib/pageCapture")>(), captureAttachments: vi.fn(),
}));
vi.mock("./Annotate", () => ({
  Annotate: (p: { notice: string; onStage: (ops: never[]) => Promise<boolean>; onClose: () => void }) =>
    React.createElement("section", { "data-testid": "frozen-still" }, p.notice,
      React.createElement("button", { onClick: async () => { if (await p.onStage([])) p.onClose(); } }, "Stage still")),
}));
const still: CaptureStill = { capture_id: "a".repeat(32), image: { mime: "image/png", data_b64: "aGk=", width: 800, height: 600 },
  url: "http://localhost:23456/", title: "Fixture", text_chars: 3, whole: false, notes: [] };
const pair = [{ name: "page.png", mime: "image/png", data_b64: "aGk=" },
  { name: "page text", mime: CAPTURE_MIME, data_b64: still.capture_id }];
let host: HTMLDivElement, root: Root, key: string | null, identity: CaptureIdentity | null;
const stage = vi.fn(() => true), remote = vi.fn();
async function show(overrides: Record<string, unknown> = {}) {
  await act(async () => root.render(React.createElement(BrowserCapture, {
    remoteTabId: "abcdef01", active: true, drawn: true, blocked: false, zoom: 100,
    identity: () => identity, destination: { target: () => key ? { key } : null, stage }, onRemote: remote, ...overrides,
  })));
}
async function click(label: string) {
  await act(async () => Array.from(host.querySelectorAll("button")).find(b => b.textContent === label)!.click());
}
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  key = "thread-original";
  identity = { window: "a".repeat(32), pane_id: 5, local_tab_id: "aaaaaa", view_id: "abcdef01" };
  vi.mocked(api.captureBrowser).mockReset().mockResolvedValue(still);
  vi.mocked(captureAttachments).mockReset().mockResolvedValue(pair);
  stage.mockReset().mockReturnValue(true); remote.mockReset();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
describe("capture gesture lifecycle", () => {
  it("annotation blocks the live Remote input surface until closed", async () => {
    const annotating = vi.fn();
    await show({ onAnnotating: annotating }); expect(annotating).toHaveBeenLastCalledWith(false);
    await click("Annotate"); expect(annotating).toHaveBeenLastCalledWith(true);
    await click("Stage still"); expect(annotating).toHaveBeenLastCalledWith(false);
  });
  it("StrictMode setup replay keeps genuine owner gestures usable", async () => {
    await act(async () => root.render(React.createElement(React.StrictMode, null,
      React.createElement(BrowserCapture, { remoteTabId: "abcdef01", active: true, drawn: true,
        blocked: false, zoom: 100, identity: () => identity,
        destination: { target: () => key ? { key } : null, stage }, onRemote: remote }))));
    await click("Annotate");
    expect(api.captureBrowser).toHaveBeenCalledTimes(1);
    expect(host.querySelector('[data-testid="frozen-still"]')).not.toBeNull();
  });
  it("Direct fallback never captures or converts without an explicit second owner gesture", async () => {
    await show({ remoteTabId: null }); await click("Annotate");
    expect(host.textContent).toContain("Open this in Remote to annotate.");
    expect(api.captureBrowser).not.toHaveBeenCalled(); expect(remote).not.toHaveBeenCalled();
    await click("Open in Remote"); expect(remote).toHaveBeenCalledTimes(1);
  });
  it("Send refuses without a selected drawn chat before any request", async () => {
    key = null; await show(); await click("Send to chat");
    expect(host.textContent).toContain(CAPTURE_NO_CHAT); expect(api.captureBrowser).not.toHaveBeenCalled();
  });
  it("pins the selected draft before capture and export, while late selection changes do not retarget", async () => {
    const capture = deferred<unknown>(), image = deferred<typeof pair>();
    vi.mocked(api.captureBrowser).mockReturnValue(capture.promise);
    vi.mocked(captureAttachments).mockReturnValue(image.promise);
    await show(); await click("Send to chat");
    key = "other-thread"; await act(async () => capture.resolve(still));
    key = "another-thread"; await act(async () => image.resolve(pair));
    expect(stage).toHaveBeenCalledExactlyOnceWith({ key: "thread-original" }, pair);
    expect(host.querySelector('[data-testid="frozen-still"]')).toBeNull();
  });
  it("a card which appears and disappears cannot revive an old response", async () => {
    const capture = deferred<unknown>(); vi.mocked(api.captureBrowser).mockReturnValue(capture.promise);
    await show(); await click("Send to chat"); await show({ blocked: true }); await show();
    await act(async () => capture.resolve(still));
    expect(captureAttachments).not.toHaveBeenCalled(); expect(stage).not.toHaveBeenCalled();
    expect(host.querySelector('[data-testid="frozen-still"]')).toBeNull();
  });
  it("a hidden view or changed receipt discards pending pixels", async () => {
    for (const hidden of [true, false]) {
      const capture = deferred<unknown>(); vi.mocked(api.captureBrowser).mockReturnValue(capture.promise);
      await show(); await click("Annotate");
      if (hidden) await show({ drawn: false });
      else identity = { ...identity!, view_id: "11111111" };
      await act(async () => capture.resolve(still));
      expect(host.querySelector('[data-testid="frozen-still"]')).toBeNull();
      await show();
    }
  });
  it("coverage during export never stages; capacity refusal keeps the still and ink", async () => {
    const image = deferred<typeof pair>(); vi.mocked(captureAttachments).mockReturnValue(image.promise);
    await show(); await click("Send to chat"); await show({ blocked: true });
    await act(async () => image.resolve(pair)); expect(stage).not.toHaveBeenCalled();
    await show(); vi.mocked(captureAttachments).mockResolvedValue(pair); stage.mockReturnValue(false);
    await click("Stage still");
    expect(host.querySelector('[data-testid="frozen-still"]')?.textContent).toContain("two attachment slots");
    expect(stage).toHaveBeenCalledExactlyOnceWith({ key: "thread-original" }, pair);
  });
  it("Annotate fetches a still without staging and pins the chat only at its later Send gesture", async () => {
    await show(); await click("Annotate");
    expect(captureAttachments).not.toHaveBeenCalled(); expect(stage).not.toHaveBeenCalled();
    key = "later-selected"; await click("Stage still");
    expect(stage).toHaveBeenCalledExactlyOnceWith({ key: "later-selected" }, pair);
  });
  it("duplicate clicks do not start two captures, and whole text is selected before capture", async () => {
    const capture = deferred<unknown>(); vi.mocked(api.captureBrowser).mockReturnValue(capture.promise);
    await show();
    await act(async () => host.querySelector("input")!.click());
    await click("Annotate"); await click("Annotate");
    expect(api.captureBrowser).toHaveBeenCalledExactlyOnceWith("abcdef01", identity, true);
    await act(async () => capture.resolve({ ...still, whole: true }));
    expect(host.querySelector("input")!.disabled).toBe(true);
  });
});

it("pins the original destination before the uncover hook and waits before capture", async () => {
  const ready = deferred<void>();
  await show({ beforeCapture: () => ready.promise });
  await click("Send to chat");
  expect(api.captureBrowser).not.toHaveBeenCalled();
  key = "later-chat";
  await act(async () => ready.resolve());
  expect(stage).toHaveBeenCalledExactlyOnceWith({ key: "thread-original" }, pair);
});
it("a card or hidden tab during the uncover hook cannot revive the capture or destination pin", async () => {
  for (const changed of [{ blocked: true }, { drawn: false }, { active: false }, { remoteTabId: "changed1" }]) {
    const ready = deferred<void>(), release = vi.fn();
    const destination = { target: () => ({ key: "pinned-before-wait" }), stage, release };
    await show({ beforeCapture: () => ready.promise, destination });
    await click("Send to chat");
    await show({ ...changed, beforeCapture: () => ready.promise, destination });
    await show({ beforeCapture: () => ready.promise, destination });
    await act(async () => ready.resolve());
    expect(api.captureBrowser).not.toHaveBeenCalled();
    expect(stage).not.toHaveBeenCalled();
    expect(release).toHaveBeenCalledExactlyOnceWith({ key: "pinned-before-wait" });
  }
});
it("portal controls can be hidden without unmounting the controller or captured still", async () => {
  const panel = document.createElement("div"); document.body.append(panel);
  await show({ controlsTarget: panel });
  expect(host.querySelector('[data-testid="browser-capture-actions"]')).toBeNull();
  await act(async () => [...panel.querySelectorAll("button")].find(b=>b.textContent==="Annotate")!.click());
  expect(host.querySelector('[data-testid="frozen-still"]')).not.toBeNull();
  panel.hidden = true;
  await show({ controlsTarget: panel });
  expect(host.querySelector('[data-testid="frozen-still"]')).not.toBeNull();
  panel.remove();
});

describe("Direct capture behind the owner's switch (B-DIRECT-CAPTURE)", () => {
  let frame: HTMLIFrameElement, shield: HTMLDivElement, framehost: HTMLDivElement, on: boolean;
  const ref = { capture_id: "b".repeat(32), url: "http://localhost:23456/", title: "", text_chars: 0, whole: false,
    notes: ["Direct capture: only the address is attached."] };
  beforeEach(() => {
    framehost = document.createElement("div"); framehost.className = "framehost";
    frame = document.createElement("iframe"); shield = document.createElement("div"); shield.className = "frameshield";
    framehost.append(frame, shield); document.body.append(framehost);
    place(frame, { left: 100, top: 100, width: 400, height: 250 });
    (document as unknown as { elementFromPoint: () => Element }).elementFromPoint = () => shield;
    on = true;
    vi.mocked(api.directCapture).mockReset().mockResolvedValue(ref);
  });
  afterEach(() => framehost.remove());
  const direct = (fake: ReturnType<typeof fakeEnv>) => ({ remoteTabId: null,
    direct: { enabled: () => on, frame: () => frame, env: fake.env } });
  const ended = (f: ReturnType<typeof fakeEnv>) => f.tracks.length > 0 && f.tracks.every(t => t.readyState === "ended");
  const settle = () => act(async () => { for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 10)); });

  it("off: the documented fallback, no prompt and no reference", async () => {
    on = false; const f = fakeEnv();
    await show(direct(f)); await click("Annotate"); await settle();
    expect(host.textContent).toContain("Open this in Remote to annotate.");
    expect(f.calls).toEqual([]); expect(api.directCapture).not.toHaveBeenCalled();
  });
  it("on: a trusted Annotate freezes one cropped frame and asks the daemon for the URL-only reference after it", async () => {
    const f = fakeEnv();
    await show(direct(f)); await click("Annotate"); await settle();
    expect(host.querySelector('[data-testid="frozen-still"]')).not.toBeNull();
    expect(ended(f)).toBe(true);
    expect(api.directCapture).toHaveBeenCalledExactlyOnceWith(identity);
    expect(api.captureBrowser).not.toHaveBeenCalled();
    await click("Stage still");
    expect(stage).toHaveBeenCalledExactlyOnceWith({ key: "thread-original" }, pair);
    expect(vi.mocked(captureAttachments).mock.calls[0][0]).toMatchObject({ capture_id: ref.capture_id,
      image: { mime: "image/png", width: 800, height: 500 }, url: ref.url, text_chars: 0 });
  });
  it("on: Send to chat stages the pair into the chat pinned at the press", async () => {
    const f = fakeEnv();
    await show(direct(f)); await click("Send to chat"); key = "later"; await settle();
    expect(stage).toHaveBeenCalledExactlyOnceWith({ key: "thread-original" }, pair);
    expect(ended(f)).toBe(true);
  });
  it("an untrusted press never prompts", async () => {
    const f = fakeEnv({ trusted: false });
    await show(direct(f)); await click("Annotate"); await settle();
    expect(f.calls).toEqual([]);
    expect(host.textContent).toContain("Press Annotate or Send to chat yourself");
    expect(host.querySelector(".capture-remote")).toBeNull();
  });
  it("a cancelled prompt or a failed pixel check offers Remote and leaves no reference", async () => {
    for (const [o, text] of [[{ prompt: "NotAllowedError" }, "share prompt was cancelled"],
      [{ frame: { width: 10, height: 10 } }, "did not match this page's size"],
      [{ sample: null }, "was blank"]] as const) {
      const f = fakeEnv(o);
      await show(direct(f)); await click("Annotate"); await settle();
      expect(host.textContent).toContain(text);
      expect(host.querySelector(".capture-remote")).not.toBeNull();
      expect(api.directCapture).not.toHaveBeenCalled();
      expect(host.querySelector('[data-testid="frozen-still"]')).toBeNull();
      expect(f.tracks.every(t => t.readyState === "ended")).toBe(true);
      await act(async () => root.unmount()); root = createRoot(host);
    }
  });
  it("a card, a hidden pane or unmount during the prompt stops the tracks and keeps nothing", async () => {
    for (const change of [{ blocked: true }, { drawn: false }, { active: false }, "unmount"] as const) {
      const f = fakeEnv({ hold: true });
      await show(direct(f)); await click("Send to chat"); await settle();
      expect(f.calls).toContain("getDisplayMedia");
      if (change === "unmount") await act(async () => root.unmount());
      else await show({ ...direct(f), ...change });
      f.release(); await settle();
      expect(ended(f)).toBe(true);
      expect(api.directCapture).not.toHaveBeenCalled(); expect(stage).not.toHaveBeenCalled();
      expect(host.querySelector('[data-testid="frozen-still"]')).toBeNull();
      root = createRoot(host); if (change !== "unmount") await act(async () => root.unmount()), root = createRoot(host);
    }
  });
  it("the daemon refusing the reference keeps no still", async () => {
    const f = fakeEnv();
    vi.mocked(api.directCapture).mockRejectedValue(new Error("Direct capture is off: turn it on in Settings, Browser"));
    await show(direct(f)); await click("Annotate"); await settle();
    expect(host.textContent).toContain("Direct capture is off");
    expect(host.querySelector('[data-testid="frozen-still"]')).toBeNull();
    expect(ended(f)).toBe(true);
  });
  it("the capture never moves focus", async () => {
    const f = fakeEnv(); let focused = 0; frame.focus = () => { focused++; };
    await show(direct(f));
    const button = Array.from(host.querySelectorAll("button")).find(b => b.textContent === "Annotate")!;
    button.focus(); await click("Annotate"); await settle();
    expect(focused).toBe(0); expect(document.activeElement).toBe(button);
  });
});
