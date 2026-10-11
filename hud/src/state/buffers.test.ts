import { beforeEach, describe, expect, it } from "vitest";
import { ApiError } from "../api";
import { Buffers, PlainModel, refOf, type BufferDeps, type TextModel, type WriteBody } from "./buffers";
import type { FileRead } from "../types";

/**
 * A text model with Monaco's version semantics: every edit bumps the version,
 * and undo/redo restore the *alternative* version id of the state they return
 * to — which is what makes "undo back to the saved text reads clean" true.
 */
class FakeModel implements TextModel {
  private stack: { text: string; alt: number }[];
  private at = 0;
  private nextId = 2;
  private fns = new Set<() => void>();
  private dead = false;
  eol: string;
  constructor(text: string) {
    this.stack = [{ text, alt: 1 }];
    this.eol = text.includes("\r\n") ? "\r\n" : "\n";
  }
  getValue() {
    return this.stack[this.at].text;
  }
  getAlternativeVersionId() {
    return this.stack[this.at].alt;
  }
  private push(text: string) {
    this.stack = this.stack.slice(0, this.at + 1);
    this.stack.push({ text, alt: this.nextId++ });
    this.at += 1;
    for (const f of Array.from(this.fns)) f();
  }
  /** The owner typing. */
  type(text: string) {
    this.push(this.getValue() + text);
  }
  undo() {
    if (this.at === 0) return;
    this.at -= 1;
    for (const f of Array.from(this.fns)) f();
  }
  redo() {
    if (this.at === this.stack.length - 1) return;
    this.at += 1;
    for (const f of Array.from(this.fns)) f();
  }
  onDidChangeContent(fn: () => void) {
    this.fns.add(fn);
    return { dispose: () => void this.fns.delete(fn) };
  }
  isDisposed() {
    return this.dead;
  }
  dispose() {
    this.dead = true;
  }
  getEOL() {
    return this.eol;
  }
  getLanguageId() {
    return "plaintext";
  }
  pushEOL(eol: number) {
    this.eol = eol === 1 ? "\r\n" : "\n";
  }
  getPositionAt(offset: number) {
    const before = this.getValue().slice(0, offset).split("\n");
    return { lineNumber: before.length, column: before[before.length - 1].length + 1 };
  }
  private offsetAt(line: number, col: number) {
    const lines = this.getValue().split("\n");
    let o = 0;
    for (let i = 0; i < line - 1; i++) o += lines[i].length + 1;
    return o + col - 1;
  }
  pushEditOperations(_: any, ops: any[]) {
    const op = ops[0];
    const v = this.getValue();
    const a = this.offsetAt(op.range.startLineNumber, op.range.startColumn);
    const z = this.offsetAt(op.range.endLineNumber, op.range.endColumn);
    this.push(v.slice(0, a) + op.text + v.slice(z));
    return null;
  }
}

const tagOf = (text: string) => `e${[...text].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(16).padStart(8, "0")}`.padEnd(32, "0");

/** A little disk, a recorder, and the three routes over it. */
class World {
  files = new Map<string, string>();
  kinds = new Map<string, string>();
  writes: (WriteBody & { projectId: string })[] = [];
  reads: string[] = [];
  statCalls = 0;
  roots: Record<string, string> = { p1: "/w", inbox: "/" };
  models: FakeModel[] = [];
  deps: BufferDeps = {
    read: async (projectId, path): Promise<FileRead> => {
      const abs = this.abs(projectId, path);
      this.reads.push(abs);
      const kind = this.kinds.get(abs) ?? "text";
      if (kind === "withheld") return { path, abs, kind, protected: true, readonly: "withheld" };
      const content = this.files.get(abs);
      if (content === undefined) throw new ApiError(404, "file not found");
      return {
        path, abs, content, etag: tagOf(content), kind, protected: false, encoding: "utf-8",
        eol: content.includes("\r\n") ? "CRLF" : "LF", readonly: kind === "text" ? null : `a ${kind} file`,
      };
    },
    write: async (projectId, body) => {
      this.writes.push({ ...body, projectId });
      const abs = this.abs(projectId, body.path);
      const now = this.files.get(abs);
      const etag = now === undefined ? null : tagOf(now);
      if (etag !== body.expected_etag) throw new ApiError(409, "changed", { error: "changed", etag });
      this.files.set(abs, body.content);
      return { etag: tagOf(body.content) };
    },
    stat: async (projectId, paths) => {
      this.statCalls += 1;
      const files: Record<string, any> = {};
      for (const p of paths) {
        const abs = this.abs(projectId, p);
        const c = this.files.get(abs);
        files[p] = this.kinds.get(abs) === "withheld" ? { kind: "withheld" }
          : c === undefined ? { gone: true } : { etag: tagOf(c), kind: this.kinds.get(abs) ?? "text" };
      }
      return { files };
    },
    makeModel: async (text) => {
      const m = new FakeModel(text);
      this.models.push(m);
      return m;
    },
    now: () => 1000,
  };
  abs(projectId: string, path: string) {
    const root = this.roots[projectId];
    return root === "/" ? `/${path}` : `${root}/${path}`;
  }
}

