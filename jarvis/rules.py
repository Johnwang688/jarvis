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
# The same flags in their *attached* spelling. shlex merges the quoted argument
# into the flag token — `python -c"import os"` becomes `['python', '-cimport
# os']` — so an exact-token test saw no `-c` at all and the stem's ALLOW stood.
# The comment above says allowing the stem must not allow the language; without
# this, one deleted space allowed the language.
_INLINE_ATTACHED = ("-c", "-e")
_INTERPRETERS = {"python", "python3", "node", "deno", "bun", "perl", "ruby", "php",
                 "sh", "bash", "zsh", "ksh", "dash"}

_URL = re.compile(r"https?://[^\s'\"|;&)]+")

# Directories where loosening permissions is never routine.
_SENSITIVE_PERM_PATH = re.compile(
    r"(^|/)\.(ssh|gnupg|aws|config/jarvis)(/|$)|(^|/)\.env(\.|$)|authorized_keys|id_(rsa|ed25519)"
)


# Longest first: `||` must beat `|`, and `&&` must beat `&`.
_SEPARATORS = ("||", "&&", ";", "|", "\n", "&")


def urls(command: str) -> list[str]:
    return _URL.findall(command)


def segments(command: str) -> list[str]:
    """Split a command line into the individual commands it will actually run.

    **A bare `&` separates two commands exactly as `;` does**, and this used to
    miss it — so `ls & rm -rf ~/work` came back as *one* segment, `rm -rf
    ~/work` was read as arguments to `ls`, and the whole line was judged ALLOW
    on the strength of the stem `ls`. That is the module's headline invariant
    ("every segment is judged, and the worst verdict wins") failing on the
    cheapest possible spelling. `&&` is tried before `&` so it still splits as
    one token.

    **`&` is only a separator when it is not part of a redirect** (fixed
    2026-08-17). `2>&1` was being cut into `2>` and `1`, which made the first
    half look like a redirect to a file and the second half like a command
    named `1` — so `make build 2>&1` asked for approval twice over. A `&`
    immediately after a `>` belongs to that redirect, and a `&` immediately
    before one is the `&>` operator; neither starts a new command.

    The split is **quote-aware**, which it had to become in the same change.
    Splitting the raw string on `&` turns `git commit -m 'fix A & B'` into two
    segments and drags an ordinary commit into an approval prompt — and the
    same flaw was already there for `;` and `|`, just rarer. A separator inside
    quotes is text the shell will never treat as a separator, so judging it as
    one is wrong in both directions. If the quotes do not balance the scan is
    not trustworthy, so it falls back to the naive split, which over-segments
    rather than under-segments.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        else:
            hit = next((s for s in _SEPARATORS if command.startswith(s, i)), "")
            if hit == "&" and (
                "".join(buf).rstrip().endswith(">") or command.startswith("&>", i)
            ):
                hit = ""  # part of `2>&1` / `&>file`, not a command separator
            if hit:
                parts.append("".join(buf))
                buf = []
                i += len(hit)
                continue
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    if quote:
        parts = re.split(r"\|\||&&|;|\||\n|&", command)
    return [p.strip() for p in parts if p.strip()]


def unquoted_indices(text: str, *, double_is_quote: bool = True) -> list[int]:
    """Indices of the characters the shell will read as syntax, not as text.

    shlex tokens cannot answer this. `echo pwned>~/.bashrc` splits into
    `['echo', 'pwned>~/.bashrc']`, so a check that looked for a token that *is*
    or *starts with* a redirect saw nothing — and the 2026-08-10 fix that
    stopped `echo pwned > ~/.bashrc` running unasked was reachable again by
    deleting one space. Scanning the raw text with quote state is the only way
    to tell a redirect from a `>` inside a commit message.

    **Public, and deliberately the only implementation of this idea in the
    codebase.** `tools/shell.py` needs exactly the same answer for its own
    operator check, and a second hand-written copy is how `run_readonly` ended
    up refusing `grep 'a&b' f.txt` on the same day `segments()` learned not to
    (2026-08-17). One scanner, two callers.

    `double_is_quote=False` **reports** what is inside double quotes instead of
    skipping it. That is not a loosening — it is for the things a double quote
    does *not* protect: the shell still expands `$(...)` and backticks in
    there, so "inside quotes" is not a reason to stop looking for them.

    It is emphatically **not** "treat `"` as an ordinary character", which is
    how it was first written (2026-08-17) and which opened a live hole in the
    ungated `run_readonly` the same day. A `'` inside a double-quoted string is
    an apostrophe, not a quote — but that spelling let it open a single-quote
    region that ran to the end of the line, so

        grep "it's $(touch /tmp/PWNED)" f
        echo "don't `touch /tmp/PWNED`"

    hid their substitution from the scan and *executed it*, with no approval
    asked, on a tool that is `dangerous=False`. The lesson is the one this file
    already carries about `_READONLY`: **a scanner is only correct together
    with the quoting rules it models.** So both quote kinds are always tracked
    with real nesting (a `'` inside `"…"` is text, and a `"` inside `'…'` is
    text); `double_is_quote` decides only whether the *contents* of a
    double-quoted run are reported, never whether `"` still delimits one.
    """
    out: list[int] = []
    quote = ""
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and not double_is_quote:
                out.append(i)
            continue
        if ch in "'\"":
            quote = ch
            continue
        out.append(i)
    return out


def first_unquoted(
    text: str, needles: tuple[str, ...], *, double_is_quote: bool = True
) -> str:
    """The first of `needles` that appears outside quotes, or ''."""
    ordered = sorted(needles, key=len, reverse=True)
    for i in unquoted_indices(text, double_is_quote=double_is_quote):
        for needle in ordered:
            if text.startswith(needle, i):
                return needle
    return ""


def _unquoted_positions(segment: str, chars: str) -> bool:
    """True if any of `chars` appears in `segment` outside quotes."""
    return bool(first_unquoted(segment, tuple(chars)))


def redirects_to_file(segment: str) -> bool:
    """True if this segment sends output to a *path*.

    A real redirect is never auto-approved, and that is deliberate: it writes
    somewhere no allow list ever looked at, so the stem stops being a useful
    thing to judge — `echo` is harmless right up until `echo pwned >
    ~/.bashrc`. Both the spaced and the unspaced spelling must keep asking.

    But a bare scan for `>` is not that check, and it was the one shipped
    (2026-08-17). Two shapes carry a `>` and redirect nothing to any file:

      * **file-descriptor duplication** — `2>&1`, `1>&2`, `2>&-`. These aim one
        descriptor at another, or close it; no path is named and nothing is
        created. `make build 2>&1`, `npm run build 2>&1` and `pytest -x 2>&1`
        are about as ordinary as commands get, and all three had started
        asking. "A fix that makes ordinary work ask is a different bug, not a
        smaller one."
      * **a `>` inside quotes** — `git commit -m 'fix a > b'`, `echo 'a > b'`.
        The shell will never treat that as syntax, so neither should this.

    `&>file` and `&>>file` are *not* excluded: the `&` there precedes the `>`
    and a filename still follows, so they are real redirects. Nor is
    `2>/dev/null` — it names a path, and this function answers a syntactic
    question rather than guessing which paths are harmless.
    """
    syntax = set(unquoted_indices(segment))
    i = 0
    while i < len(segment):
        if i not in syntax or segment[i] != ">":
            i += 1
            continue
        j = i + 1
        if segment[j : j + 1] == ">":  # `>>`
            j += 1
        if segment[j : j + 1] == "|":  # `>|`, the noclobber override
            j += 1
        while segment[j : j + 1] in (" ", "\t"):
            j += 1
        # `>&1` / `>&-`: a descriptor number or a close, not a filename. Note
        # that bash's `>&word` with a *non*-numeric word is `&>word` — a real
        # redirect of both streams to a file — so only digits and `-` count.
        if segment[j : j + 1] == "&":
            k = j + 1
            while segment[k : k + 1].isdigit():
                k += 1
            if k > j + 1:
                # Resume *at* k, not past it: `2>&1>out.log` puts a real
                # redirect immediately after the duplication, and skipping one
                # more character stepped straight over it.
                i = k
                continue
            if segment[k : k + 1] == "-":
                i = k + 1
                continue
        return True
    return False


def unwrap(tokens: list[str]) -> list[str]:
    """Public `_unwrap`: the real command inside its wrappers and assignments.

    Exported because `tools/shell.py` needs the same answer this module needs —
    an allowlist that judges `env` instead of the `sh -c '…'` it is wrapping is
    judging nothing, and two copies of that idea is how they drifted apart.
    """
    return _unwrap(list(tokens))


def command_targets(command: str) -> list[tuple[str, str]]:
    """Per segment, the command that runs — as a stem *and* as written.

    Both spellings come back because two callers want different ones. Judging a
    command wants the stem: `/usr/bin/git push` is a `git push` however it was
    spelled, and a rule that missed that would be trivially dodged. Matching
    an allowlist the **owner wrote by hand** wants the text they wrote.

    Basenaming everything (2026-08-17) quietly killed every full-path entry in
    the owner's real allowlist — `{"prefix": "/usr/local/bin/mytool"}` stopped
    covering `/usr/local/bin/mytool run`, and the owner's live file carries a
    full-path `powershell.exe` entry. An allowlist that silently stops matching
    is worse than one that never matched: the owner does not find out from the
    file, they find out from being asked again about something they settled
    months ago.

    Returns an **empty list** when the line contains command substitution: a
    `$(...)` runs something this function never sees, so there is no honest
    enumeration of what the line does. Callers treat empty as "cannot be
    covered by any allowlist", which is why it must not be confused with "no
    commands here".
    """
    if not command or not command.strip():
        return []
    if "$(" in command or "`" in command:
        return []
    targets: list[tuple[str, str]] = []
    for segment in segments(command):
        tokens = _unwrap(_tokens(segment))
        if not tokens:
            return []
        targets.append((_basename(tokens[0]), tokens[0]))
    return targets if targets and all(stem for stem, _ in targets) else []


def command_stems(command: str) -> list[str]:
    """The resolved stem of every command a line will run, or [] if unknowable.

    "Resolved" means wrappers and leading assignments are stripped and the path
    is reduced to a basename, so `FOO=1 nohup /usr/bin/git push` answers `git`
    — the command that actually runs, not the costume it arrived in.
    """
    return [stem for stem, _ in command_targets(command)]


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


def writes_anyway(tokens: list[str]) -> str:
    """The flag that turns a read-only staple into a write, or ''.

    Public because `run_readonly` needs the same answer (2026-08-17) and a
    second copy of this list is how the two files disagree. `find` is on that
    tool's allowlist because searching is a read — and `find . -exec rm -rf {}
    \\;` is on the same allowlist, wearing the same name. The `\\;` had been
    caught only by accident, as a raw `;` in an operator scan; the moment that
    scan became quote- and escape-aware (correctly — the shell does not treat
    an escaped `;` as a separator either) the accident stopped happening, and
    `-delete` and `-exec … +` had never been caught at all.
    """
    stem = _stem(tokens)
    for flag in _WRITES_ANYWAY.get(stem, ()):
        if any(t == flag or t.startswith(flag) for t in tokens[1:]):
            return flag
    return ""


def _has_inline_source(tokens: list[str]) -> bool:
    """True if an interpreter was handed source on the command line."""
    for token in tokens[1:]:
        if token in _INLINE_CODE_FLAGS:
            return True
        if any(token.startswith(f) and len(token) > len(f) for f in _INLINE_ATTACHED):
            return True
    return False


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
    # until `echo pwned > ~/.bashrc`. See `redirects_to_file` for the two
    # shapes that carry a `>` and redirect nothing.
    if redirects_to_file(segment):
        return Verdict(ASK, "redirects output to a file")

    writing = writes_anyway(tokens)
    if writing:
        return Verdict(ASK, f"{stem} with {writing} writes, and is not a read")

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

    if stem in _INTERPRETERS and _has_inline_source(tokens):
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
