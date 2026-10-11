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

// Monaco's context view class, patched for the HUD's CSS zoom in
// lib/monacoBundle.ts. Internal; the package ships no declaration for it.
declare module "monaco-editor/base/browser/ui/contextview/contextview" {
  export const ContextView: { prototype: { doLayout(): void } };
}
