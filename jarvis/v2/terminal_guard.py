"""What `terminal_read` may hand the model (WP-F; decisions W-2). Pure.

A read is the last `lines` lines of a terminal's normal buffer
(`terminal_text`). Before any of it is returned, three rules can refuse the
**whole** read, and the refusal is always the same sentence — `REFUSAL` — so
it never says what matched, where, or which rule:

1. **Exact known values.** The text holds a value from
   `secrets.secret_values()` (every `.env` value and token bundle Jarvis can
   reach), or one from the terminal folder's own `.env`, `.env.local` and
   `.env.*` files and its parents' (not `.env.example`, `.env.sample`,
   `.env.template`), or the base64 of one (any alignment, standard or URL-safe
   alphabet) or its URL-encoding.
2. **Known credential formats** (`jarvis.credential_patterns`) — and a
   private key's block: a read that starts inside a `-----BEGIN … PRIVATE
   KEY-----` block (opened in the stretch before it and not closed there) is
   refused, so "the last 50 lines" of a 52-line key never comes back without
   its header.
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
     startup file has no spans at all. The text is judged on the screen and
     on `ink`, the states lines were in when they were left or erased, so a
     command line cleared off the screen still counts. The stretch before the
     read is `LOOKBACK` bytes — room for the tail of a secret printer's output
     to reach into the read with its command line just above it — cut short
     only at a boundary the signed marks prove (a command that started, or
     ended, before the read began), and never at a span record below
     `spans_from`, where the backend has forgotten which command printed what.

Then the **heuristic** withholds single lines, never the read: a keyword
(`KEYWORDS`) with a long, high-entropy token after it, or a URL carrying a
password. A bare 40-character hex hash or a UUID stays unless a keyword is
beside it.

**Everything here is linear in what it reads** (2026-10-10 review): a read
renders at most a ring (1 MiB) at the terminal's width, checks at most
`LINE_SCAN` characters of each returned line, tries at most
`CANDIDATES_PER_LINE` command candidates of at most `CANDIDATE_CAP` characters
on a line, and caches by total size, never by count.

**The limit, stated plainly** (W-2): a secret with no recognisable shape and
no keyword beside it — a random password printed on its own — is not caught;
nor is one printed in pieces, reversed, or encoded some other way. Rule 3
judges command lines by shape, so it cannot see through an alias, a shell
function, a script, a symlink or a program that prints a secret on its own
initiative: their output meets rules 1 and 2 and the heuristic only. The
owner's "Jarvis can read" switch and the HUD note on every read are the
backstops.
"""
from __future__ import annotations

import base64
from bisect import bisect_right
from dataclasses import dataclass
import math
from pathlib import PurePosixPath
import re
import shlex
import threading
from urllib.parse import quote, quote_plus

from jarvis import credential_patterns, rules, untrusted
from jarvis.tools import secrets as v1_secrets

from . import terminal_text

REFUSAL = "possible credential in this output"
LOOKBACK = 32 * 1024
DEFAULT_LINES = 200
MAX_LINES = 1000
LINE_CAP = 2000            # characters of one line handed back
LINE_SCAN = LINE_CAP + 256 # characters of one returned line the rules read (a superset)
MAX_CHARS = 48_000         # characters of all of them
SCAN_LINE_CAP = 16_384     # characters of one look-back or ink line rule 3 reads
CANDIDATE_CAP = 512        # characters of one command-line candidate
CANDIDATES_PER_LINE = 16   # prompt-glyph candidates tried on one line
RUNNER_TAILS = 24          # argument positions a runner's command may start at
CACHE_BYTES = 4 << 20      # the prints_secrets cache, by total key size
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


# -- what counts as a credential file here -----------------------------------------------

# `.env.*` is protected for rule 3 here (and its values count for rule 1),
# beyond v1's `secrets.is_protected`: `.env.production` is exactly the output
# the owner asked to be denied. Templates stay readable.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_ENV_VARIANT = re.compile(r"(?<![\w.\-])\.env\.(?!(?:example|sample|template)(?![\w.\-]))[\w\-][\w.\-]*")

