; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/markdown_inline/injections.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

((html_tag) @injection.content
  (#set! injection.language "html")
  (#set! injection.combined))

((latex_block) @injection.content
  (#set! injection.language "latex")
  (#set! injection.include-children))
