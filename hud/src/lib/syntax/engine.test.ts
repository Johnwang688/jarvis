// @vitest-environment node
// The syntax engine on the real runtime and grammars (ED-5, editor plan §2.6):
// what it colours, injections, the incremental property, and the budgets.

import { decode } from "./flatten";
import { Lines, type Change } from "./positions";
import { TOKEN_MODIFIERS, TOKEN_TYPES } from "./legend";
import { assets, language, nodeEngine } from "./testkit";
import { workerPredicates, type SyntaxEngine } from "./engine";
import { GRAMMAR_NAMES, grammarFor, injectionGrammar, type GrammarName } from "./grammars";
import { Parser, Query } from "web-tree-sitter";

interface Tok {
  line: number;
  char: number;
  text: string;
  type: string;
  lang: string;
}

function toks(data: Uint32Array | null, text: string): Tok[] {
  if (!data) return [];
  const lines = new Lines(text);
  return decode(data).map(([line, char, length, type, mods]) => ({
    line,
    char,
    text: text.slice(lines.start(line) + char, lines.start(line) + char + length),
    type: TOKEN_TYPES[type],
    lang: TOKEN_MODIFIERS.filter((_, i) => mods & (1 << i)).join("."),
  }));
}

let ids = 0;
async function colour(e: SyntaxEngine, grammar: GrammarName, text: string, styledAs: string) {
  const id = `t${++ids}`;
  const state = await e.open(id, grammar, text, 1, styledAs);
  const r = e.tokens(id, 1, 0, 1e9);
  return { id, state, toks: toks(r.data, text), r };
}

const typeOf = (ts: Tok[], text: string, nth = 0) => ts.filter((t) => t.text.trim() === text)[nth]?.type;