CREDENTIAL_FILE = re.compile(
    r"(?:^|/)(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|\.netrc|_netrc|\.git-credentials|\.pgpass"
    r"|\.npmrc|\.pypirc|\.my\.cnf|\.credentials\.json)$"
    r"|(?:^|/)\.aws/credentials$|(?:^|/)gh/hosts\.ya?ml$|(?:^|/)\.docker/config\.json$"
    r"|(?:^|/)\.kube/config$|(?:^|/)\.codex/auth\.json$|(?:^|/)proc/[^/]+/environ$"
)
_HINTS = (".env", "token.json", "keys.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "netrc",
          "credentials", "environ", "hosts.y", ".npmrc", ".pypirc", "auth.json", ".pgpass",
          ".my.cnf", "config.json", ".kube")


def protected_name(name: str) -> bool:
    """A path whose contents are a credential: v1's protected names, `.env.*`
    but the templates, and the `CREDENTIAL_FILE` paths."""
    name = name.strip("\"'")
    base = PurePosixPath(name).name
    return (v1_secrets.is_protected(base) or (base.startswith(".env.") and base not in ENV_TEMPLATES)
            or bool(CREDENTIAL_FILE.search(name)))


def _names_credential(arg: str) -> bool:
    """An argument that names a credential file: the word itself, or the path
    after a `REV:` (`git show HEAD:.env`) or an `--opt=`."""
    arg = arg.strip("\"'")
    return any(protected_name(part) for part in {arg, arg.rsplit(":", 1)[-1], arg.split("=", 1)[-1]}
               if part)


_NAME_EDGE = r"A-Za-z0-9_.\-"
_RAW_PROTECTED = re.compile(
    rf"(?<![{_NAME_EDGE}])(?:" + "|".join(sorted(map(re.escape, v1_secrets.PROTECTED_NAMES)))
    + rf")(?![{_NAME_EDGE}])"
    r"|(?<![\w.\-])(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|\.netrc|_netrc|\.git-credentials|\.pgpass"
    r"|\.npmrc|\.pypirc|\.my\.cnf|\.credentials\.json)(?![\w.\-])"
    r"|(?:\.aws/credentials|gh/hosts\.ya?ml|\.docker/config\.json|\.kube/config|\.codex/auth\.json"
    r"|/proc/[^\s/]+/environ)(?![\w.\-])")


def protected_in(text: str) -> bool:
    """Does a command line name a credential file anywhere (a glob onto one
    included)? Regular expressions first; v1's tokenising check only for a
    line with a glob in it."""
    if not any(hint in text for hint in _HINTS):
        return False
    if _RAW_PROTECTED.search(text) or _ENV_VARIANT.search(text):
        return True
    return any(c in text for c in "*?[") and v1_secrets.protected_in_command(text) is not None


# -- rule 3: secret-printing commands -------------------------------------------------

