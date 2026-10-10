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
from dataclasses import dataclass, field
from typing import Callable

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
# `system()` and `print > file`). The rest stay only in their read-only forms,
# and which forms those are is `READER_HAZARDS` (below `redirections`): the
# one table, per stem, of the options, operands and script commands that make
# a reader write, run something, or read past its operands.
_READONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "file", "stat",
    "echo", "pwd", "which", "true", "test", "sort", "uniq", "cut",
    "date", "printf", "dirname", "basename", "sed", "find",
}

# Wrappers that run *another* command. Judging the wrapper is judging nothing:
# `env sh -c '…'` and `nohup rm -rf /` are the inner command wearing a hat, and
# the allow list would have waved both through on the strength of `env` and
# `nohup`.
_WRAPPERS = {
    "env", "nohup", "timeout", "setsid", "stdbuf", "nice", "ionice",
    "command", "exec", "xargs", "time", "watch",
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


def join_continuations(text: str) -> str:
    """The line as bash reads it, with every backslash-newline pair removed.

    **bash deletes `\\<newline>` before it splits anything** (outside single
    quotes and comments), so a continuation can sit in the middle of any word
    or operator and the shell never sees it. Nothing here knew that, and every
    scanner read the pair as text (2026-10-09): a continuation between `$` and
    `(` hid a command substitution from `decide()` *and* from `run_readonly`,
    which then ran it with nobody asked; one in front of an operand turned a
    copy onto the allowlist into a copy onto a path that starts with a
    newline — ALLOW, and unseen by the gate-state check.

    Every judge calls this first, so they all read the line bash will run.
    A comment is copied verbatim to its newline: joining a comment's trailing
    backslash with the next line would fold that line *into* the comment,
    which bash does not do, and hide it from every judge.
    """
    if "\\\n" not in text:
        return text
    out: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if ch == "\\":
            if text[i + 1:i + 2] == "\n":
                i += 2
                continue
            out.append(text[i:i + 2])
            i += 2
            continue
        if not quote and ch == "#" and (i == 0 or text[i - 1] in " \t\n;|&()"):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(text[i:end])
            i = end
            continue
        if ch == '"':
            quote = "" if quote else '"'
        elif ch == "'" and not quote:
            quote = "'"
        out.append(ch)
        i += 1
    return "".join(out)


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

    **A `|` straight after `>` is the noclobber redirect `>|`, not a pipe**
    (2026-10-09). Splitting it cut `echo x >| FILE` into `echo x >` and a
    "command" named FILE, so the gate-state check never saw FILE as a
    redirect target at all.

    Both exceptions need the `>` to be **real syntax, right beside it**: an
    escaped `\\>` is a literal character, so `echo \\>&X` and `echo \\>|X` are
    an echo followed by a second command X — which the old test (does the
    text so far end in `>`, spaces allowed?) glued onto the echo and judged
    as its arguments.
    """
    command = join_continuations(command)
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    after_gt = False      # the last character taken was an unquoted, unescaped `>`
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
            after_gt = False
        elif ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            after_gt = False
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
            after_gt = False
        else:
            hit = next((s for s in _SEPARATORS if command.startswith(s, i)), "")
            if hit == "&" and (after_gt or command.startswith("&>", i)):
                hit = ""  # part of `2>&1` / `&>file`, not a command separator
            elif hit == "|" and after_gt:
                hit = ""  # `>|`, the noclobber redirect
            if hit:
                parts.append("".join(buf))
                buf = []
                i += len(hit)
                after_gt = False
                continue
            buf.append(ch)
            after_gt = ch == ">"
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


def has_substitution(command: str) -> bool:
    """True if the line runs a command this module never sees.

    `$(…)` and backticks are matched anywhere, quoted or not — the shell
    expands both inside double quotes, and over-matching only asks. Process
    substitution `<(…)`/`>(…)` joined them on 2026-10-09: it runs its
    command exactly as `$(…)` does, and `cat <(PROGRAM)` was an ALLOW on the
    strength of `cat`, with PROGRAM never judged. It is matched only
    unquoted, because bash does not expand it inside quotes and a regex like
    `grep '<(div|span)'` is ordinary.
    """
    if "$(" in command or "`" in command:
        return True
    return bool(first_unquoted(command, ("<(", ">(")))


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

    Since 2026-10-09 this is `redirections()`, the one reading of bash's
    redirect grammar in the codebase; see there for what it covers.
    """
    return any(r.writes for r in redirections(join_continuations(segment)))


@dataclass(frozen=True)
class Redirect:
    """One redirection on a command line, as bash will perform it.

    `word` is the target as written, quotes kept ('' when there is none);
    `writes` means it opens a path for output. `start`/`end` span the whole
    redirection — an fd number or `{name}` in front included — so it can be
    cut out of the line before the command's own arguments are read.
    """
    op: str
    word: str
    writes: bool
    start: int
    end: int


# Operators longest first, so `<<<` beats `<<` beats `<`, and `>>` beats `>`.
_REDIRECT_OPS = ("<<<", "<<-", "<<", "<>", "<&", "<", ">>", ">|", ">&", ">")
# Opening a path for output: plain, append, clobber, read-write and both-streams.
_WRITE_OPS = frozenset({">", ">>", ">|", "<>", "&>", "&>>"})
_WORD_STOP = " \t\n;|&<>()"
_DUP_WORD = re.compile(r"\d+-?|-")


def redirections(segment: str) -> list[Redirect]:
    """Every redirection in one segment, read with bash's grammar.

    **One implementation, two callers** (2026-10-09). `redirects_to_file`
    here and the gate-state check in `protected_state.py` each had a scan of
    their own, and the gate's copy had drifted: it skipped one character too
    many after a duplication, so in `2>&1>FILE` and `>&2>FILE` it stepped
    over the second `>` and never saw FILE; it read `>& FILE` (a space after
    the `&`) as a redirect to nothing; and `segments()` cut `>|` in half.
    Each of those let a write onto the allowlist through the mandatory
    refusal, so under `--dangerously-skip-permissions` it ran.

    What bash writes to, all `writes=True`: `[n]>word`, `[n]>>word`,
    `[n]>|word` (noclobber override), `[n]<>word` (opened read-write, and
    created if missing), `&>word`, `&>>word`, and `[n]>&word` when *word* is
    not a descriptor — bash's `>&file` is `&>file`. `{name}` in place of `n`
    is the same redirect with bash choosing the descriptor.

    What is not a write: `>&N`, `>&N-` and `>&-` (duplicate, move or close a
    descriptor — `2>&1`); input `<`, `<&`; here-documents `<<`/`<<-`; and
    here-strings `<<<`. The word after any operator may be glued on, spaced
    off, or quoted; a `>` inside quotes or behind a backslash is text.
    `<(…)`/`>(…)` are process substitutions, not redirections, and `decide()`
    treats them as substitutions.

    Scanning resumes at the end of each redirection's word — never past it —
    so a redirect written immediately after another is still seen.
    """
    syntax = set(unquoted_indices(segment))
    found: list[Redirect] = []
    i, n = 0, len(segment)
    while i < n:
        if i not in syntax or segment[i] not in "<>":
            i += 1
            continue
        if segment[i + 1:i + 2] == "(":
            i += 1
            continue
        op = next(o for o in _REDIRECT_OPS if segment.startswith(o, i))
        j = i + len(op)
        start = i
        if op in (">", ">>") and i > 0 and segment[i - 1] == "&" and (i - 1) in syntax:
            op, start = "&" + op, i - 1
        else:
            k = i
            while k > 0 and segment[k - 1].isdigit():
                k -= 1
            if k < i and _word_starts(segment, k):
                start = k
            elif i > 0 and segment[i - 1] == "}":
                m = segment.rfind("{", 0, i)
                if m != -1 and re.fullmatch(r"\{\w+\}", segment[m:i]) and _word_starts(segment, m):
                    start = m
        while j < n and segment[j] in " \t":
            j += 1
        w0, quote = j, ""
        while j < n:
            ch = segment[j]
            if quote:
                if ch == quote:
                    quote = ""
                elif ch == "\\" and quote == '"':
                    j += 1
            elif ch == "\\":
                j += 1
            elif ch in "'\"":
                quote = ch
            elif ch in _WORD_STOP:
                break
            j += 1
        j = min(j, n)
        word = segment[w0:j]
        if op in _WRITE_OPS:
            writes = True
        elif op == ">&":
            writes = not _DUP_WORD.fullmatch(word.strip("'\""))
        else:
            writes = False
        found.append(Redirect(op, word, writes, start, j))
        i = max(j, i + len(op))
    return found


def _word_starts(text: str, k: int) -> bool:
    """True if a word starts at k: the line's start, or after a blank or an
    operator — but not after the `&` of a `>&`/`<&`, whose digits are the
    duplication's own word (`2>&1>f`: the `1` is not an fd for `>f`)."""
    if k == 0:
        return True
    before = text[k - 1]
    if before == "&":
        return k < 2 or text[k - 2] not in "<>"
    return before in " \t\n;|("


def strip_redirections(segment: str) -> str:
    """The segment with every redirection cut out, so what is left is the
    command and its own arguments. Quote-aware, unlike stripping tokens after
    shlex, which cannot tell a quoted `'>name'` argument from a redirect."""
    spans = [(r.start, r.end) for r in redirections(segment)]
    if not spans:
        return segment
    out, last = [], 0
    for start, end in spans:
        if start < last:
            start = last
        out.append(segment[last:start])
        out.append(" ")
        last = max(last, end)
    out.append(segment[last:])
    return "".join(out)


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
    command = join_continuations(command or "")
    if not command.strip():
        return []
    if has_substitution(command):
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


# --- readers that write, run something, or read past their operands ----------
#
# **One table, three callers** (2026-10-09). A "read-only" program is only
# read-only in some of its forms, and three places hold such programs:
# `_READONLY` above (auto-ALLOW), `protected_state._READERS` (naming a gate
# file to one is a read, not a write) and `tools/shell.py`'s `READ_ONLY` (the
# ungated `run_readonly`, where the allowlist *is* the boundary). Each used to
# carry its own idea of which forms write — `rules` had find/sed/rg flags,
# `protected_state` added `sort -o` on its own, `run_readonly` had nothing for
# `tree -o`, `ss -D`, `date -s` or `env -S` — and the more permissive copy
# decided. Now each stem's hazards live here, keyed by stem, and all three
# ask `reader_hazard()`.
#
# Per stem, because the grammars differ and one global rule is wrong for
# somebody: a prefix match is right for `sed -i.bak` and wrong for rg, whose
# `--pre` prefix caught `--pretty` (PR #23); find's predicates are whole words,
# so `-executable` is not `-exec`; getopt_long programs accept any unique
# prefix of a long option (`sort --out=F` is `--output=F`), ripgrep accepts
# none. Every stem in the three sets is either here or in `_NO_HAZARD`, which
# records that its options were audited and none writes, runs or reads past
# its operands; `tests/rules_check.py` fails if a set gains a stem in neither.


@dataclass(frozen=True)
class Hazard:
    """Why a reader, in this form, is not a read.

    `reason` completes a sentence that starts with the program's name.
    `writes` lists files it will write when they can only be read out of a
    script (a sed `w` command's file is inside one shlex token, invisible to
    anything that looks at operands). `runs` means the line carries text the
    program executes, so a gate file named in it is treated like inline
    interpreter source.
    """
    reason: str
    writes: tuple[str, ...] = ()
    runs: bool = False


@dataclass(frozen=True)
class _Getopt:
    """How one program reads its options, as far as the hazard check needs.

    Modelled on getopt_long (GNU, iproute2, net-tools) and clap (uutils, which
    is what coreutils are on this machine): a short cluster is read letter by
    letter until one that takes a value; `--` ends the options; a long option
    may be shortened to any prefix, so **any** prefix of a dangerous long name
    counts as that name (an ambiguous one only makes the program fail).

    `arg_shorts` must be exact. A letter listed here that takes no value would
    end the scan early and hide a dangerous letter behind it, which is the
    permissive direction; a value letter left out only makes a glued value's
    letters look like options, which can only add a refusal.
    """
    arg_shorts: str = ""                 # take a value, glued or the next word
    optional_shorts: str = ""            # take a value only when glued (`-I[FMT]`)
    danger_shorts: dict = field(default_factory=dict)
    arg_longs: frozenset = frozenset()   # exact names whose value may be the next word
    danger_longs: dict = field(default_factory=dict)
    whole_cluster: bool = False          # tree: letters after a value letter still count
    max_operands: int | None = None      # uniq/xxd: an operand past this many is written
    operand_danger: Callable[[str], bool] | None = None
    operand_reason: str = ""

    def __call__(self, args: list[str]) -> str:
        operands: list[str] = []
        i, ended = 0, False
        while i < len(args):
            token = args[i]
            i += 1
            if ended or token == "-" or not token.startswith("-"):
                operands.append(token)
                continue
            if token == "--":
                ended = True
                continue
            if token.startswith("--"):
                name, eq, _ = token[2:].partition("=")
                for long, why in self.danger_longs.items():
                    if name and long.startswith(name):
                        return why
                if name in self.arg_longs and not eq:
                    i += 1
                continue
            letters = token[1:]
            for k, letter in enumerate(letters):
                if letter in self.danger_shorts:
                    return self.danger_shorts[letter]
                if letter in self.optional_shorts:
                    break
                if letter in self.arg_shorts:
                    if self.whole_cluster:
                        i += 1
                        continue
                    if k == len(letters) - 1:
                        i += 1
                    break
        if self.operand_danger is not None and any(self.operand_danger(o) for o in operands):
            return self.operand_reason
        if self.max_operands is not None and len(operands) > self.max_operands:
            return self.operand_reason
        return ""


_FIND_HAZARDS = {
    "-delete": "-delete deletes files",
    "-exec": "-exec runs a program", "-execdir": "-execdir runs a program",
    "-ok": "-ok runs a program", "-okdir": "-okdir runs a program",
    "-fprint": "-fprint writes a file", "-fprint0": "-fprint0 writes a file",
    "-fprintf": "-fprintf writes a file", "-fls": "-fls writes a file",
}


def _find_hazard(args: list[str]) -> str:
    """find's predicates are whole words: `-executable` (a test of the mode
    bits) is not `-exec`, which the old prefix match made it."""
    return next((_FIND_HAZARDS[t] for t in args if t in _FIND_HAZARDS), "")


# ripgrep runs a program of the caller's choosing over every file it searches
# (a preprocessor), or to ask for the hostname — the shape git had (2026-10-09):
# allowlisted for what it does by default, with a flag that runs a program.
# Matched whole, `--pre` or `--pre=X`: ripgrep refuses an abbreviated flag
# (15.1.0: "unrecognized flag --pr"), so `--pretty` and `--pre-glob` are reads.
_RG_HAZARDS = {"--pre": "--pre runs a program on every file it searches",
               "--hostname-bin": "--hostname-bin runs a program"}


def _rg_hazard(args: list[str]) -> str:
    return next((_RG_HAZARDS[t.split("=", 1)[0]] for t in args
                 if t.split("=", 1)[0] in _RG_HAZARDS), "")


def _less_hazard(args: list[str]) -> str:
    """less writes a log file (`-o`/`-O`/`--log-file`/`--LOG-FILE`), loads key
    files that can set LESSOPEN — a program run on every file (`-k`,
    `--lesskey-*`) — and runs its own commands at start-up (`+cmd`). Its
    clusters are not modelled letter by letter: any of those letters anywhere
    in a short option counts, which can only add a refusal."""
    for token in args:
        if token == "--":
            break
        if token.startswith("+"):
            return "+ runs less commands at start-up"
        if token.startswith("--"):
            name = token[2:].split("=", 1)[0]
            for long in ("log-file", "LOG-FILE", "lesskey-file", "lesskey-src",
                         "lesskey-content"):
                if name and long.startswith(name):
                    return f"--{long} writes a log or loads key bindings"
        elif token.startswith("-") and any(c in token[1:] for c in "oOk"):
            return "-o/-O write a log file and -k loads key bindings"
    return ""


_XXD_VALUE_WORDS = {"c": "cols", "g": "groupsize", "l": "len", "n": "name",
                    "o": "offset", "s": "seek", "R": "R"}


def _xxd_hazard(args: list[str]) -> str:
    """`xxd [options] [infile [outfile]]`: a second operand is written. xxd's
    options are single-dash words (`-c8`, `-c 8`, `-cols 8`), so a value is
    the next word when the option is spelled whole or as a prefix of its long
    word, and glued otherwise."""
    operands: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token == "-" or not token.startswith("-"):
            operands.append(token)
            continue
        if token == "--":
            operands += args[i:]
            break
        word = _XXD_VALUE_WORDS.get(token[1:2])
        if word is not None and (len(token) == 2 or word.startswith(token[1:])):
            i += 1
    return "writes its second operand, the output file" if len(operands) > 1 else ""


def _bat_hazard(args: list[str]) -> str:
    """bat runs a pager of the caller's choosing, and `bat cache` writes its
    cache. (bat is not installed here; from its documentation.)"""
    operands = [t for t in args if not t.startswith("-")]
    if operands[:1] == ["cache"]:
        return "cache writes bat's cache"
    for token in args:
        name = token[2:].split("=", 1)[0] if token.startswith("--") else ""
        if name and "pager".startswith(name):
            return "--pager runs a program"
    return ""


# sed's options, from `sed --help` / sed.info for GNU sed 4.9.
_SED_FLAGS = "nrEsuzb"             # short options with no value
_SED_LONGS = {                     # every long option -> takes a required value
    "expression": True, "file": True, "line-length": True, "in-place": False,
    "quiet": False, "silent": False, "debug": False, "follow-symlinks": False,
    "posix": False, "regexp-extended": False, "separate": False,
    "sandbox": False, "unbuffered": False, "null-data": False,
    "zero-terminated": False, "binary": False, "help": False, "version": False,
}
_SED_UNSURE = "is given an option this check does not model, so it is not provably a read"
# Commands that take no argument at all.
_SED_SIMPLE = frozenset("=dDgGhHnNpPxzF")
# `w /dev/stdout` and `w /dev/stderr` are special-cased by GNU sed: output,
# not a file. `sed -n 's/x/y/w /dev/stdout'` is an ordinary idiom.
_SED_STD_STREAMS = frozenset({"/dev/stdout", "/dev/stderr"})


def _sed_hazard(args: list[str]) -> str | Hazard:
    """sed is a read only when that is provable.

    **A sed script is a program** (finding 3, 2026-10-09). `w`/`W` and the
    `w` flag of `s` write files, `r`/`R` read files that are not operands,
    and on GNU sed `e` and the `e` flag of `s` run commands — and sed sat on
    the auto-ALLOW list with only `-i` checked, so all of those ran unasked,
    including a `w` aimed at the allowlist. `-i` was only seen as a word of
    its own or a prefix: `-ni`, `-Ei` and `--in-pl` (getopt_long takes any
    unique prefix) are in-place edits too.

    Provable means one of two things. Either every script sed will compile
    parses, with this scanner, as commands that do none of those things; or
    `--sandbox` (which GNU sed documents as rejecting e/r/w) is in force when
    the script is compiled. GNU sed compiles each `-e`/`-f` as it reads it, so
    only a `--sandbox` *before* one covers it; a script given as the first
    operand is compiled after all options, so `--sandbox` anywhere covers it.
    A `-f` script file cannot be read here, so without the sandbox it asks.

    The scanner follows GNU sed's own parser closely enough to be
    conservative where it is not exact: addresses (`N`, `$`, `first~step`,
    `/re/I`, `\\cREc`, `addr,+N`, `addr,~N`), `!`, `{ }`, `;` and newline
    separators, labels and branches, `s` and `y` with any delimiter and
    backslash escapes (brackets are not special, as in GNU sed), and the
    `a`/`i`/`c` text commands, whose text runs to the end of the line and may
    contain any letters — `1a write w x` appends text, it writes nothing.
    Anything it does not recognise is "not provably a read".
    """
    scripts: list[tuple[str, bool]] = []   # (script, sandboxed when compiled)
    operands: list[str] = []
    sandbox = in_place = from_file = False
    file_unboxed = False
    i, ended = 0, False
    while i < len(args):
        token = args[i]
        i += 1
        if ended or token == "-" or not token.startswith("-"):
            operands.append(token)
            continue
        if token == "--":
            ended = True
            continue
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            matches = ([o for o in _SED_LONGS if o == name]
                       or [o for o in _SED_LONGS if name and o.startswith(name)])
            if len(matches) != 1:
                return Hazard(_SED_UNSURE)
            option = matches[0]
            if _SED_LONGS[option] and not eq:
                if i >= len(args):
                    return Hazard(_SED_UNSURE)
                value, i = args[i], i + 1
            if option == "in-place":
                in_place = True
            elif option == "sandbox":
                sandbox = True
            elif option == "expression":
                scripts.append((value, sandbox))
            elif option == "file":
                from_file, file_unboxed = True, file_unboxed or not sandbox
            continue
        letters = token[1:]
        for k, letter in enumerate(letters):
            if letter == "i":                # `-i[SUFFIX]`: the rest is the suffix
                in_place = True
                break
            if letter in "efl":
                value = letters[k + 1:]
                if not value:
                    if i >= len(args):
                        return Hazard(_SED_UNSURE)
                    value, i = args[i], i + 1
                if letter == "e":
                    scripts.append((value, sandbox))
                elif letter == "f":
                    from_file, file_unboxed = True, file_unboxed or not sandbox
                break
            if letter not in _SED_FLAGS:
                return Hazard(_SED_UNSURE)
    if in_place:
        return Hazard("-i edits files in place")
    if from_file and file_unboxed:
        return Hazard("-f runs a script file this check cannot read")
    if not scripts and not from_file:
        if not operands:
            return ""
        scripts = [(operands[0], sandbox)]
    # GNU sed joins its -e fragments with newlines into one program, so they
    # are scanned joined: `-e 'a\\' -e 'text'` is one append.
    text = "\n".join(script for script, boxed in scripts if not boxed)
    if not text:
        return ""
    writes, reads, runs, parsed = _sed_script(text)
    if runs:
        return Hazard("script runs a command (an e command or the e flag of s)",
                      tuple(writes), runs=True)
    if writes:
        return Hazard("script writes a file (a w or W command, or the w flag of s)",
                      tuple(writes))
    if reads:
        return Hazard("script reads a file that is not an operand (an r or R command)")
    if not parsed:
        return Hazard("script could not be fully parsed, so it is not provably a read")
    return ""


def _sed_until(script: str, i: int, delim: str) -> int | None:
    """Index just past the closing `delim`, or None. GNU sed's rule: a
    backslash carries the next character, an unescaped newline ends the
    command unfinished, and brackets are not special."""
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            return None
        if ch == delim:
            return i + 1
        i += 1
    return None


def _sed_address(script: str, i: int, second: bool = False) -> int | None:
    """Index past the address at i (i itself if there is none), or None."""
    n = len(script)
    if i >= n:
        return None if second else i
    ch = script[i]
    if second and ch in "+~":
        j = i + 1
        while j < n and script[j].isdigit():
            j += 1
        return j if j > i + 1 else None
    if ch.isdigit():
        j = i
        while j < n and script[j].isdigit():
            j += 1
        if j < n and script[j] == "~":
            k = j = j + 1
            while j < n and script[j].isdigit():
                j += 1
            if j == k:
                return None
        return j
    if ch == "$":
        return i + 1
    if ch in "/\\":
        if ch == "\\":
            if i + 1 >= n or script[i + 1] in "\n\\":
                return None
            delim, j = script[i + 1], i + 2
        else:
            delim, j = "/", i + 1
        end = _sed_until(script, j, delim)
        if end is None:
            return None
        while end < n and script[end] in "IM":
            end += 1
        return end
    return None if second else i


def _sed_script(script: str) -> tuple[list[str], list[str], bool, bool]:
    """(files written, files read, runs a command, parsed cleanly)."""
    writes: list[str] = []
    reads: list[str] = []
    runs = False
    n, i, depth = len(script), 0, 0
    fail = (writes, reads, runs, False)

    def to_eol(k: int) -> tuple[str, int]:
        end = script.find("\n", k)
        return (script[k:], n) if end == -1 else (script[k:end], end + 1)

    def end_of_command(k: int) -> int | None:
        while k < n and script[k] in " \t":
            k += 1
        if k >= n:
            return n
        if script[k] in ";\n":
            return k + 1
        if script[k] in "}#":
            return k
        return None

    while i < n:
        if script[i] in " \t\n;":
            i += 1
            continue
        if script[i] == "#":
            _, i = to_eol(i)
            continue
        j = _sed_address(script, i)
        if j is None:
            return fail
        if j > i:
            k = j
            while k < n and script[k] in " \t":
                k += 1
            if k < n and script[k] == ",":
                k += 1
                while k < n and script[k] in " \t":
                    k += 1
                j = _sed_address(script, k, second=True)
                if j is None:
                    return fail
        while j < n and script[j] in " \t":
            j += 1
        if j < n and script[j] == "!":
            j += 1
            while j < n and script[j] in " \t":
                j += 1
        if j >= n:
            return fail
        command, i = script[j], j + 1
        end: int | None
        if command == "{":
            depth += 1
            continue
        if command == "}":
            depth -= 1
            if depth < 0:
                return fail
            end = end_of_command(i)
        elif command in _SED_SIMPLE:
            end = end_of_command(i)
        elif command in "qQlL":
            while i < n and script[i] in " \t":
                i += 1
            while i < n and script[i].isdigit():
                i += 1
            end = end_of_command(i)
        elif command == "v":
            while i < n and script[i] in " \t":
                i += 1
            while i < n and (script[i].isdigit() or script[i] == "."):
                i += 1
            end = end_of_command(i)
        elif command in ":btT":
            k = i
            while k < n and script[k] not in ";\n":
                k += 1
            label = script[i:k].strip()
            if (command == ":" and not label) or any(c in " \t" for c in label):
                return fail
            end = k
        elif command in "aic":
            while i < n:                     # to the first unescaped newline
                if script[i] == "\\":
                    i += 2
                    continue
                if script[i] == "\n":
                    i += 1
                    break
                i += 1
            end = min(i, n)
        elif command in "rRwW":
            name, end = to_eol(i)
            name = name.strip()
            if not name:
                return fail
            if command in "rR":
                reads.append(name)
            elif name not in _SED_STD_STREAMS:
                writes.append(name)
        elif command == "e":
            runs = True
            _, end = to_eol(i)
        elif command in "sy":
            if i >= n or script[i] in "\n\\":
                return fail
            delim = script[i]
            k = _sed_until(script, i + 1, delim)
            k = _sed_until(script, k, delim) if k is not None else None
            if k is None:
                return fail
            end = None
            if command == "s":
                while k < n and (script[k] in "gpiImMe" or script[k].isdigit()):
                    runs = runs or script[k] == "e"
                    k += 1
                if k < n and script[k] == "w":
                    name, end = to_eol(k + 1)
                    name = name.strip()
                    if not name:
                        return fail
                    if name not in _SED_STD_STREAMS:
                        writes.append(name)
            if end is None:
                end = end_of_command(k)
        else:
            return fail
        if end is None:
            return fail
        i = end
    return writes, reads, runs, depth == 0


READER_HAZARDS: dict[str, Callable[[list[str]], "str | Hazard"]] = {
    "sed": _sed_hazard,
    "find": _find_hazard,
    "rg": _rg_hazard,
    "less": _less_hazard,
    "xxd": _xxd_hazard,
    "bat": _bat_hazard,
    "batcat": _bat_hazard,
    "sort": _Getopt(
        arg_shorts="ktST",
        danger_shorts={"o": "-o writes its output to a file"},
        arg_longs=frozenset({"key", "field-separator", "buffer-size", "temporary-directory",
                             "batch-size", "files0-from", "parallel", "random-source", "sort"}),
        danger_longs={"output": "--output writes its output to a file",
                      "compress-program": "--compress-program runs a program"}),
    "uniq": _Getopt(
        arg_shorts="fsw", optional_shorts="D",
        arg_longs=frozenset({"skip-fields", "skip-chars", "check-chars"}),
        max_operands=1, operand_reason="writes its second operand, the output file"),
    "date": _Getopt(
        arg_shorts="dfr", optional_shorts="I",
        danger_shorts={"s": "-s sets the system clock"},
        arg_longs=frozenset({"date", "file", "reference", "rfc-3339"}),
        danger_longs={"set": "--set sets the system clock"},
        operand_danger=lambda operand: not operand.startswith("+"),
        operand_reason="with an operand that is not a +FORMAT sets the system clock"),
    "file": _Getopt(
        arg_shorts="efFmP",
        danger_shorts={"C": "-C writes a compiled magic file"},
        arg_longs=frozenset({"exclude", "separator", "files-from", "magic-file", "parameter"}),
        danger_longs={"compile": "--compile writes a compiled magic file"}),
    # tree is not installed here; from its documentation. Its parser takes each
    # value letter's value from the next word and keeps reading the cluster.
    "tree": _Getopt(
        arg_shorts="LPIHT", whole_cluster=True,
        danger_shorts={"o": "-o writes its output to a file",
                       "R": "-R writes a 00Tree.html into every directory"},
        arg_longs=frozenset({"filelimit", "timefmt", "charset", "sort", "hintro",
                             "houtro", "infofile"})),
    "ss": _Getopt(
        arg_shorts="fANF",
        danger_shorts={"D": "-D writes raw socket data to a file", "K": "-K closes sockets"},
        arg_longs=frozenset({"family", "query", "socket", "net", "filter", "bpf-map-id"}),
        danger_longs={"diag": "--diag writes raw socket data to a file",
                      "kill": "--kill closes sockets"}),
    "hostname": _Getopt(
        danger_shorts={"F": "-F sets the host name from a file", "b": "-b sets the host name"},
        danger_longs={"file": "--file sets the host name from a file",
                      "boot": "--boot sets the host name"},
        operand_danger=lambda operand: True,
        operand_reason="with an operand sets the host name"),
    # env runs a program by design, and `unwrap` hands that program to the
    # judge. What reaches this table is an env that unwrap could not see
    # through, because everything after it is an option: `-S`/`--split-string`
    # glued to a whole command line (`env '-S<program>'`), which env splits
    # and runs. `run_readonly` ran exactly that (2026-10-09).
    "env": _Getopt(
        arg_shorts="Cfua",
        danger_shorts={"S": "-S runs its argument as a command line"},
        arg_longs=frozenset({"chdir", "file", "unset", "argv0"}),
        danger_longs={"split-string": "--split-string runs its argument as a command line"}),
}

# Audited, with nothing that writes, runs a program or reads past its operands
# (local man pages, 2026-10-09; jq from its documentation, it is not
# installed). Options that only *read* a named file — `grep -f`, `sort
# --files0-from`, `date -f`, `du -X`, `hexdump -f` — are not listed as
# hazards: the file is named on the line, where the secrets layer sees it.
_NO_HAZARD = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "stat", "echo",
    "pwd", "which", "true", "test", "[", "cut", "printf", "dirname", "basename",
    "du", "df", "whoami", "uname", "ps", "uptime", "more", "jq", "diff", "cmp",
    "md5sum", "sha1sum", "sha256sum", "sha512sum", "b2sum", "cksum", "realpath",
    "readlink", "column", "od", "hexdump", "strings", "nl", "tac", "base64",
})


