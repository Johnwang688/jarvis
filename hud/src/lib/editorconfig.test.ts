import { describe, expect, it } from "vitest";
import {
  candidateDirs, configPath, deadline, globCost, GLOB_TOKENS_MAX, GLOB_TOTAL_MAX, matchGlob, overrideOf,
  parseEditorConfig, RESOLVE_MS, resolveProps, SECTIONS_MAX, type ConfigFile, type Deadline,
} from "./editorconfig";

// Load-proof timing (the flaky-test fix; `state/editorconfig.test.ts` has the
// same two tools). `held` is how long `fn` held the thread: the least of its
// wall time and the process's CPU time, each at least the thread's own CPU
// time, and only the first inflated by a CPU taken away under load (the
// thread's own CPU clock is read a scheduler tick stale on Linux, so it is no
// use at 16 ms). `workDeadline` reads the work done as its clock, so
// `RESOLVE_MS` is a fixed number of steps: a check whose point is not the time
// (a cap found, a small config's answer) cannot be timed out by a slow machine.
const cpuUsage = (globalThis as { process?: { cpuUsage?: () => { user: number; system: number } } }).process?.cpuUsage;
const cpuMs = () => {
  const u = cpuUsage!();
  return (u.user + u.system) / 1000;
};
function held(fn: () => void): number {
  const w = performance.now();
  const c = cpuUsage ? cpuMs() : 0;
  fn();
  const wall = performance.now() - w;
  return cpuUsage ? Math.min(wall, cpuMs() - c) : wall;
}
/** Nanoseconds one step stands for on the work clock: about this machine's rate (3–4 ns). */
const WORK_NS = 4;
function workDeadline(): Deadline {
  let d: Deadline | undefined;
  d = deadline(RESOLVE_MS, () => ((d?.total ?? 0) * WORK_NS) / 1e6);
  return d;
}

describe("parsing an .editorconfig (the spec's INI)", () => {
  it("reads the preamble's root, sections, pairs and comments", () => {
    const f = parseEditorConfig([
      "﻿# a comment",
      "; another",
      "root = TRUE",
      "",
      "[*]",
      "Indent_Style = Space",
      "indent_size=4",
      "  [*.{c,h}]  ",
      "indent_style = tab",
      "x_custom = KeepCase",
      "not a pair",
      "=no key",
    ].join("\r\n"));
    expect(f.root).toBe(true);
    expect(f.sections).toEqual([
      { glob: "*", props: { indent_style: "space", indent_size: "4" } },
      { glob: "*.{c,h}", props: { indent_style: "tab", x_custom: "KeepCase" } },
    ]);
  });

  it("root counts only in the preamble", () => {
    expect(parseEditorConfig("[*]\nroot = true\n").root).toBe(false);
    expect(parseEditorConfig("root = false\n[*]\n").root).toBe(false);
  });

  it("a section name may hold brackets; a broken header ends the section", () => {
    const f = parseEditorConfig("[[ab].c]\nindent_size = 2\n[broken\nindent_size = 9\n");
    expect(f.sections).toEqual([{ glob: "[ab].c", props: { indent_size: "2" } }]);
  });

  it("over-long keys and values are ignored", () => {
    const f = parseEditorConfig(`[*]\n${"k".repeat(1025)} = 1\nok = ${"v".repeat(4097)}\nfine = 1\n`);
    expect(f.sections[0].props).toEqual({ fine: "1" });
  });
});