let w: World;
let b: Buffers;

beforeEach(() => {
  w = new World();
  w.files.set("/w/a.py", "one\n");
  w.files.set("/w/b.py", "two\n");
  b = new Buffers();
  b.configure(w.deps);
});

const model = (abs: string) => b.model(abs) as FakeModel;

describe("one buffer per file on disk", () => {
  it("is read once and shared by every tab and project that opens it", async () => {
    const v = await b.open("p1", "a.py", refOf(2, "a.py"));
    expect(v.abs).toBe("/w/a.py");
    await b.open("p1", "a.py", refOf(3, "a.py"));
    // The Inbox is rooted at "/" here: the same file through another project.
    const via = await b.open("inbox", "w/a.py", refOf(4, "w/a.py"));
    expect(via.abs).toBe("/w/a.py");
    expect(b.snapshot()).toHaveLength(1);
    expect(b.get("/w/a.py")!.refs.sort()).toEqual(["2|a.py", "3|a.py", "4|w/a.py"]);
    expect(b.get("/w/a.py")!.projectId).toBe("p1"); // saves through where it was read
    expect(w.models).toHaveLength(1);
  });

  it("two opens in flight read once", async () => {
    await Promise.all([b.open("p1", "a.py", "1|a.py"), b.open("p1", "a.py", "2|a.py")]);
    expect(w.reads.filter((r) => r === "/w/a.py")).toHaveLength(1);
  });

  it("a model goes when no tab holds it and it is clean; a dirty one stays until discarded", async () => {
    await b.open("p1", "a.py", "1|a.py");
    await b.open("p1", "a.py", "2|a.py");
    const m = model("/w/a.py");
    b.release("/w/a.py", "1|a.py");
    expect(m.isDisposed()).toBe(false);
    m.type("x");
    b.release("/w/a.py", "2|a.py");
    expect(m.isDisposed()).toBe(false);
    expect(b.anyDirty()).toBe(true);
    // Reopened, it is the same buffer, edit and all.
    await b.open("p1", "a.py", "3|a.py");
    expect(b.text("/w/a.py")).toBe("one\nx");
    b.release("/w/a.py", "3|a.py", { discard: true });
    expect(m.isDisposed()).toBe(true);
    expect(b.snapshot()).toHaveLength(0);
  });

  it("releasePane lets go of every tab of a pane, a shared buffer staying with the other", async () => {
    await b.open("p1", "a.py", "2|a.py");
    await b.open("p1", "b.py", "2|b.py");
    await b.open("p1", "a.py", "3|a.py");
    model("/w/a.py").type("x");
    model("/w/b.py").type("y");
    expect(b.paneDirty(2)).toBe(true);
    b.releasePane(2, { discard: true });
    expect(b.paneDirty(2)).toBe(false);
    expect(b.get("/w/b.py")).toBeNull();
    expect(b.get("/w/a.py")!.dirty).toBe(true); // pane 3 still holds it
    expect(b.paneDirty(3)).toBe(true);
  });

  it("a read that fails rejects and holds nothing", async () => {
    await expect(b.open("p1", "missing.py", "1|missing.py")).rejects.toMatchObject({ status: 404 });
    expect(b.snapshot()).toHaveLength(0);
  });

  it("a withheld file is a buffer with no model and no text, ever", async () => {
    w.kinds.set("/w/key", "withheld");
    w.files.set("/w/key", "SECRET");
    const v = await b.open("p1", "key", "1|key");
    expect(v.kind).toBe("withheld");
    expect(b.model("/w/key")).toBeNull();
    expect(b.text("/w/key")).toBeNull();
    expect(JSON.stringify(b.snapshot())).not.toContain("SECRET");
    await b.poll();
    expect(await b.save("/w/key")).toBe("skipped");
  });
});

