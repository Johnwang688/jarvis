"""Pooled ground truth (TREC-style): the valid-new findings of every judged run,
clustered once by a blinded Opus call, adjudicated by the lead.

The pilot showed one reviewer's key is incomplete: on some cases every model,
Opus included, found real defects the original review did not, and the judge
confirmed them as "valid-new". Pooling turns those into ground truth:

1. `pool_case` collects every judged run's valid-new findings for a case and,
   for the ones not yet in a cluster, makes **one** Opus call (blinded, in a
   read-only judge cell, effort high, net gate, quota guard and ceiling as for
   the judge) that assigns each to an existing cluster or a new one. The pool
   lives in `pool/<case>.json` under the bench's home; re-running it only
   sends what is new, so it is idempotent and grows as runs arrive.
2. `adjudicate` is the lead's call: accepted clusters become the committed
   pooled key `rolebench/cases/pooled/<case>.json` (the ground-truth schema,
   `source: "pooled"`, who and when, and each cluster's members), rejected
   ones never do. The original case file is never edited. Findings a later
   pool run proposes for an accepted cluster wait as `pending` until the lead
   takes them in (`accept_members`). The key is built and validated before
   anything is saved; a committed id is never dropped or reused (a lost pool
   restarts from the key, new ids start past it) and never given another
   mechanism or location without `redefine`.
3. `rescore` scores a judged run against the original key and against the
   pooled key (original plus accepted clusters) without judging it again: a
   finding that is a member of an accepted cluster is a hit on that pooled
   id, deterministically. Runs judged after pooling see original plus pooled
   ground truth in the judge's key. A member counts only while the run's
   current verdict for that finding is still valid-new.

Pooling bias: the pool is built from the models that ran, so a defect no
model found is still missing, and a model's own finds raise its pooled recall
only when another run's finds do not already cover them.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import shutil
import time
from pathlib import Path

from . import cases, config, judge, quota, runners, sandbox, snapshot

PROPOSED, ACCEPTED, REJECTED = "proposed", "accepted", "rejected"
CLUSTER_ID = re.compile(r"^p\d+$")
NEW_ID = re.compile(r"^new\d+$")
POOL_SEVERITIES = ("HIGH", "MED", "LOW")
KEY_PREFIX = "pool-"


class PoolError(ValueError):
    pass


# --- where things live --------------------------------------------------------------


def pool_path(case_id: str) -> Path:
    return config.home() / "pool" / f"{case_id}.json"


def key_path(case_id: str) -> Path:
    return cases.pooled_path(case_id)


def _atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_pool(case_id: str) -> dict:
    """The pool, or a fresh one. A fresh pool starts from the committed key's
    accepted clusters (the pool file lives in a cache; the key is committed),
    so a lost pool can never reuse a committed id or drop an entry."""
    path = pool_path(case_id)
    if not path.exists():
        pool = {"case": case_id, "clusters": [], "calls": []}
        key = cases.load_pooled(case_id) if cases.pooled_path(case_id).exists() else None
        for g in (key or {}).get("ground_truth") or ():
            cid = g["id"][len(KEY_PREFIX):]
            pool["clusters"].append({"id": cid, "severity": g["severity"], "mechanism": g["mechanism"],
                                     "location": g["location"], "status": ACCEPTED, "judge_notes": [], "pending": [],
                                     "members": [{"run": r, "finding": int(i)} for r, i in key["members"].get(g["id"], [])],
                                     "adjudicated_by": key["adjudicated_by"], "adjudicated_on": key["date"],
                                     "restored_from_key": True})
        return pool
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("case") != case_id or not isinstance(doc.get("clusters"), list):
        raise PoolError(f"{path} is not the pool for {case_id}")
    return doc


class _Locked:
    def __init__(self, case_id: str):
        self.path = Path(str(pool_path(case_id)) + ".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.handle.close()


# --- collecting --------------------------------------------------------------------


def collect(case_id: str, runs_root: Path | None = None) -> list[dict]:
    """Every valid-new finding of every validly judged review run of the case:
    {"run", "finding" (index into findings.json), "severity" (the judge's),
    "file", "line", "title", "scenario", "note" (the judge's)}."""
    runs_root = runs_root or config.runs_root()
    out = []
    for run_dir in sorted(p.parent for p in runs_root.glob("*/run.json")):
        try:
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            if run.get("case_id") != case_id or run.get("kind") != "review":
                continue
            jd = json.loads((run_dir / "judge.json").read_text(encoding="utf-8"))
            items = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not jd.get("valid"):
            continue
        ids = jd.get("finding_ids") or {}
        for m in jd.get("matches") or ():
            if m.get("verdict") != "valid-new" or m.get("finding") not in ids:
                continue
            index = ids[m["finding"]]
            f = items[index]
            out.append({"run": run["run_id"], "finding": index, "severity": m.get("severity"),
                        "file": f.get("file", ""), "line": f.get("line"), "title": f.get("title", ""),
                        "scenario": f.get("scenario", ""), "note": m.get("note", "")})
    return out


def _member_key(item: dict) -> tuple[str, int]:
    return (item["run"], int(item["finding"]))


# --- the clustering call --------------------------------------------------------------


PROMPT = """\
You are clustering code-review findings. Several reviewers read the same change; a \
judge confirmed each finding below as a real defect that the original answer key did \
not list. You see no indication of who wrote which finding. Do not run anything.

Many findings describe the same defect in different words. Group them: two findings \
belong together when they name the same mechanism at the same place (wording, line \
numbers and severity may differ).

## Existing clusters (keep their ids; add findings to them when they match)
{clusters}

## Findings to place
{findings}

## Your job
Put every finding in exactly one cluster: an existing one by its id, or a new one you \
name "new1", "new2", .... For each new cluster give a proposed "severity" (HIGH, MED or \
LOW), a one-sentence "mechanism" and a "location" (file and function or line).

Reply with one JSON object and nothing else:
{{"assign": [{{"finding": "V1", "cluster": "p1"}}, {{"finding": "V2", "cluster": "new1"}}], \
"new_clusters": [{{"id": "new1", "severity": "MED", "mechanism": "...", "location": "..."}}]}}
"""


def blind(items: list[dict], rng: random.Random) -> tuple[str, dict]:
    """(text, {"V1": index into items, ...}), shuffled, with no run or model named."""
    order = list(range(len(items)))
    rng.shuffle(order)
    ids = {f"V{k}": i for k, i in enumerate(order, 1)}
    lines = []
    for vid, i in ids.items():
        f = items[i]
        where = f["file"] + (f":{f['line']}" if f.get("line") is not None else "")
        lines.append(f"{vid} [{f.get('severity') or '?'}] {where} - {f['title']}\n    scenario: {f['scenario']}\n"
                     f"    judge: {f.get('note') or ''}")
    return "\n".join(lines), ids


def clusters_text(pool: dict) -> str:
    live = [c for c in pool["clusters"] if c["status"] != REJECTED]
    return "\n".join(f"{c['id']} [{c['severity']}] {c['location']}: {c['mechanism']}" for c in live) or "(none yet)"


def validate_reply(doc, finding_ids: list[str], cluster_ids: list[str]) -> tuple[dict, dict]:
    """({"V1": cluster id or "newK"}, {"newK": {severity, mechanism, location}}), or PoolError."""
    if not isinstance(doc, dict) or set(doc) != {"assign", "new_clusters"}:
        raise PoolError('expected {"assign": [...], "new_clusters": [...]}')
    if not isinstance(doc["assign"], list) or not isinstance(doc["new_clusters"], list):
        raise PoolError("assign and new_clusters must be lists")
    new = {}
    for c in doc["new_clusters"]:
        if not isinstance(c, dict) or set(c) != {"id", "severity", "mechanism", "location"}:
            raise PoolError(f"a new cluster needs exactly id, severity, mechanism, location: {c!r}")
        if not isinstance(c["id"], str) or not NEW_ID.match(c["id"]) or c["id"] in new:
            raise PoolError(f"bad or repeated new cluster id {c.get('id')!r}")
        if c["severity"] not in POOL_SEVERITIES:
            raise PoolError(f"bad severity {c['severity']!r}")
        if not all(isinstance(c[k], str) and c[k].strip() for k in ("mechanism", "location")):
            raise PoolError("mechanism and location must be non-empty strings")
        new[c["id"]] = {k: c[k].strip() if isinstance(c[k], str) else c[k] for k in ("severity", "mechanism", "location")}
    assign = {}
    for a in doc["assign"]:
        if not isinstance(a, dict) or set(a) != {"finding", "cluster"}:
            raise PoolError(f"bad assignment {a!r}")
        if not isinstance(a["finding"], str) or not isinstance(a["cluster"], str):
            raise PoolError(f"assignment values must be strings: {a!r}")
        if a["finding"] not in finding_ids or a["finding"] in assign:
            raise PoolError(f"unknown or repeated finding {a['finding']!r}")
        if a["cluster"] not in cluster_ids and a["cluster"] not in new:
            raise PoolError(f"assignment names unknown cluster {a['cluster']!r}")
        assign[a["finding"]] = a["cluster"]
    if sorted(assign) != sorted(finding_ids):
        raise PoolError("every finding needs exactly one cluster")
    unused = set(new) - set(assign.values())
    if unused:
        raise PoolError(f"new clusters with no finding: {sorted(unused)}")
    return assign, new


def default_pool_fn(prompt: str, cwd: Path) -> tuple[str, float | None]:
    """One Opus call in a read-only judge cell (effort high, as the judge)."""
    result = runners.run_claude(model="opus", prompt=prompt, cwd=cwd, timeout_s=900, max_turns=5,
                                read_only=True, run_id=f"pool-{snapshot.new_name()}")
    if result.error and not result.text:
        raise PoolError(f"pool run failed: {result.error}")
    return result.text, result.cost_usd


def _next_id(pool: dict, case_id: str) -> int:
    """Past every id in the pool and every id in the committed key."""
    nums = [int(c["id"][1:]) for c in pool["clusters"] if CLUSTER_ID.match(c["id"])]
    key = cases.load_pooled(case_id) if cases.pooled_path(case_id).exists() else None
    nums += [int(g["id"][len(KEY_PREFIX) + 1:]) for g in (key or {}).get("ground_truth") or ()
             if CLUSTER_ID.match(g["id"][len(KEY_PREFIX):])]
    return max(nums, default=0) + 1


def pool_case(case_id: str, pool_fn=default_pool_fn, quota_override: str = "", quota_reader=None,
              runs_root: Path | None = None, rng: random.Random | None = None) -> dict:
    """Cluster the case's valid-new findings that are in no cluster yet (one
    call, one retry on invalid JSON). Nothing new: no call. Returns the pool."""
    cases.load(case_id)                                   # the case must exist and validate
    with _Locked(case_id):
        pool = load_pool(case_id)
        seen = members(pool)
        new_items = [it for it in collect(case_id, runs_root) if _member_key(it) not in seen]
        if not new_items:
            return pool
        if pool_fn is default_pool_fn:
            refusal = sandbox.agent_net_refusal("judge")
            if refusal:
                raise PoolError(refusal)
        reader = quota_reader or quota.daemon_reader

        def check_quota():
            skip, note = quota.guard("claude", reader, override=quota_override)
            if skip:
                raise PoolError(f"pool call skipped on quota: {note}")
        quota_before = quota.readings(["claude"], reader)
        text, ids = blind(new_items, rng or random.Random())
        live_ids = [c["id"] for c in pool["clusters"] if c["status"] != REJECTED]
        prompt = PROMPT.format(clusters=clusters_text(pool), findings=text)
        cell = config.home() / "pool-cell" / snapshot.new_name()
        cell.mkdir(parents=True, exist_ok=True)
        error, cost, result, attempt = "", 0.0, None, 0
        try:
            for attempt in (1, 2):
                check_quota()
                reply, c = pool_fn(prompt if not error else prompt + f"\n\nYour previous reply was rejected: {error}\n"
                                   "Reply again with valid JSON only.", cell)
                cost += c or 0.0
                try:
                    result = validate_reply(judge.extract_json(reply), list(ids), live_ids)
                    break
                except (PoolError, judge.JudgeError) as exc:
                    error = str(exc)
        finally:
            shutil.rmtree(cell, ignore_errors=True)
            if attempt:                                  # a paid call is recorded, however it ended
                pool.setdefault("calls", []).append({
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "findings": len(new_items), "attempts": attempt,
                    "cost_usd": cost, "valid": result is not None, "error": error if result is None else "",
                    "effort": config.EFFORT["judge"], "quota_guard": f"off: {quota_override}" if quota_override else "on",
                    "quota_before": quota_before, "quota_after": quota.readings(["claude"], reader)})
                if result is None:
                    _atomic(pool_path(case_id), pool)
        if result is None:
            raise PoolError(f"pool output invalid twice: {error}")
        assign, new = result
        named = {}
        n = _next_id(pool, case_id)
        for tmp_id in sorted(new, key=lambda k: int(k[3:])):
            cid = f"p{n}"
            n += 1
            named[tmp_id] = cid
            pool["clusters"].append({"id": cid, **new[tmp_id], "members": [], "pending": [], "judge_notes": [],
                                     "status": PROPOSED})
        by_id = {c["id"]: c for c in pool["clusters"]}
        for vid, cid in assign.items():
            cluster = by_id[named.get(cid, cid)]
            item = new_items[ids[vid]]
            member = {k: item[k] for k in ("run", "finding", "severity", "file", "line", "title", "scenario")}
            # A finding proposed for an accepted cluster waits for the lead (--accept-members)
            # before it counts: the committed key changes only by adjudication.
            cluster.setdefault("pending", [])
            (cluster["pending"] if cluster["status"] == ACCEPTED else cluster["members"]).append(member)
            if item.get("note"):
                cluster["judge_notes"].append(item["note"])
        pool["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _atomic(pool_path(case_id), pool)
        return pool


def members(pool: dict) -> set[tuple[str, int]]:
    """Every (run, finding) already placed: members and pending ones."""
    return {(m["run"], int(m["finding"])) for c in pool["clusters"]
            for m in list(c["members"]) + list(c.get("pending") or [])}


# --- adjudication ---------------------------------------------------------------------


def adjudicate(case_id: str, accept=(), reject=(), severities: dict | None = None, by: str = "",
               date: str | None = None, accept_members=(), redefine: bool = False) -> dict:
    """The lead's call. Accepted clusters (with an optional severity override)
    go to the committed pooled key; rejected ones never do; `accept_members`
    takes an accepted cluster's pending members into it. The key is built and
    validated before anything is saved. Returns the pool."""
    if not (by or "").strip():
        raise PoolError("adjudication needs --by <name>")
    by = by.strip()
    severities = dict(severities or {})
    date = date or time.strftime("%Y-%m-%d")
    with _Locked(case_id):
        pool = load_pool(case_id)
        by_id = {c["id"]: c for c in pool["clusters"]}
        for cid in list(accept) + list(reject) + list(severities) + list(accept_members):
            if cid not in by_id:
                raise PoolError(f"no cluster {cid!r} in the pool for {case_id}")
        if set(accept) & set(reject):
            raise PoolError(f"accepted and rejected at once: {sorted(set(accept) & set(reject))}")
        for cid, sev in severities.items():
            if sev not in config.SEVERITIES:
                raise PoolError(f"bad severity {sev!r} for {cid}")
            if cid not in accept and by_id[cid]["status"] != ACCEPTED:
                raise PoolError(f"{cid}: a severity is set only on an accepted cluster")
        for cid in accept_members:
            if by_id[cid]["status"] != ACCEPTED and cid not in accept:
                raise PoolError(f"{cid}: members are accepted only into an accepted cluster")
        for cid in accept:
            c = by_id[cid]
            c["status"], c["adjudicated_by"], c["adjudicated_on"] = ACCEPTED, by, date
        for cid in reject:
            c = by_id[cid]
            c["status"], c["adjudicated_by"], c["adjudicated_on"] = REJECTED, by, date
        for cid, sev in severities.items():
            by_id[cid]["severity_final"] = sev
        for cid in accept_members:
            c = by_id[cid]
            moved = c.get("pending") or []
            c["members"] = list(c["members"]) + moved
            c["pending"] = []
            c.setdefault("members_accepted", []).append(
                {"by": by, "on": date, "members": [[m["run"], int(m["finding"])] for m in moved]})
        doc = key_doc(case_id, pool, by=by, date=date, redefine=redefine)   # raises before anything is saved
        _atomic(pool_path(case_id), pool)
        _atomic(key_path(case_id), doc)
        return pool


def key_entries(pool: dict) -> tuple[list[dict], dict]:
    """(ground-truth entries, {gt id: [[run, finding], ...]}) for the accepted
    clusters. The evidence names the cluster and how many findings it holds,
    never a reviewer's text: that text is untrusted (it reaches the judge's
    key for every later run) and may carry anything a reviewer wrote."""
    gt, mem = [], {}
    for c in pool["clusters"]:
        if c["status"] != ACCEPTED:
            continue
        gid = KEY_PREFIX + c["id"]
        gt.append({"id": gid, "severity": c.get("severity_final") or c["severity"], "mechanism": c["mechanism"],
                   "location": c["location"],
                   "evidence": f"pooled cluster {c['id']}: {len(c['members'])} reviewer finding(s) the blinded judge "
                               f"confirmed as valid-new (listed under members)",
                   "source": f"pooled cluster {c['id']}, accepted by {c['adjudicated_by']} on {c['adjudicated_on']}"})
        mem[gid] = sorted([m["run"], int(m["finding"])] for m in c["members"])
    return gt, mem


def key_doc(case_id: str, pool: dict, by: str | None = None, date: str | None = None, redefine: bool = False) -> dict:
    """The pooled key the pool's accepted clusters make, validated. A committed
    id is never dropped, and never given another mechanism or location unless
    `redefine` (the CLI's --redefine)."""
    gt, mem = key_entries(pool)
    path = key_path(case_id)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    new_by_id = {g["id"]: g for g in gt}
    for g in old.get("ground_truth") or ():
        now = new_by_id.get(g["id"])
        if now is None:
            raise PoolError(f"{g['id']} is in the committed key and would be dropped")
        if (now["mechanism"], now["location"]) != (g["mechanism"], g["location"]) and not redefine:
            raise PoolError(f"{g['id']} would change its mechanism or location; pass --redefine to do that on purpose")
    doc = {"case": case_id, "source": "pooled", "adjudicated_by": by or old.get("adjudicated_by") or "",
           "date": date or old.get("date") or time.strftime("%Y-%m-%d"), "ground_truth": gt, "members": mem}
    try:
        cases.validate_pooled(doc, cases.load(case_id))
    except cases.CaseError as exc:
        raise PoolError(f"the pooled key would not validate: {exc}")
    return doc


def write_key(case_id: str, pool: dict, by: str | None = None, date: str | None = None, redefine: bool = False) -> Path:
    """Write (or rewrite) the committed pooled key from the pool's accepted clusters."""
    doc = key_doc(case_id, pool, by=by, date=date, redefine=redefine)
    _atomic(key_path(case_id), doc)
    return key_path(case_id)


# --- rescoring --------------------------------------------------------------------------


def recall(ground_truth: list[dict], caught: set[str]) -> dict:
    scored = [g for g in ground_truth if g["severity"] != "NIT"]
    total_w = sum(config.WEIGHTS[g["severity"]] for g in scored)
    got_w = sum(config.WEIGHTS[g["severity"]] for g in scored if g["id"] in caught)
    medplus = [g for g in scored if g["severity"] in ("HIGH", "MED")]
    return {"weighted_recall": got_w / total_w if total_w else None,
            "medplus_recall": (sum(g["id"] in caught for g in medplus) / len(medplus)) if medplus else None}


def rescore(run_id: str, judge_doc: dict, case: dict, key: dict | None) -> dict:
    """{"original": recall, "pooled": recall, "pooled_hits": [...]} for a judged
    run, without judging it again: a finding that is a member of an accepted
    cluster is a hit on that pooled id; a judge's match on a pooled id (a run
    judged after pooling) is one too."""
    original = case["ground_truth"]
    orig_ids = {g["id"] for g in original}
    pooled_gt = (key or {}).get("ground_truth") or []
    pooled_ids = {g["id"] for g in pooled_gt}
    matches = judge_doc.get("matches") or []
    caught = {m.get("gt") for m in matches if m.get("verdict") == "match"}
    ids = judge_doc.get("finding_ids") or {}
    # membership counts only for a finding the run's current verdict still calls
    # valid-new: never one now judged invalid, nor beside a match on the key
    still_new = {int(ids[m["finding"]]) for m in matches
                 if m.get("verdict") == "valid-new" and m.get("finding") in ids}
    via_members = {gid for gid, mem in ((key or {}).get("members") or {}).items()
                   if any(r == run_id and int(i) in still_new for r, i in mem)}
    pooled_hits = (caught & pooled_ids) | via_members
    return {"original": recall(original, caught & orig_ids),
            "pooled": recall(original + pooled_gt, (caught & orig_ids) | pooled_hits),
            "pooled_hits": sorted(pooled_hits)}


def rescore_runs(runs: list[dict], runs_root: Path | None = None) -> dict:
    """{run_id: rescore} for every judged review run whose case loads."""
    runs_root = runs_root or config.runs_root()
    out, case_cache = {}, {}
    for r in runs:
        if r.get("kind") != "review" or not r.get("judged"):
            continue
        try:
            jd = json.loads((runs_root / r["run_id"] / "judge.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not jd.get("valid"):
            continue
        cid = r["case_id"]
        if cid not in case_cache:
            try:
                case_cache[cid] = (cases.load(cid), cases.load_pooled(cid))
            except cases.CaseError:
                case_cache[cid] = None
        if case_cache[cid] is None:
            continue
        case, key = case_cache[cid]
        out[r["run_id"]] = rescore(r["run_id"], jd, case, key)
    return out