describe("globs (the spec's table)", () => {
  const cases: [string, string, boolean][] = [
    // a glob with no slash matches at any depth
    ["*.py", "a.py", true], ["*.py", "src/deep/a.py", true], ["*.py", "a.pyc", false],
    ["*", "anything/at/all.txt", true],
    // `*` never crosses a folder; `**` does
    ["src/*.py", "src/a.py", true], ["src/*.py", "src/x/a.py", false],
    ["src/**.py", "src/x/y/a.py", true], ["src/**/a.py", "src/a.py", true], ["src/**/a.py", "src/x/y/a.py", true],
    ["**/a.py", "a.py", true],
    // a leading slash anchors at the file's folder
    ["/a.py", "a.py", true], ["/a.py", "x/a.py", false], ["lib/a.py", "x/lib/a.py", false],
    // ?, brackets
    ["?.c", "a.c", true], ["?.c", "ab.c", false], ["a?c", "a/c", false],
    ["[abc].txt", "b.txt", true], ["[abc].txt", "d.txt", false], ["[!abc].txt", "d.txt", true], ["[!abc].txt", "a.txt", false],
    ["[a-c].txt", "b.txt", true], ["[a-c].txt", "x.txt", false],
    ["file[/]x", "file[/]x", true], ["[unclosed", "[unclosed", true],
    // braces: alternatives (nested), ranges, a single word is literal
    ["*.{c,h}", "x.h", true], ["*.{c,h}", "x.cpp", false], ["{a,{b,c}}.md", "c.md", true],
    ["{single}.txt", "{single}.txt", true], ["{single}.txt", "single.txt", false],
    ["{}.txt", "{}.txt", true],
    ["file{1..3}.c", "file2.c", true], ["file{1..3}.c", "file4.c", false], ["f{-3..-1}", "f-2", true],
    ["f{10..2}", "f5", true], ["f{1..3}", "f", false],
    ["{a,{b,c}}{1..2}", "c2", true], ["{a,{b,c}}{1..2}", "c3", false],
    // escapes
    ["\\*.c", "*.c", true], ["\\*.c", "x.c", false], ["a\\{b,c\\}", "a{b,c}", true],
    // regex characters are literal
    ["a+b(c).txt", "a+b(c).txt", true], ["a.c", "abc", false], ["$^.txt", "$^.txt", true],
  ];
  it.each(cases)("%s against %s → %s", (glob, path, want) => {
    expect(matchGlob(glob, path)).toBe(want);
  });

  it("a deep brace nest, or too many alternatives, is no glob at all rather than a slow one", () => {
    expect(matchGlob("{x,".repeat(10) + "a" + "}".repeat(10), "a")).toBe(true);
    expect(matchGlob("{x,".repeat(40) + "a" + "}".repeat(40), "a")).toBe(false);
    expect(matchGlob("{a,b}".repeat(6), "ababab")).toBe(true); // 64 alternatives: the cap
    expect(matchGlob("{a,b}".repeat(7), "abababa")).toBe(false); // 128: past it
  });

  // An .editorconfig is project content an agent can write: a glob must never
  // be a way to hang the window that gates approvals (a backtracking regex
  // takes minutes on these).
  it("stars and question marks against a long path stay linear", () => {
    const t = performance.now();
    expect(matchGlob("*a*a*a*a*a*a*a*a*a*a*a*a*b", "a".repeat(1000))).toBe(false);
    expect(matchGlob("*?*?*?*?*?*?*?*?*?*?*?*?*?*?.zzz", "x".repeat(1000))).toBe(false);
    expect(matchGlob("**a**a**a**a**a**a**a**b", ("a/".repeat(400)).slice(0, 1000))).toBe(false);
    expect(performance.now() - t).toBeLessThan(500);
  });

  it("a path past the cap matches nothing", () => {
    expect(matchGlob("*", "a".repeat(1025))).toBe(false);
    expect(matchGlob("*", "a".repeat(1024))).toBe(true);
  });
});

const file = (text: string): ConfigFile => parseEditorConfig(text);

