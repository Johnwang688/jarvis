// @vitest-environment node
// The query translator (ED-5): Lua patterns as JS regular expressions, on
// every pattern the vendored nvim queries use, and what it keeps, strips and
// drops.

import { readdirSync } from "node:fs";
import {
  inheritanceOrder, luaToRegex, predicatesOf, splitPatterns, translateQuery, unquote, Untranslatable,
} from "./translate";
import { HUD, read } from "./testkit";

/**
 * Every Lua pattern the vendored queries contain, with strings Lua's
 * `string.find` matches and strings it does not (worked by hand from the Lua
 * 5.1 manual's pattern rules: `%u` upper, `%l` lower, `%d` digit, `%s` space,
 * `.` any character including a line break, `%x` before punctuation escapes
 * it). A pattern the vendored queries gain must be added here.
 */
const LUA: Record<string, [string[], string[]]> = {
  "%${": [["${x}", "a${"], ["$x", "{"]],
  "%slang%s*=": [[' lang="ts"', "\tlang  ="], ["lang=", " language="]],
  "%stype%s*=": [[' type="x"', "\ntype="], ["type="]],
  "/[*/][!*/]<?[^a-zA-Z]": [["/** x", "//! x", "///< x", "/*! a"], ["/* x", "// x", "//!x"]],
  "^#!/": [["#!/bin/sh"], [" #!/bin/sh", "#! /bin"]],
  "^#![ \t]*/": [["#!/bin/sh", "#! \t/usr"], ["#!x/", " #!/"]],
  "^%u": [["Foo", "X"], ["foo", "_X", "1"]],
  "^/[*][*][^*].*[*]/$": [["/** doc */", "/** a\n * b\n */"], ["/* x */", "/*** x */", "/** x"]],
  "^[%l_].*$": [["foo", "_x", "a\nb"], ["Foo", "1x"]],
  "^[%u]": [["A"], ["a"]],
  "^[-][-]": [["--x", "--"], ["-x", "x--"]],
  "^[A-Z]": [["Ab"], ["ab"]],
  "^[A-Z].*[a-z]": [["Foo", "AB c"], ["FOO", "foo", "A"]],
  "^[A-Z][A-Z0-9_]+$": [["MAX_1", "AB"], ["A", "Max", "MAX-1"]],
  "^[A-Z][A-Z_0-9]*$": [["A", "MAX_SPEED"], ["a", "Max", "A b"]],
  "^[a-zA-Z][a-zA-Z0-9]*$": [["js", "Python3"], ["3d", "c++", "a b"]],
  "^[a-zA-Z_][a-zA-Z0-9_]*$": [["_x1", "HOME"], ["1x", "a-b"]],
  "^_*[A-Z][A-Z%d_]*$": [["__X", "A1_B"], ["_x", "Ab"]],
  "^__[a-zA-Z0-9_]*__$": [["__init__", "____"], ["_init_", "__init"]],
  "^__builtin_": [["__builtin_expect"], ["_builtin_", "x__builtin_"]],
  "^`#graphql": [["`#graphql\nquery"], ["#graphql", " `#graphql"]],
  "^m_.*$": [["m_speed", "m_"], ["speed", "am_"]],
  "^on[a-z]+$": [["onclick"], ["on", "onClick", "xonclick"]],
};

