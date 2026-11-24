// Accident guard for the v2/v3 frontend split. This is not an adversarial boundary.
import { existsSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import ts from "typescript";
import type { Plugin } from "vite";

const sharedDirs = new Set(["components", "lib", "aurora", "syntax"]);
const mockMethods = new Set(["mock", "doMock", "unmock", "doUnmock", "importActual", "importMock"]);
const codeFile = /\.(?:[cm]?[jt]s|[jt]sx)$/i;

interface Violation {
  file: string;
  line: number;
  spec: string;
  rule: string;
}

function repoPath(root: string, path: string): string {
  return relative(root, path).split(sep).join("/");
}

function allowed(path: string): boolean {
  if (path.startsWith("hud3/") || path === "hud/src/theme.css") return true;
  const [hud, src, area] = path.split("/");
  return hud === "hud" && src === "src" && sharedDirs.has(area);
}

// Resolve symlinked ancestors even when an import omits the extension or names a glob.
function canonical(path: string): string {
  if (existsSync(path)) return realpathSync(path);
  const parent = dirname(path);
  return parent === path ? path : resolve(canonical(parent), relative(parent, path));
}

export function sharedAllowed(repoRoot: string, absolutePath: string): boolean {
  return isAbsolute(absolutePath) && allowed(repoPath(repoRoot, absolutePath))
    && allowed(repoPath(realpathSync(repoRoot), canonical(absolutePath)));
}

function sourceFiles(directory: string): string[] {
  if (!existsSync(directory)) return [];
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      return ["node_modules", "dist"].includes(entry.name) ? [] : sourceFiles(path);
    }
    return [path];
  });
}

type Report = (spec: string, offset: number, resource?: boolean) => void;

function isImportMetaMember(node: ts.Node | undefined, name: string): boolean {
  return !!node && ts.isPropertyAccessExpression(node) && node.name.text === name
    && ts.isMetaProperty(node.expression) && node.expression.keywordToken === ts.SyntaxKind.ImportKeyword;
}

function isRequireOrMock(callee: ts.Expression): boolean {
  if (ts.isIdentifier(callee)) return callee.text === "require";
  if (!ts.isPropertyAccessExpression(callee) || !ts.isIdentifier(callee.expression)) return false;
  const receiver = callee.expression.text;
  const member = callee.name.text;
  return (receiver === "require" && member === "resolve")
    || ((receiver === "vi" || receiver === "jest") && mockMethods.has(member));
}

function inspectCode(file: string, content: string, report: Report, problem: Report) {
  const source = ts.createSourceFile(file, content, ts.ScriptTarget.Latest, true);
  // createSourceFile records syntactic diagnostics alongside its recovery AST.
  const { parseDiagnostics } = source as ts.SourceFile & { parseDiagnostics: readonly ts.Diagnostic[] };
  for (const diagnostic of parseDiagnostics) problem("<syntax error>", diagnostic.start ?? 0);
  for (const reference of source.referencedFiles) report(reference.fileName, reference.pos);

  const check = (node: ts.Node | undefined, owner: ts.Node) => {
    if (node && ts.isStringLiteralLike(node)) report(node.text, node.getStart(source));
    else problem("<computed specifier>", (node ?? owner).getStart(source));
  };

  const visit = (node: ts.Node) => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier) check(node.moduleSpecifier, node);
    } else if (ts.isExternalModuleReference(node)) {
      check(node.expression, node);
    } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) {
      check(node.argument.literal, node);
    } else if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (callee.kind === ts.SyntaxKind.ImportKeyword || isRequireOrMock(callee)) {
        check(node.arguments[0], node);
      } else if (isImportMetaMember(callee, "glob")) {
        const glob = node.arguments[0];
        for (const pattern of glob && ts.isArrayLiteralExpression(glob) ? glob.elements : [glob]) {
          check(pattern, node);
        }
      }
    } else if (ts.isNewExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "URL"
      && isImportMetaMember(node.arguments?.[1], "url")) {
      check(node.arguments?.[0], node);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
}