def reader_hazard(tokens: list[str]) -> Hazard | None:
    """Why this reader, in this form, is not a read — or None.

    `tokens` is the unwrapped command (`unwrap`), with redirections already
    removed (`strip_redirections`): a trailing `2>&1` is not an operand.
    """
    check = READER_HAZARDS.get(_stem(tokens))
    if check is None:
        return None
    found = check(list(tokens[1:]))
    if not found:
        return None
    return found if isinstance(found, Hazard) else Hazard(found)


def writes_anyway(tokens: list[str]) -> str:
    """Why a read-only staple, in this form, is not a read; '' if it is one.

    Public because `run_readonly` and `protected_state` need the same answer
    (2026-08-17) and a second copy of this list is how files disagree. `find`
    is on that tool's allowlist because searching is a read — and `find .
    -exec rm -rf {} \\;` is on the same allowlist, wearing the same name. The
    `\\;` had been caught only by accident, as a raw `;` in an operator scan;
    the moment that scan became quote- and escape-aware the accident stopped
    happening, and `-delete` and `-exec … +` had never been caught at all.
    Since 2026-10-09 the answer is `READER_HAZARDS`.
    """
    hazard = reader_hazard(tokens)
    return hazard.reason if hazard else ""


# --- inline source ------------------------------------------------------------
#
# `_INLINE_CODE_FLAGS` and `_INLINE_ATTACHED` above are the original rule, kept
# whole. It missed the same three shapes the gate-state check missed for shells
# (2026-10-09): the source flag **inside a cluster** (`python3 -Ic …`, `-uc`,
# `node -pe …`), a source option **with its value glued on by `=`** (`node
# --eval=…`, `--print`), and an interpreter that evaluates a **subcommand's**
# argument (`deno eval …`). python and node are auto-ALLOW stems, so each of
# those was arbitrary code with nobody asked.
_INLINE_LETTERS = {"python": "c", "python3": "c", "node": "ep", "bun": "ep",
                   "perl": "eE", "ruby": "e", "php": "r",
                   "sh": "c", "bash": "c", "zsh": "c", "ksh": "c", "dash": "c"}
