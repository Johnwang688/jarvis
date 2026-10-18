"""SEC-2 acceptance tests: the approval card's attribution string.

`jarvis.v2.approvals.label` (v2) and `jarvis.tasks._label` (v1) build the
`origin` an owner reads before approving. Model-chosen text (a task brief)
reaches it, so it must carry no unseen character (zero-width, bidi override,
NUL, line/paragraph separator, ...) and must be one line, capped at 60
characters with a closing "…" when cut. The oracle for "unseen" is
`jarvis.untrusted.unseen`; no character table is written here.

Pure and free: no network, no config, no daemon.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/approval_label_check.py
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import tasks, untrusted  # noqa: E402
from jarvis.v2.approvals import label  # noqa: E402
from jarvis.v2.model import Role, Task  # noqa: E402
from jarvis.v2.permissions import PermitContext  # noqa: E402
from jarvis.v2.provider import Brief  # noqa: E402

CHECKS = 0
FAILURES: list[str] = []

# The card's examples, spelled by code point so the file stays readable.
RLO = "‮"          # right-to-left override
ZWSP = "​"         # zero-width space
LSEP = " "         # line separator
LRI = "⁦"          # left-to-right isolate
NUL = "\x00"
SAMPLES = {
    "rlo": f"ok{RLO}evil",
    "zwsp": f"a{ZWSP}b",
    "line-separator": f"x{LSEP}y",
    "isolate": f"x{LRI}y",
    "nul": f"x{NUL}y",
    "paragraph-separator": "x y",
    "zwj-bom-wordjoiner": "x‍﻿⁠y",
    "bidi-embeds": "x‪‫‬‭⁧⁨⁩y",
    "c1-and-newline": "x\u0085\r\ny",
    "leading-trailing": f"{RLO}{ZWSP} lead and trail {NUL}{LSEP}",
}


def ok(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")


def unseen_in(text: str) -> list[str]:
    return [f"U+{ord(c):04X}" for c in text if untrusted.unseen(c)]


def one_line(text: str) -> bool:
    return len(text.splitlines()) <= 1 and "\n" not in text and "\r" not in text


def quoted(text: str) -> str:
    """The name part of `task <id> "<name>"` (or of `"<name>"`)."""
    first, last = text.find('"'), text.rfind('"')
    return text[first + 1:last] if first != -1 and last > first else ""


def section(name):
    print(f"-- {name}")
    before = len(FAILURES)

    def done():
        print(f"{'ok  ' if len(FAILURES) == before else 'FAIL'} {name}")
    return done


def oracle_checks():
    done = section("oracle: every sample really holds an unseen character")
    for key, text in SAMPLES.items():
        ok(unseen_in(text), f"sample {key!r} has no character the oracle calls unseen")
    done()


def name_checks():
    for key, text in SAMPLES.items():
        done = section(f"case 1 v2 name: {key}")
        for who, out in (("with id", label("t1", text)), ("name only", label("", text))):
            ok(not unseen_in(out), f"{key} ({who}): unseen {unseen_in(out)} survive in {out!r}")
            ok(one_line(out), f"{key} ({who}): not a single line: {out!r}")
        ok(label("t1", text).startswith('task t1 "'), f"{key}: the 'task <id> \"...\"' shape changed")
        done()
    done = section("case 1 v2 name: visible text around a hidden character survives")
    out = label("t1", f"ok{RLO}evil")
    ok("ok" in out and "evil" in out, f"visible text lost: {out!r}")
    done()


def origin_id_checks():
    for key, text in SAMPLES.items():
        done = section(f"case 2 v2 origin_id: {key}")
        out = label(text, "name")
        ok(not unseen_in(out), f"{key}: unseen {unseen_in(out)} survive in id of {out!r}")
        ok(one_line(out), f"{key}: not a single line: {out!r}")
        done()
    done = section("case 2 v2 origin_id: id only, no name")
    out = label(f"t{RLO}1{ZWSP}", "")
    ok(not unseen_in(out) and one_line(out), f"id-only label unclean: {out!r}")
    done()


def cap_checks():
    done = section("case 3 v2 cap: long name ends in an ellipsis within 60")
    out = label("t1", "x" * 100)
    name = quoted(out)
    ok(len(name) <= 60, f"name is {len(name)} long, over the cap: {out!r}")
    ok(name.endswith("…"), f"a cut name must end in the ellipsis: {out!r}")
    ok(name.startswith("xxxx"), f"the head of the name was lost: {out!r}")
    done()
    done = section("case 3 v2 cap: a name of exactly 60 is left whole")
    out = label("t1", "y" * 60)
    ok(quoted(out) == "y" * 60, f"60 characters must not be cut: {out!r}")
    done()
    done = section("case 3 v2 cap: 61 characters is cut with the ellipsis")
    name = quoted(label("t1", "z" * 61))
    ok(len(name) <= 60 and name.endswith("…"), f"61 characters not capped with ellipsis: {name!r}")
    done()
    done = section("case 3 v2 cap: long origin_id is capped too")
    out = label("i" * 100, "n")
    idpart = out.split(" ")[1] if out.startswith("task ") else ""
    ok(0 < len(idpart) <= 60, f"origin_id {len(idpart)} long: {out!r}")
    done()
    done = section("case 3 v2 backticks: removed from name and id")
    out = label("t`1", "a`b``c")
    ok("`" not in out, f"backtick survived: {out!r}")
    ok("abc" in out and "t1" in out, f"visible text around backticks lost: {out!r}")
    done()
    done = section("case 3 v2 whitespace: runs collapse to one space, ends trimmed")
    ok(label("t1", "  a \t\n  b  ") == 'task t1 "a b"', f"got {label('t1', '  a \t\n  b  ')!r}")
    ok(label("t1", "") == "task t1" and label("", "") == "", "empty name/id shapes changed")
    done()


def v1_checks():
    for key, text in SAMPLES.items():
        done = section(f"case 4 v1 _label name: {key}")
        out = tasks._label("tk-9", text)
        ok(not unseen_in(out), f"{key}: unseen {unseen_in(out)} survive in {out!r}")
        ok(one_line(out), f"{key}: not a single line: {out!r}")
        done()
    done = section("case 4 v1 _label cap: long name ends in an ellipsis within 60")
    name = quoted(tasks._label("tk-9", "x" * 100))
    ok(len(name) <= 60 and name.endswith("…"), f"v1 long name not capped with ellipsis: {name!r}")
    done()
    done = section("case 4 v1 _label: backticks removed, empty name keeps the short shape")
    ok("`" not in tasks._label("tk-9", "a`b"), "v1 backtick survived")
    ok(tasks._label("tk-9", "") == "task tk-9", "v1 empty-name shape changed")
    done()
    done = section("case 4 v1 == v2: same inputs, same label")
    inputs = list(SAMPLES.values()) + ["x" * 100, "y" * 60, "z" * 61, "a`b", "  a \t\n b ",
                                       "", "plain name", "é" * 70, "evil`\nname\t" + "x" * 100]
    for text in inputs:
        a, b = tasks._label("tk-9", text), label("tk-9", text)
        ok(a == b, f"v1/v2 differ for {text[:25]!r}: {a!r} != {b!r}")
    done()


def origin_end_to_end_checks():
    brief = Brief(role=Role.IMPLEMENTER, cwd="/tmp")
    hostile = f"fix the build{RLO} and{ZWSP} then{NUL} exit{LSEP}`ls`\nsecond line"
    done = section("end to end: PermitContext.origin() with a hostile task brief")
    task = Task("ab12cd34", "proj", hostile)
    out = PermitContext("chat", brief, task=task).origin()
    ok(out.startswith("task ab12cd34 "), f"origin shape changed: {out!r}")
    ok(not unseen_in(out), f"origin carries unseen {unseen_in(out)}: {out!r}")
    ok(one_line(out) and "`" not in out, f"origin not one clean line: {out!r}")
    ok("fix the build" in out, f"visible text lost from origin: {out!r}")
    done()
    done = section("end to end: origin() with a long hostile brief is capped with an ellipsis")
    task = Task("ab12cd34", "proj", f"{RLO}" + "w" * 200)
    out = PermitContext("chat", brief, task=task).origin()
    ok(not unseen_in(out), f"origin carries unseen {unseen_in(out)}: {out!r}")
    ok(len(quoted(out)) <= 60 and quoted(out).endswith("…"), f"origin name not capped with ellipsis: {out!r}")
    done()
    done = section("end to end: origin() with a hostile task id")
    task = Task(f"ab{RLO}12{ZWSP}", "proj", "ok")
    out = PermitContext("chat", brief, task=task).origin()
    ok(not unseen_in(out) and one_line(out), f"hostile id leaked into origin: {out!r}")
    done()
    done = section("end to end: origin() for a chat thread is empty")
    ok(PermitContext("chat", brief).origin() == "", "a chat origin must stay empty")
    done()


def main():
    oracle_checks()
    name_checks()
    origin_id_checks()
    cap_checks()
    v1_checks()
    origin_end_to_end_checks()
    print(f"\n{CHECKS} checks, {len(FAILURES)} failed")
    for msg in FAILURES:
        print(f"  FAIL: {msg}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