# The list, in one place. Each entry is what the decisions name, plus a few
# equally plain ways to print a credential (marked "+"), in the safe direction.
SECRET_PRINTERS = {
    "environment": "env (with no command to run), printenv, set (bare), export (bare or -p), "
                   "+declare/typeset (bare, or -p), +reading /proc/*/environ, +ps e…, "
                   "+systemctl show-environment, +docker/podman inspect",
    "credential files": "cat, less, head, tail, bat (+ the other readers in READERS, + interpreters, "
                        "+ git show/diff/blame/cat-file/log/grep) of a protected path — "
                        "secrets.is_protected, + .env.* but the templates, + the CREDENTIAL_FILE "
                        "paths — named as an operand, after `REV:`, or as an input redirection; "
                        "+ a reader of `$var`/`{}`/a substitution, find -exec or xargs reader, on "
                        "a line that names one",
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
INTERPRETERS = frozenset({"python", "python3", "node", "ruby", "perl", "php", "deno", "bun",
                          "lua", "Rscript", "tclsh", "pwsh"})
GIT_READS = frozenset({"show", "cat-file", "blame", "diff", "log", "grep", "archive", "annotate"})

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
        (("aws",), ("secretsmanager", "get-secret-value")),
        (("aws",), ("ssm", "get-parameter")),
        (("aws",), ("ssm", "get-parameters")),
        (("vercel", "vc"), ("env", "pull")),
        (("git",), ("credential",)),
        (("git",), ("credential-store",)),
        (("git",), ("credential-cache",)),
        (("gcloud",), ("print-access-token",)),
        (("gcloud",), ("print-identity-token",)),
        (("gcloud",), ("secrets", "versions", "access")),
        (("az",), ("get-access-token",)),
        (("az",), ("keyvault", "secret", "show")),
        (("kubectl", "oc"), ("config", "view", "--raw")),
        (("kubectl", "oc"), ("get", "secret")),
        (("kubectl", "oc"), ("get", "secrets")),
        (("heroku",), ("auth:token",)),
        (("heroku",), ("config",)),
        (("op",), ("read",)),
        (("op",), ("item", "get")),
        (("bw",), ("get",)),
        (("lpass",), ("show",)),
        (("secret-tool",), ("lookup",)),
        (("vault",), ("read",)),
        (("vault",), ("kv", "get")),
        (("vault",), ("print", "token")),
        (("doppler",), ("secrets",)),
        (("npm", "pnpm", "yarn"), ("token", "create")),
        (("security",), ("find-generic-password",)),
        (("security",), ("find-internet-password",)),
        (("terraform", "tofu"), ("output",)),
        (("systemctl",), ("show-environment",)),
        (("docker", "podman", "nerdctl"), ("inspect",)),
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
# of the first RUNNER_TAILS tails of their arguments may be that command.
# (Not `find`, `xargs`, `fd`, `parallel`: they append file names to the
# command, so `find . -name env` would read as a bare `env`; their reader
# forms are judged on their own, below.)
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
_XARGS_VALUED = frozenset({"-a", "-d", "-E", "-e", "-I", "-i", "-L", "-l", "-n", "-P", "-s",
                           "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines",
                           "--max-args", "--max-procs", "--max-chars", "--process-slot-var"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-x", "-X", "--exec", "--exec-batch"})
_ENV_VALUED = frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0", "-S", "--split-string"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_SUB = re.compile("\x01(\\d+)\x02")
# Leading words of the shell's own grammar: what follows is a command.
_LEADERS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "{", "(", "((",
                      "coproc", "function"})
_GRAMMAR_WORDS = frozenset({"case", "for", "select", "done", "fi", "esac"})
_CLOSERS = frozenset({"fi", "done", "esac", "}", ")", "))", ";;", "&&", "||"})
_REDIR = re.compile(r"(?:\d+|\{[A-Za-z_]\w*\})?(<<<|<<-?|<>|<&|>&|&>>|&>|>>|>\||<|>)(.*)", re.DOTALL)
_PRINTER_WORDS = frozenset({"env", "printenv", "set", "export", "declare", "typeset"})

# A word any rule 3 shape needs; a line with none of them is skipped unparsed.
_TRIGGER_WORDS = frozenset(
    {"env", "printenv", "set", "export", "declare", "typeset", "echo", "printf", "print", "eval",
     "pass", "ps", "git", "find", "fd", "bfs", "xargs"}
    | {p for progs, _ in SUBCOMMANDS for p in progs} | READERS | SHELLS | RUNNERS
    | set(_WRAPPERS) | INTERPRETERS | (_LEADERS - {"!", "{", "(", "(("}) | _GRAMMAR_WORDS)
_WORDS_RE = re.compile(r"[\w.\-]+")


def _flat(text: str) -> str:
    """The text without quotes and backslashes: `e''nv` is `env` to a shell."""
    return text.replace("'", "").replace('"', "").replace("\\", "")


def _has_trigger(flat: str) -> bool:
    if "--show-" in flat or "environ" in flat or "$" in flat or "`" in flat or "<" in flat:
        return True
    return any(word in _TRIGGER_WORDS for word in _WORDS_RE.findall(flat))


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False)
    except ValueError:
        return segment.split()


def _base(token: str) -> str:
    # Case is kept: a shell runs `env`, never `ENV` (a Dockerfile line).
    return PurePosixPath(token).name if token else ""


