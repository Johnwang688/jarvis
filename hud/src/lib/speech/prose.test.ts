import { describe, expect, it } from "vitest";
import { ProseFilter, codeSpan, pathBasename, proseOf, tokens } from "./prose";

/** The text delivered in pieces of `size` characters. */
function chunked(text: string, size: number): string {
  const f = new ProseFilter();
  let out = "";
  for (let i = 0; i < text.length; i += size) out += f.push(text.slice(i, i + size));
  return out + f.end();
}

const DOC = [
  "Here is the plan. I will change two files.",
  "",
  "```ts",
  "const x = 1;",
  "console.log(x);",
  "```",
  "",
  "Then the tests run. They pass.",
  "",
  "- first item is done",
  "- second item, with `code(x)` inside",
  "",
  "| name | qty |",
  "|---|---|",
  "| bolt | 12 |",
  "",
  "Done.",
].join("\n");

describe("the fence", () => {
  it("drops fenced code and keeps the prose around it", () => {
    const out = proseOf("Before it.\n\n```py\nx = 1\nprint(x)\n```\n\nAfter it.");
    expect(out).toContain("Before it.");
    expect(out).toContain("After it.");
    expect(out).not.toMatch(/x = 1|print/);
  });

  it("drops a fence whose marker arrives split across deltas, however it is cut", () => {
    const text = "Intro line.\n```python\nsecret_code()\n```\nOutro line.\n";
    for (let size = 1; size <= 9; size++) {
      const out = chunked(text, size);
      expect(out, `size ${size}`).not.toContain("secret_code");
      expect(out, `size ${size}`).toContain("Intro line.");
      expect(out, `size ${size}`).toContain("Outro line.");
    }
    // Every two-way split of the whole text, including inside the fence markers.
    for (let i = 1; i < text.length; i++) {
      const f = new ProseFilter();
      const out = f.push(text.slice(0, i)) + f.push(text.slice(i)) + f.end();
      expect(out, `split at ${i}`).not.toContain("secret_code");
      expect(out, `split at ${i}`).toContain("Outro line.");
    }
  });

  it("honours a longer fence and a tilde fence, and a fence that never closes speaks nothing after it", () => {
    expect(proseOf("A.\n````\n```\ninner\n```\n````\nB.")).not.toContain("inner");
    expect(proseOf("A.\n~~~\ncode\n~~~\nB.")).not.toContain("code");
    const unclosed = proseOf("Start here.\n```\nnever closed\nstill code");
    expect(unclosed).toContain("Start here.");
    expect(unclosed).not.toContain("never closed");
    expect(proseOf("```\nonly code\n```")).toBe("");
  });

  it("starts each message outside any fence", () => {
    const f = new ProseFilter();
    f.push("Text.\n```\nopen forever");
    f.end();
    expect(f.push("Fresh message.\n") + f.end()).toContain("Fresh message.");
  });
});

describe("independence from where the deltas fall", () => {
  it("gives the same speech for every delta size", () => {
    const whole = proseOf(DOC);
    for (const size of [1, 2, 3, 5, 8, 13, 40]) expect(chunked(DOC, size), `size ${size}`).toBe(whole);
  });
});

