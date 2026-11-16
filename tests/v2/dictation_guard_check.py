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
        ("extend the grace period", "Extend the grace period."),  # literal spoken-punct word kept
        ("pay -5 dollars", "Pay -5 dollars."),                    # a word's own sign stays
        ("two hundred eleven no wait two hundred eight runs", "208 runs."),  # whole units deleted/kept
        ("pay minus five dollars", "Pay minus five dollars."),
        ("set rate fifty percent", "Set rate 50%."),
        ("one thousand percent", "1,000%"),
        ("we do not know", "We don\u2019t know."),             # curly apostrophe
        ("we don\u2019t know", "We don't know."),
        ("i'm ready", "I am ready."),                             # contraction split
        ("at three thirty", "At 3:30."),
        ("it's five o'clock", "It's 5 o'clock."),
        ("meet at four forty five", "Meet at 4:45."),             # clock time
        ("it costs one thousand dollars", "It costs 1,000 dollars."),
        ("re enable it", "Re-enable it."),                       # hyphen joins two words
        ("echo period", "echo period"),  # identity always passes
        ("put a colon here comma then save", "Put a colon here, then save."),
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
        ("test the period", "Test the comma.", "added_word"),          # spoken punct is still a word
        ("list files", "ls -la.", "added_word"),
        # Sol review of #392: values, merged numbers, command syntax, folding.
        ("pay 1.05 dollars", "Pay 1.5 dollars.", "added_word"),
        ("pay five dollars", "Pay -5 dollars.", "new_symbol"),
        ("the rate is five percent", "The rate is .5%.", "new_symbol"),
        ("choose twenty, one, or five", "Choose 21 or 5.", "added_word"),
        ("buy one thousand and one thousand parts", "Buy 1001 thousand parts.", "added_word"),
        ("echo hello", "!!; echo hello", "new_symbol"),
        ("ls la", "ls -la", "new_symbol"),
        ("um", "!!", "new_symbol"),
        ("Masse", "Maße", "added_word"),
        ("run the ss command", "Run the \u00df command.", "added_word"),
        ("run the ff command", "Run the \ufb00 command.", "new_symbol"),
        ("one two", "1 2.", "added_word"),
        ("meet at ten zero", "Meet at 10:00.", "added_word"),
        # Sol delta review of #392: signs, gap reuse, glued decimals, %, malformed prose, İ.
        ("pay -5 dollars", "Pay 5 dollars.", "value"),
        ("pay .5 dollars", "Pay 5 dollars.", "value"),
        ("ls la - thanks", "ls -la", "gap_reuse"),
        ("re-enable five volts", "Re-enable -5 volts.", "gap_reuse"),
        ("dose 1.05mg now", "Dose 1-05mg now.", "value"),
        ("pay 5% now", "Pay 5 now.", "value"),
        ("echo hello", "echo!!!; hello", "new_symbol"),
        ("i m here", "\u0130'm here", "fold"),
        ("one thousand one thousand", "1001 thousand.", "merged_number"),
        # Final Sol review of #392: number units and syntax tails.
        ("pay twenty five dollars", "Pay 5 dollars.", "partial_number"),
        ("pay twenty five dollars", "Pay five dollars.", "partial_number"),
        ("pay twenty five", "Pay twenty.", "unit_tail_deleted"),
        ("pay twenty five dollars", "Pay twenty 5 dollars.", "digits_mid_unit"),
        ("twelve fifty nine", "59.", "partial_number"),
        ("pay minus five dollars", "Pay 5 dollars.", "dropped_sign"),
        ("pay zero point five dollars", "Pay 5 dollars.", "dropped_decimal"),
        ("set rate fifty percent", "Set rate 50.", "dropped_percent"),
        ("echo $ foo", "echo $? foo", "syntax_tail"),
        ("cat /tmp/..", "cat /tmp/", "syntax_tail"),
    ]
    for n, (raw, cleaned, why) in enumerate(bad):
        # The label is diagnostic; what matters is the refusal.
        check(f"refuses ({why}) {n}: {cleaned!r}", not accepts(raw, cleaned))


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
    for sym in ".,?!":  # the only punctuation a cleanup may add, after a word
        check(f"allows prose punctuation {sym!r}", accepts(raw, f"List the files{sym} then count them."))
    for sym in ":;'\"":
        check(f"refuses added {sym!r} (allow-list is , . ? !)", not accepts(raw, f"List the files{sym} then count them."))
    check("refuses added brackets", not accepts(raw, "List the files (then count them)."))
    check("refuses added quotes", not accepts(raw, 'List "the files." Then count them.'))
    check("refuses joining digits with a hyphen", not accepts("pages five to ten", "Pages 5-10."))
    check("refuses a hyphen standing before a word", not accepts(raw, "List the files -then count them."))
    check("refuses a bracket glued into a word gap", reason(raw, "List the(files then count them.") == "new_symbol")
    check("a bare % is not prose punctuation", reason(raw, "List the files % then count them.") == "new_symbol")
    check("case changes only for ASCII letters", not accepts("\u00e9lan matters", "\u00c9lan matters."))
    check("ASCII case change accepted", accepts("echo matters", "Echo matters."))
    check("only , . ? ! ?! ... may follow a word", not accepts("echo hello", "echo; hello")
          and not accepts("echo hello", "echo hello;") and not accepts("echo hello", "echo& hello"))
    check("a word keeps its own trailing marks", accepts("wow!!! nice", "Wow!!! Nice."))
    check("no runs of marks beyond the allow-list", not accepts("echo hello", "echo!!! hello") and not accepts("echo hello", "Echo,, hello."))
    check("hyphen joins letters only", not accepts("ls 5", "ls-5") and not accepts("v5 brain", "v5-brain"))
    check("homoglyph word refused", not accepts("test the intake", "Test the \u0456ntake."))
    check("non-text refused", reason("x", None) == "not_text" and reason(None, "x") == "not_text")
    long_raw = "test " * 20
    check("much longer output refused", reason(long_raw, long_raw + "." * 40) == "too_long")


def deletion_cap() -> None:
    raw = "one two three four five six seven eight nine ten"
    check("over-deletion refused", reason("alpha beta gamma delta epsilon zeta eta theta iota kappa",
                                          "Alpha beta gamma.") == "over_deletion")
    check("part of a number unit refused", not accepts(raw, "One two three."))
    greek = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    check("exactly 40% kept accepted", accepts(greek, "Alpha beta gamma delta."))
    check("a run of separate number words never becomes digits", reason(raw, "1 2 3 4.") == "added_word")
    check("empty refused when content was said", reason("test the intake", "") == "empty")
    check("punctuation-only output refused", not accepts("test the intake", "..."))
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
    extra = vocab + ["comma", "period", "question", "mark", "colon", "um", "uh", "twenty", "five",
                     "hundred", "don't", "it's", "percent"]
    identity_fail = sum(1 for _ in range(3000)
                        if not accepts(r := " ".join(rng.choice(extra) for _ in range(rng.randint(1, 16))), r))
    check("3000 identity pairs (words, numbers, spoken punctuation, fillers) accepted", identity_fail == 0)


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