// CSS and HTML resource syntax, separate from the TypeScript import walker.
function inspectResources(file: string, content: string, report: Report) {
  const blank = (comment: string) => comment.replace(/[^\n]/g, " ");
  if (file.endsWith(".css")) {
    const css = content.replace(/\/\*[\s\S]*?\*\//g, blank);
    for (const match of css.matchAll(/@import\s+(?:"([^"]+)"|'([^']+)')|\burl\(\s*(?:"([^"]+)"|'([^']+)'|([^\s)]+))\s*\)/gi)) {
      report(match.slice(1).find((part) => part !== undefined)!, match.index, true);
    }
  } else if (file.endsWith(".html")) {
    const html = content.replace(/<!--[\s\S]*?-->/g, blank);
    for (const match of html.matchAll(/<(?:script|link)\b[^>]*?\s(?:src|href)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))/gi)) {
      const spec = match[1] ?? match[2] ?? match[3];
      report(spec, match.index, true);
    }
  }
}

function targetPath(root: string, file: string, spec: string, resourceRoot?: string): string | null {
  const clean = spec.split(/[?#]/, 1)[0].replace(/^!/, "");
  if (clean === "@hud" || clean.startsWith("@hud/")) return resolve(root, "hud/src", clean.slice(5));
  if (resourceRoot && clean.startsWith("//")) return null;
  if (resourceRoot && clean.startsWith("/@fs/")) return resolve("/", clean.slice(5));
  if (resourceRoot && clean.startsWith("/")) return resolve(resourceRoot, `.${clean}`);
  if (isAbsolute(clean)) return resolve(clean);
  if (clean.startsWith(".")) return resolve(dirname(file), clean);
  return null; // A bare package specifier will be checked after Vite resolves it.
}

export function violations(repoRoot: string): Violation[] {
  const result: Violation[] = [];
  const physicalRoot = realpathSync(repoRoot);
  const v3Root = resolve(repoRoot, "hud3");
  for (const tree of [v3Root, resolve(repoRoot, "hud")]) {
    const v3 = tree === v3Root;
    for (const file of sourceFiles(tree)) {
      const name = repoPath(repoRoot, file);
      // Shared source enters through an explicit import from hud3.
      if (v3 && !repoPath(physicalRoot, canonical(file)).startsWith("hud3/")) {
        result.push({ file: name, line: 1, spec: name, rule: "Source resolves outside hud3" });
        continue;
      }
      if (!codeFile.test(file) && !/\.(?:html|css)$/.test(file)) continue;
      const content = readFileSync(file, "utf8");
      const add = (spec: string, offset: number, rule: string) => {
        const line = content.slice(0, offset).split("\n").length;
        result.push({ file: name, line, spec, rule });
      };
      const report: Report = (spec, offset, resource) => {
        const target = targetPath(repoRoot, file, spec, resource ? tree : undefined);
        if (!target) return;
        if (v3 && !sharedAllowed(repoRoot, target)) {
          add(spec, offset, "hud3 may import only shared HUD files");
        } else if (!v3 && (repoPath(repoRoot, target).startsWith("hud3/")
          || repoPath(physicalRoot, canonical(target)).startsWith("hud3/"))) {
          add(spec, offset, "hud may not import hud3");
        }
      };
      if (codeFile.test(file)) {
        inspectCode(file, content, report, (spec, offset) => {
          if (v3) add(spec, offset, "Cannot check import");
        });
      } else {
        inspectResources(file, content, report);
      }
    }
  }
  return result;
}

export function bundleGuard(repoRoot: string): Plugin {
  const disallowed = (id: string) => {
    const file = id.split(/[?#]/, 1)[0];
    return isAbsolute(file) && !sharedAllowed(repoRoot, file) ? repoPath(repoRoot, file) : null;
  };

  return {
    name: "hud3-import-boundary",
    apply: "build",
    enforce: "pre",
    load(id) {
      const file = disallowed(id);
      if (file !== null) this.error(`hud3 bundle reached outside the shared HUD: ${file}`);
      return null;
    },
    buildEnd(error) {
      if (error) return;
      // Vite inlines stylesheet imports and reports them as watched files.
      for (const id of this.getWatchFiles()) {
        if (!id.endsWith(".css")) continue;
        const file = disallowed(id);
        if (file !== null) this.error(`hud3 bundle reached outside the shared HUD: ${file}`);
      }
    },
  };
}