describe("luaToRegex", () => {
  it("covers every Lua pattern in the vendored queries", () => {
    const found = new Set<string>();
    for (const dir of readdirSync(`${HUD}vendor/nvim-treesitter/queries`)) {
      for (const kind of ["highlights", "injections"]) {
        let text = "";
        try {
          text = read(`vendor/nvim-treesitter/queries/${dir}/${kind}.scm`);
        } catch {
          continue;
        }
        for (const m of text.matchAll(/#(?:not-|any-)?lua-match\?\s+@[\w.\-]+\s+("(?:[^"\\]|\\.)*")/g)) found.add(unquote(m[1]));
      }
    }
    expect([...found].sort()).toEqual(Object.keys(LUA).map((k) => k).sort());
  });

  for (const [lua, [yes, no]] of Object.entries(LUA)) {
    it(`${JSON.stringify(lua)} matches what Lua would`, () => {
      const re = new RegExp(luaToRegex(lua));
      for (const s of yes) expect(re.test(s), `${JSON.stringify(s)} should match`).toBe(true);
      for (const s of no) expect(re.test(s), `${JSON.stringify(s)} should not match`).toBe(false);
    });
  }

  it("translates classes, complements, sets, escapes and the lazy quantifier", () => {
    expect(luaToRegex("^%a%d%s$")).toBe("^[A-Za-z][0-9][ \\t\\n\\r\\f\\v]$");
    expect(luaToRegex("%U")).toBe("[^A-Z]");
    expect(luaToRegex("[%w_]+")).toBe("[A-Za-z0-9_]+");
    expect(luaToRegex("a.-b")).toBe("a[\\s\\S]*?b");
    expect(luaToRegex("%.%(%)%%")).toBe("\\.\\(\\)%");
    expect(luaToRegex("a|b{1}")).toBe("a\\|b\\{1\\}");
    // `^` and `$` are anchors only at the ends, as in Lua.
    expect(new RegExp(luaToRegex("a^b$c")).test("a^b$c")).toBe(true);
  });

  it("refuses what JavaScript cannot say: %b, %f, back-references, position captures", () => {
    for (const lua of ["%b()", "%f[%w]", "(a)%1", "()a", "[%U]", "abc%"]) {
      expect(() => luaToRegex(lua), lua).toThrow(Untranslatable);
    }
  });
});

describe("splitting a query", () => {
  it("finds each top-level pattern with its captures and quantifiers, through strings and comments", () => {
    const src = '; a comment with ( and "\n(identifier) @variable\n\n"if" @keyword ; trailing (\n[ "a" "b" ] @x @y\n((call) @c (#eq? @c ")"))\n(_)* @z\n';
    const ps = splitPatterns(src);
    expect(ps.map((p) => p.text)).toEqual([
      "(identifier) @variable",
      '"if" @keyword',
      '[ "a" "b" ] @x @y',
      '((call) @c (#eq? @c ")"))',
      "(_)* @z",
    ]);
    expect(ps.map((p) => p.line)).toEqual([2, 4, 5, 6, 7]);
  });

  it("finds a pattern's predicates, not ones inside strings", () => {
    const p = '((x) @a (#lua-match? @a "(#eq? no)") (#set! priority 99))';
    expect(predicatesOf(p).map((x) => x.name)).toEqual(["lua-match?", "set!"]);
  });
});

describe("translateQuery", () => {
  it("rewrites #lua-match? to #match?, keeps native and worker predicates", () => {
    const src = [
      '((identifier) @constant (#lua-match? @constant "^%u[%u%d_]*$"))',
      '((identifier) @x (#not-lua-match? @x "^_"))',
      "((field_identifier) @property (#has-ancestor? @property field_declaration))",
      '((identifier) @y (#any-of? @y "a" "b") (#set! priority 101))',
    ].join("\n");
    const t = translateQuery(src, "x/highlights.scm");
    expect(t.dropped).toEqual([]);
    expect(t.text).toContain('(#match? @constant "^[A-Z][A-Z0-9_]*$")');
    expect(t.text).toContain('(#not-match? @x "^_")');
    expect(t.text).toContain("#has-ancestor? @property field_declaration");
    expect(t.text).toContain("(#set! priority 101)");
    expect(t.text).not.toContain("lua-match");
  });

  it("drops a pattern using an unsupported predicate or an untranslatable Lua pattern, and says why", () => {
    const src = [
      "(a) @keep",
      '((b) @x (#gsub! @x "(.+)/(.+)" "%2"))',
      '((c) @y (#lua-match? @y "%b()"))',
      '((d) @z (#vim-match? @z "x"))',
    ].join("\n");
    const t = translateQuery(src, "q.scm");
    expect(t.text.trim()).toBe("(a) @keep");
    expect(t.dropped.map((d) => [d.line, d.reason])).toEqual([
      [2, "#gsub! is not supported"],
      [3, "Lua's %b has no JS equivalent"],
      [4, "#vim-match? is not supported"],
    ]);
  });

  it("takes out nvim's per-capture #set! metadata and keeps the pattern", () => {
    const t = translateQuery('((url) @string.special.url\n  (#set! @string.special.url url @string.special.url))', "q.scm");
    expect(t.dropped).toEqual([]);
    expect(t.stripped).toHaveLength(1);
    expect(t.text.trim()).toBe("((url) @string.special.url)");
  });

  it("resolves inherits in nvim's order, each file once", () => {
    const files: Record<string, string> = {
      tsx: "; inherits: typescript,jsx\n(t)",
      typescript: "; inherits: ecma\n(ts)",
      jsx: "; inherits: ecma\n(j)",
      ecma: "(e)",
    };
    expect(inheritanceOrder("tsx", (l) => files[l] ?? null)).toEqual(["ecma", "typescript", "jsx", "tsx"]);
    expect(() => inheritanceOrder("a", (l) => ({ a: "; inherits: b", b: "; inherits: a" } as Record<string, string>)[l] ?? null)).toThrow(/cycle/);
  });
});
