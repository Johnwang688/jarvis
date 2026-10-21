"""Detection, not prevention: did a run reach for the network, or the world
outside its snapshot? Reported next to the score.

Best-effort pattern matching over the tool calls' text. The real control is
the sandbox (`rolebench.sandbox`): what these flags catch is an attempt, not
an escape.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import urllib.parse
from pathlib import Path

from . import sandbox

# Markers beyond swecompare's: the GitHub CLI and API host.
EXTRA_NETWORK = ("gh ", "api.github")
# Absolute trees every cell may name (system, interpreter, its own /tmp and /opt/rolebench).
ALLOWED_ABS = ("/usr", "/bin", "/lib", "/lib64", "/dev", "/proc/self", "/tmp", str(sandbox.OPT))   # /dev: bwrap's own


def network_flags(result) -> list[str]:
    from swecompare.bench import audit_network    # the swecompare audit, reused as is
    hits = audit_network(result)
    seen = set(hits)
    for name, args in result.tool_calls:
        blob = args.lower()
        if any(marker in blob for marker in EXTRA_NETWORK):
            hit = f"{name}: {args[:160]}"
            if hit not in seen:
                hits.append(hit)
                seen.add(hit)
    return hits


# File bodies are not paths: an Edit's `new_string` with `import "../x"` is not
# a climb out of the snapshot.
BODY_KEYS = {"content", "new_string", "old_string", "text", "new_source", "edits", "todos", "plan", "items"}


def scan_text(args: str) -> str:
    """The parts of one tool call's arguments that name paths or run commands."""
    try:
        doc = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return args
    if not isinstance(doc, dict):
        return args
    return "\n".join(str(v) for k, v in doc.items() if k not in BODY_KEYS and isinstance(v, (str, int, float)))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


SHELLS = {"sh", "bash", "dash", "zsh"}
SHELL_C = re.compile(r"^-[a-z]*c$")
ASSIGN = re.compile(r"^-*([A-Za-z_][\w.-]*)=(.*)$", re.S)        # NAME=value, key=value, --opt=value
VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")
TILDE_USER = re.compile(r"^~([a-z_][\w.-]*)(?=/|$)")
PROC_ROOT = re.compile(r"^/proc/(?:self|thread-self|\d+)/root(?=/|$)")
GLOB = re.compile(r"[*?\[{$]")
QUOTED = re.compile(r"""(['"])([^'"\s]+?)\1""")


def _shell_tokens(text: str) -> list[str]:
    import shlex
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        return list(lex)
    except ValueError:                       # unbalanced quotes: fall back to whitespace
        return text.split()


def literal(word: str, env: dict) -> str:
    """What a word names once its known variables, `~`, `~user`, `file://` and
    `/proc/<n>/root` are resolved, cut at its first glob or unknown variable
    (`/home/u/projects/*` names `/home/u/projects/`)."""
    w = word
    if w.startswith("file://"):
        rest = urllib.parse.unquote(w[len("file://"):])
        w = rest if rest.startswith("/") else ("/" + rest.split("/", 1)[1] if "/" in rest else "")
    w = VAR.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), w)
    if w == "~" or w.startswith("~/"):
        w = env["HOME"] + w[1:]
    else:
        m = TILDE_USER.match(w)
        if m:
            try:
                w = pwd.getpwnam(m.group(1)).pw_dir + w[m.end():]
            except KeyError:
                pass
    cut = GLOB.search(w)
    if cut:
        w = w[:cut.start()]
    while PROC_ROOT.match(w):
        w = PROC_ROOT.sub("", w, count=1) or "/"
    return w


def _is_candidate(lit: str) -> bool:
    return lit.startswith("/") or ".." in lit.split("/")


def path_candidates(text: str, env: dict | None = None, depth: int = 0) -> list[tuple[str, str]]:
    """(word, what it names) for the path arguments in one argument value:
    shell words (and the value of every NAME=value, key=value or --opt=value
    word, with simple assignments tracked so a later $NAME expands), the
    script of `sh -c`/`bash -lc` (tokenized again), and quoted strings inside
    inline code such as `python -c`. Prose in a quoted argument or a commit
    message is never split into words."""
    env = env if env is not None else {}
    out = []
    tokens = _shell_tokens(text)
    for i, tok in enumerate(tokens):
        if (depth < 3 and i >= 2 and SHELL_C.match(tokens[i - 1])
                and os.path.basename(tokens[i - 2]) in SHELLS):
            out += path_candidates(tok, env, depth + 1)
            continue
        words = [tok]
        m = ASSIGN.match(tok)
        if m:
            words = [m.group(2)]
            if re.fullmatch(r"[A-Za-z_]\w*", m.group(1)) and not tok.startswith("-"):
                env[m.group(1)] = literal(m.group(2), env) if not any(c.isspace() for c in m.group(2)) else m.group(2)
        for word in words:
            if any(c.isspace() for c in word):
                inner = [q.group(2) for q in QUOTED.finditer(word)]
            else:
                inner = [word]
                if not _is_candidate(literal(word, env)):
                    inner = [q.group(2) for q in QUOTED.finditer(word)]
            for w in inner:
                lit = literal(w, env)
                if lit and _is_candidate(lit):
                    out.append((w, lit))
    return out


def escape_flags(result, snap: Path, extra_allowed: tuple[str, ...] = ()) -> list[str]:
    """Path arguments in tool calls that name something outside the cell:

    - an absolute path is in-cell under the snapshot, the system/interpreter
      trees, the cell's /tmp (but not the lead's /tmp/claude-*), or, under the
      cell's HOME, only the trees that exist in the cell (its CLI's own state
      and the binds, `sandbox.cell_home_paths`): the cell's HOME is a tmpfs at
      the real home's path, so anything else there (`~/projects/Jarvis`,
      `~/.config/gh`) is an attempt on the host's copy;
    - an absolute path whose top-level directory does not exist on the host
      (`/v.slice` from a sed or grep pattern) names nothing, so it is not one;
    - a relative path whose `..` climbs out of the snapshot (taken relative to
      the snapshot root) is."""
    snap_s = os.path.normpath(str(snap))
    roots = [snap_s, os.path.normpath(os.path.realpath(snap)), *map(os.path.normpath, extra_allowed)]
    home = os.path.normpath(sandbox.sandbox_home())
    profile = {"claude": "claude", "codex": "codex", "jarvis": "jarvis"}.get(getattr(result, "harness", ""), "grade")
    in_home = [os.path.normpath(p) for p in sandbox.cell_home_paths(profile)]
    hits = []
    for name, args in result.tool_calls:
        for raw, lit in path_candidates(scan_text(args), {"HOME": home}):
            if not lit.startswith("/"):
                path = os.path.normpath(os.path.join(snap_s, lit))
                if not any(_within(path, r) for r in roots):
                    hits.append(f"{name}: {raw[:160]}")
                continue
            path = os.path.normpath(lit)
            if any(_within(path, r) for r in roots):
                continue
            if _within(path, home):
                if not any(_within(path, p) for p in in_home):
                    hits.append(f"{name}: {raw[:160]}")
                continue
            if path.startswith("/tmp/claude-"):
                hits.append(f"{name}: {raw[:160]}")
                continue
            if any(_within(path, a) for a in ALLOWED_ABS):
                continue
            top = "/" + path.lstrip("/").split("/", 1)[0]
            if top != "/" and not os.path.lexists(top):
                continue                         # names nothing on the host: a pattern, not a path
            hits.append(f"{name}: {raw[:160]}")
    return hits
