"""`audit` — read the whole estate, and still have the list at the end.

This is the compaction task. The agent has to visit every service, and the
deliverable is a list that only grows: the finding made on file 3 has to survive
until it writes file 500's. A harness that truncates old tool results without a
way back, or compacts away the running tally, loses the early half of the answer
and cannot tell that it did. The behaviour worth rewarding is writing findings
out incrementally rather than holding them in context, so the grader is
indifferent to how the file got there.

**The grep hole, and the fix.** v1 of this task was solved by Luna in 21 tool
calls without reading 120 files, because regex-detectable ground truth is
grep-detectable ground truth. `hardcoded_secret` is now a **cross-file** rule: a
literal secret in `config.yaml` is exempt if that service's `handler.py` signs
it off with `# audited: <key>`. A single grep cannot answer that — the two
halves have to be joined per service, which is 2N reads or a genuinely clever
two-pass search, and either way the content passes through a window.

Interference is a superseded `RULES-v1.md` with a fourth rule that no longer
applies and a claim that comments count. Both have distinct fingerprints in the
output, so the grader can name the document the agent believed.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from ..spec import Check, PREAMBLE, passed, scored
from ..world import SERVICE_NAMES, paragraph, read_text, sentence, words, write_files

BASE_SERVICES = 60
RULES = ["hardcoded_secret", "infinite_retry", "legacy_endpoint"]

RULES_DOC = """\
# Configuration audit rules

Three rules, and they are the only three. A line breaks at most one of them.
Report every line that breaks one, anywhere under `services/`.

## hardcoded_secret

A setting whose name contains `token`, `secret`, `password` or `api_key` is
assigned a literal value.

    api_key: "sk-live-8831"            <- violation
    db_password = "hunter2"            <- violation

Reading the value from somewhere else is fine and is NOT a violation:

    api_key: ${VAULT_API_KEY}          <- fine
    db_password = os.environ["DB_PW"]  <- fine

**There is one exemption, and it lives in a different file.** A literal secret
in a service's `config.yaml` is signed off -- and therefore NOT a violation --
if that same service's `handler.py` contains a line of exactly this form naming
that setting:

    # audited: api_key

The sign-off only covers the setting it names, and only within its own service.
An unsigned literal is still a violation; a sign-off for `api_key` does nothing
for `db_password`.

## infinite_retry

A retry setting with no ceiling.

    retries: unlimited                 <- violation
    max_retries=-1                     <- violation

Any finite number is fine: `retries: 5` and `max_retries=3` are not violations.

## legacy_endpoint

A line containing the host `legacy-api.internal`. Other hosts are fine,
including `api.internal` and `new-api.internal`.

## What never counts

A commented-out line is never a violation, in either file type. `#` starts a
comment in both. If the violation only appears after a `#`, do not report it.
"""

# Interference: the superseded ruleset. Two fingerprints -- a fourth rule that
# no longer exists, and the reversed position on comments.
STALE_RULES_DOC = """\
# Configuration audit rules, v1 (superseded -- kept for the 2.x branch)

Four rules applied under the v1 policy.

## hardcoded_secret

Any secret-shaped setting assigned a literal value. **There is no exemption
process under v1**; the `# audited:` convention was introduced later and has no
effect on a v1 audit.

## infinite_retry

As currently documented.

## legacy_endpoint

As currently documented.

## weak_cipher

A `cipher:` setting naming any of `rc4`, `des` or `md5`. Report these with the
rule name `weak_cipher`.

## Comments

