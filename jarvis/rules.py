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

import contextlib
import contextvars
import functools
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

# --- deny: anything that names or runs VEX's device tool (VX-1, decision V-2) --
#
# `vexcom` downloads programs to a VEX V5 robot and talks to its brain. Sending
# code to a robot is a **physical** action, so it is meant to be only the
# owner's click in the HUD (`jarvis/v2/vex.py`): no tool, MCP tool, CLI or
# Discord verb reaches it, and an agent command line that *names* it, or
# *runs* something that could be it, is DENY — never put to the owner as a
# yes/no, because a card that offers "download to the robot" can be approved
# by a tired owner as easily as any other.
#
# Two readings of a line, both pure text work (nothing runs):
#
# **What the line names** (`vexcom_reading`). Deliberately wide: the word
# anywhere on the line, in any case (Windows is case-insensitive), in a path
# (`/mnt/c/…/vexcom.exe`, the Linux `…/linux-x64/vexcom`, `C:\…\vexcom.exe`),
# behind a wrapper, inside a `sh -c` / `cmd.exe /c` / `powershell -c` string,
# after the quote, escape and expansion tricks that split a word without
# changing it (bash `'"\`, `$'…'`, `${X}`, `${X:-vex}`, `$()`, cmd's `^` and
# `%X%`, PowerShell's backtick, brace and bracket expansion), a glob that keeps
# at least four of its letters (`vexc*`, `ve?com`, `*xcom.exe` — never `*vex*`,
# `v*` or `*.exe`, which are ordinary arguments in a VEX project), or a
# PowerShell `-EncodedCommand` (any unique prefix, `-`, `/` or a dash
# character, `:value` or a separate word) that decodes to it. A grep or a
# commit message that merely mentions the word is refused too — the `.env`
# rule's trade: a refusal the model can rephrase past costs less than a
# download nobody chose.
#
# **What the line runs** (`_vex_line`, below `_has_inline_source`). The
# four-letter threshold is for *arguments* only. A word in **program
# position** — command position, past a wrapper (`env`, `nohup`, `timeout`,
# `xargs`, …), the program a runner starts (`uv run`, `poetry run`, `pipx
# run`, `npx`, `npm exec`, …, options parsed), a `sh -c` string, a `$(…)` —
# that is a glob or holds an expansion and could be `vexcom`/`vexcom.exe` by
# its file name is a vexcom run, DENY, with no threshold: `uv run
# /mnt/c/…/tools/v*/*/*/v*.exe --user` and `…/*/*/win32/*.exe` are downloads.
# A program that cannot be told at all — nothing but an expansion (`$X`,
# `"$@"`, `$(…)`), a program read from input (`xargs env`, `find -exec {}`),
# an option the runner's table does not know that leaves two candidates — and
# that could be vexcom is **asked whatever else stands**: ASK in v1, never
# covered by an allowlist entry, always-ask in v2 (`vex_uncoverable`).
#
# **A guard that can be made slow is a guard that can be made absent**: past
# the caps below a glob is *too complex to judge* — ASK, never ALLOW, never
# allowlist-covered, always-ask in v2 — and the make file guard refuses.
#
# **An accident guard, not a boundary**, exactly as the DENY note above says —
# and the owner's button is not a boundary either. An agent can still reach a
# plugged-in brain: through a script it writes and runs (which can assemble the
# name or call the device itself), a shell fed its commands through a pipe,
# cmd.exe's or PowerShell's own wildcard forms, a program launcher this file
# does not model (`systemd-run` and `flock` are; `parallel`, `strace` are not),
# `make -f` on a makefile of its own, or by POSTing to the HUD's own download
# route with a forged Origin (it runs as the owner). What this buys is that the
# obvious spellings never run and never become an approval card; what makes a
# forged download *visible* is the id-only `vex_device` bus record every device
# action publishes. That residual gap is stated, not closed: closing it needs
# the device to belong to a user the agent's processes are not.
_VEXCOM = "vexcom"
_VEX_TARGETS = ("vexcom", "vexcom.exe")
_SPLITTERS = re.compile(r"[\"'\\^`]")
_DEFAULT_EXPANSIONS = re.compile(r"\$\{[#!]?[A-Za-z_][A-Za-z0-9_]*:?[-=+?]([^{}]*)\}")
# `[^}$]`, not `[^}]`: a flood of `${` with no `}` made each one scan to the
# end of the line (quadratic); stopping at the next `$` keeps it linear.
_EMPTY_EXPANSIONS = re.compile(
    r"\$\{[^}$]*\}|\$\([^()]*\)|\$[@*#?$!0-9-]|\$[A-Za-z_][A-Za-z0-9_]*|%[^%\s]*%|`[^`]*`")
_BRACES = re.compile(r"[{},\[\]]")
_ANSI_C_QUOTE = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
_POWERSHELL = re.compile(r"(^|[\\/\s'\"])(powershell|pwsh)(\.exe)?($|[\s'\"])", re.I)
# PowerShell takes `-`, `/` and the dash characters as a parameter's prefix.
_PS_PREFIX = "-/\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_WORD_SPLIT = re.compile(r"[\s;&|<>()=]+")

# What one line may hold before it is too complex to judge (review round 2,
# MED 2: `ls [[[[…` took seconds on a quadratic scan, 20,000 of them 3.4 s, and
# the make file guard read one 40,000-`[` word for 14 s). Each glob word costs
# a bounded scan, and there is a bounded number of them.
GLOB_WORD_CAP = 256        # characters in a glob word's file-name part
GLOB_META_CAP = 64         # `*`, `?` and `[` in it
GLOB_WORDS_CAP = 512       # distinct glob words judged in one line or make file
NAMES, COMPLEX = "names", "complex"
TOO_COMPLEX = ("it is too complex to judge whether it names or runs vexcom, VEX's tool for "
               "the robot, so it is asked whatever else stands")


def _ansi_c_decoded(text: str) -> str:
    def decode(match: re.Match) -> str:
        try:
            return match.group(1).encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeError, ValueError):
            return match.group(0)
    return _ANSI_C_QUOTE.sub(decode, text)


# A glob, read the way bash reads it, as atoms: `_STAR`, `_ANY` (a `?`, or a
# whole bracket expression — read as any one character, which can only widen
# what it is said to match), or one literal character.
_STAR, _ANY = object(), object()


def _bracket_end(pat: str, i: int, posix: bool = True) -> int | None:
    """Index just past the bracket expression bash opens at `pat[i] == "["`,
    or None if it never closes (then the `[` is a literal). A leading `!`
    or `^` negates and a `]` straight after that is a member. With `posix`,
    `[:class:]`, `[=x=]` and `[.x.]` run to their own closers; without it the
    first `]` closes (fnmatch's reading). Callers read both and take either
    match, so a spelling the two disagree on can only widen what a glob is
    said to match. Callers bound `pat` first."""
    n, j = len(pat), i + 1
    if j < n and pat[j] in "!^":
        j += 1
    if j < n and pat[j] == "]":
        j += 1
    while j < n:
        close = pat.find("]", j)
        if close == -1:
            return None
        if not posix:
            return close + 1
        opener = -1
        for kind in ":=.":
            k = pat.find("[" + kind, j, close)
            if k != -1 and (opener == -1 or k < opener):
                opener = k
        if opener == -1:
            return close + 1
        closer = pat.find(pat[opener + 1] + "]", opener + 2)
        j = closer + 2 if closer != -1 else opener + 1
    return None


def _glob_atoms(pat: str, posix: bool = True) -> list:
    """`pat` (already cut to its file-name part) as atoms; a `[` that never
    closes is a literal, a backslash makes the next character one."""
    atoms: list = []
    i, n = 0, len(pat)
    while i < n:
        ch = pat[i]
        if ch == "*":
            if not atoms or atoms[-1] is not _STAR:
                atoms.append(_STAR)
        elif ch == "?":
            atoms.append(_ANY)
        elif ch == "[":
            end = _bracket_end(pat, i, posix)
            if end is not None:
                atoms.append(_ANY)
                i = end
                continue
            atoms.append("[")
        elif ch == "\\" and i + 1 < n:
            atoms.append(_fold(pat[i + 1]))
            i += 2
            continue
        else:
            atoms.append(_fold(ch))
        i += 1
    return atoms


def _fold(ch: str) -> str:
    low = ch.lower()
    return low if len(low) == 1 else ch


def _atoms_match(atoms: list, target: str) -> bool:
    """Could the atoms match `target`? Linear in the atoms: the target is a
    short, fixed name."""
    reach = {0}
    end = len(target)
    for atom in atoms:
        if atom is _STAR:
            reach = set(range(min(reach), end + 1))
        else:
            reach = {p + 1 for p in reach
                     if p < end and (atom is _ANY or atom == target[p])}
        if not reach:
            return False
    return end in reach


def _literal_letters(atoms: list) -> int:
    """How many characters of the glob are literal, a trailing `.exe` not
    counted (`*xcom.exe` keeps four: x, c, o, m)."""
    tail = atoms[-4:]
    if len(atoms) >= 4 and all(isinstance(a, str) for a in tail) and "".join(tail) == ".exe":
        atoms = atoms[:-4]
    return sum(isinstance(a, str) for a in atoms)


def _glob_reading(word: str) -> str | None:
    """NAMES for an argument glob that could be vexcom's name **and keeps at
    least four of its letters** (`vexc*`, `ve?com`, `*xcom.exe`); COMPLEX past
    the caps; else None. `*vex*`, `v*`, `*.exe` and `*` are ordinary work in a
    VEX project and are never refused for this."""
    base = re.split(r"[\\/]", word)[-1]
    if not base or not any(c in base for c in "*?["):
        return None
    if len(base) > GLOB_WORD_CAP or sum(base.count(c) for c in "*?[") > GLOB_META_CAP:
        return COMPLEX
    for posix in (True, False):
        atoms = _glob_atoms(base, posix)
        if any(_atoms_match(atoms, t) for t in _VEX_TARGETS) and _literal_letters(atoms) >= 4:
            return NAMES
    return None


def _encoded_powershell(text: str) -> list[str]:
    """What a PowerShell `-EncodedCommand` (base64 of UTF-16LE) on this line
    says, decoded, so a hidden `vexcom` is still a named one. Any unique
    prefix of the parameter (`-e`, `-ec`, `-enc`, …), any prefix character
    PowerShell takes, and both `-enc VALUE` and `-enc:VALUE`."""
    import base64
    import binascii
    if not _POWERSHELL.search(text):
        return []
    words = text.split()
    found = []
    for i, raw in enumerate(words):
        word = raw.strip("'\"")
        if not word or word[0] not in _PS_PREFIX:
            continue
        name, colon, inline = word.lstrip(_PS_PREFIX).partition(":")
        name = name.lower()
        if not (name in ("e", "ec") or (name.startswith("en") and "encodedcommand".startswith(name))):
            continue
        payload = inline if colon else (words[i + 1] if i + 1 < len(words) else "")
        payload = payload.strip("'\"")
        try:
            found.append(base64.b64decode(payload + "=" * (-len(payload) % 4),
                                          validate=True).decode("utf-16-le", "ignore"))
        except (binascii.Error, ValueError):
            continue
    return found


def _spellings(text: str) -> set[str]:
    """The text and the spellings of it that the quote, escape and expansion
    tricks above leave a shell reading the same: NFKC, ANSI-C `$'…'`
    decoded, `${X:-word}` read as its word, empty expansions cut, quotes and
    escapes removed, braces and brackets removed. Pure text work. What
    `vexcom_reading` and `secret_service_reading` look for a name in."""
    import unicodedata
    variants = {text, unicodedata.normalize("NFKC", text)}
    variants |= {_ansi_c_decoded(v) for v in list(variants)}
    variants |= {_DEFAULT_EXPANSIONS.sub(r"\1", v) for v in list(variants)}
    for v in list(variants):
        cut = _EMPTY_EXPANSIONS.sub("", v)
        variants |= {cut, _SPLITTERS.sub("", v), _SPLITTERS.sub("", cut)}
    variants |= {_BRACES.sub("", v) for v in list(variants)}
    return variants


def vexcom_reading(command: str, _depth: int = 0, *, shell: bool = True) -> str | None:
    """NAMES if this text names VEX's device tool in any spelling the note
    above lists, COMPLEX if a glob in it is past the caps (or there are too
    many to judge), else None. Pure text work; nothing runs. The make file
    guard (`vex.makefile_refusal`) reads make files with it too, with
    `shell=False`. In a command line the glob words are read with each
    here-document's body set aside — bash never globs one — keeping only the
    `$(…)` an unquoted body would run; the name itself is looked for
    everywhere."""
    text = join_continuations(command or "")
    if not text.strip():
        return None
    variants = _spellings(text)
    for v in variants:
        if _VEXCOM in v.casefold():
            return NAMES
    complex_ = False
    judged: set[str] = set()
    if shell and "<<" in text:
        try:
            globbed = _inline_heredocs(command or "", keep=False)
        except _TooComplex:
            # Where a body ends cannot be told: no glob word can be said to
            # be data or a command, so none is judged; the line is asked.
            complex_, globbed = True, ""
        variants = _spellings(globbed)
    for v in variants:
        for word in _WORD_SPLIT.split(v):
            if word in judged or not any(c in word for c in "*?["):
                continue
            judged.add(word)
            if len(judged) > GLOB_WORDS_CAP:
                return COMPLEX
            reading = _glob_reading(word)
            if reading == NAMES:
                return NAMES
            complex_ = complex_ or reading == COMPLEX
    if _depth < 2:
        for decoded in _encoded_powershell(text):
            reading = vexcom_reading(decoded, _depth + 1)
            if reading == NAMES:
                return NAMES
            complex_ = complex_ or reading == COMPLEX
    return COMPLEX if complex_ else None


def names_vexcom(command: str) -> bool:
    """True if this command line names VEX's device tool (`vexcom_reading`)."""
    return vexcom_reading(command) == NAMES


# --- deny: agent calls to the Secret Service (B-LOGIN follow-up, 2026-10-15) --
#
# The HUD Browser keeps Chrome's password key in the owner's login keyring
# (`v2/browser/password_store.py`). While it is unlocked, gnome-keyring has
# no per-application rule: any program running as the owner may ask the
# Secret Service for that key, or for any other saved secret. So an agent's
# shell line that **calls** it is DENY, never put to the owner — approving a
# command is not consent to what it reads, as with `.env`.
#
# **Judged per program, in command position** (review of #218, LOW 3: a
# whole-line reading refused `git commit -m 'add secret-tool rule'`, a PR
# body or a `grep` that only named one). Every program the line runs is
# found by the vex reading's walk (`_Walk`: every segment, wrappers and
# runners from the one table, `bash -c`/`sh -c` strings, `eval`, `find
# -exec`, substitutions), and each is judged alone, its word and arguments
# read in every spelling `_spellings` gives (quotes, escapes, `$'…'`,
# `${X:-…}`, braces), case-folded:
#
# - **a secret reader as the program**: `secret-tool`, `kwallet-query`,
#   `lssecret`, whatever the subcommand (they look up, search — `secret-tool
#   search` prints secrets — store and clear); Python's `keyring` CLI with
#   `get`, `set` or `del`; and any Python run with `-m keyring` (review of
#   #218, MED 2: `uv run --with keyring python3 -m keyring get S U` was ALLOW);
# - **a caller whose own arguments name the Secret Service**: a D-Bus client
#   (`busctl`, `gdbus`, `dbus-send`, `qdbus*`, `dbus-monitor`), an
#   interpreter or a shell, as the program of the segment, whose words (a
#   `-c` script included) name its bus name, interfaces or object paths
#   (`org.freedesktop.secrets`, `org.freedesktop.Secret.*`,
#   `/org/freedesktop/secrets/…`), the portal's `org.freedesktop.portal.Secret`
#   or its backend `org.freedesktop.impl.portal.Secret`, gnome-keyring's own
#   `org.gnome.keyring` or KWallet's `org.kde.kwalletd*`. A shell elsewhere on
#   the line does not count: `grep org.gnome.keyring x | sh -c 'wc -l'` is
#   not a call;
# - **a session-bus monitor** (`_bus_monitor`): `dbus-monitor` unless
#   `--system`, `gdbus monitor` unless `--system`/`-y`, `busctl
#   monitor|capture` with `--user`, an address, a machine or a host. It
#   would read the key in the reply that carries it to Chrome. A line that
#   assigns `DBUS_SYSTEM_BUS_ADDRESS=` or `DBUS_SESSION_BUS_ADDRESS=` gets no
#   `--system` exemption (the "system" bus may then be the session's).
#
# A line too complex for the walk (its caps, `_TooComplex`) is read the old,
# stricter way over its whole text (`_secret_service_text`). v1's gate and
# v2's layer 1 both start from `decide`, so the two agree.
#
# **An accident guard, not a wall** (the script review's standing): a call
# by a bus *unique name* with a computed object path, a program word that is
# only an expansion, or a script file that talks D-Bus is not seen. The
# same-user boundary in the Browser notes (*Password store*) is the honest
# limit.
SECRET_SERVICE_DENIED = (
    "it calls the Secret Service (the owner's keyring), where Chrome's password key and the "
    "owner's other saved secrets live; approving a command is not consent to what it reads, "
    "so no agent command reaches it")
