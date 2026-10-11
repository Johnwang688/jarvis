// The semantic tokens legend tree-sitter colour hands Monaco (ED-5): ED-3's
// capture names as the token types, and the covered languages as the
// modifiers (editor plan §2.5–§2.6).
//
// Monaco styles a semantic token by joining its type and modifiers with dots
// and matching that against the theme's rules by dotted prefix, the longest
// winning (StandaloneTheme.getTokenStyleMetadata). So a token of type
// `property` with the modifier `json` is styled by the rule `property.json`
// when there is one and by `property` otherwise — ED-3's per-language rows —
// and each token carries exactly one modifier, the language it is styled as.

import { LEGEND } from "../editorTheme";
import { COVERED_LANGUAGES } from "./grammars";

/**
 * Text tree-sitter captured as nothing, or did not capture: drawn as plain
 * text (no theme rule names it, so Monaco's default, `--text`, applies — and
 * Monarch's guess underneath is replaced). nvim's own `@none` maps here too.
 */
export const NONE = "none";

export const TOKEN_TYPES: readonly string[] = [...LEGEND, NONE];
export const TOKEN_MODIFIERS: readonly string[] = [...COVERED_LANGUAGES];

export const NONE_TYPE = TOKEN_TYPES.indexOf(NONE);

const typeIndex = new Map(TOKEN_TYPES.map((t, i) => [t, i]));
const cache = new Map<string, number>();

/**
 * A capture name's token type: the longest dotted prefix of it that is in the
 * legend (`markup.heading.1` → `markup.heading`, `keyword.conditional.ternary`
 * → `keyword.conditional`), or -1 for a capture that draws nothing — nvim's
 * `@spell`, `@nospell` and `@conceal`, a private `@_name`, an injection's
 * `@injection.*`, or anything the Graphite scheme has no row for.
 */
export function typeOf(capture: string): number {
  const hit = cache.get(capture);
  if (hit !== undefined) return hit;
  let found = -1;
  if (!capture.startsWith("_") && !capture.startsWith("injection.")) {
    const parts = capture.split(".");
    for (let n = parts.length; n > 0 && found < 0; n--) {
      const t = typeIndex.get(parts.slice(0, n).join("."));
      if (t !== undefined) found = t;
    }
  }
  cache.set(capture, found);
  return found;
}

/** The modifier index for a Monaco language, or -1 if it is not one tree-sitter colours. */
export function modifierOf(languageId: string): number {
  return TOKEN_MODIFIERS.indexOf(languageId);
}

export const legend = () => ({ tokenTypes: [...TOKEN_TYPES], tokenModifiers: [...TOKEN_MODIFIERS] });
