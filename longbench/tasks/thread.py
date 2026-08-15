"""`thread` — forty sequential reads, one rule from the start, two broken files.

Where `audit` is wide, this one is deep: the answer cannot be assembled without
walking a chain, so the run is necessarily long and the interesting facts are
spread across its whole length. Three separate things have to survive to the
final file:

  the cohort rule       stated once, in the prompt, and never repeated
  the manifest salt     read in the first thirty seconds, needed in the last act
  two damaged shards    recoverable from a `.bak` the manifest mentions in passing

The damaged shards are the recovery test, and they are the reason the task has a
category `audit` does not. An agent that treats a JSONDecodeError as a dead end
loses two of twelve fragments and produces a confidently wrong answer; the
manifest tells it what to do, one clause into a field it read at step one.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from ..spec import Check, PREAMBLE, passed, scored
from ..world import paragraph, read_text, sentence, words, write_files

# Pools, not answers. The first cut assigned fragments in alphabetical order,
# which made the code `ABCDEFGHJKLM` on every seed — a bench whose answer can be
# guessed without reading anything. Twelve are now sampled from this pool in
# random order per seed. The two pools stay disjoint because the discipline
# check is "no out-of-cohort character in the code".
ALPHA_POOL = "ABCDEFGHJKLMNPQRSTUVWXYZ"
BETA_FRAGMENTS = "nopqrstuvwxyz23456789#@$%"
SHARD_COUNT = 40
DAMAGED = 2


def plan(seed: int, scale: float = 1.0) -> dict:
    rng = random.Random(seed * 15485863 + 17)

    # The in-scope twelve stay fixed however far this scales — the answer has a
    # fixed length, and everything scale adds is decoys and payload to wade
    # through, which is the load being applied.
    shard_count = max(14, round(SHARD_COUNT * scale))
    payload_paragraphs = max(3, round(3 * scale))
    ids = [f"sh-{index:02d}" for index in range(shard_count)]
    rng.shuffle(ids)
    alpha_ids = sorted(ids[:12])
    beta_ids = sorted(ids[12:])

    # Sequence numbers are shuffled and non-contiguous, so file order and id
    # order are both wrong answers.
    seqs = rng.sample(range(10, 90), 12)
    ordered = sorted(zip(seqs, alpha_ids))
    fragments = rng.sample(ALPHA_POOL, 12)
    code = "".join(fragments)
    alpha = {
        shard_id: {"seq": seq, "fragment": fragments[position]}
        for position, (seq, shard_id) in enumerate(ordered)
    }

    beta = {
        shard_id: {"seq": rng.randint(10, 90), "fragment": rng.choice(BETA_FRAGMENTS)}
        for shard_id in beta_ids
    }

    damaged = sorted(rng.sample(alpha_ids, DAMAGED))
    salt = f"{words(rng, 1)}{rng.randint(100, 999)}"

    files: dict[str, str] = {}
    for shard_id in sorted(ids):
        info = alpha.get(shard_id) or beta[shard_id]
        cohort = "alpha" if shard_id in alpha else "beta"
        body = {
            "id": shard_id,
            "cohort": cohort,
            "seq": info["seq"],
            "fragment": info["fragment"],
            "payload": " ".join(paragraph(rng, 6) for _ in range(payload_paragraphs)),
        }
        text = json.dumps(body, indent=2) + "\n"
        if shard_id in damaged:
            # Truncated mid-object: a real JSONDecodeError, not a subtle one.
            files[f"shards/{shard_id}.json"] = text[: len(text) // 2]
            files[f"shards/{shard_id}.json.bak"] = text
        else:
            files[f"shards/{shard_id}.json"] = text

    manifest = {
        "generated": "2026-08-15",
        "salt": salt,
        "notes": (
            "Shard payloads are archival and can be ignored. A shard that fails "
            "to parse was truncated in transit; the complete copy is the .bak "
            "file beside it and is safe to use verbatim."
        ),
        "shards": [{"id": shard_id, "path": f"shards/{shard_id}.json"}
                   for shard_id in sorted(ids)],
    }
    files["manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    files["README.md"] = f"# shard-archive\n\n{sentence(rng)}\n"

    return {
        "task": "thread",
        "seed": seed,
        "scale": scale,
        "files": files,
        "salt": salt,
        "code": code,
        "answer": f"{code}-{salt}",
        "order": [shard_id for _, shard_id in ordered],
        "damaged": damaged,
        "damaged_positions": [
            [shard_id for _, shard_id in ordered].index(shard_id) for shard_id in damaged
        ],
    }


def materialize(plan_: dict, root: Path) -> None:
    write_files(root, plan_["files"])


def prompt(plan_: dict) -> str:
    return f"""{PREAMBLE}
