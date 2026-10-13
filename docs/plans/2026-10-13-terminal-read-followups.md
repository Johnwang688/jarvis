# Terminal-read follow-ups

Close the reviewed IPython, irb and pry explicit-environment input gaps; recognize immediate IPython shell escapes with the existing parse budget. Secret-named `%env` and `%set_env` assignments echo values, so they refuse alongside lookups and dumps. Benign variable assignments stay readable. The source semantics are in [IPython osm.py](https://github.com/ipython/ipython/blob/main/IPython/core/magics/osm.py) (`env` and `set_env`); this is shape detection, not evaluation of arbitrary Python expansion.

Replace bun/deno verb recursion and repeated tail copies with a single cursor. Preserve source/option ambiguity semantics. Tests cover 2,000 verbs, benign and credential operands, option parsing, REPL false positives, bounded shell escapes and unseen magic beyond the scan limit. Document dynamic references, aliases/imports, os.system and assigned shell capture as remaining limits. No commands in fixture text execute and no real credentials are used.

Plan independently reviewed by haiku_cache; implementation review and full sweep required before merge.
