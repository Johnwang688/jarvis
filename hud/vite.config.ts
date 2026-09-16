/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The daemon serves hud/dist from `/` on FACE_PORT 8402 and its assets from
// `/assets/*` (docs/hud-api.md, Static), so the base stays absolute.
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Monaco is large and loaded on demand by the File and Diff tabs; the
    // warning is not a defect to chase here.
    chunkSizeWarningLimit: 6144,
  },
  test: {
    globals: true,
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
