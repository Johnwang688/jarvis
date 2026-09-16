// The sanitizer's rules. This window draws authorization cards, so a reply
// that could inject markup into it could draw its own AUTHORIZE button.

import { describe, expect, it } from "vitest";
import { renderMarkdown, safeHref } from "./markdown";

/** Parse the rendered HTML back into a DOM — asserting on elements rather than
 *  on substrings, because escaped text legitimately *contains* the word
 *  "onerror" while carrying no handler at all. */
function dom(html: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host;
}
const elements = (html: string, sel: string) => Array.from(dom(html).querySelectorAll(sel));
const text = (html: string) => dom(html).textContent || "";
const everyAttr = (html: string) =>
  elements(html, "*").flatMap((el) => Array.from(el.attributes).map((a) => a.name));

describe("markdown rendering", () => {
  it("renders the constructs the model actually writes", () => {
    const html = renderMarkdown(
      "# Head\n\n**bold** and `code`\n\n- one\n- two\n\n> quoted\n\n```\nfenced\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
    );
    for (const tag of ["<h1>", "<strong>", "<code>", "<ul>", "<li>", "<blockquote>", "<pre>", "<table>"])
      expect(html).toContain(tag);
  });

  it("renders a reply carrying markup as text", () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)"> <b>hi</b>');
    // Nothing becomes an element: the words are there, the tags are not.
    expect(elements(html, "img")).toHaveLength(0);
    expect(elements(html, "b")).toHaveLength(0);
    expect(text(html)).toContain("<img src=x");
  });

  it("drops a script tag entirely", () => {
    const html = renderMarkdown("<script>window.__pwned = 1</script>ok");
    expect(html.toLowerCase()).not.toContain("<script");
    expect(html).toContain("ok");
  });

  it("renders a javascript: link as text, not as an anchor", () => {
    // Two spellings, because markdown-it refuses the plain one itself and the
    // scheme check in renderMarkdown is the layer that has to catch the rest.
    for (const src of ["[click](javascript:alert(1))", "[click](JaVaScRiPt:alert(1))"]) {
      const html = renderMarkdown(src);
      expect(elements(html, "a")).toHaveLength(0);
      expect(text(html)).toContain("click");
    }
  });

  it("unwraps an anchor whose scheme is refused rather than leaving a dead link", () => {
    // `ftp:` and `tel:` are the cases that reach the scheme check at all:
    // markdown-it refuses `javascript:`/`data:` itself, so testing only those
    // leaves this layer unexercised — and it is the layer that has to hold if
    // the renderer is ever swapped.
    for (const src of ["before [x](ftp://host/f) after", "call [me](tel:+15551234)"]) {
      const html = renderMarkdown(src);
      expect(elements(html, "a")).toHaveLength(0);
      expect(html).not.toContain("href");
    }
    // The words survive: unwrapped, not dropped.
    expect(text(renderMarkdown("before [x](ftp://host/f) after"))).toContain("before x after");
  });

  it("keeps http/https/mailto links, opened away from the HUD", () => {
    const html = renderMarkdown("[x](https://example.com) [m](mailto:a@b.c)");
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain("mailto:a@b.c");
  });

  it("refuses every other scheme", () => {
    for (const bad of ["javascript:alert(1)", "data:text/html,<b>x", "vbscript:x", "file:///etc/passwd"])
      expect(safeHref(bad)).toBeNull();
    for (const ok of ["https://x.test/y", "http://localhost:5173/", "mailto:a@b.c"])
      expect(safeHref(ok)).toBe(ok);
  });

  it("never emits an event-handler attribute, whatever the source says", () => {
    const html = renderMarkdown('<div onclick="x()">t</div>\n\n<a href="#" onmouseover="y()">z</a>');
    // Checked on real attributes, not on the text: this is the window that
    // gates approvals, so an on* handler surviving would be the whole hole.
    expect(everyAttr(html).filter((n) => n.startsWith("on"))).toHaveLength(0);
  });

  it("is empty-safe", () => {
    expect(renderMarkdown("")).toBe("");
  });
});
