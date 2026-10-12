// What scripts/vendor-queries.mjs fetches, pinned (ED-5). Changing a pin means
// running the script again and committing what it writes; the tests compare
// the vendored files with these.

/**
 * nvim-treesitter's queries: the `main` branch at a commit at least two weeks
 * old when it was pinned (2026-10-11; this one is 2026-09-19).
 */
export const NVIM = {
  repo: "nvim-treesitter/nvim-treesitter",
  commit: "f603a2f4da48728f80257fb5fbb90145fd1dc173",
  date: "2026-09-19",
} as const;

/**
 * The Markdown grammar (block and inline), whose npm package (0.3.2, 2024)
 * ships no `.wasm`: the v0.5.3 release's own assets (2026-02-26), by SHA-256.
 */
export const MARKDOWN = {
  repo: "tree-sitter-grammars/tree-sitter-markdown",
  tag: "v0.5.3",
  files: {
    "tree-sitter-markdown.wasm": "dd9fc12ac2804d7c7da787e4774125b32e4fb3c244e5e7031a77cb7dd8036020",
    "tree-sitter-markdown_inline.wasm": "d47e5c43683c39b645ced8720ab90c3b3f467e715d3426b29ef3f69984d88c15",
  },
} as const;