_SECRET_READER_STEMS = frozenset({"secret-tool", "kwallet-query", "lssecret"})
_SECRET_READERS = re.compile(r"(?<![\w.-])(?:secret-tool|kwallet-query|lssecret)(?![\w.-])")
_KEYRING_VERBS = frozenset({"get", "set", "del"})
_SECRET_SERVICE_NAMES = ("org.freedesktop.secret", "org/freedesktop/secret",
                         "org.freedesktop.portal.secret", "org.freedesktop.impl.portal.secret",
                         "org.gnome.keyring", "org/gnome/keyring", "org.kde.kwalletd",
                         "org/modules/kwalletd")
_BUS_CALLERS = frozenset({"busctl", "gdbus", "dbus-send", "dbus-monitor", "qdbus", "qdbus6",
                          "qdbus-qt5", "qdbus-qt6", "dbus"})
_SCRIPT_CALLER = re.compile(r"python[0-9.]*|pypy[0-9.]*|perl[0-9.]*|ruby[0-9.]*|node[0-9]*")
_CALLER_WORD = re.compile(r"[^\s;&|<>()=`'\"]+")
_BUS_ASSIGNMENT = re.compile(r"dbus_(?:system|session)_bus_address\s*=")
# Cheap first look on the whole line's spellings: none of these, nothing to walk.
_SECRET_HINTS = ("secret", "keyring", "kwallet", "monitor", "capture")


def _program_stem(word: str) -> str:
    base = re.split(r"[\\/]", word)[-1].casefold()
    return base[:-4] if base.endswith(".exe") else base


def _is_caller(stem: str) -> bool:
    """A program that could make a D-Bus call: a D-Bus client, an
    interpreter or a shell (any version suffix on the script runtimes)."""
    return (stem in _BUS_CALLERS or stem in _INTERPRETERS or stem in SHELLS
            or bool(_SCRIPT_CALLER.fullmatch(stem)))


def _names_service(text: str) -> bool:
    return any(name in v.casefold() for v in _spellings(text) for name in _SECRET_SERVICE_NAMES)


def _runs_keyring_module(args: list[str]) -> bool:
    """`-m keyring` (or `keyring.cli`), `-mkeyring`, or `-m` ending a flag
    cluster (`-Im keyring`)."""
    for i, arg in enumerate(args):
        if arg.startswith("-m") and len(arg) > 2 and not arg.startswith("--"):
            module = arg[2:]
        elif arg.startswith("-") and not arg.startswith("--") and arg.endswith("m"):
            module = args[i + 1] if i + 1 < len(args) else ""
        else:
            continue
        module = module.casefold()
        if module == "keyring" or module.startswith("keyring."):
            return True
    return False


def _bus_monitor(stem: str, args: list[str], assigned: bool) -> str | None:
    """Why this program watches the session bus (DENY), or None: the bus
    carries the keyring's secrets to the programs that ask. `dbus-monitor`
    watches the session bus unless told `--system`; `gdbus monitor` unless
    `--system`/`-y`; `busctl monitor|capture` watches the system bus unless
    told `--user`, an address, a machine or a host. `assigned`: the line
    sets a `DBUS_*_BUS_ADDRESS`, so `--system` is no exemption."""
    if stem == "dbus-monitor":
        if (not assigned and "--system" in args
                and not any(a == "--session" or a.startswith("--address") for a in args)):
            return None
    elif stem == "gdbus":
        if args[:1] != ["monitor"] or (not assigned and ("--system" in args or "-y" in args)):
            return None
    elif stem == "busctl":
        if not any(a in ("monitor", "capture") for a in args):
            return None
        if not assigned and not any(
                a == "--user" or a.split("=", 1)[0] in ("--address", "--machine", "--host")
                or (a.startswith(("-M", "-H")) and not a.startswith("--")) for a in args):
            return None
    else:
        return None
    return (f"{stem} would watch the session bus, which carries the keyring's secrets (Chrome's "
            "password key among them) to the programs that ask; no agent command watches it")


def _program_verdict(word: str, args: list[str], assigned: bool) -> str | None:
    """One program the line runs, judged alone (note above)."""
    words = [str(a) for a in args]
    for spelled in _spellings(str(word)):
        stem = _program_stem(spelled)
        if stem in _SECRET_READER_STEMS:
            return SECRET_SERVICE_DENIED
        if stem == "keyring" and any(a in _KEYRING_VERBS for a in words):
            return SECRET_SERVICE_DENIED
        if _PYTHON.fullmatch(stem) and _runs_keyring_module(words):
            return SECRET_SERVICE_DENIED
        monitor = _bus_monitor(stem, words, assigned)
        if monitor:
            return monitor
        if _is_caller(stem) and words and _names_service(" ".join(words)):
            return SECRET_SERVICE_DENIED
    return None


def secret_service_reading(command: str) -> str | None:
    """SECRET_SERVICE_DENIED (or the monitor's reason) if a program this line
    runs calls the Secret Service or watches the session bus (note above),
    else None. Pure text work; nothing runs. Short lines are cached, as the
    vex reading's are."""
    command = command or ""
    if len(command) <= 16384:
        return _secret_service_cached(command)
    return _secret_service_uncached(command)


@functools.lru_cache(maxsize=256)
def _secret_service_cached(command: str) -> str | None:
    return _secret_service_uncached(command)


def _secret_service_uncached(command: str) -> str | None:
    text = join_continuations(command)
    if not text.strip():
        return None
    spelled = [v.casefold() for v in _spellings(text)]
    if not any(hint in v for v in spelled for hint in _SECRET_HINTS):
        return None
    assigned = any(_BUS_ASSIGNMENT.search(v) for v in spelled)
    for variant in _program_variants(text):
        walk = _Walk()
        try:
            walk.line(variant, True, 0)
        except _TooComplex:
            return _secret_service_text(text)
        for prog in walk.progs:
            if not prog.word:
                continue
            why = _program_verdict(prog.word, list(prog.args), assigned)
            if why:
                return why
    return None


def _secret_service_text(text: str) -> str | None:
    """The stricter whole-text reading, for a line too complex to walk: a
    reader's name anywhere, or a service name and a caller anywhere."""
    for variant in _spellings(text):
        folded = variant.casefold()
        if _SECRET_READERS.search(folded):
            return SECRET_SERVICE_DENIED
        if (any(name in folded for name in _SECRET_SERVICE_NAMES)
                and any(_is_caller(_program_stem(w)) for w in _CALLER_WORD.findall(variant))):
            return SECRET_SERVICE_DENIED
    return None


VEXCOM_DENIED = ("it names vexcom, VEX's tool for downloading to and talking to the robot; "
                 "that is only ever the owner's click in the HUD, never an agent's command "
                 "(build with vexbuild instead)")


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
# Every shell this module knows by name — the one list (delta review of PR
# #75, M-B): `_INTERPRETERS`, the inline-source tables, the vex walk
# (`_SHELL_STEMS`) and `protected_state._SHELLS` read it. Each runs the
# string after `-c` (fish's `--command`, nu's `--commands` too).
SHELLS = frozenset({"sh", "bash", "rbash", "dash", "zsh", "ksh", "ksh93", "lksh", "oksh", "mksh",
                    "posh", "ash", "sash", "yash", "fish", "csh", "tcsh", "nu", "xonsh",
                    "elvish", "ion", "osh", "ysh"})
