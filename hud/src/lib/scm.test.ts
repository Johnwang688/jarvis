import { describe, expect, it } from "vitest";
import {
  absUnder, baseSide, diffMode, diffWhy, readSide, revealTop, rowShows, SCM_LETTER, SCM_OVERSCAN, SCM_ROW_PX,
  SCM_UNMEASURED_ROWS, scmKeyStep, scmRows, scmSummary, scmWindow, wholeUntracked,
} from "./scm";

const P = ["p1", "p2"];
const T = ["k1"];
const mode = (o: Partial<Parameters<typeof diffMode>[0]>) =>
  diffMode({ pin: null, projectIds: P, taskId: null, taskFocus: false, taskShown: false, taskIds: T, follow: "p1", ...o });

describe("which mode a Diff pane is in", () => {
  it("with no task, the chat's project's uncommitted changes", () => {
    expect(mode({})).toEqual({ kind: "project", projectId: "p1", pinned: false });
  });
  it("a task the window is about shows its diff, as before", () => {
    expect(mode({ taskId: "k1", taskFocus: true })).toEqual({ kind: "task", taskId: "k1", why: "focus" });
  });
  it("a task picked before a thread no longer decides: the chat's project", () => {
    expect(mode({ taskId: "k1", taskFocus: false })).toEqual({ kind: "project", projectId: "p1", pinned: false });
  });
  it("a task that is gone is not shown", () => {
    expect(mode({ taskId: "gone", taskFocus: true })).toEqual({ kind: "project", projectId: "p1", pinned: false });
    expect(mode({ taskId: "gone", taskShown: true })).toEqual({ kind: "project", projectId: "p1", pinned: false });
  });
  it("a pin beats the chat's project; a task picked beats the pin", () => {
    expect(mode({ pin: "p2" })).toEqual({ kind: "project", projectId: "p2", pinned: true });
    expect(mode({ pin: "p2", taskId: "k1", taskFocus: false })).toEqual({ kind: "project", projectId: "p2", pinned: true });
    // A File pane pins its project when it opens a file; switched to Diff, a
    // task picked must still show the task (hud_v2_csp_check's single pane).
    expect(mode({ pin: "p2", taskId: "k1", taskFocus: true })).toEqual({ kind: "task", taskId: "k1", why: "focus" });
  });
  it("never another task than a drawn Task pane: while one shows a task, the Diff shows it (review of PR #67, L3)", () => {
    // A thread picked since (taskFocus false): the Task pane still shows k1, so does the Diff.
    expect(mode({ taskId: "k1", taskFocus: false, taskShown: true })).toEqual({ kind: "task", taskId: "k1", why: "pane" });
    // Over a pin too.
    expect(mode({ pin: "p2", taskId: "k1", taskShown: true })).toEqual({ kind: "task", taskId: "k1", why: "pane" });
    // The Task pane shows nothing: the rest of the rule.
    expect(mode({ taskId: null, taskShown: true })).toEqual({ kind: "project", projectId: "p1", pinned: false });
  });
  it("the caption says why", () => {
    expect(diffWhy(mode({ taskId: "k1", taskShown: true }))).toBe("the Task pane's task");
    expect(diffWhy(mode({ taskId: "k1", taskFocus: true }))).toBe("the task picked last");
    expect(diffWhy(mode({ pin: "p2" }))).toBe("this pane's project");
    expect(diffWhy(mode({}))).toBe("the chat's project");
    expect(diffWhy(mode({ follow: null }))).toBe("");
  });
  it("a pin whose project went away follows again", () => {
    expect(mode({ pin: "archived" })).toEqual({ kind: "project", projectId: "p1", pinned: false });
  });
  it("nothing to follow: nothing to show", () => {
    expect(mode({ follow: null })).toEqual({ kind: "none" });
    expect(mode({ follow: "unknown" })).toEqual({ kind: "none" });
  });
});

