; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/toml/highlights.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

(bare_key) @property

[
  (string)
  (quoted_key)
] @string

(boolean) @boolean

(comment) @comment @spell

(escape_sequence) @string.escape

(integer) @number

(float) @number.float

[
  (local_date)
  (local_date_time)
  (local_time)
  (offset_date_time)
] @string.special

"=" @operator

[
  "."
  ","
] @punctuation.delimiter

[
  "["
  "]"
  "[["
  "]]"
  "{"
  "}"
] @punctuation.bracket
