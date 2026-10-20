"""Case files: load and strictly validate (schema in the decisions doc)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
SECRET_RE = re.compile(r"ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
COMMON = {"id", "kind", "pr", "title", "base", "brief", "notes"}
REVIEW = COMMON | {"head", "focus", "suites", "control", "seeded", "ground_truth"}
IMPLEMENT = COMMON | {"size", "merged", "hidden_tests", "regression_suites", "scope", "fixtures"}


class CaseError(ValueError):
    pass


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise CaseError(msg)


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    _need(isinstance(v, str) and v.strip() != "", f"{key!r} must be a non-empty string")
    return v


def _paths(d: dict, key: str, *, nonempty: bool = False) -> list[str]:
    v = d.get(key)
    _need(isinstance(v, list) and all(isinstance(x, str) for x in v), f"{key!r} must be a list of strings")
    _need(bool(v) or not nonempty, f"{key!r} must not be empty")
    for p in v:
        _need(bool(p) and not p.startswith("/") and "\\" not in p and ".." not in p.split("/"),
              f"{key!r}: unsafe path {p!r}")
    return v


def validate(d) -> dict:
    """Return the case unchanged if it is well formed, else raise CaseError."""
    _need(isinstance(d, dict), "a case is a JSON object")
    kind = d.get("kind")
    _need(kind in ("review", "implement"), "kind must be 'review' or 'implement'")
    extra = set(d) - (REVIEW if kind == "review" else IMPLEMENT)
    _need(not extra, f"unknown keys: {sorted(extra)}")
    _need(bool(ID_RE.match(_text(d, "id"))), "id must be a lowercase slug")
    _need(d.get("pr") is None or (isinstance(d["pr"], int) and not isinstance(d["pr"], bool)),
          "pr must be an integer or null")
    _need(bool(SHA_RE.match(_text(d, "base"))), "base must be a hex sha")
    _text(d, "brief")
    _need(not SECRET_RE.search(json.dumps(d)), "case text contains something that looks like a credential")
    if kind == "review":
        _text(d, "title")
        _need(bool(SHA_RE.match(_text(d, "head"))), "head must be a hex sha")
        _need(isinstance(d.get("focus"), list) and all(isinstance(x, str) for x in d["focus"]),
              "focus must be a list of strings")
        _paths(d, "suites")
        _need(isinstance(d.get("control"), bool), "control must be true or false")
        seeded = d.get("seeded")
        _need(seeded is None or (isinstance(seeded, dict) and set(seeded) == {"description", "patch"}
                                 and all(isinstance(v, str) and v.strip() for v in seeded.values())),
              "seeded must be null or {description, patch}")
        gt = d.get("ground_truth")
        _need(isinstance(gt, list), "ground_truth must be a list")
        ids = set()
        for g in gt:
            _need(isinstance(g, dict) and set(g) == {"id", "severity", "mechanism", "location", "evidence", "source"},
                  "ground_truth entries need exactly id, severity, mechanism, location, evidence, source")
            _need(all(isinstance(v, str) and v.strip() for v in g.values()), "ground_truth fields must be non-empty strings")
            _need(g["severity"] in config.SEVERITIES, f"bad severity {g['severity']!r}")
            _need(g["id"] not in ids, f"duplicate ground truth id {g['id']!r}")
            ids.add(g["id"])
        scored = [g for g in gt if g["severity"] != "NIT"]
        if d["control"]:
            _need(not scored, "a control case has no scored ground truth")
        else:
            _need(bool(scored), "a non-control case needs at least one scored ground truth")
    else:
        _need(d.get("size") in ("S", "M", "L"), "size must be S, M or L")
        _need(bool(SHA_RE.match(_text(d, "merged"))), "merged must be a hex sha")
        _paths(d, "hidden_tests", nonempty=True)
        _paths(d, "regression_suites")
        _paths(d, "scope")
        _paths(d, "fixtures")
    return d


def load(case_id: str, directory: Path | None = None) -> dict:
    path = (directory or config.cases_dir()) / f"{case_id}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"cannot read {path}: {exc}") from exc
    validate(d)
    _need(d["id"] == case_id, f"file {path.name} holds id {d['id']!r}")
    return d