_INTERPRETERS = {"python", "python3", "node", "deno", "bun", "perl", "ruby", "php"} | SHELLS

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
        # A segment whose program cannot be told (`unwrap_sure`) is covered by
        # nothing: `env -u python3 rm …` is not the `python3` it looked like.
        tokens = unwrap_sure(_tokens(segment))
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
    line cannot loop. Where an option the table does not know leaves more
    than one reading, this is the first; `unwrap_sure` says when there is
    only one.
    """
    return _unwrap_reading(tokens)[0]


def unwrap_sure(tokens: list[str], peeled: list | None = None,
                writes: list | None = None) -> list[str] | None:
    """`unwrap`, or None when which program runs cannot be told: an option the
    table does not know may or may not take the next word, `env -S` splits
    past its bound, or the wrappers nest past `PROGRAM_DEPTH`. `run_readonly`
    runs nothing it cannot tell, and `command_targets` covers nothing.
    `peeled`, when given, receives the stem of each wrapper taken off;
    `writes`, the files those wrappers write themselves (`time -o FILE`)."""
    tokens, sure = _unwrap_reading(list(tokens), peeled, writes)
    return tokens if sure else None


# A shell assignment word: `NAME=VALUE`, `NAME+=VALUE` or `NAME[i]=VALUE`,
# ASCII names only (bash runs `Xé=1` as a command). **The one pattern every
# reader of a segment's first word strips** (third delta review of PR #75,
# HIGH: the gate-state detector kept `X=1` in front of `time -o FILE`, read
# `X=1` as the program, and so never saw the wrapper's own output file — a
# write onto the allowlist ran unasked behind an `ls` entry). `unwrap` had
# its own narrower copy, which missed `+=` and `[i]=`.
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=.*", re.S)
_ASSIGNMENT = ASSIGNMENT
_ASSIGNMENT_PARTS = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[([^\]]*)\])?(\+?)=(.*)", re.S)


def assignment(word: str) -> tuple[str, str | None, bool, str] | None:
    """(name, subscript or None, whether it appends, value) of an assignment
    word (`ASSIGNMENT`), or None for any other word."""
    match = _ASSIGNMENT_PARTS.fullmatch(word)
    if match is None:
        return None
    name, subscript, plus, value = match.groups()
    return name, subscript, bool(plus), value


def strip_assignments(words: list[str]) -> list[str]:
    """The words from the first that is not a leading assignment: `X=1 A="x
    y" time -o F ls` runs `time`. Every place that reads a command's first
    word strips them with this, and only this (`command_start` too)."""
    words = list(words)
    k = 0
    while k < len(words) and ASSIGNMENT.fullmatch(words[k]):
        k += 1
    return words[k:]


def command_start(words: list[str], *, assignments: bool = True) -> list[str]:
    """A segment's words from its program on: the shell's words that may
    stand before a command and are not it — the reserved words that open a
    compound command or a pipeline (`then`, `do`, `else`, `!`, `{`, `(`,
    `coproc`: `_LEAD_WORDS`), a `case … in` and its pattern (`a)`, `(a|b)`),
    a function's header (`f()`, `function f`), parens glued to the program —
    and, with `assignments`, the `NAME=VALUE` words (`strip_assignments`).
    `for`/`select` are left in front: their words are a list, not a program,
    and each caller judges that. One function for the gate-state detector,
    the owner-op detector and the vex reading (third delta review of PR #75:
    `if true; then time -o <allowlist> ls; fi` hid the wrapper behind
    `then` from the gate-state detector)."""
    words = list(words)
    while words:
        word = words[0]
        if word in _LEAD_WORDS or word.endswith("()") or (
                assignments and ASSIGNMENT.fullmatch(word)):
            words.pop(0)
        elif word == "case":
            words = words[words.index("in") + 1:] if "in" in words else []
        elif word == "function":
            words = words[2:]
        elif word.startswith("(") and len(word) > 1:
            words[0] = _cut(word, len(word) - len(word.lstrip("(")))
            if not words[0]:
                words.pop(0)
        elif len(words) > 1 and word.endswith(")") and word.count(")") > word.count("("):
            words.pop(0)                    # a `case` pattern
        else:
            break
    return words


def _unwrap_reading(tokens: list[str], peeled: list | None = None,
                    writes: list | None = None) -> tuple[list[str], bool]:
    """(the command inside its wrappers, whether it is the only reading).

    **The wrappers' options are read from the one table** (`_WRAPPER_OPTS`,
    through `starts`; review of PR #75, HIGH 3). This used to drop every word
    that looked like an option or a duration and take what was left, so the
    value of an option that takes one became the program: `env -u ls rm -rf
    build` was `ls`, which `run_readonly` — ungated, held by workflows and
    sub-agents — then ran as a read, and `env -u python3 rm …` was covered by
    a `python3` allowlist entry. Same for `env -C`/`-a`, `exec -a`, `xargs
    -a` and `time -f`."""
    tokens = list(tokens)
    sure = True
    for _ in range(PROGRAM_DEPTH + 2):
        tokens = strip_assignments(tokens)
        if not tokens or _basename(tokens[0]) not in _WRAPPERS:
            return tokens, sure
        stem = _basename(tokens[0])
        if stem == "command" and any(t.startswith("-") and set(t[1:]) & {"v", "V"}
                                     for t in tokens[1:2]):
            # `command -v x` looks x up and runs nothing; judged as x, as ever.
            rest = [t for t in tokens[1:] if not t.startswith("-")]
            return (rest or tokens), sure
        try:
            started = starts(tokens)
        except _TooComplex:
            return tokens, False
        if started is not None and writes is not None:
            writes.extend(started.writes)
        if started is None or not started.programs:
            return tokens, sure and not (started and started.strings)
        if not started.sure:
            sure = False
        if peeled is not None:
            peeled.append(stem)
        tokens = list(started.programs[0])
    return tokens, False


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
#
# **What the audit was made against, and why it is a union** (2026-10-09, PR
# #24 review). The first pass read GNU documentation and missed that one name
# is two programs here. Ubuntu 26.04's PATH has GNU grep 3.12, GNU findutils
# 4.10, GNU sed 4.9, ripgrep 15.1, uutils coreutils 0.8.0 (GNU 9.7 for `df` and
# `true`), procps-ng 4.0.4, util-linux 2.41.3, GNU diffutils 3.12, binutils
# strings 2.46, file 5.46, less 668, xxd, iproute2 6.19 `ss` and net-tools
# hostname 3.25 — but in Claude Code's shell, which v2 Claude workers run their
# commands through, `grep` is a function running **ugrep 7.8.4** and `find` one
# running **bfs 4.1.1**. Two backends, two answers is a lesson this file already
# carries (grep_files and its gitignore bug), so every entry is the **union** of
# the GNU program's dangerous forms and each installed implementation's: a
# verdict never depends on which one answers. Audited from each installed
# binary's own `--help`/`man` (run by argv, never through a shell); `jq`,
# `tree` and `bat` are not installed and were audited from their upstream
# documentation. `AUDITED_IMPLEMENTATIONS` below names them, and the rules
# suite prints AUDIT NOT VERIFIED when a binary reports something else.


@dataclass(frozen=True)
class Hazard:
    """Why a reader, in this form, is not a read.

    `reason` completes a sentence that starts with the program's name.
    `writes` lists the files it will write — out of a script (a sed `w`
    command's file is inside one shlex token, invisible to anything that
    looks at operands), an option's value (`sort -o FILE`) or an operand by
    position (`uniq IN OUT`). `operands`: it writes every operand it is given
    (`sed -i`, `find -delete`). `runs` means the line carries text the
    program executes, so a gate file named in it is treated like inline
    interpreter source.

    **Only those are certain writes** (2026-10-12, PR #24's follow-up 1):
    `protected_state` used to read any hazard as a certain write of every
    operand, so a hazard that only runs something (`grep --pager`, `rg
    --pre`, `find -exec`), writes some other file (`less --save-marks`,
    ugrep's `.ugrep`), or was matched by an abbreviation another grep reads
    differently (`--p`, `--q`, `--con=`) made a gate file it merely reads a
    DENY. Those are uncertain now: asked, never refused unasked.
    """
    reason: str
    writes: tuple[str, ...] = ()
    runs: bool = False
    operands: bool = False


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
    # Dangerous options whose value is the file written (`sort -o FILE`): a
    # letter, or a long name abbreviated to three letters or more (a shorter
    # one may be another of the program's options: a hazard, not a certain write).
    writes_value: frozenset = frozenset()

    def __call__(self, args: list[str]) -> "str | Hazard":
        # Every danger on the line, not the first (review of PR #75, HIGH 2:
        # `sort --compress-program=gzip -o <allowlist> f` stopped at the
        # program and never saw the write).
        found: list = []
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
                name, eq, value = token[2:].partition("=")
                hit = False
                for long, why in self.danger_longs.items():
                    if name and long.startswith(name):
                        hit = True
                        if long in self.writes_value and len(name) >= 3:
                            if not eq:
                                value = args[i] if i < len(args) else ""
                                i += 1
                            found.append(Hazard(why, writes=(value,) if value else ()))
                        else:
                            found.append(why)
                        break
                if not hit and name in self.arg_longs and not eq:
                    i += 1
                continue
            letters = token[1:]
            for k, letter in enumerate(letters):
                if letter in self.danger_shorts:
                    if letter in self.writes_value:
                        value = "" if self.whole_cluster else letters[k + 1:]
                        if not value:
                            value = args[i] if i < len(args) else ""
                            i += 1
                        found.append(Hazard(self.danger_shorts[letter],
                                            writes=(value,) if value else ()))
                        if self.whole_cluster:
                            continue
                        break
                    found.append(self.danger_shorts[letter])
                    continue
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
            found.append(self.operand_reason)
        if self.max_operands is not None and len(operands) > self.max_operands:
            found.append(Hazard(self.operand_reason, writes=tuple(operands[self.max_operands:])))
        return _merged(found)


def _merged(found: list) -> "str | Hazard":
    """Every hazard a reader's words carry, kept together (review of PR #75,
    HIGH 2): the readers returned the first danger they met, so an option
    that only runs something, read before a real write, hid the write and
    turned a DENY into an ASK. Its operands are written if any hazard says
    so, every file named as written is kept, it runs if any runs, and the
    reason is the strongest one's. One hazard comes back as it was."""
    found = [f for f in found if f]
    if not found:
        return ""
    if len(found) == 1:
        return found[0]
    hazards = [f if isinstance(f, Hazard) else Hazard(f) for f in found]
    lead = max(hazards, key=lambda h: (h.operands, bool(h.writes), h.runs))
    return Hazard(lead.reason, writes=tuple(dict.fromkeys(w for h in hazards for w in h.writes)),
                  runs=any(h.runs for h in hazards), operands=any(h.operands for h in hazards))


# GNU findutils 4.10 and bfs 4.1.1 together. bfs adds `-rm`, an alias of
# `-delete` (bfs -help: "Delete any found files"); its other extras — `-exit`,
# `-limit`, `-printx`, `-status`, `-unique`, `-files0-from`, the `-S`/`-j`/`-O`
# flags — print, stop or read.
_FIND_HAZARDS = {
    "-delete": "-delete deletes files", "-rm": "-rm deletes files",
    "-exec": "-exec runs a program", "-execdir": "-execdir runs a program",
    "-ok": "-ok runs a program", "-okdir": "-okdir runs a program",
    "-fprint": "-fprint writes a file", "-fprint0": "-fprint0 writes a file",
    "-fprintf": "-fprintf writes a file", "-fls": "-fls writes a file",
}


def _find_hazard(args: list[str]) -> str | Hazard:
    """find's predicates are whole words: `-executable` (a test of the mode
    bits) is not `-exec`, which the old prefix match made it. `-delete`
    removes what it is given; `-fprint FILE` and its kin write FILE; `-exec`
    and `-ok` run a program, which writes nothing provable by itself (what
    the program does with `{}` is `protected_state`'s to judge). Every
    predicate counts, not the first (review of PR #75, HIGH 2: `find <G>
    -exec cat {} + -delete` was only "runs a program")."""
    found: list = []
    for k, token in enumerate(args):
        why = _FIND_HAZARDS.get(token)
        if why is None:
            continue
        if token in ("-delete", "-rm"):
            found.append(Hazard(why, operands=True))
        elif token.startswith("-f"):
            found.append(Hazard(why, writes=tuple(args[k + 1:k + 2])))
        else:
            found.append(why)
    return _merged(found)


# grep is GNU grep 3.12 on PATH and ugrep 7.8.4 in Claude Code's shell; GNU
# grep has no option that writes or runs anything, ugrep has several (its
# `--help`, 2026-10-09):
#   --filter=COMMANDS     runs COMMANDS on every file searched — not gated on a
#                         terminal, so the closest thing to rg's --pre;
#   --pager[=COMMAND]     runs COMMAND (on a terminal);
#   --view[=COMMAND], -Q  the query TUI, which runs COMMAND to view or edit;
#   --save-config[=FILE]  writes FILE (default `.ugrep`);
#   --config[=FILE], ---[FILE]   loads options from FILE (default `.ugrep`),
#                         and a configuration file can carry `filter=`.
# Long names are matched as prefixes (getopt_long-style abbreviation is assumed
# rather than relied on), so `--filt=…` counts; `--no-config` and `--no-pager`
# do not. Only letters that take a value in **both** greps end a short cluster
# (`-A -B -C -D -d -e -f -m`): a letter that takes one in only one of them is
# scanned through, so `-XQ` (ugrep: hex, then query) still finds the Q.
_GREP_DANGER = {
    "filter": "--filter runs a command on every file it searches",
    "pager": "--pager runs a program",
    "view": "--view runs a program from the query screen",
    "query": "--query starts a screen whose view command runs a program",
    "save-config": "--save-config writes a configuration file",
    "config": "--config loads options from a file, which can name a filter command",
}
_GREP_VALUE_SHORTS = "ABCDdefm"


def _grep_hazard(args: list[str]) -> str | Hazard:
    # Every danger, not the first (review of PR #75, HIGH 2: `--pager=less
    # --save-config=<G>` was only "runs a program").
    found: list = []
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token == "--":
            break
        if token.startswith("---"):          # ugrep's short spelling of --config
            found.append(_GREP_DANGER["config"])
            continue
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            for long, why in _GREP_DANGER.items():
                if name and long.startswith(name):
                    # `--s` may be `--silent`; from `--sa` on it is only this.
                    if long == "save-config" and name.startswith("sa"):
                        found.append(Hazard(why, writes=(value if eq and value else ".ugrep",)))
                    else:
                        found.append(why)
                    break
            continue
        if token.startswith("-") and len(token) > 1:
            letters = token[1:]
            for k, letter in enumerate(letters):
                if letter == "Q":
                    found.append("-Q starts a screen whose view command runs a program")
                    continue
                if letter in _GREP_VALUE_SHORTS:
                    if k == len(letters) - 1:
                        i += 1
                    break
    return _merged(found)


def _ug_hazard(args: list[str]) -> str | Hazard:
    """`ug` is ugrep that loads `.ugrep` from the working or home directory on
    its own — options from a file, filter included — unless told not to. A
    `--no-config` after `--` is a pattern or a file name, not the option
    (PR #24's follow-up 3)."""
    options = args[:args.index("--")] if "--" in args else args
    if "--no-config" in options:
        return _grep_hazard(args)
    return _merged([_grep_hazard(args),
                    "loads a .ugrep configuration file, which can name a filter command"])


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


def _less_hazard(args: list[str]) -> str | Hazard:
    """less writes a log file (`-o`/`-O`/`--log-file`/`--LOG-FILE`), loads key
    files that can set LESSOPEN — a program run on every file (`-k`,
    `--lesskey-*`) — and runs its own commands at start-up (`+cmd`). Its
    clusters are not modelled letter by letter: any of those letters anywhere
    in a short option counts, which can only add a refusal. `--save-marks`
    (less 668) writes its history file. Only a log file named plainly
    (`-o FILE`, `-OFILE`, `--log-file=FILE`) is a certain write of that file;
    the rest write elsewhere, run something, or may be read otherwise."""
    found: list = []         # every one, not the first (review of PR #75, HIGH 2)
    for k, token in enumerate(args):
        if token == "--":
            break
        if token.startswith("+"):
            found.append("+ runs less commands at start-up")
            continue
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            for long in ("log-file", "LOG-FILE", "lesskey-file", "lesskey-src",
                         "lesskey-content", "save-marks"):
                if name and long.startswith(name):
                    why = f"--{long} writes a file or loads key bindings"
                    if long in ("log-file", "LOG-FILE") and len(name) >= 3:
                        value = value if eq else (args[k + 1] if k + 1 < len(args) else "")
                        found.append(Hazard(why, writes=(value,) if value else ()))
                    else:
                        found.append(why)
                    break
        elif token.startswith("-") and any(c in token[1:] for c in "oOk"):
            why = "-o/-O write a log file and -k loads key bindings"
            if token[1:2] in ("o", "O"):
                value = token[2:] or (args[k + 1] if k + 1 < len(args) else "")
                found.append(Hazard(why, writes=(value,) if value else ()))
            else:
                found.append(why)
    return _merged(found)


_XXD_VALUE_WORDS = {"c": "cols", "g": "groupsize", "l": "len", "n": "name",
                    "o": "offset", "s": "seek", "R": "R"}


def _xxd_hazard(args: list[str]) -> str | Hazard:
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
    if len(operands) > 1:
        return Hazard("writes its second operand, the output file", writes=(operands[1],))
    return ""


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
    backslash escapes, POSIX bracket expressions in `s`'s pattern and in
    addresses (a delimiter inside `[…]` is text, as GNU sed reads it — see
    `_sed_until`), and the
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
    # `-i` and the script's own writes together (review of PR #75, LOW 3:
    # `sed -i 'w <G>' notes.txt` returned at `-i`, and the `w` file was lost).
    edits = Hazard("-i edits files in place", operands=True) if in_place else ""
    if from_file and file_unboxed:
        return _merged([edits, Hazard("-f runs a script file this check cannot read")])
    if not scripts and not from_file:
        if not operands:
            return edits
        scripts = [(operands[0], sandbox)]
    # GNU sed joins its -e fragments with newlines into one program, so they
    # are scanned joined: `-e 'a\\' -e 'text'` is one append.
    text = "\n".join(script for script, boxed in scripts if not boxed)
    if not text:
        return edits
    writes, reads, runs, parsed = _sed_script(text)
    if runs:
        return _merged([edits, Hazard("script runs a command (an e command or the e flag of s)",
                                      tuple(writes), runs=True)])
    if writes:
        return _merged([edits, Hazard("script writes a file (a w or W command, or the w flag "
                                      "of s)", tuple(writes))])
    if reads:
        return _merged([edits, Hazard("script reads a file that is not an operand (an r or R "
                                      "command)")])
    if not parsed:
        return _merged([edits, Hazard("script could not be fully parsed, so it is not provably "
                                      "a read")])
    return edits


def _sed_until(script: str, i: int, delim: str, regex: bool = False) -> int | None:
    """Index just past the closing `delim`, or None if the part never closes.

    GNU sed's rule outside a bracket: a backslash carries the next character
    and an unescaped newline ends the command unfinished. **In a regex part**
    (`s`'s pattern, an address) a bracket expression is read whole first, so
    a delimiter inside one is text: `s/[/]/X/` matches a slash. The
    replacement and both halves of `y` are not bracket-aware — there the
    first unescaped delimiter ends the part, `[` or not.

    Pinned against GNU sed 4.9 itself (2026-10-09, argv only, `--sandbox -n`,
    input on stdin, BRE and ERE alike): `[/]`, `[^/]`, `[]/]`, `[^]/]`,
    `[[:alpha:]/]`, `[[=/=]]`, `[[./.]]`, `[\\/]` and `,[,],` in `s` and in
    `/re/` and `\\%re%` addresses all delimit after the bracket; `[a/…`,
    `[]/…`, `[[:/]/…` and a newline inside a bracket are unterminated; a
    delimiter that *is* `[` is a delimiter; and the replacement `s/x/[/]/`
    ends at the first `/`."""
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
        if ch == "[" and regex:
            end = _sed_bracket_end(script, i)
            if end is None:
                return None
            i = end
            continue
        i += 1
    return None


def _sed_bracket_end(script: str, i: int) -> int | None:
    """Index just past the bracket expression opening at `script[i] == "["`,
    or None if it never closes. POSIX, as GNU sed reads it: an optional `^`,
    then a `]` that would otherwise close it is a literal; `[:class:]`,
    `[=x=]` and `[.x.]` run to their own closer whatever they contain; a
    backslash is an ordinary character (`[\\]` is a bracket holding `\\`); a
    raw newline leaves it unterminated."""
    n, j = len(script), i + 1
    if j < n and script[j] == "^":
        j += 1
    if j < n and script[j] == "]":
        j += 1
    while j < n:
        ch = script[j]
        if ch == "\n":
            return None
        if ch == "[" and j + 1 < n and script[j + 1] in ":=.":
            close = script.find(script[j + 1] + "]", j + 2)
            if close == -1 or "\n" in script[j + 2:close]:
                return None
            j = close + 2
            continue
        if ch == "]":
            return j + 1
        j += 1
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
        end = _sed_until(script, j, delim, regex=True)
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
            # `s`'s pattern is a regex (bracket-aware); its replacement and
            # both halves of `y` are not.
            k = _sed_until(script, i + 1, delim, regex=command == "s")
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
                      "compress-program": "--compress-program runs a program"},
        writes_value=frozenset({"o", "output"})),
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
                             "houtro", "infofile"}),
        writes_value=frozenset({"o"})),
    "ss": _Getopt(
        arg_shorts="fANF",
        danger_shorts={"D": "-D writes raw socket data to a file", "K": "-K closes sockets"},
        arg_longs=frozenset({"family", "query", "socket", "net", "filter", "bpf-map-id"}),
        danger_longs={"diag": "--diag writes raw socket data to a file",
                      "kill": "--kill closes sockets"},
        writes_value=frozenset({"D", "diag"})),
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
    # GNU grep ∪ ugrep (see `_grep_hazard`); egrep/fgrep are GNU's `grep -E`/
    # `grep -F` wrappers, and ugrep installs under the same names.
    "grep": _grep_hazard,
    "egrep": _grep_hazard,
    "fgrep": _grep_hazard,
    "ugrep": _grep_hazard,
    "ug": _ug_hazard,
    "bfs": _find_hazard,
}

# Audited against the installed binaries' own `--help`/`man` (2026-10-09, see
# the section comment for the versions; `jq` from its documentation, it is not
# installed), with nothing that writes, runs a program or reads past its
# operands. Options that only *read* a named file — `grep -f`, `sort
# --files0-from`, `date -f`, `du -X`, `hexdump -f`, `diff --from-file` — are
# not hazards: the file is named on the line, where the secrets layer sees it.
# Two that run a *fixed* program are not hazards either, as with rg's and
# file's built-in decompression: `diff -l` pipes through `pr`. And two that
# write state no one else reads are recorded, not listed: `sort -T DIR`
# (temporary files it deletes) and `file -p` (restores the atime it changed).
_NO_HAZARD = frozenset({
    "ls", "cat", "head", "tail", "wc", "stat", "echo",
    "pwd", "which", "true", "test", "[", "cut", "printf", "dirname", "basename",
    "du", "df", "whoami", "uname", "ps", "uptime", "more", "jq", "diff", "cmp",
    "md5sum", "sha1sum", "sha256sum", "sha512sum", "b2sum", "cksum", "realpath",
    "readlink", "column", "od", "hexdump", "strings", "nl", "tac", "base64",
})

# What `READER_HAZARDS` and `_NO_HAZARD` were audited against: for each probe
# binary, the implementation names its `--version` may report. The rules suite
# runs each `--version` (argv, no shell) and prints AUDIT NOT VERIFIED when the
# installed one is not listed — informational, never a failure: a new
# implementation needs its options read before the table can claim it.
AUDITED_IMPLEMENTATIONS = {
    "grep": ("GNU grep", "ugrep"),
    "find": ("GNU findutils", "bfs"),
    "sed": ("GNU sed",),
    "ls": ("uutils coreutils", "GNU coreutils"),
    "sort": ("uutils coreutils", "GNU coreutils"),
    "rg": ("ripgrep",),
}


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
                   "perl": "eE", "ruby": "e", "php": "r", **{s: "c" for s in SHELLS}}
# Letters that take a value end a cluster: `python -mcProfile` names a module.
_VALUE_LETTERS = {"python": "mWXQ", "python3": "mWXQ", "node": "rC", "bun": "r",
                  **{s: "oO" for s in SHELLS}}
_INLINE_LONGS = {"node": ("eval", "print"), "bun": ("eval", "print"),
                 "fish": ("command",), "nu": ("commands",)}
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


# --- vex: what a line runs (VX-1, review round 2) ------------------------------
#
# **Program position is judged strictly; the four-letter threshold is for
# arguments only** (MED 1). Round 1 let a glob through when it kept fewer than
# four of vexcom's letters, which is right for `ls v*` and wrong for the word a
# line *runs*: `uv run /mnt/c/…/tools/v*/*/*/v*.exe --name x --slot 1 --write
# build/x.bin` was ALLOW (an ALLOW stem handing a glob to its program), so v1's
# human-backed gate ran it with nobody asked and v2 allowed it under AUTO and
# ASK alike. So every word the line will start as a program is found —
# command position, past each wrapper, the program a runner starts (options
# parsed), a `sh -c` string, `eval`, `watch`, `find -exec`, `xargs`, a `$(…)`,
# backticks, process substitution and a here-string fed to a shell — and:
#
# * a glob, or a word holding an expansion (`${X}com.exe`, `$X.exe`), that
#   could be `vexcom`/`vexcom.exe` by its file name is a vexcom run: DENY;
# * a program that cannot be told — nothing but an expansion (`$X`, `"$@"`,
#   `$(…)`), a program read from input (`xargs env`, `find -exec {}`), a
#   candidate an option the runner's table does not know may have moved — that
#   could be vexcom is asked whatever else stands (`vex_uncoverable`);
# * `vexbuild`, or `jarvis.v2.vex` run as a module (`python -m`, `uv run -m`,
#   `python -m runpy`), asks like any command (V-2).
#
# Runner and wrapper options are read from tables (`_Opts`). **An option the
# table does not know may or may not take the next word, and both readings are
# followed** (`_option_walk`), so a short table can only make a program
# uncertain, never hide it: `uv run --no-project vexbuild` asks, as does `uv
# run --some-future-flag x vexbuild`. Nothing here is general runner
# unwrapping — `uv run rm -rf ~` is judged as it always was; only what could be
# vexcom or vexbuild moves.
_VEX_MODULE = re.compile(r"jarvis\.v2\.vex(\.[\w.]*)?")
_PYTHON = re.compile(r"(python|pypy)[0-9.]*")
VEXBUILD_ASKS = "this runs vexbuild, which asks unless you allowlist exactly it"
PROGRAM_WORD_CAP = 1024    # characters in one program word
PROGRAM_CAP = 4096         # segments and programs examined in one line
PROGRAM_DEPTH = 4          # `sh -c`, `$(…)`, wrappers and runners followed inside each other
SUBSTITUTION_CAP = 64      # `$(…)`, backticks and `<(…)` read in one text
RUNS, UNKNOWN, VEXBUILD = "runs", "unknown", "vexbuild"


class _TooComplex(Exception):
    """Past a cap: the line is too complex to judge."""


TooComplex = _TooComplex      # what `protected_state` and v2 catch
STARTS_READINGS = 32       # places a wrapper's or runner's program may stand, per call


