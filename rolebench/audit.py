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


def fields(args: str) -> list[tuple[str | None, str]]:
    """(key, value) for the parts of one tool call's arguments that name paths
    or run commands; a non-JSON argument (a codex shell command) is (None, it)."""
    try:
        doc = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return [(None, args)]
    if not isinstance(doc, dict):
        return [(None, args)]
    return [(k, str(v)) for k, v in doc.items() if k not in BODY_KEYS and isinstance(v, (str, int, float))]


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
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|<>()\n")
        lex.whitespace = " \t\r"                # a newline separates commands, like ;
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
    return _resolve(w)[0]


def _resolve(w: str) -> tuple[str, bool]:
    """(the literal prefix up to the first glob or unknown variable, whether it was cut)."""
    cut = GLOB.search(w)
    if cut:
        w = w[:cut.start()]
    while PROC_ROOT.match(w):
        w = PROC_ROOT.sub("", w, count=1) or "/"
    return w, bool(cut)


def literal_cut(word: str, env: dict) -> bool:
    """Whether `literal(word, env)` stopped at a glob or an unknown variable."""
    w = VAR.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), word)
    return bool(GLOB.search(w.replace("file://", "", 1)))


def _is_candidate(lit: str) -> bool:
    return lit.startswith("/") or ".." in lit.split("/")


PUNCT = set(";&|<>()\n")
SEPARATOR_KINDS = {"seq", "and", "or", "pipe", "bg"}


def _punct_kind(tok: str) -> str | None:
    """None for a word; else the kind of a punctuation token: seq (; or a
    newline), and (&&), or (||), pipe (| or |&), bg (&), redirect, paren."""
    if not tok or not set(tok) <= PUNCT:
        return None
    bare = tok.replace("(", "").replace(")", "")
    if not bare:
        return "paren"
    if ";" in bare or "\n" in bare:
        return "seq"
    if bare == "&&":
        return "and"
    if bare == "||":
        return "or"
    if bare in ("|", "|&"):
        return "pipe"
    if bare == "&":
        return "bg"
    return "redirect"


def _certain_dir(path: str | None, root: str | None) -> str | None:
    """`path` resolved, if it is an existing directory inside the snapshot; else None."""
    if not path or not root:
        return None
    real = os.path.realpath(path)
    return real if os.path.isdir(real) and _within(real, root) else None


def path_candidates(text: str, env: dict | None = None, depth: int = 0, state: dict | None = None,
                    track_cd: bool = False) -> list[tuple[str, str, str | None]]:
    """(word, what it names, the shell's cwd there) for the path arguments in
    one argument value: shell words (and the value of every NAME=value,
    key=value or --opt=value word, with simple assignments tracked so a later
    $NAME expands), the script of `sh -c`/`bash -lc` (tokenized again), and
    quoted strings inside inline code such as `python -c`. Prose in a quoted
    argument or a commit message is never split into words.

    The cwd (`state["cwd"]`, None = unknown, judged from the snapshot root) is
    moved only by a `cd` it is certain of (`track_cd`: a `command` field): a
    `cd` in command position at the start, after `;` or a newline, or after
    `&&` when every command before it in that chain was such a `cd`; never in
    `( … )`, a pipeline, `&` or `||`, never with an option, and only to an
    existing directory inside the snapshot (resolved, so a symlink out is
    caught). A cd to a path that does not exist leaves a known cwd where it
    was, as the shell does (were it deleted after the run, the cwd judged
    from is only shallower, which flags more); anything else makes it
    unknown. A `-c` script works on a copy and never moves the parent's cwd;
    `pushd`/`popd` anywhere make it unknown."""
    env = env if env is not None else {}
    state = state if state is not None else {"cwd": None, "root": None}
    out = []
    tokens = _shell_tokens(text)
    paren, last_sep, chain_ok, cmd_start = 0, "seq", True, True
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        kind = _punct_kind(tok)
        if kind is not None:
            paren += tok.count("(") - tok.count(")")
            if kind in SEPARATOR_KINDS:
                last_sep, cmd_start = kind, True
                if kind == "seq":
                    chain_ok = True
                elif kind != "and":
                    chain_ok = False
            elif kind == "paren" and "(" in tok:
                cmd_start, chain_ok = True, False
            i += 1
            continue
        starting, cmd_start = cmd_start, False
        if (depth < 3 and i >= 2 and SHELL_C.match(tokens[i - 1])
                and os.path.basename(tokens[i - 2]) in SHELLS):
            out += path_candidates(tok, env, depth + 1, dict(state), track_cd)      # a copy: never the parent's cwd
            i += 1
            continue
        if track_cd and tok in ("pushd", "popd"):
            state["cwd"] = None
        if track_cd and tok == "cd" and starting:
            j, args = i + 1, []
            while j < len(tokens) and _punct_kind(tokens[j]) not in SEPARATOR_KINDS:
                if _punct_kind(tokens[j]) == "redirect":
                    j += 2                              # the redirect and its target
                    continue
                if _punct_kind(tokens[j]) is None:
                    args.append(tokens[j])
                j += 1
            nxt = _punct_kind(tokens[j]) if j < len(tokens) else None
            certain = (paren == 0 and nxt not in ("pipe", "bg", "or")
                       and (last_sep == "seq" or (last_sep == "and" and chain_ok)))
            before = state.get("cwd")
            target = args[0] if args else "~"
            lit = literal(target, env)
            for arg in args:                            # every operand is judged (`cd -P ..`, `cd -- ..`)
                alit = literal(arg, env)
                if alit and _is_candidate(alit):
                    out.append((arg, alit, before))
            if len(args) > 1 or target.startswith("-") or literal_cut(target, env) or not lit:
                new = None
            else:
                base = before or state.get("root")
                dest = lit if lit.startswith("/") else (os.path.join(base, lit) if base else None)
                if dest and before and not os.path.lexists(dest):
                    new = before                        # nothing there: the cd failed and the cwd stayed
                else:
                    new = _certain_dir(dest, state.get("root"))
            if certain:
                state["cwd"] = new
            elif nxt not in ("pipe", "bg") and paren == 0:
                state["cwd"] = None                     # may or may not have run: unknown
            if not certain:
                chain_ok = False
            i = j
            continue
        if starting:
            chain_ok = False                            # a command other than an accepted cd
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
                    out.append((w, lit, state.get("cwd")))
        i += 1
    return out