/** A seeded generator (mulberry32), so a failing sequence can be replayed. */
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe("what tree-sitter colours", () => {
  it("C++: a call and a definition are functions, void a builtin type (--syn-type, not bold: the table wins), the include a directive", async () => {
    const e = await nodeEngine();
    const src = '#include "vex.h"\nvoid driveFor(double inches) {\n  if (inches > 0.5) { Drivetrain.driveFor(vex::forward); }\n  bool ok = true;\n}\n';
    const { state, toks: ts } = await colour(e, "cpp", src, "cpp");
    expect(state.state).toBe("ready");
    expect(typeOf(ts, "driveFor", 0)).toBe("function");
    expect(typeOf(ts, "driveFor", 1)).toMatch(/^function\.(method\.)?call$/);
    expect(typeOf(ts, "void")).toBe("type.builtin");
    expect(typeOf(ts, "double")).toBe("type.builtin");
    expect(typeOf(ts, "#include")).toBe("keyword.import");
    expect(ts.find((t) => t.text === "#include")?.lang).toBe("cpp");
    expect(typeOf(ts, '"vex.h"')).toMatch(/^string/);
    expect(typeOf(ts, "if")).toBe("keyword.conditional");
    expect(typeOf(ts, "true")).toBe("boolean");
    expect(typeOf(ts, "vex")).toBe("module");
    expect(typeOf(ts, ">")).toBe("operator");
    expect(typeOf(ts, "0.5")).toBe("number");
  });

  it("C: `#define SQ(x)` is a macro, not a function (an ED-3 left-over), styled as C", async () => {
    const e = await nodeEngine();
    const { toks: ts } = await colour(e, "cpp", "#define SQ(x) ((x) * (x))\nint y = SQ(2);\n", "c");
    expect(typeOf(ts, "SQ", 0)).toBe("function.macro");
    expect(ts.find((t) => t.text === "SQ")?.lang).toBe("c");
    expect(typeOf(ts, "*")).toBe("operator");
  });

  it("Python: a class and its base are types, `match` as a name is not a keyword, escapes in strings (ED-3 left-overs)", async () => {
    const e = await nodeEngine();
    const src = 'class Foo(Base):\n    pass\nmatch = 1\nprint(match)\ns = "a\\nb"\nx = a -> b if False else None\n';
    const { toks: ts } = await colour(e, "python", src, "python");
    expect(typeOf(ts, "Foo")).toBe("type");
    expect(typeOf(ts, "Base")).toBe("type");
    expect(typeOf(ts, "match", 0)).toBe("variable");
    expect(typeOf(ts, "match", 1)).toBe("variable");
    expect(typeOf(ts, "\\n")).toBe("string.escape");
    expect(typeOf(ts, "print")).toBe("function.builtin");
    expect(typeOf(ts, "None")).toBe("constant.builtin");
  });

  it("Python's `match` statement is a keyword where it is one", async () => {
    const e = await nodeEngine();
    const { toks: ts } = await colour(e, "python", "match x:\n    case 1:\n        pass\n", "python");
    expect(typeOf(ts, "match")).toMatch(/^keyword/);
    expect(typeOf(ts, "case")).toMatch(/^keyword/);
  });

  it("text no capture claims is `none`, so Monarch's guess never shows through; blank runs get no token", async () => {
    const e = await nodeEngine();
    const { toks: ts } = await colour(e, "markdown", "plain words here\n\n   \n", "markdown");
    expect(ts.some((t) => t.type === "none" && t.text.includes("plain words"))).toBe(true);
    expect(ts.every((t) => t.text.trim().length > 0)).toBe(true);
  });

  it("Markdown: a ```python fence is coloured as Python; bullets and link URLs as Markdown (ED-3 left-overs)", async () => {
    const e = await nodeEngine();
    const src = "# Plan\n\n- one [link](https://example.com)\n\n```python\ndef f(x):\n    return x\n```\n";
    const id = "md-fence";
    let changed = 0;
    const e2 = await nodeEngine({ changed: () => changed++ });
    await e2.open(id, "markdown", src, 1, "markdown");
    // The first answer has no Python yet: its grammar loads on first sight,
    // and the engine says so once it is in.
    const first = toks(e2.tokens(id, 1, 0, 1e9).data, src);
    expect(first.find((t) => t.text === "def")).toBeUndefined();
    let ts = first;
    for (let i = 0; i < 80 && !(ts.some((t) => t.text === "def") && ts.some((t) => t.text === "link")); i++) {
      await new Promise((r) => setTimeout(r, 25));
      ts = toks(e2.tokens(id, 1, 0, 1e9).data, src);
    }
    expect(changed).toBeGreaterThan(0);
    const def = ts.find((t) => t.text === "def");
    expect(def?.type).toBe("keyword.function");
    expect(def?.lang).toBe("python");
    expect(ts.find((t) => t.text === "f")?.type).toBe("function");
    expect(typeOf(ts, "```", 0)).toBe("markup.raw.block");
    expect(typeOf(ts, "python")).toBe("label");
    expect(typeOf(ts, "-")).toBe("markup.list");
    expect(typeOf(ts, "https://example.com")).toBe("markup.link.url");
    expect(typeOf(ts, "link")).toBe("markup.link.label");
    expect(typeOf(ts, "# Plan")).toBe("markup.heading");
    void e;
  });

  it("an unknown fence language keeps the host's colour", async () => {
    const e = await nodeEngine();
    const src = "```cobol\nMOVE A TO B.\n```\n";
    const { toks: ts } = await colour(e, "markdown", src, "markdown");
    expect(typeOf(ts, "MOVE A TO B.")).toBe("markup.raw.block");
  });

  it("HTML: <script> is JavaScript and <style> is CSS", async () => {
    const e = await nodeEngine();
    await e.preload("javascript");
    await e.preload("css");
    const src = "<p class=\"x\">hi</p>\n<script>\nconst n = 42;\n</script>\n<style>\nbody { color: red; }\n</style>\n";
    const { toks: ts } = await colour(e, "html", src, "html");
    expect(ts.find((t) => t.text === "const")?.lang).toBe("javascript");
    expect(typeOf(ts, "42")).toBe("number");
    expect(ts.find((t) => t.text === "color")?.lang).toBe("css");
    expect(typeOf(ts, "p")).toBe("tag");
    expect(typeOf(ts, "class")).toBe("tag.attribute");
  });

  it("JSON, YAML and TOML keys are `property`, styled per language (ED-3's per-language rows)", async () => {
    const e = await nodeEngine();
    const json = await colour(e, "json", '{"name": 1, "ok": true}\n', "json");
    expect(json.toks.find((t) => t.text === '"name"')).toMatchObject({ type: "property", lang: "json" });
    const yaml = await colour(e, "yaml", "name: robot\nlist:\n  - 1\n", "yaml");
    expect(yaml.toks.find((t) => t.text === "name")).toMatchObject({ type: "property", lang: "yaml" });
    const toml = await colour(e, "toml", '[pkg]\nname = "x"\n', "toml");
    expect(toml.toks.find((t) => t.text === "name")).toMatchObject({ type: "property", lang: "toml" });
  });

  it("a .tsx, a .js and a shell script parse with their own grammars", async () => {
    const e = await nodeEngine();
    const tsx = await colour(e, "tsx", "const A = () => <div className=\"x\">{n}</div>;\n", "typescript");
    expect(typeOf(tsx.toks, "div")).toMatch(/^tag/);
    const js = await colour(e, "javascript", "function f() { return 1; }\n", "javascript");
    expect(typeOf(js.toks, "function")).toBe("keyword.function");
    const sh = await colour(e, "bash", "#!/bin/bash\necho \"$HOME\"\n", "shell");
    expect(sh.toks.find((t) => t.text === "echo")).toMatchObject({ lang: "shell" });
  });
});

