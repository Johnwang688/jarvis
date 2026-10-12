; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/c/folds.scm, runtime/queries/cpp/folds.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

[
  (for_statement)
  (if_statement)
  (while_statement)
  (do_statement)
  (switch_statement)
  (case_statement)
  (function_definition)
  (struct_specifier)
  (enum_specifier)
  (comment)
  (preproc_if)
  (preproc_elif)
  (preproc_else)
  (preproc_ifdef)
  (preproc_function_def)
  (initializer_list)
  (gnu_asm_expression)
  (preproc_include)+
] @fold

(compound_statement
  (compound_statement) @fold)

[
  (for_range_loop)
  (class_specifier)
  (field_declaration
    type: (enum_specifier)
    default_value: (initializer_list))
  (template_declaration)
  (namespace_definition)
  (try_statement)
  (catch_clause)
  (lambda_expression)
] @fold
