import { describe, expect, it } from "vitest";
import { gitKey, gitMarksOf, gitMarksOfKey, NO_GIT } from "./gitMarks";

const status = {
  files: {
    "src/robot/drive.cpp": "modified",
    "src/new.h": "untracked",
    "README.md": "added",
    "docs/old.md": "deleted",
    "debug.log": "ignored",
    "lib/ignored-only/x.o": "ignored",
    "merge/me.txt": "conflict",
    "moved.txt": "renamed",
  },
  dirs: { build: "ignored", "scratch": "untracked" },
};

describe("which state a tree row wears", () => {
  const g = gitMarksOf(status);

  it("a file wears its own state; a clean one none", () => {
    expect(g.file("src/robot/drive.cpp")).toBe("modified");
    expect(g.file("README.md")).toBe("added");
    expect(g.file("debug.log")).toBe("ignored");
    expect(g.file("moved.txt")).toBe("renamed");
    expect(g.file("src/clean.cpp")).toBeNull();
    expect(g.file("/src/robot/drive.cpp/")).toBe("modified");
  });

  it("a file inside a folder git reported whole wears that folder's state", () => {
    expect(g.file("build/out/main.o")).toBe("ignored");
    expect(g.file("scratch/a.txt")).toBe("untracked");
    expect(g.folder("build")).toBe("ignored");
    expect(g.folder("build/out")).toBe("ignored");
  });

  it("a folder wears the strongest state under it; ignored files never colour one", () => {
    expect(g.folder("src")).toBe("modified");
    expect(g.folder("src/robot")).toBe("modified");
    expect(g.folder("docs")).toBe("modified"); // a deleted file is a change
    expect(g.folder("merge")).toBe("conflict");
    expect(g.folder("lib")).toBeNull();
    expect(g.folder("lib/ignored-only")).toBeNull();
    expect(g.folder("elsewhere")).toBeNull();
    const added = gitMarksOf({ files: { "a/x": "untracked", "a/y": "added" } });
    expect(added.folder("a")).toBe("added");
  });

  it("the project's own folder reported whole colours everything in it", () => {
    const all = gitMarksOf({ files: {}, dirs: { "": "untracked" } });
    expect(all.file("main.cpp")).toBe("untracked");
    expect(all.folder("src")).toBe("untracked");
  });

  it("garbage is ignored, and no status is no marks", () => {
    expect(gitMarksOf(null)).toBe(NO_GIT);
    expect(gitMarksOf({ files: { a: "exploded", b: 3 as any }, dirs: null })).toBe(NO_GIT);
  });
});

describe("the key a tree compares", () => {
  it("equal exactly when the states are, whatever order they came in", () => {
    const a = gitKey({ files: { b: "modified", a: "added" }, dirs: {} });
    const b = gitKey({ files: { a: "added", b: "modified" }, dirs: {} });
    expect(a).toBe(b);
    expect(gitKey({ files: { a: "added", b: "untracked" } })).not.toBe(a);
    expect(gitKey({ files: {}, dirs: {} })).toBe("");
    expect(gitKey(null)).toBe("");
  });

  it("round-trips through the key", () => {
    const g = gitMarksOfKey(gitKey(status));
    expect(g.folder("src")).toBe("modified");
    expect(g.file("build/x")).toBe("ignored");
    expect(gitMarksOfKey("not json")).toBe(NO_GIT);
  });
});