describe("diffs, indented code, tables, diagnostics", () => {
  it("drops an unfenced diff", () => {
    const diff = ["I changed it:", "", "diff --git a/x.py b/x.py", "index 1a2b3c4..5d6e7f8 100644", "--- a/x.py", "+++ b/x.py",
      "@@ -1,2 +1,2 @@", " keep", "-old line", "+new line", "", "That is the diff."].join("\n");
    const out = proseOf(diff);
    expect(out).toContain("I changed it:");
    expect(out).toContain("That is the diff.");
    expect(out).not.toMatch(/old line|new line|keep|@@|diff --git/);
    expect(chunked(diff, 3)).toBe(out);
  });

  it("drops indented code after a blank line but not a list continuation or a lazy line", () => {
    const code = proseOf("Run this:\n\n    npm run build\n    npm test\n\nThen it works.");
    expect(code).not.toContain("npm");
    expect(code).toContain("Then it works.");
    const list = proseOf("- an item\n\n    continues the item\n\nDone.");
    expect(list).toContain("continues the item");
    expect(proseOf("A paragraph\n    with a lazy line\nends.")).toContain("lazy line");
  });

  it("reads a short table as a list and a long one as 'a table'", () => {
    expect(proseOf("| name | qty |\n|---|---|\n| bolt | 12 |\n")).toBe("name, qty\nbolt, 12\n");
    const long = ["| a | b |", "|---|---|", "| 1 | 2 |", "| 3 | 4 |", "| 5 | 6 |", "| 7 | 8 |"].join("\n");
    const out = proseOf("Results:\n" + long + "\nAll good.");
    expect(out).toContain("A table.");
    expect(out).not.toMatch(/[0-9]/);
    expect(out).toContain("All good.");
    expect(chunked("Results:\n" + long + "\nAll good.", 2)).toBe(out);
  });

  it("never speaks a bracketed diagnostic, nor the fast path's cut-off marker", () => {
    expect(proseOf("[session not saved: disk is full]")).toBe("");
    expect(proseOf("Real prose.\n[session not saved: disk is full]\nMore prose.")).not.toContain("session");
    const cut = proseOf("The answer is long and it stops here [cut off at the token limit]");
    expect(cut).toContain("stops here");
    expect(cut).not.toContain("cut off");
    expect(proseOf("It ends. [cut off at the token limit]")).not.toContain("cut off");
    // A markdown link is not a diagnostic.
    expect(proseOf("[the docs](https://example.com) say so.")).toContain("the docs say so.");
  });

  it("drops rules", () => {
    expect(proseOf("One.\n\n---\n\nTwo.")).not.toContain("-");
  });
});

describe("inline code, paths, links, ids", () => {
  it("reads identifiers and file names, drops real code", () => {
    expect(codeSpan("context.py")).toBe("context.py");
    expect(codeSpan("snake_case_name")).toBe("snake_case_name");
    expect(codeSpan("foo(bar)")).toBe("");
    expect(codeSpan("x = compute(1)")).toBe("");
    expect(codeSpan("rm -rf build dir")).toBe("");
    expect(codeSpan("a".repeat(60))).toBe("");
    const out = proseOf("Call `foo(bar)` to start, then open `src/lib/speech/queue.ts`.");
    expect(out).toBe("Call to start, then open queue.ts.\n".replace(/\n$/, ""));
  });

  it("says a path as its basename, a URL as a link, hashes and ids as such", () => {
    expect(pathBasename("/home/johnw/projects/Jarvis/hud/src/App.tsx:120")).toBe("App.tsx");
    expect(pathBasename("and/or")).toBeNull();
    expect(tokens("see jarvis/face/server.py, line 60.")).toBe("see server.py, line 60.");
    expect(tokens("go to https://example.com/a/b?x=1.")).toBe("go to a link.");
    expect(tokens("fixed in 5d6e7f8a1 and abc123def456abc123def456abc123def456abcd")).toBe("fixed in a commit and a commit");
    expect(tokens("id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 ok")).toBe("id an id ok");
    expect(tokens("input/output and 1/2 on 2026/10/13")).toBe("input/output and 1/2 on 2026/10/13");
    expect(tokens("1234567 deadbeef")).toBe("1234567 deadbeef");
  });
});

describe("early release", () => {
  it("releases each finished sentence of a long line before its newline", () => {
    const f = new ProseFilter();
    const a = f.push("The first sentence is done. And the second is");
    expect(a).toBe("The first sentence is done. ");
    expect(f.push(" still going. Third")).toBe("And the second is still going. ");
    expect(f.end()).toBe("Third");
  });

  it("holds a line whose kind is not known yet", () => {
    for (const lead of ["``", "```", "| a", "    x", "[note", "~~~", "diff --git", "@@"]) {
      const f = new ProseFilter();
      expect(f.push(lead + " one. two. "), lead).toBe("");
    }
  });

  it("does not cut inside an open code span or bold", () => {
    const f = new ProseFilter();
    expect(f.push("Use `a. b` here and **bold. text** too. Next")).toBe("Use a. b here and bold. text too. ");
    // The span's closing tick has not arrived: nothing is released inside it.
    const g = new ProseFilter();
    expect(g.push("Use `a. b ")).toBe("");
    expect(g.push("` now. Next")).toBe("Use a. b now. ");
  });
});

