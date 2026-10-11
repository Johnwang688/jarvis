/// <reference types="vite/client" />

// Vite's `?worker` suffix imports are resolved by the bundler, not by tsc, so
// the module shape is declared here rather than left as an error.
declare module "*?worker" {
  const WorkerFactory: new () => Worker;
  export default WorkerFactory;
}

// Monaco's INI Monarch definition, borrowed for TOML (lib/monaco.ts). The
// package ships no declaration for its definition modules.
declare module "monaco-editor/languages/definitions/ini/ini" {
  export const conf: unknown;
  export const language: unknown;
}

// Monaco's C/C++ and Python Monarch definitions, refined for the Graphite
// scheme (lib/monarchRefine.ts).
declare module "monaco-editor/languages/definitions/cpp/cpp" {
  export const conf: unknown;
  export const language: unknown;
}
declare module "monaco-editor/languages/definitions/python/python" {
  export const conf: unknown;
  export const language: unknown;
}

// Monaco's own token-theme matcher and token metadata, which editorTheme.test.ts
// resolves the scheme's rules through (the same code the editor runs).
declare module "monaco-editor/editor/common/languages/supports/tokenization" {
  export const TokenTheme: {
    createFromRawTokenTheme(rules: unknown[], customTokenColors: string[]): {
      _match(token: string): { metadata: number };
      getColorMap(): { toString(): string }[];
    };
  };
}
declare module "monaco-editor/editor/common/encodedTokenAttributes" {
  export const TokenMetadata: {
    getForeground(metadata: number): number;
    getFontStyle(metadata: number): number;
  };
}

// Monaco's Monarch compiler and lexer, which monarchRefine.test.ts tokenizes
// the refined definitions with.
declare module "monaco-editor/editor/standalone/common/monarch/monarchCompile" {
  export function compile(languageId: string, json: unknown): unknown;
}
declare module "monaco-editor/editor/standalone/common/monarch/monarchLexer" {
  export class MonarchTokenizer {
    constructor(languageService: unknown, themeService: unknown, languageId: string, lexer: unknown, config: unknown);
    getInitialState(): unknown;
    tokenize(line: string, hasEOL: boolean, state: unknown): { tokens: { offset: number; type: string }[]; endState: unknown };
  }
}

// Monaco's context view class, patched for the HUD's CSS zoom in
// lib/monacoBundle.ts. Internal; the package ships no declaration for it.
declare module "monaco-editor/base/browser/ui/contextview/contextview" {
  export const ContextView: { prototype: { doLayout(): void } };
}