Only shards whose cohort is "alpha" are in scope for this task. Shards in any
other cohort are archival and must not contribute to the answer.

./manifest.json lists every shard in ./shards. Each shard is a JSON object with
an id, a cohort, a sequence number, a one-character fragment, and a payload you
can ignore.

Assemble the recovery code: take every in-scope shard, order them by their
sequence number ascending, and concatenate their fragments. Twelve shards are in
scope, so the code is twelve characters.

Then write two files:

- ./answer.txt containing exactly the code, a hyphen, and the manifest's salt
  value -- `<code>-<salt>` -- and nothing else.
- ./trace.md listing the ids of the shards you used, in the same order you used
  them, one per line and nothing else.
"""


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def grade(plan_: dict, root: Path) -> list[Check]:
    answer = read_text(root, "answer.txt").strip()
    code_part = answer.split("-")[0] if "-" in answer else answer
    expected_code = plan_["code"]

    positions = sum(
        1 for index, char in enumerate(expected_code)
        if index < len(code_part) and code_part[index] == char
    )
    code_ratio = positions / len(expected_code)

    # Recovery is scored on the two characters that only exist in a .bak file.
    recovered = sum(
        1 for index in plan_["damaged_positions"]
        if index < len(code_part) and code_part[index] == expected_code[index]
    )

    beta_chars = sum(1 for char in code_part if char in BETA_FRAGMENTS)
    # "Assembled a code" has to mean something, or prose scores. Discipline is
    # the fraction of an assembled code that is in-cohort, and arbitrary text
    # is not an assembled code — otherwise `answer.txt` full of English earns
    # partial credit for the letters that happen not to be beta fragments.
    plausible = bool(code_part) and all(
        char in ALPHA_POOL or char in BETA_FRAGMENTS for char in code_part
    )

    reported_ids = re.findall(r"sh-\d{2}", read_text(root, "trace.md"))
    expected_order = plan_["order"]
    in_order = sum(
        1 for index, shard_id in enumerate(expected_order)
        if index < len(reported_ids) and reported_ids[index] == shard_id
    )

    return [
        passed("correctness", "answer.txt is exactly <code>-<salt>",
               answer == plan_["answer"], 3.0,
               f"got {answer[:40]!r}" if answer != plan_["answer"] else ""),
        scored("correctness", "the assembled code is right, character by character",
               code_ratio, 4.0, f"{positions}/{len(expected_code)} characters"),
        passed("retention", "the manifest salt reached the final file",
               answer.endswith(f"-{plan_['salt']}"), 3.0, f"expected salt {plan_['salt']}"),
        scored("recovery", "both truncated shards were recovered from their .bak",
               recovered / DAMAGED, 3.0,
               f"{recovered}/{DAMAGED} ({', '.join(plan_['damaged'])})"),
        scored("discipline", "assembled a code with no out-of-cohort fragment in it",
               (1.0 - beta_chars / max(len(code_part), 1)) if plausible else 0.0, 2.0,
               "nothing assembled" if not plausible else f"{beta_chars} foreign character(s)"),
        scored("correctness", "trace.md lists the twelve shards in the order used",
               in_order / len(expected_order), 3.0,
               f"{in_order}/{len(expected_order)} positions"),
    ]