describe("the rows", () => {
  const status = {
    repo: true,
    files: {
      "src/main.cpp": "modified", "added.txt": "added", "untracked.txt": "untracked", "src/robot.cpp": "renamed",
      "blob.bin": "deleted", "merge.txt": "conflict", "debug.log": "ignored", "odd.txt": "weird",
      ".env": "modified", "config/.env.local": "untracked", ".env.example": "modified",
    },
    dirs: { newdir: "untracked", build: "ignored" },
  };
  const rows = scmRows(status);
  const by = Object.fromEntries(rows.map((r) => [r.path, r]));

  it("every changed state, with its letter; ignored and unknown states never listed", () => {
    expect(by["src/main.cpp"].letter).toBe("M");
    expect(by["added.txt"].letter).toBe("A");
    expect(by["untracked.txt"].letter).toBe("U");
    expect(by["src/robot.cpp"].letter).toBe("R");
    expect(by["blob.bin"].letter).toBe("D");
    expect(by["merge.txt"].letter).toBe("C");
    expect(by["debug.log"]).toBeUndefined();
    expect(by["odd.txt"]).toBeUndefined();
    expect(by["build"]).toBeUndefined();
    expect(Object.values(SCM_LETTER).sort()).toEqual(["A", "C", "D", "M", "R", "U"]);
  });
  it("an untracked folder is one row, a folder; the project's own folder is said, not listed", () => {
    expect(by["newdir"]).toMatchObject({ folder: true, state: "untracked", letter: "U" });
    const whole = { repo: true, files: {}, dirs: { "": "untracked" } };
    expect(scmRows(whole)).toEqual([]);
    expect(wholeUntracked(whole)).toBe(true);
    expect(scmSummary(whole, [], null)).toMatch(/whole folder is untracked/);
  });
  it("sorted by path", () => {
    const paths = rows.map((r) => r.path);
    expect(paths).toEqual([...paths].sort());
  });
  it("env files are listed, and flagged by the owner's .env* rule", () => {
    expect(by[".env"].env).toBe(true);
    expect(by["config/.env.local"].env).toBe(true);
    expect(by[".env.example"].env).toBe(false);
    expect(by["src/main.cpp"].env).toBe(false);
  });
  it("an env row is never diffed", () => {
    for (const r of rows) if (r.env) expect(rowShows(r)).toBe("env");
    expect(rowShows({ env: true, folder: false })).toBe("env");
    expect(rowShows({ env: false, folder: true })).toBe("folder");
    expect(rowShows({ env: false, folder: false })).toBe("diff");
    expect(rowShows(by[".env.example"])).toBe("diff");
  });
  it("nothing, or something malformed, lists nothing", () => {
    expect(scmRows(null)).toEqual([]);
    expect(scmRows({ files: null, dirs: null })).toEqual([]);
    expect(scmRows({ files: { "": "modified", "/a/": "modified" } })).toEqual([
      { path: "a", state: "modified", letter: "M", folder: false, env: false },
    ]);
  });
});

describe("the line above the list", () => {
  it("says how many, on which branch, or why there are none", () => {
    expect(scmSummary(null, [], null)).toMatch(/Reading/);
    expect(scmSummary({ repo: false }, [], null)).toMatch(/Not a git repository/);
    expect(scmSummary({ repo: true, files: {} , branch: "main" } as any, [], null)).toBe("No uncommitted changes on main.");
    const rows = scmRows({ files: { a: "modified" } });
    expect(scmSummary({ repo: true, branch: "main" } as any, rows, null)).toBe("1 uncommitted change on main");
    expect(scmSummary({ repo: true, truncated: true } as any, rows, null)).toMatch(/TRUNCATED/);
    expect(scmSummary(null, [], "refused")).toMatch(/refused/);
  });
});

describe("the sides", () => {
  it("HEAD's side: its text, empty for a file HEAD lacks, a sentence instead of a diff", () => {
    expect(baseSide("ok", "a\n")).toEqual({ text: "a\n" });
    for (const s of ["new", "no_head", "no_repo"]) expect(baseSide(s, null)).toEqual({ text: "" });
    for (const s of ["binary", "too_large", "not_a_file", "unavailable", "anything"]) {
      expect("note" in baseSide(s, null)).toBe(true);
    }
  });
  it("the working side: text and safety shown, env never, the rest a sentence", () => {
    expect(readSide("text", false)).toBe("show");
    expect(readSide("safety", false)).toBe("show");
    expect(readSide("env", false)).toBe("env");
    expect(readSide("binary", false)).toBe("binary");
    expect(readSide("gate", false)).toBe("withheld");
    expect(readSide("withheld", true)).toBe("withheld");
    expect(readSide("text", true)).toBe("withheld");
    expect(readSide(undefined, false)).toBe("withheld");
  });
  it("where a project path resolves", () => {
    expect(absUnder("/home/o/proj/", "src/a.c")).toBe("/home/o/proj/src/a.c");
    expect(absUnder(null, "a")).toBeNull();
  });
});