def _substitutions(command: str) -> tuple[str, list[str]]:
    """`command` with each `$(…)`, `` `…` ``, `<(…)` and `>(…)` replaced by a
    placeholder word, and their inner texts (judged as command lines of their
    own). Single-quoted text is literal. Unbalanced: the rest is the inner."""
    out, inner = [], []
    i, n, quote_ = 0, len(command), ""
    while i < n:
        ch = command[i]
        if quote_ == "'":
            out.append(ch)
            if ch == "'":
                quote_ = ""
            i += 1
            continue
        if ch == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if ch == "'" and not quote_:
            quote_ = "'"
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            quote_ = "" if quote_ == '"' else '"'
            out.append(ch)
            i += 1
            continue
        opener = command[i:i + 2]
        if opener in ("$(", "<(", ">(") or ch == "`":
            if ch == "`":
                close = command.find("`", i + 1)
                close = n if close < 0 else close
                body, end = command[i + 1:close], close + 1
            else:
                depth, k = 1, i + 2
                while k < n and depth:
                    if command[k] == "(":
                        depth += 1
                    elif command[k] == ")":
                        depth -= 1
                    k += 1
                body, end = command[i + 2:k - 1 if depth == 0 else n], k
            out.append(f"\x01{len(inner)}\x02")
            inner.append(body)
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out), inner


def _clean(tokens: list[str]) -> tuple[list[str], list[str]]:
    """(the command's words, the files it reads by redirection): the shell's
    own leading words (`then`, `do`, `!`, `{`, `case … )`, …) and every
    redirection taken off, and trailing closers (`fi`, `done`, `}`) dropped."""
    tokens = list(tokens)
    while tokens:
        first = tokens[0]
        if first in _LEADERS:
            tokens = tokens[1:]
        elif first == "case":
            close = next((k for k in range(1, len(tokens)) if tokens[k].endswith(")")), None)
            tokens = tokens[close + 1:] if close is not None else []
        elif len(first) > 1 and first[0] in "!({":
            tokens = [first[1:]] + tokens[1:]
        else:
            break
    words, inputs, k = [], [], 0
    while k < len(tokens):
        match = _REDIR.fullmatch(tokens[k])
        if match is not None:
            op, target = match.group(1), match.group(2)
            if not target and k + 1 < len(tokens):
                k += 1
                target = tokens[k]
            if op in ("<", "<>"):
                inputs.append(target)
            k += 1
            continue
        words.append(tokens[k])
        k += 1
    while words and words[-1] in _CLOSERS:
        words.pop()
    if words and words[-1].endswith(")") and words[-1] != ")":
        words[-1] = words[-1].rstrip(")")
    return [w for w in words if w], inputs


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


def _unwrap(tokens: list[str], depth: int, ctx) -> tuple[list[str], bool]:
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
                        return [], _prints(value, depth + 1) or not value.strip()
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


def _dynamic(arg: str) -> bool:
    """An operand only the shell knows: a variable, a substitution, `{}`."""
    return "$" in arg or "`" in arg or "{}" in arg or "\x01" in arg


class _Context:
    """One command line's state: what it names, what it assigns, its
    substitutions."""

    def __init__(self, line: str, inner: list[str]):
        self.protected = protected_in(_flat(line)) or any(protected_in(_flat(t)) for t in inner)
        self.inner = inner
        self.variables: dict[str, str] = {}


