"""Command rules: what runs without asking, and what never runs at all.

The gate used to be binary — `dangerous=True` meant ask, and the only way to
stop being asked was the persistent allowlist, matched on a command's first
word. "git" as an allowlist entry silences `git push` as thoroughly as `git
status`, which is too coarse to be useful and too coarse to be safe.

Three verdicts now, decided per command:

  DENY   never runs, and is never even put to the owner. The same shape as the
         `.env` refusal: approving a command is not consent to what it does, so
         some things should not be approvable at all.

         **DENY is a guardrail against accidents, not a sandbox.** It is string
         and token matching over a shell line, and a shell has more ways to
         spell a thing than any matcher has patterns: `rm -rf "/"` and `rm -rf
         / --no-preserve-root` both fall through to ASK rather than DENY, which
         is safe but is not the same as being caught. The value is that the
         obvious catastrophic typo cannot be approved by a tired owner at
         11pm — not that a determined agent could not get past it. The real
         boundary is still filesystem permissions and the fact that `sudo`
         cannot be answered from a tool call at all.
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
    (re.compile(r"\bchown\b[^|;&]*\s-[a-zA-Z]*R[^|;&]*\s+/\s*$"), "chown -R on the filesystem root"),
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

# Read-only staples. They turn up as segments of a compound command, and one
# `ls` should not drag the whole line into an approval prompt.
#
# **This list is not shell.py's READ_ONLY, and copying that one here was a bug**
# (found 2026-08-10, a day after shipping). That set is safe *in its own
# context*: `run_readonly` separately refuses every shell operator, so `tee`
# has nothing to write through and `echo` has no redirect. Lifted out of that
# context into a rule that auto-approves, the same names became
# `echo pwned > ~/.bashrc` and `tee /etc/hosts` running with nobody asked.
#
# So: no binary whose ordinary use writes. `tee` and `awk` are gone (awk has
# `system()` and `print > file`); `sed` and `find` stay but only in their
# read-only forms — see `_WRITES_ANYWAY`.
_READONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "file", "stat",
    "echo", "pwd", "which", "true", "test", "sort", "uniq", "cut",
    "date", "printf", "dirname", "basename", "sed", "find",
}

# Flags that turn a read-only staple into a write. Checked per stem, because
# the destructive form is the flag, not the binary.
_WRITES_ANYWAY: dict[str, tuple[str, ...]] = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
             "-fprintf", "-fls"),
    "sed": ("-i", "--in-place"),
}

# Wrappers that run *another* command. Judging the wrapper is judging nothing:
# `env sh -c '…'` and `nohup rm -rf /` are the inner command wearing a hat, and
# the allow list would have waved both through on the strength of `env` and
# `nohup`.
_WRAPPERS = {
    "env", "nohup", "timeout", "setsid", "stdbuf", "nice", "ionice",
    "command", "exec", "xargs", "time", "watch",
}

# Redirection writes to a path no allow list ever looked at, so a segment
# containing one is never auto-approved. Matched as a *token* after shlex
# splitting, so a `>` inside a quoted commit message does not trip it.
_REDIRECTS = {">", ">>", "&>", "&>>", "2>", "2>>", ">|", "1>", "1>>"}

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

# Directories where loosening permissions is never routine.
_SENSITIVE_PERM_PATH = re.compile(
    r"(^|/)\.(ssh|gnupg|aws|config/jarvis)(/|$)|(^|/)\.env(\.|$)|authorized_keys|id_(rsa|ed25519)"
)


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


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].lower()


def _unwrap(tokens: list[str]) -> list[str]:
    """Strip leading assignments and command wrappers to reach the real command.

    `FOO=1 nohup timeout 5 python -c '…'` has to be judged as the python run it
    is, not as an assignment, a nohup or a timeout. Bounded so a pathological
    line cannot loop.
    """
    for _ in range(5):
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]
        if not tokens or _basename(tokens[0]) not in _WRAPPERS:
            return tokens
        rest = tokens[1:]
        # Drop the wrapper's own flags and bare durations (`timeout 5 …`).
        while rest and (rest[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", rest[0])):
            rest = rest[1:]
        if not rest:
            return tokens
        tokens = rest
    return tokens


def _stem(tokens: list[str]) -> str:
    return _basename(tokens[0]) if tokens else ""


def _first_word_arg(tokens: list[str]) -> str:
    """The first non-flag argument — a subcommand, if the tool has them."""
    for token in tokens[1:]:
        if not token.startswith("-"):
            return token.lower()
    return ""


def _judge_segment(segment: str) -> Verdict:
    raw = _tokens(segment)
    tokens = _unwrap(list(raw))
    stem = _stem(tokens)
    if not stem:
        return Verdict(ASK, "could not parse the command")

    # Escalation is checked across every token, not just the stem: `echo x |
    # sudo tee /etc/hosts` puts sudo in the middle of the line.
    for token in raw:
        base = _basename(token)
        if base in _ESCALATION:
            return Verdict(DENY, f"{base} runs as another user, outside every gate here")
        if base in _DESTRUCTION or base.startswith("mkfs."):
            return Verdict(DENY, f"{base} has no recoverable failure mode")

    for pattern, why in _DENY_PATTERNS:
        if pattern.search(segment):
            return Verdict(DENY, why)

    # A redirect writes to a path nothing in the allow list ever examined, so
    # the stem stops being a useful thing to judge: `echo` is harmless right up
    # until `echo pwned > ~/.bashrc`.
    if any(token in _REDIRECTS or token.startswith(">") for token in raw):
        return Verdict(ASK, "redirects output to a file")

    flags = _WRITES_ANYWAY.get(stem, ())
    if flags and any(t == f or t.startswith(f) for t in tokens[1:] for f in flags):
        return Verdict(ASK, f"{stem} with {flags[0]} writes, and is not a read")

    # chmod/chown are on the allow list because moving files around is ordinary
    # work, but two shapes are not ordinary and are silent when they go wrong:
    # world-readable permissions on a key directory, and a recursive 777. ssh
    # in particular *fails closed* on loose permissions, so the damage shows up
    # later and somewhere else.
    if stem in ("chmod", "chown"):
        if any(_SENSITIVE_PERM_PATH.search(t) for t in tokens[1:]):
            return Verdict(ASK, f"{stem} on a credential directory")
        if any(t in ("777", "666", "a+rwx", "a=rwx") for t in tokens[1:]):
            return Verdict(ASK, "world-writable permissions")

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
