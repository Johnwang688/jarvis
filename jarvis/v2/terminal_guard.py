"""What `terminal_read` may hand the model (WP-F; decisions W-2). Pure.

A read is the last `lines` lines of a terminal's normal buffer
(`terminal_text`). Before any of it is returned, three rules can refuse the
**whole** read, and the refusal is always the same sentence — `REFUSAL` — so
it never says what matched, where, or which rule:

1. **Exact known values.** The text holds a value from
   `secrets.secret_values()` (every `.env` value and token bundle Jarvis can
   reach, plus the terminal folder's own `.env` files), or its base64 (any
   alignment, standard or URL-safe alphabet) or URL-encoding.
2. **Known credential formats** (`jarvis.credential_patterns`).
3. **Secret-printing commands** (`SECRET_PRINTERS`, below). Two halves,
   and the second **always** runs:
   * the spans: a real (signed) shell-integration span overlapping the read
     whose command line matches — or one that matches and backgrounds its
     work, whose output may land anywhere after it;
   * the text: any line drawn in the read, or in the stretch before it,
     that shows such a command line (a prompt and the command, or one of the
     unmistakable ones anywhere). **Spans are advisory**: a program can print
     anything between real marks, a nested shell (`ssh`, `sudo -i`,
     `python`) sits under the outer command's span, and a shell without the
     startup file has no spans at all. The text is judged on `ink`, every
     state a line was in, so a command line cleared off the screen still
     counts. The stretch before the read is `LOOKBACK` bytes — room for the
     tail of a secret printer's output to reach into the read with its
     command line just above it — cut short only at a boundary the signed
     marks prove (a command that started, or ended, before the read began),
     and never at a span record below `spans_from`, where the backend has
     forgotten which command printed what.

Then the **heuristic** withholds single lines, never the read: a keyword
(`KEYWORDS`) with a long, high-entropy token after it. A bare 40-character
hex hash or a UUID stays unless a keyword is beside it.

**The limit, stated plainly** (W-2): a secret with no recognisable shape and
no keyword beside it — a random password printed on its own — is not caught;
nor is one printed in pieces, reversed, or encoded some other way. The
owner's "Jarvis can read" switch and the HUD note on every read are the
backstops.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import functools
import math
from pathlib import PurePosixPath
import re
import shlex
from urllib.parse import quote, quote_plus

from jarvis import credential_patterns, rules, untrusted
from jarvis.tools import secrets as v1_secrets

from . import terminal_text

REFUSAL = "possible credential in this output"
LOOKBACK = 32 * 1024
DEFAULT_LINES = 200
MAX_LINES = 1000
LINE_CAP = 2000            # characters of one line handed back
MAX_CHARS = 48_000         # characters of all of them
WITHHELD_NOTE = "[{n} line(s) withheld: possible secret]"
ENTROPY_FLOOR = 3.0        # bits per character, for the heuristic's token
_DEPTH = 4


# -- rule 1: exact values -------------------------------------------------------------

def _b64_cores(value: bytes) -> list[str]:
    """The base64 of `value` as it appears inside a longer base64 string, at
    each of the three alignments: the characters that depend only on `value`'s
    own bytes. Standard and URL-safe alphabets."""
    out = []
    for align in range(3):
        encoded = base64.b64encode(b"\0" * align + value)
        lo = (8 * align + 5) // 6
        hi = (8 * (align + len(value))) // 6
        core = encoded[lo:hi]
        if len(core) >= 10:
            text = core.decode()
            out.append(text)
            out.append(text.replace("+", "-").replace("/", "_"))
    return out


def value_forms(values) -> list[str]:
    """Every spelling rule 1 looks for: each value, its URL-encodings and its
    base64 cores. Never logged, never returned."""
    forms: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) < v1_secrets.MIN_SECRET_LEN:
            continue
        forms.add(value)
        for encoded in (quote(value, safe=""), quote_plus(value)):
            if encoded != value:
                forms.add(encoded)
        forms.update(_b64_cores(value.encode("utf-8", "replace")))
    return sorted(forms, key=len, reverse=True)


def holds_value(text: str, forms) -> bool:
    return any(form in text for form in forms)


# -- rule 3: secret-printing commands -------------------------------------------------

# The list, in one place. Each entry is what the decisions name, plus a few
# equally plain ways to print a credential (marked "+"), in the safe direction.
SECRET_PRINTERS = {
    "environment": "env (with no command to run), printenv, set (bare), export (bare or -p), "
                   "+declare/typeset (bare, or -p), +reading /proc/*/environ",
    "credential files": "cat, less, head, tail, bat (+ the other readers in READERS) of a "
                        "secrets.is_protected path (+ the CREDENTIAL_FILE paths: SSH private keys, "
                        "~/.aws/credentials, .netrc, .git-credentials, gh's hosts.yml …)",
    "tokens": "gh auth token, gh auth status -t, anything with --show-token, aws configure "
              "get / export-credentials, vercel env pull, git credential fill (+ SUBCOMMANDS)",
    "variables": "+echo/printf of a $VARIABLE whose name holds a heuristic keyword",
}

READERS = frozenset({
    "cat", "tac", "nl", "less", "more", "most", "pg", "head", "tail", "bat", "batcat", "view",
    "xxd", "od", "hexdump", "strings", "base64", "base32", "grep", "egrep", "fgrep", "rg", "ag",
    "ack", "ugrep", "sed", "awk", "gawk", "mawk", "cut", "sort", "uniq", "column", "jq", "yq",
    "diff", "colordiff", "paste", "fold", "fmt", "pr", "expand", "iconv", "dd", "tr", "rev",
})

CREDENTIAL_FILE = re.compile(
    r"(?:^|/)(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|\.netrc|_netrc|\.git-credentials|\.pgpass"
    r"|\.npmrc|\.pypirc|\.my\.cnf|\.credentials\.json)$"
    r"|(?:^|/)\.aws/credentials$|(?:^|/)gh/hosts\.ya?ml$|(?:^|/)\.docker/config\.json$"
    r"|(?:^|/)\.kube/config$|(?:^|/)\.codex/auth\.json$|(?:^|/)proc/[^/]+/environ$"
)

# (programs, words that must appear in this order among the arguments).
SUBCOMMANDS: tuple[tuple[frozenset, tuple[str, ...]], ...] = tuple(
    (frozenset(progs), words) for progs, words in (
        (("gh",), ("auth", "token")),
        (("gh",), ("auth", "status", "-t")),
        (("aws",), ("configure", "get")),
        (("aws",), ("configure", "export-credentials")),
        (("aws",), ("sts", "get-session-token")),
        (("aws",), ("sts", "assume-role")),
        (("aws",), ("sts", "get-federation-token")),
        (("aws",), ("get-login-password",)),
        (("aws",), ("get-authorization-token",)),
        (("vercel", "vc"), ("env", "pull")),
        (("git",), ("credential",)),
        (("git",), ("credential-store",)),
        (("git",), ("credential-cache",)),
        (("gcloud",), ("print-access-token",)),
        (("gcloud",), ("print-identity-token",)),
        (("az",), ("get-access-token",)),
        (("kubectl", "oc"), ("config", "view", "--raw")),
        (("kubectl", "oc"), ("get", "secret")),
        (("kubectl", "oc"), ("get", "secrets")),
        (("heroku",), ("auth:token",)),
        (("op",), ("read",)),
        (("op",), ("item", "get")),
        (("secret-tool",), ("lookup",)),
        (("vault",), ("read",)),
        (("vault",), ("kv", "get")),
        (("vault",), ("print", "token")),
        (("doppler",), ("secrets",)),
        (("npm", "pnpm", "yarn"), ("token", "create")),
        (("security",), ("find-generic-password",)),
        (("security",), ("find-internet-password",)),
    )
)

# Unmistakable anywhere in a line — in a command line or in output that
# shows one. Word-bounded.
_STRONG = re.compile(
    r"(?<![\w-])printenv(?![\w-])|--show-(?:token|secrets?)\b|(?<![\w-])gh\s+auth\s+token\b"
    r"|(?<![\w-])git\s+credential(?:-store|-cache)?\s+(?:fill|get)\b|\bexport-credentials\b"
    r"|(?<![\w-])vercel\s+env\s+pull\b|(?<![\w-])aws\s+configure\s+get\b"
    r"|/proc/[^\s/]+/environ\b"
)
_KEYWORD_VAR = re.compile(
    r"\$\{?[A-Za-z_]*(?:key|token|secret|pass|pwd|auth|bearer|cred|private)[A-Za-z0-9_]*",
    re.IGNORECASE)

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "yash", "posh",
                    "busybox", "rbash"})
# Programs that run a command they are handed after their own arguments: any
# tail of their arguments may be that command. (Not `find`, `xargs`, `fd`,
# `parallel`: they append file names to the command, so `find . -name env`
# would read as a bare `env` — and what they run on is not visible anyway.)
RUNNERS = frozenset({
    "ssh", "mosh", "docker", "podman", "nerdctl", "kubectl", "oc", "lxc", "incus", "vagrant", "su",
    "runuser", "script", "flock", "systemd-run", "nsenter", "chroot", "unshare", "firejail",
    "bwrap", "distrobox", "toolbox", "wsl", "wsl.exe", "sg", "watch", "strace", "ltrace", "npx",
    "uv", "uvx", "poetry", "pipenv", "nix", "nix-shell", "devbox", "direnv", "dotenv", "op",
    "doppler", "aws-vault", "chamber",
})

# Wrappers that run the rest of the line: their options that take a value.
_WRAPPERS: dict[str, frozenset] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U", "-R"}),
    "doas": frozenset({"-u", "-C"}),
    "nohup": frozenset(), "setsid": frozenset(), "command": frozenset(), "builtin": frozenset(),
    "exec": frozenset({"-a"}), "time": frozenset({"-f", "-o"}), "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u"}), "stdbuf": frozenset({"-i", "-o", "-e"}),
    "chrt": frozenset(), "taskset": frozenset(), "caffeinate": frozenset(),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
}
_ENV_VALUED = frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0", "-S", "--split-string"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")

# A word any rule 3 shape needs; a line with none of them is skipped unparsed.
_TRIGGER = re.compile(
    r"(?<![\w-])(?:env|printenv|set|export|declare|typeset|echo|printf|eval|" +
    "|".join(sorted({re.escape(p) for progs, _ in SUBCOMMANDS for p in progs}
                    | {re.escape(r) for r in READERS} | {re.escape(s) for s in SHELLS}
                    | {re.escape(r) for r in RUNNERS} | {re.escape(w) for w in _WRAPPERS})) +
    r")(?![\w-])|--show-|environ", re.IGNORECASE)


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False)
    except ValueError:
        return segment.split()


def _base(token: str) -> str:
    # Case is kept: a shell runs `env`, never `ENV` (a Dockerfile line).
    return PurePosixPath(token).name if token else ""


def _strip_grouping(tokens: list[str]) -> list[str]:
    """`( cmd )`, `{ cmd; }`, `! cmd`: the command inside."""
    out = [t for t in tokens if t not in ("(", ")", "{", "}", "!", "((", "))")]
    if out and out[0].startswith("("):
        out[0] = out[0].lstrip("(")
    if out and out[-1].endswith(")"):
        out[-1] = out[-1].rstrip(")")
    return [t for t in out if t]


def _skip_options(args: list[str], valued: frozenset) -> list[str]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1:]
        if not arg.startswith("-") or arg == "-":
            return args[i:]
        name = arg.split("=", 1)[0]
        i += 2 if (name in valued and "=" not in arg) else 1
    return []


def _unwrap(tokens: list[str], depth: int) -> tuple[list[str], bool]:
    """(the command that runs, prints) — `prints` is True when a wrapper on
    the way is itself a secret printer (bare `env`, `env -S` running one)."""
    for _ in range(8):
        while tokens and _ASSIGNMENT.fullmatch(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return [], False
        prog = _base(tokens[0])
        if prog == "env":
            args = tokens[1:]
            rest, i = [], 0
            while i < len(args):
                arg = args[i]
                if arg == "--":
                    rest = args[i + 1:]
                    break
                if arg.startswith("-") and arg != "-":
                    name = arg.split("=", 1)[0]
                    if name in ("-S", "--split-string") or arg.startswith("-S"):
                        value = (args[i + 1] if i + 1 < len(args) else "") if (
                            name in ("-S", "--split-string") and "=" not in arg) else (
                            arg.split("=", 1)[1] if "=" in arg else arg[2:])
                        return [], prints_secrets(value, depth + 1) or not value.strip()
                    i += 2 if (name in _ENV_VALUED and "=" not in arg) else 1
                    continue
                if _ASSIGNMENT.fullmatch(arg):
                    i += 1
                    continue
                rest = args[i:]
                break
            if not rest:
                return ["env"], True                       # env with no command: prints it
            tokens = rest
            continue
        valued = _WRAPPERS.get(prog)
        if valued is None:
            return tokens, False
        rest = _skip_options(tokens[1:], valued)
        if prog == "timeout" and rest and _DURATION.fullmatch(rest[0]):
            rest = rest[1:]
        if prog == "nice" and tokens[1:2] and re.fullmatch(r"-\d+", tokens[1]):
            rest = _skip_options(tokens[2:], valued)
        if not rest:
            return [], False                                # `sudo -i`, `sudo -k`: nothing printed here
        tokens = rest
    return tokens, False


def _subsequence(words: tuple[str, ...], args: list[str]) -> bool:
    it = iter(a.lower() for a in args)
    return all(any(a == w or a.startswith(w + "=") for a in it) for w in words)


def _names_credential(arg: str) -> bool:
    arg = arg.strip("\"'")
    return v1_secrets.is_protected(arg) or bool(CREDENTIAL_FILE.search(arg))


def _simple(tokens: list[str], depth: int) -> bool:
    """One simple command, already unwrapped: does it print a secret?"""
    prog, args = _base(tokens[0]), tokens[1:]
    if any(a.startswith(("--show-token", "--show-secret")) for a in args):
        return True
    if prog == "printenv":
        return True
    if prog == "set":
        return not args or args == ["--"]
    if prog == "export":
        return all(a == "-p" for a in args)
    if prog in ("declare", "typeset"):
        options = [a for a in args if a.startswith("-")]
        return not [a for a in args if not a.startswith("-")] or any("p" in o for o in options)
    if prog in READERS:
        operands = [a for a in args if not a.startswith("-") or "/" in a]
        if any(_names_credential(a) for a in operands):
            return True
        return v1_secrets.protected_in_command(" ".join(shlex.quote(a) for a in args)) is not None
    if prog in ("echo", "printf", "print"):
        return any(_KEYWORD_VAR.search(a) for a in args)
    if prog == "pass":
        return bool(args) and args[0] not in ("ls", "list", "find", "search", "init", "git", "insert",
                                              "add", "edit", "generate", "rm", "remove", "mv", "cp",
                                              "help", "version", "--help", "--version")
    for progs, words in SUBCOMMANDS:
        if prog in progs and _subsequence(words, args):
            return True
    if prog in SHELLS:
        for k, arg in enumerate(args):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                if k + 1 < len(args) and prints_secrets(args[k + 1], depth + 1):
                    return True
                break
    if prog == "eval":
        return prints_secrets(" ".join(args), depth + 1)
    if prog in RUNNERS:
        for k in range(1, len(tokens)):
            tail = tokens[k]
            if any(c in tail for c in " ;|&") and prints_secrets(tail, depth + 1):
                return True
            inner, printed = _unwrap(tokens[k:], depth + 1)
            if printed or (inner and _base(inner[0]) not in RUNNERS and _simple(inner, depth + 1)):
                return True
    return False


def _segment_prints(segment: str, depth: int) -> bool:
    tokens = _strip_grouping(_tokens(segment))
    if not tokens:
        return False
    inner, printed = _unwrap(tokens, depth)
    if printed:
        return True
    return bool(inner) and _simple(inner, depth)


@functools.lru_cache(maxsize=4096)
def prints_secrets(command: str, depth: int = 0) -> bool:
    """True when a command line (as typed) would print a secret, by shape.

    Robust to leading whitespace, leading `VAR=value` assignments, paths
    (`/usr/bin/env`), wrappers (`sudo -k`, `env -i`, `nohup`, `timeout 5`
    …), pipelines and lists (every segment is judged), subshells and
    `sh -c '…'` strings. Past `_DEPTH` levels of nesting the answer is yes:
    a guard that cannot see is not a reason to read."""
    if depth > _DEPTH:
        return True
    command = rules.join_continuations(str(command or ""))
    if not command.strip():
        return False
    if _STRONG.search(command):
        return True
    if not _TRIGGER.search(command):
        return False
    return any(_segment_prints(segment, depth) for segment in rules.segments(command))


def backgrounds(command: str) -> bool:
    """True when the line puts work in the background (a lone `&`), or hands
    it to something that outlives the command (`nohup`, `setsid`, `disown`)."""
    command = rules.join_continuations(str(command or ""))
    for i in rules.unquoted_indices(command):
        if command[i] == "&":
            before, after = command[i - 1:i], command[i + 1:i + 2]
            if before not in ("&", ">", "<") and after not in ("&", ">"):
                return True
    return bool(re.search(r"(?<![\w-])(?:nohup|setsid|disown)(?![\w-])", command))


# The end of a prompt: a glyph shells and themes end their prompts with, then
# whitespace. What follows may be a command line.
_PROMPT_END = re.compile(r"[$#%>❯»➜λ✗➤▶→]\s+")
_BARE_WEAK = frozenset({"env", "set", "export", "declare", "typeset"})


def shows_printer(line: str) -> bool:
    """True when a line of terminal text shows a secret-printing command line:
    one of the unmistakable ones anywhere, or a command after a prompt (or as
    the whole line, unless it is a single common word that `ls -1` could
    print)."""
    line = untrusted.strip_invisible(line)
    if _STRONG.search(line):
        return True
    if not _TRIGGER.search(line):
        return False
    whole = line.strip()
    if whole and whole.lower() not in _BARE_WEAK and prints_secrets(whole):
        return True
    for match in _PROMPT_END.finditer(line):
        rest = line[match.end():].strip()
        if rest and _TRIGGER.search(rest) and prints_secrets(rest):
            return True
    return False


# -- the heuristic -------------------------------------------------------------------

KEYWORDS = ("key", "token", "secret", "password", "passwd", "pwd", "auth", "bearer",
            "credential", "api_key", "private")
_KEYWORD = re.compile("|".join(KEYWORDS), re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z0-9_.\-]*")
# Base64 never starts with its padding, so `KEY=value` finds `value`.
_TOKEN = re.compile(r"[A-Za-z0-9+/_\-.~][A-Za-z0-9+/=_\-.~]{19,}")
_NEAR = 40                 # characters between the keyword's word and the token
_PATH = re.compile(r"(?:~|\.{1,2})?/[\w.\-/]*")
_DOTTED = re.compile(r"[A-Za-z_]+(?:\.[A-Za-z_]+)+")
_WORDS = re.compile(r"[a-z]+(?:[_\-][a-z]+)+")


def entropy(token: str) -> float:
    if not token:
        return 0.0
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(token)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def secretish(token: str) -> bool:
    """A long base64/hex-ish token that could be a credential: high enough
    entropy, and not a path, a dotted name or words joined by `_`/`-`."""
    if len(token) < 20 or entropy(token) < ENTROPY_FLOOR:
        return False
    if _PATH.fullmatch(token) or _DOTTED.fullmatch(token) or _WORDS.fullmatch(token):
        return False
    return True


def withhold(line: str) -> bool:
    """The heuristic: a keyword, and after it — past `=`, `:`, whitespace or a
    quote, within a few characters — a long, high-entropy token."""
    for match in _KEYWORD.finditer(line):
        word_end = _WORD.match(line, match.end()).end()
        for token in _TOKEN.finditer(line, word_end):
            if token.start() - word_end > _NEAR:
                break
            if token.start() == word_end:
                continue                                     # glued to the keyword's word
            if secretish(token.group()):
                return True
    return False


# -- a read ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    refused: bool
    text: str = ""          # the kept lines, joined; "" when refused
    covered: int = 0        # lines the read's window covered
    withheld: int = 0
    cut: int = 0            # oldest lines left out to fit MAX_CHARS
    shortened: int = 0      # lines cut to LINE_CAP


def _span_overlaps(span, lo: int, hi: int) -> bool:
    return span.start < hi and (span.end is None or span.end > lo)


def scan_start(window_start: int, spans, spans_from: int, integrated: bool) -> int:
    """Where the text fallback starts: `LOOKBACK` bytes before the read,
    raised to the latest boundary the signed marks prove — a command's
    prompt, its output's start, or its end — at or before the read's start.
    Never from a span record below `spans_from`: there the backend has
    forgotten which command printed what."""
    lo = window_start - LOOKBACK
    if not integrated or window_start < spans_from:
        return lo
    proven = [offset for span in spans
              for offset in (span.prompt, span.start, span.end)
              if offset is not None and spans_from <= offset <= window_start]
    return max([lo, *proven])


def rule3(window_start: int, end: int, ink, spans, spans_from: int, integrated: bool) -> bool:
    for span in spans:
        if prints_secrets(span.command) and (
                _span_overlaps(span, window_start, end)
                or (backgrounds(span.command) and span.start < end)):
            return True
    lo = scan_start(window_start, spans, spans_from, integrated)
    return any(line.last >= lo and shows_printer(line.text) for line in ink)


def judge(data: bytes, start: int, *, lines: int = DEFAULT_LINES, alt: bool = False, rows: int = 24,
          spans=(), spans_from: int = 0, integrated: bool = False, values=()) -> Verdict:
    """One read: the last `lines` lines of `data` (the ring from offset
    `start`), refused whole or handed back with lines withheld."""
    lines = max(1, min(MAX_LINES, int(lines)))
    rendered = terminal_text.render(data, start, alt=alt, rows=rows)
    window = rendered.lines[-lines:]
    window_start = min((line.first for line in window), default=rendered.end)
    text = untrusted.strip_invisible("\n".join(line.text for line in window))
    if holds_value(text, value_forms(values)) or credential_patterns.holds_credential(text):
        return Verdict(True, covered=len(window))
    if rule3(window_start, rendered.end, rendered.ink, spans, spans_from, integrated):
        return Verdict(True, covered=len(window))
    kept, withheld, shortened = [], 0, 0
    for line in text.split("\n") if window else []:
        if withhold(line):
            withheld += 1
            continue
        if len(line) > LINE_CAP:
            line = line[:LINE_CAP - 1] + "…"
            shortened += 1
        kept.append(line)
    cut, size = 0, sum(len(line) + 1 for line in kept)
    while kept and size > MAX_CHARS:
        size -= len(kept.pop(0)) + 1
        cut += 1
    return Verdict(False, "\n".join(kept), len(window), withheld, cut, shortened)