def _simple(tokens: list[str], depth: int, ctx: _Context) -> bool:
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
        if ctx.protected and (any(_dynamic(a) for a in operands) or not operands):
            return True                                    # `for f in .env; do cat "$f"`
        return v1_secrets.protected_in_command(" ".join(shlex.quote(a) for a in args)) is not None
    if prog in INTERPRETERS:
        return protected_in(_flat(" ".join(args)))
    if prog == "git":
        rest = _skip_options(args, frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"}))
        if rest and rest[0] in GIT_READS and any(_names_credential(a) for a in rest[1:]):
            return True
    if prog in ("find", "fd", "bfs") and ctx.protected:
        for k, arg in enumerate(args):
            if arg in _FIND_EXEC and k + 1 < len(args):
                inner, printed = _unwrap(args[k + 1:], depth + 1, ctx)
                if printed or (inner and _base(inner[0]) in READERS):
                    return True
    if prog == "xargs":
        rest = _skip_options(args, _XARGS_VALUED)
        arg_file = next((args[k + 1] for k, a in enumerate(args[:-1]) if a in ("-a", "--arg-file")), "")
        if _names_credential(arg_file):
            return True                                    # its lines become the command's words
        inner, printed = _unwrap(rest, depth + 1, ctx) if rest else (["echo"], False)
        if printed or (inner and _base(inner[0]) in READERS
                       and (ctx.protected or _names_credential(arg_file))):
            return True
        if inner and _simple(inner, depth + 1, ctx):
            return True
    if prog == "ps":
        # BSD-style options (`ps e`, `ps axe`, `ps eww`) print each process's environment.
        return bool(args) and re.fullmatch(r"[A-Za-z]+", args[0]) is not None and "e" in args[0]
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
                if k + 1 < len(args) and _prints(args[k + 1], depth + 1):
                    return True
                break
    if prog == "eval":
        return _prints(" ".join(args), depth + 1)
    if prog in RUNNERS:
        for k in range(1, min(len(tokens), RUNNER_TAILS + 1)):
            tail = tokens[k]
            if any(c in tail for c in " ;|&") and _prints(tail, depth + 1):
                return True
            inner, printed = _unwrap(tokens[k:], depth + 1, ctx)
            if printed or (inner and _base(inner[0]) not in RUNNERS and _simple(inner, depth + 1, ctx)):
                return True
    return False


def _command_word(tokens: list[str], ctx: _Context) -> tuple[list[str], bool]:
    """Resolve a command word the shell computes: a `$name` assigned earlier
    on the line, or a substitution — which prints when what it runs prints
    or names a printer (`$(echo env)`, `"$(which env)"`)."""
    word = tokens[0]
    var = _VAR.fullmatch(word)
    if var is not None and var.group(1) in ctx.variables:
        return _tokens(ctx.variables[var.group(1)]) + tokens[1:], False
    sub = _SUB.fullmatch(word)
    if sub is not None:
        text = ctx.inner[int(sub.group(1))]
        if _PRINTER_WORDS & set(_tokens(_flat(text))):
            return tokens, True
    return tokens, False


def _segment_prints(segment: str, depth: int, ctx: _Context) -> bool:
    words, inputs = _clean(_tokens(segment))
    if any(_names_credential(t) or (ctx.protected and _dynamic(t)) for t in inputs):
        return True                                        # `< .env cat`, `done < .env`
    if not words:
        return False
    if all(_ASSIGNMENT.fullmatch(w) for w in words):
        for w in words:
            name, _, value = w.partition("=")
            ctx.variables[name] = value
        return False
    lead = 0
    while lead < len(words) and _ASSIGNMENT.fullmatch(words[lead]):
        lead += 1
    if lead == len(words):
        return False
    words, printed = _command_word(words[lead:], ctx)
    if printed:
        return True
    inner, printed = _unwrap(words, depth, ctx)
    if printed:
        return True
    return bool(inner) and _simple(inner, depth, ctx)


_cache: dict[str, bool] = {}
_cache_size = 0
_cache_lock = threading.Lock()


def _prints(command: str, depth: int) -> bool:
    if depth > _DEPTH:
        return True
    command = rules.join_continuations(str(command or ""))[:4096]
    if not command.strip():
        return False
    flat = _flat(command)
    if _STRONG.search(command) or _STRONG.search(flat):
        return True
    if not (_has_trigger(flat) or protected_in(flat)):
        return False
    body, inner = _substitutions(command)
    if any(_prints(text, depth + 1) for text in inner):
        return True
    ctx = _Context(command, inner)
    return any(_segment_prints(segment, depth, ctx) for segment in rules.segments(body))


def prints_secrets(command: str, depth: int = 0) -> bool:
    """True when a command line (as typed) would print a secret, by shape.

    Robust to leading whitespace, leading `VAR=value` assignments, paths
    (`/usr/bin/env`), quoting (`e''nv`), wrappers (`sudo -k`, `env -i`,
    `nohup`, `timeout 5` …), the shell's own grammar (`then`, `do`, `!`,
    `{`, `case … )`, `coproc`), redirections (`< .env cat`), pipelines and
    lists (every segment is judged), subshells, `$(…)` and backticks (judged
    as command lines too), `sh -c '…'` strings and `ssh host cmd`. Past
    `_DEPTH` levels of nesting the answer is yes: a guard that cannot see is
    not a reason to read. Cached by total key size (`CACHE_BYTES`)."""
    global _cache_size
    command = str(command or "")
    if depth == 0 and len(command) <= 2048:
        hit = _cache.get(command)
        if hit is not None:
            return hit
        answer = _prints(command, 0)
        with _cache_lock:
            if _cache_size + len(command) > CACHE_BYTES:
                _cache.clear()
                _cache_size = 0
            if command not in _cache:
                _cache[command] = answer
                _cache_size += len(command)
        return answer
    return _prints(command, depth)


def _cache_clear() -> None:
    global _cache_size
    with _cache_lock:
        _cache.clear()
        _cache_size = 0


prints_secrets.cache_clear = _cache_clear       # the lru_cache spelling, for tests and probes


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


# The end of a prompt: a glyph shells and themes end their prompts with
# (powerline's private-use separators included), then whitespace. What
# follows may be a command line.
_PROMPT_END = re.compile("[$#%>❯»›➜λ✗➤▶→-]\\s+")
_BARE_WEAK = frozenset({"env", "set", "export", "declare", "typeset"})
# Where a reading command may start on a line that names a credential file,
# whatever the prompt looks like (one made of words and spaces has no glyph).
_READER_WORD = re.compile(r"(?<![\w./\-])(?:" + "|".join(sorted(map(re.escape, READERS | INTERPRETERS
                                                                    | {"git", "xargs", "find"})))
                          + r")(?=\s)")


def _worth_parsing(candidate: str) -> bool:
    """A cheap look at a candidate's first word before any parsing."""
    head = candidate.lstrip("!({ ").split(None, 1)
    if not head:
        return False
    word = _flat(head[0])
    return (word in _TRIGGER_WORDS or "=" in word or word[:1] in ("<", "$", "\x01", "`", ">")
            or _base(word) in _TRIGGER_WORDS or protected_in(_flat(candidate)))


def shows_printer(line: str) -> bool:
    """True when a line of terminal text shows a secret-printing command line:
    one of the unmistakable ones anywhere, or a command after a prompt (or as
    the whole line, unless it is a single common word that `ls -1` could
    print). Linear: at most `CANDIDATES_PER_LINE` candidates of at most
    `CANDIDATE_CAP` characters each."""
    line = line[:SCAN_LINE_CAP]
    if not line.isascii():
        line = untrusted.strip_invisible(line)
    flat = _flat(line)
    if _STRONG.search(line) or _STRONG.search(flat):
        return True
    if not (_has_trigger(flat) or protected_in(flat)):
        return False
    whole = line.strip()[:CANDIDATE_CAP]
    if whole and whole.lower() not in _BARE_WEAK and _worth_parsing(whole) and prints_secrets(whole):
        return True
    for count, match in enumerate(_PROMPT_END.finditer(line)):
        if count >= CANDIDATES_PER_LINE:
            break
        rest = line[match.end():match.end() + CANDIDATE_CAP].strip()
        if rest and _worth_parsing(rest) and prints_secrets(rest):
            return True
    if protected_in(flat):
        for count, match in enumerate(_READER_WORD.finditer(line)):
            if count >= CANDIDATES_PER_LINE:
                break
            if prints_secrets(line[match.start():match.start() + CANDIDATE_CAP]):
                return True
    return False


# -- the heuristic -------------------------------------------------------------------

KEYWORDS = ("key", "token", "secret", "password", "passwd", "pwd", "auth", "bearer",
            "credential", "api_key", "private")
_KEYWORD = re.compile("|".join(KEYWORDS), re.IGNORECASE)
_WORDRUN = re.compile(r"[A-Za-z0-9_.\-]+")
# Runs of base64/hex-ish characters; `=` only as trailing padding, so
# `KEY=value` yields `value`.
_TOKEN = re.compile(r"[A-Za-z0-9+/_\-.~]+={0,3}")
_NEAR = 40                 # characters between the keyword's word and the token
_PATH = re.compile(r"(?:~|\.{1,2})?/[\w.\-/]*")
_DOTTED = re.compile(r"[A-Za-z_]+(?:\.[A-Za-z_]+)+")
_WORDS = re.compile(r"[a-z]+(?:[_\-][a-z]+)+")
_URL_CRED = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,30}://[^\s/:@]{1,256}:([^\s/@]{3,256})@")


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
    if len(token.rstrip("=")) < 20 or entropy(token) < ENTROPY_FLOOR:
        return False
    if _PATH.fullmatch(token) or _DOTTED.fullmatch(token) or _WORDS.fullmatch(token):
        return False
    return True