describe("predicates the worker evaluates", () => {
  it("#has-ancestor? and #has-parent?, and their not- forms, filter matches", async () => {
    await (await nodeEngine()).preload("cpp");
    const lang = await language("cpp");
    const parser = new Parser();
    parser.setLanguage(lang);
    const tree = parser.parse("struct S { int x; void f(); };\nint g() { return h(1); }\n")!;
    const q = new Query(lang, [
      "((field_identifier) @a (#has-ancestor? @a field_declaration))",
      "((field_identifier) @b (#not-has-ancestor? @b function_declarator))",
      "((identifier) @c (#has-parent? @c call_expression))",
      "((identifier) @d (#not-has-parent? @d call_expression function_declarator))",
    ].join("\n"));
    const got = (cap: string) => q.matches(tree.rootNode).filter((m) => workerPredicates(q, m))
      .flatMap((m) => m.captures.filter((c) => c.name === cap).map((c) => c.node.text));
    expect(got("a").sort()).toEqual(["f", "x"]);
    expect(got("b")).toEqual(["x"]);
    expect(got("c")).toEqual(["h"]);
    expect(got("d")).toEqual([]);
    // Without them every pattern matches every node it names.
    expect(q.matches(tree.rootNode).filter((m) => m.patternIndex === 2).length).toBeGreaterThan(1);
    tree.delete();
    parser.delete();
  });
});

// ---- the incremental property --------------------------------------------

const BASES: [GrammarName, string, string][] = [
  ["cpp", "cpp", '#include "vex.h"\n// drive\nvoid driveFor(double inches, int speed) {\n  if (inches > 0.5) {\n    Drivetrain.driveFor(vex::forward, inches);\n  }\n  return;\n}\n'],
  ["python", "python", 'import math\n\n@property\ndef lift(self, height=90.0):\n    """Raise."""\n    if height > 1:\n        print(f"up {height}\\n")\n    return math.sqrt(height)\n'],
  ["json", "json", '{\n  "name": "jarvis",\n  "list": [1, 2.5, true, null],\n  "nested": {"a": "b"}\n}\n'],
  ["markdown", "markdown", "# Title\n\nSome *emphasis* and `code` here.\n\n- item [link](http://x.y)\n\n```python\ndef f():\n    return 1\n```\n\nEnd.\n"],
  ["bash", "shell", "#!/bin/bash\nfor f in *.txt; do\n  echo \"$f\" | wc -l\ndone\n"],
  ["typescript", "typescript", "export function f(a: number): string {\n  return `${a}`;\n}\nconst x = { y: 1 } as const;\n"],
];

