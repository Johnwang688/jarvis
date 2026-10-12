; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/markdown/folds.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

([
  (fenced_code_block)
  (indented_code_block)
  (list_item
    (list))
  (section)
] @fold
  (#trim! @fold))

(section
  (list) @fold
  (#trim! @fold))
