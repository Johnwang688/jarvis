// Node's file system and hashing, as the syntax tests use them (ED-5). Those
// tests run under vitest's `node` environment, with the same web-tree-sitter
// and grammars the window loads; the HUD itself never imports these modules.
// A shim rather than @types/node, which would change the window's own global
// types (a timer's return type among them) for every file.

declare module "node:fs" {
  export function readFileSync(path: string): Uint8Array;
  export function readFileSync(path: string, encoding: "utf8"): string;
  export function existsSync(path: string): boolean;
  export function readdirSync(path: string): string[];
}

declare module "node:crypto" {
  export function createHash(algorithm: "sha256"): {
    update(data: Uint8Array | string): { digest(encoding: "hex"): string };
  };
}
