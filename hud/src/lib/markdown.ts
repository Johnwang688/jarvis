// Markdown for his replies and for docs, with v1's rules intact
// (CLAUDE.md, "Markdown: rendered in the HUD, stripped for speech").
//
// This window draws authorization cards, so a reply that could inject markup
// into it could draw its own AUTHORIZE button. Three layers, and the order
// matters: markdown-it with `html: false` never emits raw HTML in the first
// place; DOMPurify rebuilds what is left from an allowlist; and then every
// anchor is scheme-checked by hand, because "this is a valid <a>" and "this
// href is safe to click" are different questions.
//
// Only *his* messages are rendered. The owner's own line goes up verbatim,
// because typing `*foo*` means `*foo*`.

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

const md = new MarkdownIt({
  html: false, // never pass raw HTML through
  linkify: false, // a bare URL in prose is text, not a click target
  breaks: true,
  typographer: false,
});

const ALLOWED_TAGS = [
  "p", "br", "strong", "em", "s", "del", "code", "pre", "blockquote",
  "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "a",
  "table", "thead", "tbody", "tr", "th", "td",
];

const ALLOWED_SCHEMES = ["http:", "https:", "mailto:"];

export function safeHref(href: string | null): string | null {
  if (!href) return null;
  const raw = href.trim();
  if (raw.startsWith("#")) return null; // a fragment goes nowhere useful here
  try {
    // Resolved against the document so a relative href is judged as what it
    // would actually navigate to.
    const url = new URL(raw, "http://127.0.0.1:8402/");
    return ALLOWED_SCHEMES.includes(url.protocol) ? raw : null;
  } catch {
    return null;
  }
}

/** Sanitized HTML for `text`. Anchors are already scheme-checked. */
export function renderMarkdown(text: string): string {
  const dirty = md.render(text ?? "");
  const clean = DOMPurify.sanitize(dirty, {
    ALLOWED_TAGS,
    ALLOWED_ATTR: ["href", "title", "align"],
    ALLOW_DATA_ATTR: false,
    RETURN_DOM_FRAGMENT: true,
  }) as unknown as DocumentFragment;

  // A link whose scheme is not allowed renders as plain text rather than as a
  // dead anchor: an anchor the owner can click and that does nothing is worse
  // than words, and `javascript:` must never survive as an href at all.
  const anchors = Array.from(clean.querySelectorAll("a"));
  for (const a of anchors) {
    const ok = safeHref(a.getAttribute("href"));
    if (!ok) {
      a.replaceWith(...Array.from(a.childNodes));
      continue;
    }
    a.setAttribute("href", ok);
    a.setAttribute("target", "_blank"); // the HUD itself never navigates
    a.setAttribute("rel", "noopener noreferrer");
  }

  const host = document.createElement("div");
  host.appendChild(clean);
  return host.innerHTML;
}

/** Render into an element. Never assigns un-sanitized text to innerHTML. */
export function renderMarkdownInto(el: HTMLElement, text: string) {
  el.innerHTML = renderMarkdown(text);
}
