// @vitest-environment node
import { cpSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build } from "vite";
import { it } from "vitest";

it("builds a real shared React component with React installed only in hud3", async () => {
  const repo = mkdtempSync(join(tmpdir(), "hud3-shared-react-"));
  const hud3 = join(repo, "hud3");
  const here = join(import.meta.dirname, "..");

  try {
    mkdirSync(join(hud3, "node_modules"), { recursive: true });
    mkdirSync(join(repo, "hud/src/components"), { recursive: true });
    symlinkSync(join(here, "node_modules"), join(repo, "node_modules"), "dir");
    for (const file of ["boundary.ts", "vite.config.ts"]) {
      cpSync(join(here, file), join(hud3, file));
    }
    cpSync(join(here, "node_modules/react"), join(hud3, "node_modules/react"), { recursive: true });
    const component = "hud/src/components/HoverTitle.tsx";
    cpSync(join(here, "..", component), join(repo, component));
    writeFileSync(join(hud3, "index.html"), `<script type="module">
      import { HoverTitle } from "@hud/components/HoverTitle";
      globalThis.component = HoverTitle;
    </script>`);

    await build({
      configFile: join(hud3, "vite.config.ts"),
      root: hud3,
      logLevel: "silent",
      build: { write: false },
    });
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});
