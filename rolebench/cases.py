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


GT_KEYS = {"id", "severity", "mechanism", "location", "evidence", "source"}
POOLED_KEYS = {"case", "source", "adjudicated_by", "date", "ground_truth", "members"}
# A pooled key may also hold non-issues: clusters a one-time Opus ruling found
# invalid and the lead accepted as such (`pool --accept-invalid`).
POOLED_NI_KEYS = {"id", "claim", "location", "why_not", "citation", "source"}
POOLED_NI_PREFIX = "pool-ni-"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def pooled_path(case_id: str, directory: Path | None = None) -> Path:
    """The committed pooled key: rolebench/cases/pooled/<case>.json."""
    return (directory or config.cases_dir()) / "pooled" / f"{case_id}.json"


def validate_pooled(d, case: dict, rubric: dict | None = None) -> dict:
    """A pooled key (pool.adjudicate writes it): the ground-truth schema with
    `pool-` ids that never collide with the original key, who adjudicated it
    and when, and each entry's member findings (run id, finding index).
    Optional `non_issues` (`pool-ni-` ids) are clusters ruled invalid and
    accepted as non-issues; `members` lists both kinds. Optional `duplicates`
    maps a key id (original, rubric or pooled) to the findings of clusters a
    ruling found to be that entry. One finding is never under two ids."""
    _need(isinstance(d, dict) and POOLED_KEYS <= set(d) <= POOLED_KEYS | {"non_issues", "duplicates"},
          f"a pooled key has exactly {sorted(POOLED_KEYS)} (and optionally non_issues, duplicates)")
    _need(d["case"] == case["id"], f"pooled key for {d['case']!r}, not {case['id']!r}")
    _need(d["source"] == "pooled", "source must be 'pooled'")
    _need(isinstance(d["adjudicated_by"], str) and d["adjudicated_by"].strip() != "", "adjudicated_by must be a name")
    _need(isinstance(d["date"], str) and bool(DATE_RE.match(d["date"])), "date must be YYYY-MM-DD")
    _need(not SECRET_RE.search(json.dumps(d)), "pooled key contains something that looks like a credential")
    original = {g["id"] for g in case.get("ground_truth") or []}
    gt = d["ground_truth"]
    _need(isinstance(gt, list), "ground_truth must be a list")
    ids = set()
    for g in gt:
        _need(isinstance(g, dict) and set(g) == GT_KEYS, f"pooled ground truth entries need exactly {sorted(GT_KEYS)}")
        _need(all(isinstance(v, str) and v.strip() for v in g.values()), "ground_truth fields must be non-empty strings")
        _need(g["severity"] in config.SEVERITIES, f"bad severity {g['severity']!r}")
        _need(g["id"].startswith("pool-") and not g["id"].startswith(POOLED_NI_PREFIX)
              and g["id"] not in ids and g["id"] not in original,
              f"pooled id {g['id']!r} must start with pool- and be unique")
        ids.add(g["id"])
    nis = d.get("non_issues", [])
    _need(isinstance(nis, list), "non_issues must be a list")
    for n in nis:
        _need(isinstance(n, dict) and set(n) == POOLED_NI_KEYS, f"pooled non-issues need exactly {sorted(POOLED_NI_KEYS)}")
        _need(all(isinstance(v, str) and v.strip() for v in n.values()), "non-issue fields must be non-empty strings")
        _need(n["id"].startswith(POOLED_NI_PREFIX) and n["id"] not in ids and n["id"] not in original,
              f"pooled non-issue id {n['id']!r} must start with {POOLED_NI_PREFIX} and be unique")
        ids.add(n["id"])
    mem = d["members"]
    _need(isinstance(mem, dict) and set(mem) == ids, "members must list exactly the pooled ids")
    dup = d.get("duplicates", {})
    targets = original | {g["id"] for g in gt} | {e["id"] for e in (rubric or {}).get("entries") or ()}
    _need(isinstance(dup, dict) and all(isinstance(k, str) and k in targets for k in dup),
          f"duplicates must map key ids ({sorted(targets)}) to findings")
    placed: dict = {}
    for gid, rows in list(mem.items()) + [(f"duplicate of {k}", v) for k, v in dup.items()]:
        _need(isinstance(rows, list) and rows and all(
            isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and r[0]
            and isinstance(r[1], int) and not isinstance(r[1], bool) and r[1] >= 0 for r in rows),
            f"members of {gid} must be [run id, finding index] pairs")
        for r in rows:
            _need(tuple(r) not in placed, f"finding {r} is under both {placed.get(tuple(r))} and {gid}")
            placed[tuple(r)] = gid
    return d


