import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import css from "../theme.css?raw";
import iconSource from "../components/FileIcon.tsx?raw";
import ruleSource from "./fileIcons.ts?raw";
import markSource from "../components/DirtyMark.tsx?raw";
import { FileIcon } from "../components/FileIcon";
import { hueSat, isWarm, parseToken } from "./editorTheme";
import { ICONS, ICON_TOKENS, iconFor, isEnvName, type IconId } from "./fileIcons";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

describe("which icon a file wears", () => {
  it("the owner's list, by extension", () => {
    const cases: [string, IconId][] = [
      ["drive.c", "c"], ["drive.cpp", "cpp"], ["drive.cc", "cpp"], ["drive.cxx", "cpp"],
      ["drive.h", "h"], ["drive.hpp", "h"],
      ["calc.py", "python"], ["app.js", "js"], ["app.mjs", "js"], ["app.ts", "ts"], ["types.d.ts", "ts"],
      ["App.tsx", "react"], ["App.jsx", "react"],
      ["config.json", "json"], ["README.md", "markdown"], ["index.html", "html"], ["theme.css", "css"],
      ["pyproject.cfg", "config"], ["Cargo.lock", "lock"], ["settings.toml", "config"], ["ci.yaml", "config"],
      ["ci.yml", "config"],
      ["build.sh", "shell"], ["q.sql", "sql"], ["notes.txt", "text"], ["logo.png", "image"], ["icon.svg", "image"],
      ["data.bin", "file"], ["noextension", "file"], ["weird.", "file"],
    ];
    for (const [name, id] of cases) expect(iconFor(name), name).toBe(id);
  });

  it("a whole name first, then the extension", () => {
    expect(iconFor("package.json")).toBe("package"); // not json
    expect(iconFor("package-lock.json")).toBe("lock");
    expect(iconFor("pnpm-lock.yaml")).toBe("lock");
    expect(iconFor("Makefile")).toBe("make");
    expect(iconFor("Dockerfile")).toBe("docker");
    expect(iconFor(".gitignore")).toBe("git");
    expect(iconFor(".gitattributes")).toBe("git");
    expect(iconFor("CMakeLists.txt")).toBe("make"); // not text
  });

  it("case-insensitive, by the last path part", () => {
    expect(iconFor("DRIVE.CPP")).toBe("cpp");
    expect(iconFor("MAKEFILE")).toBe("make");
    expect(iconFor("Config.JSON")).toBe("json");
    expect(iconFor("src/robot/drive.H")).toBe("h");
  });

  it(".env files wear the lock, as their editor badge does; .env.example is settings", () => {
    for (const n of [".env", ".env.local", ".env.production", ".ENV", ".env. "]) {
      expect(isEnvName(n), n).toBe(true);
      expect(iconFor(n), n).toBe("env");
    }
    expect(isEnvName(".env.example")).toBe(false);
    expect(iconFor(".env.example")).toBe("config");
    expect(iconFor(".envrc")).toBe("shell");
    // The daemon's word wins whatever the name says.
    expect(iconFor("secrets.json", "env")).toBe("env");
  });

  it("a dotfile's name is not its extension", () => {
    expect(iconFor(".json")).toBe("file");
    expect(iconFor(".bashrc")).toBe("shell");
  });
});

/** The declarations on theme.css's `:root` (editorTheme.test.ts's reader). */
function rootDeclarations(): Map<string, string> {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const block = /:root\s*\{([\s\S]*?)\n\}/.exec(text);
  if (!block) throw new Error("theme.css has no :root block");
  return new Map([...block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

const code = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");

describe("the colour rule: theme tokens only, never amber or red", () => {
  const decl = rootDeclarations();

  it("every icon is drawn in a token the theme declares as an opaque colour outside the warm band", () => {
    for (const [id, icon] of Object.entries(ICONS)) {
      expect((ICON_TOKENS as readonly string[]).includes(icon.color), id).toBe(true);
    }
    for (const t of ICON_TOKENS) {
      const hex = parseToken(decl.get(t) ?? "");
      expect(hex, t).toMatch(/^#[0-9a-f]{6}$/);
      const { h, s } = hueSat(hex!);
      expect(isWarm(hex!), `${t} ${hex}: ${h.toFixed(0)}° s ${s.toFixed(2)}`).toBe(false);
    }
  });

  it("the warm test bites: amber, red and VS Code's JSON yellow are warm", () => {
    for (const t of ["--amber", "--red"]) expect(isWarm(parseToken(decl.get(t)!)!), t).toBe(true);
    expect(isWarm("#cbcb41")).toBe(true); // VS Code's `{}` yellow
    expect(isWarm("#e37933")).toBe(true); // its HTML orange
  });

  it("no component or rule writes a colour of its own: no hex literal, no colour word", () => {
    for (const [name, src] of [["FileIcon.tsx", iconSource], ["fileIcons.ts", ruleSource], ["DirtyMark.tsx", markSource]]) {
      const c = code(src);
      expect(c.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [], name).toEqual([]);
      expect(c.match(/\b(?:rgba?|hsla?)\(/g) ?? [], name).toEqual([]);
      // SVG paint is currentColor or none: the colour comes from the token.
      for (const m of c.matchAll(/\b(?:fill|stroke)="([^"]*)"/g)) expect(["currentColor", "none"], `${name}: ${m[0]}`).toContain(m[1]);
    }
  });

  it("the unsaved-changes dots are --text-2, in the tree and on a tab", () => {
    const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = (sel: string) => new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}").exec(text)?.[1] ?? "";
    expect(rule(".dirtydot")).toMatch(/background:\s*var\(--text-2\)/);
    expect(rule(".edtab-dot.on::before")).toMatch(/background:\s*var\(--text-2\)/);
  });

  it("every icon renders: an aria-hidden 16px SVG with something in it, coloured by its token", () => {
    const sample: Record<IconId, string> = {
      c: "a.c", cpp: "a.cpp", h: "a.h", python: "a.py", js: "a.js", ts: "a.ts", react: "a.tsx", json: "a.json",
      markdown: "a.md", html: "a.html", css: "a.css", config: "a.toml", shell: "a.sh", sql: "a.sql", text: "a.txt",
      image: "a.png", lock: "a.lock", env: ".env", git: ".gitignore", make: "Makefile", docker: "Dockerfile",
      package: "package.json", file: "a.bin",
    };
    const host = document.createElement("div");
    const root = createRoot(host);
    for (const [id, name] of Object.entries(sample) as [IconId, string][]) {
      act(() => root.render(createElement(FileIcon, { name })));
      const span = host.querySelector(".ficon") as HTMLElement;
      expect(span.dataset.icon, name).toBe(id);
      expect(span.getAttribute("aria-hidden"), id).toBe("true");
      expect(span.style.color, id).toBe(`var(${ICONS[id].color})`);
      const svg = span.querySelector("svg")!;
      expect(svg.getAttribute("width"), id).toBe("16");
      expect(svg.getAttribute("focusable"), id).toBe("false");
      expect(svg.children.length, id).toBeGreaterThan(0);
      expect(svg.querySelector("text"), id).toBeNull(); // a row's text stays its name
      expect(span.querySelector("[tabindex], a, button"), id).toBeNull();
    }
    act(() => root.unmount());
  });
});