# --- the work budget for what wrappers and runners start (review of PR #75) ---
#
# An option the table does not know is followed both ways, so each one
# doubles where a program may stand, and every reading is judged again,
# runners inside runners: `npx --x ` forty times before a command took 7.5 s
# in `decide` and 17 s in `command_touch`, sixty times 52 s and 139 s —
# synchronously, inside dispatch and v2's permit. `PROGRAM_DEPTH` bounded the
# depth, not the work. So every reader of the table charges **one budget per
# line** (`work_scope`; nested scopes share the outermost, so `decide`'s owner
# ops and body backstop spend the same one), readings already judged are not
# judged again (`_Work.memo`), and past the budget or any cap the answer is
# **too complex to judge**: asked, never allowed, never covered by an
# allowlist entry (`runners_unsure`), always-ask in v2.
WORK_CAP = 20_000          # words a line's wrappers and runners may make the readers scan
RUNNERS_TOO_COMPLEX = ("what its wrappers and runners start is too complex to judge (past the "
                       "work budget or a nesting cap), so it is asked, and no allowlist entry "
                       "covers it")
NUL_TOO_COMPLEX = ("the line holds a NUL byte, which no shell can be handed: too complex to "
                   "judge, so it is asked, and no allowlist entry covers it")


@dataclass
class _Work:
    left: int
    exhausted: str | None = None
    memo: dict = field(default_factory=dict)       # `_judge_words` per reading
    cache: dict = field(default_factory=dict)      # protected_state: names resolved, judged


_WORK: contextvars.ContextVar = contextvars.ContextVar("jarvis_rules_work", default=None)


@contextlib.contextmanager
def work_scope(*, fresh: bool = False):
    """The line's work budget: the outermost scope makes it and nested ones
    share it (`fresh` always makes one)."""
    work = None if fresh else _WORK.get()
    if work is not None:
        yield work
        return
    work = _Work(WORK_CAP)
    token = _WORK.set(work)
    try:
        yield work
    finally:
        _WORK.reset(token)


def charge(units: int) -> None:
    """Spend `units` of the line's budget; past it, `TooComplex` (and the
    scope remembers why, so the line is asked)."""
    work = _WORK.get()
    if work is None:
        return
    work.left -= max(units, 1)
    if work.left < 0:
        work.exhausted = work.exhausted or RUNNERS_TOO_COMPLEX
        raise _TooComplex


def exhaust(reason: str = RUNNERS_TOO_COMPLEX) -> None:
    """A reader past one of its own caps: the line is too complex to judge."""
    work = _WORK.get()
    if work is not None:
        work.exhausted = work.exhausted or reason


@dataclass(frozen=True)
class _Opts:
    """A wrapper's or runner's options, as far as finding its program goes."""
    flags: str = ""                     # short letters that take no value
    values: str = ""                    # short letters that take one
    optional: str = ""                  # short letters whose value, if any, is glued on
    long_flags: frozenset = frozenset()
    long_values: frozenset = frozenset()
    strings: frozenset = frozenset()    # options whose value is a command line (npx -c)
    module: frozenset = frozenset()     # options that make the program a Python module
    shell: frozenset = frozenset()      # options that make the rest one command line
    positionals: int = 0                # words between the options and the program
    assignments: bool = False           # NAME=VALUE words before the program (env)
    dash: bool = False                  # a lone `-` is an option (env)
    program: bool = True                # False: no operand is a program (script)
    # Options whose value is a file the wrapper itself writes (`time -o FILE`,
    # `script -O FILE`; review of PR #75's delta review, H-B), short letters
    # and long names alike; `operand_writes`: its first operand is one
    # (`script FILE`); `property_writes`: options whose `KEY=VALUE` value may
    # point standard output or error at a file (`systemd-run -p
    # StandardOutput=file:PATH`).
    writes: frozenset = frozenset()
    operand_writes: bool = False
    property_writes: frozenset = frozenset()


def _opts(flags="", values="", optional="", lflags="", lvalues="", strings="", module="",
          shell="", positionals=0, assignments=False, dash=False, program=True, writes="",
          operand_writes=False, property_writes="") -> _Opts:
    return _Opts(flags, values, optional, frozenset(lflags.split()), frozenset(lvalues.split()),
                 frozenset(strings.split()), frozenset(module.split()), frozenset(shell.split()),
                 positionals, assignments, dash, program, frozenset(writes.split()),
                 operand_writes, frozenset(property_writes.split()))


# Wrappers: run their operand as a program. **This table and `_RUNNER_SPECS`
# below are the one table of what runs another program** (2026-10-12): the
# vex reading (`_Walk`), `decide` (`_judge_started`), the owner-op detector
# (`protected_state._peel_runner`), the gate-state write detector and v2's
# always-ask stems all read them through `starts()`. `_WRAPPERS` above is
# still the naive peel `unwrap` does for the allowlist's stems and
# `run_readonly`, whose meanings are their own; `decide` judges both readings
# and the worse wins, so the table can only add a refusal there.
_WRAPPER_OPTS = {
    # uutils env 0.8.0 (`env --help`): -u, -C, -S, -a and -f take a value.
    "env": _opts("i0vhV", "uCSaf", lflags="ignore-environment null debug list-signal-handling "
                 "help version block-signal default-signal ignore-signal",
                 lvalues="unset chdir split-string argv0 file", assignments=True, dash=True),
    "nohup": _opts(lflags="help version"),
    "chronic": _opts("evh"),
    "timeout": _opts("v", "sk", lflags="preserve-status foreground verbose help version",
                     lvalues="signal kill-after", positionals=1),
    "setsid": _opts("cfw", lflags="ctty fork wait help version"),
    "stdbuf": _opts(values="ioe", lvalues="input output error"),
    "nice": _opts("0123456789", "n", lvalues="adjustment"),
    "ionice": _opts("t", "cnpPu", lflags="ignore help version",
                    lvalues="class classdata pid pgid uid"),
    "command": _opts("p"),
    "exec": _opts("cl", "a"),
    # GNU time 1.9: `-o FILE`/`--output=FILE` writes FILE (with `-a`, appends).
    "time": _opts("pavq", "fo", lflags="portability verbose quiet append help version",
                  lvalues="format output", writes="o output"),
    "xargs": _opts("0prtxo", "adEILnPs", optional="eil",
                   lflags="null interactive no-run-if-empty verbose exit open-tty show-limits "
                   "help version eof replace max-lines",
                   lvalues="arg-file delimiter max-args max-procs max-chars process-slot-var"),
    "watch": _opts("bcCdeftgprwx", "nq", lflags="beep color no-color differences errexit chgexit "
                   "exec no-title no-wrap no-linewrap precise help version",
                   lvalues="interval equexit"),
    "busybox": _opts(),
    "systemd-run": _opts("rdtPSqG", "puEHM",
                         lflags="user system scope remain-after-exit send-sighup same-dir pty pipe "
                         "shell quiet collect wait no-block no-ask-password slice-inherit help "
                         "version",
                         lvalues="unit property description slice service-type uid gid nice "
                         "working-directory setenv host machine on-active on-boot on-startup "
                         "on-unit-active on-unit-inactive on-calendar timer-property "
                         "path-property socket-property json expand-environment",
                         property_writes="p property socket-property timer-property "
                         "path-property"),
    "flock": _opts("sxeunoFhV", "wEc", lflags="shared exclusive unlock nonblock nb close no-fork "
                   "verbose help version", lvalues="timeout wait conflict-exit-code command",
                   strings="c command", positionals=1),
    # util-linux script 2.41: its file operand (default `typescript`), `-I`,
    # `-O`, `-B`, `-T` and their long names, and `-t[FILE]`/`--timing=FILE`
    # each write a log file.
    "script": _opts("aefqhV", "cEIOBTmo", optional="t", lflags="append return flush quiet help "
                    "version force timing", lvalues="command echo log-in log-out log-io "
                    "log-timing logging-format output-limit", strings="c command", program=False,
                    writes="I O B T t log-in log-out log-io log-timing timing",
                    operand_writes=True),
}
# The rest of this table writes no file it is told of (the audit of review of
# PR #75's delta review, H-B, from each tool's own `--help`): env's `-f FILE`
# and xargs' `-a FILE` read; stdbuf's `-o MODE` is a buffering mode; nohup
# writes only `nohup.out` (in the working folder or HOME, when its output is a
# terminal) — a fixed name, never a gate file; flock opens its lock file
# read-only (`O_RDONLY|O_CREAT`): it may create an empty one, never write or
# truncate it. The runners' options name folders they cache into
# (`--cache-dir`) or files they read (`--config-file`, `--env-file`,
# `--userconfig`); none writes a file it names.

_STD_FILE = re.compile(r"Standard(?:Output|Error)=(?:file|append|truncate):(.+)", re.S)
# A unit property that holds a command line: `ExecStart=`, `ExecStartPre=`,
# `ExecStartPost=`, `ExecCondition=`, `ExecReload=`, `ExecStop=`,
# `ExecStopPost=` and their `…Ex=` forms (third delta review of PR #75, LOW
# 2: `systemd-run -p ExecStartPost=<a command> true` ran a command line no
# reader looked at). Every `Exec…=` property is read so — `ExecSearchPath=`
# and `ExecPaths=` change which program runs or what it may reach — which
# can only ask or refuse. systemd's own prefixes (`@`, `-`, `:`, `+`, `!`,
# `|`) are taken off the command.
_EXEC_PROPERTY = re.compile(r"\s*Exec[A-Za-z]*=(.*)", re.S)

_UV_FLAGS = ("quiet verbose native-tls offline no-cache no-progress no-config preview no-preview "
             "managed-python no-managed-python no-python-downloads show-settings help version")
_UV_VALUES = ("cache-dir color directory project config-file python-preference "
              "allow-insecure-host preview-features")
_UV_RUN = _opts(
    "qvnhmsU", "pwifPC",
    lflags=_UV_FLAGS + " module script gui-script all-extras no-dev dev no-default-groups "
    "all-groups only-dev no-editable editable exact inexact no-env-file isolated active no-active "
    "no-sync locked frozen all-packages no-project no-index upgrade reinstall pre "
    "no-build-isolation compile-bytecode no-sources no-build no-binary refresh no-install-project "
    "no-install-workspace no-install-local",
    lvalues=_UV_VALUES + " extra no-extra group no-group only-group env-file with with-editable "
    "with-requirements package python max-recursion-depth index default-index index-url "
    "extra-index-url find-links upgrade-package reinstall-package index-strategy "
    "keyring-provider resolution prerelease fork-strategy config-setting "
    "no-build-isolation-package exclude-newer exclude-newer-package link-mode no-build-package "
    "no-binary-package refresh-package python-platform torch-backend no-install-package from "
    "constraints overrides build-constraints with-executables-from",
    module="m module")
_UV = _opts("qvnhV", lflags=_UV_FLAGS, lvalues=_UV_VALUES)
_NPX = _opts("yqh", "pcw", lflags="yes no ignore-existing quiet no-install workspaces "
             "include-workspace-root help", lvalues="package call node-options prefix userconfig "
             "cache registry workspace shell loglevel", strings="c call")
_NPM = _opts("ysqdgh", "pcwC", lflags="yes no silent quiet json global workspaces "
             "include-workspace-root help", lvalues="package call workspace prefix loglevel "
             "registry userconfig cache", strings="c call")
_BUNX = _opts("h", "p", lflags="bun silent verbose help", lvalues="package")
_PNPM = _opts("rh", "FC", lflags="recursive silent help", lvalues="filter dir workspace-concurrency "
              "reporter loglevel")
_PNPM_EXEC = _opts("rch", "FC", lflags="recursive parallel shell-mode report-summary help",
                   lvalues="filter dir resume-from workspace-concurrency reporter",
                   shell="c shell-mode")
_PNPM_DLX = _opts("csh", lflags="shell-mode silent help", lvalues="package allow-build",
                  shell="c shell-mode")
_POETRY = _opts("qvVnh", "CP", lflags="quiet verbose version no-interaction ansi no-ansi "
                "no-plugins no-cache help", lvalues="directory project")
_PIPX = _opts("qvh", lflags="quiet verbose version help")
_PIPX_RUN = _opts("eqvh", "i", lflags="no-cache path pypackages editable system-site-packages "
                  "fetch-missing-python quiet verbose help",
                  lvalues="spec python pip-args index-url backend")
_PDM = _opts("vqVhI", lflags="verbose quiet version help ignore-python")
_PDM_RUN = _opts("ljsgvqh", "p", lflags="list json site-packages global verbose quiet recreate "
                 "help", lvalues="project venv")
_HATCH = _opts("vqh", "ep", lflags="verbose quiet interactive no-interactive version help",
               lvalues="env project color data-dir cache-dir config")
_PLAIN = _opts("vqh", lflags="verbose quiet help version")
_PIXI_RUN = _opts("vqnh", "e", lflags="frozen locked clean-env force-activate no-install "
                  "revalidate skip-deps dry-run verbose quiet help",
                  lvalues="manifest-path environment executable pypi-keyring-provider "
                  "concurrent-solves concurrent-downloads color")
_PIXI_EXEC = _opts("h", "scp", lflags="force-reinstall help", lvalues="spec channel platform with")
_CONDA_RUN = _opts("vh", "np", lflags="no-capture-output live-stream dev debug-wrapper-scripts "
                   "verbose help", lvalues="name prefix cwd")
_BUN_RUN = _opts("bh", "Fcrdl", lflags="silent bun watch hot no-clear-screen smol if-present help",
                 lvalues="cwd env-file filter config preload main-fields tsconfig-override define "
                 "loader port conditions elide-lines")
_STRING = "string"          # the subcommand's next word is a command line (`bun exec`)

_NPM_SPEC = (_NPM, {"exec": _NPM, "x": _NPM})
_PNPM_SPEC = (_PNPM, {"exec": _PNPM_EXEC, "dlx": _PNPM_DLX})
_YARN_DLX = _opts("q", "p", lflags="quiet", lvalues="package")
# yarn runs a program through `exec` and `dlx`, and through them inside a
# workspace: `yarn workspace <name> exec …` and `yarn workspaces foreach
# [options] exec …` (yarn 4's `--help`; review of PR #75, MED 2).
_YARN_FOREACH = _opts("ARWvpitn", "j", lflags="all recursive worktree verbose parallel "
                      "interlaced topological topological-dev no-private dry-run help",
                      lvalues="from jobs include exclude")
_YARN_SPEC = (_opts("h", lvalues="cwd", lflags="help"),
              {"exec": _opts(), "dlx": _YARN_DLX,
               "workspace": (_opts(positionals=1), {"exec": _opts(), "dlx": _YARN_DLX}),
               "workspaces": (_PLAIN, {"foreach": (_YARN_FOREACH, {"exec": _opts()})})})

# Runners: a stem's own program table, or (its options before a subcommand,
# {subcommand: what follows}). A subcommand not listed starts no program —
# `uv pip`, `npm run` (a package.json script by that name, not a program).
_RUNNER_SPECS = {
    "npx": _NPX, "bunx": _BUNX, "uvx": _UV_RUN, "pnpx": _PNPM_DLX,
    "uv": (_UV, {"run": _UV_RUN, "tool": (_UV, {"run": _UV_RUN})}),
    "poetry": (_POETRY, {"run": _POETRY}),
    "pipx": (_PIPX, {"run": _PIPX_RUN}),
    "pdm": (_PDM, {"run": _PDM_RUN}),
    "rye": (_PLAIN, {"run": _opts("lvqh", lflags="list verbose quiet help",
                                  lvalues="pyproject")}),
    "hatch": (_HATCH, {"run": _PLAIN}),
    "pixi": (_PLAIN, {"run": _PIXI_RUN, "exec": _PIXI_EXEC}),
    "conda": (_PLAIN, {"run": _CONDA_RUN}),
    "mamba": (_PLAIN, {"run": _CONDA_RUN}),
    "micromamba": (_PLAIN, {"run": _CONDA_RUN}),
    "bun": (_PLAIN, {"run": _BUN_RUN, "x": _BUNX, "exec": _STRING}),
    "npm": _NPM_SPEC,
    "pnpm": _PNPM_SPEC,
    "yarn": _YARN_SPEC, "yarnpkg": _YARN_SPEC,
    # corepack runs the package manager it is named (`corepack yarn@4 exec …`;
    # review of PR #75, MED 2).
    "corepack": (_PLAIN, {"yarn": _YARN_SPEC, "yarnpkg": _YARN_SPEC, "npm": _NPM_SPEC,
                          "pnpm": _PNPM_SPEC, "npx": _NPX, "pnpx": _PNPM_DLX}),
}
_SHELL_STEMS = SHELLS
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# Words that open or close a compound command rather than name a program.
_LEAD_WORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(",
                         "((", "coproc", "fi", "done", "esac", "}", ")", "))", ";;"})


def _wrote(opts: _Opts, name: str, value: str, writes: list | None, long: bool,
           hidden: list | None = None) -> None:
    """Record the file an option's value names, when that option writes one:
    by name, or — for a long option this table does not know — as a unique
    prefix of one that does (getopt_long reads `--out=F` as `--output=F`).
    A property option's `Exec…=` value is a command line the unit runs:
    into `hidden`."""
    if writes is None or not name:
        return

    def named(options: frozenset) -> bool:
        return name in options or (long and any(
            w.startswith(name) and len(w) > 1 for w in options))

    if named(opts.property_writes):
        match = _STD_FILE.fullmatch(value.strip())
        if match:
            writes.append(match.group(1))
        match = _EXEC_PROPERTY.fullmatch(value)
        if match and hidden is not None:
            line = match.group(1).lstrip().lstrip("@-:+!|")
            if line.strip():
                hidden.append(line)
    if named(opts.writes):
        writes.append(value)


