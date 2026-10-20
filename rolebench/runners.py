"""Run plumbing shared by every harness. (Harness adapters: still to do.)"""

from __future__ import annotations

import os

from . import config


def scrubbed_env(base=None) -> dict:
    """The environment a run gets: no tokens, no API keys."""
    return {k: v for k, v in (os.environ if base is None else base).items() if k not in config.SCRUBBED_ENV}


def wrap_scope(argv: list[str]) -> list[str]:
    return ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={config.MEMORY_MAX}",
            "-p", "MemorySwapMax=0", *argv]
