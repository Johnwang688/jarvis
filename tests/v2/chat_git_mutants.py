"""Mutation evidence for the Codex chat git tools (owner decision CG-0,
2026-10-15; plan §4.5's ten named mutants, then more).

Each mutant breaks one guard in a temp copy of the package and runs the
named check against the copy. Every mutant must make its check fail; one
that survives is a guard no test holds. Free: no network, temp HOME, local
bare remotes and a fake gh inside the checks.

    python tests/v2/chat_git_mutants.py          # all mutants
    python tests/v2/chat_git_mutants.py 3 7      # by index
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CHAT = "jarvis/v2/chat_git.py"
TOOLS = "jarvis/v2/tools/chat_git_tools.py"
PERMIT = "jarvis/v2/permissions.py"
SESSION = "jarvis/v2/session_tools.py"
CODEX_CONFIG = "jarvis/v2/providers/codex_config.py"
CODEX_PROVIDER = "jarvis/v2/providers/codex.py"
BRIDGE = "jarvis/v2/bridge.py"
DISPATCH = "jarvis/tools/__init__.py"


def cg(*sections):
    return ["tests/v2/chat_git_check.py", *sections]


PERMS = ["tests/v2/permissions_check.py", "chat_git_checks"]
CODEX = ["tests/v2/codex_provider_check.py", "Checks.test_clean_env_is_exact_plus_the_owners_identity",
         "Checks.test_cg1_raw_network_git_and_gh_are_declined_with_the_pointer",
         "Checks.test_owner_only_merge_card_says_what_it_does"]

# (name, file, old, new, check)
MUTANTS = [
    # The plan's ten (§4.5).
    ("1: gate_line not handed to merge_gate (merge headline lost)", PERMIT,
     "owner_only = _owner_only(tool, chat.line if chat is not None else command,",
     "owner_only = _owner_only(tool, command,", PERMS),
    ("2: chat_pr_merge not dangerous", TOOLS,
     "@tool(dangerous=True)\ndef chat_pr_merge(", "@tool\ndef chat_pr_merge(", cg("session_tool_checks", "gh_checks")),
    ("3: gh runs in the worktree, not the mirror", CHAT,
     "            code, out, err = run_gh(argv, str(mirror))\n",
     "            code, out, err = run_gh(argv, target.worktree)\n", cg("gh_checks")),
    ("4: the pin is not compared", CHAT,
     "    if current is not None and current.lower() != target.repo.lower():\n        raise Refused(",
     "    if False:\n        raise Refused(", cg("refusal_checks")),
    ("5: any https:// host accepted", CHAT,
     '_HTTPS = re.compile(r"https://github\\.com/([^/\\s]+)/([^/\\s]+?)/?")',
     '_HTTPS = re.compile(r"https://[^/\\s]+/([^/\\s]+)/([^/\\s]+?)/?")', cg("unit_checks", "refusal_checks")),
    ("6: the local fetch runs with the credentialed env", CHAT,
     '    return _plain_git(["--git-dir", str(mirror), "fetch", "--no-tags", "--no-write-fetch-head",',
     '    return run_git(["--git-dir", str(mirror), "fetch", "--no-tags", "--no-write-fetch-head",',
     cg("env_checks")),
    ("7: the credential.helper reset dropped", CHAT,
     '    pins = ["-c", "credential.helper="]', "    pins = []", cg("env_checks")),
    ("8: clean_env gains GH_CONFIG_DIR", CODEX_CONFIG,
     "    env.update(git_identity())\n    return env",
     '    env.update(git_identity())\n    env["GH_CONFIG_DIR"] = str(Path.home() / ".config" / "gh")\n    return env',
     CODEX),
    ("9: CG-1 lets a merge reach the card first", PERMIT,
     "                    and any(chat_git.raw_remote(line) for line in (command, *also_commands))):",
     "                    and owner_only is None\n"
     "                    and any(chat_git.raw_remote(line) for line in (command, *also_commands))):",
     PERMS),
    ("10: foreground_only dropped", SESSION,
     "SESSION_TOOLS[_name] = SessionTool(_name, providers=_CODEX_ONLY, foreground_only=True)",
     "SESSION_TOOLS[_name] = SessionTool(_name, providers=_CODEX_ONLY)", cg("session_tool_checks")),
    # Further guards.
    ("a delegation grants the call", PERMIT,
     "and (dangerous_tool if tool in chat_git.DANGEROUS else True)):", "and True):", PERMS),
    ("the line is not re-checked at run time", CHAT,
     "    if fresh.line != approved.line:", "    if False:", cg("push_checks")),
    ("a grant is not used up", CHAT,
     "    held = _GRANT.get()\n    _GRANT.set(None)\n", "    held = _GRANT.get()\n", cg("push_checks")),
    ("a grant for other arguments accepted", CHAT,
     "    return held if held.args == tuple(sorted(values.items())) else None", "    return held",
     cg("push_checks")),
    ("a worktree of another repository accepted", CHAT,
     "    if common != own[-1]:", "    if False:", cg("refusal_checks")),
    ("a worktree outside the chat root accepted", CHAT,
     "        if chats not in here.parents:", "        if False:", cg("refusal_checks")),
    ("protected branches pushed", CHAT,
     "    if lowered in defaults or any(fnmatch(lowered, p) for p in merge_gate.PROTECTED_BRANCHES):",
     "    if False:", cg("refusal_checks")),
    ("a detached HEAD pushed", CHAT,
     '    if branch is None:\n        raise Refused("HEAD is detached', '    if False:\n        raise Refused("HEAD is detached',
     cg("refusal_checks")),
    ("a branch name git would misread accepted", CHAT,
     "    if not valid_branch(branch):\n        raise Refused(f\"branch name",
     "    if False:\n        raise Refused(f\"branch name", cg("refusal_checks")),
    ("the remote's default branch not read at push time", CHAT,
     "    judge_push(target, default)\n", "    judge_push(target)\n", cg("refusal_checks")),
    ("includes followed when origin is read", CHAT,
     '"--no-includes", "--get-all"', '"--includes", "--get-all"', cg("refusal_checks")),
    ("more than one origin URL accepted", CHAT,
     "    if len(values) != 1:", "    if not values:", cg("refusal_checks")),
    ("gh pr merge without --repo", CHAT,
     '["pr", "merge", str(args["number"]), "--repo", repo, f"--{args[\'method\']}"]',
     '["pr", "merge", str(args["number"]), f"--{args[\'method\']}"]', cg("gh_checks")),
    ("gh sees the daemon's whole environment", CHAT,
     "    env = {k: os.environ[k] for k in _OWNER_ENV_KEYS if os.environ.get(k)}",
     "    env = dict(os.environ)", cg("gh_checks")),
    ("output not scrubbed", CHAT, "        return scrub_text(secrets.strip_url_credentials(text))", "        return text",
     cg("gh_checks")),
    ("an unreadable target asked, not refused", PERMIT,
     '                return finish(Decision.DENY, L_DENY, f"{tool}: {exc}")',
     "                chat = None", PERMS),
    ("the card shows the model's arguments, not the line", PERMIT,
     "                                       chat.line if chat is not None else command, reason, L_HUMAN,",
     "                                       command, reason, L_HUMAN,", PERMS),
    ("CG-1 off", PERMIT,
     "                return finish(Decision.DENY, L_CHAT_GIT, chat_git.POINTER)", "                pass", PERMS),
    ("CG-1 declines a local-path fetch too", CHAT,
     "    return not _LOCAL.match(str(remote))", "    return True", cg("unit_checks")),
    ("identity values not validated", CODEX_CONFIG,
     "    if not value or len(value) > IDENTITY_CAP or not value.isprintable():", "    if not value:", CODEX),
    ("the chat git tools join the whole registry", TOOLS,
     "EXPLICIT_ONLY.update(chat_git.TOOLS)", "pass", cg("session_tool_checks")),
    ("offered to Claude too", SESSION,
     "_CODEX_ONLY = frozenset({ProviderName.CODEX.value})", "_CODEX_ONLY = _CHAT_PROVIDERS",
     cg("session_tool_checks")),
    ("a sub-agent depth allowed", CHAT, "    if runtime.depth() != 0:", "    if False:", cg("push_checks")),
    # #223's review, LOW 1: a refusal nobody was asked about is not "declined".
    ("LOW 1: the bridge drops the unasked reason", BRIDGE,
     "        return tools.Refusal(verdict.reason)\n    return False",
     "        return False\n    return False", cg("refusal_checks")),
    ("LOW 1: dispatch reads a Refusal as the owner's no", DISPATCH,
     "        if isinstance(answer, Refusal):", "        if False:", cg("refusal_checks")),
    ("LOW 1: a nondangerous tool's unasked reason dropped", BRIDGE,
     "    if isinstance(refusal, tools.Refusal):", "    if False:", cg("refusal_checks")),
    ("LOW 1: the owner's no not marked asked", PERMIT,
     "            asked[0] = True\n            if decision is Decision.ALLOW",
     "            if decision is Decision.ALLOW", cg("push_checks")),
    ("LOW 1: an asked DENY read as unasked", BRIDGE,
     "verdict.decision is Decision.DENY and not verdict.asked", "verdict.decision is Decision.DENY",
     cg("push_checks")),
    # LOW 2: a line CG-1 cannot be sure of takes the ordinary ask.
    ("LOW 2: an expanded remote declined", CHAT,
     "    if _unsure(remote):\n        return False", "    if False:\n        return False",
     cg("unit_checks")),
    ("LOW 2: a program a wrapper may not run read as sure", CHAT,
     "                if prog.word and prog.certain and _remote_prog(prog, merge_gate):",
     "                if prog.word and _remote_prog(prog, merge_gate):", cg("unit_checks")),
    # LOW 3: gh's login and target variables; the default branch read fails closed.
    ("LOW 3: GH_TOKEN reaches gh", CHAT,
     '"GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR")',
     '"GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "GH_TOKEN")', cg("guard_checks")),
    ("LOW 3: GH_REPO reaches gh", CHAT,
     '"GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR")',
     '"GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "GH_REPO")', cg("guard_checks")),
    ("LOW 3: an unreadable default read as none (fails open)", CHAT,
     "    if code != 0:\n        return False, None", "    if code != 0:\n        return True, None",
     cg("guard_checks")),
    ("LOW 3: the push ignores an unreadable default", CHAT,
     "    if not read:\n", "    if False:\n", cg("guard_checks")),
    # NIT 5: the HUD pointer is CG-1's alone.
    ("NIT 5: the pointer fires for any denied network line", CODEX_PROVIDER,
     "and verdict.decision is Decision.DENY and verdict.layer == L_CHAT_GIT):",
     "and verdict.decision is Decision.DENY and verdict.layer in (L_CHAT_GIT, \"deny\")):",
     ["tests/v2/codex_provider_check.py",
      "Checks.test_cg1_raw_network_git_and_gh_are_declined_with_the_pointer"]),
]


def run(index: int, mutant, python: str) -> tuple[bool, str]:
    name, rel, old, new, check = mutant
    with tempfile.TemporaryDirectory(prefix="cg-mutant-", dir="/var/tmp") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(ROOT / "jarvis", copy / "jarvis",
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        shutil.copytree(ROOT / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        target = copy / rel
        text = target.read_text()
        if text.count(old) != 1:
            return False, f"mutation site not found exactly once ({text.count(old)})"
        target.write_text(text.replace(old, new))
        env = dict(os.environ, PYTHONPATH=f"{copy}:{copy / 'tests' / 'v2'}")
        proc = subprocess.run([python, *check], cwd=copy, env=env, capture_output=True,
                              text=True, timeout=900)
        out = proc.stdout + proc.stderr
        killed = proc.returncode != 0 and ("FAIL" in out or "Error" in out)
        tail = [l for l in out.splitlines() if "FAIL" in l][:1] or out.strip().splitlines()[-1:]
        return killed, (tail[0][:140] if tail else "")


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(index, MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}", flush=True)
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