describe("fences the review found leaking", () => {
  const NESTED = "1. Do this:\n    ```bash\n    cat ~/.config/jarvis/.env\n    export KEY=sk-abcdef\n    ```\nDone.\n";
  const LISTED = "- a\n  - b\n    ```py\n    secret_value = 1\n    ```\nAfter it.\n";
  const MIDLINE = "Here is the code: ```python\nprint('leaked')\n```\nThen prose.\n";
  const QUOTED = "> Look at this:\n> ```py\n> quoted_secret = 1\n> ```\nBack to prose.\n";
  const DIFF = "Changed:\n\ndiff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n keep one\n \n-removed_line\n+added_line\n keep two\n\nDiff over.\n";

  /** Whole, then in every delta size and every two-way split: never leaks, never loses the prose. */
  function everyway(text: string, leaks: RegExp, keeps: string[]) {
    const outs = [proseOf(text)];
    for (const size of [1, 2, 3, 7]) outs.push(chunked(text, size));
    for (let i = 1; i < text.length; i++) {
      const f = new ProseFilter();
      outs.push(f.push(text.slice(0, i)) + f.push(text.slice(i)) + f.end());
    }
    for (const out of outs) {
      expect(out).not.toMatch(leaks);
      for (const k of keeps) expect(out).toContain(k);
    }
  }

  it("does not speak a fence nested under a list item", () => {
    everyway(NESTED, /cat|\.env|KEY|sk-abc|export/, ["Do this:", "Done."]);
    everyway(LISTED, /secret_value|py\b/, ["After it."]);
  });

  it("honours an indented closing fence, a longer fence, and a fence that never closes", () => {
    expect(proseOf("A.\n  ```\n  code\n  ```\nB.")).not.toContain("code");
    expect(proseOf("A.\n    ````\n    ```\n    inner\n    ```\n    ````\nB.")).not.toContain("inner");
    const open = proseOf("Start.\n    ```sh\n    never_closed\n    still code");
    expect(open).toContain("Start.");
    expect(open).not.toContain("never_closed");
  });

  it("opens a fence in the middle of a line", () => {
    everyway(MIDLINE, /leaked|python/, ["Here is the code:", "Then prose."]);
    expect(proseOf("Use ```x = 1``` inline, then go on.")).not.toContain("x = 1");
    expect(proseOf("Use ```x = 1``` inline, then go on.")).toContain("then go on.");
  });

  it("opens a fence inside a blockquote, and closes it there", () => {
    everyway(QUOTED, /quoted_secret/, ["Look at this:", "Back to prose."]);
  });

  it("keeps a diff going through a context line that is a single space", () => {
    everyway(DIFF, /removed_line|added_line|keep one|keep two/, ["Changed:", "Diff over."]);
  });
});

describe("list numbers are not sentences", () => {
  it("never speaks '1.' on its own, whatever the delta size", () => {
    const text = "1. First thing happens.\n2. Second thing follows after it.\n10) Tenth item arrives last.\n";
    for (const size of [1, 2, 3, 5]) {
      const f = new ProseFilter();
      const pieces: string[] = [];
      for (let i = 0; i < text.length; i += size) pieces.push(f.push(text.slice(i, i + size)));
      pieces.push(f.end());
      const out = pieces.join("");
      expect(out, `size ${size}`).toBe(proseOf(text));
      expect(out).toContain("First thing happens.");
      expect(pieces.filter(Boolean).some((p) => /^\s*\d+[.)]\s*$/.test(p)), `size ${size}`).toBe(false);
    }
  });
});