def load_pooled(case_id: str, directory: Path | None = None) -> dict | None:
    """The case's validated pooled key, or None when it has none."""
    path = pooled_path(case_id, directory)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"cannot read {path}: {exc}") from exc
    return validate_pooled(d, load(case_id, directory), rubric=load_rubric(case_id, directory))


def full_ground_truth(case: dict, directory: Path | None = None) -> list[dict]:
    """The original key plus the accepted pooled entries (what a new run is judged against)."""
    key = load_pooled(case["id"], directory) if case.get("kind") == "review" else None
    return list(case.get("ground_truth") or []) + list((key or {}).get("ground_truth") or [])


# --- locations ------------------------------------------------------------------------

# A location is free text that names one or more `path[:line[-line]]`. A path
# needs a known extension, so prose (`text.splitlines()`) is never one. After a
# path, `:12, :40-44` and `12-15, 43` add lines to that path; an approximate
# `:~1846`, `~3167-3175` or `(~1565)` is a line too. A path named bare and then
# given lines only by such later references takes those lines.
LOC_EXT = ("py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "jsonl", "md", "toml", "yaml", "yml",
           "css", "html", "sh", "txt", "cfg", "ini", "c", "h", "cpp", "hpp", "rs", "go", "lock", "svg", "service",
           "ps1", "sql", "xml", "csv")
_EXT = "|".join(sorted(LOC_EXT, key=len, reverse=True))
_RANGE = r"~?(\d+)(?:\s*-\s*~?(\d+))?"
LOC_RE = re.compile(r"(?<![\w./-])((?:[\w.@+-]+/)*[\w.@+-]*\.(?:" + _EXT + r"))(?![\w-])"
                    r"|(?:(?<=[\s(;,])|^)(?::|~)" + _RANGE)
LINES_RE = re.compile(r":" + _RANGE + r"((?:\s*,\s*:?~?\d+(?:\s*-\s*~?\d+)?)*)")


def parse_locations(text: str) -> list[tuple[str, int | None, int | None]]:
    """[(path, first line or None, last line or None), ...] named in `text`."""
    out: list[tuple[str, int | None, int | None]] = []
    text = (text or "").replace("\\", "/")
    last, bare = None, None          # bare: index of the last path's line-less entry
    pos = 0
    while True:
        m = LOC_RE.search(text, pos)
        if not m:
            break
        if m.group(1):
            path = m.group(1)
            while path.startswith("./"):
                path = path[2:]
            last, bare = path, None
            lines = LINES_RE.match(text, m.end())
            if lines:
                out.append((path, int(lines.group(1)), int(lines.group(2) or lines.group(1))))
                for extra in re.finditer(_RANGE, lines.group(3) or ""):
                    out.append((path, int(extra.group(1)), int(extra.group(2) or extra.group(1))))
                pos = lines.end()
            else:
                out.append((path, None, None))
                bare = len(out) - 1
                pos = m.end()
        else:
            if last is not None:
                ref = (last, int(m.group(2)), int(m.group(3) or m.group(2)))
                if bare is not None:
                    out[bare], bare = ref, None
                else:
                    out.append(ref)
            pos = m.end()
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# --- the rubric -----------------------------------------------------------------------

