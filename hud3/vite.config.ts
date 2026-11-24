import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";
import { bundleGuard } from "./boundary";

const root = import.meta.dirname;
const repoRoot = resolve(root, "..");
const hudRoot = resolve(repoRoot, "hud/src");

export default defineConfig({
  root,
  base: "/",
  plugins: [bundleGuard(repoRoot), react()],
  resolve: {
    alias: { "@hud": hudRoot },
    // Resolve bare React imports in shared files from hud3, including JSX runtimes.
    dedupe: ["react", "react-dom"],
  },
  server: {
    fs: {
      allow: [root, ...["components", "lib", "aurora", "syntax", "theme.css"].map((part) => resolve(hudRoot, part))],
    },
  },
  build: {
    outDir: "dist",
    manifest: "build-manifest.json",
    rollupOptions: {
      output: {
        sanitizeFileName: (name) => name.replace(/[^A-Za-z0-9._-]/g, "_"),
      },
    },
  },
  worker: { plugins: () => [bundleGuard(repoRoot)] },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
