// Which icon a file in the File pane's tree wears (ED-2 follow-up, the
// owner's VS Code screenshot): a small, curated set drawn in
// components/FileIcon.tsx, chosen here by name.
//
// The rule is VS Code's: a **whole file name** first (`package.json`,
// `Makefile`, `.gitignore`, `Dockerfile`), then the **longest extension**
// (`.d.ts` before `.ts`), case-insensitive, else the generic document. An
// env file — the daemon's `policy: "env"`, or the `.env*` name rule
// (`secrets.is_env_credential_name`: `.env` or `.env.<anything>`, never
// `.env.example`) — wears the lock, as its editor badge does.
//
// **Colours are theme tokens, never a hex** (theme.css is the one place a
// colour is written). They are the Graphite scheme's quiet hues only: amber
// and red mean "pending" and "error" in this HUD and nothing else may wear
// them, so VS Code's yellow `{}` and orange HTML are not copied
// (fileIcons.test.ts holds every colour to that, from theme.css's own text).

/** The colours an icon may be drawn in: the editor's syntax hues and the quiet text tokens. */
export const ICON_TOKENS = [
  "--syn-fn", "--syn-string", "--syn-const", "--syn-type", "--text-2", "--text-muted",
] as const;
export type IconToken = (typeof ICON_TOKENS)[number];

export type IconId =
  | "c" | "cpp" | "h" | "python" | "js" | "ts" | "react" | "json" | "markdown" | "html" | "css"
  | "config" | "shell" | "sql" | "text" | "image" | "lock" | "env" | "git" | "make" | "docker"
  | "package" | "file";

/** What each icon is, for its tooltip-free name in tests, and the token it is drawn in. */
export const ICONS: Record<IconId, { label: string; color: IconToken }> = {
  c: { label: "C", color: "--syn-type" },
  cpp: { label: "C++", color: "--syn-fn" },
  h: { label: "C/C++ header", color: "--syn-const" },
  python: { label: "Python", color: "--syn-fn" },
  js: { label: "JavaScript", color: "--syn-string" },
  ts: { label: "TypeScript", color: "--syn-fn" },
  react: { label: "JSX/TSX", color: "--syn-fn" },
  json: { label: "JSON", color: "--syn-const" },
  markdown: { label: "Markdown", color: "--syn-fn" },
  html: { label: "HTML", color: "--syn-string" },
  css: { label: "CSS", color: "--syn-const" },
  config: { label: "configuration", color: "--text-2" },
  shell: { label: "shell", color: "--syn-string" },
  sql: { label: "SQL", color: "--syn-type" },
  text: { label: "text", color: "--text-2" },
  image: { label: "image", color: "--syn-const" },
  lock: { label: "lock file", color: "--text-muted" },
  env: { label: "env file", color: "--text-2" },
  git: { label: "git", color: "--syn-type" },
  make: { label: "build", color: "--text-2" },
  docker: { label: "Docker", color: "--syn-fn" },
  package: { label: "package manifest", color: "--syn-string" },
  file: { label: "file", color: "--text-muted" },
};

/** Whole file names, lower-cased: checked before any extension. */
const NAMES: Record<string, IconId> = {
  "package.json": "package",
  "pyproject.toml": "package",
  "cargo.toml": "package",
  "go.mod": "package",
  "requirements.txt": "package",
  "package-lock.json": "lock",
  "yarn.lock": "lock",
  "pnpm-lock.yaml": "lock",
  "bun.lockb": "lock",
  "go.sum": "lock",
  makefile: "make",
  gnumakefile: "make",
  "cmakelists.txt": "make",
  dockerfile: "docker",
  containerfile: "docker",
  ".dockerignore": "docker",
  "docker-compose.yml": "docker",
  "docker-compose.yaml": "docker",
  "compose.yml": "docker",
  "compose.yaml": "docker",
  ".gitignore": "git",
  ".gitattributes": "git",
  ".gitmodules": "git",
  ".gitkeep": "git",
  ".git-blame-ignore-revs": "git",
  ".bashrc": "shell",
  ".bash_profile": "shell",
  ".zshrc": "shell",
  ".profile": "shell",
  ".envrc": "shell",
  ".editorconfig": "config",
  license: "text",
  licence: "text",
  copying: "text",
  readme: "text",
  notice: "text",
};

/** Extensions, lower-cased and without the dot; a longer one (`d.ts`) wins over its tail. */
const EXTS: Record<string, IconId> = {
  c: "c",
  cpp: "cpp", cc: "cpp", cxx: "cpp", "c++": "cpp", ino: "cpp",
  h: "h", hpp: "h", hh: "h", hxx: "h", "h++": "h",
  py: "python", pyi: "python", pyw: "python", ipynb: "python",
  js: "js", mjs: "js", cjs: "js",
  ts: "ts", mts: "ts", cts: "ts", "d.ts": "ts",
  jsx: "react", tsx: "react",
  json: "json", jsonc: "json", json5: "json", jsonl: "json",
  md: "markdown", markdown: "markdown", mdx: "markdown",
  html: "html", htm: "html", xml: "html", xhtml: "html",
  css: "css", scss: "css", sass: "css", less: "css",
  toml: "config", yaml: "config", yml: "config", ini: "config", cfg: "config", conf: "config",
  properties: "config",
  sh: "shell", bash: "shell", zsh: "shell", fish: "shell", ps1: "shell", bat: "shell", cmd: "shell",
  sql: "sql", sqlite: "sql", sqlite3: "sql", db: "sql",
  txt: "text", text: "text", log: "text", rst: "text", csv: "text", tsv: "text",
  png: "image", jpg: "image", jpeg: "image", gif: "image", svg: "image", webp: "image", ico: "image",
  bmp: "image", avif: "image",
  lock: "lock",
  mk: "make", cmake: "make",
  dockerfile: "docker",
};

/** `secrets.is_env_credential_name`'s rule, as the tree sees a name: `.env`, `.env.*`, never `.env.example`. */
export function isEnvName(name: string): boolean {
  // As written and with trailing dots and spaces trimmed (Windows drops them).
  for (const c of new Set([name, name.replace(/[ .]+$/, "")])) {
    const f = c.toLowerCase();
    if (/^\.env(\..*)?$/s.test(f) && !f.endsWith(".example")) return true;
  }
  return false;
}

/**
 * The icon a file named `name` wears. `policy` is the tree entry's (the
 * daemon's word): `env` is the lock whatever the name.
 */
export function iconFor(name: string, policy?: string | null): IconId {
  const base = name.slice(name.lastIndexOf("/") + 1);
  const lower = base.toLowerCase();
  if (policy === "env" || isEnvName(base)) return "env";
  const whole = NAMES[lower];
  if (whole) return whole;
  // `.env.example` and friends: settings, not secrets.
  if (lower.startsWith(".env.")) return "config";
  // Every extension the name has, longest first: `x.d.ts` asks `d.ts`, then `ts`.
  const parts = lower.split(".");
  for (let i = 1; i < parts.length; i++) {
    if (i === 1 && parts[0] === "") continue; // a dotfile's name is not an extension
    const ext = parts.slice(i).join(".");
    const hit = EXTS[ext];
    if (hit) return hit;
  }
  return "file";
}
