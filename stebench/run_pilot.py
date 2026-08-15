"""Pilot orchestrator: generate -> score -> analyse.

Split into stages so a failure never costs a re-spend:

  python run_pilot.py generate   # OpenRouter, ~$0.02, no GPTZero key needed
  python run_pilot.py human      # Wikipedia pre-2020, free
  python run_pilot.py detect     # GPTZero, 48 scans
  python run_pilot.py analyse

`detect` skips anything already scored, so an interrupted run resumes
instead of paying twice.
"""

import json
import sys
import time
from pathlib import Path

from prompts import MODEL, TASKS, CONDITIONS
from stylometry import features

DATA = Path(__file__).parent / "data"
CORPUS = DATA / "corpus.json"
RESULTS = DATA / "pilot_results.json"


def _load(p: Path) -> list:
    return json.loads(p.read_text()) if p.exists() else []


def _save(p: Path, rows: list) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, indent=2))


def stage_generate() -> None:
    from generate import generate

    rows = _load(CORPUS)
    done = {(r["task_id"], r["condition"]) for r in rows if r["kind"] == "ai"}
    total = len(TASKS) * len(CONDITIONS)
    i = 0
    for domain, tid, task in TASKS:
        for cond in CONDITIONS:
            i += 1
            if (tid, cond) in done:
                print(f"[{i:3d}/{total}] skip {tid} {cond}")
                continue
            print(f"[{i:3d}/{total}] gen  {tid} {cond} ... ", end="", flush=True)
            try:
                g = generate(task, cond)
            except Exception as e:  # noqa: BLE001
                print(f"FAILED {type(e).__name__}: {e}")
                continue
            rows.append({
                "kind": "ai", "task_id": tid, "domain": domain, "task": task,
                "condition": cond, "model": MODEL, **g,
            })
            _save(CORPUS, rows)
            print(f"{g['n_words']}w"
                  + ("" if g["in_band"] else " OUT-OF-BAND")
                  + (" (resampled)" if g.get("resampled") else ""))
            time.sleep(0.3)
    print(f"\ncorpus: {sum(1 for r in rows if r['kind']=='ai')} ai texts -> {CORPUS}")


def stage_human() -> None:
    from generate import HUMAN_PAGES, fetch_human

    rows = _load(CORPUS)
    have = {r["task_id"] for r in rows if r["kind"] == "human"}
    for page, domain in HUMAN_PAGES:
        tid = f"H_{page}"
        if tid in have:
            print(f"skip {page}")
            continue
        print(f"wiki {page} ... ", end="", flush=True)
        try:
            txt = fetch_human(page)
        except Exception as e:  # noqa: BLE001
            print(f"FAILED {type(e).__name__}: {e}")
            continue
        if not txt:
            print("no usable pre-2020 text")
            continue
        rows.append({
            "kind": "human", "task_id": tid, "domain": domain,
            "task": f"wikipedia:{page} (rev <2020-01-01)",
            "condition": "HUMAN", "model": "human", "text": txt,
            "n_words": len(txt.split()), "in_band": True,
        })
        _save(CORPUS, rows)
        print(f"{len(txt.split())}w")
        time.sleep(0.4)
    print(f"\nhuman controls: {sum(1 for r in rows if r['kind']=='human')}")


def results_path(backend: str) -> Path:
    return DATA / f"pilot_results_{backend}.json"