RUBRIC_KEYS = {"case", "head", "seeded", "built_by", "built_at", "original_review", "entries", "non_issues"}
RUBRIC_ENTRY_KEYS = {"id", "severity", "mechanism", "location", "evidence", "verified", "fixed_later"}
RUBRIC_NI_KEYS = {"id", "claim", "location", "why_not"}
REVIEW_KEYS = {"gt", "status", "note"}
VERIFIED = ("reproduced", "traced")
REVIEW_STATUS = ("confirmed", "disputed")
RB_ID = re.compile(r"^rb\d+$")
NI_ID = re.compile(r"^ni\d+$")


def rubric_path(case_id: str, directory: Path | None = None) -> Path:
    return (directory or config.cases_dir()) / "rubric" / f"{case_id}.json"


def validate_rubric(d, case: dict) -> dict:
    """A case's rubric (rolebench/cases/README.md, "Rubric"), or CaseError."""
    _need(isinstance(d, dict) and RUBRIC_KEYS <= set(d) <= RUBRIC_KEYS | {"notes"},
          f"a rubric has exactly {sorted(RUBRIC_KEYS)} (and optionally notes)")
    _need(case.get("kind") == "review", "only a review case has a rubric")
    _need(d["case"] == case["id"], f"rubric for {d['case']!r}, not {case['id']!r}")
    head = d["head"]
    _need(isinstance(head, str) and bool(SHA_RE.match(head))
          and (head.startswith(case["head"]) or case["head"].startswith(head)),
          f"rubric head {head!r} is not the case's head {case['head']!r}")
    _need(d["seeded"] is (case.get("seeded") is not None), "rubric 'seeded' must say whether the case is seeded")
    _text(d, "built_by")
    _need(isinstance(d["built_at"], str) and bool(DATE_RE.match(d["built_at"])), "built_at must be YYYY-MM-DD")
    _need("notes" not in d or isinstance(d["notes"], str), "notes must be a string")
    _need(not SECRET_RE.search(json.dumps(d)), "rubric contains something that looks like a credential")
    review = d["original_review"]
    _need(isinstance(review, list), "original_review must be a list")
    for o in review:
        disputed = isinstance(o, dict) and o.get("status") == "disputed"
        # a disputed entry names the non-issue that answers it, so a match on it scores as that non-issue
        _need(isinstance(o, dict) and set(o) == (REVIEW_KEYS | {"non_issue"} if disputed else REVIEW_KEYS),
              f"original_review entries need exactly {sorted(REVIEW_KEYS)} (and non_issue when disputed)")
        _need(all(isinstance(v, str) and v.strip() for v in o.values()), "original_review fields must be non-empty strings")
        _need(o["status"] in REVIEW_STATUS, f"bad original_review status {o['status']!r}")
    gt_ids = [g["id"] for g in case["ground_truth"]]
    reviewed = [o["gt"] for o in review]
    _need(len(reviewed) == len(set(reviewed)) and sorted(reviewed) == sorted(gt_ids),
          f"original_review must cover exactly the ground truth {gt_ids}, once each (got {reviewed})")
    ids: set[str] = set(gt_ids)
    _need(isinstance(d["entries"], list), "entries must be a list")
    for e in d["entries"]:
        _need(isinstance(e, dict) and set(e) == RUBRIC_ENTRY_KEYS, f"rubric entries need exactly {sorted(RUBRIC_ENTRY_KEYS)}")
        _need(isinstance(e["id"], str) and bool(RB_ID.match(e["id"])) and e["id"] not in ids,
              f"rubric entry id {e.get('id')!r} must be rb<n> and unique")
        ids.add(e["id"])
        _need(all(isinstance(e[k], str) and e[k].strip() for k in ("severity", "mechanism", "location", "evidence", "verified")),
              f"{e['id']}: fields must be non-empty strings")
        _need(e["severity"] in config.SEVERITIES, f"{e['id']}: bad severity {e['severity']!r}")
        _need(e["verified"] in VERIFIED, f"{e['id']}: verified must be one of {VERIFIED}")
        _need(e["fixed_later"] is None or (isinstance(e["fixed_later"], str) and e["fixed_later"].strip() != ""),
              f"{e['id']}: fixed_later must be null or a string")
        _need(bool(parse_locations(e["location"])), f"{e['id']}: location {e['location']!r} names no path[:line[-line]]")
    _need(isinstance(d["non_issues"], list), "non_issues must be a list")
    for n in d["non_issues"]:
        _need(isinstance(n, dict) and set(n) == RUBRIC_NI_KEYS, f"rubric non-issues need exactly {sorted(RUBRIC_NI_KEYS)}")
        _need(isinstance(n["id"], str) and bool(NI_ID.match(n["id"])) and n["id"] not in ids,
              f"non-issue id {n.get('id')!r} must be ni<n> and unique")
        ids.add(n["id"])
        _need(all(isinstance(n[k], str) and n[k].strip() for k in ("claim", "location", "why_not")),
              f"{n['id']}: fields must be non-empty strings")
    ni_ids = {n["id"] for n in d["non_issues"]}
    for o in review:
        if o["status"] == "disputed":
            _need(o["non_issue"] in ni_ids, f"disputed {o['gt']} names {o['non_issue']!r}, which is not one of the rubric's non-issues")
    return d


