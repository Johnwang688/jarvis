// @vitest-environment node
// Folding from the tree (ED-6, editor plan §2.6), on the real runtime, the
// bundled grammars and the bundled folds queries: what folds, where a fold
// ends, and the engine's rules around it (request versions, a stale tree, the
// query's time budget).

import type { Query } from "web-tree-sitter";
import { assets, nodeEngine } from "./testkit";
import type { SyntaxEngine } from "./engine";
import type { FoldRange } from "./folds";
import type { GrammarName } from "./grammars";

let ids = 0;
async function foldsOf(e: SyntaxEngine, grammar: GrammarName, text: string, styledAs = grammar as string) {
  const id = `f${++ids}`;
  const state = await e.open(id, grammar, text, 1, styledAs);
  expect(state.state).toBe("ready");
  return { id, r: e.folds(id, 1) };
}

/**
 * Grammar assets whose folds query is slow on demand: while `slow()` says so,
 * a run moves the engine's clock past any budget and asks its own progress
 * callback whether to stop (recorded in `stops`), as engine.test.ts's
 * `slowQuery` does for the other queries.
 */
function slowFolds(clock: { t: number }, slow: () => boolean, stops: boolean[]) {
  return async (g: GrammarName) => {
    const a = await assets(g);
    const q = a.folds!;
    const folds = new Proxy(q, {
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
    return { ...a, folds };
  };
}

/** Each fold as "start-end" (1-based lines), with its kind when it has one. */
const show = (ranges: FoldRange[] | null) => (ranges ?? []).map((f) => `${f.start}-${f.end}${f.kind ? ` ${f.kind}` : ""}`);

const CPP = `#include "vex.h"
#include <cmath>
#include <string>

// A robot.
class Robot {
public:
  void drive(int speed) {
    if (speed > 0) {
      go(speed);
    }
  }

private:
  int speed_ = 0;
};

/* Waits,
   then spins. */
int main() {
  Robot r;
  auto f = [](int x) {
    return x;
  };
  return 0;
}
`;

const PY = `import os
import sys

def lift(arm, speed):
    """Raise the arm.

    Slowly."""
    if speed > 0:
        arm.spin(
            speed,
        )
    return {
        "ok": True,
    }

class Arm:
    pass
`;

const MD = `# Arm

Intro text.

## Lift

Lift slowly.

\`\`\`cpp
int x;
\`\`\`

## Drop

- item
  - sub
- two

`;

describe("fold ranges from nvim's folds.scm", () => {
  it("a C++ class: the class, its method and the if inside it, each keeping its closing brace in view", async () => {
    const e = await nodeEngine();
    const { r } = await foldsOf(e, "cpp", CPP);
    expect(r.state).toBe("ready");
    expect(show(r.ranges)).toEqual([
      "1-3 imports", // three #includes, one fold (a quantified capture)
      "6-15", // class Robot { … } — line 16, `};`, stays
      "8-11", // void drive(…) { … }
      "9-10", // if (…) { … }
      "18-19 comment", // a block comment
      "20-25", // int main() { … }
      "22-23", // the lambda, its `};` in view
    ]);
  });

  it("a Python function: the whole def (its last statement's bracket is not the def's), the docstring, the if, a call's arguments and a dict", async () => {
    const e = await nodeEngine();
    const { r } = await foldsOf(e, "python", PY);
    expect(show(r.ranges)).toEqual([
      "1-2 imports",
      "4-14", // def lift(…): through the dict's `}` — a lone `}` is never left behind
      "5-7", // the docstring
      "8-11", // if speed > 0: through the call's `)`
      "9-10", // arm.spin( … ) — the call's own `)` stays in view
      "12-13", // { … } — its own `}` stays
      "16-17", // class Arm:
    ]);
  });

  it("a Markdown section: each heading folds to the end of its section, trailing blank lines trimmed; a fence and a nested list fold too", async () => {
    const e = await nodeEngine();
    const { r } = await foldsOf(e, "markdown", MD);
    expect(show(r.ranges)).toEqual([
      "1-17", // # Arm, to the last list item (blank lines after it trimmed: #trim!)
      "5-11", // ## Lift, through the fence's closing line
      "9-11", // the fence
      "13-17", // ## Drop
      "15-17", // the list (the item with a sublist, 15-16, starts on the same line: the widest is kept)
    ]);
  });

  it("where a fold ends: the node's own closer stays in view (past `;`, through an else or a catch), anything else folds whole", async () => {
    const e = await nodeEngine();
    const cases: [GrammarName, string, string[]][] = [
      // An if … else: the else's `}` (three levels down) closes the whole statement.
      ["cpp", "void f() {\n  if (a) {\n    x();\n  } else {\n    y();\n  }\n}\n", ["1-6", "2-5"]],
      // A brace on a line of its own (Allman): the fold starts at the signature, where the function does.
      ["cpp", "void autonomousRoutine(int side,\n                       int delay)\n{\n  go();\n}\n", ["1-4"]],
      // try … catch.
      ["cpp", "try {\n  a();\n} catch (int e) {\n  b();\n}\n", ["1-4", "3-4"]],
      // `};` and `},`: the bracket still starts the line.
      ["typescript", "type X = {\n  a: string;\n};\n", ["1-2"]],
      ["javascript", "const o = [\n  {\n    a: 1,\n  },\n];\n", ["1-4", "2-3"]],
      // A closer that does not start its line folds with the rest: `})` hides.
      ["javascript", "run(function () {\n  go();\n});\n", ["1-3"]],
      // A one-line node is no fold.
      ["cpp", "int f() { return 1; }\n", []],
    ];
    for (const [g, text, want] of cases) {
      const { r } = await foldsOf(e, g, text);
      expect(show(r.ranges), JSON.stringify(text)).toEqual(want);
    }
  });

  it("CRLF line ends fold on the same lines", async () => {
    const e = await nodeEngine();
    const { r: lf } = await foldsOf(e, "cpp", CPP);
    const { r: crlf } = await foldsOf(e, "cpp", CPP.replace(/\n/g, "\r\n"));
    expect(show(crlf.ranges)).toEqual(show(lf.ranges));
  });

  it("every covered grammar with a folds query folds something, and the inline grammar (never a host) has none", async () => {
    const e = await nodeEngine();
    const samples: [GrammarName, string, string][] = [
      ["javascript", "function f() {\n  return 1;\n}\n", "1-2"],
      ["typescript", "interface A {\n  x: number;\n}\n", "1-2"],
      ["tsx", "const e = (\n  <div>\n    x\n  </div>\n);\n", "2-3"],
      ["json", '{\n  "a": [\n    1\n  ]\n}\n', "1-4"],
      ["bash", "f() {\n  echo hi\n}\n", "1-2"],
      ["css", "a {\n  color: red;\n}\n", "1-2"],
      ["html", "<div>\n  <p>x</p>\n</div>\n", "1-2"],
      ["yaml", "a:\n  b: 1\n  c: 2\n", "1-3"],
      ["toml", "[a]\nb = 1\nc = 2\n", "1-3"],
    ];
    for (const [g, text, first] of samples) {
      const { r } = await foldsOf(e, g, text);
      expect(show(r.ranges)[0], g).toBe(first);
    }
  });
});

describe("the engine's rules for folds", () => {
  it("a request for a version the worker never saw answers null (Monaco folds by indentation; the client drops it)", async () => {
    const e = await nodeEngine();
    const { id } = await foldsOf(e, "cpp", CPP);
    expect(e.folds(id, 2).ranges).toBeNull();
    expect(e.folds("nope", 1)).toMatchObject({ version: -1, ranges: null, state: "basic" });
  });

  it("after an edit, the folds follow the new text", async () => {
    const e = await nodeEngine();
    const { id } = await foldsOf(e, "python", "def f():\n    pass\n");
    expect(show(e.folds(id, 1).ranges)).toEqual(["1-2"]);
    // Append a second function.
    const at = "def f():\n    pass\n".length;
    e.change(id, 2, [{ offset: at, length: 0, text: "\ndef g():\n    return 1\n" }]);
    expect(show(e.folds(id, 2).ranges)).toEqual(["1-2", "4-5"]);
  });

  it("a reparse over budget reads the folds from the last tree, edited to the text (stale), never none", async () => {
    const e = await nodeEngine();
    const src = "int f() {\n  return 1;\n}\n";
    const { id } = await foldsOf(e, "cpp", src);
    expect(show(e.folds(id, 1).ranges)).toEqual(["1-2"]);
    // Two lines inserted at the top, and every reparse is now over budget.
    e.change(id, 2, [{ offset: 0, length: 0, text: "// a\n// b\n" }]);
    const parse = (e as any).parse.bind(e);
    (e as any).parse = (g: GrammarName, text: string, old: unknown, budget: number) => (old ? null : parse(g, text, old, budget));
    const r = e.folds(id, 2);
    expect(r.stale).toBe(true);
    expect(show(r.ranges)).toEqual(["3-4"]); // moved down with the edit
  });

  it("the folds query has a time budget: past it, null (indentation), and twice running the document folds by indentation from then on", async () => {
    const clock = { t: 0 };
    let slow = true;
    const stops: boolean[] = [];
    const e = await nodeEngine({ now: () => clock.t, load: slowFolds(clock, () => slow, stops) });
    const id = "budget";
    await e.open(id, "cpp", CPP, 1, "cpp");
    const first = e.folds(id, 1);
    expect(stops).toEqual([true]); // the run was told to stop
    expect(first.ranges).toBeNull();
    expect(first.state).toBe("ready"); // colour is untouched
    e.change(id, 2, [{ offset: CPP.length, length: 0, text: "\n" }]);
    expect(e.folds(id, 2).ranges).toBeNull();
    expect(stops).toEqual([true, true]);
    // From then on it is not run at all, however fast it would be now.
    slow = false;
    e.change(id, 3, [{ offset: CPP.length, length: 0, text: "\n" }]);
    expect(e.folds(id, 3).ranges).toBeNull();
    expect(stops).toEqual([true, true]);
    expect(e.state(id)?.state).toBe("ready");
    expect(e.tokens(id, 3, 0, 5).data?.length).toBeGreaterThan(0);
  });

  it("reading the matches shares the query's budget: a file with thousands of folds that takes too long to read answers null", async () => {
    // The query itself runs at no cost; once it has, each look at the clock is 1 ms later.
    const clock = { t: 0, step: 0, slow: true };
    const e = await nodeEngine({
      now: () => (clock.t += clock.step),
      budgets: { matchMs: 5 },
      load: async (g) => {
        const a = await assets(g);
        const q = a.folds!;
        const folds = new Proxy(q, {
          get(target, prop) {
            if (prop === "matches") {
              return (...args: Parameters<Query["matches"]>) => {
                clock.step = 0;
                const out = target.matches(...args);
                clock.step = clock.slow ? 1 : 0;
                return out;
              };
            }
            const v = Reflect.get(target, prop);
            return typeof v === "function" ? v.bind(target) : v;
          },
        });
        return { ...a, folds };
      },
    });
    const src = "void f(int x) {\n  g(x);\n}\n".repeat(2000);
    await e.open("many", "cpp", src, 1, "cpp");
    expect(e.folds("many", 1).ranges).toBeNull();
    clock.slow = false;
    e.change("many", 2, [{ offset: 0, length: 0, text: "\n" }]);
    expect(e.folds("many", 2).ranges).toHaveLength(2000);
  });

  it("a cancelled run's partial matches are never folds: one slow run, then a fast one, folds again", async () => {
    const clock = { t: 0 };
    let slow = 1;
    const stops: boolean[] = [];
    const e = await nodeEngine({ now: () => clock.t, load: slowFolds(clock, () => slow-- > 0, stops) });
    await e.open("once", "cpp", CPP, 1, "cpp");
    expect(e.folds("once", 1).ranges).toBeNull();
    expect(show(e.folds("once", 1).ranges)).toContain("6-15");
  });

  it("asked twice for one version (Monaco's folding and its sticky scroll), the query runs once", async () => {
    const e = await nodeEngine();
    const { id } = await foldsOf(e, "cpp", CPP);
    let runs = 0;
    const matches = (e as any).matches.bind(e);
    (e as any).matches = (...a: unknown[]) => {
      runs++;
      return matches(...a);
    };
    const a = e.folds(id, 1);
    const b = e.folds(id, 1);
    expect(runs).toBe(0); // the open's own answer above is cached already
    expect(b.ranges).toBe(a.ranges);
  });

  it("a document past the size budget is never parsed, and so never folded from a tree", async () => {
    const e = await nodeEngine();
    const big = "x = 1\n".repeat((e.budgets.maxChars / 6) + 10);
    const s = await e.open("big", "python", big, 1, "python");
    expect(s).toMatchObject({ state: "basic", reason: "large file" });
    expect(e.folds("big", 1)).toMatchObject({ ranges: null, state: "basic" });
  });
});
