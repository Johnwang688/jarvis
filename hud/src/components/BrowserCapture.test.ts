// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { BrowserCapture } from "./BrowserCapture";
import { CAPTURE_MIME, CAPTURE_NO_CHAT, captureAttachments, type CaptureIdentity, type CaptureStill } from "../lib/pageCapture";
vi.mock("../api", () => ({ api: { captureBrowser: vi.fn() } }));
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
    identity: () => identity, destination: { target: () => key, stage }, onRemote: remote, ...overrides,
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
        destination: { target: () => key, stage }, onRemote: remote }))));
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
    expect(stage).toHaveBeenCalledExactlyOnceWith("thread-original", pair);
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
    expect(stage).toHaveBeenCalledExactlyOnceWith("thread-original", pair);
  });
  it("Annotate fetches a still without staging and pins the chat only at its later Send gesture", async () => {
    await show(); await click("Annotate");
    expect(captureAttachments).not.toHaveBeenCalled(); expect(stage).not.toHaveBeenCalled();
    key = "later-selected"; await click("Stage still");
    expect(stage).toHaveBeenCalledExactlyOnceWith("later-selected", pair);
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
