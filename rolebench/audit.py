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


PUNCT = set(";&|<>()")
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


def path_candidates(text: str, env: dict | None = None, depth: int = 0) -> list[tuple[str, str, int | None]]:
    """(word, what it names, its word ordinal) for the path arguments in one
    argument value: shell words (and the value of every NAME=value, key=value
    or --opt=value word, with simple assignments tracked so a later $NAME
    expands), the script of `sh -c`/`bash -lc` (tokenized again), and quoted
    strings inside inline code such as `python -c`. Prose in a quoted argument
    or a commit message is never split into words.

    The candidates are main's (c717ed9), word for word; the ordinal (the
    word's place among the words of `text`, None inside a `-c` script) only
    lets `escape_details` attach an advisory label."""
    env = env if env is not None else {}
    out = []
    tokens = _shell_tokens(text)
    # broad on purpose (a detector): any shell word, command position or not, as main read it
    scripts = (shell_scripts(tokens, command_only=False)
               | {i for i in range(2, len(tokens)) if SHELL_C.match(tokens[i - 1])
                  and os.path.basename(tokens[i - 2]) in SHELLS}) if depth < 3 else set()
    ordinal = -1
    for i, tok in enumerate(tokens):
        if not set(tok) <= PUNCT:
            ordinal += 1
        if i in scripts:
            out += [(w, lit, None) for w, lit, _ in path_candidates(tok, env, depth + 1)]
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
                    out.append((w, lit, ordinal if depth == 0 else None))
    return out


# Wrappers that run the next word as the command (`env X=1 bash -c ...`).
WRAPPER_WORDS = {"env", "exec", "command", "builtin", "nohup", "time", "nice", "sudo", "stdbuf", "timeout", "xargs"}
# Shell options that take the next word as their argument (`-o pipefail`, `-O extglob`).
SHELL_ARG_OPT = re.compile(r"^[-+][A-Za-z]*[oO]$|^--(?:rcfile|init-file)$")


def command_words(tokens: list[str]) -> set[int]:
    """The indices of the words in command position: the first word of each
    command (after `;`, `&&`, `|`, ...), past NAME=value assignments, and the
    word a wrapper (`env`, `sudo`, `timeout 30`, ...) runs."""
    out, start, i = set(), True, 0
    punct = set(AWARE_PUNCT) | PUNCT
    while i < len(tokens):
        t = tokens[i]
        if set(t) <= punct:
            start, i = True, i + 1
            continue
        if start:
            if re.fullmatch(r"[A-Za-z_]\w*=.*", t, re.S):
                i += 1
                continue
            out.add(i)
            if os.path.basename(t) in WRAPPER_WORDS:
                # a wrapper's options may take arguments (`sudo -u root`, `nice -n 5`, `timeout -s KILL 5`,
                # `env -u FOO`, `stdbuf -o L`): any later word of this simple command may be what it runs,
                # except under `command -v` / `-V`, which only looks the name up
                j = i + 1
                if os.path.basename(t) == "command" and j < len(tokens) and tokens[j] in ("-v", "-V"):
                    start, i = False, j + 1
                    continue
                while j < len(tokens) and not set(tokens[j]) <= punct:
                    out.add(j)
                    j += 1
                start, i = False, j
                continue
            start = False
        i += 1
    return out


def shell_scripts(tokens: list[str], command_only: bool = True) -> set[int]:
    """The indices of the scripts that `sh`/`bash`/... run with `-c`, whatever
    the options' order (`bash -lc S`, `bash --login -c S`, `bash -c -l S`,
    `bash -o pipefail -c S`): the first word after the options, once one of
    them carries `c`, an option's own argument (`-o pipefail`) skipped. With
    `command_only` the shell must be in command position (`echo bash -c x`
    runs no shell)."""
    out = set()
    commands = command_words(tokens) if command_only else None
    punct = set(AWARE_PUNCT) | PUNCT
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) not in SHELLS or (commands is not None and i not in commands):
            continue
        has_c, j = False, i + 1
        while j < len(tokens):
            t = tokens[j]
            if set(t) <= punct:
                break
            if (t.startswith("-") or t.startswith("+")) and len(t) > 1:
                has_c = has_c or bool(SHELL_C.match(t)) or (t.startswith("-") and not t.startswith("--") and "c" in t[1:])
                j += 2 if SHELL_ARG_OPT.match(t) else 1
                continue
            if has_c:
                out.add(j)
            break
    return out


