import { describe, expect, it } from "vitest";
import { compile } from "monaco-editor/editor/standalone/common/monarch/monarchCompile";
import { MonarchTokenizer } from "monaco-editor/editor/standalone/common/monarch/monarchLexer";
import { language as cpp } from "monaco-editor/languages/definitions/cpp/cpp";
import { language as python } from "monaco-editor/languages/definitions/python/python";
import { C_WORDS, PY_WORDS, refine } from "./monarchRefine";

/**
 * Monaco's own Monarch compiler and lexer, on the installed definitions: each
 * line's tokens as `[text, type]`, whitespace dropped.
 */
function tokenize(languageId: string, language: unknown, text: string): [string, string][][] {
  const config = { getValue: () => 20000, onDidChangeConfiguration: () => ({ dispose() {} }) };
  const tokenizer = new MonarchTokenizer(
    { languageIdCodec: { encodeLanguageId: () => 0 }, isRegisteredLanguageId: () => false } as any,
    {} as any, languageId, compile(languageId, structuredCloneish(language)), config as any,
  );
  let state = tokenizer.getInitialState();
  return text.split("\n").map((line) => {
    const r = tokenizer.tokenize(line, true, state);
    state = r.endState;
    return r.tokens.map((t: { offset: number; type: string }, i: number) => {
      const end = i + 1 < r.tokens.length ? r.tokens[i + 1].offset : line.length;
      return [line.slice(t.offset, end), t.type] as [string, string];
    }).filter(([s]: [string, string]) => s.trim() !== "");
  });
}

/** Monarch's compiler writes into the object it is given; give it a copy, regexes kept. */
function structuredCloneish(v: any): any {
  if (v instanceof RegExp || typeof v !== "object" || v === null) return v;
  if (Array.isArray(v)) return v.map(structuredCloneish);
  return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, structuredCloneish(x)]));
}

const typeOf = (lines: [string, string][][], text: string, nth = 0) =>
  lines.flat().filter(([s]) => s.trim() === text)[nth]?.[1];

const CPP = `#include "vex.h"
// Drive forward a set distance
void driveFor(double inches, int speed) {
  if (inches > 0.5) {
    Drivetrain.driveFor(forward, inches, vex::inches);
  }
  bool ok = this->ready && true;
  return sizeof(int);
}`;

const PY = `import re
# move the arm
@property
def lift(self, height=90.0):
    """Raise the arm."""
    if height > 1 and not None:
        print(f"up {height}\\n", len(str(height)))
    match = re.match(r"x", "y")
    return math.sqrt(height) is True`;

describe("Monarch's C++, refined", () => {
  it("finds the identifier rule in the installed definition", () => {
    expect(refine(cpp, C_WORDS)).not.toBeNull();
  });

  it("a call or a definition is a function; a qualifier is a module; builtins split; keywords stay", () => {
    const t = tokenize("cpp", refine(cpp, C_WORDS), CPP);
    expect(typeOf(t, "driveFor", 0)).toBe("function.cpp");   // the definition
    expect(typeOf(t, "driveFor", 1)).toBe("function.cpp");   // the call
    expect(typeOf(t, "vex")).toBe("module.cpp");
    expect(typeOf(t, "void")).toBe("type.builtin.cpp");
    expect(typeOf(t, "double")).toBe("type.builtin.cpp");
    expect(typeOf(t, "bool")).toBe("type.builtin.cpp");
    expect(typeOf(t, "int", 1)).toBe("type.builtin.cpp");     // sizeof(int)
    expect(typeOf(t, "true")).toBe("constant.builtin.cpp");
    expect(typeOf(t, "this")).toBe("variable.builtin.cpp");
    expect(typeOf(t, "if")).toBe("keyword.if.cpp");
    expect(typeOf(t, "sizeof")).toBe("keyword.sizeof.cpp");   // a keyword before ( stays one
    expect(typeOf(t, "return")).toBe("keyword.return.cpp");
    expect(typeOf(t, "inches")).toBe("identifier.cpp");
    expect(typeOf(t, "Drivetrain")).toBe("identifier.cpp");
    expect(typeOf(t, "#include")).toBe("keyword.directive.include.cpp");
    expect(typeOf(t, "vex.h")).toBe("string.include.identifier.cpp");
    expect(typeOf(t, "0.5")).toBe("number.float.cpp");
    expect(t[1][0][1]).toBe("comment.cpp");
  });

  it("unrefined, Monaco's own C++ calls every one of those an identifier or a keyword", () => {
    const t = tokenize("cpp", cpp, CPP);
    expect(typeOf(t, "driveFor", 1)).toBe("identifier.cpp");
    expect(typeOf(t, "void")).toBe("keyword.void.cpp");
  });
});

describe("Monarch's Python, refined", () => {
  it("finds the identifier rule in the installed definition", () => {
    expect(refine(python, PY_WORDS)).not.toBeNull();
  });

  it("calls and definitions are functions; builtins, types, constants and self split from the keywords", () => {
    const t = tokenize("python", refine(python, PY_WORDS), PY);
    expect(typeOf(t, "lift")).toBe("function.python");
    expect(typeOf(t, "sqrt")).toBe("function.python");
    expect(typeOf(t, "match", 1)).toBe("function.python");           // re.match(…)
    expect(typeOf(t, "print")).toBe("function.builtin.python");
    expect(typeOf(t, "len")).toBe("function.builtin.python");
    expect(typeOf(t, "str")).toBe("type.builtin.python");
    expect(typeOf(t, "None")).toBe("constant.builtin.python");
    expect(typeOf(t, "True")).toBe("constant.builtin.python");
    expect(typeOf(t, "self")).toBe("variable.builtin.python");
    expect(typeOf(t, "def")).toBe("keyword.python");
    expect(typeOf(t, "if")).toBe("keyword.python");
    expect(typeOf(t, "not")).toBe("keyword.python");
    expect(typeOf(t, "import")).toBe("keyword.python");
    expect(typeOf(t, "match", 0)).toBe("keyword.python");            // the soft keyword, as a name
    expect(typeOf(t, "height")).toBe("identifier.python");
    expect(typeOf(t, "@property")).toBe("tag.python");
    expect(typeOf(t, "90.0")).toBe("number.python");
    expect(t[1][0][1]).toBe("comment.python");
    // f-strings still open as strings: the call rule comes after Python's string rules.
    expect(t.flat().some(([s, ty]) => s.startsWith("up") && ty === "string.python")).toBe(true);
  });

  it("unrefined, Monaco's own Python calls its builtins keywords", () => {
    const t = tokenize("python", python, PY);
    expect(typeOf(t, "print")).toBe("keyword.python");
    expect(typeOf(t, "None")).toBe("keyword.python");
    expect(typeOf(t, "sqrt")).toBe("identifier.python");
  });
});

describe("refine", () => {
  it("returns null, and so leaves Monaco's tokenizer alone, when the identifier rule is not where it was", () => {
    expect(refine({ tokenizer: { root: [[/[a-z]+/, "identifier"]] } }, C_WORDS)).toBeNull();
    expect(refine({}, C_WORDS)).toBeNull();
    expect(refine(null, C_WORDS)).toBeNull();
  });

  it("never changes the definition it was given", () => {
    const before = JSON.stringify(cpp, (_k, v) => (v instanceof RegExp ? v.source : v));
    refine(cpp, C_WORDS);
    expect(JSON.stringify(cpp, (_k, v) => (v instanceof RegExp ? v.source : v))).toBe(before);
  });
});
