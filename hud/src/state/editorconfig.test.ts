import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyEditorConfig, editorConfigFor, forgetEditorConfig, overrideFor, reapplyEditorConfig, TIMEOUT_MS,
  type EditorConfigDeps,
} from "./editorconfig";
import { Buffers, type BufferDeps, type TextModel } from "./buffers";
import type { FileRead } from "../types";

/** A project's files (relative paths) and a recorder over the two routes. */
function world(files: Record<string, string>) {
  const stats: string[][] = [];
  const reads: string[] = [];
  const deps: EditorConfigDeps = {
    stat: async (_id, paths) => {
      stats.push(paths);
      const out: Record<string, any> = {};
      for (const p of paths) out[p] = p in files ? { etag: "0".repeat(32), kind: "text" } : { gone: true };
      return { files: out };
    },
    read: async (_id, path): Promise<FileRead> => {
      reads.push(path);
      if (!(path in files)) throw new Error("404");
      return { path, content: files[path], kind: "text", protected: false };
    },
  };
  return { deps, stats, reads };
}

/** The bits of a Monaco model applyEditorConfig touches. */
class Model {
  opts: Record<string, unknown> = { insertSpaces: true, tabSize: 4, indentSize: 4 };
  eol = "\n";
  version = 1;
  dead = false;
  constructor(public text: string) {}
  updateOptions(o: Record<string, unknown>) {
    this.opts = { ...this.opts, ...o };
  }
  getLineCount() {
    return this.text.split(/\r\n|\n/).length;
  }
  getEOL() {
    return this.eol;
  }
  setEOL(e: number) {
    this.eol = e === 1 ? "\r\n" : "\n";
    this.version += 1; // Monaco's setEOL moves the version, as an edit would
  }
  onWillDispose() {
    return { dispose() {} };
  }
  isDisposed() {
    return this.dead;
  }
}

beforeEach(() => forgetEditorConfig());
afterEach(() => vi.useRealTimers());

describe("finding a file's .editorconfig (ED-8)", () => {
  it("one stat of every folder up to the project's root, never above", async () => {
    const w = world({});
    expect(await editorConfigFor(w.deps, "p1", "a/b/c.py")).toEqual({});
    expect(w.stats).toEqual([["a/b/.editorconfig", "a/.editorconfig", ".editorconfig"]]);
    expect(w.reads).toEqual([]); // nothing there, nothing read
    expect(w.stats.flat().some((p) => p.includes(".."))).toBe(false);
  });

  it("reads nearest first, stops at root = true, and nearer wins", async () => {
    const w = world({
      ".editorconfig": "[*]\nindent_size = 8\n",
      "a/.editorconfig": "root = true\n[*]\nindent_style = space\nindent_size = 4\n",
      "a/b/.editorconfig": "[*.py]\nindent_size = 2\n",
    });
    expect(await editorConfigFor(w.deps, "p1", "a/b/c.py")).toEqual({
      indent_style: "space", indent_size: "2", tab_width: "2",
    });
    expect(w.reads).toEqual(["a/b/.editorconfig", "a/.editorconfig"]); // the root's never read
  });

  it("a folder's answer is kept a moment: a tab restore asks once per folder", async () => {
    const w = world({ ".editorconfig": "[*]\nindent_style = tab\n" });
    await Promise.all([
      editorConfigFor(w.deps, "p1", "x.c", 1000), editorConfigFor(w.deps, "p1", "y.c", 1000),
      editorConfigFor(w.deps, "p1", "z.c", 1500),
    ]);
    expect(w.stats).toHaveLength(1);
    expect(w.reads).toEqual([".editorconfig"]);
    await editorConfigFor(w.deps, "p1", "x.c", 1000 + 3001);
    expect(w.stats).toHaveLength(2); // stale: asked again
  });

  it("a refused stat, a failed read, a withheld or non-text answer: no configuration, no throw", async () => {
    const failing: EditorConfigDeps = {
      stat: async () => { throw new Error("403"); },
      read: async () => { throw new Error("never"); },
    };
    expect(await editorConfigFor(failing, "p1", "a.c")).toEqual({});
    forgetEditorConfig();
    const odd: EditorConfigDeps = {
      stat: async (_id, paths) => ({ files: Object.fromEntries(paths.map((p) => [p, { kind: "withheld" }])) }),
      read: async () => { throw new Error("never"); },
    };
    expect(await editorConfigFor(odd, "p1", "a.c")).toEqual({});
    forgetEditorConfig();
    const binary: EditorConfigDeps = {
      stat: async (_id, paths) => ({ files: Object.fromEntries(paths.map((p) => [p, { etag: "0".repeat(32), kind: "text" }])) }),
      read: async (_id, path) => ({ path, kind: "binary", protected: false }),
    };
    expect(await editorConfigFor(binary, "p1", "a.c")).toEqual({});
  });
});

