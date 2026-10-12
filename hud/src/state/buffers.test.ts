import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import {
  Buffers, PlainModel, refOf, WRITE_TIMEOUT_MS, type BufferDeps, type TextModel, type WriteBody,
} from "./buffers";
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
  /** Files the daemon says the editor may not save (an env file that is not UTF-8). */
  uneditable = new Set<string>();
  uriRegistry = false;
  byUri = new Map<string, FakeModel>();
  /** While set, each write waits until `release()` lets it go. */
  hold = false;
  held: (() => void)[] = [];
  release() {
    for (const r of this.held.splice(0)) r();
  }
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
        editable: (kind === "text" || kind === "env") && !this.uneditable.has(abs),
      };
    },
    write: async (projectId, body) => {
      this.writes.push({ ...body, projectId });
      if (this.hold) await new Promise<void>((resolve) => this.held.push(resolve));
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
    makeModel: async (text, abs) => {
      // Like Monaco's model registry, when asked (`uriRegistry`): one live
      // model per URI, an older one at the same URI disposed.
      if (this.uriRegistry) this.byUri.get(abs)?.dispose();
      const m = new FakeModel(text);
      this.byUri.set(abs, m);
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

  it("what closing a pane would lose: only the edits no other pane holds; savePane saves them", async () => {
    await b.open("p1", "a.py", "2|a.py");
    await b.open("p1", "b.py", "2|b.py");
    await b.open("p1", "a.py", "3|a.py");
    model("/w/a.py").type("x");
    model("/w/b.py").type("y");
    expect(b.paneLosing(2).map((v) => v.path)).toEqual(["b.py"]);
    expect(b.paneLosing(3)).toEqual([]);
    expect([b.paneLoses(2), b.paneLoses(3), b.paneDirty(3)]).toEqual([true, false, true]);
    expect(await b.savePane(2)).toBe(true);
    expect(w.files.get("/w/b.py")).toBe("two\ny");
    expect(b.paneLosing(2)).toEqual([]);
    expect(b.isDirty("/w/a.py")).toBe(true); // pane 3's too: not saved by closing pane 2
    w.files.set("/w/a.py", "theirs\n");
    b.release("/w/a.py", "3|a.py");
    expect(await b.savePane(2)).toBe(false); // a conflict: the pane stays open
    expect(w.files.get("/w/a.py")).toBe("theirs\n");
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
    expect(b.get("/w/a.py")!.conflict).toMatchObject({ state: "changed", etag: tagOf("theirs\n") });
    expect(b.isDirty("/w/a.py")).toBe(true);
    // Another write lands meanwhile: Keep mine against the *shown* etag fails again.
    w.files.set("/w/a.py", "newer\n");
    expect(await b.keepMine("/w/a.py")).toBe("conflict");
    expect(w.writes.at(-1)!.expected_etag).toBe(tagOf("theirs\n"));
    expect(w.files.get("/w/a.py")).toBe("newer\n");
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("newer\n"), again: true });
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
    expect(b.get("/w/a.py")!.conflict).toMatchObject({ state: "changed", etag: tagOf("theirs\n") });
    w.deps.write = real;
  });
});

