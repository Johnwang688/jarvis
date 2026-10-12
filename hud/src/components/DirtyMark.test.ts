// The tree's dirty marks redraw only when a buffer goes clean or dirty — never
// per keystroke (the owner: typing with several panes open is a sore point),
// and never for a file this project's tree does not show.

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { useDirtyMarks } from "./DirtyMark";
import { Buffers, PlainModel, type BufferDeps } from "../state/buffers";
import type { Marks } from "../lib/dirtyMarks";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const ROOTS: Record<string, string> = { p1: "/w", p2: "/other" };

function registry() {
  const disk = new Map<string, string>([["/w/src/a.cpp", "int a;\n"], ["/other/b.py", "b = 1\n"]]);
  const deps: BufferDeps = {
    read: async (projectId, path) => {
      const abs = `${ROOTS[projectId]}/${path}`;
      const content = disk.get(abs)!;
      return { path, abs, content, etag: `e-${content.length}`, kind: "text", protected: false, editable: true };
    },
    write: async (projectId, body) => {
      disk.set(`${ROOTS[projectId]}/${body.path}`, body.content);
      return { etag: `e-${body.content.length}` };
    },
    stat: async () => ({ files: {} }),
    makeModel: async (text) => new PlainModel(text),
    now: () => 0,
  };
  const reg = new Buffers();
  reg.configure(deps);
  return reg;
}

describe("useDirtyMarks", () => {
  it("redraws once when a file goes dirty, never per keystroke, and once when it is saved", async () => {
    const reg = registry();
    let renders = 0;
    let seen: Marks | null = null;
    const Probe = () => {
      renders += 1;
      seen = useDirtyMarks("p1", "/w", reg);
      return null;
    };
    const root = createRoot(document.createElement("div"));
    act(() => root.render(createElement(Probe)));
    await act(async () => {
      await reg.open("p1", "src/a.cpp", "2|src/a.cpp");
      await reg.open("p2", "b.py", "3|b.py");
    });
    const base = renders;
    expect(seen!.size).toBe(0);

    const m = reg.model("/w/src/a.cpp") as PlainModel;
    act(() => m.setValue("int a; // 1\n"));
    expect(renders).toBe(base + 1);
    expect(seen!.file("src/a.cpp")).toBe(true);
    expect(seen!.folder("src")).toBe(true);

    // Forty more keystrokes: the registry says nothing, the tree draws nothing.
    for (let i = 2; i < 42; i++) act(() => m.setValue(`int a; // ${i}\n`));
    expect(renders).toBe(base + 1);

    // Another project's file going dirty is not this tree's.
    act(() => (reg.model("/other/b.py") as PlainModel).setValue("b = 2\n"));
    expect(renders).toBe(base + 1);

    await act(async () => {
      await reg.save("/w/src/a.cpp");
    });
    expect(renders).toBe(base + 2);
    expect(seen!.size).toBe(0);
    act(() => root.unmount());
  });

  it("a file read through another project is marked by where it sits under this root", async () => {
    const reg = registry();
    let seen: Marks | null = null;
    const Probe = () => {
      seen = useDirtyMarks("p2", "/w", reg);
      return null;
    };
    const root = createRoot(document.createElement("div"));
    act(() => root.render(createElement(Probe)));
    await act(async () => {
      await reg.open("p1", "src/a.cpp", "2|src/a.cpp");
    });
    act(() => (reg.model("/w/src/a.cpp") as PlainModel).setValue("int b;\n"));
    expect(seen!.file("src/a.cpp")).toBe(true);
    act(() => root.unmount());
  });
});
