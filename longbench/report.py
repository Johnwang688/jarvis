"""Printing results. Stdlib only, so long-bench runs anywhere Python does."""

from __future__ import annotations

import json
from pathlib import Path

from .spec import CATEGORIES


def _cell(value: float | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value}{suffix}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def label(run: dict) -> str:
    """`harness/model`, with the mode appended when it is not the default.

    The mode rides in the row label rather than its own column so that pooling
    a shell run with a no-shell one is visible at a glance instead of quietly
    averaging two different experiments.
    """
    marks = []
    if run.get("mode", "shell") != "shell":
        marks.append(run["mode"])
    if run.get("window"):
        marks.append(f"w{run['window'] // 1000}k")
    suffix = f" [{', '.join(marks)}]" if marks else ""
    return f"{run['harness']}/{run['model']}{suffix}"


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    rule = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    ]
    return "\n".join([line, rule, *body])


def one_result(document: dict) -> str:
    """The full detail of a single graded run, failures spelled out."""
    run = document["run"]
    out = [
        f"\n{document['task']}  seed {document['seed']}  "
        f"{label(run)}",
        f"  {'overall':<14}{document['overall']:.0%}",
    ]
    for category in CATEGORIES:
        if category in document["categories"]:
            out.append(f"  {category:<14}{document['categories'][category]:.0%}")
    out.append(
        f"  cost {_cell(run['cost_usd'] and round(run['cost_usd'], 4), ' USD')} · "
        f"{_cell(run['duration_s'] and round(run['duration_s'], 1), 's')} · "
        f"{_cell(run['turns'])} turns · {run['tool_calls']} tool calls · "
        f"{run['redundant_calls']} repeated"
    )
    if run.get("tokens"):
        t = run["tokens"]
        out.append(
            f"  tokens in {t.get('input', 0):,} · out {t.get('output', 0):,} · "
            f"cache read {t.get('cache_read', 0):,}"
        )
    if run.get("error"):
        out.append(f"  ERROR     {run['error']}")

    missed = [c for c in document["checks"] if not c["ok"]]
    if missed:
        out.append("  missed:")
        for check in missed:
            got = f"{check['earned']:g}/{check['possible']:g}"
            detail = f" — {check['detail']}" if check["detail"] else ""
            out.append(f"    [{check['category']}] {check['name']}  {got}{detail}")
    return "\n".join(out)


def comparison(documents: list[dict]) -> str:
    """One row per (harness, model, task), plus a per-harness summary."""
    if not documents:
        return "no results"

    rows = []
    for doc in sorted(documents, key=lambda d: (label(d["run"]), d["task"])):
        run = doc["run"]
        rows.append([
            label(run),
            doc["task"],
            _pct(doc["overall"]),
            *[_pct(doc["categories"].get(c)) for c in CATEGORIES],
            _cell(run["cost_usd"] and round(run["cost_usd"], 4)),
            _cell(run["duration_s"] and round(run["duration_s"])),
            _cell(run["turns"]),
            str(run["tool_calls"]),
        ])
    headers = ["harness/model", "task", "overall", *CATEGORIES,
               "$", "s", "turns", "calls"]

    grouped: dict[str, list[dict]] = {}
    for doc in documents:
        grouped.setdefault(label(doc["run"]), []).append(doc)

    summary_rows = []
    for key, docs in sorted(grouped.items()):
        costs = [d["run"]["cost_usd"] for d in docs if d["run"]["cost_usd"] is not None]
        secs = [d["run"]["duration_s"] for d in docs if d["run"]["duration_s"] is not None]
        summary_rows.append([
            key,
            f"{len(docs)} task(s)",
            _pct(sum(d["overall"] for d in docs) / len(docs)),
            *[
                _pct(
                    sum(d["categories"][c] for d in docs if c in d["categories"])
                    / max(1, sum(1 for d in docs if c in d["categories"]))
                ) if any(c in d["categories"] for d in docs) else "—"
                for c in CATEGORIES
            ],
            _cell(round(sum(costs), 4) if costs else None),
            _cell(round(sum(secs)) if secs else None),
            "", "",
        ])

    return (
        "\nper task\n" + table(rows, headers)
        + "\n\nper harness (mean score, total cost)\n" + table(summary_rows, headers)
    )


def load_results(root: Path) -> list[dict]:
    documents = []
    for path in sorted(root.rglob("result.json")):
        try:
            documents.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return documents