def bare_cds(command: str, depth: int = 0) -> int:
    """How many bare `cd`s (no operand: it goes HOME) a shell command runs, in
    command position and followed by an operator or the end, counting those
    in the script of `sh -c`/`bash -lc '...'` (the shape every codex command
    arrives in). Only a command is read, never a pattern or a description."""
    tokens = _aware_tokens(command)
    scripts = shell_scripts(tokens) if depth < 3 else set()
    splits = env_split_strings(tokens) if depth < 3 else set()
    n = 0
    for i, tok in enumerate(tokens):
        if tok == "cd" and (i == 0 or set(tokens[i - 1]) <= set(AWARE_PUNCT)) \
                and (i + 1 == len(tokens) or set(tokens[i + 1]) <= set(AWARE_PUNCT)):
            n += 1
        elif i in scripts:
            n += bare_cds(tok, depth + 1)
        elif i in splits:
            n += bare_cds(tok.split("=", 1)[1] if tok.startswith("--split-string=") else
                          tok[2:] if tok.startswith("-S") and len(tok) > 2 else tok, depth + 1)
    return n


def env_split_strings(tokens: list[str]) -> set[int]:
    """The indices of `env -S STRING` (`--split-string`) arguments: env splits
    STRING into a command line and runs it (`env -S 'bash -c cd'`)."""
    out = set()
    commands = command_words(tokens)
    punct = set(AWARE_PUNCT) | PUNCT
    for i in sorted(commands):
        if os.path.basename(tokens[i]) != "env":
            continue
        for j in range(i + 1, len(tokens)):
            t = tokens[j]
            if set(t) <= punct:
                break
            if t in ("-S", "--split-string") and j + 1 < len(tokens):
                out.add(j + 1)
                break
            if (t.startswith("-S") and len(t) > 2) or t.startswith("--split-string="):
                out.add(j)
                break
    return out


# --- the advisory label: a lexical reading of cd ------------------------------------

AWARE_PUNCT = ";&|<>()\n"


def _aware_tokens(text: str) -> list[str]:
    import shlex
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=AWARE_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return text.split()


def lexical_cwds(text: str, start: str | None) -> tuple[list[str | None], str | None]:
    """(the lexical cwd at each word of `text`, the cwd after it), relative to
    the snapshot root ("" is the root, None is outside or unknown). A cd in
    command position moves it to its first non-option operand, no questions
    asked: this only labels a flag, it never removes one."""
    cwds, cwd, pending = [], start, None
    tokens = _aware_tokens(text)
    for i, tok in enumerate(tokens):
        if set(tok) <= set(AWARE_PUNCT):
            if pending is not None:
                cwd, pending = pending[0], None         # a cd takes effect after its operands
            continue
        cwds.append(cwd)
        if tok == "cd" and (i == 0 or set(tokens[i - 1]) <= set(AWARE_PUNCT)):
            args = []
            for t in tokens[i + 1:]:
                if set(t) <= set(AWARE_PUNCT):
                    break
                args.append(t)
            target = next((a for a in args if not a.startswith("-")), None)
            if target is None or target == "~" or target.startswith(("/", "~", "$")) or cwd is None:
                pending = (None,)
            else:
                rel = os.path.normpath(os.path.join(cwd, target))
                pending = (None if rel == ".." or rel.startswith("../") else ("" if rel == "." else rel),)
    if pending is not None:
        cwd = pending[0]
    return cwds, cwd


def claude_task_dir(snap: Path | str) -> str:
    """Where the Claude CLI keeps its own task output for a session in `snap`:
    /tmp/claude-<uid>/<cwd with every non-alphanumeric as '-'>. In a cell
    that is the cell's own tmpfs /tmp."""
    return f"/tmp/claude-{os.getuid()}/" + re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(str(snap)))


def claude_task_dirs(snap: Path | str) -> list[str]:
    """The task directory for the snapshot's path as given and as resolved
    (the cell chdirs to the resolved work tree)."""
    return sorted({claude_task_dir(snap), claude_task_dir(os.path.realpath(str(snap)))})


