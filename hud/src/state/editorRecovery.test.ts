import { beforeEach, describe, expect, it, vi } from "vitest";
import { Buffers, PlainModel, type BufferDeps } from "./buffers";
import { dirtyPathsIn, visibleDots } from "../lib/dirtyMarks";

const abs = "/shared/source.txt";
let b: Buffers;
let deps: BufferDeps;
let removed: Set<string>;
let saved: string;
beforeEach(() => {
  b = new Buffers(); removed = new Set(); saved = "original";
  deps = {
    read: vi.fn(async (projectId: string, path: string) => {
      if (removed.has(projectId)) throw new Error("project deleted");
      return { abs, path, content: saved, kind: "text", protected: false, editable: true, etag: "a".repeat(32) };
    }),
    write: vi.fn(async (projectId: string, body) => {
      if (removed.has(projectId)) throw new Error("project deleted");
      saved = body.content; return { etag: "b".repeat(32) };
    }),
    stat: vi.fn(async () => ({ files: {} })),
    makeModel: async text => new PlainModel(text), now: () => 1000,
  };
  b.configure(deps);
});
const edit = (text = "edited") => (b.model(abs) as PlainModel).setValue(text);
const deleted = (project: string) => { removed.add(project); b.projectDeleted(project); };

describe("editor recovery follow-ups", () => {
  it("saves the shared edit through the surviving project after permanent deletion", async () => {
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("second", "folder/source.txt", "2|folder/source.txt");
    const model = b.model(abs); edit();
    deleted("first"); b.releasePane(1);
    expect(await b.save(abs)).toBe("saved");
    expect(deps.write).toHaveBeenLastCalledWith("second", expect.objectContaining({ path: "folder/source.txt", content: "edited", expected_etag: "a".repeat(32) }), expect.anything());
    expect(b.model(abs)).toBe(model); expect(b.isDirty(abs)).toBe(false);
  });
  it("marks both verified same-project aliases and their collapsed parents", async () => {
    await b.open("same", "one/link.txt", "1|one/link.txt");
    await b.open("same", "two/source.txt", "2|two/source.txt"); edit();
    expect(dirtyPathsIn(b.snapshot(), "same", "/elsewhere")).toEqual(["one/link.txt", "two/source.txt"]);
    expect(visibleDots(dirtyPathsIn(b.snapshot(), "same"), new Set())).toEqual(new Set(["one", "two"]));
    await b.save(abs);
    expect(dirtyPathsIn(b.snapshot(), "same")).toEqual([]);
  });
  it("keeps archive routes valid and does not change the first route on ordinary release", async () => {
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("second", "other/source.txt", "2|other/source.txt"); edit();
    // Archive hides a project's pane but does not remove its record or file routes.
    b.releasePane(1);
    expect(await b.save(abs)).toBe("saved");
    expect(deps.write).toHaveBeenLastCalledWith("first", expect.objectContaining({ path: "source.txt" }), expect.anything());
  });
  it("never chooses a released alias; keeps the edit when no live holder can save", async () => {
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("released", "unused/source.txt", "2|unused/source.txt"); edit();
    b.releasePane(2); deleted("first");
    expect(await b.save(abs)).toBe("error"); expect(deps.write).not.toHaveBeenCalled();
    expect(b.text(abs)).toBe("edited"); expect(b.isDirty(abs)).toBe(true);
    await b.open("new", "fresh/source.txt", "3|fresh/source.txt");
    expect(await b.save(abs)).toBe("saved");
    expect(deps.write).toHaveBeenLastCalledWith("new", expect.objectContaining({ path: "fresh/source.txt" }), expect.anything());
  });
  it("cannot resurrect a deleted project's cached open", async () => {
    await b.open("first", "source.txt", "1|source.txt"); edit(); deleted("first");
    await expect(b.open("first", "source.txt", "2|source.txt")).rejects.toThrow();
    expect(b.text(abs)).toBe("edited");
  });
  it("rejects an open that answered after project deletion", async () => {
    let release!: () => void;
    const originalRead = deps.read;
    deps.read = vi.fn(async (project, path) => {
      const row = await originalRead(project, path);
      await new Promise<void>(r => { release = r; }); return row;
    });
    const opening = b.open("first", "source.txt", "1|source.txt");
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    deleted("first"); release();
    await expect(opening).rejects.toThrow();
    expect(b.snapshot()).toHaveLength(0);
  });
  it("a delayed reload from the deleted route cannot replace surviving text", async () => {
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("second", "other/source.txt", "2|other/source.txt"); edit();
    let release!: () => void;
    deps.read = vi.fn(async (_project, path) => {
      await new Promise<void>(r => { release = r; });
      return { abs, path, content: "stale", kind: "text", protected: false, editable: true, etag: "c".repeat(32) };
    });
    const reload = b.reload(abs); deleted("first"); release();
    expect(await reload).toBe(false); expect(b.text(abs)).toBe("edited");
    expect(b.get(abs)?.etag).toBe("a".repeat(32));
  });
  it("an already successful write updates the shared etag and queued save uses the survivor", async () => {
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("second", "other/source.txt", "2|other/source.txt"); edit("edit one");
    let release!: () => void;
    deps.write = vi.fn(async (_project, body) => {
      if (body.content === "edit one") await new Promise<void>(r => { release = r; });
      saved = body.content; return { etag: body.content === "edit one" ? "b".repeat(32) : "c".repeat(32) };
    });
    const first = b.save(abs); edit("edit two"); const queued = b.save(abs);
    deleted("first"); release();
    expect(await first).toBe("saved"); expect(await queued).toBe("saved");
    expect(deps.write).toHaveBeenNthCalledWith(2, "second", expect.objectContaining({ path: "other/source.txt", content: "edit two", expected_etag: "b".repeat(32) }), expect.anything());
    expect(b.text(abs)).toBe("edit two"); expect(b.isDirty(abs)).toBe(false);
  });

  it("a delayed stat or Compare response from the deleted route is ignored", async () => {
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("second", "other/source.txt", "2|other/source.txt"); edit();
    let releaseStat!: () => void; let releaseRead!: () => void;
    deps.stat = vi.fn(async () => {
      await new Promise<void>(r => { releaseStat = r; });
      return { files: { "source.txt": { gone: true } } };
    });
    deps.read = vi.fn(async (_project, path) => {
      await new Promise<void>(r => { releaseRead = r; });
      return { abs, path, content: "stale", kind: "text", protected: false, editable: true, etag: "c".repeat(32) };
    });
    const poll = b.poll(); const compare = b.readDisk(abs);
    deleted("first"); releaseStat(); releaseRead();
    await poll; expect(await compare).toBeNull();
    expect(b.get(abs)?.conflict).toBeNull(); expect(b.text(abs)).toBe("edited");
  });
  it("rename retains the surviving verified holder and model", async () => {
    const file = "/shared/source.txt";
    await b.open("first", "source.txt", "1|source.txt");
    await b.open("second", "shared/source.txt", "2|shared/source.txt"); edit();
    const original = b.model(file);
    b.renamed(file, "/shared/renamed.txt"); deleted("first");
    expect(b.get("/shared/renamed.txt")?.refs).toEqual(["1|renamed.txt", "2|shared/renamed.txt"]);
    expect(b.get("/shared/renamed.txt")?.path).toBe("shared/renamed.txt");
    expect(b.model("/shared/renamed.txt")).toBe(original);
    expect(await b.save("/shared/renamed.txt")).toBe("saved");
    expect(deps.write).toHaveBeenLastCalledWith("second", expect.objectContaining({ path: "shared/renamed.txt" }), expect.anything());
  });

});
