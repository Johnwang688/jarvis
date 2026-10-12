import { describe, expect, it } from "vitest";
import {
  candidateDirs, configPath, matchGlob, overrideOf, parseEditorConfig, resolveProps, type ConfigFile,
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
    expect(resolveProps([{ dir: "", file: root }, { dir: "sub", file: sub }], "sub/x.c")).toEqual({
      indent_style: "tab", indent_size: "8", tab_width: "8",
    });
    // sub's section does not reach a file outside sub
    expect(resolveProps([{ dir: "", file: root }, { dir: "sub", file: sub }], "x.c")).toEqual({
      indent_style: "space", indent_size: "4", tab_width: "4",
    });
  });

  it("`unset` removes a property", () => {
    const f = file("[*]\nindent_size = 2\nend_of_line = lf\n[*.md]\nend_of_line = unset\n");
    expect(resolveProps([{ dir: "", file: f }], "a.md")).toEqual({ indent_size: "2", tab_width: "2" });
  });

  it("the spec's indent rules: tab style means a tab's size; indent_size = tab takes tab_width", () => {
    expect(resolveProps([{ dir: "", file: file("[*]\nindent_style = tab\n") }], "a")).toEqual({
      indent_style: "tab", indent_size: "tab",
    });
    expect(resolveProps([{ dir: "", file: file("[*]\nindent_style = tab\ntab_width = 3\n") }], "a")).toEqual({
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