def escape_details(result, snap: Path, extra_allowed: tuple[str, ...] = ()) -> list[dict]:
    """Every path argument in a tool call that names something outside the
    cell, judged exactly as on main (c717ed9), so the flags are a superset of
    main's by construction (the one exception: a claude cell's own task
    output under /tmp/claude-<uid>/<its cwd slug>/, which is in-cell):

    - a relative path is judged from the snapshot root, whatever `cd` did;
    - an absolute path is in-cell under the snapshot, the system/interpreter
      trees, the cell's /tmp (not the lead's /tmp/claude-*), or, under the
      cell's HOME, only the trees that exist in the cell (its CLI's own state
      and the binds, `sandbox.cell_home_paths`); anything else there
      (`~/projects/Jarvis`, `~/.config/gh`, a bare `cd`) is an attempt;
    - an absolute path whose top-level directory does not exist on the host
      (`/v.slice` from a sed or grep pattern) names nothing, so it is not one.

    Each flag is {"flag", "probable_cd_relative", "assumed_cwd", "command"}.
    The label is a triage hint only: a relative path that a lexical reading of
    the preceding cds (Claude's Bash keeps its cwd between calls) would put
    inside the snapshot is marked. The reading is unsound (it is the inference
    two review rounds showed leaking), so a labelled flag still counts against
    the run until the lead clears it by hand (`rolebench clear-flags`)."""
    snap_s = os.path.normpath(str(snap))
    roots = [snap_s, os.path.normpath(os.path.realpath(snap)), *map(os.path.normpath, extra_allowed)]
    home = os.path.normpath(sandbox.sandbox_home())
    harness = getattr(result, "harness", "")
    profile = {"claude": "claude", "codex": "codex", "jarvis": "jarvis"}.get(harness, "grade")
    in_home = [os.path.normpath(p) for p in sandbox.cell_home_paths(profile)]
    own_tmp = claude_task_dirs(snap_s) if harness == "claude" else None
    out = []
    carried = ""                                  # Claude's lexical cwd across calls; the others start at the root
    for name, args in result.tool_calls:
        text = scan_text(args)
        command = _command_of(args)
        cwds, after = lexical_cwds(command, carried if harness == "claude" else "") if command else ([], None)
        start = len(_aware_word_offset(text, command)) if command else 0
        if harness == "claude" and command:
            # Claude resets its shell to the project when a cd leaves it: an
            # unknown or outside cwd starts the next call at the root
            carried = after if after is not None else ""
        candidates = path_candidates(text, {"HOME": home})
        candidates += [("cd", home, None)] * (bare_cds(command) if command else 0)   # a bare cd goes HOME
        for raw, lit, ordinal in candidates:
            if not _outside(lit, snap_s, roots, home, in_home, own_tmp):
                continue
            soft, assumed = False, None
            if not lit.startswith("/") and ordinal is not None and command:
                k = ordinal - start
                cwd = cwds[k] if 0 <= k < len(cwds) else None
                if cwd is not None and cwd != "":
                    inside = os.path.normpath(os.path.join(snap_s, cwd, lit))
                    if any(_within(inside, r) for r in roots):
                        soft, assumed = True, cwd
            out.append({"flag": f"{name}: {raw[:160]}", "probable_cd_relative": soft, "assumed_cwd": assumed,
                        "command": (command or text)[:300]})
    return out


def escape_flags(result, snap: Path, extra_allowed: tuple[str, ...] = ()) -> list[str]:
    """Every flag (hard and probable-cd-relative alike): a superset of main's."""
    return [d["flag"] for d in escape_details(result, snap, extra_allowed)]


def _command_of(args: str) -> str:
    """The shell command of a tool call (a `command` field, or a codex string), or ""."""
    try:
        doc = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return args if isinstance(args, str) else ""
    return str(doc.get("command") or "") if isinstance(doc, dict) else ""


def _aware_word_offset(text: str, command: str) -> list[str]:
    """The words of `text` (scan_text's join of the call's fields) before the
    command's own words, so a candidate's ordinal maps onto the command."""
    idx = text.find(command)
    if idx <= 0:
        return []
    return [t for t in _shell_tokens(text[:idx]) if not set(t) <= PUNCT]


def _outside(lit: str, snap_s: str, roots: list[str], home: str, in_home: list[str], own_tmp: list[str] | None) -> bool:
    if not lit.startswith("/"):
        path = os.path.normpath(os.path.join(snap_s, lit))
        return not any(_within(path, r) for r in roots)
    path = os.path.normpath(lit)
    if any(_within(path, r) for r in roots):
        return False
    if _within(path, home):
        return not any(_within(path, p) for p in in_home)
    if path.startswith("/tmp/claude-"):
        return not (own_tmp and any(_within(path, t) for t in own_tmp))
    if any(_within(path, a) for a in ALLOWED_ABS):
        return False
    top = "/" + path.lstrip("/").split("/", 1)[0]
    return not (top != "/" and not os.path.lexists(top))     # a top that is not on the host names nothing