def withhold(line: str) -> bool:
    """The heuristic: a keyword, and after it — past `=`, `:`, whitespace or a
    quote, within `_NEAR` characters of the keyword's word — a long,
    high-entropy token; or a URL with a password in it. One pass over the
    line (2026-10-10 review: the old loop rescanned the line per keyword)."""
    line = line[:LINE_SCAN]
    for match in _URL_CRED.finditer(line):
        if set(match.group(1)) - set("*xX•."):
            return True
    keywords = list(_KEYWORD.finditer(line))
    if not keywords:
        return False
    runs = [(m.start(), m.end()) for m in _WORDRUN.finditer(line)]
    starts = [start for start, _ in runs]
    ends = set()
    for keyword in keywords:
        index = bisect_right(starts, keyword.start()) - 1
        ends.add(runs[index][1] if index >= 0 and runs[index][1] > keyword.start() else keyword.end())
    word_ends = sorted(ends)
    for token in _TOKEN.finditer(line):
        if len(token.group()) < 20:
            continue
        index = bisect_right(word_ends, token.start() - 1) - 1
        if index < 0 or token.start() - word_ends[index] > _NEAR:
            continue
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


def rule3(window_start: int, end: int, lines, ink, spans, spans_from: int, integrated: bool,
          ink_overflow: int | None = None) -> bool:
    for span in spans:
        if prints_secrets(span.command) and (
                _span_overlaps(span, window_start, end)
                or (backgrounds(span.command) and span.start < end)):
            return True
    lo = scan_start(window_start, spans, spans_from, integrated)
    if ink_overflow is not None:
        return True                                        # the ink is incomplete: fail closed
    return (any(line.last >= lo and shows_printer(line.text) for line in lines)
            or any(line.last >= lo and shows_printer(line.text) for line in ink))