def stage_detect(backend: str = "sapling", max_chars: int | None = None) -> None:
    """Score the corpus. Resumable, and bounded by `max_chars` so a free
    trial key's 50k/24h quota can be spent across several days without
    re-paying for anything already scored."""
    from detect import estimate_cost, score

    corpus = _load(CORPUS)
    if not corpus:
        raise SystemExit("no corpus - run `generate` and `human` first")

    out = results_path(backend)
    results = _load(out)
    scored = {(r["task_id"], r["condition"]) for r in results}
    todo = [r for r in corpus if (r["task_id"], r["condition"]) not in scored]

    remaining = len(todo)
    if max_chars is not None:
        budgeted, used = [], 0
        for r in todo:
            if used + len(r["text"]) > max_chars:
                break
            budgeted.append(r)
            used += len(r["text"])
        todo = budgeted

    est = estimate_cost([r["text"] for r in todo], backend)
    chars = sum(len(r["text"]) for r in todo)
    print(f"backend={backend}  to scan={len(todo)}/{remaining}  chars={chars:,}"
          + (f"  est cost=${est:.2f}" if est == est else "  est cost=n/a"))
    if max_chars is not None and len(todo) < remaining:
        print(f"budget capped at {max_chars:,} chars - "
              f"{remaining - len(todo)} texts left for the next run")
    if not todo:
        print("nothing to do")
        return

    for i, row in enumerate(todo, 1):
        print(f"[{i:3d}/{len(todo)}] scan {row['task_id']:28s} {row['condition']:12s} ... ",
              end="", flush=True)
        det = score(row["text"], backend)
        if det["error"]:
            print(f"ERROR {det['error']}")
            # A spent daily quota applies to every remaining text, so stop
            # rather than walking the rest of the corpus into the same wall.
            if str(det["error"]).startswith("QUOTA_EXHAUSTED"):
                print(f"\nquota exhausted - {len(todo) - i + 1} texts left. "
                      f"Re-run in 24h (free tier) or subscribe; already-scored "
                      f"texts are skipped.")
                break
            continue
        results.append({
            **{k: v for k, v in row.items() if k != "text"},
            "backend": backend,
            "ai_prob": det["ai_prob"],
            "predicted_class": det["predicted_class"],
            "class_probabilities": det.get("class_probabilities"),
            "sentence_probs": det.get("sentence_probs"),
            "n_sentences_scored": det["n_sentences_scored"],
            "token_probs": det.get("token_probs"),
            "tokens": det.get("tokens"),
            **features(row["text"]),
        })
        _save(out, results)
        print(f"ai_prob={det['ai_prob']}")
        time.sleep(0.4)
    print(f"\nscored {len(results)} texts -> {out}")


def stage_analyse(backend: str = "sapling") -> None:
    from analyze import report
    report(results_path(backend))


def stage_dryrun() -> None:
    """Everything except the two paid/network stages - proves the pipeline."""
    from prompts import build_prompt
    sample = build_prompt(TASKS[0][2], "C1_ste_full")
    print(f"prompt chars: C0={len(build_prompt(TASKS[0][2],'C0_baseline'))} "
          f"C1={len(sample)} "
          f"C3={len(build_prompt(TASKS[0][2],'C3_humanize'))}")
    demo = (
        "Remove the rear wheel. Put the chain on the smallest sprocket. "
        "Use a chain tool to push out the pin. Do not push the pin fully out of "
        "the outer plate, because you cannot put it back. Pull the chain apart.\n\n"
        "Measure the new chain against the old chain. Make sure that the two "
        "chains have the same number of links. Remove the extra links."
    )
    print(json.dumps(features(demo), indent=2))


STAGES = {
    "generate": stage_generate, "human": stage_human, "detect": stage_detect,
    "analyse": stage_analyse, "analyze": stage_analyse, "dryrun": stage_dryrun,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in STAGES:
        raise SystemExit(
            f"usage: python run_pilot.py [{'|'.join(STAGES)}] [backend]\n"
            f"       backend defaults to 'sapling' (also: 'gptzero')"
        )
    stage = STAGES[sys.argv[1]]
    if sys.argv[1] == "detect":
        backend = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].isdigit() else "sapling"
        budget = next((int(a) for a in sys.argv[2:] if a.isdigit()), None)
        stage(backend, budget)
    elif sys.argv[1] in ("analyse", "analyze"):
        stage(sys.argv[2] if len(sys.argv) > 2 else "sapling")
    else:
        stage()
