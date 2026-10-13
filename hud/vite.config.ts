/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The daemon serves hud/dist from `/` on FACE_PORT 8402 and its assets from
// `/assets/*` (docs/hud-api.md, Static), so the base stays absolute.
export default defineConfig({
  base: "/",
  plugins: [react()],
  resolve: {
    alias: [
      // monaco-vim (ED-8, decisions E-11) imports Monaco by its pre-0.56 deep
      // paths (`monaco-editor/esm/vs/editor/editor.api`), which 0.57's export
      // map spells `monaco-editor/editor/editor.api`. The same file either way,
      // so vim drives the one Monaco the HUD loads, never a second copy.
      { find: /^monaco-editor\/esm\/vs\/(.*)$/, replacement: "monaco-editor/$1" },
      // Its ES module build, not the UMD one its `browser` export names (a
      // CommonJS wrapper around the same code).
      {
        find: /^monaco-vim$/,
        replacement: decodeURIComponent(new URL("./node_modules/monaco-vim/dist/index.mjs", import.meta.url).pathname),
      },
    ],
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Monaco is large and loaded on demand by the File and Diff tabs; the
    // warning is not a defect to chase here.
    chunkSizeWarningLimit: 6144,
  },
  // Workers are ES modules (`new Worker(url, {type: "module"})`): the syntax
  // worker's web-tree-sitter has code-split imports (its Node-only branches),
  // which an IIFE worker cannot hold (ED-5). Monaco's editor.worker runs the
  // same either way.
  worker: { format: "es" },
  test: {
    globals: true,
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
    // Vitest replaces CSS with an empty string unless told otherwise, `?raw`
    // included. editorTheme.test.ts reads theme.css's own text, to recompute
    // the editor scheme's contrast and hue from the tokens there (ED-3).
    css: { include: [/theme\.css/] },
  },
});
