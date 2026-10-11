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
     that shows such a command line (a command at the line's start, after a
     prompt glyph or after a prompt made of words, or one of the
     unmistakable ones anywhere — but never a reader's name in prose: a
     commit message "Add tail call before loading .env.production" is not
     `tail`). **Spans are advisory**: a program can print
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

What loads a file is not what prints it (2026-10-11 review): an
`--env-file` (`node --env-file=.env server.js`, the Node 20 way to start a
dev server) loads one, `wc -l < .env` counts one, and `docker inspect
--format '{{.State.Status}}'` prints no environment — so a dev server's log
stays readable for as long as it runs. An interpreter prints a credential
when it is handed inline source on a line naming one, inline source reading
the environment (`node -p process.env`), or a credential file as its script.

**Everything here is linear in what it reads** (2026-10-10 review): a read
renders at most a ring (1 MiB) at the terminal's width, checks at most
`LINE_SCAN` characters of each returned line, tries at most
`CANDIDATES_PER_LINE` command candidates of at most `CANDIDATE_CAP` characters
on a line (looked for in the first `SCAN_LINE_CAP`), judges each distinct
line text once, and caches by total size and by count. **And bounded**
(2026-10-11 review): a read parses at most `PARSE_BUDGET` characters of
commands it has not judged before, and past that it is refused — an
adversarial screen can buy a refusal, never a slow read. **Every bound fails
closed**: what a bound leaves unparsed (a command past `COMMAND_CUT`, a
candidate past its cap, candidates past the per-line cap, a line past the
scan, a runner's arguments past `RUNNER_TAILS`, wrappers past the unwrap, a
span command past the backend's `COMMAND_CAP`) refuses the read when it
`could_print` — names a credential file, a printer, a secret-named variable,
an environment dump — and only then, so a long command is not refused for
being long.

**The limit, stated plainly** (W-2): a secret with no recognisable shape and
no keyword beside it — a random password printed on its own — is not caught;
nor is one printed in pieces, reversed, or encoded some other way. Rule 3
judges command lines by shape, so it cannot see through an alias, a shell
function, a script, a symlink or a program that prints a secret on its own
initiative: their output meets rules 1 and 2 and the heuristic only — and
so does a script handed a credential file as an argument (`python3 show.py
.env`): only an interpreter's own inline source or script counts. The
owner's "Jarvis can read" switch and the HUD note on every read are the
backstops.
"""
from __future__ import annotations

import base64
from bisect import bisect_right
from dataclasses import dataclass
import itertools
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
CACHE_ENTRIES = 32_768     # … and by count (8-character keys would otherwise be ~524k entries)
COMMAND_CUT = 4096         # characters of one command line parsed; past it, `could_print`
PARSE_BUDGET = 128 * 1024  # characters rule 3 may parse (cache misses) in one read; past it, refused
WORD_PROMPT_CAP = 160      # how far into a line a prompt made of words may reach
HAZARD_COST = 8            # characters `could_print` scans per character of PARSE_BUDGET
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


# One compression suffix: `zcat id_rsa.gz` prints the key.
_COMPRESSED = re.compile(r"\.(?:gz|bz2|xz|zst|lz4|lzma|lz|Z|z|br)$")


def _names_credential(arg: str) -> bool:
    """An argument that names a credential file: the word itself, or the path
    after a `REV:` (`git show HEAD:.env`, `file://…/.env`) or an `--opt=`,
    with or without one compression suffix."""
    arg = arg.strip("\"'")
    parts = {arg, arg.rsplit(":", 1)[-1], arg.split("=", 1)[-1]}
    parts |= {_COMPRESSED.sub("", part) for part in parts}
    return any(protected_name(part) for part in parts if part)


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
                   "+systemctl/tmux show-environment, +docker/podman inspect (unless --format "
                   "names only safe fields), +docker compose config, +dotenv list/get, "
                   "+an interpreter's inline source that reads the whole environment or a "
                   "secret-named variable (node -p process.env, python3 -c '…os.environ…'), "
                   "+php -i",
    "credential files": "cat, less, head, tail, bat (+ the other readers in READERS, decompressing "
                        "ones included, + git show/diff/blame/cat-file/log/grep) of a protected "
                        "path — secrets.is_protected, + .env.* but the templates, + the "
                        "CREDENTIAL_FILE paths, + either with a compression suffix — named as an "
                        "operand, after `REV:`, as a file: URL, or as an input redirection (but "
                        "into a counter: wc, the checksums); + an interpreter given inline source "
                        "on a line naming one, or running one as its script (never for a "
                        "--env-file: that loads it); + a copy onto the terminal (cp .env "
                        "/dev/stdout), an archive member to stdout (tar -xO, unzip -p, 7z -so), "
                        "a compressor's -c; + a reader of `$var`/`{}`/a substitution, find -exec "
                        "or xargs reader, on a line that names one",
    "tokens": "gh auth token, gh auth status -t, anything with --show-token, aws configure "
              "get / export-credentials, vercel env pull, git credential fill (+ SUBCOMMANDS)",
    "variables": "+echo/printf of a $VARIABLE whose name holds a heuristic keyword",
}

READERS = frozenset({
    "cat", "tac", "nl", "less", "more", "most", "pg", "head", "tail", "bat", "batcat", "view",
    "xxd", "od", "hexdump", "strings", "base64", "base32", "grep", "egrep", "fgrep", "rg", "ag",
    "ack", "ugrep", "sed", "awk", "gawk", "mawk", "cut", "sort", "uniq", "column", "jq", "yq",
    "diff", "colordiff", "paste", "fold", "fmt", "pr", "expand", "iconv", "dd", "tr", "rev",
    # Decompressing readers (2026-10-11 review): `zcat .env.gz`.
    "zcat", "gzcat", "bzcat", "xzcat", "lzcat", "zstdcat", "lz4cat", "zless", "zmore", "bzless",
    "bzmore", "xzless", "xzmore", "zgrep", "zegrep", "zfgrep", "bzgrep", "xzgrep", "zstdgrep", "zdiff",
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
        # Whole-environment and resolved-config dumps (2026-10-11 review).
        (("tmux",), ("show-environment",)),
        (("tmux",), ("showenv",)),
        (("docker", "podman", "nerdctl"), ("compose", "config")),
        (("docker-compose", "podman-compose"), ("config",)),
        (("dotenv",), ("list",)),
        (("dotenv",), ("get",)),
        (("dotenvx",), ("get",)),
        (("dotenvx",), ("decrypt", "--stdout")),
        (("railway",), ("variables",)),
        (("netlify", "ntl"), ("env:list",)),
        (("netlify", "ntl"), ("env:get",)),
    )
)
# `docker inspect` prints `Config.Env` unless a format names only safe
# fields (`_inspect_prints`).
CONTAINER_CLIS = frozenset({"docker", "podman", "nerdctl"})

# Unmistakable anywhere in a line — in a command line or in output that
# shows one. Word-bounded.
_STRONG = re.compile(
    r"(?<![\w-])printenv(?![\w-])|--show-(?:token|secrets?)\b|(?<![\w-])gh\s+auth\s+token\b"
    r"|(?<![\w-])git\s+credential(?:-store|-cache)?\s+(?:fill|get)\b|\bexport-credentials\b"
    r"|(?<![\w-])vercel\s+env\s+pull\b|(?<![\w-])aws\s+configure\s+get\b"
    r"|/proc/[^\s/]+/environ\b"
)
# A `$VARIABLE` whose name holds a keyword — matched as the whole name and
# then searched, so a 1 MiB name is one linear pass, never a backtrack.
_VAR_WORD = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*")
_VAR_KEYWORD = re.compile(r"key|token|secret|pass|pwd|auth|bearer|cred|private", re.IGNORECASE)


def _keyword_vars(text: str):
    """Each `$VARIABLE` in `text` whose name holds a heuristic keyword."""
    return (m for m in _VAR_WORD.finditer(text) if _VAR_KEYWORD.search(m.group()))

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
_NAME = re.compile(r"[A-Za-z_][\w.:-]*")
_FUNCTION_HEAD = re.compile(r"[A-Za-z_][\w.:-]*\(\)")
_REDIR = re.compile(r"(?:\d+|\{[A-Za-z_]\w*\})?(<<<|<<-?|<>|<&|>&|&>>|&>|>>|>\||<|>)(.*)", re.DOTALL)
_PRINTER_WORDS = frozenset({"env", "printenv", "set", "export", "declare", "typeset"})

# An option that loads a file into a program's environment rather than
# printing it (2026-10-11 review): `node --env-file=.env server.js` is how
# Node 20 starts a dev server, and that server's log is the read this tool
# exists for. Deno takes the value only after `=`.
_LOADER = re.compile(r"--(?:env[-_]?file(?:-if-exists)?|dotenv(?:[-_]path)?)(?=$|=)")
_LOADER_TEXT = re.compile(r"(?<!\S)--(?:env[-_]?file(?:-if-exists)?|dotenv(?:[-_]path)?)(?:=\S*|\s+(?!-)\S+)?")
# Programs that read their input without printing it: `wc -l < .env`.
COUNTERS = frozenset({"wc", "md5sum", "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum",
                      "b2sum", "cksum", "sum", "shasum"})
# Where a copy lands on the terminal: `cp .env /dev/stdout`.
_TERMINAL_OUT = re.compile(r"/dev/(?:stdout|stderr|tty|console|fd/\d+|pts/\d+)"
                           r"|/proc/(?:self|thread-self|\d+)/fd/\d+")
_COMPRESSORS = frozenset({"gzip", "gunzip", "pigz", "unpigz", "bzip2", "bunzip2", "pbzip2", "xz", "unxz",
                          "lzma", "unlzma", "lzip", "zstd", "unzstd", "lz4", "unlz4", "brotli"})
_ARCHIVERS = frozenset({"tar", "gtar", "bsdtar", "unzip", "7z", "7za", "7zr", "7zz", "ar"})

# Interpreters: (short letters whose value is inline source, short letters
# that take a value, short letters that take only a glued value, long
# inline-source options, long options that take a value). What is not here
# is read as a flag; a misread costs only when the word taken for the
# script is itself a credential file.
_INTERPRETER_OPTIONS: dict[str, tuple[str, str, str, frozenset, frozenset]] = {
    "python": ("c", "mWXQ", "", frozenset(), frozenset({"--check-hash-based-pycs"})),
    "node": ("ep", "rC", "", frozenset({"--eval", "--print"}),
             frozenset({"--require", "--import", "--loader", "--experimental-loader", "--input-type",
                        "--conditions", "--title", "--inspect-port"})),
    "bun": ("ep", "r", "", frozenset({"--eval", "--print"}),
            frozenset({"--require", "--preload", "--config", "--cwd", "--tsconfig-override"})),
    "deno": ("", "", "", frozenset({"--eval"}), frozenset({"--config", "--import-map", "--lock",
                                                           "--location", "--cert", "--seed"})),
    "ruby": ("e", "rICEFx", "i", frozenset(), frozenset()),
    "perl": ("eE", "IMmx", "il0", frozenset(), frozenset()),
    "php": ("rBRFE", "dcfz", "", frozenset(), frozenset()),
    "lua": ("e", "l", "", frozenset(), frozenset()),
    "Rscript": ("e", "", "", frozenset(), frozenset()),
}
_INTERPRETER_OPTIONS["python3"] = _INTERPRETER_OPTIONS["python"]
_POWERSHELLS = frozenset({"pwsh", "powershell", "pwsh.exe", "powershell.exe"})
# Python modules that print a file they are handed.
_PRINTING_MODULES = frozenset({"json.tool", "base64", "quopri", "uu", "gzip", "zipfile", "tarfile",
                               "tokenize", "ast", "dis", "pickletools", "binascii"})
# Inline source that reads the environment: the whole of it, or one
# variable (`.NAME`, `["NAME"]`, `.get("NAME")`, `getenv("NAME")`,
# `$ENV{NAME}`, `$env:NAME`) — which counts only when the name looks secret.
_ENV_REF = re.compile(
    r"process\.env|Bun\.env|Deno\.env(?:\.toObject)?|os\.environb?|os\.getenv|Sys\.getenv"
    r"|System\.getenv|GetEnvironmentVariables?|(?<![\w.])environ(?!\w)|(?<![\w$%.])ENV(?!\w)|%ENV|\$ENV"
    r"|(?<![\w.])getenv(?!\w)|\$_ENV|\$_SERVER|\$env:|(?<![\w$-])env:")
_ENV_NAMED = re.compile(r"\s*(?:\.(?:get|fetch|getenv)\s*\(\s*|\[\s*|\{\s*|\(\s*|\.)\s*[\"']?([A-Za-z_]\w*)")
_ENV_COLON_NAMED = re.compile(r"([A-Za-z_]\w*)")       # after `$env:`
_ENV_DUMP_METHODS = frozenset({"toObject", "items", "keys", "values", "copy", "to_h", "to_a", "to_hash",
                               "inspect", "each", "each_pair", "entries", "__dict__", "data", "all"})
_SECRET_NAME = re.compile(r"key|token|secret|pass|pwd|auth|bearer|cred|private|dsn|_ur[li]$", re.IGNORECASE)

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
        if first == "function" and len(tokens) > 1:
            tokens = tokens[2:]                            # `function f { …`: the body runs when called
            if tokens[:1] == ["()"]:
                tokens = tokens[1:]
        elif _FUNCTION_HEAD.fullmatch(first) or (tokens[1:2] == ["()"] and _NAME.fullmatch(first)):
            tokens = tokens[1 if first.endswith("()") else 2:]   # `f() { cat .env; }; f`
        elif first in _LEADERS:
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
    if tokens and (_base(tokens[0]) == "env" or _base(tokens[0]) in _WRAPPERS):
        return [], True                                     # wrappers past the loop: not seen, not read
    return tokens, False


def _subsequence(words: tuple[str, ...], args: list[str]) -> bool:
    it = iter(a.lower() for a in args)
    return all(any(a == w or a.startswith(w + "=") for a in it) for w in words)


def _dynamic(arg: str) -> bool:
    """An operand only the shell knows: a variable, a substitution, `{}`."""
    return "$" in arg or "`" in arg or "{}" in arg or "\x01" in arg


def reads_environment(source: str, loaded: bool = False) -> bool:
    """Does an interpreter's inline source print the environment: the whole of
    it, a variable whose name looks secret, or — after an `--env-file` loaded
    one — any variable at all?"""
    for ref in _ENV_REF.finditer(source):
        if loaded:
            return True
        if ref.group().endswith(":"):
            named = _ENV_COLON_NAMED.match(source, ref.end())
        else:
            named = _ENV_NAMED.match(source, ref.end())
        if named is None:
            return True                                    # the whole environment
        name = named.group(1)
        if name in _ENV_DUMP_METHODS or _SECRET_NAME.search(name):
            return True
    return False


def _drop_loaders(prog: str, args: list[str]) -> tuple[list[str], bool]:
    """`args` without `--env-file`-style options and their values, and
    whether there was one."""
    out, loaded, i = [], False, 0
    while i < len(args):
        arg = args[i]
        if _LOADER.match(arg):
            loaded = True
            i += 1 if ("=" in arg or prog == "deno" or i + 1 >= len(args) or args[i + 1].startswith("-")) else 2
            continue
        out.append(arg)
        i += 1
    return out, loaded


def _interpreter_parts(prog: str, args: list[str]):
    """(inline sources, the script, the module, what follows the script or
    module) for an interpreter's arguments. `None` for the sources means an
    encoded command nobody can read here."""
    if prog in _POWERSHELLS:
        for k, arg in enumerate(args):
            low = arg.lower()
            if len(low) >= 2 and ("-encodedcommand".startswith(low) or low == "-ec"):
                return None, None, None, []
            if len(low) >= 2 and "-command".startswith(low):
                return [" ".join(args[k + 1:])], None, None, []
            if len(low) >= 2 and "-file".startswith(low):
                return [], (args[k + 1] if k + 1 < len(args) else None), None, args[k + 2:]
        return [], None, None, []
    inline_s, valued_s, glued_s, inline_l, valued_l = _INTERPRETER_OPTIONS.get(
        prog, ("", "", "", frozenset(), frozenset()))
    sources: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in inline_l:
                sources.append(value if eq else (args[i + 1] if i + 1 < len(args) else ""))
                i += 1 if eq else 2
            else:
                i += 2 if (name in valued_l and not eq) else 1
            continue
        step, j = 1, 1
        while j < len(arg):
            letter, glued = arg[j], arg[j + 1:]
            j += 1
            if letter in glued_s:
                if letter in "l0":                         # perl -l[octal], -0[digits]: the cluster goes on
                    while j < len(arg) and (arg[j].isdigit() or (letter == "0" and arg[j] in "xX")):
                        j += 1
                    continue
                break                                      # -i[extension]: the rest is the extension
            if letter in inline_s or letter in valued_s:
                value = glued or (args[i + 1] if i + 1 < len(args) else "")
                step = 1 if glued else 2
                if letter in inline_s:
                    sources.append(value)
                elif letter == "m" and prog in ("python", "python3"):
                    return sources, None, value, args[i + step:]
                elif letter == "f" and prog == "php":
                    return sources, value, None, args[i + step:]
                break
        i += step
    rest = args[i:]
    if prog in ("deno", "bun") and rest and rest[0] in ("run", "eval", "serve", "test", "bench", "x"):
        verb, rest = rest[0], _skip_options(rest[1:], valued_l)
        if verb == "eval":
            return sources + rest[:1], None, None, rest[1:]
    return sources, (rest[0] if rest else None), None, rest[1:]


def _interpreter_prints(prog: str, args: list[str], depth: int, ctx) -> bool:
    """An interpreter prints a credential when it is handed inline source on
    a line that names a credential file, or inline source that reads the
    environment (`node -p process.env`); when the script it runs *is* one
    (`python3 .env`); or when a module prints one it is handed. Never for an
    `--env-file`: that loads the file (2026-10-11 review)."""
    if prog == "php" and any(a in ("-i", "--info") for a in args):
        return True                                        # phpinfo(): the environment
    args, loaded = _drop_loaders(prog, args)
    sources, script, module, rest = _interpreter_parts(prog, args)
    if sources is None:
        return True                                        # an encoded command: unseen
    if sources:
        return (protected_in(_flat(" ".join(args)))
                or any(reads_environment(source, loaded) for source in sources))
    if module is not None:
        if module in _PRINTING_MODULES and any(_names_credential(a) for a in rest):
            return True
        return module not in INTERPRETERS and _simple([module, *rest], depth + 1, ctx)
    return script is not None and _names_credential(script)


_INSPECT_RISKY = re.compile(r"env|config|args?\b|cmd|entrypoint|label|secret|auth|cred|token|pass|key",
                            re.IGNORECASE)


def _inspect_prints(args: list[str]) -> bool:
    """`docker inspect` prints a container's `Config.Env` — unless a format
    names only fields that cannot hold one (`{{.State.Status}}`)."""
    fmt = None
    for k, arg in enumerate(args):
        if arg in ("-f", "--format"):
            fmt = args[k + 1] if k + 1 < len(args) else ""
        elif arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
        elif arg.startswith("-f") and not arg.startswith("--") and len(arg) > 2:
            fmt = arg[2:]
    if fmt is None:
        return True
    actions = re.findall(r"\{\{(.*?)\}\}", fmt, re.DOTALL)
    if not actions:
        return True                                        # `--format json`: the whole object
    for action in actions:
        if _INSPECT_RISKY.search(action) or re.search(r"(?:^|[\s(])\.(?=$|[\s)|])", action.strip()):
            return True                                    # a risky field, or `.` itself
    return False


def _archive_prints(prog: str, args: list[str]) -> bool:
    """An archive member or a compressed file written to the terminal:
    `tar -xOf b.tar .env`, `unzip -p a.zip .env`, `7z e -so a.7z .env`,
    `gzip -dc .env.gz`."""
    if not any(_names_credential(a) for a in args if not a.startswith("-") or "/" in a):
        return False
    clusters = [a for a in args if re.fullmatch(r"-[A-Za-z]+", a)]
    if prog in _COMPRESSORS:
        return any(a in ("--stdout", "--to-stdout") for a in args) or any("c" in a for a in clusters)
    if prog in ("tar", "gtar", "bsdtar"):
        first = args[0] if args else ""
        return (any(a in ("-O", "--to-stdout") for a in args) or any("O" in a for a in clusters)
                or (re.fullmatch(r"[A-Za-z]+", first) is not None and "O" in first))
    if prog == "unzip":
        return any("p" in a or "c" in a for a in clusters)
    if prog == "ar":
        return bool(args) and re.fullmatch(r"-?[A-Za-z]+", args[0]) is not None and "p" in args[0]
    return "-so" in args                                   # 7z


class _Context:
    """One command line's state: what it names, what it assigns, its
    substitutions."""

    def __init__(self, line: str, inner: list[str]):
        self.protected = (protected_in(_flat(_LOADER_TEXT.sub(" ", line)))
                          or any(protected_in(_flat(_LOADER_TEXT.sub(" ", t))) for t in inner))
        self.inner = inner
        self.variables: dict[str, str] = {}


def _simple(tokens: list[str], depth: int, ctx: _Context) -> bool:
    """One simple command, already unwrapped: does it print a secret?"""
    prog, args = _base(tokens[0]), tokens[1:]
    if any(a.startswith(("--show-token", "--show-secret")) for a in args):
        return True
    if any(_names_credential(a) for a in args):
        if any(_TERMINAL_OUT.fullmatch(a.split("=", 1)[-1]) for a in args):
            return True                                    # `cp .env /dev/stdout`, `dd … of=/dev/tty`
        if any("file:" in a.lower() and _names_credential(a[a.lower().index("file:"):]) for a in args):
            return True                                    # `curl -s file://$PWD/.env`
        if (prog in _ARCHIVERS or prog in _COMPRESSORS) and _archive_prints(prog, args):
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
    if prog in INTERPRETERS or prog in _POWERSHELLS:
        return _interpreter_prints(prog, args, depth, ctx)
    if prog in CONTAINER_CLIS:
        rest = _skip_options(args, frozenset({"-H", "--host", "-c", "--context", "--config", "-l",
                                              "--log-level", "--url", "--connection", "--identity"}))
        if rest[:1] == ["inspect"] or (rest[1:2] == ["inspect"] and rest[0] in (
                "container", "image", "network", "volume", "service", "secret", "config", "plugin")):
            return _inspect_prints(rest)
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
        return any(next(_keyword_vars(a), None) is not None for a in args)
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
        if len(tokens) > RUNNER_TAILS + 1 and could_print(" ".join(tokens[RUNNER_TAILS + 1:])):
            return True                                    # tails never tried: not seen, not read
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
        head = _unwrap(list(words), depth, ctx)[0] if words else []
        if not (head and _base(head[0]) in COUNTERS):
            return True                                    # `< .env cat`, `done < .env` — not `wc -l < .env`
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
    full = rules.join_continuations(str(command or ""))
    command = full[:COMMAND_CUT]
    if len(full) > COMMAND_CUT and could_print(full, COMMAND_CUT):
        return True                                        # past the cut: not seen, not read
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


class Budget:
    """Characters rule 3 may still parse in one read (`PARSE_BUDGET`). Spent
    only on cache misses; once it is gone every further command counts as a
    printer, so the read is refused — an adversarial screen can make a read
    refused, never slow (2026-10-11 review: 18.6 s and 85.7 s)."""

    __slots__ = ("left",)

    def __init__(self, chars: int | None = None):
        self.left = PARSE_BUDGET if chars is None else chars

    def spend(self, chars: int) -> bool:
        self.left -= chars
        return self.left >= 0


def prints_secrets(command: str, depth: int = 0, budget: Budget | None = None) -> bool:
    """True when a command line (as typed) would print a secret, by shape.

    Robust to leading whitespace, leading `VAR=value` assignments, paths
    (`/usr/bin/env`), quoting (`e''nv`), wrappers (`sudo -k`, `env -i`,
    `nohup`, `timeout 5` …), the shell's own grammar (`then`, `do`, `!`,
    `{`, `case … )`, `coproc`), redirections (`< .env cat`), pipelines and
    lists (every segment is judged), subshells, `$(…)` and backticks (judged
    as command lines too), `sh -c '…'` strings and `ssh host cmd`. Past
    `_DEPTH` levels of nesting the answer is yes: a guard that cannot see is
    not a reason to read — and so, past `COMMAND_CUT` characters, is a tail
    that `could_print`. Cached, bounded by total key size (`CACHE_BYTES`)
    and by count (`CACHE_ENTRIES`). With a `budget`, a cache miss spends its
    length, and an empty budget answers yes (uncached)."""
    global _cache_size
    command = str(command or "")
    if depth == 0 and len(command) <= 2048:
        hit = _cache.get(command)
        if hit is not None:
            return hit
        if budget is not None and not budget.spend(len(command)):
            return True
        answer = _prints(command, 0)
        with _cache_lock:
            if _cache_size + len(command) > CACHE_BYTES or len(_cache) >= CACHE_ENTRIES:
                _cache.clear()
                _cache_size = 0
            if command not in _cache:
                _cache[command] = answer
                _cache_size += len(command)
        return answer
    if budget is not None and not budget.spend(min(len(command), COMMAND_CUT)):
        return True
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


# -- what a part rule 3 never parsed could hold ---------------------------------------

# A guard that cannot see is not a reason to read — for length as for depth
# (2026-10-11 review). Past each bound on what rule 3 parses (`COMMAND_CUT`,
# `CANDIDATE_CAP`, `CANDIDATES_PER_LINE`, `SCAN_LINE_CAP`, `RUNNER_TAILS`,
# the wrappers `_unwrap` peels), the part not parsed refuses the read when it
# holds anything a secret-printing command line needs: a credential file's
# name (or a glob onto one), an unmistakable printer, a secret-named
# variable, a printer word standing alone, a whole-environment dump, or the
# start of a token-printing subcommand. Regular expressions only — linear —
# and ordinary long lines hold none of it, so a long command is not refused
# for being long.
_BARE_PRINTER = re.compile(r"(?:^|(?<=[\s;&|({`'\"=]))(?:env|printenv|set|export|declare|typeset)"
                           r"(?=\s*(?:$|[;&|)}`'\"]|-p\b))", re.MULTILINE)
_DUMP_TEXT = re.compile(
    r"process\.env(?![\w.\[])|Bun\.env(?![\w.\[])|os\.environb?(?![\w.\[])|%ENV\b|getenv\s*\(\s*\)"
    r"|Deno\.env\.toObject|show-?env(?:ironment)?\b|GetEnvironmentVariables\b|(?<![\w$-])env:(?!\w)"
    r"|(?<![\w-])ps\s+[A-Za-z]*e[A-Za-z]*\b|(?<![\w-])php\s+(?:-\w+\s+)*-i\b"
    r"|(?<![\w./-])(?:docker|podman|nerdctl)\s+(?:\S{1,64}\s+){0,3}?inspect\b"
    r"|(?<![\w-])pass\s+(?!(?:ls|list|find|search|init|git|insert|add|edit|generate|rm|remove|mv|cp"
    r"|help|version)\b)[\w/]")


def _subcommand_heads() -> re.Pattern:
    by_prog: dict[str, set[str]] = {}
    for progs, words in SUBCOMMANDS:
        for prog in progs:
            by_prog.setdefault(prog, set()).add(words[0])
    alternatives = [re.escape(prog) + r"\s+(?:\S{1,64}\s+){0,6}?(?:"
                    + "|".join(map(re.escape, sorted(words))) + ")"
                    for prog, words in sorted(by_prog.items())]
    return re.compile(r"(?<![\w./-])(?:" + "|".join(alternatives) + r")(?![\w-])")


_SUBCOMMAND_HEADS = _subcommand_heads()
# A glob onto a credential file: a dotfile glob at a word's start (`.env*`,
# `.e*`, `.*`), anything under ~/.ssh, ~/.aws, ~/.gnupg, ~/.kube or ~/.docker,
# and `id_*`. (`/proc/*/environ` is `_STRONG`'s.) Bounded quantifiers, so
# linear.
_GLOB_HAZARD = re.compile(r"(?:^|(?<=[\s'\"`=:]))\.(?!\d)[\w.\-]{0,32}[*?\[]"
                          r"|\.(?:ssh|aws|gnupg|kube|docker)/[^\s'\"`;|&<>()]{0,64}[*?\[]"
                          r"|(?<!\w)id_[\w.\-]{0,32}[*?\[]")
# Each pattern with the substrings one of its matches must contain: a text
# holding none of them is not scanned by it (C-speed `in` before a regex).
_HAZARDS = (
    (_RAW_PROTECTED, _HINTS), (_ENV_VARIANT, (".env.",)),
    (_STRONG, ("printenv", "--show-", "auth", "credential", "env", "configure", "environ")),
    (_BARE_PRINTER, ("env", "set", "export", "declare", "typeset")),
    (_DUMP_TEXT, ("env", "ENV", "environ", "Environment", "ps", "php", "inspect", "pass")),
    (_SUBCOMMAND_HEADS, None), (_GLOB_HAZARD, ("*", "?", "[")),
)


def last_hazard_end(text: str) -> int:
    """Where the last thing `could_print` looks for ends in `text`, or -1."""
    last = -1
    for pattern, needles in _HAZARDS:
        if needles is not None and not any(needle in text for needle in needles):
            continue
        for match in pattern.finditer(text):
            last = max(last, match.end())
    for match in _keyword_vars(text):
        last = max(last, match.end())
    return last


def unseen_end(text: str) -> int:
    """`last_hazard_end` of the text and, when it has quotes or backslashes,
    of the text as the shell reads it (`p""rintenv`). A position in the flat
    text is never past the same character's in the raw text, so comparing
    either with a cut in the raw text never places a hazard past it wrongly."""
    last = last_hazard_end(text)
    if any(c in text for c in "'\"\\"):
        last = max(last, last_hazard_end(_flat(text)))
    return last


def could_print(text: str, after: int = 0) -> bool:
    """Does `text` hold, ending past `after`, anything a secret-printing
    command line needs? The fail-closed answer for a part never parsed."""
    return len(text) > after and unseen_end(text) > after


# The end of a prompt: a glyph shells and themes end their prompts with
# (powerline's private-use separators included), then whitespace. What
# follows may be a command line.
_GLYPHS = "$#%>❯»›➜λ✗➤▶→-"
_PROMPT_END = re.compile("[" + _GLYPHS + "]\\s+")
_GLYPH = re.compile("[" + _GLYPHS + "]")
_BARE_WEAK = frozenset({"env", "set", "export", "declare", "typeset"})


def _worth_parsing(candidate: str) -> bool:
    """A cheap look at a candidate's first word before any parsing."""
    head = candidate.lstrip("!({ ").split(None, 1)
    if not head:
        return False
    word = _flat(head[0])
    if _GLYPH.fullmatch(word):
        return False                                       # a prompt: its glyph candidate reads the command
    return (word in _TRIGGER_WORDS or "=" in word or word[:1] in ("<", "$", "\x01", "`", ">")
            or _base(word) in _TRIGGER_WORDS or protected_in(_flat(candidate)))


