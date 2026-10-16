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
     '        if any(m in low for m in _GRAPHQL_MERGES):', '        if False:', PERMS_CHECK),
    ("GraphQL close mutation not matched", GATE,
     '        if "closepullrequest" in low or ("updatepullrequest" in low and "closed" in low):',
     '        if False:', PERMS_CHECK),
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
     '        common = gitdir\n        pointer = _read(gitdir / "commondir", 4096)\n        if pointer and pointer.strip():\n'
     '            common = (gitdir / pointer.strip()).resolve()\n        return gitdir, common',
     '        common = gitdir\n        return gitdir, common', PERMS_CHECK),
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
     '                _text(line, " ".join(str(a) for a in args), inline=True)', '                pass', PERMS_CHECK),
    ("too-complex line not asked", GATE,
     'line.hit(UNSURE, "the line is too complex to judge")', 'pass', PERMS_CHECK),
    # Review of #208, H-1: an earlier program moves HEAD or rewrites config.
    ("H-1: HEAD moves not read", GATE,
     '    if name in _GIT_MOVES_HEAD:\n        _git_moves_head(', '    if False:\n        _git_moves_head(', PERMS_CHECK),
    ("H-1: a switch target ignored", GATE,
     '        self.heads = list(dict.fromkeys(kept + list(names)))', '        pass', PERMS_CHECK),
    ("H-1: `;` lets a failed switch go on, ignored", GATE,
     '        kept = before if (self.lenient or self.subshell) else []', '        kept = []', PERMS_CHECK),
    ("H-1: a config write before a plain push ignored", GATE,
     '        if line.config_changed:\n            line.hit(PUSH_UNSURE, "an earlier',
     '        if False:\n            line.hit(PUSH_UNSURE, "an earlier', PERMS_CHECK),
    ("H-1: a config write before an alias ignored", GATE,
     '    return UNKNOWN if line.config_changed else ""', '    return ""', PERMS_CHECK),
    ("H-1: gh pr checkout leaves HEAD", GATE,
     '            line.switch(UNKNOWN)        # HEAD is now the PR\'s branch', '            pass', PERMS_CHECK),
    ("H-1: branch -m not read", GATE,
     '            if moving:\n                line.switch([new])', '            if False:\n                line.switch([new])',
     PERMS_CHECK),
    ("H-1: symbolic-ref not read", GATE,
     '                line.switch([branch] if branch else UNKNOWN)', '                pass', PERMS_CHECK),
    ("H-1: a file checkout read as a switch", GATE,
     '        if len(positionals) > 1 or _is_path(line, dirs, text):\n            return',
     '        if False:\n            return', PERMS_CHECK),
    # M-1..M-6 and the LOWs.
    ("M-1: brace expansion literal", GATE,
     '    return _brace_expands(text, mask)', '    return False', PERMS_CHECK),
    ("M-2: no raw-line backstop", GATE, '    if line.executor:\n', '    if False:\n', PERMS_CHECK),
    ("M-2: unknown programs are not executors", GATE,
     '            # raw-line backstop applies (`judge`).\n            line.executor = True',
     '            # raw-line backstop applies (`judge`).\n            pass', PERMS_CHECK),
    ("M-2: git exec settings not executors", GATE,
     '                if str(value).lower().startswith(_GIT_EXEC_KEYS):\n                    line.executor = True',
     '                if False:\n                    line.executor = True', PERMS_CHECK),
    ("M-2: GIT_SSH_COMMAND not an executor", GATE,
     '        self.executor = bool(_GIT_EXEC_ENV.search(command))', '        self.executor = False', PERMS_CHECK),
    ("M-2: git exec subcommands not executors", GATE,
     '    if name in _GIT_EXEC_SUBS:\n        line.executor = True', '    if False:\n        line.executor = True',
     PERMS_CHECK),
    ("M-2: the backstop repeats what the structure named", GATE,
     '        if not inline and pr_known:\n            break', '        if False:\n            break', PERMS_CHECK),
    ("M-3: an inline bare push not judged", GATE,
     '            repos = line.repos()\n            _current(line, repos, _defaults(repos))', '            pass', PERMS_CHECK),
    ("M-4: any .git is a git dir", GATE,
     '        if not _is_git_dir(gitdir):\n            return UNKNOWN', '        if False:\n            return UNKNOWN',
     PERMS_CHECK),
    ("M-4: a symlinked HEAD not read", GATE,
     '        if head.is_symlink():', '        if False:', PERMS_CHECK),
    ("M-4: a garbled HEAD read as detached", GATE,
     '    if _HEX_HEAD.fullmatch(text.strip()):\n        return None\n    return UNKNOWN',
     '    return None', PERMS_CHECK),
    ("M-5: enqueuePullRequest not a merge", GATE, '                   "enqueuepullrequest")', ')', PERMS_CHECK),
    ("M-5: deleteRef not read", GATE, '        if "deleteref" in low:', '        if False:', PERMS_CHECK),
    ("M-5: updateRef not read", GATE, '        if "updateref" in low:', '        if False:', PERMS_CHECK),
    ("M-5: createCommitOnBranch not read", GATE, '        if "createcommitonbranch" in low:', '        if False:',
     PERMS_CHECK),
    ("M-6: config parsed per call", GATE,
     '        return _config_entries(_config_files(repos), self._config_cache)',
     '        return _config_entries(_config_files(repos), None)', PERMS_CHECK),
    ("LOW: an unreadable issue edit not asked", GATE,
     '    elif re.search(r"(^|/)issues/[^/]+$", path) and unreadable_fields:',
     '    elif False:', PERMS_CHECK),
    ("LOW: percent-encoding not decoded", GATE, '    path = urllib.parse.unquote(path)\n', '', PERMS_CHECK),
    ("LOW: `..` not resolved", GATE, '    path = posixpath.normpath("/" + path)\n', '', PERMS_CHECK),
    ("LOW: config comments kept", GATE,
     '            value = _strip_comment(value) if eq else "true"', '            value = value.strip() if eq else "true"',
     PERMS_CHECK),
    ("LOW: first config value wins", GATE,
     '        if sec == section and name == key and (sub is None or subsection == sub):\n            found = value',
     '        if sec == section and name == key and (sub is None or subsection == sub):\n            return value',
     PERMS_CHECK),
    # One card.
    ("one card: a yes is not held", PERMIT,
     '                memo.remember(tool_use_id, tool, args)', '                pass', PERMS_CHECK),
    ("one card: other arguments answered", PERMIT,
     '        return (held_tool == tool and digest == _digest(dict(args or {}))',
     '        return (held_tool == tool', PERMS_CHECK),
    ("one card: the TTL ignored", PERMIT,
     '                and 0 <= self._clock() - at < OWNER_YES_TTL_S)', ')', PERMS_CHECK),
    ("one card: answered more than once", PERMIT,
     '            held = self._held.pop(tool_use_id, None)', '            held = self._held.get(tool_use_id)', PERMS_CHECK),
    ("one card: can_use_tool does not recall", CLAUDE,
     '                    if recall(tool_use_id, str(tool_name or ""), dict(args)) is True:',
     '                    if False:', CLAUDE_CHECK),
    ("one card: the hook does not pass tool_use_id", CLAUDE,
     '                if tool_use_id is not None and _takes_keyword(session.permit, "tool_use_id"):',
     '                if False:', CLAUDE_CHECK),
    ("permit: layer 2 does not consult the gate", PERMIT,
     '        if owner_only is not None:\n            reason = f"always-ask rule',
     '        if False:\n            reason = f"always-ask rule', PERMS_CHECK),
    ("permit: an Always is offered", PERMIT,
     'allowlistable=False, headline=owner_only.headline, origin=who,',
     'allowlistable=True, headline=owner_only.headline, origin=who,', PERMS_CHECK),
    ("permit: the card has no headline", PERMIT,
     'allowlistable=False, headline=owner_only.headline, origin=who,',
     'allowlistable=False, origin=who,', PERMS_CHECK),
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
