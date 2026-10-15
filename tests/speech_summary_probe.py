"""Paid probe: cost and latency of spoken summaries on Haiku 5.5 (V8, voice plan §3.8).

The owner approved ONE measurement, capped at $0.10 in total. This makes real
API calls through `jarvis.v2.speech_summary.summarize` -- the same code path as
`POST /speech/summary` -- on SYNTHETIC reply texts only (invented prose about a
made-up project; nothing from a real chat).

It refuses to run without `--yes-spend`, stops before the cap
(`--cap`, default 0.09 USD), makes at most 30 calls, and never prints the API
key (it reads `OPENROUTER_API_KEY` from the environment through `config`; it
reads no `.env` file of its own and prints no environment).

Run (needs OPENROUTER_API_KEY in the environment):
    PYTHONPATH=. .venv/bin/python tests/speech_summary_probe.py --yes-spend [--calls 24] [--cap 0.09] [--out FILE]

Output: one line per call (characters in, tokens in/out, cost, seconds), then
the totals, p50/p95 latency and the model id. `--out` writes the same numbers
as JSON (no texts).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time

from jarvis import config
from jarvis.v2 import speech_summary as S

# Hand-written fragments about an invented project, "Marlin", so no real
# conversation is ever sent. They are shuffled into replies of the sizes below.
OPENERS = [
    "I looked into why the nightly export for Marlin keeps timing out.",
    "The migration finished, but two of the checks did not pass.",
    "Here is what I found in the invoice parser after the refactor.",
    "The build is green again, though the way we got there needs a decision from you.",
    "I went through the retry logic in the sync worker end to end.",
]
BODY = [
    "The cache layer was returning stale entries whenever two workers wrote the same key within a few milliseconds.",
    "I changed the lock so that the second writer waits for the first instead of overwriting it silently.",
    "That fixed the flaky test, but it also made the slowest request about forty milliseconds slower on average.",
    "There is a second, older code path in the exporter that does not go through the lock at all.",
    "I left that one alone because three other modules import it and I could not tell which of them rely on the current behaviour.",
    "The log files showed the same warning about a missing index on the orders table every few minutes.",
    "Adding the index takes about ninety seconds on the staging copy and no downtime, but it does need a migration.",
    "Two of the integration tests depend on the order of results, which the database does not promise.",
    "I rewrote them to sort explicitly, and they now pass ten times in a row.",
    "The documentation for the configuration file was out of date, so I updated the three sections that were wrong.",
    "One option is to ship the fix now and track the slower path as a separate ticket.",
    "The other option is to hold the release until the older code path is migrated as well, which is a day of work.",
    "None of this touches the public interface, so clients should not notice anything except the speed difference.",
    "I also noticed that the error messages from the exporter never say which customer failed, which makes support harder.",
    "A small change would add the customer id to the message without changing the log format anywhere else.",
]
CLOSERS = [
    "My recommendation is to ship the lock fix and the index now, and to schedule the older path for next week.",
    "Tell me which option you prefer and I will carry on from there.",
    "If you would rather I look at the older path first, say so and I will stop here.",
]
# (target characters, how many calls)
PLAN = [(800, 8), (1800, 8), (3600, 6), (8000, 2)]


def synthetic(chars: int, seed: int) -> str:
    rng = random.Random(seed)
    parts = [rng.choice(OPENERS)]
    while sum(len(p) + 1 for p in parts) < chars - 140:
        parts.append(rng.choice(BODY))
    parts.append(rng.choice(CLOSERS))
    return " ".join(parts)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes-spend", action="store_true", help="really call the API (the owner-approved probe)")
    ap.add_argument("--calls", type=int, default=24)
    ap.add_argument("--cap", type=float, default=0.09, help="stop before total spend reaches this (USD)")
    ap.add_argument("--out", help="write the numbers (no texts) as JSON here")
    args = ap.parse_args()
    if not args.yes_spend:
        print("This makes paid calls on", config.TIERS["speech_summary"], "- pass --yes-spend to run it.")
        return 2
    if not 1 <= args.calls <= 30 or not 0 < args.cap <= 0.10:
        print("--calls must be 1..30 and --cap at most 0.10")
        return 2
    if not __import__("os").environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set in the environment.")
        return 2

    texts: list[str] = []
    for chars, n in PLAN:
        texts += [synthetic(chars, seed=chars * 100 + i) for i in range(n)]
    texts = texts[: args.calls]
    random.Random(7).shuffle(texts)

    rows = []
    spent = 0.0
    print(f"model {S.candidates()[0]}, {len(texts)} synthetic replies, cap ${args.cap:.2f}")
    for i, text in enumerate(texts, 1):
        if spent >= args.cap - 0.01:
            print(f"stopping before call {i}: spent ${spent:.5f} is near the cap")
            break
        try:
            out = S.summarize(text, deadline_s=20.0)
        except S.SummaryError as exc:
            # The reason code and numbers only (HTTP status, finish reason, token counts): never text.
            print(f"{i:2d}  {len(text):5d} chars  FAILED {exc.status} {exc.message} "
                  f"[reason={exc.reason or '-'} {json.dumps(exc.detail, sort_keys=True)}]")
            rows.append({"chars": len(text), "failed": exc.status, "reason": exc.reason, "detail": exc.detail})
            continue
        cost = out.cost_usd
        estimated = False
        if not cost:   # usage not reported: price it from the CLAUDE.md list ($0.10 in, $0.50 out per M)
            cost = out.prompt_tokens * 0.10e-6 + out.completion_tokens * 0.50e-6
            estimated = True
        spent += cost
        rows.append({"chars": len(text), "model": out.model, "prompt_tokens": out.prompt_tokens,
                     "completion_tokens": out.completion_tokens, "cost_usd": cost, "estimated": estimated,
                     "seconds": round(out.seconds, 3), "summary_chars": len(out.text),
                     "calls": out.calls, "retries": out.retries})
        print(f"{i:2d}  {len(text):5d} chars  {out.prompt_tokens:5d} in  {out.completion_tokens:4d} out  "
              f"${cost:.6f}{'~' if estimated else ' '} {out.seconds:5.2f}s  "
              f"{'retried ' + str(out.retries) + 'x  ' if out.retries else ''}-> {out.text[:90]}")

    done = [r for r in rows if "failed" not in r]
    if not done:
        print("no successful calls")
        print(json.dumps([{k: r[k] for k in ("chars", "reason", "detail")} for r in rows], indent=2))
        return 1
    secs = [r["seconds"] for r in done]
    total = sum(r["cost_usd"] for r in done)
    summary = {
        "date": time.strftime("%Y-%m-%d"), "model": done[0]["model"], "calls": len(rows),
        "failed": len(rows) - len(done), "total_usd": round(total, 6),
        "mean_usd": round(total / len(done), 6),
        "prompt_tokens_mean": round(statistics.mean(r["prompt_tokens"] for r in done)),
        "completion_tokens_mean": round(statistics.mean(r["completion_tokens"] for r in done)),
        "p50_s": round(pct(secs, 0.5), 2), "p95_s": round(pct(secs, 0.95), 2),
        "max_s": max(secs), "any_cost_estimated": any(r["estimated"] for r in done),
        "retried_calls": sum(1 for r in done if r["retries"]),
        "failure_reasons": {k: sum(1 for r in rows if r.get("reason") == k)
                            for k in sorted({r["reason"] for r in rows if "failed" in r})},
    }
    print("\n" + json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": summary, "calls": rows}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