_PEM_MARK = re.compile(r"-----(BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")


def inside_private_key(lines_before) -> bool:
    """Does the read start inside a private key's block — a `BEGIN … PRIVATE
    KEY` line before it with no `END` after that?"""
    inside = False
    for line in lines_before:
        text = line.text[:SCAN_LINE_CAP]
        if "-----" in text:
            for mark in _PEM_MARK.finditer(text):
                inside = mark.group(1) == "BEGIN"
    return inside


def judge(data: bytes, start: int, *, lines: int = DEFAULT_LINES, alt: bool = False, rows: int = 24,
          cols: int = 80, spans=(), spans_from: int = 0, integrated: bool = False,
          values=()) -> Verdict:
    """One read: the last `lines` lines of `data` (the ring from offset
    `start`) at the terminal's size, refused whole or handed back with lines
    withheld."""
    lines = max(1, min(MAX_LINES, int(lines)))
    rendered = terminal_text.render(data, start, alt=alt, rows=rows, cols=cols)
    every = rendered.lines
    window = every[-lines:]
    window_start = min((line.first for line in window), default=rendered.end)
    # What the rules read is a superset of what is returned: each line to
    # LINE_SCAN, after invisible characters are stripped.
    text = "\n".join(line.text[:LINE_SCAN] for line in window)
    if not text.isascii():
        text = untrusted.strip_invisible(text)
    if holds_value(text, value_forms(values)) or credential_patterns.holds_credential(text):
        return Verdict(True, covered=len(window))
    before = every[:len(every) - len(window)]
    look = [line for line in before if line.last >= window_start - LOOKBACK]
    if inside_private_key(look):
        return Verdict(True, covered=len(window))
    scanned = [line for line in every if line.last >= window_start - LOOKBACK]
    if rule3(window_start, rendered.end, scanned, rendered.ink, spans, spans_from, integrated,
             rendered.ink_overflow):
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