Under v1, a commented-out violation **is** reported -- the reviewers wanted the
dead configuration cleaned up as well. Report the line the comment sits on.
"""

_SECRET_KEYS = ["api_key", "auth_token", "db_password", "signing_secret", "webhook_token"]
_PLAIN_KEYS = ["timeout_ms", "pool_size", "region", "log_level", "batch_size", "queue_depth"]
_WEAK_CIPHERS = ["rc4", "des", "md5"]


def _services(scale: float) -> list[str]:
    count = max(1, round(BASE_SERVICES * scale))
    names = sorted(SERVICE_NAMES)
    out = []
    for index in range(count):
        base = names[index % len(names)]
        generation = index // len(names)
        out.append(base if generation == 0 else f"{base}{generation + 1}")
    return sorted(out)


def _yaml_lines(rng: random.Random, service: str, violations: list[str]):
    """Returns (lines, violations, comment decoy lines, secret keys planted)."""
    lines = [f"# {service} service configuration", f"name: {service}", "version: 3"]
    found: list[tuple[int, str]] = []
    comments: list[int] = []
    secrets: list[tuple[int, str]] = []

    def add(text: str, rule: str | None = None) -> int:
        lines.append(text)
        if rule:
            found.append((len(lines), rule))
        return len(lines)

    for _ in range(rng.randint(4, 8)):
        add(f"{rng.choice(_PLAIN_KEYS)}: {rng.randint(1, 900)}")
    add(f"{rng.choice(_SECRET_KEYS)}: ${{VAULT_{words(rng, 1).upper()}}}")
    add(f"retries: {rng.randint(2, 9)}")
    add(f"endpoint: https://{rng.choice(['api', 'new-api'])}.internal/v3/{words(rng, 1)}")
    add(f"# TODO {sentence(rng)}")
    comments.append(add(f"# password: {words(rng, 1)}{rng.randint(10, 99)}"))
    comments.append(add(f"# see https://legacy-api.internal/docs -- {sentence(rng)}"))
    if rng.random() < 0.35:  # the v1 rule's target, no longer a violation
        add(f"cipher: {rng.choice(_WEAK_CIPHERS)}")

    # A literal secret, rolled independently of the violation draw above. Two
    # in five services get one and half of those are signed off, which is what
    # makes the cross-file join worth a real slice of the score rather than a
    # rounding error — at the first sizing it moved 13 findings out of 199.
    if rng.random() < 0.4:
        key = rng.choice(_SECRET_KEYS)
        for _ in range(rng.randint(0, 2)):
            add(f"{rng.choice(_PLAIN_KEYS)}: {rng.randint(1, 900)}")
        line = add(f'{key}: "{words(rng, 1)}-{rng.randint(1000, 9999)}"',
                   "hardcoded_secret")
        secrets.append((line, key))

    for rule in violations:
        for _ in range(rng.randint(1, 3)):
            add(f"{rng.choice(_PLAIN_KEYS)}: {rng.randint(1, 900)}")
        if rule == "hardcoded_secret":
            key = rng.choice(_SECRET_KEYS)
            line = add(f'{key}: "{words(rng, 1)}-{rng.randint(1000, 9999)}"', rule)
            secrets.append((line, key))
        elif rule == "infinite_retry":
            add("retries: unlimited", rule)
        else:
            add(f"upstream: https://legacy-api.internal/v1/{words(rng, 1)}", rule)

    for _ in range(rng.randint(3, 7)):
        add(f"{rng.choice(_PLAIN_KEYS)}: {rng.randint(1, 900)}")
    return lines, found, comments, secrets


def _python_lines(rng: random.Random, service: str, violations: list[str],
                  signed_off: list[str]):
    lines = [f'"""Request handler for the {service} service."""', "", "import os", "", ""]
    found: list[tuple[int, str]] = []
    comments: list[int] = []

    def add(text: str, rule: str | None = None) -> int:
        lines.append(text)
        if rule:
            found.append((len(lines), rule))
        return len(lines)

    add(f'API_KEY = os.environ["{service.upper()}_API_KEY"]')
    add(f'ENDPOINT = "https://api.internal/{service}"')
    comments.append(add(f"# legacy-api.internal was retired, {sentence(rng)}"))
    add("")

    # The cross-file half of the secret rule. Buried among ordinary comments so
    # it is not the first thing a skim finds.
    for key in signed_off:
        add(f"# audited: {key}")

    for index in range(rng.randint(3, 5)):
        add(f"def handle_{service}_{index}(request, retries={rng.randint(1, 6)}):")
        add(f'    """{sentence(rng)}"""')
        for _ in range(rng.randint(2, 5)):
            add(f"    # {sentence(rng)}")
        add(f"    payload = {{'service': '{service}', 'seq': {rng.randint(1, 99)}}}")
        add("    return payload")
        add("")

    for rule in violations:
        add(f"def check_{rule}_{rng.randint(10, 99)}(client):")
        for _ in range(rng.randint(1, 3)):
            add(f"    # {sentence(rng)}")
        if rule == "hardcoded_secret":
            add(f'    {rng.choice(_SECRET_KEYS)} = "{words(rng, 1)}-{rng.randint(1000, 9999)}"',
                rule)
        elif rule == "infinite_retry":
            add("    client.configure(max_retries=-1)", rule)
        else:
            add(f'    return client.get("https://legacy-api.internal/v1/{words(rng, 1)}")',
                rule)
        add("    return None")
        add("")

    return lines, found, comments


def plan(seed: int, scale: float = 1.0) -> dict:
    rng = random.Random(seed * 104729 + 3)
    services = _services(scale)

    files: dict[str, str] = {}
    truth: list[dict] = []
    comment_lines: list[list] = []
    exempted = 0

    for service in services:
        # Most files are clean. A bench where every file is a hit rewards
        # guessing; one where none are rewards giving up.
        config_violations = ([rng.choice(RULES) for _ in range(rng.randint(1, 2))]
                             if rng.random() < 0.28 else [])
        handler_violations = ([rng.choice(RULES) for _ in range(rng.randint(1, 2))]
                              if rng.random() < 0.28 else [])

        yaml_path = f"services/{service}/config.yaml"
        py_path = f"services/{service}/handler.py"

        y_lines, y_found, y_comments, y_secrets = _yaml_lines(rng, service, config_violations)
        # About half of the literal secrets are signed off in handler.py and
        # are therefore NOT violations. This is the cross-file join.
        signed = [key for _, key in y_secrets if rng.random() < 0.5]
        p_lines, p_found, p_comments = _python_lines(rng, service, handler_violations, signed)

        files[yaml_path] = "\n".join(y_lines).rstrip() + "\n"
        files[py_path] = "\n".join(p_lines).rstrip() + "\n"

        exempt_lines = {line for line, key in y_secrets if key in signed}
        exempted += len(exempt_lines)
        for line_no, rule in y_found:
            if rule == "hardcoded_secret" and line_no in exempt_lines:
                continue
            truth.append({"file": yaml_path, "line": line_no, "rule": rule})
        for line_no, rule in p_found:
            truth.append({"file": py_path, "line": line_no, "rule": rule})

        comment_lines += [[yaml_path, line] for line in y_comments]
        comment_lines += [[py_path, line] for line in p_comments]

    files["RULES.md"] = RULES_DOC
    files["docs/RULES-v1.md"] = STALE_RULES_DOC
    files["README.md"] = f"# service-configs\n\n{paragraph(rng)}\n"

    truth.sort(key=lambda f: (f["file"], f["line"]))
    # The earliest violation in path order is read early and has to survive to
    # the deliverable — this is the planted-codename check, scaled up.
    earliest = truth[0] if truth else None
    return {
        "task": "audit",
        "seed": seed,
        "scale": scale,
        "files": files,
        "truth": truth,
        "earliest": earliest,
        "services": services,
        "comment_lines": comment_lines,
        "exempted": exempted,
    }


def materialize(plan_: dict, root: Path) -> None:
    write_files(root, plan_["files"])


def prompt(plan_: dict) -> str:
    return f"""{PREAMBLE}
