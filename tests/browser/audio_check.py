"""Phase 0/1 of the face: audio round trip + Jarvis speaking, in the real window.

Thin wrapper around jarvis.face.server — serves the HUD static dir with the
/say TTS route and opens audiocheck.html in the app-mode Chromium window.
Steps 1–3 are free; step 4 (Speak) makes a real TTS call (fractions of a cent).

Run:  .venv/bin/python tests/browser/audio_check.py
"""

from __future__ import annotations

import sys

from jarvis.face import server

if __name__ == "__main__":
    sys.exit(server.main("audiocheck.html"))
