import { describe, expect, it } from "vitest";
import { dirtyKey, dirtyPathsIn, marksOf, marksOfKey, rowDot, visibleDots, type DirtyBuffer } from "./dirtyMarks";

const buf = (abs: string, projectId: string, path: string, dirty = true): DirtyBuffer => ({ abs, projectId, path, dirty });

describe("which files a project's tree marks dirty", () => {
  it("its own buffers by the path they were read as; clean ones never", () => {
    const bufs = [
      buf("/w/src/robot/drive.cpp", "p1", "src/robot/drive.cpp"),
      buf("/w/calc.py", "p1", "calc.py", false),
      buf("/w/README.md", "p1", "README.md"),
    ];
    expect(dirtyPathsIn(bufs, "p1", "/w")).toEqual(["README.md", "src/robot/drive.cpp"]);
  });

  it("a file read through another project, by where it sits under this one's root", () => {
    // One file on disk is one buffer, saved through the project that read it.
    const bufs = [buf("/w/src/main.py", "inbox", "w/src/main.py")];
    expect(dirtyPathsIn(bufs, "p1", "/w")).toEqual(["src/main.py"]);
    expect(dirtyPathsIn(bufs, "p1", "/w/")).toEqual(["src/main.py"]);
    expect(dirtyPathsIn(bufs, "p1", null)).toEqual([]);
    // A root of "/" holds every absolute path.
    expect(dirtyPathsIn([buf("/w/a.py", "p1", "a.py")], "inbox", "/")).toEqual(["w/a.py"]);
  });

  it("a sibling folder that shares the root's prefix is not under it", () => {
    expect(dirtyPathsIn([buf("/work/a.py", "p2", "a.py")], "p1", "/w")).toEqual([]);
    expect(dirtyPathsIn([buf("/w", "p2", "x")], "p1", "/w")).toEqual([]);
  });

  it("the key is equal exactly when the dirty set is", () => {
    const a = dirtyKey(dirtyPathsIn([buf("/w/b", "p1", "b"), buf("/w/a", "p1", "a")], "p1", "/w"));
    const b = dirtyKey(dirtyPathsIn([buf("/w/a", "p1", "a"), buf("/w/b", "p1", "b")], "p1", "/w"));
    expect(a).toBe(b);
    expect(dirtyKey(["a"])).not.toBe(dirtyKey(["a", "b"]));
    expect(marksOfKey("").size).toBe(0);
    expect(marksOfKey(a).file("b")).toBe(true);
  });
});

describe("which rows wear the dot", () => {
  const marks = marksOf(["src/robot/drive.cpp", "README.md"]);

  it("a dirty file, and every folder above it at any depth", () => {
    expect(marks.file("src/robot/drive.cpp")).toBe(true);
    expect(marks.file("README.md")).toBe(true);
    expect(marks.file("calc.py")).toBe(false);
    expect(marks.folder("src")).toBe(true);
    expect(marks.folder("src/robot")).toBe(true);
    expect(marks.folder("src/rob")).toBe(false); // a prefix of a name is not a folder above it
    expect(marks.folder("lib")).toBe(false);
  });

  it("a folder wears it only while collapsed; an open one's dirty child does", () => {
    expect(rowDot(marks, "src", "dir", false)).toBe(true);
    expect(rowDot(marks, "src", "dir", true)).toBe(false);
    expect(rowDot(marks, "src/robot/drive.cpp", "file", true)).toBe(true);
  });

  it("given what is expanded, each dirty file's dot lands on the outermost collapsed folder above it", () => {
    const dirty = ["src/robot/drive.cpp", "src/main.py", "README.md"];
    expect(visibleDots(dirty, new Set())).toEqual(new Set(["src", "README.md"]));
    expect(visibleDots(dirty, new Set(["src"]))).toEqual(new Set(["src/robot", "src/main.py", "README.md"]));
    expect(visibleDots(dirty, new Set(["src", "src/robot"])))
      .toEqual(new Set(["src/robot/drive.cpp", "src/main.py", "README.md"]));
    // An expanded folder inside a collapsed one is still hidden: the outer one wears it.
    expect(visibleDots(dirty, new Set(["src/robot"]))).toEqual(new Set(["src", "README.md"]));
    expect(visibleDots([], new Set(["src"]))).toEqual(new Set());
  });
});
