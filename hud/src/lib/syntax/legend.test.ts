// The legend and the language tables (ED-5).

import { LEGEND, SCHEME, themeRules, readPalette } from "../editorTheme";
import { COVERED_LANGUAGES, FOR_LANGUAGE, GRAMMAR_NAMES, STYLED_AS, grammarFor, injectionGrammar } from "./grammars";
import { legend, modifierOf, NONE, NONE_TYPE, TOKEN_MODIFIERS, TOKEN_TYPES, typeOf } from "./legend";

describe("the semantic tokens legend", () => {
  it("is ED-3's capture names, then `none`; the modifiers are the covered languages", () => {
    expect(TOKEN_TYPES).toEqual([...LEGEND, NONE]);
    expect(NONE_TYPE).toBe(LEGEND.length);
    expect(TOKEN_MODIFIERS).toEqual(COVERED_LANGUAGES);
    expect(TOKEN_MODIFIERS.length).toBeLessThanOrEqual(31);
    expect(legend()).toEqual({ tokenTypes: [...TOKEN_TYPES], tokenModifiers: [...TOKEN_MODIFIERS] });
  });

  it("maps a capture to its longest prefix in the legend", () => {
    expect(TOKEN_TYPES[typeOf("markup.heading.1")]).toBe("markup.heading");
    expect(TOKEN_TYPES[typeOf("keyword.conditional.ternary")]).toBe("keyword.conditional");
    expect(TOKEN_TYPES[typeOf("function.method.call")]).toBe("function.method.call");
    expect(TOKEN_TYPES[typeOf("string.special.url")]).toBe("string.special");
    expect(TOKEN_TYPES[typeOf("variable.parameter.builtin")]).toBe("variable.parameter");
    expect(typeOf("none")).toBe(NONE_TYPE);
  });

  it("draws nothing for spell and conceal marks, private captures and injection captures", () => {
    for (const c of ["spell", "nospell", "conceal", "_parent", "injection.content", "injection.language", "nonsense.x"]) {
      expect(typeOf(c), c).toBe(-1);
    }
  });

  it("`none` names no theme rule, so it draws in the default text colour", () => {
    const p = readPalette(() => "#808080");
    expect(themeRules(p).some((r) => r.token === NONE || r.token.startsWith(`${NONE}.`))).toBe(false);
  });

  it("every per-language rule in the scheme names a language the legend can carry", () => {
    const perLanguage = SCHEME.flatMap((r) => r.captures).filter((c) => /\.(c|cpp|json|yaml|toml|python)$/.test(c));
    for (const c of perLanguage) expect(modifierOf(c.split(".").pop()!), c).toBeGreaterThanOrEqual(0);
  });
});

describe("which grammar colours what", () => {
  it("from the Monaco language and the path: .h and .c are C++'s grammar, .tsx its own", () => {
    expect(grammarFor("cpp", "/p/robot-config.h")).toBe("cpp");
    expect(grammarFor("c", "/p/main.c")).toBe("cpp");
    expect(grammarFor("typescript", "/p/App.tsx")).toBe("tsx");
    expect(grammarFor("typescript", "/p/api.ts")).toBe("typescript");
    expect(grammarFor("shell", "/p/x.sh")).toBe("bash");
    expect(grammarFor("go", "/p/x.go")).toBeNull();
    expect(grammarFor("plaintext")).toBeNull();
  });

  it("a fence's info string or an injection's language name, by alias", () => {
    expect(injectionGrammar("python")).toBe("python");
    expect(injectionGrammar("py")).toBe("python");
    expect(injectionGrammar("C++")).toBe("cpp");
    expect(injectionGrammar("sh")).toBe("bash");
    expect(injectionGrammar("{.js}")).toBe("javascript");
    expect(injectionGrammar("yml")).toBe("yaml");
    expect(injectionGrammar("cobol")).toBeNull();
    expect(injectionGrammar("regex")).toBeNull();
  });

  it("every grammar is styled as a covered language, and every covered language has a grammar", () => {
    for (const g of GRAMMAR_NAMES) expect(COVERED_LANGUAGES).toContain(STYLED_AS[g]);
    for (const l of COVERED_LANGUAGES) expect(GRAMMAR_NAMES).toContain(FOR_LANGUAGE[l]);
  });
});