describe("the change poll", () => {
  it("a clean buffer reloads in place, undoably, and the toast names the writer the feed saw", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const notes: string[] = [];
    b.onNotice((_abs, t) => notes.push(t));
    b.noteTouch("/w/a.py", "Codex · task 41");
    b.noteTouch("a.py", "a relative path: ignored");
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

  it("Keep mine with the etag the owner was shown: a write after it is a 409, never overwritten", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    w.files.set("/w/a.py", "first\n");
    await b.poll();
    const shown = (b.get("/w/a.py")!.conflict as any).etag;
    w.files.set("/w/a.py", "second\n");
    await b.poll(); // the bar now says "again", for the newer write
    expect(b.get("/w/a.py")!.conflict).toEqual({ state: "changed", etag: tagOf("second\n"), again: true });
    // A click made on what was shown before (Compare's left side) does not clobber it.
    expect(await b.keepMine("/w/a.py", shown)).toBe("conflict");
    expect(w.files.get("/w/a.py")).toBe("second\n");
  });

  it("a dirty buffer gets the bar, never a reload; the same write is not raised twice", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    w.files.set("/w/a.py", "theirs\n");
    let emits = 0;
    b.subscribe(() => (emits += 1));
    await b.poll();
    expect(b.text("/w/a.py")).toBe("one\nx");
    expect(b.get("/w/a.py")!.conflict).toMatchObject({ state: "changed", etag: tagOf("theirs\n") });
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
    expect(b.get("/w/a.py")!.conflict).toMatchObject({ state: "changed", etag: tagOf("theirs\n") });
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

describe("a byte-order mark is the file's, not the text's", () => {
  it("is kept off the model and put back on save", async () => {
    w.files.set("/w/bom.txt", "\ufeffhi\n");
    const read = w.deps.read;
    w.deps.read = async (p, path) => ({ ...(await read(p, path)), encoding: path === "bom.txt" ? "utf-8-bom" : "utf-8" });
    await b.open("p1", "bom.txt", "1|bom.txt");
    const m = model("/w/bom.txt");
    expect(m.getValue()).toBe("hi\n");
    m.type("x");
    expect(await b.save("/w/bom.txt")).toBe("saved");
    expect(w.files.get("/w/bom.txt")).toBe("\ufeffhi\nx");
    // A reload in place never types the mark in as a character.
    w.files.set("/w/bom.txt", "\ufeffhi\nthere\n");
    await b.poll();
    expect(m.getValue()).toBe("hi\nthere\n");
    w.deps.read = read;
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

describe("the review of PR #49 (each failing on f869543)", () => {
  it("H1: releasePane lets go of this pane's buffers only — another pane's orphaned edit survives", async () => {
    await b.open("p1", "a.py", "2|a.py");
    model("/w/a.py").type("orphan");
    b.release("/w/a.py", "2|a.py");            // pane 2 unmounted: the edit is kept, held by no tab
    await b.open("p1", "b.py", "3|b.py");
    b.releasePane(5, { discard: true });        // × on a chat pane
    b.releasePane(3, { discard: true });        // × on another File pane
    expect(b.get("/w/a.py")?.dirty).toBe(true);
    expect(b.text("/w/a.py")).toBe("one\norphan");
    expect(b.get("/w/b.py")).toBeNull();        // its own clean buffer still goes
    // Pane 3 with its own edit, discarded: that one goes.
    await b.open("p1", "b.py", "3|b.py");
    model("/w/b.py").type("mine");
    b.releasePane(3, { discard: true });
    expect(b.get("/w/b.py")).toBeNull();
    expect(b.get("/w/a.py")?.dirty).toBe(true);
  });

  it("H2: panesLose asks the panes together — two panes sharing an edit hold it as a set", async () => {
    await b.open("p1", "a.py", "2|a.py");
    await b.open("p1", "a.py", "3|a.py");
    await b.open("p1", "b.py", "4|b.py");
    model("/w/a.py").type("x");
    // Pane by pane, neither holds the last view.
    expect([b.paneLoses(2), b.paneLoses(3)]).toEqual([false, false]);
    // Together they do.
    expect(b.panesLose([2, 3]).map((v) => v.path)).toEqual(["a.py"]);
    expect(b.panesHolding([2, 3, 4])).toEqual(["2", "3"]);
    expect(b.panesLose([2])).toEqual([]);
    expect(b.panesHolding([2, 4])).toEqual([]);   // pane 3, outside the set, still holds it
  });

  it("L1: one file read through two projects at once is one model, never a disposed one", async () => {
    w.uriRegistry = true;
    w.files.set("/w/src/main.py", "print('hi')\n");
    const [v1, v2] = await Promise.all([
      b.open("p1", "src/main.py", "2|src/main.py"),
      b.open("inbox", "w/src/main.py", "3|w/src/main.py"),
    ]);
    expect(v1.abs).toBe(v2.abs);
    expect(w.models.filter((m) => !m.isDisposed())).toHaveLength(1);
    const m = b.model("/w/src/main.py") as FakeModel;
    expect(m.isDisposed()).toBe(false);
    expect(b.get("/w/src/main.py")!.refs.sort()).toEqual(["2|src/main.py", "3|w/src/main.py"]);
  });

  it("L4: a reload in place never reads dirty, not even for a moment", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const seen: boolean[] = [];
    b.subscribe(() => seen.push(b.isDirty("/w/a.py")));
    w.files.set("/w/a.py", "one\nchanged\n");
    await b.poll();
    expect(b.text("/w/a.py")).toBe("one\nchanged\n");
    expect(seen.length).toBeGreaterThan(0);
    expect(seen).not.toContain(true);
  });

  it("MED1: a buffer the read calls not editable is never dirty and never saved, whatever its kind", async () => {
    w.files.set("/w/.env.utf16", "A=1\n");
    w.kinds.set("/w/.env.utf16", "env");
    w.uneditable.add("/w/.env.utf16");
    const v = await b.open("p1", ".env.utf16", "1|.env.utf16");
    expect([v.kind, v.editable]).toEqual(["env", false]);
    model("/w/.env.utf16").type("B=2");
    expect(b.isDirty("/w/.env.utf16")).toBe(false);
    expect(await b.save("/w/.env.utf16")).toBe("skipped");
    expect(w.writes).toHaveLength(0);
  });

  it("L2: a kind this window does not know opens read-only", async () => {
    w.kinds.set("/w/a.py", "brand-new-kind");
    const v = await b.open("p1", "a.py", "1|a.py");
    expect([v.kind, v.editable]).toEqual(["unknown", false]);
    model("/w/a.py").type("x");
    expect(b.isDirty("/w/a.py")).toBe(false);
  });

  it("Ctrl+S during a save waits for the first, then saves once more against the etag it returned (#49 LOW-2)", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    w.hold = true;                                         // a slow disk: /mnt/c
    const first = b.save("/w/a.py");
    model("/w/a.py").type("B");
    const second = b.save("/w/a.py");
    const third = b.save("/w/a.py");                       // a third press joins the same follow-up
    expect(third).toBe(second);
    await Promise.resolve();
    expect(w.writes).toHaveLength(1);                      // nothing else on its way yet: never a stale etag
    expect(b.get("/w/a.py")!.saving).toBe(true);
    w.hold = false;
    w.release();
    expect(await first).toBe("saved");
    expect(await second).toBe("saved");
    expect(w.writes).toHaveLength(2);                      // exactly one follow-up
    expect(w.writes[1]).toMatchObject({ content: "one\nAB", expected_etag: tagOf("one\nA") });
    expect(w.files.get("/w/a.py")).toBe("one\nAB");
    expect(b.get("/w/a.py")!.conflict).toBeNull();
    expect(b.isDirty("/w/a.py")).toBe(false);
    expect(b.get("/w/a.py")!.saving).toBe(false);
  });

  it("double Ctrl+S with nothing typed between: one PUT, no false conflict", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    w.hold = true;
    const first = b.save("/w/a.py");
    const second = b.save("/w/a.py");
    w.hold = false;
    w.release();
    expect([await first, await second]).toEqual(["saved", "saved"]);
    expect(w.writes).toHaveLength(1);
    expect(b.get("/w/a.py")!.conflict).toBeNull();
    expect(b.isDirty("/w/a.py")).toBe(false);
  });

  it("a first save that 409s runs no follow-up: the conflict bar, and the edit kept", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    w.hold = true;
    const first = b.save("/w/a.py");
    w.files.set("/w/a.py", "theirs\n");                   // someone else wrote it first
    model("/w/a.py").type("B");
    const second = b.save("/w/a.py");
    w.hold = false;
    w.release();
    expect([await first, await second]).toEqual(["conflict", "conflict"]);
    expect(w.writes).toHaveLength(1);
    expect(w.files.get("/w/a.py")).toBe("theirs\n");
    expect(b.get("/w/a.py")!.conflict).toMatchObject({ state: "changed", etag: tagOf("theirs\n") });
    expect(b.isDirty("/w/a.py")).toBe(true);
    expect(b.get("/w/a.py")!.saving).toBe(false);
  });

  it("a poll whose stat crossed a save raises no conflict against the owner's own write", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    let answerStat: () => void = () => {};
    const stat = w.deps.stat;
    // The stat reads the disk before the slow write lands, and answers after it.
    w.deps.stat = async (projectId, paths) => {
      const before = await stat(projectId, paths);
      await new Promise<void>((resolve) => (answerStat = resolve));
      return before;
    };
    w.hold = true;
    const saving = b.save("/w/a.py");
    const polled = b.poll();
    await Promise.resolve();
    w.hold = false;
    w.release();
    expect(await saving).toBe("saved");
    model("/w/a.py").type("B");                            // typing on after the save
    answerStat();
    await polled;
    expect(b.get("/w/a.py")!.conflict).toBeNull();
  });

  it("a late save answer never lands on a newer buffer for the same file", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    const notes: string[] = [];
    b.onNotice((_abs, t) => notes.push(t));
    w.hold = true;
    const late = b.save("/w/a.py");
    b.release("/w/a.py", "1|a.py", { discard: true });   // thrown away while the save was out
    w.hold = false;
    const again = b.open("p1", "a.py", "2|a.py");          // and read again: a new buffer
    await again;
    const fresh = b.get("/w/a.py")!;
    w.release();
    await late;
    expect(notes).not.toContain("saved");
    expect(b.get("/w/a.py")!.etag).toBe(fresh.etag);
  });

  it("a 409 whose etag is not an etag, and no stat to ask, tells the owner", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("x");
    const notes: string[] = [];
    b.onNotice((_abs, t) => notes.push(t));
    w.deps.write = async () => {
      throw new ApiError(409, "changed", { error: "changed", etag: 12345 });
    };
    w.deps.stat = async () => {
      throw new ApiError(500, "down");
    };
    expect(await b.save("/w/a.py")).toBe("conflict");
    expect(notes).toContain("changed on disk since it was read");
  });

  it("L3: saveAll saves every unsaved edit, one no tab holds included", async () => {
    await b.open("p1", "a.py", "2|a.py");
    await b.open("p1", "b.py", "3|b.py");
    model("/w/a.py").type("x");
    model("/w/b.py").type("y");
    b.release("/w/a.py", "2|a.py");                        // the workspace crashed: no tab holds it
    expect(b.dirtyCount()).toBe(2);
    expect(await b.saveAll()).toBe(true);
    expect([w.files.get("/w/a.py"), w.files.get("/w/b.py")]).toEqual(["one\nx", "two\ny"]);
    expect(b.dirtyCount()).toBe(0);
  });

  it("the writer's toast: an absolute path only", async () => {
    await b.open("p1", "a.py", "1|a.py");
    const notes: string[] = [];
    b.onNotice((_abs, t) => notes.push(t));
    b.noteTouch("a.py", "a relative path");
    w.files.set("/w/a.py", "moved\n");
    await b.poll();
    expect(notes).toEqual(["changed on disk — reloaded"]);
  });
});