const SNIPPETS = [
  "", "x", " ", "\n", "\r\n", "\t", "(", ")", "{", "}", "[", "]", '"', "'", "`", "#", "*", "-", "😀", "é", "𝔘𝔫𝔦",
  "if (a) { b(); }", "def g():\n    pass\n", "```python\nx = 1\n```\n", "// c\n", "/* x */", "return 1;", "true", "null",
  "  - a\n", "> q\n", "\\n", "$(x)", ": ", ", ", "0x1F",
];

describe("incremental colouring equals a fresh parse's", () => {
  it("over 300 random edit sequences (astral characters, CRLF and tabs included)", async () => {
    const e = await nodeEngine();
    for (const g of ["cpp", "python", "json", "markdown", "markdown_inline", "bash", "typescript"] as GrammarName[]) {
      expect(await e.preload(g)).toBe(true);
    }
    const rand = rng(20261011);
    const pick = <T,>(xs: readonly T[]) => xs[Math.floor(rand() * xs.length)];
    let failures = 0;
    for (let seq = 0; seq < 300; seq++) {
      const [grammar, styled, base] = BASES[seq % BASES.length];
      const id = `inc${seq}`;
      let text = base;
      let version = 1;
      expect((await e.open(id, grammar, text, version, styled)).state).toBe("ready");
      const steps = 1 + Math.floor(rand() * 10);
      for (let s = 0; s < steps; s++) {
        // One change, or two in one event as Monaco sends them: ranges in the
        // text before the event, the later one first.
        const one = (): Change => {
          const offset = Math.floor(rand() * (text.length + 1));
          const length = Math.min(Math.floor(rand() * 9), text.length - offset);
          return { offset, length, text: pick(SNIPPETS) };
        };
        let changes: Change[] = [one()];
        if (rand() < 0.3) {
          const b = one();
          const [x, y] = [changes[0], b].sort((p, q) => p.offset - q.offset);
          if (x.offset + x.length <= y.offset) changes = [y, x];
        }
        for (const c of changes) text = text.slice(0, c.offset) + c.text + text.slice(c.offset + c.length);
        e.change(id, ++version, changes);
        // Sometimes colour mid-sequence, so later edits land on a reparsed tree.
        if (rand() < 0.4) e.tokens(id, version, 0, 1e9);
      }
      expect(e.text(id)).toBe(text);
      const inc = e.tokens(id, version, 0, 1e9);
      const fresh = `fresh${seq}`;
      await e.open(fresh, grammar, text, 1, styled);
      const ref = e.tokens(fresh, 1, 0, 1e9);
      const same = !!inc.data && !!ref.data && inc.data.length === ref.data.length && inc.data.every((v, i) => v === ref.data![i]);
      if (!same) {
        failures++;
        if (failures <= 3) console.error(`sequence ${seq} (${grammar}) differs: ${JSON.stringify(text)}`);
      }
      e.close(id);
      e.close(fresh);
    }
    expect(failures).toBe(0);
    expect(e.stats().docs).toBe(0);
  }, 60_000);
});

// ---- budgets -------------------------------------------------------------

