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

export { monaco };
