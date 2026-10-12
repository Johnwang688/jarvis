; Built by scripts/vendor-queries.mjs from nvim-treesitter f603a2f4da48728f80257fb5fbb90145fd1dc173 (Apache-2.0):
; runtime/queries/python/injections.scm.
; Modified for web-tree-sitter; see NOTICE and dropped.json beside this directory.

(call
  function: (attribute
    object: (identifier) @_re)
  arguments: (argument_list
    (string
      (string_start)
      [
        (string_content) @injection.content
        (interpolation)
      ]+
      (string_end)))
  (#eq? @_re "re")
  (#set! injection.language "regex"))

(call
  function: (attribute
    object: (identifier) @_re)
  arguments: (argument_list
    (concatenated_string
      (string
        (string_start)
        [
          (string_content) @injection.content
          (interpolation)
        ]+
        (string_end))))
  (#eq? @_re "re")
  (#set! injection.language "regex"))

((binary_operator
  left: (string
    (string_content) @injection.content)
  operator: "%")
  (#set! injection.language "printf"))

((comment) @injection.content
  (#set! injection.language "comment"))