describe("dirty means 'not the version last read or saved'", () => {
  it("an edit is dirty; undoing back to the saved text is clean; redoing is dirty again", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const m = model("/w/a.py");
    m.type("x");
    expect(b.isDirty("/w/a.py")).toBe(true);
    m.undo();
    expect(b.isDirty("/w/a.py")).toBe(false);
    m.redo();
    expect(b.isDirty("/w/a.py")).toBe(true);
  });

  it("is said at once, in the edit's own change event, and only when it moves", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const seen: boolean[] = [];
    b.subscribe(() => seen.push(b.paneDirty(1)));
    const m = model("/w/a.py");
    m.type("x");
    expect(seen).toEqual([true]);
    m.type("y");
    expect(seen).toEqual([true]);
    m.undo();
    m.undo();
    expect(seen).toEqual([true, false]);
  });

  it("a read-only kind is never dirty and never saved", async () => {
    w.kinds.set("/w/a.py", "gate");
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    expect(b.isDirty("/w/a.py")).toBe(false);
    expect(await b.save("/w/a.py")).toBe("skipped");
    expect(w.writes).toHaveLength(0);
  });
});

describe("saving: against the etag it was read as, the text read once", () => {
  it("saves, and is clean after; undo past the save is dirty", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const m = model("/w/a.py");
    m.type("x");
    expect(await b.save("/w/a.py")).toBe("saved");
    expect(w.writes[0]).toMatchObject({ path: "a.py", content: "one\nx", expected_etag: tagOf("one\n"), projectId: "p1" });
    expect(b.isDirty("/w/a.py")).toBe(false);
    expect(b.get("/w/a.py")!.etag).toBe(tagOf("one\nx"));
    m.undo();
    expect(b.isDirty("/w/a.py")).toBe(true);
  });

  it("a keystroke during the save stays unsaved", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const m = model("/w/a.py");
    m.type("x");
    const saving = b.save("/w/a.py");
    m.type("y");
    await saving;
    expect(w.files.get("/w/a.py")).toBe("one\nx");
    expect(b.isDirty("/w/a.py")).toBe(true);
  });

  it("a 409 is the conflict bar with the disk's etag, and the file is untouched; Keep mine saves against it", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("mine");
    w.files.set("/w/a.py", "theirs\n");
    expect(await b.save("/w/a.py")).toBe("conflict");
    expect(w.files.get("/w/a.py")).toBe("theirs\n");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("theirs\n") });
    expect(b.isDirty("/w/a.py")).toBe(true);
    // Another write lands meanwhile: Keep mine against the *shown* etag fails again.
    w.files.set("/w/a.py", "newer\n");
    expect(await b.keepMine("/w/a.py")).toBe("conflict");
    expect(w.writes.at(-1)!.expected_etag).toBe(tagOf("theirs\n"));
    expect(w.files.get("/w/a.py")).toBe("newer\n");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("newer\n") });
    expect(await b.keepMine("/w/a.py")).toBe("saved");
    expect(w.files.get("/w/a.py")).toBe("one\nmine");
    expect(b.get("/w/a.py")!.conflict).toBeNull();
  });

  it("gone: a save says so (etag null), and Save to recreate creates it with expected null", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    w.files.delete("/w/a.py");
    expect(await b.save("/w/a.py")).toBe("conflict");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "gone" });
    expect(await b.keepMine("/w/a.py")).toBe("saved");
    expect(w.writes.at(-1)!.expected_etag).toBeNull();
    expect(w.files.get("/w/a.py")).toBe("one\nx");
  });

  it("a 409 with no etag (an older daemon) asks the poll route what is on disk", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    w.files.set("/w/a.py", "theirs\n");
    const real = w.deps.write;
    w.deps.write = async () => {
      throw new ApiError(409, "file changed since it was read");
    };
    expect(await b.save("/w/a.py")).toBe("conflict");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("theirs\n") });
    w.deps.write = real;
  });
});