describe("budgets: you never see a broken file, only a quieter one", () => {
  it("a 1.5 MB file stays on Monarch: large file, and no parse at all", async () => {
    let parsed = 0;
    const e = await nodeEngine({ now: () => (parsed++, 0) });
    const big = "int x = 1;\n".repeat(140_000); // 1.54 MB
    expect(big.length).toBeGreaterThan(1.5e6);
    const s = await e.open("big", "cpp", big, 1, "cpp");
    expect(s).toEqual({ state: "basic", reason: "large file" });
    expect(parsed).toBe(0);
    expect(e.tokens("big", 1, 0, 100).data).toBeNull();
  });

  it("a file that grows past the cap goes back to Monarch and stays there", async () => {
    const e = await nodeEngine();
    expect((await e.open("grow", "cpp", "int x;\n", 1, "cpp")).state).toBe("ready");
    const s = e.change("grow", 2, [{ offset: 0, length: 0, text: "x".repeat(e.budgets.maxChars) }]);
    expect(s).toEqual({ state: "basic", reason: "large file" });
    expect(e.change("grow", 3, [{ offset: 0, length: e.budgets.maxChars, text: "" }])).toEqual({ state: "basic", reason: "large file" });
    expect(e.tokens("grow", 3, 0, 10).data).toBeNull();
  });

  it("a first parse past its budget is cancelled and the model stays on Monarch", async () => {
    const clock = { t: 0, step: 0 };
    const e = await nodeEngine({ now: () => (clock.t += clock.step) });
    await e.preload("cpp");
    clock.step = 400; // every look at the clock is 400 ms later: past 1 s on the third
    const src = "int f(int a) { return a * 2 + g(a, b); }\n".repeat(5000);
    const s = await e.open("slow", "cpp", src, 1, "cpp");
    expect(s).toEqual({ state: "basic", reason: "slow parse" });
    expect(e.tokens("slow", 1, 0, 10).data).toBeNull();
    // The parser was reset: the next document parses whole, from its own text.
    clock.step = 0;
    const ok = await colour(e, "cpp", "void f() {}\n", "cpp");
    expect(ok.state.state).toBe("ready");
    expect(typeOf(ok.toks, "void")).toBe("type.builtin");
  });

  it("a reparse past its budget keeps the last tree (stale) and retries; twice running is Monarch", async () => {
    const clock = { t: 0, step: 0 };
    const e = await nodeEngine({ now: () => (clock.t += clock.step) });
    const src = "int f(int a) { return a * 2 + g(a, b); }\n".repeat(5000);
    expect((await e.open("re", "cpp", src, 1, "cpp")).state).toBe("ready");
    e.change("re", 2, [{ offset: 0, length: 0, text: "/* " }]);
    clock.step = 200; // 250 ms budget: cancelled on the third look
    expect(e.reparse("re")).toBe("stale");
    // Colour asked for: the second cancel in a row, and Monarch.
    expect(e.tokens("re", 2, 0, 10)).toMatchObject({ data: null, state: "basic", reason: "slow edits" });
    expect(e.state("re")).toMatchObject({ state: "basic", reason: "slow edits" });
    expect(e.tokens("re", 2, 0, 10).data).toBeNull();
  });

  it("one cancelled reparse, then a quick one: back to fresh colour, the count reset", async () => {
    const clock = { t: 0, step: 0 };
    const e = await nodeEngine({ now: () => (clock.t += clock.step) });
    const src = "int f(int a) { return a * 2 + g(a, b); }\n".repeat(5000);
    await e.open("re2", "cpp", src, 1, "cpp");
    e.change("re2", 2, [{ offset: 0, length: 0, text: "x" }]);
    clock.step = 200;
    expect(e.reparse("re2")).toBe("stale");
    clock.step = 0;
    expect(e.reparse("re2")).toBe("ok");
    e.change("re2", 3, [{ offset: 0, length: 1, text: "" }]);
    clock.step = 200;
    expect(e.reparse("re2")).toBe("stale"); // one again, not two
    expect(e.state("re2")?.state).toBe("ready");
  });

  it("a token request stops at the chunk where its time ran out, and says where", async () => {
    const clock = { t: 0, step: 0 };
    // Each look at this clock is 10 ms later, and a query run looks often
    // (its progress callback): `matchMs` is out of the way here, as this is
    // the request's own budget; the run's is the next describe's.
    const e = await nodeEngine({ now: () => (clock.t += clock.step), budgets: { matchMs: 1e9 } });
    const src = "int f(int a) { return a; }\n".repeat(1000);
    await e.open("q", "cpp", src, 1, "cpp");
    clock.step = 10; // 8 ms budget: over after the first chunk
    const r = e.tokens("q", 1, 0, 999);
    expect(r.truncatedAt).toBe(e.budgets.chunkLines - 1);
    const ts = toks(r.data, src);
    expect(ts.length).toBeGreaterThan(0);
    expect(Math.max(...ts.map((t) => t.line))).toBe(e.budgets.chunkLines - 1);
  });

  it("a line over 10,000 characters gets no tokens (Monarch keeps it)", async () => {
    const e = await nodeEngine();
    const src = `int a = 1;\nint b[] = {${"1,".repeat(6000)}};\nint c = 2;\n`;
    const { toks: ts } = await colour(e, "cpp", src, "cpp");
    expect(ts.some((t) => t.line === 1)).toBe(false);
    expect(ts.some((t) => t.line === 0) && ts.some((t) => t.line === 2)).toBe(true);
  });

  it("past 256 KB of injected text a fence keeps its host's colour, in document order", async () => {
    const e = await nodeEngine({ budgets: { injectedChars: 20 } });
    await e.preload("python");
    await e.preload("markdown_inline");
    const src = "```python\ndef a():\n    pass\n```\n\n```python\ndef b():\n    pass\n```\n";
    const { toks: ts } = await colour(e, "markdown", src, "markdown");
    expect(ts.find((t) => t.text === "def")?.lang).toBe("python");
    expect(ts.filter((t) => t.text === "def")).toHaveLength(1);
    expect(ts.find((t) => t.text.includes("def b"))?.type).toBe("markup.raw.block");
  });

  it("an answer for a version the engine has moved past is nothing, not wrong colour", async () => {
    const e = await nodeEngine();
    await e.open("v", "python", "x = 1\n", 1, "python");
    e.change("v", 2, [{ offset: 0, length: 1, text: "y" }]);
    expect(e.tokens("v", 1, 0, 10).data).toBeNull();
    expect(e.tokens("v", 2, 0, 10).data).not.toBeNull();
  });

  it("closing frees the tree; a closed or unknown document answers nothing", async () => {
    const e = await nodeEngine();
    await e.open("c", "python", "x = 1\n", 1, "python");
    e.close("c");
    expect(e.stats().docs).toBe(0);
    expect(e.tokens("c", 1, 0, 10).data).toBeNull();
    expect(e.change("c", 2, [])).toBeNull();
  });

  it("a grammar that fails to load leaves the model on Monarch", async () => {
    const e = await nodeEngine({ load: () => Promise.reject(new Error("refused")) });
    expect(await e.open("f", "cpp", "int x;\n", 1, "cpp")).toEqual({ state: "basic", reason: "grammar failed" });
  });
});

