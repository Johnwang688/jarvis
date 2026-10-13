import { describe, expect, it } from "vitest";
import { renderMarkdown, renderMarkdownInto } from "./markdown";
import { MAX_FORMULA, MAX_FORMULAS, MAX_MATH_SOURCE } from "./markdownMath";
const dom = (source: string) => {
  const node = document.createElement("div");
  node.innerHTML = renderMarkdown(source);
  return node;
};
describe("Markdown math", () => {
  it("renders the reported MVT inline comparison and display fraction", () => {
    const node = dom("For $a<b$ there is a point $c$ such that:\n$$f'(c)=\\frac{f(b)-f(a)}{b-a}.$$");
    expect(node.querySelectorAll(".katex")).toHaveLength(3);
    expect(node.querySelector(".katex-display .mfrac")).not.toBeNull();
    expect(node.querySelector("math mfrac")).not.toBeNull();
  });
  it("supports numeric dollar math and both model backslash delimiters", () => {
    for (const source of ["$2$", "$1/2$", "$2x+1$", "$2 + 2 = 4$", "$2 \\pi$",
      "\\(a<b\\)", "\\[\\frac{1}{2}\\]", "$$\\frac{1}{2}$$",
      "$$\n\\begin{aligned}a&=b\\\\ c&=d\\end{aligned}\n$$",
      "\\[\nx+y\n\n=z\n\\]"]) {
      expect(dom(source).querySelector(".katex"), source).not.toBeNull();
    }
  });
  it("keeps prices, escaped delimiters, inline code and fences literal", () => {
    for (const source of ["It costs $5 and $10.", "\\$5 and \\$10", "\\$a<b\\$",
      "`$x$`", "```\n$$x$$\n\\(x\\)\n```", "    $$x$$",
      "`\\(x\\)`", "\\\\(x\\\\)", "``$x$``"]) {
      expect(dom(source).querySelector(".katex"), source).toBeNull();
    }
    expect(dom("It costs $5 and $10.").textContent).toContain("It costs $5 and $10.");
    expect(dom("\\$a<b\\$").textContent).toContain("$a<b$");
  });
  it("keeps incomplete math readable and rerenders once the stream completes", () => {
    for (const source of ["$x+", "$$\\frac{1}", "\\(x+", "\\[x+", "$ x $", "$$"]) {
      const node = dom(source);
      expect(node.querySelector(".katex")).toBeNull();
      expect(node.textContent?.trim(), source).toBe(source);
    }
    const node = document.createElement("div");
    renderMarkdownInto(node, "$$\\frac{1}");
    expect(node.querySelector(".katex")).toBeNull();
    renderMarkdownInto(node, "$$\\frac{1}{2}$$");
    expect(node.querySelector(".mfrac")).not.toBeNull();
  });
  it("handles math beside Markdown and preserves unsupported source safely", () => {
    const node = dom("**Result:** $x^2$ and [details](https://example.com)\n\n$\\notACommand{<img src=x onerror=alert(1)>}$");
    expect(node.querySelector("strong")?.textContent).toBe("Result:");
    expect(node.querySelectorAll(".katex")).toHaveLength(1);
    expect(node.querySelector("a")?.getAttribute("rel")).toBe("noopener noreferrer");
    expect(node.querySelector("img")).toBeNull();
    expect(node.textContent).toContain("\\notACommand");
  });
  it("never enables TeX links, resources, arbitrary HTML classes/ids/styles or raw tags", () => {
    const commands = [
      "\\href{javascript:alert(1)}{click}", "\\href{https://evil.test/}{click}",
      "\\url{https://evil.test/}", "\\includegraphics{https://evil.test/x.png}",
      "\\htmlStyle{position:fixed;inset:0;z-index:999999}{x}",
      "\\htmlClass{auth}{x}", "\\htmlId{approve}{x}", "\\htmlData{x=1}{x}",
      "\\text{<img src=x onerror=alert(1)>}", "\\text{</span><script>alert(1)</script>}",
    ];
    for (const command of commands) {
      const node = dom("$" + command + "$");
      expect(node.querySelector("a,img,script,iframe,button,input,style,[id],[data-x]"), command).toBeNull();
      expect(node.querySelector(".auth"), command).toBeNull();
      for (const el of Array.from(node.querySelectorAll("*")))
        for (const attr of Array.from(el.attributes)) {
          expect(attr.name.startsWith("on"), command).toBe(false);
          expect(attr.value, command).not.toMatch(/position\s*:\s*fixed|url\s*\(/i);
        }
    }
  });
  it("bounds macro expansion and isolates macro definitions across expressions", () => {
    const source = "$\\def\\a{\\a}\\a$";
    expect(dom(source).querySelector(".katex")).toBeNull();
    expect(dom(source).textContent?.trim()).toBe(source);
    const node = dom("$\\gdef\\foo{z}\\foo$ then $\\foo$");
    expect(node.querySelectorAll(".katex")).toHaveLength(1);
    expect(node.textContent).toContain("$\\foo$");
  });
  it("caps user dimensions and preserves prose after math budgets are exhausted", () => {
    const sized = dom("$\\rule{1000000em}{1000000em}$");
    expect(sized.querySelector(".katex")).not.toBeNull();
    for (const el of Array.from(sized.querySelectorAll("[style]")))
      expect(el.getAttribute("style")).not.toContain("1000000");
    const many = dom(Array.from({ length: MAX_FORMULAS + 1 }, () => "$x$").join(" ") + " TAIL");
    expect(many.querySelectorAll(".katex")).toHaveLength(MAX_FORMULAS);
    expect(many.textContent).toContain("$x$ TAIL");
    const prose = "ordinary prose ".repeat(3000);
    expect(dom(prose + "$x$").querySelector(".katex")).not.toBeNull();
    expect(dom(prose + "$x$").textContent).toContain(prose.slice(0, 100));
    const huge = "$$" + "x".repeat(MAX_FORMULA + 1) + "$$";
    expect(dom(huge).querySelector(".katex")).toBeNull();
    expect(dom(huge).textContent?.trim()).toBe(huge);
    const formula = "x".repeat(MAX_FORMULA);
    const budget = dom(Array.from({ length: MAX_MATH_SOURCE / MAX_FORMULA + 1 }, () => "$" + formula + "$").join(" ") + " END");
    expect(budget.querySelectorAll(".katex")).toHaveLength(MAX_MATH_SOURCE / MAX_FORMULA);
    expect(budget.textContent).toContain(" END");
  });

  it("bounds repeated invalid delimiters without rescanning each lookahead", () => {
    for (const source of ["$ ".repeat(120000), "$1".repeat(120000),
      "$$\n" + "\n".repeat(MAX_FORMULA) + "TAIL"]) {
      const started = performance.now();
      const node = dom(source);
      expect(node.querySelector(".katex")).toBeNull();
      expect(node.textContent?.replace(/\n/g, "").trim()).toBe(source.replace(/\n/g, "").trim());
      expect(performance.now() - started).toBeLessThan(1500);
    }
  });

});
