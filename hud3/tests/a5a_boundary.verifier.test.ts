// @vitest-environment node
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { build } from "vite";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { bundleGuard, violations } from "../boundary";

let repo: string;

beforeEach(() => {
  repo = mkdtempSync(join(tmpdir(), "a5a-boundary-"));
});

afterEach(() => rmSync(repo, { recursive: true, force: true }));

function put(path: string, content: string) {
  const file = join(repo, path);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, content);
}

function bundle(assetsInlineLimit?: number) {
  return build({
    configFile: false,
    root: join(repo, "hud3"),
    logLevel: "silent",
    plugins: [bundleGuard(repo)],
    build: { write: false, assetsInlineLimit },
  });
}

it("accepts a Vite root-relative import of a hud3 module", async () => {
  put("hud3/index.html", '<script type="module" src="/src/main.ts"></script>');
  put("hud3/src/main.ts", 'import { label } from "/src/label"; document.title = label;');
  put("hud3/src/label.ts", 'export const label = "Agent";');

  await bundle();

  expect(violations(repo)).toEqual([]);
});

it("reports a v2 import of hud3 from a module script in HTML", async () => {
  put("hud3/src/label.ts", 'export const label = "Agent";');
  put("hud/index.html", `<script type="module">
    import { label } from "../hud3/src/label";
    document.title = label;
  </script>`);
  await build({
    configFile: false,
    root: join(repo, "hud"),
    logLevel: "silent",
    build: { write: false },
  });

  expect(violations(repo)).toContainEqual(expect.objectContaining({
    file: "hud/index.html",
    spec: "../hud3/src/label",
  }));
});

describe.each([0, 4096])("with a Vite asset inline limit of %d bytes", (limit) => {
  it.each([
    ["a CSS url()", 'import "./card.css";'],
    ["a JavaScript asset URL", 'document.body.style.backgroundImage = `url(${new URL("../media/card.svg", import.meta.url)})`;'],
  ])("rejects a non-shared asset reached through %s in shared code", async (_kind, code) => {
    put("hud3/index.html", '<div class="card"></div><script type="module" src="/src/main.ts"></script>');
    put("hud3/src/main.ts", 'import "../../hud/src/components/card";');
    put("hud/src/components/card.ts", code);
    put("hud/src/components/card.css", '.card { background-image: url("../media/card.svg"); }');
    put("hud/src/media/card.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0h1v1z"/></svg>');

    await expect(bundle(limit).then((result) => {
      if (!("output" in result)) throw new Error("Expected one Vite output");
      return result.output.map((file) => {
        const content = file.type === "chunk" ? file.code : String(file.source);
        return content.includes("data:image/svg+xml") ? `${file.fileName} (inlined SVG)` : file.fileName;
      });
    })).rejects.toThrow(/outside the shared HUD: hud\/src\/media\/card\.svg/);
  });
});

it("builds the shared xterm loader with its dependencies installed only in hud3", async () => {
  // Config dependencies resolve from the fixture's parent package directory.
  symlinkSync(join(import.meta.dirname, "../node_modules"), join(repo, "node_modules"), "dir");
  put("hud3/vite.config.ts", readFileSync(new URL("../vite.config.ts", import.meta.url), "utf8"));
  put("hud3/boundary.ts", readFileSync(new URL("../boundary.ts", import.meta.url), "utf8"));
  put("hud3/index.html", '<script type="module" src="/src/main.ts"></script>');
  put("hud3/src/main.ts", 'import { loadXterm } from "@hud/lib/xterm"; void loadXterm();');
  put("hud/src/lib/xterm.ts", readFileSync(new URL("../../hud/src/lib/xterm.ts", import.meta.url), "utf8"));
  for (const [name, symbol] of [
    ["xterm", "Terminal"], ["addon-fit", "FitAddon"], ["addon-web-links", "WebLinksAddon"],
  ]) {
    const dir = `hud3/node_modules/@xterm/${name}`;
    put(`${dir}/package.json`, '{"type":"module","main":"index.js"}');
    put(`${dir}/index.js`, `export class ${symbol} {}`);
  }
  put("hud3/node_modules/@xterm/xterm/css/xterm.css", ".xterm { display: block; }");

  await build({
    configFile: join(repo, "hud3/vite.config.ts"),
    root: join(repo, "hud3"),
    logLevel: "silent",
    build: { write: false },
  });
});