def claude_task_dir(snap: Path | str) -> str:
    """Where the Claude CLI keeps its own task output for a session in `snap`:
    /tmp/claude-<uid>/<cwd with every non-alphanumeric as '-'>. In a cell
    that is the cell's own tmpfs /tmp."""
    return f"/tmp/claude-{os.getuid()}/" + re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(str(snap)))


def escape_flags(result, snap: Path, extra_allowed: tuple[str, ...] = ()) -> list[str]:
    """Path arguments in tool calls that name something outside the cell:

    - every path is resolved first: a relative one against the shell's cwd
      as `cd` moved it (from the snapshot root; Claude's Bash keeps its cwd
      from one call to the next, the other harnesses start each call there);
    - an absolute path is in-cell under the snapshot, the system/interpreter
      trees, the cell's /tmp (but not the lead's /tmp/claude-*: only a claude
      cell's own task directory there, `claude_task_dir`), or, under the
      cell's HOME, only the trees that exist in the cell (its CLI's own state
      and the binds, `sandbox.cell_home_paths`): the cell's HOME is a tmpfs at
      the real home's path, so anything else there (`~/projects/Jarvis`,
      `~/.config/gh`) is an attempt on the host's copy;
    - an absolute path whose top-level directory does not exist on the host
      (`/v.slice` from a sed or grep pattern) names nothing, so it is not one."""
    snap_s = os.path.normpath(str(snap))
    roots = [snap_s, os.path.normpath(os.path.realpath(snap)), *map(os.path.normpath, extra_allowed)]
    home = os.path.normpath(sandbox.sandbox_home())
    harness = getattr(result, "harness", "")
    profile = {"claude": "claude", "codex": "codex", "jarvis": "jarvis"}.get(harness, "grade")
    in_home = [os.path.normpath(p) for p in sandbox.cell_home_paths(profile)]
    own_tmp = claude_task_dir(snap_s) if harness == "claude" else None
    hits = []
    root = os.path.realpath(snap_s)
    state = {"cwd": None, "root": root}
    for name, args in result.tool_calls:
        if harness != "claude" or not (state.get("cwd") and _within(state["cwd"], root)):
            # codex and jarvis start every command at the snapshot; Claude keeps
            # its cwd only while it is certain and inside the snapshot
            state = {"cwd": None, "root": root}
        env = {"HOME": home}
        for key, value in fields(args):
            for raw, lit, cwd in path_candidates(value, env, state=state, track_cd=key in ("command", None)):
                hit = _judge(lit, cwd or snap_s, roots, home, in_home, own_tmp)
                if hit:
                    hits.append(f"{name}: {raw[:160]}")
    return hits


def _judge(lit: str, cwd: str, roots: list[str], home: str, in_home: list[str], own_tmp: str | None) -> bool:
    """Whether a path a tool call names is outside the cell."""
    path = os.path.normpath(lit if lit.startswith("/") else os.path.join(cwd, lit))
    if any(_within(path, r) for r in roots):
        return False
    if _within(path, home):
        return not any(_within(path, p) for p in in_home)
    if path.startswith("/tmp/claude-"):
        return not (own_tmp and _within(path, own_tmp))
    if any(_within(path, a) for a in ALLOWED_ABS):
        return False
    top = "/" + path.lstrip("/").split("/", 1)[0]
    return not (top != "/" and not os.path.lexists(top))     # a top that is not on the host names nothing