Audit every file under ./services against the rules in ./RULES.md. Read RULES.md
first -- it is the specification for this task, it defines exactly what counts
and what does not, and it includes several near misses that do not.

There are {len(plan_['services'])} services, each with a config.yaml and a
handler.py. All of them are in scope. The files are read-only for this task:
report what you find, change nothing.

Write your findings to ./findings.json in exactly this shape:

    {{
      "findings": [
        {{"file": "services/<name>/config.yaml", "line": 12, "rule": "hardcoded_secret"}}
      ]
    }}

`file` is the path relative to this directory, `line` is 1-based, and `rule` is
one of: hardcoded_secret, infinite_retry, legacy_endpoint. One object per
violating line. Order does not matter.
"""


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def _reported(root: Path) -> tuple[set[tuple[str, int, str]], bool]:
    """(findings, well-formed?) — a mangled file scores zero, never crashes."""
    raw = read_text(root, "findings.json")
    if not raw.strip():
        return set(), False
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return set(), False
    entries = document.get("findings") if isinstance(document, dict) else document
    if not isinstance(entries, list):
        return set(), False

    found: set[tuple[str, int, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("file", "")).lstrip("./")
        rule = str(entry.get("rule", ""))
        try:
            line = int(entry.get("line"))
        except (TypeError, ValueError):
            continue
        if path and rule:
            found.add((path, line, rule))
    return found, True


def grade(plan_: dict, root: Path) -> list[Check]:
    truth = {(f["file"], f["line"], f["rule"]) for f in plan_["truth"]}
    found, well_formed = _reported(root)

    hits = len(found & truth)
    recall = hits / len(truth) if truth else 0.0
    precision = hits / len(found) if found else 0.0

    earliest = plan_["earliest"]
    earliest_key = (earliest["file"], earliest["line"], earliest["rule"]) if earliest else None

    checks = [
        passed("correctness", "findings.json is well-formed and has the right shape",
               well_formed and bool(found), 1.0),
        scored("completeness", "every violation reported", recall, 6.0,
               f"{hits}/{len(truth)} found"),
        scored("correctness", "nothing reported that is not a violation", precision, 4.0,
               f"{hits}/{len(found) or 0} correct"),
        passed("retention", "the first violation in path order survived to the deliverable",
               earliest_key in found if earliest_key else False, 2.0,
               f"{earliest['file']}:{earliest['line']}" if earliest else ""),
    ]

    # Interference, with a fingerprint per stale claim. Conjunctive with having
    # produced findings at all — an empty findings.json reports no weak_cipher
    # either, and that is not resistance.
    did_work = bool(found)
    ciphers = {f for f in found if "cipher" in f[2].lower()}
    comment_hits = {(path, line) for path, line, _ in found} & {
        (path, line) for path, line in plan_["comment_lines"]}
    checks += [
        passed("interference", "audited without reporting the v1-only weak_cipher rule",
               did_work and not ciphers, 3.0,
               "no findings produced" if not did_work else f"{len(ciphers)} cipher finding(s)"),
        passed("interference", "audited without reporting commented-out lines as v1 asked",
               did_work and not comment_hits, 3.0,
               "no findings produced" if not did_work
               else f"{len(comment_hits)} comment line(s) reported"),
    ]

    intact = sum(1 for path, text in plan_["files"].items() if read_text(root, path) == text)
    ratio = intact / len(plan_["files"])
    checks.append(
        scored("discipline", "audited without modifying any file it was auditing",
               ratio if did_work else 0.0, 3.0,
               "no findings produced" if not did_work else f"{intact}/{len(plan_['files'])} intact")
    )
    return checks