describe("resolving a file's properties", () => {
  it("nearer files win, later sections win, globs are relative to their file's folder", () => {
    const root = file("root = true\n[*]\nindent_style = space\nindent_size = 4\n[sub/*.c]\nindent_size = 8\n");
    const sub = file("[*.c]\nindent_style = tab\n");
    expect(resolveProps([{ dir: "", file: root }, { dir: "sub", file: sub }], "sub/x.c").props).toEqual({
      indent_style: "tab", indent_size: "8", tab_width: "8",
    });
    // sub's section does not reach a file outside sub
    expect(resolveProps([{ dir: "", file: root }, { dir: "sub", file: sub }], "x.c").props).toEqual({
      indent_style: "space", indent_size: "4", tab_width: "4",
    });
  });

  it("`unset` removes a property", () => {
    const f = file("[*]\nindent_size = 2\nend_of_line = lf\n[*.md]\nend_of_line = unset\n");
    expect(resolveProps([{ dir: "", file: f }], "a.md").props).toEqual({ indent_size: "2", tab_width: "2" });
  });

  it("the spec's indent rules: tab style means a tab's size; indent_size = tab takes tab_width", () => {
    expect(resolveProps([{ dir: "", file: file("[*]\nindent_style = tab\n") }], "a").props).toEqual({
      indent_style: "tab", indent_size: "tab",
    });
    expect(resolveProps([{ dir: "", file: file("[*]\nindent_style = tab\ntab_width = 3\n") }], "a").props).toEqual({
      indent_style: "tab", indent_size: "3", tab_width: "3",
    });
  });
});

describe("what the editor takes", () => {
  it("indent style and sizes, and LF or CRLF for new lines", () => {
    expect(overrideOf({ indent_style: "tab", indent_size: "4", tab_width: "4", end_of_line: "crlf" })).toEqual({
      insertSpaces: false, tabSize: 4, indentSize: 4, eol: "CRLF",
    });
    expect(overrideOf({ indent_style: "space", indent_size: "2", tab_width: "8" })).toEqual({
      insertSpaces: true, tabSize: 8, indentSize: 2,
    });
    expect(overrideOf({ end_of_line: "lf" })).toEqual({ eol: "LF" });
  });

  it("never anything that rewrites a file on save, nor CR (no Monaco model has it)", () => {
    expect(overrideOf({ trim_trailing_whitespace: "true", insert_final_newline: "true", charset: "latin1" })).toBeNull();
    expect(overrideOf({ end_of_line: "cr" })).toBeNull();
  });

  it("a size that is not a sane number is left to the file", () => {
    expect(overrideOf({ indent_size: "0" })).toBeNull();
    expect(overrideOf({ indent_size: "999" })).toBeNull();
    expect(overrideOf({ indent_size: "2x" })).toBeNull();
    expect(overrideOf({ indent_style: "tab", indent_size: "tab" })).toEqual({ insertSpaces: false });
  });
});

describe("where it looks", () => {
  it("the file's folder up to the project's root, nearest first, never above", () => {
    expect(candidateDirs("a/b/c.txt")).toEqual(["a/b", "a", ""]);
    expect(candidateDirs("c.txt")).toEqual([""]);
    expect(candidateDirs("a/b/c.txt").map(configPath)).toEqual(["a/b/.editorconfig", "a/.editorconfig", ".editorconfig"]);
    for (const d of candidateDirs("x/y/z/w.c")) expect(d.includes("..")).toBe(false);
  });

  it("a very deep file still ends at the root, within the cap", () => {
    const deep = Array.from({ length: 60 }, (_, i) => `d${i}`).join("/") + "/f.c";
    const dirs = candidateDirs(deep);
    expect(dirs.length).toBeLessThanOrEqual(32);
    expect(dirs[dirs.length - 1]).toBe("");
  });
});