# Letters that take a value end a cluster: `python -mcProfile` names a module.
_VALUE_LETTERS = {"python": "mWXQ", "python3": "mWXQ", "node": "rC", "bun": "r",
                  "sh": "oO", "bash": "oO", "zsh": "oO", "ksh": "oO", "dash": "oO"}
_INLINE_LONGS = {"node": ("eval", "print"), "bun": ("eval", "print")}
_INLINE_SUBCOMMANDS = {"deno": frozenset({"eval", "repl"}), "bun": frozenset({"repl"})}
# Given no program, these read one from standard input — a pipe, a here-string
# or a here-document is inline source by another route (`… | python3`).
_STDIN_PROGRAM = {"python": frozenset({"-V", "-VV", "--version", "-h", "--help", "-?"}),
                  "python3": frozenset({"-V", "-VV", "--version", "-h", "--help", "-?"}),
                  "node": frozenset({"-v", "--version", "-h", "--help"}),
                  "deno": frozenset({"-V", "--version", "-h", "--help"})}


def _program_named(stem: str, args: list[str]) -> bool:
    """True if the interpreter was told what to run: a script, `-m`, `-c`/`-e`,
    or (deno) a subcommand."""
    letters = _INLINE_LETTERS.get(stem, "") + ("m" if stem.startswith("python") else "")
    values = _VALUE_LETTERS.get(stem, "")
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token == "--":
            return i < len(args)
        if token == "-" or not token.startswith("-"):
            return True
        if token.startswith("--"):
            if any(token.split("=", 1)[0] == "--" + name for name in _INLINE_LONGS.get(stem, ())):
                return True
            continue
        for k, letter in enumerate(token[1:]):
            if letter in letters:
                return True
            if letter in values:
                if k == len(token) - 2:
                    i += 1
                break
    return False


