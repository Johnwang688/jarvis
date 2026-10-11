// Monaco as the HUD loads it (editor plan §2.2, ED-1). This module is only
// ever reached through `loadMonaco()`'s dynamic import, so everything here is
// one lazy chunk that a window which never opens a File or Diff view never
// fetches.
//
// **A curated feature list, not `editor.main`.** Monaco 0.56+ ships each
// editor feature as its own `features/<name>/register` entry. These are the
// ones that work in every language with no language service behind them:
// find/replace, folding, comment toggle, multi-cursor, brackets, word
// suggestions, sticky scroll, go to line, the command list. Left out on
// purpose, because they only light up with a language server and the owner's
// answer is "Claude Code is my intellisense" (decisions E-1): rename,
// gotoSymbol, referenceSearch, quickOutline, parameterHints, codeAction,
// codelens, inlayHints, colorPicker, inlineCompletions, format,
// documentSymbols — and every language-service worker (TypeScript, JSON, CSS,
// HTML), which is why no `languages/features/*` module is imported here.
//
// Additions to the plan's list, each for a reason that is not taste:
//  - `editor/contrib/suggest/browser/suggestController`: 0.57's
//    `features/suggest/register` brings only the inline half; the widget that
//    Ctrl+Space opens is this module (editor.main imports it separately).
//  - `features/codicon/register`: the icon font the find widget, the
//    suggest widget and the context menu draw their buttons with.
//  - `features/anchorSelect/register`: Ctrl+K Ctrl+B (set selection anchor),
//    the chord the HUD's Ctrl+B rule stands aside for (decisions E-10).
//  - `features/lineSelection/register` (Ctrl+L) and
//    `features/toggleTabFocusMode/register` (Ctrl+M, so Tab can leave the
//    editor — a keyboard user is otherwise trapped in it).

import * as monaco from "monaco-editor/editor";
import { ContextView } from "monaco-editor/base/browser/ui/contextview/contextview";
import { effectiveZoom, placePopup, type Rect } from "./zoomfix";
import { cssPalette, themeData } from "./editorTheme";
import { C_WORDS, PY_WORDS, refine, type WordClasses } from "./monarchRefine";

import "monaco-editor/features/codeEditor/register";
import "monaco-editor/features/codicon/register";
import "monaco-editor/features/diffEditor/register";

import "monaco-editor/features/anchorSelect/register";
import "monaco-editor/features/bracketMatching/register";
import "monaco-editor/features/caretOperations/register";
import "monaco-editor/features/clipboard/register";
import "monaco-editor/features/comment/register";
import "monaco-editor/features/contextmenu/register";
import "monaco-editor/features/cursorUndo/register";
import "monaco-editor/features/dnd/register";
import "monaco-editor/features/find/register";
import "monaco-editor/features/folding/register";
import "monaco-editor/features/gotoLine/register";
import "monaco-editor/features/hover/register";
import "monaco-editor/features/inPlaceReplace/register";
import "monaco-editor/features/indentation/register";
import "monaco-editor/features/insertFinalNewLine/register";
import "monaco-editor/features/lineSelection/register";
import "monaco-editor/features/linesOperations/register";
import "monaco-editor/features/links/register";
import "monaco-editor/features/middleScroll/register";
import "monaco-editor/features/multicursor/register";
import "monaco-editor/features/placeholderText/register";
import "monaco-editor/features/quickCommand/register";
import "monaco-editor/features/readOnlyMessage/register";
import "monaco-editor/features/semanticTokens/register";
import "monaco-editor/features/smartSelect/register";
import "monaco-editor/features/snippet/register";
import "monaco-editor/features/stickyScroll/register";
import "monaco-editor/features/suggest/register";
import "monaco-editor/editor/contrib/suggest/browser/suggestController";
import "monaco-editor/features/toggleTabFocusMode/register";
import "monaco-editor/features/unicodeHighlighter/register";
import "monaco-editor/features/wordHighlighter/register";
import "monaco-editor/features/wordOperations/register";
import "monaco-editor/features/wordPartOperations/register";

