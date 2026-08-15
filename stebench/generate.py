"""Corpus generation through OpenRouter, plus the human control set.

Every model goes through one code path with identical sampling params.
That is the reason Claude is called here rather than through Claude Code
subagents: a subagent carries its own system prompt and tool-use framing,
which would be a confound baked into some cells and not others.
"""

import os
import re
import time
from pathlib import Path

import httpx

from prompts import MODEL, TEMPERATURE, WORDS_MAX, WORDS_MIN, build_prompt

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DATA = Path(__file__).parent / "data"


def _key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                    break
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not found in environment or .env")
    return key


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))


def generate(task: str, condition: str, model: str = MODEL, attempt: int = 0) -> dict:
    """One generation. Resamples once if the length lands outside the band."""
    payload = {
        "model": model,
        "temperature": TEMPERATURE,
        # Luna is a reasoning model: it bills reasoning against max_tokens and
        # returns EMPTY content if the budget is spent thinking. At 1200 this
        # silently produced two zero-word texts. Headroom is not optional.
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": build_prompt(task, condition)}],
    }
    with httpx.Client(timeout=180) as c:
        r = c.post(
            OPENROUTER,
            headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        body = r.json()

    text = (body["choices"][0]["message"].get("content") or "").strip()
    usage = body.get("usage", {})
    n = word_count(text)

    # STE compresses systematically, so one resample is not enough to pull the
    # C1 cells into the band - a single retry of a biased process is still
    # biased. Retry up to 3x, and never accept an empty completion.
    if (n == 0 or not (WORDS_MIN <= n <= WORDS_MAX)) and attempt < 3:
        time.sleep(0.5)
        retry = generate(task, condition, model, attempt + 1)
        retry["resampled"] = True
        retry["attempts"] = attempt + 2
        return retry

    return {
        "text": text,
        "model": model,
        "condition": condition,
        "n_words": n,
        "in_band": WORDS_MIN <= n <= WORDS_MAX,
        "resampled": False,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost": usage.get("cost"),
    }


# --- human control set --------------------------------------------------
#
# Pre-2020 Wikipedia revisions. The cutoff is the cleanest human guarantee
# available at this budget: the text existed before instruction-tuned LLMs
# were writing prose at scale.

HUMAN_PAGES = [
    ("Bicycle_chain", "D1_procedural"),
    ("Disc_brake", "D1_procedural"),
    ("Tap_(valve)", "D1_procedural"),
    ("Jump_start_(vehicle)", "D1_procedural"),
    ("Heat_pump", "D2_expository"),
    ("Differential_(mechanical_device)", "D2_expository"),
    ("Cabin_pressurization", "D2_expository"),
    ("Error_correction_code", "D2_expository"),
    ("Laptop", "D3_essay"),
    ("Free_public_transport", "D3_essay"),
    ("Distance_education", "D3_essay"),
    ("Basic_income", "D3_essay"),
]

CUTOFF = "2019-12-31T00:00:00Z"


def fetch_human(page: str, target_words: int = 380) -> str | None:
    """Extract plain text from the last pre-2020 revision of a Wikipedia page."""
    api = "https://en.wikipedia.org/w/api.php"
    # Wikimedia's UA policy rejects generic agents with 403 - it wants a
    # descriptive name plus contact info.
    ua = "stebench-research/0.1 (https://github.com/johnw; ste-detector-study) python-httpx"
    with httpx.Client(timeout=60, headers={"User-Agent": ua}) as c:
        r = c.get(api, params={
            "action": "query", "prop": "revisions", "titles": page,
            "rvlimit": 1, "rvstart": CUTOFF, "rvdir": "older",
            "rvprop": "content", "rvslots": "main", "format": "json",
        })
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for p in pages.values():
            revs = p.get("revisions")
            if not revs:
                return None
            wikitext = revs[0]["slots"]["main"]["*"]
            return _clean_wikitext(wikitext, target_words)
    return None


def _clean_wikitext(src: str, target_words: int) -> str | None:
    t = src
    for _ in range(6):  # nested templates and tables
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
        t = re.sub(r"\{\|[^{}]*?\|\}", "", t, flags=re.S)
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"<ref.*?</ref>", "", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\[\[File:.*?\]\]", "", t, flags=re.S | re.I)
    t = re.sub(r"\[\[Image:.*?\]\]", "", t, flags=re.S | re.I)
    t = re.sub(r"\[\[Category:.*?\]\]", "", t, flags=re.I)
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"'''?", "", t)
    t = re.sub(r"^\s*[*#:;].*$", "", t, flags=re.M)   # list markup
    t = re.sub(r"^\s*=+.*?=+\s*$", "", t, flags=re.M)  # headings

    paras, total = [], 0
    for p in (x.strip() for x in t.split("\n\n")):
        if len(p) < 200 or p.startswith("|") or "http" in p[:40]:
            continue
        paras.append(p)
        total += len(re.findall(r"[A-Za-z][A-Za-z'-]*", p))
        if total >= target_words:
            break
    out = "\n\n".join(paras)

    # Trim to the same length band the AI texts are held to. Detector scores
    # move with length, so an untrimmed human set would confound the one
    # comparison the control exists to support.
    if word_count(out) > WORDS_MAX:
        kept, n = [], 0
        for para in out.split("\n\n"):
            sents = re.split(r"(?<=[.!?])\s+", para)
            buf = []
            for s in sents:
                sn = word_count(s)
                if n + sn > WORDS_MAX:
                    break
                buf.append(s)
                n += sn
            if buf:
                kept.append(" ".join(buf))
            if n >= WORDS_MAX - 40:
                break
        out = "\n\n".join(kept)

    return out if word_count(out) >= WORDS_MIN else None