// ---- review of PR #54 -------------------------------------------------------

/**
 * `q` with a slow `matches()`: while `slow()` says so, each run moves the
 * engine's clock past any budget and asks the run's own progress callback
 * whether to stop (recorded in `stops`) — what a pathological query looks
 * like to the engine, on demand.
 */
function slowQuery(q: Query, clock: { t: number }, slow: () => boolean, stops: boolean[]): Query {
  return new Proxy(q, {
    get(target, prop) {
      if (prop === "matches") {
        return (node: Parameters<Query["matches"]>[0], opts?: Parameters<Query["matches"]>[1]) => {
          if (slow()) {
            clock.t += 10_000;
            stops.push(!!(opts?.progressCallback as ((s: { currentOffset: number }) => unknown) | undefined)?.({ currentOffset: 0 }));
          }
          return target.matches(node, opts);
        };
      }
      const v = Reflect.get(target, prop);
      return typeof v === "function" ? v.bind(target) : v;
    },
  });
}

describe("every query run has a budget (review of PR #54, LOW 2)", () => {
  it("a viewport chunk whose query runs past matchMs is dropped: the answer stops at the lines before it", async () => {
    const clock = { t: 0 };
    let calls = 0;
    const stops: boolean[] = [];
    const e = await nodeEngine({
      now: () => clock.t,
      load: async (g) => {
        const a = await assets(g);
        return { ...a, highlights: slowQuery(a.highlights, clock, () => ++calls === 2, stops) };
      },
    });
    const src = "int f(int a) { return a; }\n".repeat(200);
    await e.open("vq", "cpp", src, 1, "cpp");
    const r = e.tokens("vq", 1, 0, 199);
    expect(stops).toEqual([true]); // the second chunk's run was told to stop
    expect(r.truncatedAt).toBe(e.budgets.chunkLines - 1);
    const ts = toks(r.data, src);
    expect(ts.length).toBeGreaterThan(0);
    expect(Math.max(...ts.map((t) => t.line))).toBe(e.budgets.chunkLines - 1);
  });

  it("the whole-document injections query past matchMs: fences keep their host's colour; twice running, it is not run again", async () => {
    const clock = { t: 0 };
    let slow = true, runs = 0;
    const stops: boolean[] = [];
    const e = await nodeEngine({
      now: () => clock.t,
      load: async (g) => {
        const a = await assets(g);
        if (g !== "markdown" || !a.injections) return a;
        const inj = slowQuery(a.injections, clock, () => (runs++, slow), stops);
        return { ...a, injections: inj };
      },
    });
    await e.preload("python");
    await e.preload("markdown_inline");
    const src = "Text.\n\n```python\ndef f(x):\n    return x\n```\n";
    await e.open("inj", "markdown", src, 1, "markdown");
    let ts = toks(e.tokens("inj", 1, 0, 1e9).data, src);
    expect(stops).toEqual([true]);
    expect(ts.find((t) => t.text === "def")).toBeUndefined();
    expect(ts.find((t) => t.text.includes("def f"))?.type).toBe("markup.raw.block");
    // An edit: the query runs again, and is cancelled again (twice running).
    e.change("inj", 2, [{ offset: 0, length: 4, text: "Note" }]);
    ts = toks(e.tokens("inj", 2, 0, 1e9).data, src.replace("Text", "Note"));
    expect(stops).toEqual([true, true]);
    // From then on it is not run at all, however fast it would be now.
    slow = false;
    const before = runs;
    e.change("inj", 3, [{ offset: 0, length: 4, text: "Text" }]);
    ts = toks(e.tokens("inj", 3, 0, 1e9).data, src);
    expect(runs).toBe(before);
    expect(ts.find((t) => t.text === "def")).toBeUndefined();
    expect(e.state("inj")?.state).toBe("ready"); // the document itself keeps its colour
    expect(typeOf(ts, "```", 0)).toBe("markup.raw.block");
  });

  it("an injected range whose highlights run past matchMs keeps its host's colour", async () => {
    const clock = { t: 0 };
    const stops: boolean[] = [];
    const e = await nodeEngine({
      now: () => clock.t,
      load: async (g) => {
        const a = await assets(g);
        return g === "python" ? { ...a, highlights: slowQuery(a.highlights, clock, () => true, stops) } : a;
      },
    });
    await e.preload("python");
    await e.preload("markdown_inline");
    const src = "```python\ndef f(x):\n    return x\n```\n";
    const { toks: ts } = await colour(e, "markdown", src, "markdown");
    expect(stops).toEqual([true]);
    expect(ts.find((t) => t.text === "def")).toBeUndefined();
    expect(ts.find((t) => t.text.includes("def f"))?.type).toBe("markup.raw.block");
  });
});

