import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";
import { bundleGuard } from "./boundary";

const root = import.meta.dirname;
const repoRoot = resolve(root, "..");
const hudRoot = resolve(repoRoot, "hud/src");
const modules = resolve(root, "node_modules") + "/";
const hud3Importer = resolve(root, "index.html");

function sharedPackages(): Plugin {
  return {
    name: "hud3-shared-packages",
    enforce: "pre",
    async resolveId(id, importer, options) {
      if (!importer?.startsWith(hudRoot + "/") || !/^[\w@][^:]*$/.test(id)) return;
      const result = await this.resolve(id, hud3Importer, { ...options, skipSelf: true });
      if (!result?.id.startsWith(modules)) {
        this.error(`Shared HUD package ${id} must resolve from hud3/node_modules`);
      }
      return result;
    },
  };
}

export default defineConfig({
  root,
  base: "/",
  plugins: [bundleGuard(repoRoot), sharedPackages(), react()],
  resolve: {
    alias: { "@hud": hudRoot },
    // Keep one React instance across the app and its dependencies.
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
  worker: { plugins: () => [bundleGuard(repoRoot), sharedPackages()] },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
