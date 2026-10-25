// An assistant's reply on the phone: the HUD's sanitized Markdown renderer
// (lib/markdown.ts: markdown-it with `html: false`, then DOMPurify from an
// allowlist, then every link's scheme checked), shared as the design's PH-9
// says, and made one step stricter for the phone's Content-Security-Policy.
//
// The phone's CSP is C1's (`style-src 'self'`, no `'unsafe-inline'`), so
// a `style` attribute in markup would be refused by the browser anyway, and
// KaTeX's HTML layer is built from them. Rather than widen the policy, the
// phone keeps KaTeX's MathML layer (which iOS Safari draws natively) and
// drops the HTML layer and every `style` attribute before anything reaches
// the live document. The sanitized string is parsed in an inert <template>
// (no script runs, no resource loads, no policy is consulted there).

import { renderMarkdown } from "../lib/markdown";

/** Sanitized, style-free nodes for `text`. */
export function phoneMarkdown(text: string): DocumentFragment {
  const template = document.createElement("template");
  template.innerHTML = renderMarkdown(text);
  const content = template.content;
  for (const el of Array.from(content.querySelectorAll(".katex-html"))) el.remove();
  for (const el of Array.from(content.querySelectorAll("[style]"))) el.removeAttribute("style");
  return content;
}

/** Render into `el`, replacing what it held. */
export function renderPhoneMarkdown(el: HTMLElement, text: string): void {
  el.replaceChildren(phoneMarkdown(text));
}
