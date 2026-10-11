// Monarch's C, C++ and Python, refined just enough for the Graphite scheme's
// rows to show (ED-3, editor plan §2.5). Monaco's tokenizers call every word
// either a keyword or an identifier: a function call is an identifier, and
// Python's keyword list is also its builtins (`print`, `len`, `int`, `self`,
// `True`, `None` all came out as bold keywords). Those are the languages of
// the owner's VEX work, so their fallback colour tells apart what the
// scheme's rows tell apart, in the capture names the rows are keyed by:
//
//   - a word directly before `(` is a `function` (a call or a definition);
//   - a C++ word directly before `::` is a `module` (`vex::`, `std::`);
//   - the builtins split into `constant.builtin`, `type.builtin`,
//     `function.builtin` and `variable.builtin` (`self`, `this`);
//   - a real keyword stays the keyword token it was.
//
// Tree-sitter colour (ED-5) replaces this wherever it covers a file; Monarch
// stays underneath as the fallback, which is what this refines. The change
// is one rule of each definition, found by its exact shape: if Monaco's
// definition ever moves on (a new version), `refine` returns null and the
// language keeps Monaco's own tokenizer — less colour, never a broken one.
// `editorTheme.test.ts` checks the shape against the installed C++ module,
// and the headless editor-scheme check reads the colours back for both.

/** Monaco's identifier rule, in the C++ and Python definitions alike. */
const IDENT = "[a-zA-Z_]\\w*";

export interface WordClasses {
  constants: readonly string[];
  types: readonly string[];
  builtins: readonly string[];
  selves: readonly string[];
  /** Replaces the definition's own keyword list (Python's carries its builtins). */
  keywords?: readonly string[];
  /** Keywords that stay keywords before `(`; the rest of a `keywords` list is a call there. */
  callKeywords?: readonly string[];
  /** Words before `::` are modules (C++). */
  namespaces?: boolean;
}

type Lang = { keywords?: string[]; tokenizer?: { root?: unknown[] } } & Record<string, unknown>;

/** A refined copy of a Monarch definition, or null if its identifier rule is not where it was. */
export function refine(language: unknown, words: WordClasses): Lang | null {
  const lang = language as Lang;
  const root = lang?.tokenizer?.root;
  if (!Array.isArray(root)) return null;
  const at = root.findIndex(
    (r) => Array.isArray(r) && r[0] instanceof RegExp && r[0].source === IDENT
      && typeof r[1] === "object" && r[1] !== null && "@keywords" in ((r[1] as any).cases ?? {}),
  );
  if (at < 0) return null;
  const keywordAction = ((root[at] as any)[1].cases as Record<string, unknown>)["@keywords"];
  const classes = (keywordList: string) => ({
    "@jarvisConstants": "constant.builtin",
    "@jarvisTypes": "type.builtin",
    "@jarvisSelves": "variable.builtin",
    "@jarvisBuiltins": "function.builtin",
    [keywordList]: keywordAction,
  });
  const rules: unknown[] = [
    [new RegExp(`${IDENT}(?=\\s*\\()`), { cases: { ...classes("@jarvisCallKeywords"), "@default": "function" } }],
    ...(words.namespaces
      ? [[new RegExp(`${IDENT}(?=\\s*::)`), { cases: { ...classes("@keywords"), "@default": "module" } }]]
      : []),
    [new RegExp(IDENT), { cases: { ...classes("@keywords"), "@default": "identifier" } }],
  ];
  const keywords = [...(words.keywords ?? lang.keywords ?? [])];
  return {
    ...lang,
    keywords,
    jarvisConstants: [...words.constants],
    jarvisTypes: [...words.types],
    jarvisSelves: [...words.selves],
    jarvisBuiltins: [...words.builtins],
    jarvisCallKeywords: [...(words.callKeywords ?? keywords)],
    tokenizer: { ...lang.tokenizer, root: [...root.slice(0, at), ...rules, ...root.slice(at + 1)] },
  };
}

/**
 * What a language's tokenizer factory hands Monaco: the refined definition if
 * it can be made *and* compiled, else the definition as Monaco shipped it —
 * never a throw. Monaco compiles a factory's definition only after `create`
 * returns, outside anything that could catch it, and a factory that throws
 * leaves its language with no colour at all; so the refined copy is compiled
 * here first (`compile` is Monaco's Monarch compiler, which throws on a bad
 * definition), on a copy, since it writes into the object it is given.
 * Review of PR #45, LOW-2.
 */
export function refinedOrOwn(
  languageId: string,
  language: unknown,
  words: WordClasses,
  compile: (languageId: string, definition: unknown) => unknown,
  log: (message: string) => void = (m) => console.error(m),
): unknown {
  try {
    const refined = refine(language, words);
    if (!refined) return language;
    compile(languageId, { ...refined });
    return refined;
  } catch (e) {
    const why = e instanceof Error ? e.message : String(e);
    log(`editor: ${languageId}'s refined tokenizer failed (${why.split("\n")[0].slice(0, 200)}); Monaco's own is used`);
    return language;
  }
}

// ---- the word lists ---------------------------------------------------------

/** C and C++ (`tree-sitter-c`'s `primitive_type` and literals, as nvim-treesitter colours them). */
export const C_WORDS: WordClasses = {
  constants: ["true", "false", "nullptr", "NULL", "__nullptr"],
  types: [
    "void", "bool", "char", "char8_t", "char16_t", "char32_t", "wchar_t", "short", "int", "long", "float",
    "double", "signed", "unsigned", "auto", "size_t", "ssize_t", "ptrdiff_t", "intptr_t", "uintptr_t",
    "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
  ],
  builtins: [],
  selves: ["this"],
  namespaces: true,
};

/** Python's statements; Monaco's list adds its builtins, which are not keywords. */
const PY_KEYWORDS = [
  "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else",
  "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
  "or", "pass", "raise", "return", "try", "while", "with", "yield",
];

export const PY_WORDS: WordClasses = {
  constants: ["True", "False", "None", "NotImplemented", "Ellipsis", "__debug__"],
  types: [
    "bool", "int", "float", "complex", "str", "bytes", "bytearray", "memoryview", "list", "tuple", "range",
    "dict", "set", "frozenset", "object", "type",
  ],
  builtins: [
    "abs", "aiter", "all", "anext", "any", "ascii", "bin", "breakpoint", "callable", "chr", "classmethod",
    "compile", "delattr", "dir", "divmod", "enumerate", "eval", "exec", "filter", "format", "getattr",
    "globals", "hasattr", "hash", "help", "hex", "id", "input", "isinstance", "issubclass", "iter", "len",
    "locals", "map", "max", "min", "next", "oct", "open", "ord", "pow", "print", "property", "repr",
    "reversed", "round", "setattr", "slice", "sorted", "staticmethod", "sum", "super", "vars", "zip",
    "__import__",
  ],
  selves: ["self", "cls"],
  // `match` and `case` are statements too (soft keywords); before `(` they
  // are someone's function — `re.match(…)`.
  keywords: [...PY_KEYWORDS, "match", "case"],
  callKeywords: PY_KEYWORDS,
};