def _option_walk(tokens: list[str], start: int, opts: _Opts, writes: list | None = None,
                 hidden: list | None = None):
    """(indices where the program may stand, command-line strings, whether
    the program is a module, whether the rest is one command line). An
    index of `len(tokens)` means no program. An unknown option is followed
    both ways — taking the next word, and not — so the set is every place
    the program could be. `writes`, when given, receives every value of an
    option that writes a file (`_Opts.writes`), in any reading; `hidden`,
    every command line a property option names (`Exec…=`)."""
    n = len(tokens)
    found: set[int] = set()
    strings: list[str] = []
    module = shell = False
    todo, seen = [(start, opts.positionals)], set()
    while todo:
        state = todo.pop()
        if state in seen:
            continue
        seen.add(state)
        i, left = state
        if i >= n:
            found.add(n)
            continue
        token = tokens[i]
        if token == "--":
            found.add(min(i + 1 + left, n))
            continue
        if (opts.dash and token == "-") or (opts.assignments and _ASSIGNMENT.fullmatch(token)):
            todo.append((i + 1, left))
            continue
        if token.startswith("--") and len(token) > 2:
            name, eq, value = token[2:].partition("=")
            if (name not in opts.long_values and name not in opts.long_flags
                    and (opts.writes or opts.property_writes)):
                # getopt_long (GNU time, util-linux script, systemd-run) takes
                # a unique prefix: `--outp F` is `--output F`, a certain write.
                full = [w for w in opts.long_values | opts.long_flags if w.startswith(name)]
                if len(full) == 1 and full[0] in opts.writes | opts.property_writes:
                    name = full[0]
            module = module or name in opts.module
            shell = shell or name in opts.shell
            following = tokens[i + 1] if i + 1 < n else ""
            if name in opts.long_values:
                if name in opts.strings:
                    strings.append(value if eq else following)
                _wrote(opts, name, value if eq else following, writes, True, hidden)
                todo.append((i + (1 if eq else 2), left))
            elif name in opts.long_flags or eq:
                if name in opts.strings and eq:
                    strings.append(value)
                if eq:
                    _wrote(opts, name, value, writes, True, hidden)
                todo.append((i + 1, left))
            else:
                _wrote(opts, name, following, writes, True, hidden)
                todo += [(i + 1, left), (i + 2, left)]
            continue
        if token.startswith("-") and len(token) > 1:
            letters = token[1:]
            for k, letter in enumerate(letters):
                module = module or letter in opts.module
                shell = shell or letter in opts.shell
                if letter in opts.values:
                    glued = letters[k + 1:]
                    if letter in opts.strings:
                        strings.append(glued or (tokens[i + 1] if i + 1 < n else ""))
                    _wrote(opts, letter, glued or (tokens[i + 1] if i + 1 < n else ""), writes,
                           False, hidden)
                    todo.append((i + (1 if glued else 2), left))
                    break
                if letter in opts.optional:
                    if letters[k + 1:]:
                        _wrote(opts, letter, letters[k + 1:], writes, False, hidden)
                    todo.append((i + 1, left))
                    break
                if letter in opts.flags:
                    if k == len(letters) - 1:
                        todo.append((i + 1, left))
                    continue
                todo += [(i + 1, left), (i + 2, left)]
                break
            continue
        if left:
            todo.append((i + 1, left - 1))
            continue
        found.add(i)
    return found, strings, module, shell


def _runner_walk(tokens: list[str], spec):
    """`_option_walk` for a runner, through its subcommands."""
    if isinstance(spec, _Opts):
        return _option_walk(tokens, 1, spec)
    before, subcommands = spec
    found: set[int] = set()
    strings: list[str] = []
    module = shell = False
    places, *_ = _option_walk(tokens, 1, before)
    if len(places) > STARTS_READINGS:
        raise _TooComplex
    for p in places:
        # `corepack yarn@4.1.0 …` names a version with the program.
        follow = (subcommands.get(tokens[p]) or subcommands.get(str(tokens[p]).split("@", 1)[0])
                  if p < len(tokens) else None)
        if follow is None:
            continue
        if follow == _STRING:
            strings += tokens[p + 1:p + 2]
            continue
        inner = tokens[p:]
        more, texts, mod, sh = _runner_walk(inner, follow)
        found |= {p + q for q in more}
        strings += texts
        module, shell = module or mod, shell or sh
    return found, strings, module, shell


def _env_splice(tokens: list[str]) -> list[str]:
    """`env -S STRING` / `--split-string=STRING` splits STRING into words
    that stand where it stood; read the line as env will."""
    out, i, splices = tokens[:1], 1, 0
    while i < len(tokens):
        token = tokens[i]
        value = None
        if token == "--split-string" and i + 1 < len(tokens):
            value, i = tokens[i + 1], i + 2
        elif token.startswith("--split-string="):
            value, i = token.split("=", 1)[1], i + 1
        elif token.startswith("-") and not token.startswith("--") and "S" in token[1:]:
            k = token.index("S", 1)
            if token[1:k].strip("i0v") == "":
                if len(token) > 1 and k > 1:
                    out.append(token[:k])
                value = token[k + 1:] or (tokens[i + 1] if i + 1 < len(tokens) else "")
                i += 1 if token[k + 1:] else 2
        if value is not None:
            splices += 1
            if splices > 8:
                raise _TooComplex
            out += _tokens(value)
            continue
        if token in ("-u", "-C", "--unset", "--chdir"):
            out += tokens[i:i + 2]
            i += 2
            continue
        if token == "--" or not token.startswith("-"):
            if not _ASSIGNMENT.fullmatch(token):
                out += tokens[i:]
                break
        out.append(token)
        i += 1
    return out


# ssh runs its command on another host — which may be this one — as a command
# line. Read only where that matters (`starts(remote=True)`: the owner-op
# detector, for `ssh localhost jarvis restart`); `decide` already asks every
# ssh, and the vex reading sees vexcom by name anywhere on the line.
_REMOTE_OPTS = {
    "ssh": _opts("46AaCfGgKkMNnqsTtVvXxYy", "BbcDEeFIiJLlmOoPpQRSWw", positionals=1),
}


@dataclass(frozen=True)
class Started:
    """What a wrapper or a runner at the front of a command starts (`starts`).

    `programs` holds one word list per reading of where the program stands —
    more than one when an option the table does not know may or may not take
    the next word (`sure` is False then) — and `joined` says each is one
    command line, not a program (`watch` without `-x`, pnpm's shell mode,
    ssh's remote command). `strings` are command lines an option carries
    (`npm exec -c`/`--call`, `bun exec`, `flock -c`, `script -c`).
    `appends`: its input is added as more arguments (xargs). `module`: the
    program is a Python module (`uv run -m`). `open_end`: one reading leaves
    it no program at all (an unknown option took the last word). `writes`:
    files the wrapper itself writes (`time -o FILE`, `script FILE`,
    `systemd-run -p StandardOutput=file:FILE`) — `writes_certain` is False
    when an option this table does not know leaves the parse in doubt.
    `hidden`: those of `strings` a unit property holds (`systemd-run -p
    ExecStartPost=CMD`), judged as lines and never auto-approved."""
    programs: tuple = ()
    strings: tuple = ()
    joined: bool = False
    module: bool = False
    appends: bool = False
    open_end: bool = False
    writes: tuple = ()
    writes_certain: bool = True
    hidden: tuple = ()

    @property
    def sure(self) -> bool:
        return len(self.programs) <= 1


def starts(tokens: list[str], *, remote: bool = False) -> Started | None:
    """What `tokens[0]`, a wrapper or a runner from the one table, starts —
    or None when it is neither. A `Started` with no programs and no strings
    starts nothing: `uv pip install`, `npm run build` (a package.json script,
    not a program), `command -v x` (a lookup). Raises `_TooComplex` past
    `env -S`'s bound (`_env_splice`), or when an option the table does not
    know leaves more than `STARTS_READINGS` places the program may stand.
    Leading assignments are not the program (`strip_assignments`)."""
    tokens = strip_assignments(tokens)
    if not tokens:
        return None
    stem = _basename(tokens[0])
    if stem in _WRAPPER_OPTS or (remote and stem in _REMOTE_OPTS):
        tokens = list(tokens)
        opts = _WRAPPER_OPTS.get(stem) or _REMOTE_OPTS[stem]
        if stem == "command" and any(t.startswith("-") and set(t[1:]) & {"v", "V"}
                                     for t in tokens[1:2]):
            return Started()                    # `command -v x` looks x up
        if stem == "env":
            tokens = _env_splice(tokens)
        writes: list[str] = []
        hidden: list[str] = []
        places, strings, _module, _shell = _option_walk(tokens, 1, opts, writes, hidden)
        strings += hidden
        if len(places) > STARTS_READINGS or len(strings) > STARTS_READINGS:
            raise _TooComplex
        hidden_lines = tuple(dict.fromkeys(hidden))
        if opts.operand_writes:
            writes += [tokens[p] for p in sorted(places) if p < len(tokens)]
        writes_certain = len(places) <= 1
        if not opts.program:
            return Started(strings=tuple(strings), writes=tuple(dict.fromkeys(writes)),
                           writes_certain=writes_certain, hidden=hidden_lines)
        joined = stem in _REMOTE_OPTS or (
            stem == "watch" and not any(t in ("-x", "--exec") for t in tokens[1:]))
        return Started(tuple(tuple(tokens[p:]) for p in sorted(places) if p < len(tokens)),
                       tuple(strings), joined=joined, appends=stem == "xargs",
                       open_end=len(tokens) in places, writes=tuple(dict.fromkeys(writes)),
                       writes_certain=writes_certain, hidden=hidden_lines)
    spec = _RUNNER_SPECS.get(stem)
    if spec is None:
        return None
    tokens = list(tokens)
    places, strings, module, shell = _runner_walk(tokens, spec)
    if len(places) > STARTS_READINGS or len(strings) > STARTS_READINGS:
        raise _TooComplex
    return Started(tuple(tuple(tokens[p:]) for p in sorted(places) if p < len(tokens)),
                   tuple(strings), joined=shell, module=module, open_end=len(tokens) in places)


def takes_value(stem: str, token: str) -> bool:
    """Whether `token`, an option of the wrapper `stem`, takes the next word
    as its value — from the one table (`_WRAPPER_OPTS`), so `protected_state`
    keeps no second copy of, say, env's `-u NAME`."""
    opts = _WRAPPER_OPTS.get(stem)
    if opts is None or not token.startswith("-") or token == "-":
        return False
    if token.startswith("--"):
        name, eq, _ = token[2:].partition("=")
        return not eq and name in opts.long_values
    letters = token[1:]
    for k, letter in enumerate(letters):
        if letter in opts.values:
            return k == len(letters) - 1
        if letter not in opts.flags:
            return False
    return False


def started_commands(segment: str) -> list[list[str]]:
    """The words of every program the wrappers and runners at the front of
    one segment start — through one another, every reading, and through the
    command lines they hand a shell — each as `unwrap` leaves it. What v2's
    always-ask rules read beside the segment's own words: `npx vercel --prod`
    is a `vercel --prod`, `uv run git push --force` a `git push --force`.
    [] when it starts nothing. Bounded by the line's work budget, each
    reading walked once; past the budget, `PROGRAM_DEPTH` or `PROGRAM_CAP`
    it raises `TooComplex` — the caller asks (review of PR #75)."""
    out: list[list[str]] = []
    seen: set = set()

    def walk(words: list[str], depth: int) -> None:
        # From the program on: `then npx vercel --prod` starts vercel too.
        words = command_start(words)
        if not words or tuple(words) in seen:
            return
        seen.add(tuple(words))
        if _basename(words[0]) not in _WRAPPER_OPTS and _basename(words[0]) not in _RUNNER_SPECS:
            return
        if depth > PROGRAM_DEPTH or len(out) >= PROGRAM_CAP:
            raise _TooComplex
        charge(len(words))
        started = starts(words)
        if started is None:
            return
        if started.joined:
            lines = list(started.strings) + [" ".join(p) for p in started.programs]
            programs: list[list[str]] = []
        else:
            lines, programs = list(started.strings), [list(p) for p in started.programs]
        for line in lines:
            programs += [_tokens(strip_redirections(s)) for s in segments(join_continuations(line))]
        for program in programs:
            inner = _unwrap(list(program))
            if inner:
                out.append(inner)
                walk(program, depth + 1)

    with work_scope():
        walk(_tokens(strip_redirections(segment)), 0)
    return out


def _shell_command(tokens: list[str]) -> tuple[list[str], bool]:
    """(the command string a shell runs, whether it reads its commands from
    standard input). The `c` is found in a cluster (`bash -lc`); `-o`/`-O`
    and `--rcfile`/`--init-file` take the next word; `--` or `-` end the
    options; fish's `--command`. Only the first operand is the command:
    the rest are `$0`, `$1`, and a program named by one of those is an
    expansion in the command, which is read as one."""
    inline = stdin = False
    operands: list[str] = []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token in ("--", "-"):
            operands = tokens[i:]
            break
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            if name in ("command", "commands"):         # fish; nu
                return ([value] if eq else tokens[i:i + 1]), False
            if name in ("rcfile", "init-file", "init-command") and not eq:
                i += 1
            continue
        if len(token) > 1 and token[0] in "-+":
            for letter in token[1:]:
                if letter == "c" and token[0] == "-":
                    inline = True
                elif letter == "s" and token[0] == "-":
                    stdin = True
                elif letter in "oO":
                    i += 1
            continue
        operands = tokens[i - 1:]
        break
    if inline:
        return operands[:1], False
    return [], stdin or not operands


def _xargs_replace(tokens: list[str]) -> str | None:
    """The string `xargs -I`/`-i`/`--replace` substitutes, if any."""
    for i, token in enumerate(tokens[1:], 1):
        if token == "-I" and i + 1 < len(tokens):
            return tokens[i + 1]
        if token.startswith("-I") and len(token) > 2:
            return token[2:]
        if token in ("-i", "--replace"):
            return "{}"
        if token.startswith("-i") and len(token) > 2:
            return token[2:]
        if token.startswith("--replace="):
            return token.split("=", 1)[1] or "{}"
        if not token.startswith("-"):
            break
    return None


def _substitution_texts(text: str) -> list[str]:
    """The commands inside `$(…)` and backticks (outside single quotes; bash
    expands both inside double ones) and `<(…)`/`>(…)` (outside any quotes),
    at the top level of `text` — each is read again for its own. Linear; at
    most `SUBSTITUTION_CAP`."""
    return _substitutions(text)[0]


def _substitutions(text: str) -> tuple[list[str], str]:
    """(`_substitution_texts`, the text with each of them emptied to `$(_)`,
    `` `_` `` or `<(_)`). The walk segments the emptied text: what is inside
    a substitution is its own command line — a `"` or a newline in a commit
    message inside `"$(cat <<<'…')"` must not end the outer quote or the
    outer command."""
    out: list[str] = []
    masked: list[str] = []
    quote = ""
    i, n, last = 0, len(text), 0
    while i < n:
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if not quote and text.startswith("$'", i):
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "'" and not quote:
            quote = "'"
            i += 1
            continue
        if ch == '"':
            quote = "" if quote == '"' else '"'
            i += 1
            continue
        if text.startswith("$((", i):
            i += 3
            continue
        if text.startswith("$(", i) or (not quote and text.startswith(("<(", ">("), i)):
            j = _close_paren(text, i + 2)
            out.append(text[i + 2:j - 1 if text[j - 1:j] == ")" else n])
            masked.append(text[last:i + 2] + "_)")
            i = last = j
        elif ch == "`":
            end = text.find("`", i + 1)
            end = n if end == -1 else end
            out.append(text[i + 1:end])
            masked.append(text[last:i + 1] + "_`")
            i = last = end + 1
        else:
            i += 1
        if len(out) > SUBSTITUTION_CAP:
            raise _TooComplex
    masked.append(text[last:])
    return out, "".join(masked)


class _Word(str):
    """A word as bash reads it: its text with quotes removed, and for each
    character whether it is *active* — a glob or expansion character bash
    will act on (unquoted, or `$` and backticks inside double quotes)."""
    active: tuple

    def __new__(cls, text: str, active):
        word = super().__new__(cls, text)
        word.active = tuple(active)
        return word


def _cut(word: str, start: int, end: int | None = None) -> str:
    """`word[start:end]`, keeping a `_Word`'s mask."""
    if isinstance(word, _Word):
        return _Word(str.__getitem__(word, slice(start, end)), word.active[start:end])
    return word[start:end]


def _span_end(text: str, i: int) -> int:
    """Index just past the `$(…)` (nesting counted) or `` `…` `` at `i`."""
    n = len(text)
    if text[i] == "`":
        end = text.find("`", i + 1)
        return n if end == -1 else end + 1
    return _close_paren(text, i + 2)


def _close_paren(text: str, j: int) -> int:
    """Index just past the `)` that closes a `$(`, `<(` or `>(` whose inside
    starts at `j` (nesting counted, quoted text skipped, so a `1)` in a
    quoted message does not end it), or the text's end."""
    n, depth = len(text), 1
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch in "'\"":
            k = j + 1
            while k < n and text[k] != ch:
                k += 2 if ch == '"' and text[k] == "\\" else 1
            j = k + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


