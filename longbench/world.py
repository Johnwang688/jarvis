"""Deterministic fixture helpers.

Every generator here is a pure function of a seeded `random.Random`, so a task's
world is byte-identical on every machine that builds it with the same seed. That
is what lets the grader recompute ground truth instead of storing it.

The filler is not decoration. long-bench exists to put a run *past* the point
where a harness has to start throwing context away, and the only honest way to
do that is with tool results that are genuinely large. A task whose fixture fits
in one context window measures nothing this bench is for.
"""

from __future__ import annotations

import random
from pathlib import Path

WORDS = (
    "buffer channel cursor daemon envelope fragment gateway handler ingest "
    "journal kernel ledger manifest nonce offset partition quorum registry "
    "shard tenant upstream vector window yield zone anchor bridge cluster "
    "digest export filter groups header index jitter keyspace latency mirror "
    "nodes origin packet queue router socket target unit vault worker"
).split()

SERVICE_NAMES = (
    "billing catalog checkout dispatch events feeds gateway identity inventory "
    "jobs ledger mailer notify orders payments pricing profiles quotas ratings "
    "receipts refunds reports reviews routing search sessions shipping stock "
    "support sync tagging tasks taxes tokens tracing users vendors wallets "
    "webhooks workers audit alerts archive assets backup batch cache config "
    "consent content crawler devices exports imports leases limits locks"
).split()


def words(rng: random.Random, count: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(count))


def sentence(rng: random.Random, count: int = 12) -> str:
    text = words(rng, count)
    return text[0].upper() + text[1:] + "."


def paragraph(rng: random.Random, sentences: int = 4) -> str:
    return " ".join(sentence(rng, rng.randint(8, 16)) for _ in range(sentences))


def filler_lines(rng: random.Random, count: int, prefix: str = "") -> list[str]:
    return [f"{prefix}{sentence(rng, rng.randint(6, 14))}" for _ in range(count)]


def write_files(root: Path, files: dict[str, str]) -> None:
    """Materialize a {relative path: text} mapping under `root`."""
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def read_text(root: Path, relative: str) -> str:
    """A file's contents, or "" if it is missing or unreadable.

    Graders must never raise on a world the agent mangled — a grader crash
    reads as an infrastructure failure when it is really a score of zero.
    """
    try:
        return (root / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def unchanged(root: Path, relative: str, original: str) -> bool:
    """True if the file is still byte-for-byte what the fixture wrote."""
    return read_text(root, relative) == original