describe("the change poll", () => {
  it("a clean buffer reloads in place, undoably, and the toast names the writer the feed saw", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const notes: string[] = [];
    b.onNotice((_abs, t) => notes.push(t));
    b.noteTouch("a.py", "Codex · task 41");
    w.files.set("/w/a.py", "one\ntwo\n");
    await b.poll();
    const m = model("/w/a.py");
    expect(m.getValue()).toBe("one\ntwo\n");
    expect(b.isDirty("/w/a.py")).toBe(false);
    expect(b.get("/w/a.py")!.etag).toBe(tagOf("one\ntwo\n"));
    expect(notes).toEqual(["changed on disk by Codex · task 41 — reloaded"]);
    // Undo steps back to the old text, which is then unsaved against the disk.
    m.undo();
    expect(m.getValue()).toBe("one\n");
    expect(b.isDirty("/w/a.py")).toBe(true);
  });

  it("a dirty buffer gets the bar, never a reload; the same write is not raised twice", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    w.files.set("/w/a.py", "theirs\n");
    let emits = 0;
    b.subscribe(() => (emits += 1));
    await b.poll();
    expect(b.text("/w/a.py")).toBe("one\nx");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("theirs\n") });
    const after = emits;
    await b.poll();
    expect(emits).toBe(after);
  });

  it("Take theirs loads the disk's text undoably; Ctrl+Z brings the edit back", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const m = model("/w/a.py");
    m.type("x");
    w.files.set("/w/a.py", "theirs\n");
    await b.poll();
    await b.takeTheirs("/w/a.py");
    expect(m.getValue()).toBe("theirs\n");
    expect(b.isDirty("/w/a.py")).toBe(false);
    expect(b.get("/w/a.py")!.conflict).toBeNull();
    m.undo();
    expect(m.getValue()).toBe("one\nx");
  });

  it("an edit made while the reload was reading wins: the bar, not the reload", async () => {
    await b.open("p1", "a.py", "1|a.py");
    w.files.set("/w/a.py", "theirs\n");
    const read = w.deps.read;
    w.deps.read = async (p, path) => {
      model("/w/a.py").type("typed meanwhile");
      return read(p, path);
    };
    await b.poll();
    w.deps.read = read;
    expect(b.text("/w/a.py")).toBe("one\ntyped meanwhile");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("theirs\n") });
  });

  it("gone, then back with the same bytes: the bar clears", async () => {
    await b.open("p1", "a.py", "1|a.py");
    w.files.delete("/w/a.py");
    await b.poll();
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "gone" });
    w.files.set("/w/a.py", "one\n");
    await b.poll();
    expect(b.get("/w/a.py")!.conflict).toBeNull();
  });

  it("only buffers a tab holds are polled, one request per project", async () => {
    await b.open("p1", "a.py", "1|a.py");
    await b.open("p1", "b.py", "1|b.py");
    model("/w/b.py").type("x");
    b.release("/w/b.py", "1|b.py"); // dirty, held by no tab
    w.statCalls = 0;
    await b.poll();
    expect(w.statCalls).toBe(1);
  });

  it("a CRLF file reloads as CRLF", async () => {
    w.files.set("/w/c.txt", "a\r\nb\r\n");
    await b.open("p1", "c.txt", "1|c.txt");
    w.files.set("/w/c.txt", "a\r\nb\r\nc\r\n");
    await b.poll();
    expect(model("/w/c.txt").getValue()).toBe("a\r\nb\r\nc\r\n");
  });
});

describe("the fallback's shim model", () => {
  it("counts versions and tells its listeners", () => {
    const m = new PlainModel("a");
    let n = 0;
    m.onDidChangeContent(() => (n += 1));
    m.setValue("b");
    m.setValue("b");
    expect([m.getValue(), m.getAlternativeVersionId(), n]).toEqual(["b", 2, 1]);
  });
});