# --- here-documents ------------------------------------------------------------
#
# **A here-document's body is data, not commands** (review round 3): `git
# commit -m "$(cat <<'EOF'` with a `* raise kP` line, a C doc comment written
# with `cat > auton.h <<'EOF'`, a crontab line — each body line used to be
# read as a command, so a `*` or `/*` at its start was "a glob in program
# position" and the whole line DENY. `_inline_heredocs` moves each body into
# its operator as a here-string (`cat <<'EOF'⏎…⏎EOF` → `cat <<<'…'`), so the
# walk reads it only where a shell is fed it (`bash <<EOF`), as it reads a
# here-string. A quoted delimiter keeps the body literal; an unquoted one
# leaves its `$(…)` and backticks live, as bash does. The lexer recognises an
# operator only where bash does — never inside quotes, a comment, `${…}` or
# arithmetic (`$((1<<2))`, `((x<<=1))`, `$[…]`) — because one recognised where
# bash would not hides the lines after it.
_HEREDOC_STOP = " \t\n;|&<>()`"
HEREDOC_CAP = 64           # here-documents open on one line before it is too complex (round 3b)
# The `$'…'` escapes whose decoding by `_ansi_c_decoded` is exactly bash's;
# any other (`\x`, octal, `\u`, `\c`, `\e`, `\?`) makes the delimiter unsure.
_ANSI_C_PLAIN = re.compile(r"(?:[^\\]|\\[\\'\"abfnrtv])*", re.S)


def _heredoc_word(text: str, j: int, in_tick: bool = False) -> tuple[str, bool, int]:
    """(delimiter, quoted, index past it) for the word after `<<`/`<<-`, as
    bash forms it: never expanded, quotes removed (round 3b, each checked
    with `bash -n` on bash 5.3, which names the delimiter it wanted). In
    `"…"` (and `$"…"`) a backslash escapes only `"`, `\\`, `$`, `` ` `` and a
    newline (removed); elsewhere it stays. `$'…'` is decoded. An unquoted
    backslash quotes the next character. `$NAME` stays literal.

    **What it cannot read with certainty raises `_TooComplex`** (asked,
    never allowlist-covered): an unbalanced quote; `$(`, `${`, `$[` or a
    backtick in the word, whose extent bash finds by parsing them; an
    unquoted backslash-newline or a trailing backslash; a `$'…'` escape
    outside `_ANSI_C_PLAIN`; and an extglob `@(`, `*(`, … which bash keeps
    in the word when `extglob` is on. A backtick straight after the word
    ends it only where it closes the backticks the operator is in
    (`in_tick`)."""
    n = len(text)
    while j < n and text[j] in " \t":
        j += 1
    out: list[str] = []
    quoted = False
    while j < n and text[j] not in _HEREDOC_STOP:
        ch = text[j]
        if ch == "'":
            end = text.find("'", j + 1)
            if end == -1:
                raise _TooComplex
            out.append(text[j + 1:end])
            quoted, j = True, end + 1
        elif text.startswith("$'", j):
            k = j + 2
            while k < n and text[k] != "'":
                k += 2 if text[k] == "\\" else 1
            if k >= n or not _ANSI_C_PLAIN.fullmatch(text, j + 2, k):
                raise _TooComplex
            out.append(_ansi_c_decoded(text[j:k + 1]))
            quoted, j = True, k + 1
        elif ch == '"' or text.startswith('$"', j):
            k = j + (2 if ch == "$" else 1)
            while True:
                if k >= n:
                    raise _TooComplex
                c = text[k]
                if c == '"':
                    break
                if c == "\\" and k + 1 < n and text[k + 1] in '"\\$`\n':
                    if text[k + 1] != "\n":
                        out.append(text[k + 1])
                    k += 2
                    continue
                if c == "`" or (c == "$" and text[k + 1:k + 2] in ("(", "{", "[")):
                    raise _TooComplex
                out.append(c)
                k += 1
            quoted, j = True, k + 1
        elif ch == "\\":
            if j + 1 >= n or text[j + 1] == "\n":
                raise _TooComplex
            out.append(text[j + 1])
            quoted, j = True, j + 2
        elif ch == "$" and text[j + 1:j + 2] in ("(", "{", "["):
            raise _TooComplex
        else:
            out.append(ch)
            j += 1
    if j < n and ((text[j] == "`" and not in_tick)
                  or (text[j] == "(" and text[j - 1] in "@*+?!")):
        raise _TooComplex
    return "".join(out), quoted, j


def _here_word(body: str, quoted: bool, keep: bool) -> str:
    """A here-document's body as one quoted word. `keep=False` keeps only
    what bash would run from it: nothing for a quoted delimiter, and the
    `$(…)`/backtick spans of an unquoted one (bash never globs a body)."""
    if not keep:
        if quoted:
            return "''"
        try:
            spans = _substitution_texts(body)
        except _TooComplex:
            spans = [body]
        body = " ".join(f"$({span})" for span in spans)
    if quoted:
        return "'" + body.replace("'", "'\\''") + "'"
    return '"' + body.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _joins(line: str) -> bool:
    """Does this physical line of an unquoted body end in a backslash-newline
    bash removes? Only an odd run: a backslash before a backslash quotes it
    (bash 5.3's `read_a_line`, checked with `bash -n`)."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _inline_heredocs(text: str, keep: bool = True, bodies: list | None = None,
                     unsure: list | None = None) -> str:
    """The text with each here-document's body moved into its operator as a
    here-string (see above). Several on one line are read in order, `<<-`
    strips leading tabs, a body with no terminator runs to the end (as in
    bash), and an operator with no line after it is left as it was. Each
    body, as bash reads it, is also added to `bodies` when one is given
    (the body backstop reads them, `_body_marker`).

    **An unquoted body is read in logical lines** (round 4): bash removes a
    backslash-newline while it reads one, before the line is compared with
    the delimiter, so `EO\\⏎F` ends the body under `<<EOF` (an odd run of
    backslashes only; `<<-` strips the joined line's tabs). A quoted body is
    read line by line, as written. Under `<<-` a line is compared both
    before and after its tabs are stripped (a delimiter can start with a
    tab).

    Raises `_TooComplex` where bash's reading is unsure (round 3b, round 4):
    a delimiter `_heredoc_word` cannot read with a line after it (with none,
    there is no body and the word does not matter); a here-document opened
    inside a command or process substitution (`$(…)`, `<(…)`, `>(…)`) or
    backticks with a body line that starts with its delimiter and holds a
    `)` after it (bash 5.3 ends the body at such a line — `EOF)`, and
    `Exposes a)` under `<<'E'` — and reads the rest as code; bash 5.1 and
    older end it only at `EOF)`; reading on would hide what follows); one
    opened inside a `$(…)`, `<(…)` or `>(…)` that closes before its body;
    a backslash in a delimiter or a body line inside backticks (bash takes
    backslashes out of backtick text before it reads it, and `bash -n` does
    not parse backticks, so no reading of it was checked); a NUL in a text
    with a line after `<<` (bash drops NUL bytes as it reads a body line);
    and more than `HEREDOC_CAP` open on one line. One shape is flagged in
    `unsure` instead (when given) and read on: a here-document opened in
    backticks that close before its body — its lines after are read as
    commands, the stricter reading."""
    if "<<" not in text:
        return text
    if "\x00" in text and "\n" in text:
        raise _TooComplex
    out: list[str] = []
    stack: list = []            # '"', "$(", "`", "(", or [kind, depth] for ${…} and arithmetic
    # (delimiter, strip tabs, quoted, slot in out, as written, depth, inside a substitution)
    pending: list[tuple] = []
    i, n = 0, len(text)
    last_newline = text.rfind("\n")
    while i < n:
        ch = text[i]
        top = stack[-1] if stack else ""
        if isinstance(top, list):                       # ${…}, $((…)), ((…)), $[…]: inert
            opener, closer = ("[", "]") if top[0] == "$[" else ("{", "}") if top[0] == "${" \
                else ("(", ")")
            if ch == opener:
                top[1] += 1
            elif ch == closer:
                top[1] -= 1
                if top[1] == 0:
                    stack.pop()
            out.append(ch)
            i += 1
            continue
        if top == '"':
            if ch == "\\":
                out.append(text[i:i + 2])
                i += 2
                continue
            if ch == '"':
                stack.pop()
            elif text.startswith("${", i):
                stack.append(["${", 1])
                out.append("${")
                i += 2
                continue
            elif text.startswith("$((", i):
                stack.append(["((", 2])
                out.append("$((")
                i += 3
                continue
            elif text.startswith("$(", i):
                stack.append("$(")
                out.append("$(")
                i += 2
                continue
            elif ch == "`":
                stack.append("`")
            out.append(ch)
            i += 1
            continue
        # a command context: the top level, `$(…)`, backticks, a group inside `$(…)`
        if ch == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if text.startswith("$'", i):
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if ch == "'":
            end = text.find("'", i + 1)
            end = n if end == -1 else end
            out.append(text[i:end + 1])
            i = end + 1
            continue
        if ch == '"':
            stack.append('"')
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t\n;|&()"):
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("${", i):
            stack.append(["${", 1])
            out.append("${")
            i += 2
            continue
        if text.startswith("$((", i) or (text.startswith("((", i)
                                         and (i == 0 or text[i - 1] in " \t\n;|&(")):
            width = 3 if ch == "$" else 2
            stack.append(["((", 2])
            out.append(text[i:i + width])
            i += width
            continue
        if text.startswith("$[", i):
            stack.append(["$[", 1])
            out.append("$[")
            i += 2
            continue
        if text.startswith("$(", i) or text.startswith("<(", i) or text.startswith(">(", i):
            # A process substitution is read as bash 5.3 reads a `$(…)`
            # (round 4, checked with `bash -n`): the same body rules hold.
            stack.append("$(")
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == "`":
            if top == "`":
                stack.pop()
                # Backtick text is parsed on its own: a here-document opened
                # inside it has no body outside it. Which lines bash reads as
                # its body then cannot be checked (`bash -n` does not parse
                # backticks): flagged, and the lines after read as commands.
                closing = [e for e in pending if e[5] > len(stack)]
                if closing and last_newline > i and unsure is not None:
                    unsure.append("a here-document in backticks that close before its body")
                for entry in closing:
                    out[entry[3]] = entry[4]
                    pending.remove(entry)
            else:
                stack.append("`")
        elif ch == "(" and top in ("$(", "("):
            stack.append("(")
        elif ch == ")" and top in ("$(", "("):
            stack.pop()
            # A here-document opened in a `$(…)` that closes before its body
            # begins: bash 5.3 reads the body from the lines after it, older
            # bash reads them as commands (round 3b, checked with `bash -n`).
            if top == "$(" and last_newline > i and any(e[5] > len(stack) for e in pending):
                raise _TooComplex
        elif text.startswith("<<<", i):             # a here-string: one operator, not two
            out.append("<<<")
            i += 3
            continue
        elif text.startswith("<<", i):
            strip = text.startswith("<<-", i)
            try:
                delimiter, quoted, j = _heredoc_word(text, i + (3 if strip else 2), top == "`")
            except _TooComplex:
                if last_newline > i:
                    raise
                out.append("<<")                    # no line after it: no body to place
                i += 2
                continue
            if delimiter or quoted:                 # `<<''` ends at the first empty line
                if len(pending) >= HEREDOC_CAP:
                    raise _TooComplex
                if "`" in stack and "\\" in text[i:j] and last_newline > i:
                    raise _TooComplex               # backtick text loses backslashes first
                pending.append((delimiter, strip, quoted, len(out), text[i:j], len(stack),
                                "$(" in stack or "`" in stack))
                out.append("")
                i = j
                continue
        elif ch == "\n" and pending:
            out.append(ch)
            i += 1
            in_tick = "`" in stack
            for delimiter, strip, quoted, slot, _written, _depth, in_sub in pending:
                lines: list[str] = []
                while i < n:
                    end = text.find("\n", i)
                    end = n if end == -1 else end
                    # Inside backticks the text ends at the closing one: the
                    # body cannot run past it.
                    tick = text.find("`", i, end) if in_tick else -1
                    line = text[i:end if tick == -1 else tick]
                    i = end + 1 if tick == -1 else tick
                    if in_tick and "\\" in line:
                        raise _TooComplex           # backtick text loses backslashes first
                    # An unquoted body: a backslash-newline is removed before
                    # the line is compared (round 4, the review's HIGH). The
                    # backslash left before a join is an even run, so each
                    # piece's own run decides whether it joins the next.
                    if not quoted and tick == -1 and end < n and _joins(line):
                        parts = [line]
                        while end < n and _joins(parts[-1]):
                            parts[-1] = parts[-1][:-1]
                            end = text.find("\n", i)
                            end = n if end == -1 else end
                            parts.append(text[i:end])
                            i = end + 1
                        line = "".join(parts)
                    whole = line                    # `<<-` compares it unstripped too
                    if strip:
                        line = line.lstrip("\t")
                    if line == delimiter or whole == delimiter:
                        break
                    if in_sub and line.startswith(delimiter) and ")" in line[len(delimiter):]:
                        raise _TooComplex           # bash 5.3 checks the stripped line
                    if tick != -1:
                        if line:
                            lines.append(line)
                        break
                    lines.append(line)
                body = "".join(l + "\n" for l in lines)
                if bodies is not None:
                    bodies.append(body)
                out[slot] = "<<<" + _here_word(body, quoted, keep)
            pending = []
            continue
        out.append(ch)
        i += 1
    for _delimiter, _strip, _quoted, slot, written, _depth, _in_sub in pending:
        out[slot] = written                         # no line after it: no body
    return "".join(out)


def _shell_words(segment: str) -> list[str]:
    """One segment's words as bash splits them, as `_Word`s. Quotes are
    removed and what they held is inactive; `$'…'` is decoded; a `$(…)` or
    backtick span stays inside its word, active. Linear."""
    words: list[str] = []
    text: list[str] = []
    active: list[bool] = []
    started = False
    i, n = 0, len(segment)

    def put(chars: str, on: bool) -> None:
        text.extend(chars)
        active.extend([on] * len(chars))

    while i < n:
        ch = segment[i]
        if ch in " \t\n":
            if started:
                words.append(_Word("".join(text), active))
                text.clear()
                active.clear()
                started = False
            i += 1
            continue
        started = True
        if ch == "\\":
            put(segment[i + 1:i + 2], False)
            i += 2
        elif segment.startswith("$'", i):
            j = i + 2
            while j < n and segment[j] != "'":
                j += 2 if segment[j] == "\\" else 1
            put(_ansi_c_decoded(segment[i:j + 1]) if j < n else segment[i + 2:], False)
            i = j + 1
        elif ch == "'":
            end = segment.find("'", i + 1)
            end = n if end == -1 else end
            put(segment[i + 1:end], False)
            i = end + 1
        elif ch == '"':
            j = i + 1
            while j < n and segment[j] != '"':
                c = segment[j]
                if c == "\\" and segment[j + 1:j + 2] in ("$", "`", '"', "\\"):
                    put(segment[j + 1], False)
                    j += 2
                elif segment.startswith("$(", j) or c == "`":
                    end = _span_end(segment, j)
                    put(segment[j:end], True)
                    j = end
                else:
                    put(c, c == "$")
                    j += 1
            i = j + 1
        elif segment.startswith("$(", i) or ch == "`":
            end = _span_end(segment, i)
            put(segment[i:end], True)
            i = end
        else:
            put(ch, True)
            i += 1
    if started:
        words.append(_Word("".join(text), active))
    return words


def _command_words(tokens: list[str]) -> list[str]:
    """A segment's words from the program on: compound-command keywords,
    `(`, a function's name, a `case` pattern and assignments cut off the
    front, and the `)`/`}` that close a group cut off the end."""
    tokens = command_start(tokens)
    if tokens and tokens[0] in ("for", "select"):
        return []                           # the words of `for x in …` are not run
    tokens = [t for t in tokens if t not in (")", "}", "))", ";;")]
    if tokens:
        last = tokens[-1]
        while last.endswith(")") and last.count(")") > last.count("("):
            last = _cut(last, 0, -1)
        while last.endswith("}") and last.count("}") > last.count("{"):
            last = _cut(last, 0, -1)
        tokens[-1] = last
        if not tokens[-1]:
            tokens.pop()
    return tokens


@dataclass(frozen=True)
class _Prog:
    word: str               # as the line spells it ("" when it comes from input)
    certain: bool           # the only word that can stand here
    args: tuple
    via_input: bool = False
    replace: str | None = None


class _Walk:
    """Every program a line runs, found as described above, within caps."""

    def __init__(self):
        self.progs: list[_Prog] = []
        self.modules = False
        self.spent = 0
        self.bodies: list[str] = []     # every here-document body set aside, for the backstop
        self.flagged: list[str] = []    # a here-document reading flagged unsure
        self.seen: set = set()          # readings already walked
        self.words = 0                  # words of readings walked (`WORK_CAP`)

    def _spend(self, depth: int) -> None:
        self.spent += 1
        if self.spent > PROGRAM_CAP or depth > PROGRAM_DEPTH:
            raise _TooComplex

    def line(self, text: str, certain: bool, depth: int) -> None:
        self._spend(depth)
        text = join_continuations(_inline_heredocs(text, bodies=self.bodies,
                                                   unsure=self.flagged))
        inners, outer = _substitutions(text)
        for inner in inners:
            self.line(inner, certain, depth + 1)
        for segment in segments(outer):
            here = [r.word for r in redirections(segment) if r.op == "<<<"]
            self.segment(_shell_words(strip_redirections(segment)), certain, depth, here=here)

    def segment(self, tokens, certain, depth, via_input=False, here=(), replace=None) -> None:
        # A reading reached again (an option the table does not know offers
        # the same tail more than once) is not walked again.
        key = (tuple((str(t), getattr(t, "active", None)) for t in tokens), certain, depth,
               via_input, tuple(here), replace)
        if key in self.seen:
            return
        self.seen.add(key)
        self._spend(depth)
        tokens = _command_words(tokens)
        if not tokens:
            if via_input:
                self.progs.append(_Prog("", certain, (), True, replace))
            return
        word, args = tokens[0], tokens[1:]
        self.progs.append(_Prog(word, certain, tuple(args), via_input, replace))
        stem = _basename(word)
        if _PYTHON.fullmatch(stem) and _module_run(args):
            self.modules = True
        if stem in _WRAPPER_OPTS or stem in _RUNNER_SPECS:
            self._started(stem, tokens, certain, depth, via_input, replace, here)
        elif stem in _SHELL_STEMS:
            strings, stdin = _shell_command(tokens)
            for text in strings + ([_unquoted(w) for w in here] if stdin else []):
                self.line(text, certain, depth + 1)
        elif stem == "eval":
            self.line(" ".join(args), certain, depth + 1)
        elif stem in ("find", "bfs"):
            i = 1
            while i < len(tokens):
                if tokens[i] in _FIND_EXEC:
                    j = i + 1
                    while j < len(tokens) and tokens[j] not in (";", "+"):
                        j += 1
                    self.segment(tokens[i + 1:j], certain, depth, via_input=True,
                                 replace="{}")
                    i = j
                i += 1

    def _started(self, stem, tokens, certain, depth, via_input, replace, here=()) -> None:
        """A wrapper or a runner: what it starts, from the one table (`starts`)."""
        started = starts(tokens)
        if started is None:
            return
        for text in started.strings:
            self.line(text, certain, depth + 1)
        if stem == "xargs":
            replace = _xargs_replace(tokens) or replace
        elif stem not in _WRAPPER_OPTS:
            replace = None                      # a runner's program is its own word
        # A wrapper's reading is sure when one place is possible at all (an
        # option that may swallow its program leaves none); a runner's, when
        # one program is.
        wrapper = stem in _WRAPPER_OPTS
        sure = certain and (len(started.programs) + (wrapper and started.open_end)) == 1
        for program in started.programs:
            # Walking a reading costs its words; past `WORK_CAP` the line is
            # too complex to judge (review of PR #75, HIGH 1). The walk's own
            # budget, not the line's: the vex reading is cached per line.
            self.words += len(program)
            if self.words > WORK_CAP:
                raise _TooComplex
            program = list(program)
            if started.module and _VEX_MODULE.fullmatch(program[0]):
                self.modules = True
            # One level deeper per wrapper or runner (review of PR #75, L1: the
            # depth was passed on unchanged, so a thousand nested `npx`
            # recursed past Python's limit and raised out of dispatch).
            if started.joined:
                self.line(" ".join(program), sure, depth + 1)
                if stem in _RUNNER_SPECS:       # pnpm's shell mode also names a program
                    self.segment(program, sure, depth + 1, via_input=via_input, here=here)
                continue
            # Standard input flows on to the program, except through xargs,
            # which reads it as arguments.
            self.segment(program, sure, depth + 1, via_input=via_input or started.appends,
                         replace=replace, here=() if started.appends else here)
        if wrapper and started.open_end and not started.joined and stem != "xargs":
            # A wrapper fed its program from input (`xargs nohup`): the
            # program is read from input, which `segment` records.
            self.segment([], sure, depth + 1, via_input=via_input, replace=replace, here=here)


def _unquoted(word: str) -> str:
    """A here-string's text, quotes removed and its lines kept: a body fed to
    a shell is a script, one command per line."""
    return " ".join(str(w) for w in _shell_words(word))


def _module_run(args) -> bool:
    """`-m jarvis.v2.vex` in any spelling a Python takes (`-m X`, `-Im X`,
    `-mX`), or `-m runpy jarvis.v2.vex`."""
    for i, token in enumerate(args):
        before = args[i - 1] if i else ""
        if _VEX_MODULE.fullmatch(token) and (
                before == "runpy" or before == "--module"
                or (before.startswith("-") and not before.startswith("--") and before.endswith("m"))):
            return True
        if token.startswith("--module=") and _VEX_MODULE.fullmatch(token.split("=", 1)[1]):
            return True
        glued = re.fullmatch(r"-[A-Za-z]*m(.+)", token)
        if glued and _VEX_MODULE.fullmatch(glued.group(1)):
            return True
    return False


def _matching(word: str, i: int) -> int:
    """Index just past the closer of the `{` or `(` at `word[i]`, or the
    word's end when it never closes."""
    opener, closer = word[i], "}" if word[i] == "{" else ")"
    depth = 0
    for j in range(i, len(word)):
        if word[j] == opener:
            depth += 1
        elif word[j] == closer:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(word)


def _first_alternative(inner: str) -> str | None:
    """The first word a brace expansion `{inner}` makes, or None when it is
    not one (no top-level comma and no `x..y` range: bash keeps it as text)."""
    depth, parts, start = 0, [], 0
    for k, ch in enumerate(inner):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:k])
            start = k + 1
    if parts:
        return parts[0]
    bounds = re.fullmatch(r"(-?\d+|[A-Za-z])\.\.(-?\d+|[A-Za-z])(\.\.-?\d+)?", inner)
    return bounds.group(1) if bounds else None