# A prompt made of words, with no glyph: one to three words before the
# command, each carrying a prompt's mark (`john@host ~/p`, `[proj]`, `(venv)`).
_PROMPT_WORD = re.compile(r"\s*[^\s@:~/\])}]*[@:~/\])}][^\s]*\s+")


def shows_printer(line: str, budget: Budget | None = None) -> bool:
    """True when a line of terminal text shows a secret-printing command line:
    one of the unmistakable ones anywhere, or a command at the line's start,
    after a prompt glyph, or after a prompt made of words (unless it is a
    single common word that `ls -1` could print).

    Linear: at most `CANDIDATES_PER_LINE` glyph candidates of at most
    `CANDIDATE_CAP` characters, looked for in the first `SCAN_LINE_CAP`.
    What that leaves unparsed — a candidate's text past its cap, the
    candidates past the per-line cap, the line past the scan — refuses the
    read when it `could_print` (2026-10-11 review: these bounds used to fail
    open). With a `budget`, every parse spends from it."""
    if not line.isascii():
        line = untrusted.strip_invisible(line)
    scan = line[:SCAN_LINE_CAP]
    flat = _flat(scan)
    if _STRONG.search(scan) or _STRONG.search(flat):
        return True
    last: list[int] = []

    def unseen(after: int) -> bool:
        if not last:
            # A line past the scan is bounded by the window; a cut candidate is
            # not (8M characters of ink), so its scan spends from the budget.
            if (budget is not None and len(line) <= SCAN_LINE_CAP
                    and not budget.spend(2 * len(line) // HAZARD_COST)):
                last.append(len(line))                     # the budget is gone: refused
            else:
                last.append(unseen_end(line))
        return last[0] > after

    if len(line) > SCAN_LINE_CAP and unseen(SCAN_LINE_CAP):
        return True                                        # past the scan: not seen, not read
    if not (_has_trigger(flat) or protected_in(flat)):
        return False
    lead = len(scan) - len(scan.lstrip())
    whole = scan[lead:lead + CANDIDATE_CAP].rstrip()
    if whole and whole.lower() not in _BARE_WEAK and _worth_parsing(whole):
        if prints_secrets(whole, budget=budget) or unseen(lead + CANDIDATE_CAP):
            return True
    for count, match in enumerate(_PROMPT_END.finditer(scan)):
        if count >= CANDIDATES_PER_LINE:
            if unseen(match.start()):
                return True
            break
        rest = scan[match.end():match.end() + CANDIDATE_CAP].rstrip()
        if rest and _worth_parsing(rest):
            if prints_secrets(rest, budget=budget) or unseen(match.end() + CANDIDATE_CAP):
                return True
    # After a prompt made of words — and only there (2026-10-11 review: a
    # reader's or an interpreter's name in prose, "Add tail call before
    # loading .env.production", "Using python 3.12 …", is not a command).
    pos = 0
    for _ in range(3):
        match = _PROMPT_WORD.match(scan, pos, WORD_PROMPT_CAP)
        if match is None:
            break
        pos = match.end()
        rest = scan[pos:pos + CANDIDATE_CAP].rstrip()
        if rest and _worth_parsing(rest):
            if prints_secrets(rest, budget=budget) or unseen(pos + CANDIDATE_CAP):
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
          ink_overflow: int | None = None, budget: Budget | None = None) -> bool:
    budget = Budget() if budget is None else budget
    for span in spans:
        if span.start >= end:
            continue
        if ((getattr(span, "unseen", False) or prints_secrets(span.command, budget=budget))
                and (_span_overlaps(span, window_start, end) or backgrounds(span.command))):
            return True
    lo = scan_start(window_start, spans, spans_from, integrated)
    if ink_overflow is not None:
        return True                                        # the ink is incomplete: fail closed
    judged: set[str] = set()                               # one judgement per text (a redrawn row)
    for line in itertools.chain(lines, ink):
        if line.last < lo or line.text in judged:
            continue
        judged.add(line.text)
        if shows_printer(line.text, budget):
            return True
    return False


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
