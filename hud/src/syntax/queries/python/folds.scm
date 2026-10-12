; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/python/folds.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

[
  (function_definition)
  (class_definition)
  (while_statement)
  (for_statement)
  (if_statement)
  (with_statement)
  (try_statement)
  (match_statement)
  (import_from_statement)
  (parameters)
  (argument_list)
  (parenthesized_expression)
  (generator_expression)
  (list_comprehension)
  (set_comprehension)
  (dictionary_comprehension)
  (tuple)
  (list)
  (set)
  (dictionary)
  (string)
] @fold

[
  (import_statement)
  (import_from_statement)
]+ @fold
