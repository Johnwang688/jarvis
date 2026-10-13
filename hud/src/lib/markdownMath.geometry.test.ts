import { expect, it } from "vitest";
import { renderMarkdown } from "./markdown";

function render(source: string) {
  const node = document.createElement("div");
  node.innerHTML = renderMarkdown(source);
  return node;
}
it("leaves extreme negative spacing and positive/negative raisebox offsets literal", () => {
  for (const source of [
    "$\\kern{-1000000em}x$",
    "$\\raisebox{-1000000em}{x}$",
    "$\\raisebox{1000000em}{x}$",
    "$\\llap{\\kern{-1000000em}x}y$",
    "$\\rlap{\\raisebox{-1000000em}{x}}y$",
    "$\\llap{\\raisebox{1000000em}{x}}y$",
    "$\\kern{-" + "9".repeat(400) + "em}x$",
  ]) {
    const node = render(source);
    expect(node.querySelector(".katex"), source).toBeNull();
    expect(node.textContent?.trim(), source).toBe(source);
    expect(node.querySelector("[style]"), source).toBeNull();
  }
});
it("preserves ordinary negative spacing and valid equation geometry", () => {
  for (const source of ["$\\kern{-1em}x$", "$\\raisebox{-1em}{x}$",
    "$\\raisebox{1em}{x}$", "$\\llap{x}y$", "$\\frac{f(b)-f(a)}{b-a}$"]) {
    expect(render(source).querySelector(".katex"), source).not.toBeNull();
  }
});
it("caps explicit positive lap sizes while retaining long natural widths", () => {
  const lap = render("$\\llap{\\rule{1000000em}{1em}}x$");
  expect(lap.querySelector(".katex")).not.toBeNull();
  expect(lap.querySelector('[style*="border-right-width:20em"]')).not.toBeNull();
  const long = render("$$\\overbrace{" + "x+".repeat(100) + "x}$$");
  expect(long.querySelector(".math-display .katex")).not.toBeNull();
  // KaTeX deliberately draws stretchy SVGs wider than their clipped parent.
  expect(long.querySelector('svg[width="400em"]')).not.toBeNull();
});