def _has_inline_source(tokens: list[str], redirects: list[Redirect] = ()) -> bool:
    """True if an interpreter was handed source on the command line."""
    for token in tokens[1:]:
        if token in _INLINE_CODE_FLAGS:
            return True
        if any(token.startswith(f) and len(token) > len(f) for f in _INLINE_ATTACHED):
            return True
    stem, args = _stem(tokens), tokens[1:]
    letters, values = _INLINE_LETTERS.get(stem, ""), _VALUE_LETTERS.get(stem, "")
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token == "--" or not token.startswith("-"):
            break                       # the program's own arguments start here
        if token.startswith("--"):
            name = token[2:].split("=", 1)[0]
            if name in _INLINE_LONGS.get(stem, ()):
                return True
            continue
        for k, letter in enumerate(token[1:]):
            if letter in letters:
                return True
            if letter in values:
                if k == len(token) - 2:
                    i += 1              # `-W ignore`: the value is the next word
                break
    subcommands = _INLINE_SUBCOMMANDS.get(stem)
    if subcommands is not None:
        first = next((t for t in args if not t.startswith("-")), "")
        if first in subcommands:
            return True
    info = _STDIN_PROGRAM.get(stem)
    if info is not None and not (set(args) & info) and not _program_named(stem, args):
        heredoc = any(r.op in ("<<", "<<-", "<<<") for r in redirects)
        from_file = any(r.op == "<" for r in redirects)
        return heredoc or not from_file
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
    redirects = redirections(segment)
    if any(r.writes for r in redirects):
        return Verdict(ASK, "redirects output to a file")

    # The command's own words, redirections cut out first: `uniq in.txt 2>&1`
    # has one operand, not two.
    bare = _unwrap(_tokens(strip_redirections(segment))) if redirects else tokens
    hazard = reader_hazard(bare)
    if hazard is not None:
        return Verdict(ASK, f"{stem} {hazard.reason}, so it is not a read")

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

    if stem in _INTERPRETERS and _has_inline_source(bare, redirects):
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

    The line is judged as bash will read it: backslash-newline continuations
    are removed first (`join_continuations`).
    """
    command = join_continuations(command or "")
    if not command.strip():
        return Verdict(ASK, "empty command")

    # Command substitution runs a command this function never sees. There is no
    # honest verdict except "a human should look".
    if has_substitution(command):
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
