// @vitest-environment node
// Orchestrator-owned acceptance tests for A5a (implementers: do not edit; stop and say why if one is wrong).
//
// Contract: hud3/boundary.ts exports
//   violations(repoRoot: string): { file: string; line: number; spec: string; rule: string }[]
//       `file` is repo-relative, posix. Reads source, never runs it.
//   sharedAllowed(repoRoot: string, absolutePath: string): boolean
//       is this absolute path a file hud3 may reach (hud3's own, hud3/node_modules,
//       or hud/src/{components,lib,aurora,syntax}/** and hud/src/theme.css)?
//   bundleGuard(repoRoot: string): a Vite/Rollup plugin that fails the build when a bundled
//       module is not sharedAllowed (so a shared component that pulls in hud/src/api.ts is caught).
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";
import { bundleGuard, sharedAllowed, violations } from "../boundary";

let repo: string;
const put = (path: string, text: string) => {
  mkdirSync(dirname(join(repo, path)), { recursive: true });
  writeFileSync(join(repo, path), text);
};
beforeEach(() => {
  repo = mkdtempSync(join(tmpdir(), "hud3-boundary-"));
  put("hud3/src/main.tsx", "export const x = 1;\n");
  put("hud/src/main.tsx", "export const x = 1;\n");
  put("hud/src/lib/a.ts", "export const a = 1;\n");
  put("hud/src/lib/styles.css", ".a {}\n");
  put("hud/src/components/B.tsx", "export const B = 1;\n");
  put("hud/src/aurora/index.ts", "export const c = 1;\n");
  put("hud/src/syntax/index.ts", "export const d = 1;\n");
  put("hud/src/theme.css", ":root {}\n");
  put("hud/src/api.ts", "export const api = 1;\n");
  put("hud/src/types.ts", "export type T = 1;\n");
  put("hud/src/state/store.ts", "export const store = 1;\n");
  put("hud/src/mobile/sw.ts", "export const sw = 1;\n");
  put("hud/src/App.tsx", "export const App = 1;\n");
});
afterEach(() => rmSync(repo, { recursive: true, force: true }));
const only = (text: string, name = "hud3/src/probe.tsx") => {
  put(name, text);
  return violations(repo).filter((v) => v.file === name);
};

describe("hud3 may reach only the shared parts of hud/src", () => {
  it.each([
    `import { a } from "../../hud/src/lib/a";`,
    `import { a } from "../../hud/src/lib/a.ts";`,
    `import { B } from "../../hud/src/components/B";`,
    `import "../../hud/src/aurora";`,
    `import "../../hud/src/aurora/index";`,
    `import { d } from "../../hud/src/syntax/index";`,
    `import "../../hud/src/theme.css";`,
    `import css from "../../hud/src/theme.css?raw";`,
    `import { a } from "@hud/lib/a";`,
    `import "@hud/theme.css";`,
    `export { a } from "@hud/lib/a";`,
    `const m = import("@hud/components/B");`,
    `import type { X } from "@hud/lib/a";`,
    `import React from "react";`,
    `import { q } from "./main";`,
    `const s = "../../hud/src/api"; // a string that is not an import`,
    `// import "../../hud/src/api";`,
    "const t = `import x from '../../hud/src/api'`;",
  ])("allows %s", (text) => expect(only(text + "\n")).toEqual([]));

  it.each([
    `import { api } from "../../hud/src/api";`,
    `import { api } from "../../hud/src/api.ts";`,
    `import type { T } from "../../hud/src/types";`,
    `import { store } from "../../hud/src/state/store";`,
    `import "../../hud/src/mobile/sw";`,
    `import { App } from "../../hud/src/App";`,
    `import "../../hud/src/main";`,
    `import { api } from "@hud/api";`,
    `import { store } from "@hud/state/store";`,
    `import { x } from "@hud/lib/../api";`,
    `import { x } from "../../hud/src/lib/../state/store";`,
    `export * from "@hud/api";`,
    `export { api } from "../../hud/src/api";`,
    `const m = import("@hud/api");`,
    `const m = await import(/* webpackChunkName: "x" */ "../../hud/src/api");`,
    "const m = import(`@hud/api`);",
    `const m = import("@hud/" + name);`,
    "const m = import(`@hud/${name}`);",
    `type X = import("../../hud/src/types").T;`,
    `const r = require("../../hud/src/api");`,
    `import api = require("../../hud/src/api");`,
    `const w = new Worker(new URL("../../hud/src/mobile/sw.ts", import.meta.url));`,
    `const u = new URL("../../hud/src/api.ts", import.meta.url);`,
    `const g = import.meta.glob("../../hud/src/**/*.ts");`,
    `const g = import.meta.glob(["./x.ts", "@hud/state/*.ts"]);`,
    `/// <reference path="../../hud/src/api.ts" />`,
    `import x from "../../hud/vite.config";`,
    `import x from "../../hud/public/x.json";`,
    `import x from "../../jarvis/x.json";`,
    `import x from "/etc/x.json";`,
    `vi.mock("../../hud/src/api");`,
  ])("refuses %s", (text) => {
    const found = only(text + "\n");
    expect(found).toHaveLength(1);
    expect(found[0]!.line).toBe(1);
  });

  it("reports the line and the specifier", () => {
    const found = only(`import React from "react";\n\nimport {\n  api,\n} from "@hud/api";\n`);
    expect(found).toMatchObject([{ file: "hud3/src/probe.tsx", spec: "@hud/api" }]);
    expect(found[0]!.line).toBeGreaterThanOrEqual(3);
    expect(found[0]!.line).toBeLessThanOrEqual(5);
  });

  it("reads stylesheets", () => {
    expect(only(`@import "../../hud/src/theme.css";\n`, "hud3/src/ok.css")).toEqual([]);
    expect(only(`@import url("../../hud/src/mobile/m.css");\n`, "hud3/src/bad.css")).toHaveLength(1);
    expect(only(`@import "@hud/state/x.css";\n`, "hud3/src/bad2.css")).toHaveLength(1);
    expect(only(`.a { background: url(../../hud/public/x.png); }\n`, "hud3/src/bad3.css")).toHaveLength(1);
    expect(only(`/* @import "@hud/api.css"; */\n`, "hud3/src/ok2.css")).toEqual([]);
  });

  it.each(["hud3/vite.config.ts", "hud3/src/deep/er/x.ts", "hud3/tests/x.test.ts", "hud3/src/x.mts", "hud3/src/x.jsx", "hud3/src/x.js"])(
    "reads %s", (name) => {
      const up = "../".repeat(name.split("/").length - 1);
      expect(only(`import "${up}hud/src/api";\n`, name)).toHaveLength(1);
    });

  it("reads scripts and links in index.html", () => {
    expect(only(`<script type="module" src="/src/main.tsx"></script>\n`, "hud3/index.html")).toEqual([]);
    expect(only(`<script type="module" src="../hud/src/main.tsx"></script>\n`, "hud3/index.html")).toHaveLength(1);
  });

  it("skips installed packages and build output", () => {
    put("hud3/node_modules/p/index.js", `import "../../../hud/src/api";\n`);
    put("hud3/dist/assets/x.js", `import "../../../hud/src/api";\n`);
    expect(violations(repo)).toEqual([]);
  });

  it("does not let a link inside hud3 launder a v2 file", () => {
    symlinkSync(join(repo, "hud/src/api.ts"), join(repo, "hud3/src/mine.ts"));
    expect(violations(repo).length).toBeGreaterThan(0);
  });

  it("a file it cannot parse is a violation, not a pass", () => {
    expect(only(`import { from "@hud/api`).length).toBeGreaterThan(0);
  });
});

