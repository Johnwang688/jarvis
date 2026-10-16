"""Mutation evidence for command-bearing tools (2026-10-15: Claude's Monitor).

Each mutant undoes one piece of the fix in a temp copy of the package and
runs `permissions_check.py command_tool_checks` against it; every mutant must
make the check fail. Free: temp HOME inside the check, no network.

    python tests/v2/command_tool_mutants.py [index …]
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
APPROVALS = "jarvis/v2/approvals.py"
PERMIT = "jarvis/v2/permissions.py"
CHECK = ["tests/v2/permissions_check.py", "command_tool_checks"]

MUTANTS = [
    ("Monitor not a command tool by name", APPROVALS,
     '"Bash", "BashOutput", "Monitor", "PowerShell",', '"Bash", "BashOutput", "PowerShell",'),
    ("PowerShell not a command tool by name", APPROVALS,
     '"Bash", "BashOutput", "Monitor", "PowerShell",', '"Bash", "BashOutput", "Monitor",'),
    ("an unknown tool's command key is not read", APPROVALS,
     '    if not isinstance(args, dict):\n        return False\n',
     '    if True:\n        return False\n'),
    ("only the first command key is read", APPROVALS,
     '    return "\\n".join(dict.fromkeys(texts))', '    return texts[0] if texts else ""'),
    ("argv arrays are not read", APPROVALS,
     '    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):',
     '    if False:'),
    ("the allowlist maps any command-bearing tool (F1)", APPROVALS,
     '    if not is_shell_tool(tool):\n        # Not a shell',
     '    if not is_command_tool(tool, args):\n        # Not a shell'),
    ("layer 4 answers an unknown command tool (F1)", PERMIT,
     'if getattr(asker, "human_backed", False) and not is_unknown_command_tool(tool, args):',
     'if getattr(asker, "human_backed", False):'),
    ("an unknown command tool's card offers Always (F1)", PERMIT,
     '        allowlistable = False       # no stem and no whole-tool entry (#211 review, F1)',
     '        pass'),
    ("the broker mints for an unknown command tool (F1)", APPROVALS,
     '                        or is_unknown_command_tool(item.request.tool, item.request.args)):', '):'),
    ("an argv beside the command not judged", APPROVALS,
     '                texts[0] = texts[0] + " " + shlex.join(argv)', '                pass'),
    ("Workflow read as a shell (F2)", APPROVALS,
     '    if tool in CODE_TOOLS:\n        return False\n    if tool in V2_COMMAND_TOOLS:\n        return True',
     '    if tool in V2_COMMAND_TOOLS:\n        return True'),
    ("a shell with no command text runs (F3)", PERMIT,
     '        if unreadable_command(tool, args) == "deny":', '        if False:'),
    ("an unreadable unknown command is not asked (F3)", PERMIT,
     '        if match is None and unreadable_command(tool, args) == "ask":', '        if False:'),
    ("the permit reads commands by name only", PERMIT,
     '        command = _command_of(args) if is_command_tool(tool, args) else ""\n',
     '        command = _command_of(args) if tool in ("Bash", "shell", "run_command") else ""\n'),
    ("always-ask reads commands by name only", PERMIT,
     '    command = _command_of(args) if is_command_tool(tool, args) else ""\n    segments',
     '    command = _command_of(args) if tool in ("Bash", "shell") else ""\n    segments'),
]


def run(mutant, python: str) -> tuple[bool, str]:
    name, rel, old, new = mutant
    with tempfile.TemporaryDirectory(prefix="ct-mutant-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(ROOT / "jarvis", copy / "jarvis", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        proc = subprocess.run([python, *CHECK], cwd=copy, env=dict(os.environ, PYTHONPATH=str(copy)),
                              capture_output=True, text=True, timeout=600)
        out = proc.stdout + proc.stderr
        tail = [l for l in out.splitlines() if "FAIL" in l][:1] or out.strip().splitlines()[-1:]
        return proc.returncode != 0 and ("FAIL" in out or "Error" in out), (tail[0][:140] if tail else "")


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}")
        if not killed:
            survived.append(index)
    print(f"\n{'all %d mutants killed' % len(picks) if not survived else '%d of %d mutants survived' % (len(survived), len(picks))}")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
