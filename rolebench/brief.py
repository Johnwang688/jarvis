"""The fixed prompts. Every model gets the same text (plan 3.3); only the
case-specific fields change."""

from __future__ import annotations

from . import config

REVIEW = """\
You are reviewing a pull request in an isolated copy of the repository at {snap}. \
HEAD is the change under review: run `git diff HEAD~1` to see it. There is no remote, no \
network and no GitHub access. Do not push, fetch or install anything, and do not read \
outside this directory (the one exception is the Python interpreter at {python}).

## What the change claims to do
{brief}

## Focus areas
{focus}
CLAUDE.md at the repository root holds the project's rules; read the sections that touch \
the files in the diff.

## What you must do
1. Read the diff and the code around it.
2. Run the relevant suites under a memory cap, from the repository root:
   systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 env PYTHONPATH={snap} {python} <suite>
   Suites for this change:
{suites}
   (A `.ts` suite runs from hud/ with `npx vitest run <path relative to hud>`.)
3. Try at least one mutant of your own: change the code under test the way a real bug \
could, re-run the suite and report whether it caught the change. Restore the tree \
afterwards (`git checkout -- . && git clean -fd`).
4. Report findings ranked HIGH, MED, LOW or NIT, each with file:line and a concrete \
failure scenario. HIGH: breaks a safety boundary or data, or fails in normal use. MED: a \
real bug or a test that cannot catch the thing it claims to pin. LOW: a small defect, a \
gap or a stale comment. NIT: taste. If nothing is wrong, say so and report no findings. \
Do not pad the list: a finding you cannot back with a scenario is not a finding.

## Output
Write the review in prose first. The very last thing in your reply must be one fenced \
block, exactly this shape (`line` may be null; use [] for no findings):

```json
{{"findings": [{{"severity": "MED", "file": "path/to/file.py", "line": 123, \
"title": "one line", "scenario": "how it fails"}}], "ran_tests": true, "tried_mutant": true}}
```
"""

IMPLEMENT = """\
You are implementing a change in the repository at {snap}. Work only in this directory. \
There is no network and no GitHub access; do not push, fetch or install anything.

## Task
{brief}

## Rules
- Keep the change to the files it needs. Expected scope: {scope}.
- Do not commit. Leave the finished change in the working tree.
- Add or extend tests for what you change. Some acceptance tests are hidden from you; \
build to the task text and the interface it states.
- Run the existing suites that touch your change under a memory cap, from the repository root:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 env PYTHONPATH={snap} {python} <suite>
{suites}
- CLAUDE.md at the repository root holds the project's rules; follow them.
{fixtures}
When you finish, reply with a short summary of what you changed.
"""

FIX = """\
{implement}

## Review round {round}
Your previous change is already in the working tree. An independent reviewer found the \
following. Fix every HIGH and MED finding, fix a LOW one only when it is cheap, and do not \
widen the scope of the change. Re-run the suites afterwards.

{findings}
"""


def review_prompt(case: dict, snap, python: str | None = None) -> str:
    python = python or config.python_bin()
    focus = "\n".join(f"- {f}" for f in case.get("focus") or []) or "- correctness against the stated claim"
    suites = "\n".join(f"   - {s}" for s in case.get("suites") or []) or "   - (none named: find the relevant ones)"
    return REVIEW.format(snap=snap, python=python, brief=case["brief"].strip(), focus=focus, suites=suites)


def implement_prompt(case: dict, snap, python: str | None = None) -> str:
    python = python or config.python_bin()
    suites = "\n".join(f"   - {s}" for s in case.get("regression_suites") or []) or "   - (none named)"
    fixtures = ""
    if case.get("fixtures"):
        fixtures = "- Inputs the task refers to: " + ", ".join(case["fixtures"]) + "\n"
    return IMPLEMENT.format(snap=snap, python=python, brief=case["brief"].strip(),
                            scope=", ".join(case.get("scope") or []) or "as the task needs",
                            suites=suites, fixtures=fixtures)


def fix_prompt(case: dict, snap, round_no: int, findings_text: str, python: str | None = None) -> str:
    return FIX.format(implement=implement_prompt(case, snap, python), round=round_no,
                      findings=findings_text.strip() or "(the reviewer's findings could not be read)")


def reviewer_case(case: dict) -> dict:
    """The review-shaped view of an implement case, for the fixed reviewer."""
    return {"id": case["id"], "kind": "review", "brief": case["brief"],
            "focus": ["does the change do what the task says, and no more",
                      "files edited outside the expected scope: " + ", ".join(case.get("scope") or ["(unspecified)"]),
                      "tests that cannot fail"],
            "suites": list(case.get("regression_suites") or [])}