describe("applying it to a model", () => {
  const cfg = "root = true\n[*.c]\nindent_style = tab\nindent_size = 4\nend_of_line = crlf\ntrim_trailing_whitespace = true\ninsert_final_newline = true\n";

  it("sets the indentation; never the text", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("int x;\n    y;\n");
    const o = await applyEditorConfig(w.deps, m as any, "p1", "main.c");
    expect(o).toEqual({ insertSpaces: false, tabSize: 4, indentSize: 4, eol: "CRLF" });
    expect(m.opts).toMatchObject({ insertSpaces: false, tabSize: 4, indentSize: 4 });
    expect(m.text).toBe("int x;\n    y;\n");
    expect(m.eol).toBe("\n"); // a file with line endings keeps them
    expect(overrideFor(m)).toEqual(o);
  });

  it("gives a file with no line break the ending its new lines take", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("");
    await applyEditorConfig(w.deps, m as any, "p1", "new.c");
    expect(m.eol).toBe("\r\n");
  });

  it("an unmatched file, or the fallback's model (no options), is left alone", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("x");
    expect(await applyEditorConfig(w.deps, m as any, "p1", "notes.md")).toBeNull();
    expect(m.opts).toEqual({ insertSpaces: true, tabSize: 4, indentSize: 4 });
    expect(await applyEditorConfig(w.deps, { getValue: () => "" } as any, "p1", "a.c")).toBeNull();
  });

  it("is put back after Monaco recomputes a model's indentation (a settings change)", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("x;\n");
    await applyEditorConfig(w.deps, m as any, "p1", "main.c");
    m.opts = { insertSpaces: true, tabSize: 8, indentSize: 8 }; // what the model service would do
    reapplyEditorConfig();
    expect(m.opts).toMatchObject({ insertSpaces: false, tabSize: 4, indentSize: 4 });
  });

  it("never waits past its timeout: a slow disk opens the file without it", async () => {
    vi.useFakeTimers();
    const slow: EditorConfigDeps = { stat: () => new Promise(() => {}), read: () => new Promise(() => {}) };
    const m = new Model("x;\n");
    const p = applyEditorConfig(slow, m as any, "p1", "slow.c");
    await vi.advanceTimersByTimeAsync(TIMEOUT_MS + 10);
    expect(await p).toBeNull();
  });
});

describe("the buffer registry's prepare hook", () => {
  class FakeModel implements TextModel {
    version = 1;
    eol = "\n";
    fns = new Set<() => void>();
    constructor(public text: string) {}
    getValue() { return this.text; }
    getAlternativeVersionId() { return this.version; }
    onDidChangeContent(fn: () => void) { this.fns.add(fn); return { dispose: () => void this.fns.delete(fn) }; }
    isDisposed() { return false; }
    dispose() {}
    getEOL() { return this.eol; }
  }

  const deps = (prepare?: BufferDeps["prepare"]): BufferDeps => ({
    read: async (_id, path) => ({ path, abs: `/w/${path}`, content: "", etag: "0".repeat(32), kind: "text", protected: false, editable: true }),
    write: async () => ({ etag: "1".repeat(32) }),
    stat: async () => ({ files: {} }),
    makeModel: async (text) => new FakeModel(text),
    now: () => 0,
    prepare,
  });

  it("runs before the registry records the text, so an EOL set there leaves the buffer clean", async () => {
    const b = new Buffers();
    let seen: { projectId: string; path: string } | null = null;
    b.configure(deps(async (model, projectId, path) => {
      seen = { projectId, path };
      (model as FakeModel).eol = "\r\n";
      (model as FakeModel).version += 1; // as Monaco's setEOL does
    }));
    const v = await b.open("p1", "new.c", "2|new.c");
    expect(seen).toEqual({ projectId: "p1", path: "new.c" });
    expect(v.dirty).toBe(false);
    expect(v.modelEol).toBe("CRLF");
  });

  it("one that throws never stops a file opening", async () => {
    const b = new Buffers();
    b.configure(deps(async () => { throw new Error("boom"); }));
    const v = await b.open("p1", "a.c", "2|a.c");
    expect(v.dirty).toBe(false);
    expect(v.kind).toBe("text");
  });
});