// The review of PR #59: an agent-written `.editorconfig` must never freeze the
// window. The guard is a real clock (the lead's structural backstop): one
// resolution spends through one deadline, checked inside the parser, the
// compiler and the matcher, and stops at RESOLVE_MS with no configuration.
describe("a resolution is held to the clock", () => {
  /** The heaviest glob the per-glob caps allow: 64 alternatives, the most tokens each. */
  const heaviest = (n: number) => {
    const brace = "{a,b}".repeat(6);
    for (let k = 60; k > 0; k--) {
      const g = `${brace}${"*x".repeat(k)}y${n}`;
      if (globCost(g)) return g;
    }
    throw new Error("no glob fits the caps");
  };
  // The review's shape: 123 sections, each as heavy as the caps allow.
  const lines = ["root = true"];
  for (let n = 0; n < 123; n++) lines.push(`[${heaviest(n)}]`, "indent_size = 2");
  /** A fresh copy: compiled globs are kept on the file. */
  const heavy = () => parseEditorConfig(lines.join("\n"));
  const PATH = `src/${"robot_drive_subsystem_controller_for_the_left_side_".repeat(5)}.py`;
  /** A clock that moves one millisecond each time it is read: a time-out that does not depend on the machine. */
  const fake = () => {
    const c = { reads: 0 };
    return { c, d: deadline(RESOLVE_MS, () => c.reads++) };
  };

  it("the shape is what the review built: 123 sections of 64 alternatives", () => {
    const file = heavy();
    expect(file.sections).toHaveLength(123);
    expect(globCost(file.sections[0].glob)).toMatchObject({ alts: 64 });
  });

  it("stops at the deadline: no configuration, and the file to blame named", () => {
    let r: ReturnType<typeof resolveProps> | undefined;
    const ms = held(() => {
      r = resolveProps([{ dir: "", file: heavy() }], PATH);
    });
    expect(r).toMatchObject({ over: "time", props: {}, culprit: 0 });
    expect(ms).toBeLessThan(16);
  });

  it("with a fake clock the stop does not depend on the machine: a handful of looks at the clock", () => {
    const { c, d } = fake();
    const r = resolveProps([{ dir: "", file: heavy() }], PATH, d);
    expect(r.over).toBe("time");
    expect(c.reads).toBeLessThanOrEqual(RESOLVE_MS + 3);
    expect(r.steps).toBeGreaterThan(0);
  });

  it("without the clock the same file is many deadlines of work (what the clock saves)", () => {
    const t = performance.now();
    const r = resolveProps([{ dir: "", file: heavy() }], PATH, deadline(Infinity));
    expect(r.over).toBeNull();
    expect(performance.now() - t).toBeGreaterThan(4 * RESOLVE_MS);
  });

  it("32 configs deep stop in time too (the review's walk)", () => {
    const deep = `${Array.from({ length: 31 }, (_, i) => `d${i + 1}`).join("/")}/main.cpp`;
    const walk = Array.from({ length: 32 }, (_, i) => ({
      dir: deep.split("/").slice(0, i).join("/"),
      file: parseEditorConfig(lines.slice(1).join("\n")), // no root: every one applies
    }));
    let r: ReturnType<typeof resolveProps> | undefined;
    expect(held(() => {
      r = resolveProps(walk, deep);
    })).toBeLessThan(16);
    expect(r).toMatchObject({ over: "time", props: {} });
  });

  it("the number-range shape stops in time, its digits counted without slicing", () => {
    const nums = ["root = true"];
    for (let n = 0; n < 123; n++) nums.push(`[${"{0..999999999}".repeat(30)}${n}x]`, "indent_size = 2");
    const file = parseEditorConfig(nums.join("\n"));
    const name = "9".repeat(1000);
    let r: ReturnType<typeof resolveProps> | undefined;
    expect(held(() => {
      r = resolveProps([{ dir: "", file }], name);
    })).toBeLessThan(16);
    expect(r?.over).toBe("time");
    // And a range still matches as the spec says.
    expect(matchGlob("file{1..300}.c", "file250.c")).toBe(true);
    expect(matchGlob("file{1..300}.c", "file301.c")).toBe(false);
    expect(matchGlob("v{-5..5}", "v-3")).toBe(true);
  });

  it("a real-world .editorconfig resolves well inside the deadline", () => {
    const real = parseEditorConfig([
      "root = true", "[*]", "indent_style = space", "indent_size = 4", "end_of_line = lf",
      "[*.{c,h,cpp,hpp,cc}]", "indent_size = 2", "[{Makefile,makefile,*.mk}]", "indent_style = tab",
      "[*.{json,yml,yaml}]", "indent_size = 2", "[*.md]", "trim_trailing_whitespace = false",
      "[src/**/vendor/**]", "indent_size = unset", "[lib/**.js]", "indent_size = 2",
    ].join("\n"));
    // On the work clock: its ~20,000 steps look at the clock ~75 times, and a
    // real one under load ran out during them (the flake this replaces).
    const r = resolveProps([{ dir: "", file: real }], `src/${"deep/".repeat(40)}robot.cpp`, workDeadline());
    expect(r.over).toBeNull();
    expect(r.props).toMatchObject({ indent_style: "space", indent_size: "2" });
    // A few thousand steps of a few nanoseconds: nowhere near the deadline, on any machine.
    expect(r.steps).toBeLessThan(100_000);
  });

  it("a glob over a cap configures nothing, and names its file (the caller poisons it)", () => {
    const ok = parseEditorConfig("[*.c]\nindent_size = 2\n");
    const capped = parseEditorConfig(`[*.c]\nindent_size = 3\n[${"{a,b}".repeat(7)}]\nindent_size = 4\n`);
    // The work clock: finding the cap looks at the clock, which a real one under load could end first.
    const r = resolveProps([{ dir: "", file: ok }, { dir: "", file: capped }], "x.c", workDeadline());
    expect(r).toMatchObject({ over: "cap", props: {}, culprit: 1 });
  });

  it(`a file of more than ${SECTIONS_MAX} sections configures nothing`, () => {
    const many = parseEditorConfig(Array.from({ length: SECTIONS_MAX + 1 }, (_, i) => `[f${i}.c]\nindent_size = 2`).join("\n"));
    expect(many.tooMany).toBe(true);
    expect(many.sections.length).toBe(SECTIONS_MAX);
    const r = resolveProps([{ dir: "", file: many }], "f0.c");
    expect(r).toMatchObject({ over: "sections", props: {} });
    const fits = parseEditorConfig(Array.from({ length: SECTIONS_MAX }, (_, i) => `[f${i}.c]\nindent_size = 2`).join("\n"));
    // The work clock: 128 sections look at the clock, and a real one ran out under load.
    expect(resolveProps([{ dir: "", file: fits }], "f0.c", workDeadline()).props).toMatchObject({ indent_size: "2" });
  });

  it("a section's glob is compiled once and kept with its file", () => {
    const file = parseEditorConfig("[*.c]\nindent_size = 2\n");
    resolveProps([{ dir: "", file }], "a.c");
    const kept = file.compiled?.[0];
    expect(kept).toBeTruthy();
    resolveProps([{ dir: "", file }], "b.c");
    expect(file.compiled?.[0]).toBe(kept);
  });
});

