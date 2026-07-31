---
description: Use when the owner asks to open or close the whiteboard / design board / sketch pad
---

1. **Open:** `run_command` with `jarvis face whiteboard.html` (reason:
   "opening the whiteboard window"). The face server is already running, so
   this just opens a new window on it and returns immediately. It needs the
   owner's approval — they can answer ALWAYS to allowlist `jarvis` commands
   if they want this frictionless.
2. Never try to open `http://localhost:8402/whiteboard.html` with your own
   browser tools — that is your control plane and the tools refuse it by
   design. The command in step 1 opens it on the owner's screen instead.
3. **Close:** call `whiteboard_close` — the window shuts itself. It only
   works on the whiteboard; no other window listens for the signal, so do
   not try to close anything else this way.
4. Related: finished designs live in the `designs/` directory and are served
   at `http://localhost:8403/<name>/` — that origin you MAY open and
   screenshot when the owner asks about a design.