describe("a fence's info string names only a grammar the HUD ships (review of PR #54, NIT 1)", () => {
  const PROTO = ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf", "__defineGetter__"];

  it("injectionGrammar and grammarFor read own keys only", () => {
    for (const name of PROTO) {
      expect(injectionGrammar(name)).toBeNull();
      expect(injectionGrammar(`{.${name}}`)).toBeNull();
      expect(grammarFor(name)).toBeNull();
    }
    expect(injectionGrammar("py")).toBe("python");
  });

  it("fences tagged with Object.prototype's names load nothing and keep the host's colour", async () => {
    const asked: string[] = [];
    const e = await nodeEngine({
      load: (g) => {
        asked.push(g);
        return assets(g);
      },
    });
    const src = PROTO.map((n) => `\`\`\`${n}\nint x = 1;\n\`\`\`\n`).join("\n");
    const { state, toks: ts } = await colour(e, "markdown", src, "markdown");
    expect(state.state).toBe("ready");
    await new Promise((r) => setTimeout(r, 50)); // any load it would have started
    expect(asked.every((g) => (GRAMMAR_NAMES as string[]).includes(g))).toBe(true);
    expect(asked.filter((g) => PROTO.includes(g))).toEqual([]);
    expect(typeOf(ts, "int x = 1;")).toBe("markup.raw.block");
  });
});