// The Diff follow-ups (2026-10-12): the list at the status's 20,000-row cap is
// windowed — only the rows in view, and an overscan either side, are drawn.
describe("the change list's window", () => {
  const R = SCM_ROW_PX;
  it("draws only what is in view, and the overscan either side", () => {
    // 600 px in view: 25 rows, plus one for a part-row, plus the overscan below.
    expect(scmWindow(20000, 0, 600)).toEqual({ first: 0, last: 25 + 1 + SCM_OVERSCAN });
    const mid = scmWindow(20000, 1000 * R, 600);
    expect(mid).toEqual({ first: 1000 - SCM_OVERSCAN, last: 1000 + 26 + SCM_OVERSCAN });
    expect(mid.last - mid.first).toBeLessThan(60);
  });
  it("reaches the last row, and never past it", () => {
    const end = scmWindow(20000, (20000 - 25) * R, 600);
    expect(end.last).toBe(20000);
    // Scrolled past the end (the list shrank under it): the end is drawn, not nothing.
    expect(scmWindow(30, 1e6, 600)).toEqual({ first: 29 - SCM_OVERSCAN, last: 30 });
  });
  it("draws a fixed number of rows while nothing is measured, and nothing for no rows", () => {
    expect(scmWindow(20000, 0, 0)).toEqual({ first: 0, last: SCM_UNMEASURED_ROWS + SCM_OVERSCAN });
    expect(scmWindow(3, 0, 0)).toEqual({ first: 0, last: 3 });
    expect(scmWindow(0, 0, 600)).toEqual({ first: 0, last: 0 });
    expect(scmWindow(10, Number.NaN, 600)).toEqual({ first: 0, last: 10 });
  });
  it("scrolls a row into view only when it is out of it", () => {
    expect(revealTop(10, 0, 600)).toBeNull();
    expect(revealTop(30, 0, 600)).toBe(31 * R - 600);
    expect(revealTop(3, 500, 600)).toBe(3 * R);
    expect(revealTop(19999, 0, 600)).toBe(20000 * R - 600);
    expect(revealTop(5, 0, 0)).toBeNull();
    expect(revealTop(-1, 0, 600)).toBeNull();
  });
});

describe("the change list's keys", () => {
  it("move the selection a row, a page, or to either end, clamped", () => {
    expect(scmKeyStep("ArrowDown", 4, 100, 20)).toBe(5);
    expect(scmKeyStep("ArrowUp", 4, 100, 20)).toBe(3);
    expect(scmKeyStep("ArrowDown", 99, 100, 20)).toBe(99);
    expect(scmKeyStep("ArrowUp", 0, 100, 20)).toBe(0);
    expect(scmKeyStep("PageDown", 4, 100, 20)).toBe(24);
    expect(scmKeyStep("PageDown", 90, 100, 20)).toBe(99);
    expect(scmKeyStep("PageUp", 10, 100, 20)).toBe(0);
    expect(scmKeyStep("Home", 50, 100, 20)).toBe(0);
    expect(scmKeyStep("End", 50, 100, 20)).toBe(99);
  });
  it("with nothing selected, start at the top (End: the bottom)", () => {
    expect(scmKeyStep("ArrowDown", -1, 100, 20)).toBe(0);
    expect(scmKeyStep("ArrowUp", -1, 100, 20)).toBe(0);
    expect(scmKeyStep("End", -1, 100, 20)).toBe(99);
    expect(scmKeyStep("Enter", -1, 100, 20)).toBeNull();
  });
  it("Enter picks the selected row again; other keys and an empty list are not the list's", () => {
    expect(scmKeyStep("Enter", 7, 100, 20)).toBe(7);
    expect(scmKeyStep("a", 7, 100, 20)).toBeNull();
    expect(scmKeyStep(" ", 7, 100, 20)).toBeNull();
    expect(scmKeyStep("ArrowDown", 0, 0, 20)).toBeNull();
    // A selection past a list that shrank moves from its end.
    expect(scmKeyStep("ArrowUp", 500, 100, 20)).toBe(98);
    expect(scmKeyStep("PageDown", 4, 100, 0)).toBe(5);
  });
});
