// The File pane's tree redraws only when what it shows changes — a buffer
// going clean↔dirty, a save's answer — never because the window redrew around
// it (typing in a chat box redraws App, and so FileTab), never per keystroke
// in the editor, and never for a save's "saving…"/queued notices, which the
// registry now emits too (ED-2). The tree's `Node` is `memo(...)` with props
// FileTab holds still (a stable `onOpen`, `marks` changing only with the dirty
// set); this pins both halves: drop the `memo(` or hand the tree a fresh
// closure per render and these counts move (review of PR #50, LOW).
//
// What is counted: each file row draws a `FileIcon` and each folder row a
// `Chevron`; both are replaced here by plain (unmemoized) components that
// count their renders, so a count moves exactly when a `Node` renders.

import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

const counts = vi.hoisted(() => ({ icon: 0, chevron: 0 }));

vi.mock("./FileIcon", () => ({
  FileIcon: () => {
    counts.icon += 1;
    return null;
  },
  Chevron: () => {
    counts.chevron += 1;
    return null;
  },
}));
vi.mock("./Editor", () => ({ PaneEditor: () => null, CompareEditor: () => null, DiffEditor: () => null }));
vi.mock("../api", () => ({
  api: {
    tree: async (_pid: string, dir: string) => ({
      path: dir,
      entries: dir
        ? [{ name: "drive.cpp", kind: "file", size: 9, mtime: 1 }]
        : [
            { name: "src", kind: "dir", size: 0, mtime: 1 },
            { name: "config.json", kind: "file", size: 9, mtime: 1 },
            { name: "main.cpp", kind: "file", size: 9, mtime: 1 },
          ],
    }),
  },
}));

import { FileTab } from "./FileTab";
import { buffers, PlainModel, type BufferDeps } from "../state/buffers";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const flush = () => act(async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
});

// The window's one registry, configured for the test: a write held until
// `release()` so a save's "saving…" can be seen while it is out.
let release: () => void = () => {};
beforeAll(() => {
  const disk = new Map<string, string>([["/w/main.cpp", "int main() {}\n"]]);
  const deps: BufferDeps = {
    read: async (_projectId, path) => {
      const abs = `/w/${path}`;
      const content = disk.get(abs) ?? "";
      return { path, abs, content, etag: `e-${content.length}`, kind: "text", protected: false, editable: true };
    },
    write: (_projectId, body) =>
      new Promise((resolve) => {
        release = () => {
          disk.set(`/w/${body.path}`, body.content);
          resolve({ etag: `e-${body.content.length}` });
        };
      }),
    stat: async () => ({ files: {} }),
    makeModel: async (text) => new PlainModel(text),
    now: () => 0,
  };
  buffers.configure(deps);
});
afterAll(() => buffers.release("/w/main.cpp", "9|main.cpp", { discard: true }));

describe("the File pane's tree (memoized Node)", () => {
  it("does not redraw when the window redraws around it, per keystroke, or for a save's saving notice", async () => {
    let bump: () => void = () => {};
    // App's shape: something above FileTab redraws (a chat box's words) and
    // FileTab, which is not memoized, redraws with it.
    const Window = () => {
      const [n, setN] = useState(0);
      bump = () => setN((x) => x + 1);
      return createElement("div", { "data-n": n }, createElement(FileTab, {
        pane: 9, projectId: "p1", tabs: null, root: "/w",
      }));
    };
    const host = document.createElement("div");
    const root = createRoot(host);
    act(() => root.render(createElement(Window)));
    await flush();
    // Open src so a nested Node is drawn too.
    act(() => (host.querySelector('[data-testid="dir-src"]') as HTMLElement).click());
    await flush();
    expect(host.querySelector('[data-testid="file-src/drive.cpp"]')).not.toBeNull();
    const rows = { icon: counts.icon, chevron: counts.chevron };
    expect(rows.icon).toBeGreaterThan(0);

    // Twenty redraws of the window: the tree draws nothing.
    for (let i = 0; i < 20; i++) act(() => bump());
    expect(counts).toEqual(rows);

    // A buffer opened through the registry (an emit, nothing dirty): nothing.
    await act(async () => {
      await buffers.open("p1", "main.cpp", "9|main.cpp");
    });
    expect(counts).toEqual(rows);

    // Twenty characters typed: the first makes it dirty — the root Node draws
    // once (its main.cpp row gains the dot) — and the other nineteen nothing.
    const model = buffers.model("/w/main.cpp") as PlainModel;
    let text = model.getValue();
    act(() => model.setValue((text += "x")));
    expect(host.querySelector('[data-testid="dirty-main.cpp"]')).not.toBeNull();
    const dirty = { icon: counts.icon, chevron: counts.chevron };
    expect(dirty.icon).toBeGreaterThan(rows.icon);
    for (let i = 0; i < 19; i++) {
      act(() => model.setValue((text += "x")));
      act(() => bump());
    }
    expect(counts).toEqual(dirty);

    // A save: "saving…" goes out (an emit, still dirty) and draws nothing in
    // the tree; its answer makes the file clean, and the tree draws once.
    let saved: Promise<unknown> = Promise.resolve();
    act(() => {
      saved = buffers.save("/w/main.cpp");
    });
    await flush();
    expect(buffers.snapshot().find((b) => b.abs === "/w/main.cpp")?.saving).toBe(true);
    act(() => bump());
    expect(counts).toEqual(dirty);
    await act(async () => {
      release();
      await saved;
    });
    expect(host.querySelector('[data-testid="dirty-main.cpp"]')).toBeNull();
    expect(counts.icon).toBeGreaterThan(dirty.icon);
    const clean = { icon: counts.icon, chevron: counts.chevron };
    for (let i = 0; i < 5; i++) act(() => bump());
    expect(counts).toEqual(clean);

    act(() => root.unmount());
  });
});