describe("ED-7: a rename from the explorer", () => {
  it("the buffer follows with its edit, its undo stack and every hold", async () => {
    await b.open("p1", "a.py", refOf(2, "a.py"));
    await b.open("inbox", "w/a.py", refOf(4, "w/a.py"));
    const m = model("/w/a.py");
    m.type("edit");
    expect(b.isDirty("/w/a.py")).toBe(true);
    // The disk moves; then the registry is told.
    w.files.set("/w/c.py", w.files.get("/w/a.py")!);
    w.files.delete("/w/a.py");
    const { moves } = b.renamed("/w/a.py", "/w/c.py", "text");
    expect(moves.sort((x, y) => x.pane.localeCompare(y.pane))).toEqual([
      { pane: "2", from: "a.py", to: "c.py" }, { pane: "4", from: "w/a.py", to: "w/c.py" }]);
    expect(b.get("/w/a.py")).toBeNull();
    const v = b.get("/w/c.py")!;
    expect(v.dirty).toBe(true);
    expect(v.path).toBe("c.py");
    expect(v.refs.sort()).toEqual(["2|c.py", "4|w/c.py"]);
    expect(b.model("/w/c.py")).toBe(m);
    // A tab on the new name finds the same buffer, with no read.
    const reads = w.reads.length;
    const again = await b.open("p1", "c.py", refOf(2, "c.py"));
    expect(again.abs).toBe("/w/c.py");
    expect(w.reads.length).toBe(reads);
    // Its save writes the new name, against the etag it was read as.
    expect(await b.save("/w/c.py")).toBe("saved");
    expect(w.files.get("/w/c.py")).toBe("one\nedit");
    expect(w.files.has("/w/a.py")).toBe(false);
    // The old name's release is nobody's.
    b.release("/w/a.py", refOf(2, "a.py"));
    expect(b.get("/w/c.py")!.refs.length).toBe(2);
  });

  it("a folder's buffers all follow; others stay", async () => {
    w.files.set("/w/src/x.cpp", "x\n");
    w.files.set("/w/src/deep/y.cpp", "y\n");
    await b.open("p1", "src/x.cpp", refOf(1, "src/x.cpp"));
    await b.open("p1", "src/deep/y.cpp", refOf(1, "src/deep/y.cpp"));
    await b.open("p1", "a.py", refOf(1, "a.py"));
    model("/w/src/deep/y.cpp").type("!");
    const { moves } = b.renamed("/w/src", "/w/lib");
    expect(moves.map((x) => x.to).sort()).toEqual(["lib/deep/y.cpp", "lib/x.cpp"]);
    expect(b.get("/w/lib/deep/y.cpp")!.dirty).toBe(true);
    expect(b.get("/w/a.py")!.path).toBe("a.py");
    expect(b.renamed("/w/srcx", "/w/q")).toEqual({ moves: [], kept: [] });
  });

  it("a file renamed to an env name becomes an env file", async () => {
    await b.open("p1", "a.py", refOf(1, "a.py"));
    b.renamed("/w/a.py", "/w/.env.local", "env");
    expect(b.get("/w/.env.local")!.kind).toBe("env");
    // A kind it cannot turn into is never taken.
    await b.open("p1", "b.py", refOf(1, "b.py"));
    b.renamed("/w/b.py", "/w/b2.py", "gate");
    expect(b.get("/w/b2.py")!.kind).toBe("text");
  });
});

