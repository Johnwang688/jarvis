"""`python -m jarvis.face [page]` — serve the face and open its window."""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "jarvis.html"))
