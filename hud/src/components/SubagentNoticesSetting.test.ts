import { act, createElement } from "react";
import { describe, expect, it } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { SubagentNoticesSetting } from "./ReadAloud";
import { loadSubagentNotices, saveSubagentNotices } from "../lib/speech";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

describe("Settings -> Voice: sub-agent notices (SV-5)", () => {
  it("is a labelled switch that reports the owner's choice", async () => {
    const calls: boolean[] = [];
    await act(async () => root.render(createElement(SubagentNoticesSetting, { on: true, onChange: (on: boolean) => calls.push(on) })));
    const box = host.querySelector('[data-testid="subagent-notices-toggle"]') as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(host.textContent).toContain("Say when a sub-agent finishes or fails");
    await act(async () => box.click());
    expect(calls).toEqual([false]);
  });

  it("is on by default, kept per window, and survives a window that blocks storage", () => {
    const mem = new Map<string, string>();
    const storage = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => { mem.set(k, v); } };
    expect(loadSubagentNotices(storage)).toBe(true);
    saveSubagentNotices(false, storage);
    expect(loadSubagentNotices(storage)).toBe(false);
    saveSubagentNotices(true, storage);
    expect(loadSubagentNotices(storage)).toBe(true);
    const blocked = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(loadSubagentNotices(blocked)).toBe(true);
    expect(() => saveSubagentNotices(false, blocked)).not.toThrow();
  });
});
