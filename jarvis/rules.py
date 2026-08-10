"""Command rules: what runs without asking, and what never runs at all.

The gate used to be binary — `dangerous=True` meant ask, and the only way to
stop being asked was the persistent allowlist, matched on a command's first
word. "git" as an allowlist entry silences `git push` as thoroughly as `git
status`, which is too coarse to be useful and too coarse to be safe.

Three verdicts now, decided per command:

  DENY   never runs, and is never even put to the owner. The same shape as the
         `.env` refusal: approving a command is not consent to what it does, so
         some things should not be approvable at all.
  ALLOW  runs without asking.
  ASK    the default, and what anything unrecognised falls back to.

Two rules make the verdicts trustworthy:

**Every segment is judged, and the worst verdict wins.** `git commit && rm -rf
/` is one string with two commands in it, and a matcher that only looked at the
first word would wave it through on the strength of `git`. Command substitution
(`$(...)`, backticks) cannot be judged at all, so its presence forces ASK.

**Longest match wins.** `git` is allowed and `git push` is not, so the phrase
has to beat the stem.

Owner's choices, 2026-08-09, recorded here because the reasoning is the point:
git writes yes but **not push** (it reaches production directly) and **not
reset --hard / clean** (they can destroy uncommitted work); build and package
tooling yes; file shuffling yes but **never rm**; dev servers yes; sudo and
disk-destruction denied outright; auth commands deliberately *not* denied.

This module is SELF_PROTECTED (see tools/files.py). It decides what runs
without a human, so an agent that could edit it could widen its own reach —
the same reasoning that freezes permissions.py and the approval broker.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

DENY, ASK, ALLOW = "deny", "ask", "allow"
_ORDER = {DENY: 0, ASK: 1, ALLOW: 2}  # lower is stricter; the minimum wins


@dataclass
class Verdict:
    decision: str
    reason: str = ""

    def __bool__(self) -> bool:  # truthy only when nothing needs to be asked
        return self.decision == ALLOW


# --- deny: not approvable, by anyone ----------------------------------------

# Privilege escalation. You cannot answer a password prompt from a tool call
# anyway, so these only ever hang or fail confusingly — and a command that
# *did* get root would be doing it outside every boundary in this codebase.
_ESCALATION = {"sudo", "su", "doas", "pkexec", "runas"}

# Disk and system destruction. Nothing here has a recoverable failure mode.
_DESTRUCTION = {
    "dd", "mkfs", "fdisk", "parted", "sfdisk", "cfdisk", "mkswap", "fsck",
    "shutdown", "reboot", "halt", "poweroff", "init", "telinit",
}

# Shapes rather than stems: a destructive command whose *target* is what makes
# it destructive.
_DENY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\brm\b(?=[^|;&]*\s-[a-z]*r)(?=[^|;&]*\s-[a-z]*f)"
                   r"[^|;&]*?\s+(/|~|\$HOME|/\*|~/\*)\s*$"),
        "a recursive force-delete of a filesystem or home root",
    ),
    (
        re.compile(r"\brm\b(?=[^|;&]*\s-[a-z]*r)[^|;&]*?\s+"
                   r"/(bin|boot|dev|etc|lib|proc|root|sbin|srv|sys|usr|var|home)\b"),
        "a recursive delete of a system directory",
    ),
    (re.compile(r"\bchmod\b[^|;&]*\s-[a-zA-Z]*R[^|;&]*\s+/\s*$"), "chmod -R on the filesystem root"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;"), "a fork bomb"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd)[a-z0-9]*"), "a raw write to a block device"),
]


# --- allow: runs without asking ---------------------------------------------

_GIT_ALLOWED = {
    "add", "commit", "checkout", "switch", "branch", "merge", "stash", "tag",
    "restore", "revert", "cherry-pick", "init", "mv", "apply", "am",
}
# Excluded from auto-approval at the owner's instruction: push reaches
# production directly; reset --hard and clean can destroy uncommitted work.
# These are ASK, not DENY — they are ordinary operations, they just want eyes.
_GIT_NEVER_AUTO = {"push", "clean", "rebase", "filter-branch", "reset", "gc", "prune"}

_BUILD = {
    "uv", "pip", "pip3", "npm", "pnpm", "yarn", "npx", "node", "python",
    "python3", "pytest", "make", "cargo", "go", "tsc", "ruff", "black",
    "mypy", "poetry", "pipx", "deno", "bun",
}
_FILES = {"mkdir", "cp", "mv", "touch", "ln", "chmod", "chown", "rmdir", "install"}
_PROCESS = {"nohup", "kill", "pkill", "pgrep", "lsof", "systemctl", "timeout", "setsid"}

# Read-only staples. run_readonly already covers these, but they turn up as
# segments of a compound command, and one `ls` should not drag the whole line
# into an approval prompt.
_READONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "file", "stat",
    "echo", "pwd", "which", "true", "test", "sort", "uniq", "cut", "sed", "awk",
    "date", "printf", "dirname", "basename", "env", "tee",
}

_ALLOW_STEMS = _BUILD | _FILES | _PROCESS | _READONLY

# **rm is never auto-approved**, at the owner's instruction. It is not denied —
# deleting a file is ordinary — but it always gets a look.
_NEVER_AUTO_STEMS = {"rm", "shred", "truncate", "curl", "wget", "ssh", "scp", "rsync", "git"}

# An interpreter handed inline source is arbitrary code execution wearing the
# costume of a build tool. `python` is on the allow list because running a
# script or a test suite is routine; `python -c "shutil.rmtree('/')"` is not
# routine, and auto-approving the stem would auto-approve the language. So the
# inline-code flags pull it back to ASK.
_INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "--command", "-"}
_INTERPRETERS = {"python", "python3", "node", "deno", "bun", "perl", "ruby", "php",
                 "sh", "bash", "zsh", "ksh", "dash"}

_URL = re.compile(r"https?://[^\s'\"|;&)]+")


def urls(command: str) -> list[str]:
    return _URL.findall(command)


def segments(command: str) -> list[str]:
    """Split a command line into the individual commands it will actually run."""
    parts = re.split(r"\|\||&&|;|\||\n", command)
    return [p.strip() for p in parts if p.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _stem(tokens: list[str]) -> str:
    if not tokens:
        return ""
    # Skip leading VAR=value assignments — `FOO=1 python x.py` is a python run.
    for token in tokens:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        return token.rsplit("/", 1)[-1].lower()
    return ""


def _first_word_arg(tokens: list[str]) -> str:
    """The first non-flag argument — a subcommand, if the tool has them."""
    for token in tokens[1:]:
        if not token.startswith("-"):
            return token.lower()
    return ""


def _judge_segment(segment: str) -> Verdict:
    tokens = _tokens(segment)
    stem = _stem(tokens)
    if not stem:
        return Verdict(ASK, "could not parse the command")

    # Escalation is checked across every token, not just the stem: `echo x |
    # sudo tee /etc/hosts` puts sudo in the middle of the line.
    for token in tokens:
        base = token.rsplit("/", 1)[-1].lower()
        if base in _ESCALATION:
            return Verdict(DENY, f"{base} runs as another user, outside every gate here")
        if base in _DESTRUCTION or base.startswith("mkfs."):
            return Verdict(DENY, f"{base} has no recoverable failure mode")

    for pattern, why in _DENY_PATTERNS:
        if pattern.search(segment):
            return Verdict(DENY, why)

    if stem == "git":
        sub = _first_word_arg(tokens)
        if sub in _GIT_NEVER_AUTO:
            if sub == "reset" and not any(
                f in tokens for f in ("--hard", "--merge", "--keep")
            ):
                return Verdict(ALLOW)
            return Verdict(ASK, f"git {sub} needs a look before it runs")
        return Verdict(ALLOW) if sub in _GIT_ALLOWED else Verdict(ASK)

    if stem in _INTERPRETERS and any(t in _INLINE_CODE_FLAGS for t in tokens[1:]):
        return Verdict(ASK, f"{stem} with inline source is arbitrary code, not a build step")

    if stem == "systemctl" and "--user" not in tokens:
        return Verdict(ASK, "system-wide systemctl affects the whole machine")

    if stem in _NEVER_AUTO_STEMS:
        return Verdict(ASK)
    if stem in _ALLOW_STEMS:
        return Verdict(ALLOW)
    return Verdict(ASK)


def decide(command: str) -> Verdict:
    """The verdict for a whole command line.

    Every segment is judged and the strictest verdict wins, so appending
    something dangerous to something benign cannot launder it.
    """
    if not command or not command.strip():
        return Verdict(ASK, "empty command")

    # Command substitution runs a command this function never sees. There is no
    # honest verdict except "a human should look".
    if "$(" in command or "`" in command:
        inner = Verdict(ASK, "contains command substitution, which cannot be judged here")
    else:
        inner = Verdict(ALLOW)

    worst = inner
    for segment in segments(command):
        verdict = _judge_segment(segment)
        if _ORDER[verdict.decision] < _ORDER[worst.decision]:
            worst = verdict
        if worst.decision == DENY:
            break
    return worst
