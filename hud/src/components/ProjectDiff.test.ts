// The Diff pane's change list redraws only when the project's git status
// changes — never because the window redrew around it (typing in a chat box
// redraws App, and App hands the pane fresh closures each time), never for a
// poll that saw nothing new. Review of PR #67, M1: unmemoized, the list
// redrew per keystroke, 17 ms at 2,000 changed files and 132 ms at the
// status's 20,000 cap. `ProjectDiff` is `memo(...)` with a comparator that
// reads callbacks through a ref; drop either and these counts move.
//
// What is counted: each file row draws a `FileIcon`, replaced here by a plain
// component that counts its renders, so a count moves exactly when a row does.

import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const counts = vi.hoisted(() => ({ icon: 0 }));
const world = vi.hoisted(() => ({
  status: {
    repo: true, head: "c0ffee1", branch: "main", truncated: false,
    files: { "a.cpp": "modified", "b.cpp": "untracked", "c.cpp": "deleted" } as Record<string, string>,
    dirs: {} as Record<string, string>,
  },
  asked: [] as string[],
}));

vi.mock("./FileIcon", () => ({
  FileIcon: () => {
    counts.icon += 1;
    return null;
  },
}));
vi.mock("../lib/monaco", () => ({ diffUri: () => "", loadMonaco: () => new Promise(() => {}) }));
vi.mock("../api", () => ({
  api: {
    // A fresh object on every poll, as the daemon's JSON is.
    gitStatus: async () => JSON.parse(JSON.stringify(world.status)),
    gitBase: async (_p: string, path: string) => {
      world.asked.push(`base:${path}`);
      return { path, state: "ok", head: "c0ffee1", text: "" };
    },
    readFile: async (_p: string, path: string) => {
      world.asked.push(`read:${path}`);
      return { path, content: "", kind: "text", protected: false };
    },
  },
}));

import { ProjectDiff } from "./ProjectDiff";
import { gitStatus } from "../state/gitStatus";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const flush = () => act(async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
});

let unmount: () => void = () => {};
afterEach(() => unmount());

describe("the Diff pane's change list (memoized ProjectDiff)", () => {
  it("does not redraw when the window redraws, on an unchanged poll, or per keystroke; once per real change", async () => {
    let bump: () => void = () => {};
    // App's shape: the window redraws (a chat box's words) and hands the pane
    // new closures every time.
    const Window = () => {
      const [n, setN] = useState(0);
      bump = () => setN((x) => x + 1);
      return createElement("div", { "data-n": n }, createElement(ProjectDiff, {
        projectId: "p-m1", root: "/w", drawn: true, blocked: false,
        onSelect: () => {}, onOpen: () => {},
      }));
    };
    const host = document.createElement("div");
    const root = createRoot(host);
    unmount = () => act(() => root.unmount());
    act(() => root.render(createElement(Window)));
    await flush();
    expect(host.querySelector('[data-testid="scm-a.cpp"]')).not.toBeNull();
    const first = counts.icon;
    expect(first).toBe(3);

    // Twenty redraws of the window — twenty keystrokes in a chat box: nothing.
    for (let i = 0; i < 20; i++) act(() => bump());
    expect(counts.icon).toBe(first);

    // A poll that saw nothing new (a fresh object, the same states): nothing.
    await act(async () => {
      await gitStatus.refresh("p-m1");
    });
    expect(counts.icon).toBe(first);

    // A real change: every row once, the new one included — and once only.
    world.status.files = { ...world.status.files, "d.cpp": "added" };
    await act(async () => {
      await gitStatus.refresh("p-m1");
    });
    expect(host.querySelector('[data-testid="scm-d.cpp"]')).not.toBeNull();
    expect(counts.icon).toBe(first + 4);

    // And redraws after it still draw nothing.
    for (let i = 0; i < 20; i++) act(() => bump());
    expect(counts.icon).toBe(first + 4);
    // Nothing was asked about a file: no row was picked.
    expect(world.asked).toEqual([]);
  });
});
