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
  // The status's cap: 20,000 changed files (the Diff follow-ups' list at scale).
  big: {
    repo: true, head: "c0ffee1", branch: "main", truncated: true,
    files: Object.fromEntries(Array.from({ length: 20000 }, (_, i) => [`src/f${String(i).padStart(5, "0")}.py`, "modified"])),
    dirs: {},
  },
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
    gitStatus: async (p: string) => JSON.parse(JSON.stringify(p === "p-big" ? world.big : world.status)),
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

// The Diff follow-ups (2026-10-12): at the status's 20,000-row cap the list
// draws a window of rows, never all of them (a DOM that size cost every
// keystroke in the window ~100 ms), and the keys move a selection that is a
// path — it holds, and moves, when its row is not drawn.
describe("the change list at scale", () => {
  it("draws a window of the 20,000 rows, and the keys move the selection past it", async () => {
    const picked: string[] = [];
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    unmount = () => {
      act(() => root.unmount());
      host.remove();
    };
    act(() => root.render(createElement(ProjectDiff, {
      projectId: "p-big", root: "/w", drawn: true, blocked: false,
      onSelect: (p: string) => picked.push(p), onOpen: () => {},
    })));
    await flush();
    const list = host.querySelector('[data-testid="scm-rows"]') as HTMLElement;
    expect(list.dataset.count).toBe("20000");
    const drawn = () => host.querySelectorAll(".scmrow").length;
    // jsdom lays nothing out: the unmeasured window, a few dozen rows.
    expect(drawn()).toBeGreaterThan(0);
    expect(drawn()).toBeLessThan(100);
    // The spacer is as tall as every row: the scroll bar is the whole list's.
    expect((host.querySelector(".scmspace") as HTMLElement).style.height).toBe(`${20000 * 24}px`);

    const key = (k: string) => act(() => {
      list.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
    });
    const path = () => host.querySelector('[data-testid="scm-path"]')!.textContent;
    key("ArrowDown");
    expect(path()).toBe("src/f00000.py");
    key("ArrowDown");
    expect(path()).toBe("src/f00001.py");
    expect(host.querySelector('[data-testid="scm-src/f00001.py"]')!.className).toContain("sel");
    key("End");
    expect(path()).toBe("src/f19999.py");
    // Its row is not drawn here (nothing scrolled), and it is still the selection.
    expect(host.querySelector('[data-testid="scm-src/f19999.py"]')).toBeNull();
    expect(host.querySelector(".scmrow.sel")).toBeNull();
    key("ArrowUp");
    expect(path()).toBe("src/f19998.py");
    key("Home");
    expect(path()).toBe("src/f00000.py");
    // A key the list does not take is left alone, and a modified one too.
    key("a");
    act(() => {
      list.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", ctrlKey: true, bubbles: true }));
    });
    expect(path()).toBe("src/f00000.py");
    expect(picked).toEqual(["src/f00000.py", "src/f00001.py", "src/f19999.py", "src/f19998.py", "src/f00000.py"]);
  });

  it("under a card the keys move nothing", async () => {
    const host = document.createElement("div");
    const root = createRoot(host);
    unmount = () => act(() => root.unmount());
    act(() => root.render(createElement(ProjectDiff, {
      projectId: "p-big", root: "/w", drawn: true, blocked: true, onOpen: () => {},
    })));
    await flush();
    const list = host.querySelector('[data-testid="scm-rows"]') as HTMLElement;
    act(() => {
      list.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    });
    expect(host.querySelector('[data-testid="scm-path"]')!.textContent).toBe("no file selected");
  });
});
