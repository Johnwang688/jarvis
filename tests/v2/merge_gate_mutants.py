"""Mutation evidence for the owner-only gate (owner decision 2026-10-15).

Each mutant breaks one matcher or one piece of the permit's wiring in a temp
copy of the package, then runs `permissions_check.py merge_gate_checks` (and,
for the Claude hook, `claude_provider_check.py`) against the copy. Every
mutant must make its check fail; one that survives is a matcher no test
guards. Free: no network, no CLI, temp HOME inside the checks.

    python tests/v2/merge_gate_mutants.py          # all mutants
    python tests/v2/merge_gate_mutants.py 3 7      # by index
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
GATE = "jarvis/v2/merge_gate.py"
PERMIT = "jarvis/v2/permissions.py"
CLAUDE = "jarvis/v2/providers/claude.py"
PERMS_CHECK = ["tests/v2/permissions_check.py", "merge_gate_checks"]
CLAUDE_CHECK = ["tests/v2/claude_provider_check.py"]

# (name, file, old, new, check)
MUTANTS = [
    ("gh pr merge not matched", GATE,
     'elif verb == "merge":\n            line.hit(PR_MERGE',
     'elif verb == "merge-x":\n            line.hit(PR_MERGE', PERMS_CHECK),
    ("gh pr close not matched", GATE,
     'elif verb == "close":', 'elif verb == "close-x":', PERMS_CHECK),
    ("gh -R value not skipped", GATE,
     '_GH_VALUE_OPTS = frozenset({"-R", "--repo", "--hostname"})',
     '_GH_VALUE_OPTS = frozenset({"--hostname"})', PERMS_CHECK),
    ("unknown gh command (alias/extension) not asked", GATE,
     'if sure and name not in _GH_BUILTINS:', 'if False:', PERMS_CHECK),
    ("REST merge endpoint not matched", GATE,
     r'if re.search(r"(^|/)pulls/[^/]+/merge$", path) or', r'if False and re.search(r"x", path) or', PERMS_CHECK),
    ("REST pull edit (close) not matched", GATE,
     r'elif re.search(r"(^|/)pulls/[^/]+$", path):', r'elif False:', PERMS_CHECK),
    ("implicit POST with fields not a write", GATE,
     '"POST" if fields or has_input else "GET")', '"GET")', PERMS_CHECK),
    ("GraphQL merge mutation not matched", GATE,
     'if _GRAPHQL_MERGE.search(text):', 'if False:', PERMS_CHECK),
    ("GraphQL close mutation not matched", GATE,
     'if _GRAPHQL_CLOSE.search(text) or _GRAPHQL_UPDATE_CLOSED.search(text):', 'if False:', PERMS_CHECK),
    ("unreadable GraphQL query not asked", GATE,
     'if unread and not named:', 'if False:', PERMS_CHECK),
    ("protected branch never protected", GATE,
     'def _protected(name: str, defaults: set[str]) -> bool:\n    lowered',
     'def _protected(name: str, defaults: set[str]) -> bool:\n    return False\n    lowered', PERMS_CHECK),
    ("refspec judged by its source, not its destination", GATE,
     '        if not colon:\n            dst = src\n', '        dst = src\n', PERMS_CHECK),
    ("refs/heads/ prefix not stripped", GATE,
     'for prefix in ("refs/heads/", "heads/"):', 'for prefix in ():', PERMS_CHECK),
    ("`:branch` delete not matched", GATE,
     'if colon and not src:', 'if False:', PERMS_CHECK),
    ("--delete not matched", GATE,
     'if _is_long(name, "delete") or _is_long(name, "prune"):', 'if _is_long(name, "prune"):', PERMS_CHECK),
    ("-d not matched", GATE,
     'elif letter == "d":\n                    delete = True', 'elif letter == "d-x":\n                    delete = True',
     PERMS_CHECK),
    ("-f in a cluster not matched", GATE,
     'if letter == "f":\n                    force = True', 'if letter == "f-x":\n                    force = True',
     PERMS_CHECK),
    ("--force abbreviations not matched", GATE,
     'return bool(name) and full.startswith(name)', 'return name == full', PERMS_CHECK),
    ("+refspec force not matched", GATE,
     'line.hit(PUSH_FORCE, "a `+` refspec forces the push")', 'pass', PERMS_CHECK),
    ("--all/--branches not matched", GATE,
     'if _is_long(name, "all") or _is_long(name, "branches"):', 'if False:', PERMS_CHECK),
    ("push with no refspec never judged", GATE,
     '        _current(line, repos, defaults,\n                 entries', '        (lambda *a: None)(line, repos, defaults,\n                 entries',
     PERMS_CHECK),
    ("remote HEAD branch not a default", GATE,
     'defaults |= _remote_heads(common)', 'pass', PERMS_CHECK),
    ("linked worktree commondir not followed", GATE,
     'if pointer and pointer.strip():', 'if False:', PERMS_CHECK),
    ("cd on the line not followed", GATE,
     '            line.move(target, home_if_empty=True)\n            continue',
     '            continue', PERMS_CHECK),
    ("cd always unions (the shell's folder kept after `cd wt &&`)", GATE,
     'if union or self.subshell or not all(folder.is_dir() for folder in moved):', 'if True:', PERMS_CHECK),
    ("a subshell's cd outlasts it", GATE,
     'self.subshell = bool(re.search(r"(?<![$<>])\\(", command))', 'self.subshell = False', PERMS_CHECK),
    ("a missing cd target replaces the folder", GATE,
     'if union or self.subshell or not all(folder.is_dir() for folder in moved):',
     'if union or self.subshell:', PERMS_CHECK),
    ("git -C not followed", GATE,
     'if text == "-C":\n                dirs = _moved(dirs, value)', 'if False:\n                pass', PERMS_CHECK),
    ("wrapper folder options not followed", GATE,
     '            _dir_options(line, stem, args)\n', '', PERMS_CHECK),
    ("non-literal refspec not asked", GATE,
     'line.hit(PUSH_UNSURE, "the branch it pushes cannot be told from the command")', 'pass', PERMS_CHECK),
    ("xargs-appended refspecs not asked", GATE,
     '(prog.via_input and prog.replace is None)', 'False', PERMS_CHECK),
    ("git config override not asked", GATE,
     'line.hit(PUSH_UNSURE, "a git config override on the line changes what the push writes")\n        return',
     'pass', PERMS_CHECK),
    ("push.default=upstream not read", GATE,
     'entries if mode in ("upstream", "tracking") else None)', 'None)', PERMS_CHECK),
    ("git alias not expanded", GATE,
     '    expansion = _git_alias(line, name, dirs, overrides)\n',
     '    expansion = ""\n', PERMS_CHECK),
    ("submodule foreach / rebase -x not read", GATE,
     '    if name in _GIT_RUNS_SHELL:', '    if False:', PERMS_CHECK),
    ("wrappers, shells and substitutions not walked", GATE,
     '                walk.line(variant, True, 0)',
     '                [walk.segment(rules._shell_words(s), True, 0) for s in rules.segments(variant)]',
     PERMS_CHECK),
    ("non-literal program word not read", GATE,
     '            if could:\n', '            if False:\n', PERMS_CHECK),
    (".exe not dropped", GATE,
     'return (stem[:-4] if stem.endswith(".exe") else stem), False', 'return stem, False', PERMS_CHECK),
    ("inline interpreter code not read", GATE,
     '                _text(line, " ".join(str(a) for a in args))', '                pass', PERMS_CHECK),
    ("too-complex line not asked", GATE,
     'line.hit(UNSURE, "the line is too complex to judge")', 'pass', PERMS_CHECK),
    ("permit: layer 2 does not consult the gate", PERMIT,
     '        if owner_only is not None:\n            reason = f"always-ask rule',
     '        if False:\n            reason = f"always-ask rule', PERMS_CHECK),
    ("permit: an Always is offered", PERMIT,
     'allowlistable=False, headline=owner_only.headline, origin=who)',
     'allowlistable=True, headline=owner_only.headline, origin=who)', PERMS_CHECK),
    ("permit: the card has no headline", PERMIT,
     'allowlistable=False, headline=owner_only.headline, origin=who)',
     'allowlistable=False, origin=who)', PERMS_CHECK),
    ("permit: a widening card does not name the merge", PERMIT,
     'headline = (f"{owner_only.headline} · {widening}" if owner_only is not None\n                        else widening)',
     'headline = widening', PERMS_CHECK),
    ("permit: shell_cwd ignored", PERMIT, '    where = shell_cwd\n', '    where = None\n', PERMS_CHECK),
    ("permit: Codex item cwd ignored", PERMIT,
     'if where is None and ctx.provider == "codex":', 'if False:', PERMS_CHECK),
    ("permit: a model-written cwd trusted", PERMIT,
     'if where is None and ctx.provider == "codex":', 'if where is None:', PERMS_CHECK),
    ("Claude hook: cwd not passed", CLAUDE,
     'shell_cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None',
     'shell_cwd = None', CLAUDE_CHECK),
    ("Claude hook: keyword forced on every permit", CLAUDE,
     'if shell_cwd is not None and _takes_keyword(session.permit, "shell_cwd"):',
     'if shell_cwd is not None:', CLAUDE_CHECK),
]


def run(index: int, mutant, python: str) -> tuple[bool, str]:
    name, rel, old, new, check = mutant
    with tempfile.TemporaryDirectory(prefix="mg-mutant-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(ROOT / "jarvis", copy / "jarvis",
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        env = dict(os.environ, PYTHONPATH=str(copy))
        proc = subprocess.run([python, *check], cwd=copy, env=env, capture_output=True,
                              text=True, timeout=600)
        out = proc.stdout + proc.stderr
        killed = proc.returncode != 0 and ("FAIL" in out or "Error" in out)
        tail = [l for l in out.splitlines() if "FAIL" in l][:1] or out.strip().splitlines()[-1:]
        return killed, (tail[0][:140] if tail else "")


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(index, MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}")
        if not killed:
            survived.append(MUTANTS[index][0])
    print()
    if survived:
        print(f"{len(survived)} of {len(picks)} mutants survived")
        return 1
    print(f"all {len(picks)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