// The review of PR #59 (LOW): the token cap held only the first brace
// alternative to it, so a long second one walked past it.
describe("every brace alternative is held to the token cap", () => {
  it("a long second alternative makes the glob unusable, as a long plain glob is", () => {
    const long = "a".repeat(600);
    expect(matchGlob(`/${long}`, long)).toBe(false);
    expect(matchGlob(`/{z,${"a".repeat(400)}}${"a".repeat(200)}`, long)).toBe(false);
    expect(globCost(`/{z,${"a".repeat(400)}}${"a".repeat(200)}`)).toBeNull();
    // Within the cap, either alternative still matches.
    expect(matchGlob(`/{z,${"a".repeat(300)}}${"a".repeat(200)}`, "a".repeat(500))).toBe(true);
    expect(GLOB_TOKENS_MAX).toBe(512);
  });

  it("all alternatives together are held to the total cap", () => {
    const g = `/${"{a,b}".repeat(6)}${"c".repeat(70)}`; // 64 × 76 tokens > 4096
    expect(64 * 76).toBeGreaterThan(GLOB_TOTAL_MAX);
    expect(globCost(g)).toBeNull();
  });
});

// The glob is read in one pass: a glob made of unclosed brackets or braces
// is linear to read (each bracket's end used to be found by a scan of its own).
describe("reading a glob is linear", () => {
  it("thousands of unclosed brackets and braces read quickly", () => {
    const t = performance.now();
    for (const g of ["[".repeat(4000), "{".repeat(4000), "{[".repeat(2000), "[{a,b}".repeat(600)]) {
      globCost(g);
      matchGlob(g, "a".repeat(200));
    }
    expect(performance.now() - t).toBeLessThan(500);
  });
});