// About 80 Monarch definitions. Registering one is a few bytes; its tokenizer
// is a chunk of its own, fetched only when a model of that language opens.
// This is the fallback colour under tree-sitter (ED-5) and the source of each
// language's comment, bracket and auto-close rules.
import "monaco-editor/languages/definitions/register.all";

// Monaco's context view (the right-click menu, its buttons' tooltips) under the
// HUD's CSS zoom. It lays itself out in viewport pixels from a size it measures
// unzoomed and writes CSS lengths, which the zoom on #root scales again — at
// 160% a right-click in the middle of the window opened the menu off its
// right edge, and at 70% a flip upward stopped 45 px short of the pointer.
// After its own layout, the popup is placed again with its size as drawn
// (`placePopup`) and the answer converted to the CSS lengths that land it
// there. Monaco patches its own find widget the same way (features/find).
const doLayout = ContextView.prototype.doLayout;
ContextView.prototype.doLayout = function patchedDoLayout(this: any) {
  doLayout.call(this);
  if (!this.isVisible?.() || !this.view || !this.delegate) return;
  const view = this.view as HTMLElement;
  const z = effectiveZoom(view);
  if (Math.abs(z - 1) < 1e-6) return;
  const raw = this.delegate.getAnchor();
  let anchor: Rect;
  if (raw instanceof HTMLElement) {
    const r = raw.getBoundingClientRect();
    anchor = { left: r.left, top: r.top, width: r.width, height: r.height };
  } else if (typeof raw?.x === "number") {
    anchor = { left: raw.x, top: raw.y, width: raw.width || 1, height: raw.height || 2 };
  } else {
    anchor = { left: raw?.posx ?? 0, top: raw?.posy ?? 0, width: 2, height: 2 };
  }
  // Measured where the lengths are zero: the origin they count from, and the
  // size with the whole width available (near an edge a popup can wrap).
  view.style.left = "0px";
  view.style.top = "0px";
  const origin = view.getBoundingClientRect();
  const at = placePopup(
    anchor,
    { width: origin.width, height: origin.height },
    { width: window.innerWidth, height: window.innerHeight },
    { above: this.delegate.anchorPosition === 1, alignRight: this.delegate.anchorAlignment === 1 },
  );
  view.style.left = `${(at.left - origin.left) / z}px`;
  view.style.top = `${(at.top - origin.top) / z}px`;
};

// The Graphite editor scheme (ED-3, editor plan §2.5). Here, in the lazy
// chunk, rather than in lib/monaco.ts: the scheme's tables stay out of the
// first-paint chunk, and the refined tokenizers' imports of C++ and Python —
// whose definition modules import Monaco's API — are reached only from this
// chunk, so Monaco is not split into a second one to serve them.
export function applyGraphite() {
  // One theme, from the tokens on :root: theme.css is the one place a colour
  // is written (lib/editorTheme.ts).
  monaco.editor.defineTheme("jarvis", themeData(cssPalette()));
  // C, C++ and Python tell functions, builtins, types and constants apart
  // (lib/monarchRefine.ts). Each replaces the tokenizer `register.all`
  // registered (the last registration wins) with the same module, refined —
  // or as it was, if its shape has moved.
  const refined = (id: string, load: () => Promise<{ language: unknown }>, words: WordClasses) =>
    monaco.languages.registerTokensProviderFactory(id, {
      create: async () => {
        const { language } = await load();
        return (refine(language, words) ?? language) as any;
      },
    });
  refined("c", () => import("monaco-editor/languages/definitions/cpp/cpp"), C_WORDS);
  refined("cpp", () => import("monaco-editor/languages/definitions/cpp/cpp"), C_WORDS);
  refined("python", () => import("monaco-editor/languages/definitions/python/python"), PY_WORDS);
}

export { monaco };