describe("hud never imports hud3", () => {
  it.each([
    `import { x } from "../../hud3/src/main";`,
    `const m = import("../../hud3/src/main");`,
    `export * from "../../hud3/src/main";`,
    `const u = new URL("../../hud3/src/main.tsx", import.meta.url);`,
  ])("refuses %s", (text) => expect(only(text + "\n", "hud/src/probe.ts")).toHaveLength(1));
  it("leaves hud's own imports alone", () => {
    expect(only(`import { api } from "./api";\nimport "react";\n`, "hud/src/probe.ts")).toEqual([]);
  });
});

describe("the bundle", () => {
  it("knows which files are shared", () => {
    for (const ok of ["hud3/src/main.tsx", "hud3/node_modules/react/index.js", "hud/src/lib/a.ts",
                      "hud/src/components/B.tsx", "hud/src/aurora/index.ts", "hud/src/syntax/index.ts", "hud/src/theme.css"])
      expect([ok, sharedAllowed(repo, join(repo, ok))]).toEqual([ok, true]);
    for (const bad of ["hud/src/api.ts", "hud/src/state/store.ts", "hud/src/mobile/sw.ts", "hud/src/App.tsx", "hud/src/types.ts",
                       "hud/node_modules/react/index.js", "hud/src/lib2/a.ts", "hud/src/theme.css.ts", "hud/vite.config.ts",
                       "jarvis/x.js", "hud3x/a.ts", "hud/src/lib/../api.ts"])
      expect([bad, sharedAllowed(repo, join(repo, "/") + bad)]).toEqual([bad, false]);
  });

  const bundle = (entry: string) => {
    put("hud3/src/entry.ts", entry);
    return build({
      configFile: false, root: join(repo, "hud3"), logLevel: "silent", plugins: [bundleGuard(repo)],
      build: { write: false, rollupOptions: { input: join(repo, "hud3/src/entry.ts") } },
    });
  };
  it("builds a shared component that stays inside the shared parts", async () => {
    put("hud/src/lib/deep.ts", `import { a } from "./a"; export const deep = a;\n`);
    await bundle(`import { deep } from "../../hud/src/lib/deep"; console.log(deep);\n`);
  });
  it("fails when a shared component pulls in the v2 API", async () => {
    put("hud/src/lib/deep.ts", `import { api } from "../api"; export const deep = api;\n`);
    await expect(bundle(`import { deep } from "../../hud/src/lib/deep"; console.log(deep);\n`)).rejects.toThrow(/api\.ts/);
  });
  it("accepts a type-only use of the v2 API, which is erased", async () => {
    put("hud/src/lib/deep.ts", `import type { api } from "../api"; export const deep = 1 as unknown as typeof api;\n`);
    await bundle(`import { deep } from "../../hud/src/lib/deep"; console.log(deep);\n`);
  });
});

it("the real repository has no violation, and its Vite config runs the guard", async () => {
  const real = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
  expect(violations(real)).toEqual([]);
  const config = (await import("../vite.config")).default as unknown as { plugins: unknown[] };
  const names = (config.plugins.flat(9) as { name?: string }[]).map((p) => p?.name);
  expect(names).toContain(bundleGuard(real).name);
});