describe("the delta review of PR #49 (each failing on 62fc44d)", () => {
  it("MED-1 P1: Discard and reload during a slow save, Ctrl+S queued: the disk keeps A, never reverted", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    w.hold = true;                                         // a slow disk: /mnt/c
    const first = b.save("/w/a.py");
    model("/w/a.py").type("B");
    const second = b.save("/w/a.py");                      // the follow-up, queued
    expect(await b.reload("/w/a.py")).toBe(true);          // Discard and reload: reads O, the write not landed
    expect(model("/w/a.py").getValue()).toBe("one\n");
    w.hold = false;
    w.release();                                           // the write lands
    expect(await first).toBe("saved");
    await second;
    expect(w.writes).toHaveLength(1);                      // the follow-up wrote nothing
    expect(w.files.get("/w/a.py")).toBe("one\nA");
    await b.poll();                                        // and the buffer follows the disk
    expect(model("/w/a.py").getValue()).toBe("one\nA");
    expect(b.isDirty("/w/a.py")).toBe(false);
  });

  it("MED-1 P2: Discard and reload during a slow save, nothing queued: the poll brings the buffer to A", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    w.hold = true;
    const first = b.save("/w/a.py");
    expect(await b.reload("/w/a.py")).toBe(true);
    w.hold = false;
    w.release();
    expect(await first).toBe("saved");
    expect(w.files.get("/w/a.py")).toBe("one\nA");
    await b.poll();
    expect(model("/w/a.py").getValue()).toBe("one\nA");
    expect(b.isDirty("/w/a.py")).toBe(false);
    expect(b.get("/w/a.py")!.etag).toBe(tagOf("one\nA"));
    expect(b.get("/w/a.py")!.conflict).toBeNull();
  });

  it("MED-1 P3: a follow-up queued before Discard and reload never saves what is typed after it", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    const write = w.deps.write;
    let answer: () => void = () => {};
    let slow = true;
    w.deps.write = async (projectId, body, signal) => {
      const r = await write(projectId, body, signal);      // lands on disk at once…
      if (slow) {
        slow = false;
        await new Promise<void>((resolve) => (answer = resolve));  // …and the first answers late
      }
      return r;
    };
    const first = b.save("/w/a.py");
    model("/w/a.py").type("B");
    const second = b.save("/w/a.py");                      // the follow-up, for B
    expect(await b.reload("/w/a.py")).toBe(true);          // Discard and reload reads A
    expect(model("/w/a.py").getValue()).toBe("one\nA");
    model("/w/a.py").type("C");                            // typed after it: never Ctrl+S'd
    answer();
    expect(await first).toBe("saved");
    await second;
    expect(w.writes).toHaveLength(1);
    expect(w.files.get("/w/a.py")).toBe("one\nA");
    expect(b.isDirty("/w/a.py")).toBe(true);
  });

  it("LOW-1: a write that never answers ends as an error after the timeout; the text kept, the poll back", async () => {
    vi.useFakeTimers();
    try {
      await b.open("p1", "a.py", "1|a.py");
      model("/w/a.py").type("x");
      const notes: string[] = [];
      b.onNotice((_abs, t) => notes.push(t));
      const write = w.deps.write;
      let signal: AbortSignal | undefined;
      w.deps.write = (_p, _body, s) => {
        signal = s;
        return new Promise(() => {});                      // the PUT never answers
      };
      const saving = b.save("/w/a.py");
      expect(b.get("/w/a.py")!.saving).toBe(true);
      await vi.advanceTimersByTimeAsync(WRITE_TIMEOUT_MS - 1);
      expect(b.get("/w/a.py")!.saving).toBe(true);
      await vi.advanceTimersByTimeAsync(1);
      expect(b.get("/w/a.py")!.saving).toBe(false);
      expect(await saving).toBe("error");
      expect(signal?.aborted).toBe(true);
      expect(notes).toContain("the save didn't answer; your text is still here, press Ctrl+S to try again");
      expect(model("/w/a.py").getValue()).toBe("one\nx");
      expect(b.isDirty("/w/a.py")).toBe(true);
      w.files.set("/w/a.py", "theirs\n");                  // the poll is no longer held off
      await b.poll();
      expect(b.get("/w/a.py")!.conflict).toMatchObject({ state: "changed", etag: tagOf("theirs\n") });
      w.deps.write = write;                                // and Ctrl+S tries again
      w.files.set("/w/a.py", "one\n");
      await b.poll();
      expect(await b.save("/w/a.py")).toBe("saved");
      expect(w.files.get("/w/a.py")).toBe("one\nx");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ED-7: the status bar reads the registry's save state", () => {
  it("saving while a write is out; queued while a second Ctrl+S waits for it", async () => {
    await b.open("p1", "a.py", "1|a.py");
    model("/w/a.py").type("A");
    w.hold = true;
    const first = b.save("/w/a.py");
    expect(b.get("/w/a.py")!.saving).toBe(true);
    expect(b.get("/w/a.py")!.queued).toBe(false);
    model("/w/a.py").type("B");
    const second = b.save("/w/a.py");
    expect(b.get("/w/a.py")!.queued).toBe(true);
    w.hold = false;
    w.release();
    await first;
    w.release();
    await second;
    expect(b.get("/w/a.py")!.saving).toBe(false);
    expect(b.get("/w/a.py")!.queued).toBe(false);
  });
});

describe("review of PR #53, MED 2: a rename never takes a buffer's place", () => {
  it("an unsaved edit at the new name survives; the renamed buffer stays put", async () => {
    w.files.set("/w/notes.txt", "notes\n");
    w.files.set("/w/draft.txt", "draft\n");
    await b.open("p1", "notes.txt", refOf(1, "notes.txt"));
    await b.open("p1", "draft.txt", refOf(1, "draft.txt"));
    model("/w/notes.txt").type("EDIT");
    // notes.txt deleted on disk (its edit stays), draft.txt renamed onto its name
    w.files.delete("/w/notes.txt");
    w.files.set("/w/notes.txt", w.files.get("/w/draft.txt")!);
    w.files.delete("/w/draft.txt");
    const got = b.renamed("/w/draft.txt", "/w/notes.txt", "text");
    expect(got.moves).toEqual([]);
    // Its tab stays on draft.txt (the delta review's NIT): App renames nothing it holds.
    expect(got.kept).toEqual([{ pane: "1", path: "draft.txt" }]);
    expect(b.isDirty("/w/notes.txt")).toBe(true);
    expect(b.text("/w/notes.txt")).toBe("notes\nEDIT");
    expect(b.anyDirty()).toBe(true);
    expect(b.get("/w/draft.txt")).not.toBeNull();
  });

  it("a clean buffer at the new name gives way, its tabs kept on the renamed file", async () => {
    w.files.set("/w/notes.txt", "notes\n");
    w.files.set("/w/draft.txt", "draft\n");
    await b.open("p1", "notes.txt", refOf(2, "notes.txt"));
    await b.open("p1", "draft.txt", refOf(1, "draft.txt"));
    const old = model("/w/notes.txt");
    b.renamed("/w/draft.txt", "/w/notes.txt", "text");
    expect(old.isDisposed()).toBe(true);
    expect(b.text("/w/notes.txt")).toBe("draft\n");
    expect(b.get("/w/notes.txt")!.refs.sort()).toEqual(["1|notes.txt", "2|notes.txt"]);
    expect(b.snapshot()).toHaveLength(1);
  });
});
