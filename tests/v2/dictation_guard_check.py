"""The VA-3 dictation guard (`jarvis/v2/dictation_guard.py`). Free, synthetic.

What must hold (plan: docs/plans/2026-11-16-voice-anywhere-va3-local-cleanup.md):
  1. a cleanup may only delete words; added, substituted, repeated or reordered
     words are refused, through every normalisation (numbers, contractions,
     spoken punctuation)
  2. no newline, control/format/bidi/zero-width character, or symbol the owner
     did not say outside the safe punctuation set
  3. over-deletion and empty output are refused; fillers do not count
  4. choose() types the model's text only when it passes, else the rules text
     when it passes, else the raw transcript
  5. the module imports nothing from Jarvis, and is SELF_PROTECTED

Run:  PYTHONPATH=$PWD .venv/bin/python tests/v2/dictation_guard_check.py
"""
from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis.v2 import dictation_guard as g  # noqa: E402

PASSED = FAILED = 0


def check(name: str, ok: bool) -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        print(f" FAIL {name}")


def accepts(raw: str, cleaned: str) -> bool:
    return g.check(raw, cleaned).ok


def reason(raw: str, cleaned: str) -> str:
    return g.check(raw, cleaned).reason


def deletion_rule() -> None:
    good = [
        ("um so I want to uh test the intake", "So I want to test the intake."),
        ("I want I want to test the intake", "I want to test the intake."),
        ("meet on tuesday no wait wednesday at five", "Meet on Wednesday at 5."),
        ("we need twenty five parts", "We need 25 parts."),
        ("it costs two thousand twenty six dollars", "It costs 2026 dollars."),
        ("do not touch the drivetrain", "Don't touch the drivetrain."),
        ("i'm going to check it comma then send it period", "I'm going to check it, then send it."),
        ("what time is the meeting question mark", "What time is the meeting?"),
        ("fifty percent done", "50% done."),
        ("um uh", ""),
        ("", ""),
        ("comma period", ""),
        ("um um um yes", "Yes."),  # fillers do not count toward over-deletion
    ]
    for n, (raw, cleaned) in enumerate(good):
        check(f"accepts deletion/equivalence {n}: {cleaned!r}", accepts(raw, cleaned))

    bad = [
        ("test the intake", "Test the lift.", "added_word"),           # substitution
        ("please test intake", "Please test the intake.", "added_word"),
        ("we can test", "We can can test.", "added_word"),             # repeated word
        ("test the intake", "Intake the test.", "reordered"),
        ("what time is the meeting", "The meeting is at 5.", "added_word"),  # answering
        ("what time is the meeting", "The meeting is.", "reordered"),
        ("one two", "12.", "added_word"),                              # numbers never merge
        ("twenty twenty six", "2026.", "added_word"),
        ("it's done", "It has done.", "added_word"),                   # contraction is one reading
        ("its done", "It's done.", "added_word"),
        ("test the period", "Test the period.", "added_word"),         # spoken punct is raw-side only
        ("list files", "ls -la.", "added_word"),
    ]
    for n, (raw, cleaned, why) in enumerate(bad):
        check(f"refuses {why} {n}: {cleaned!r}", reason(raw, cleaned) == why)


def characters() -> None:
    raw = "list the files then count them"
    for ch, why in [("\n", "newline"), ("\r", "newline"), (" ", "newline"), ("\x0b", "newline"),
                    ("\x85", "newline"), ("\t", "control_char"), ("\x00", "control_char"),
                    ("\x1b", "control_char"), ("​", "control_char"), ("‮", "control_char"),
                    ("⁦", "control_char"), ("", "control_char"), ("﻿", "control_char")]:
        check(f"refuses {ch!r} as {why}", reason(raw, f"List the files{ch}then count them.") == why)
    for sym in "|>&$`\\/<*{}[]#~^=+@_":
        check(f"refuses new symbol {sym!r}", reason(raw, f"List the files {sym} then count them.") == "new_symbol")
    check("allows a symbol the owner's text already had",
          accepts("pipe it | count them", "Pipe it | count them."))
    for sym in ".,?!'\"-:;()":  # '%' is a word (percent), judged as one
        check(f"allows safe punctuation {sym!r}", accepts(raw, f"List the files{sym} then count them."))
    check("a bare % is the word percent, so it is added", reason(raw, "List the files % then count them.") == "added_word")
    check("homoglyph word refused", reason("test the intake", "Test the іntake.") == "added_word")
    check("non-text refused", reason("x", None) == "not_text" and reason(None, "x") == "not_text")
    long_raw = "test " * 20
    check("much longer output refused", reason(long_raw, long_raw + "." * 40) == "too_long")


def deletion_cap() -> None:
    raw = "one two three four five six seven eight nine ten"
    check("over-deletion refused", reason(raw, "One two three.") == "over_deletion")
    check("exactly 40% kept accepted", accepts(raw, "1 2 3 4."))
    check("empty refused when content was said", reason("test the intake", "") == "empty")
    check("punctuation-only counts as empty", reason("test the intake", "...") == "empty")
    check("fillers kept do not satisfy the cap", reason("um uh test the intake now please",
                                                         "Um uh.") == "over_deletion")


def choose_rule() -> None:
    raw = "um test the intake"
    check("model text typed when it passes", g.choose(raw, "Test the intake.", "Test the intake!") ==
          ("Test the intake!", "model"))
    check("rules text when the model adds a word", g.choose(raw, "Test the intake.", "Test the lift.") ==
          ("Test the intake.", "rules"))
    check("rules text when there is no model", g.choose(raw, "Test the intake.", None) ==
          ("Test the intake.", "rules"))
    check("raw when the rules text fails too", g.choose(raw, "Test the lift.", "Test the lift.") ==
          (raw, "raw"))


def property_sweep() -> None:
    """Random deletions always pass; any inserted unseen word always fails."""
    rng = random.Random(7)
    vocab = "the a we test check intake lift run auton drive team meeting today later fix send".split()
    bad_pass = good_fail = 0
    for _ in range(3000):
        words = [rng.choice(vocab) for _ in range(rng.randint(3, 14))]
        raw = " ".join(words)
        keep = [w for w in words if rng.random() < 0.8] or words[:1]
        if len(keep) < len(words) * 0.4:
            keep = words
        if not accepts(raw, " ".join(keep).capitalize() + "."):
            good_fail += 1
        pos = rng.randrange(len(keep) + 1)
        added = keep[:pos] + ["zebra"] + keep[pos:]
        if accepts(raw, " ".join(added)):
            bad_pass += 1
    check("3000 random deletions accepted", good_fail == 0)
    check("3000 random insertions of an unseen word refused", bad_pass == 0)


def isolation() -> None:
    code = ("import sys; import jarvis.v2.dictation_guard; "
            "print(sorted(m for m in sys.modules if m.startswith('jarvis')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    check("imports no other Jarvis module",
          out.returncode == 0 and out.stdout.strip() == "['jarvis', 'jarvis.v2', 'jarvis.v2.dictation_guard']")
    text = (ROOT / "jarvis/tools/files.py").read_text(encoding="utf-8")
    check("guard is listed in SELF_PROTECTED", '"jarvis/v2/dictation_guard.py"' in text)


def main() -> int:
    deletion_rule()
    characters()
    deletion_cap()
    choose_rule()
    property_sweep()
    isolation()
    print(f"dictation_guard_check: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
