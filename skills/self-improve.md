---
description: Use when the owner asks you to improve, fix, or extend your own code — "improve yourself", "add a tool for X", "fix that bug in your harness"
---

You may modify your own codebase (`~/projects/Jarvis`) — only when the owner
asks, only the change they asked for, one change per request.

1. **Read before you touch anything**: CLAUDE.md first — its invariants and
   "decisions already made" are binding, and it documents which tests cover
   what. It is long, so read it in full: read_file pages, and the footer of
   each page tells you the offset to continue from. Stopping at page one
   means working from a third of the rules. Then read every file you intend
   to change.
2. **Checkpoint first**: `run_command` `git add -A && git commit -m
   "checkpoint before <change>"`. If git is not clean, say so and stop —
   the owner may have uncommitted work.
3. **Off limits, no exceptions**: secrets.py, files.py, tools/__init__.py,
   approvals.py, permissions.py — the layers that gate you. write_file and
   edit_file both refuse them; do not route around either with shell
   commands. If the improvement needs one of them changed, explain exactly
   what and why, and let the owner do it.
4. Make the change with **edit_file**, not by rewriting whole files. Match
   the codebase's style; keep the loop small; update CLAUDE.md in the same
   session if you added a capability or invariant — which is an edit_file
   job, since it is far too long to rewrite.
5. **Verify honestly**: run the free test suites for what you touched
   (`.venv/bin/python tests/<relevant>_check.py`, and any suite CLAUDE.md
   names for those files). A failing test means fix or revert — never
   report success past a red test.
6. Commit the result with a clear message, then report: what changed, why,
   test results, that a restart is needed for running surfaces, and that
   `git revert` undoes it.
7. Never iterate autonomously: one request, one verified change, one
   report. "Keep improving yourself" still means one change, then asking
   what is next.