def _word_atoms(word: str, posix: bool) -> tuple[list, list[str]]:
    """A program word as glob atoms, each with its kind: "lit", "glob",
    "exp" (an expansion, read as `*`: `$X`, `${…}`, `$(…)`, backticks,
    find's `{}`) or "sep" (a literal `/`). Only an *active* character (a
    `_Word`'s mask; a plain string is all active) is a glob or an expansion:
    `'*'` is a file called `*`. A brace expansion is read as the first word
    it makes, which is the one that runs."""
    text = str(word)
    mask = list(getattr(word, "active", None) or [True] * len(text))
    atoms: list = []
    kinds: list[str] = []
    i = 0
    while i < len(text):
        ch, on = text[i], mask[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if on and ch == "$" and nxt in ("{", "("):
            atoms.append(_STAR)
            kinds.append("exp")
            i = _matching(text, i + 1)
            continue
        if on and ch == "$" and nxt and (nxt == "_" or (nxt.isascii() and nxt.isalpha())):
            j = i + 2
            while j < len(text) and (text[j] == "_" or (text[j].isascii() and text[j].isalnum())):
                j += 1
            atoms.append(_STAR)
            kinds.append("exp")
            i = j
            continue
        if on and ch == "$" and nxt and nxt in "@*#?$!-0123456789":
            atoms.append(_STAR)
            kinds.append("exp")
            i += 2
            continue
        if on and ch == "`":
            end = text.find("`", i + 1)
            atoms.append(_STAR)
            kinds.append("exp")
            i = len(text) if end == -1 else end + 1
            continue
        if on and ch == "{":
            end = _matching(text, i)
            first = _first_alternative(text[i + 1:end - 1]) if text[end - 1:end] == "}" else None
            if first is not None:
                # Brace expansion: the program is the first word it makes,
                # the rest are its arguments (`{v,x}excom` runs vexcom).
                text = text[:i] + first + text[end:]
                mask = mask[:i] + [True] * len(first) + mask[end:]
                continue
            if end == i + 2:                # `{}`: find's and xargs' placeholder
                atoms.append(_STAR)
                kinds.append("exp")
                i = end
                continue
        if on and ch == "*":
            atoms.append(_STAR)
            kinds.append("glob")
        elif on and ch == "?":
            atoms.append(_ANY)
            kinds.append("glob")
        elif on and ch == "[" and (end := _bracket_end(text, i, posix)) is not None:
            atoms.append(_ANY)
            kinds.append("glob")
            i = end
            continue
        else:
            atoms.append("/" if ch == "/" else _fold(ch))
            kinds.append("sep" if ch == "/" else "lit")
        i += 1
    return atoms, kinds


def _program_kind(prog: _Prog) -> str | None:
    """RUNS, UNKNOWN, VEXBUILD, COMPLEX or None for one program word."""
    if prog.via_input and (not prog.word or (prog.replace and prog.replace in prog.word)):
        return UNKNOWN
    word = prog.word
    if not word:
        return None
    if len(word) > PROGRAM_WORD_CAP or sum(word.count(c) for c in "*?[{$`") > GLOB_META_CAP:
        return COMPLEX
    could = vexbuild = only_expansions = False
    for posix in (True, False):
        atoms, kinds = _word_atoms(word, posix)
        cut = max((k for k, kind in enumerate(kinds) if kind == "sep"), default=-1)
        atoms, kinds = atoms[cut + 1:], kinds[cut + 1:]
        if not atoms:
            continue
        if "exp" not in kinds and "glob" not in kinds:
            name = "".join(atoms)
            if name in _VEX_TARGETS:
                return RUNS
            vexbuild = vexbuild or name == "vexbuild"
            continue
        if "exp" in kinds:
            # An expansion may hold a `/`, so the file name may begin inside
            # it: what follows the last one, after anything at all. With
            # nothing after it, the name is the expansion's alone: it
            # cannot be told (`$X`, `${A}.${B}`, `$DIR/$PROG`).
            last = max(k for k, kind in enumerate(kinds) if kind == "exp")
            only_expansions = only_expansions or last == len(kinds) - 1
            atoms = [_STAR] + atoms[last + 1:]
        could = could or any(_atoms_match(atoms, t) for t in _VEX_TARGETS)
        vexbuild = vexbuild or _atoms_match(atoms, "vexbuild")
    if could:
        return UNKNOWN if only_expansions or not prog.certain else RUNS
    return VEXBUILD if vexbuild else None


def _program_variants(command: str) -> list[str]:
    import unicodedata
    out = [command]
    nfkc = unicodedata.normalize("NFKC", command)
    for variant in (nfkc, _ansi_c_decoded(command), _ansi_c_decoded(nfkc)):
        if variant not in out:
            out.append(variant)
    return out


@dataclass(frozen=True)
class VexLine:
    """What a line does with VEX's tools: `deny` (it names or runs vexcom),
    `unsure` (it may, and cannot be told: asked whatever stands), `vexbuild`
    (it runs the agents' build door, which asks like any command)."""
    deny: str | None = None
    unsure: str | None = None
    vexbuild: bool = False


def _vex_line_uncached(command: str, backstop: bool = True) -> VexLine:
    if not command.strip():
        return VexLine()
    named = vexcom_reading(command)
    if named == NAMES:
        return VexLine(deny=VEXCOM_DENIED)
    unsure = TOO_COMPLEX if named == COMPLEX else None
    vexbuild = False
    bodies: list[str] = []
    for text in _program_variants(command):
        walk = _Walk()
        try:
            walk.line(text, True, 0)
        except _TooComplex:
            unsure = unsure or TOO_COMPLEX
        if walk.flagged:
            unsure = unsure or TOO_COMPLEX
        bodies.extend(walk.bodies)
        vexbuild = vexbuild or walk.modules
        for prog in walk.progs:
            kind = _program_kind(prog)
            if kind == RUNS:
                return VexLine(deny=(
                    f"it runs {_quoted_word(prog.word)} as a program, which could be vexcom, "
                    "VEX's tool for downloading to and talking to the robot; that is only ever "
                    "the owner's click in the HUD, never an agent's command (build with "
                    "vexbuild instead)"))
            if kind == UNKNOWN and unsure is None:
                unsure = (f"the program it runs ({_quoted_word(prog.word) or 'read from input'}) "
                          "cannot be told from here and could be vexcom, VEX's tool for the "
                          "robot, so it is asked whatever else stands")
            elif kind == COMPLEX:
                unsure = unsure or TOO_COMPLEX
            vexbuild = vexbuild or kind == VEXBUILD
    if unsure is None and backstop and bodies:
        unsure = _body_marker(bodies)
    return VexLine(unsure=unsure, vexbuild=vexbuild)


# **The body backstop** (round 4). Three review rounds each found a shape
# whose here-document body the lexer kept open past bash's own end (a
# delimiter formed differently, a terminator joined from a continuation), so
# the lines after it — commands to bash — were read as data. Whatever the
# lexer gets wrong next, this bounds the cost: every line of every body it
# set aside (any here-document, any quoting, as written and with its
# continuations joined) is read alone as a command, and a line that would be
# **DENY** that way (it runs something that could be vexcom, or names it)
# makes the whole line unsure — asked, never covered by an allowlist entry,
# always-ask in v2 — and never DENY, because it is usually a real mention in
# data (a `* bullet`, a C comment), which must stay approvable. So a
# misread body end can at worst turn a DENY into an ask, never into
# something an allowlist entry or v2's reviewer runs unasked. What the lexer
# reads as a command keeps its stricter verdict.
#
# **Only a DENY reading marks a body, not an unsure one**, by measurement:
# read alone as a command, a Markdown line with a code span (`` `x` `` is a
# program that is only an expansion) is unsure, and 45 of this repository's
# 49 pull-request bodies and 27 of its 546 commit messages hold one, where 0
# of the commit messages hold a DENY-shaped line. The cost of that choice:
# a body end misread over a line that is only *unsure* (`$X --user`) gives
# the general ASK, which v2 under AUTO leaves to the provider's reviewer.
#
# **Except where the general rules would ALLOW it** (round 4 delta review).
# Some lines are only unsure read alone yet ALLOW by every general rule: an
# ALLOW-stem runner given an option its table does not know, whose program
# could then be an expansion (`npx --frob $X x`), or an ALLOW stem with a
# glob past the caps (`ls [[[…`). Hidden by a misread body end, nothing else would
# turn such a line into an ask, so an allowlist entry or a human-backed v1
# surface would run it unasked. So an unsure line marks the body too when its
# general verdict alone (`_general_verdict`, `decide` with the vex reading
# off) is ALLOW. A Markdown code-span line holds a substitution, so its
# general verdict is ASK and it still marks nothing.
BODY_MARKER = ("a here-document's body holds a line that would run something that could be "
               "vexcom, VEX's tool for the robot, or name it, if it were read as a command; "
               "where a body ends is bash's reading, so it is asked whatever else stands")


def _body_marker(bodies: list[str]) -> str | None:
    """BODY_MARKER if a line of one of these bodies, read alone as a command,
    is DENY by the vex reading, or is unsure by it and ALLOW by the general
    rules; else None. Each distinct line is read once."""
    seen: set[str] = set()
    for body in bodies:
        for text in (body, join_continuations(body)):
            for line in text.split("\n"):
                if line in seen or not line.strip():
                    continue
                seen.add(line)
                alone = _vex_line_uncached(line, backstop=False)
                if alone.deny:
                    return BODY_MARKER
                # Unsure alone, and nothing but the vex reading would ask it:
                # the general verdict is read only for an unsure line.
                if alone.unsure and \
                        _general_verdict(join_continuations(line)).decision == ALLOW:
                    return BODY_MARKER
    return None


def _quoted_word(word: str) -> str:
    """A program word as a reason quotes it: one capped, printable line."""
    text = " ".join("".join(c for c in str(word) if c.isprintable() or c.isspace()).split())
    return f"`{text[:60]}{'…' if len(text) > 60 else ''}`" if text else ""


@functools.lru_cache(maxsize=256)
def _vex_line_cached(command: str) -> VexLine:
    return _vex_line_uncached(command)


_LAST_LONG: tuple = ("", VexLine())


def _vex_line(command: str) -> VexLine:
    """The vex reading of a line (`decide`, the allowlist and v2's layer 2
    each ask; short lines are cached, the last long one is kept, the
    reading is pure)."""
    global _LAST_LONG
    command = command or ""
    if len(command) <= 16384:
        return _vex_line_cached(command)
    last = _LAST_LONG
    if last[0] == command:
        return last[1]
    line = _vex_line_uncached(command)
    _LAST_LONG = (command, line)
    return line


def vex_uncoverable(command: str) -> str | None:
    """Why no allowlist entry, and no v2 profile, may answer for this line
    without asking: it names or runs vexcom (DENY before this is reached),
    or it may run vexcom and cannot be told, or it is too complex to judge.
    `permissions.allows` and v2's layer 2 read it."""
    line = _vex_line(command)
    return line.deny or line.unsure


# What `_judge_plain` answers for a stem on no list at all: asked, but only for
# being unknown. Compared by identity, never by value (`Verdict(ASK) ==
# _UNLISTED`): a runner's program that is merely unknown keeps the runner's
# own verdict (`_judge_started`), so `npx some-package` stays what it was.
_UNLISTED = Verdict(ASK)


def _judge_segment(segment: str, depth: int = 0) -> Verdict:
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
    words = _tokens(strip_redirections(segment)) if redirects else raw
    return _judge_words(words, redirects, raw, depth)


def _judge_words(words: list[str], redirects: list, raw: list[str] | None,
                 depth: int) -> Verdict:
    """One command's words (redirections out), judged two ways, the worse
    winning: stem by stem (`_judge_plain`, wrappers peeled by `unwrap`), and
    by what the wrappers and runners at its front start (`_judge_started`,
    the one table). `raw` is None for a program a runner starts: Jarvis's own
    CLI was already looked for through every runner (`runs_jarvis`)."""
    bare = _unwrap(list(words))
    tokens = _unwrap(list(raw)) if raw is not None and raw is not words else bare
    plain = _judge_plain(tokens, bare, redirects, raw)
    if plain.decision == DENY:
        return plain
    started = _judge_started(words, redirects, depth)
    if started is None:
        return plain
    if _ORDER[started.decision] < _ORDER[plain.decision]:
        return started
    if plain is _UNLISTED and started.decision == ASK:
        return started      # asked for a reason, not only for being unknown
    return plain


# A runner that runs a program only known when it runs (`uv run $CC`, `npx
# "$TOOL"`, `uv run ./bin/*`): what it starts cannot be told.
_UNTOLD = re.compile(r"[$`*?]")


def _judge_started(words: list[str], redirects: list, depth: int) -> Verdict | None:
    """What the wrapper or runner at the front of `words` starts, judged —
    or None when nothing there starts a program (`starts`).

    A wrapper in `_WRAPPERS` (`env`, `nohup`, `timeout` …) is judged by
    what it runs, its options read from the table (`env -u NAME`, `exec -a
    NAME`, `xargs -a FILE` take a value). A runner (`uv run`, `npx`, `npm
    exec`, `poetry run` …) keeps its own verdict — `uv` is a build tool —
    unless what it starts is refused, or asked for a reason of its own (`rm`,
    `git push`, `python -c`, a reader in a writing form, a writer or an
    interpreter): then that. A program that is only unknown (`npx
    create-vite`) asks nothing more. A command line it hands a shell (`npm
    exec -c`, `bun exec`, pnpm's shell mode) is judged as a line, by the same
    rule (`_line_verdict`). A program word only known when it runs, or more
    than one place the program may stand (an option the table does not
    know), asks rather than allows.

    Bounded by the line's work budget (`charge`), with every reading judged
    once (`_Work.memo`); past it, or past `PROGRAM_DEPTH`, the line is too
    complex to judge (`RUNNERS_TOO_COMPLEX`)."""
    lead = strip_assignments(words)
    if not lead:
        return None
    stem = _basename(lead[0])
    if stem not in _WRAPPER_OPTS and stem not in _RUNNER_SPECS:
        return None
    try:
        charge(len(lead))
        started = starts(lead)
    except _TooComplex:
        exhaust()
        return Verdict(ASK, RUNNERS_TOO_COMPLEX)
    if started is None or not (started.programs or started.strings):
        return None
    if depth >= PROGRAM_DEPTH:
        exhaust()
        return Verdict(ASK, RUNNERS_TOO_COMPLEX)
    transparent = stem in _WRAPPERS
    worst = Verdict(ALLOW)
    work = _WORK.get()
    # Everything a reading's verdict reads from outside its own words is in
    # its memo key: the redirections, targets included (delta review of PR
    # #75, H-A, where the gate detector's key missed a here-string).
    shape = tuple((r.op, r.word) for r in redirects)

    def worse(verdict: Verdict) -> None:
        nonlocal worst
        if _ORDER[verdict.decision] < _ORDER[worst.decision]:
            worst = verdict

    if started.writes:
        # `time -o FILE` writes FILE, as `tee FILE` does (delta review of PR
        # #75, H-B); the gate-state detector refuses it onto the allowlist.
        worse(Verdict(ASK, f"{stem} writes {started.writes[0][:60]}, the file its option names"))
    for text in started.strings:
        verdict = _line_verdict(text, depth + 1)
        if verdict.decision == DENY:
            return verdict
        if text in started.hidden:
            # A command line a unit property holds (`-p ExecStartPost=CMD`)
            # is asked whatever it is (third delta review of PR #75, LOW 2).
            worse(Verdict(ASK, f"{stem} runs a command line held in a unit property: "
                          f"{text[:60]}"))
        if verdict.decision == ALLOW or (verdict is _UNLISTED and not transparent):
            continue
        worse(Verdict(ASK, f"{stem} runs a command line that asks: {verdict.reason or text[:60]}"))
    for program in started.programs:
        key = (program, depth + 1, shape, started.joined)
        verdict = work.memo.get(key) if work is not None else None
        if verdict is None:
            try:
                charge(len(program))        # judging a reading costs its words
            except _TooComplex:
                exhaust()
                return Verdict(ASK, RUNNERS_TOO_COMPLEX)
            if started.joined:
                verdict = _line_verdict(" ".join(program), depth + 1)
            else:
                verdict = _judge_words(list(program), redirects, None, depth + 1)
            if work is not None:
                work.memo[key] = verdict
        if verdict.decision == DENY:
            return verdict
        inner = _basename(program[0])
        if not started.joined and _UNTOLD.search(program[0]):
            worse(Verdict(ASK, f"the program {stem} starts ({program[0][:60]}) is only known "
                          "when it runs"))
        own = strip_assignments(program)
        if not started.joined and own and not known_program(_basename(own[0])):
            # A program no table knows, handed `-c`/`-e`/`--command STRING`,
            # may be a shell: its string is judged as a command line (M-B).
            for text in command_strings(own):
                try:
                    line = _line_verdict(text, depth + 1)
                except _TooComplex:
                    exhaust()
                    return Verdict(ASK, RUNNERS_TOO_COMPLEX)
                if line.decision == DENY:
                    return line
                if line.decision != ALLOW and line is not _UNLISTED:
                    worse(Verdict(ASK, f"{inner} is handed a command line that asks: "
                                  f"{line.reason or text[:60]}"))
        if verdict.decision == ALLOW or (verdict is _UNLISTED and not transparent):
            continue
        if transparent:
            worse(verdict)
        else:
            what = f"`{' '.join(program)[:60]}`" if started.joined else inner
            worse(Verdict(ASK, f"{stem} starts {what}, which asks"
                          + (f": {verdict.reason}" if verdict.reason else "")))
    if not started.sure and worst.decision == ALLOW:
        worst = Verdict(ASK, f"an option of {stem}'s that this check does not know leaves "
                        "which program it starts unclear")
    return worst


def _line_verdict(text: str, depth: int) -> Verdict:
    """A command line a runner hands a shell (`npm exec -c`, `bun exec`,
    pnpm's shell mode, `watch`), judged as `decide` judges a line — owner
    ops, credential env writes, substitution, every segment, worst wins —
    except that a line asked only because its programs are unknown answers
    `_UNLISTED`, as a runner's unknown program does: `npm exec -c 'eslint .'`
    is the `npm` it always was, `npm exec -c 'eslint . && rm -rf dist'` asks."""
    text = join_continuations(text)
    whole = _general_verdict(text, depth)
    if whole.decision != ASK or has_substitution(text):
        return whole
    from . import protected_state
    if protected_state.kill_unsure(text):
        return whole
    for segment in segments(text):
        verdict = _judge_segment(segment, depth)
        if verdict.decision != ALLOW and verdict is not _UNLISTED:
            return verdict
        words = _unwrap(_tokens(strip_redirections(segment)))
        if verdict is _UNLISTED and words and _UNTOLD.search(words[0]):
            return Verdict(ASK, f"the program it runs ({words[0][:60]}) is only known when it runs")
    return _UNLISTED


# Programs that write what they are given, run code, or change the machine,
# kept off the auto-approve list on purpose (MED 1 of the review of PR #75):
# bare, each asks as an unknown stem; behind a runner, an unknown stem asks
# nothing more, so they need a reason of their own there — `uv run tee -a
# ~/.bashrc`, `uv run sh x.sh`, `uv run perl …`, `uv run crontab -r` and
# `uv run powershell.exe …` were the ALLOW of `uv`. protected_state's writer,
# shell and interpreter sets, plus these.
_ASKS_ON_ITS_OWN = frozenset({"tee", "awk", "gawk", "mawk", "nawk", "crontab", "at", "batch",
                              "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd",
                              "cmd.exe", "wsl", "wsl.exe", "fish", "mksh", "ash", "yash", "csh",
                              "tcsh"})


def known_program(stem: str) -> bool:
    """Whether some table here, or in `protected_state`, knows the program
    `stem`: one this check can say what it does with its arguments."""
    from . import protected_state
    return (stem in _ALLOW_STEMS or stem in _NEVER_AUTO_STEMS or stem in _INTERPRETERS
            or stem in _ASKS_ON_ITS_OWN or stem in _ESCALATION or stem in _DESTRUCTION
            or stem in READER_HAZARDS or stem in _NO_HAZARD or stem in _WRAPPER_OPTS
            or stem in _RUNNER_SPECS or stem in _REMOTE_OPTS or stem in _WRAPPERS
            or stem in protected_state._WRITERS or stem in protected_state._READERS
            or stem in protected_state._INTERPRETERS or stem in protected_state._SHELLS)


def command_strings(tokens: list[str]) -> list[str]:
    """The strings a program is handed with `-c`, `-e` or `--command` (or
    `--command=…`, or glued to `-c`/`-e` when they hold a space, which no
    cluster of flags does), `c` or `e` in a cluster of flags included (`-lc`,
    `-ic`, `-ec`, `-xc`, `-cl`) — what a shell this check does not know by name
    would run as a command line (delta review of PR #75, M-B: behind a
    runner, an unknown program is judged only as unknown, so `uv run myshell
    -c '<an owner op>'` was the ALLOW of `uv`). Read only for a program no
    table knows (`known_program`); options end at `--`. A value that is one
    plain word naming no program a table knows is a value, not a command
    line (`npx prettier -c src/`, `npx webpack -c webpack.config.js`)."""
    out: list[str] = []
    for i, token in enumerate(tokens[1:], 1):
        if token == "--":
            break
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        if token == "--command":
            out.append(following)
        elif token.startswith("--command="):
            out.append(token.split("=", 1)[1])
        elif token.startswith("-") and not token.startswith("--") and len(token) > 1:
            # A short-option cluster holding `c` or `e` (third delta review of
            # PR #75, LOW 1: `uv run mysh -lc '<an owner op>'` was the ALLOW
            # of `uv`; a shell reads `-lc`, `-ic`, `-ec`, `-xc`, `-cl` as `-c`
            # with flags). Read both ways a getopt program may: the rest of
            # the cluster as its glued value, and the next word as the value
            # (the cluster all flags).
            letters = token[1:]
            k = next((j for j, ch in enumerate(letters) if ch in "ce"), None)
            if k is None or not all(ch.isascii() and ch.isalpha() for ch in letters[:k]):
                continue
            glued = letters[k + 1:]
            if glued and any(ch.isspace() for ch in glued):
                out.append(glued)
            if all(ch.isascii() and ch.isalpha() for ch in letters) and following:
                out.append(following)
    return [text for text in out
            if _COMMAND_CHARS.search(text) or known_program(_basename(text.strip()))]


_COMMAND_CHARS = re.compile(r"[\s;|&$`<>(){}]")


def _judge_plain(tokens: list[str], bare: list[str], redirects: list,
                 raw: list[str] | None) -> Verdict:
    """The stem-by-stem reading of one command, wrappers peeled by `unwrap`."""
    stem = _stem(tokens)
    if not stem:
        return Verdict(ASK, "could not parse the command")
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

    # Jarvis's own CLI or modules, in any spelling — `python -m jarvis`, the
    # venv's python, `uv run`/`poetry run`/`pipx run` — is asked, never
    # auto-approved, as bare `jarvis` always was: the CLI has human-only verbs
    # (`auth`, `voice clone`, `discord commands --sync`, the owner ops) and
    # `python` and `uv` are on the allow list (review of PR #44 at 75f14c2).
    from . import protected_state
    own = protected_state.runs_jarvis(raw) if raw is not None else None
    if own:
        return Verdict(ASK, f"runs {own}; Jarvis's own commands are the owner's to run")

    if stem == "systemctl" and "--user" not in tokens:
        return Verdict(ASK, "system-wide systemctl affects the whole machine")
    if stem == "systemctl":
        # `--user set-environment` and its kin change what the v2 daemon
        # starts with next time: asked, never auto-approved (review of PR
        # #48). What changes the daemon's unit itself is DENY, above.
        why = protected_state.systemctl_asks(tokens)
        if why:
            return Verdict(ASK, why)

    if stem in _NEVER_AUTO_STEMS:
        return Verdict(ASK)
    if stem in _ALLOW_STEMS:
        return Verdict(ALLOW)
    if (stem in _ASKS_ON_ITS_OWN or stem in protected_state._WRITERS
            or stem in protected_state._SHELLS or stem in protected_state._INTERPRETERS):
        return Verdict(ASK, f"{stem} writes what it is given or runs code, so it is asked")
    return _UNLISTED


def decide(command: str) -> Verdict:
    """The verdict for a whole command line.

    Every segment is judged and the strictest verdict wins, so appending
    something dangerous to something benign cannot launder it.

    The line is judged as bash will read it: backslash-newline continuations
    are removed first (`join_continuations`). What its wrappers and runners
    start is judged within one work budget (`work_scope`); a line past it is
    asked, never allowed (`RUNNERS_TOO_COMPLEX`, `runners_unsure`).
    """
    with work_scope() as work:
        verdict = _decide(command)
    if work.exhausted and verdict.decision != DENY:
        return Verdict(ASK, work.exhausted)
    return verdict


def _decide(command: str) -> Verdict:
    raw = command or ""
    command = join_continuations(raw)
    if not command.strip():
        return Verdict(ASK, "empty command")

    # Naming VEX's device tool anywhere on the line, or running something
    # that could be it, is DENY (V-2): judged on the whole line, before
    # segmentation, so a `sh -c '…'` string, a substitution, a quoted path or
    # an ALLOW-stem runner cannot carry it past (`_vex_line`).
    # Read from the raw line: a quoted here-document's body is not joined at
    # a backslash-newline, so joining first could swallow its terminator.
    vex = _vex_line(raw)
    if vex.deny:
        return Verdict(DENY, vex.deny)
    # The Secret Service (the owner's keyring) likewise, on the raw line:
    # `secret_service_reading`.
    secret = secret_service_reading(raw)
    if secret:
        return Verdict(DENY, secret)
    if "\x00" in raw:
        # No shell can be handed a NUL byte (the line could not run as
        # written), and the readers that resolve paths raise on one: too
        # complex to judge — asked, never covered by an allowlist entry
        # (`runners_unsure`, or `vex_uncoverable` when the vex reading said
        # so first), always-ask in v2; never a ValueError out of the gate
        # (delta review of PR #75, LOW).
        if vex.unsure:
            return Verdict(ASK, vex.unsure)
        exhaust(NUL_TOO_COMPLEX)
        return Verdict(ASK, NUL_TOO_COMPLEX)

    worst = _general_verdict(command)
    # A line that may run vexcom and cannot be told, or is too complex to
    # judge, asks whatever else stands (and no allowlist entry covers it:
    # `vex_uncoverable`); `vexbuild` asks like any command unless allowlisted.
    if worst.decision != DENY:
        if vex.unsure:
            worst = Verdict(ASK, vex.unsure)
        elif vex.vexbuild:
            worst = Verdict(ASK, VEXBUILD_ASKS)
    return worst


@functools.lru_cache(maxsize=256)
def _runners_unsure_cached(command: str) -> str | None:
    with work_scope(fresh=True) as work:
        _decide(command)
    return work.exhausted


def runners_unsure(command: str) -> str | None:
    """Why no allowlist entry, and no v2 profile, may answer for this line
    without asking: what its wrappers and runners start is too complex to
    judge within the line's work budget, or past a nesting cap.
    `permissions.allows` and v2's layer 2 read it; `decide` asks it too."""
    command = command or ""
    if len(command) > 16384:
        with work_scope(fresh=True) as work:
            _decide(command)
        return work.exhausted
    return _runners_unsure_cached(command)


def _general_verdict(command: str, depth: int = 0) -> Verdict:
    """`decide`'s reading with the vex reading off: the owner ops, command
    substitution and every segment, worst wins. `command` is already joined
    (`join_continuations`). The body backstop reads it too (`_body_marker`)."""
    with work_scope():
        return _general_reading(command, depth)


def _general_reading(command: str, depth: int) -> Verdict:
    # The installed HUD's one writer, `jarvis hud install`, is the owner's and
    # the lead's — never Jarvis's, asked or not (decision C-1; review of PR
    # #44, MED-2: `python -m jarvis hud install` scored ALLOW here, and an
    # agent that can edit `hud/src` could then install it). One detector,
    # `protected_state.owner_op` (its table `OWNER_OPS` is where another
    # owner-only command goes), so v1's gate and v2's layer 1 — both of which
    # start from `decide` — refuse every spelling it sees.
    from . import protected_state
    why, unsure = protected_state.owner_reading(command)
    if why:
        return Verdict(DENY, why)
    # A write onto a credential env file, judged by name (ENV-1, the owner's
    # `.env*` rule: Jarvis writes only `*.example`): DENY, so v1's gate and
    # v2's layer 1 — both of which start here — refuse it unasked. One
    # parse of where a line writes, `protected_state`'s, not a copy.
    env = protected_state.env_write_refusal(command)
    if env:
        return Verdict(DENY, env)

    # Command substitution runs a command this function never sees. There is no
    # honest verdict except "a human should look".
    if has_substitution(command):
        inner = Verdict(ASK, "contains command substitution, which cannot be judged here")
    else:
        inner = Verdict(ALLOW)
    # A kill whose pattern is too costly to judge against the v2 daemon is
    # asked, never refused and never covered by an allowlist entry
    # (`protected_state.kill_unsure`; the delta review of PR #48).
    if unsure:
        inner = Verdict(ASK, unsure)

    worst = inner
    for segment in segments(command):
        verdict = _judge_segment(segment, depth)
        if _ORDER[verdict.decision] < _ORDER[worst.decision]:
            worst = verdict
        if worst.decision == DENY:
            break
    return worst
