; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/css/folds.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

[
  ; top-level block statements from https://github.com/tree-sitter/tree-sitter-css/blob/master/grammar.js
  ; note: (block) is not used due to unideal behavior when node before block node spans multiple lines
  (rule_set)
  (at_rule)
  (supports_statement)
  (media_statement)
  (keyframe_block)
  (import_statement)+
] @fold