def load_rubric(case_id: str, directory: Path | None = None) -> dict | None:
    """The case's validated rubric, or None when it has none."""
    path = rubric_path(case_id, directory)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"cannot read {path}: {exc}") from exc
    return validate_rubric(d, load(case_id, directory))


def scoring_key(case: dict, directory: Path | None = None) -> dict:
    """What the rubric grader scores against:
    {"original": the case's ground truth minus what the rubric disputed,
     "rubric": the rubric's verified entries, "pooled": accepted pooled entries,
     "entries": all three, "disputed": [ids, listed but never scored],
     "non_issues": rubric plus pooled non-issues, "members": the pooled key's
     members, "has_rubric": bool}."""
    rubric = load_rubric(case["id"], directory)
    pooled = load_pooled(case["id"], directory)
    review = {o["gt"]: o for o in (rubric or {}).get("original_review") or ()}
    disputed = [gid for gid, o in review.items() if o["status"] == "disputed"]
    # the rubric's note on a confirmed entry (written with the code open) helps a text-only matcher
    original = [{**g, "verified_as": review[g["id"]]["note"]} if g["id"] in review else dict(g)
                for g in case["ground_truth"] if g["id"] not in disputed]
    rb = [{"id": e["id"], "severity": e["severity"], "mechanism": e["mechanism"], "location": e["location"],
           "evidence": e["evidence"], "source": f"rubric ({e['verified']})"} for e in (rubric or {}).get("entries") or ()]
    pl = list((pooled or {}).get("ground_truth") or ())
    # a pooled non-issue's gate also reads its citation (the ruling's, always a path)
    nis = list((rubric or {}).get("non_issues") or ()) + [
        {**{k: n[k] for k in ("id", "claim", "location", "why_not")}, "gate": f"{n['location']}; {n['citation']}"}
        for n in (pooled or {}).get("non_issues") or ()]
    return {"original": original, "rubric": rb, "pooled": pl, "entries": original + rb + pl, "disputed": disputed,
            "disputed_as": {gid: review[gid]["non_issue"] for gid in disputed},
            "non_issues": nis, "members": dict((pooled or {}).get("members") or {}),
            "duplicates": {k: list(v) for k, v in ((pooled or {}).get("duplicates") or {}).items()},
            "has_rubric": rubric is not None}


def load(case_id: str, directory: Path | None = None) -> dict:
    path = (directory or config.cases_dir()) / f"{case_id}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"cannot read {path}: {exc}") from exc
    validate(d)
    _need(d["id"] == case_id, f"file {path.name} holds id {d['id']!r}")
    return d
