import { describe, expect, it } from "vitest";
import {
  candidateDirs, configPath, globCost, GLOB_TOKENS_MAX, GLOB_TOTAL_MAX, MATCH_BUDGET, matchGlob, overrideOf,
  parseEditorConfig, resolveProps, SECTIONS_MAX, type ConfigFile,
} from "./editorconfig";

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

// The review of PR #59 (MED): an agent-written `.editorconfig` must never
// freeze the window. One resolution — every section of every file on the
// walk — has one work budget, and past it the file has no configuration.
describe("a whole .editorconfig is held to one budget", () => {
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
  const file = parseEditorConfig(lines.join("\n"));
  const PATH = `src/${"robot_drive_subsystem_controller_for_the_left_side_".repeat(5)}.py`;

  it("the shape is what the review built: 123 sections of 64 alternatives", () => {
    expect(file.sections).toHaveLength(123);
    expect(globCost(file.sections[0].glob)).toMatchObject({ alts: 64 });
  });

  it("is stopped by the budget: no configuration, bounded work and time", () => {
    const t = performance.now();
    const r = resolveProps([{ dir: "", file }], PATH);
    const ms = performance.now() - t;
    expect(r.over).toBe("budget");
    expect(r.props).toEqual({}); // never a partial answer
    expect(r.work).toBeLessThanOrEqual(MATCH_BUDGET);
    expect(ms).toBeLessThan(100);
  });

  it("without a budget it is many budgets' worth of work (what the budget saves)", () => {
    const r = resolveProps([{ dir: "", file }], PATH, Infinity);
    expect(r.over).toBeNull();
    expect(r.work).toBeGreaterThan(20 * MATCH_BUDGET);
  });

  it("the walk's files share the one budget", () => {
    const sub = parseEditorConfig(lines.slice(0, 13).join("\n")); // 6 heavy sections each
    const walk = Array.from({ length: 12 }, (_, i) => ({ dir: i ? `d${i}` : "", file: sub }));
    const deep = `${Array.from({ length: 11 }, (_, i) => `d${i + 1}`).join("/")}/main.cpp`;
    // Each folder's file applies only to paths below it: nest them.
    const nested = walk.map((w, i) => ({ dir: i ? deep.split("/").slice(0, i).join("/") : "", file: w.file }));
    const r = resolveProps(nested, deep);
    expect(r.over).toBe("budget");
    expect(r.work).toBeLessThanOrEqual(MATCH_BUDGET);
  });

  it("a real-world .editorconfig is well inside it", () => {
    const real = parseEditorConfig([
      "root = true", "[*]", "indent_style = space", "indent_size = 4", "end_of_line = lf",
      "[*.{c,h,cpp,hpp,cc}]", "indent_size = 2", "[{Makefile,makefile,*.mk}]", "indent_style = tab",
      "[*.{json,yml,yaml}]", "indent_size = 2", "[*.md]", "trim_trailing_whitespace = false",
      "[src/**/vendor/**]", "indent_size = unset", "[lib/**.js]", "indent_size = 2",
    ].join("\n"));
    const r = resolveProps([{ dir: "", file: real }], `src/${"deep/".repeat(40)}robot.cpp`);
    expect(r.over).toBeNull();
    expect(r.props).toMatchObject({ indent_style: "space", indent_size: "2" });
    expect(r.work).toBeLessThan(MATCH_BUDGET / 20);
  });

  it(`a file of more than ${SECTIONS_MAX} sections configures nothing`, () => {
    const many = parseEditorConfig(Array.from({ length: SECTIONS_MAX + 1 }, (_, i) => `[f${i}.c]\nindent_size = 2`).join("\n"));
    expect(many.tooMany).toBe(true);
    expect(many.sections.length).toBe(SECTIONS_MAX);
    const r = resolveProps([{ dir: "", file: many }], "f0.c");
    expect(r).toMatchObject({ over: "sections", props: {} });
    const fits = parseEditorConfig(Array.from({ length: SECTIONS_MAX }, (_, i) => `[f${i}.c]\nindent_size = 2`).join("\n"));
    expect(resolveProps([{ dir: "", file: fits }], "f0.c").props).toMatchObject({ indent_size: "2" });
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
