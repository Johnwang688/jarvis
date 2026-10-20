"""Report math. (Markdown tables and the JSONL log writer: still to do.)"""

from __future__ import annotations


def span(values) -> dict | None:
    """Range and mean over runs, ignoring None; None when nothing was measured."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"lo": min(vals), "hi": max(vals), "mean": sum(vals) / len(vals), "n": len(vals)}


def by_model(rows: list[dict], fields: list[str]) -> dict:
    """rows (one per run, with harness, model and score fields) -> {(harness, model): {field: span}}."""
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["harness"], r["model"]), []).append(r)
    return {key: {f: span(r.get(f) for r in rs) for f in fields} for key, rs in groups.items()}
