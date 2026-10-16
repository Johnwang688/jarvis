"""What only the owner may do to the shared repository (owner decision,
2026-10-15, after GIT-LIVE; `docs/plans/2026-10-15-pr-merge-close-decisions.md`).

Jarvis chats match Claude Code's auto mode: pushing a branch and opening a
pull request proceed without asking, but **merging or closing a pull request
always needs the owner's yes** — and so do the pushes that amount to a merge
or a close: a push to the default branch, a branch deletion, a force push.

`judge(command, cwd)` is the one reader. `permissions.build_permit` asks it at
layer 2, before any always-ask list, in every profile and for every provider:
a match is put to the owner (deny-all, strict-without-a-human or a timeout
declines it), never offered an Always, and never answered by the allowlist,
rules' ALLOW or the provider's own reviewer. It is code, not an entry in the
owner's editable always-ask file, so no file, project or brief can remove it.
A Codex call that is also a sandbox widening keeps its widening ask and
carries this headline in front of it.

What it reads, per program the line runs (`rules._Walk`: every segment, a
shell's `-c` line, `eval`, substitutions, `find -exec`, and every wrapper and
runner from rules' one table — `uv run`, `npx`, `env`, `xargs`, `bash -lc`):

* `gh pr merge` and `gh pr close`, any flags; a `gh` command that is neither
  built in nor the stock `co` alias (an alias or an extension: it may be
  either); `gh api` writing to a pull request, its merge endpoint, a repo's
  `merges`, a branch ref or a file's contents, or a GraphQL mutation that
  merges, closes or enables auto-merge — and a GraphQL query it cannot read;
  `gh repo sync` onto a remote repository (it updates that repo's branch).
* `git push` (or `git-push`, or a git alias read from the repository's and
  the owner's git config) that names main, master, `prod*`/`production*`/
  `release*` or a remote's HEAD branch as a destination, in any refspec
  spelling (`HEAD:main`, `x:refs/heads/main`, `+main`, a glob); that pushes
  every branch (`--all`, `--branches`, `--mirror`); that deletes (`--delete`,
  `-d`, `:branch`, `--prune`, `--mirror`); or that forces (`--force`, `-f`,
  `--force-with-lease`, `--force-if-includes`, `+refspec`), each long option
  also in git's own abbreviations. A push with no refspec pushes the current
  branch: that is read from the repository where the command runs (the
  shell's own folder, then each `cd`, `git -C`, `env -C`, `--directory` on
  the line) and asks only when it is a protected branch.
* Inline interpreter code (`python -c`, `node -e`, a here-document fed to
  one) that spells any of the above; a bare push there is a push of the
  current branch.
* An earlier program on the same line that moves HEAD (`git checkout main
  && … && git push`, `git branch -m`, `git symbolic-ref HEAD`, `gh pr
  checkout`) or writes git config: a later plain or HEAD push is judged by
  where HEAD went, or asks when that cannot be told.
* The raw-line backstop: where the line runs a program that may run text it
  is handed (a shell, `eval`, an interpreter, `ssh`, `curl`, a git exec
  setting, a program this reader does not know), any spelling of the
  operations anywhere on the line asks. A line of only git, gh and data
  programs is read by its structure alone, so a commit message or PR body
  that mentions a merge asks nothing.

Where it cannot tell — a branch, method, endpoint or verb that is an
expansion, a folder it cannot follow, a git config override on the line, a
line too complex to walk — it asks, saying it cannot tell. It is still an
accident guard, not an adversarial boundary: a script file, a program it
does not know and the network itself are not read.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import os
from pathlib import Path
import posixpath
import re
import urllib.parse

from jarvis import rules

# --- rule ids and the plain words the card leads with -----------------------

PR_MERGE = "gh-pr-merge"
PR_CLOSE = "gh-pr-close"
PUSH_PROTECTED = "git-push-protected"   # the same ids the shipped list uses
PUSH_DELETE = "git-push-delete"
PUSH_FORCE = "git-push-force"
UNSURE = "pr-merge-unsure"
PUSH_UNSURE = "git-push-unsure"

HEADLINES = {
    PR_MERGE: "merges a pull request",
    PR_CLOSE: "closes a pull request",
    PUSH_PROTECTED: "pushes to main",
    PUSH_DELETE: "deletes a branch",
    PUSH_FORCE: "force-pushes",
    UNSURE: "may merge or close a pull request (it cannot be told from the command)",
    PUSH_UNSURE: "may push to main (what it pushes cannot be told from the command)",
}
REASON = ("the owner merges and closes pull requests (owner decision 2026-10-15): "
          "a merge, a close, a push to the default branch, a branch deletion or a "
          "force push is asked every time, with no Always")

# Branches a push may not reach unasked, beside each remote's HEAD branch.
PROTECTED_BRANCHES = ("main", "master", "prod*", "production*", "release*")

_GH_PR_VERBS = frozenset({
    "checkout", "checks", "close", "comment", "create", "diff", "edit", "list", "ls",
    "lock", "merge", "new", "ready", "reopen", "revert", "review", "status", "unlock",
    "update-branch", "view",
})
_GH_BUILTINS = frozenset({
    "accessibility", "actions", "agent-task", "alias", "api", "attestation", "auth",
    "browse", "cache", "co", "codespace", "completion", "config", "copilot",
    "environment", "exit-codes", "extension", "extensions", "ext", "formatting",
    "gist", "gpg-key", "help", "issue", "label", "mintty", "org", "pr", "preview",
    "project", "reference", "release", "repo", "ruleset", "run", "search", "secret",
    "ssh-key", "status", "variable", "version", "workflow",
})
# gh options that take the next word (the ones agents use; an unknown one is
# read both ways where it matters).
_GH_VALUE_OPTS = frozenset({"-R", "--repo", "--hostname"})
_GH_API_VALUES = {"-X": "method", "--method": "method", "-f": "field", "--raw-field": "field",
                  "-F": "field", "--field": "field", "--input": "input", "-H": "header",
                  "--header": "header", "-q": "jq", "--jq": "jq", "-t": "template",
                  "--template": "template", "--hostname": "host", "--cache": "cache",
                  "-p": "preview", "--preview": "preview"}

# git's own options before the subcommand that take the next word.
_GIT_GLOBAL_VALUES = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                                "--config-env", "--super-prefix", "--exec-path", "--list-cmds"})
_GIT_DIR_OPTS = frozenset({"--git-dir", "--work-tree"})
# git push options that take the next word when not written `--x=v`.
_PUSH_VALUES = frozenset({"repo", "push-option", "receive-pack", "exec", "recurse-submodules"})
# Common git subcommands that are never a push and never an alias (git does
# not let an alias shadow one), so no config is read for them.
_GIT_NOT_PUSH = frozenset({
    "add", "am", "apply", "bisect", "blame", "branch", "checkout", "cherry-pick", "clean",
    "clone", "commit", "config", "describe", "diff", "fetch", "format-patch", "grep",
    "init", "log", "ls-files", "ls-remote", "merge", "merge-base", "mv", "pull", "rebase",
    "reflog", "remote", "reset", "restore", "rev-list", "rev-parse", "revert", "rm",
    "shortlog", "show", "show-ref", "stash", "status", "switch", "symbolic-ref", "tag",
    "worktree", "help", "version", "cat-file", "for-each-ref", "notes", "submodule",
    "update-index", "hash-object", "var", "check-ignore", "count-objects", "gc", "fsck",
})
_DIR_OPTION = re.compile(r"^(--chdir|--directory|--dir|--cwd|--working-directory|--prefix|--cd)(=|$)")
_SHORT_DIR = ("-C", "-D")
_INTERPRETERS = re.compile(r"(python|pypy)[0-9.]*|node|nodejs|deno|bun|perl|ruby|php|osascript|pwsh|powershell")
_GIT_ENV = re.compile(r"(?<![A-Za-z0-9_])GIT_(DIR|WORK_TREE|COMMON_DIR|CONFIG[A-Z_]*|NAMESPACE)\s*=")
_CURRENT_BRANCH = re.compile(
    r"^\$\(\s*git\s+(branch\s+--show-current|rev-parse\s+--abbrev-ref\s+HEAD|"
    r"symbolic-ref\s+--short\s+(-q\s+)?HEAD)\s*\)$")
# Inline code (`python -c`, a here-document fed to node) read as text.
_TEXT_RULES = (
    (re.compile(r"\bpr\b\W{0,8}\bmerge\b", re.I), PR_MERGE),
    (re.compile(r"\bpr\b\W{0,8}\bclose\b", re.I), PR_CLOSE),
    (re.compile(r"mergepullrequest|enablepullrequestautomerge|enqueuepullrequest|"
                r"pulls/[^\s'\"/]+/merge", re.I), PR_MERGE),
    (re.compile(r"closepullrequest", re.I), PR_CLOSE),
)
_TEXT_PUSH = re.compile(r"\bpush\b", re.I)
_TEXT_GIT_PUSH = re.compile(r"\bgit\b\W{1,6}push\b", re.I)
# Programs that only print, test or file data: never a runner of text.
_DATA_STEMS = frozenset({
    "cat", "echo", "printf", "true", "false", "test", "[", "[[", "grep", "egrep", "fgrep", "rg",
    "head", "tail", "wc", "sort", "uniq", "ls", "pwd", "date", "tee", "mkdir", "touch", "cd",
    "pushd", "popd", "export", "jq", "cut", "tr", "diff", "basename", "dirname", "realpath",
    "sleep", "which", "type", "read", "set", "unset", "local", "declare", ":", "wait", "cp", "mv",
})
# git settings and variables that make git run a program (review M-2 of #208).
_GIT_EXEC_KEYS = ("core.sshcommand", "core.pager", "core.editor", "core.fsmonitor",
                  "core.hookspath", "credential.helper", "diff.external", "sequence.editor",
                  "pager.", "alias.", "gpg.program", "uploadpack.", "receivepack.", "protocol.",
                  "remote.", "url.", "filter.", "merge.", "difftool.", "mergetool.")
_GIT_EXEC_ENV = re.compile(r"(?<![A-Za-z0-9_])GIT_(SSH|SSH_COMMAND|EDITOR|PAGER|EXTERNAL_DIFF|"
                           r"SEQUENCE_EDITOR|ASKPASS|PROXY_COMMAND|EXEC_PATH|CONFIG_PARAMETERS)\s*=")
_GIT_EXEC_SUBS = frozenset({"difftool", "mergetool", "filter-branch", "filter-repo", "bisect",
                            "rebase", "submodule", "daemon", "send-email"})
_TEXT_PUSH_WHAT = (
    (re.compile(r"\b(main|master)\b"), PUSH_PROTECTED),
    (re.compile(r"--force|(?<![\w-])-f\b|\+\w"), PUSH_FORCE),
    (re.compile(r"--delete|--mirror|--prune|(?<![\w-])-d\b"), PUSH_DELETE),
)

CONFIG_CAP = 65_536     # bytes of one git config file read for aliases and push refspecs
DIRS_CAP = 16           # candidate folders one line may run git in
ALIAS_DEPTH = 3         # git aliases followed inside each other
REMOTES_CAP = 32        # remotes whose HEAD branch is read


@dataclass(frozen=True)
class Hit:
    """What a line does that only the owner may approve. `rule` is the first
    one found; `headline` names every one, in plain words, for the card."""
    rule: str
    headline: str
    reason: str
    rules: tuple = ()

    @property
    def label(self) -> str:
        """Every rule id the line matched, for the reason and the log."""
        return ", ".join(self.rules or (self.rule,))


class _Unknown:
    """A folder or a branch that cannot be told from here."""
    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()
_REPO_HEAD = _Unknown()      # "HEAD as the repository has it", in `_Line.heads`


# --- reading the repository, never running git --------------------------------


def _read(path: Path, cap: int = CONFIG_CAP) -> str | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(cap).decode("utf-8", "replace")
    except OSError:
        return None


_HEX_HEAD = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def _is_git_dir(gitdir: Path) -> bool:
    """git's own test (`is_git_directory`): a HEAD, and `objects/` and
    `refs/` here or in the common dir a linked worktree's `commondir` names."""
    try:
        if not os.path.lexists(gitdir / "HEAD"):    # a symlinked HEAD may name an unborn ref
            return False
        common = gitdir
        pointer = _read(gitdir / "commondir", 4096)
        if pointer and pointer.strip():
            common = (gitdir / pointer.strip()).resolve()
        return (common / "objects").is_dir() and (common / "refs").is_dir()
    except OSError:
        return False


def _git_dirs(start: Path):
    """(git dir, common dir) of the repository holding `start`; None when it
    is in none (a push there fails); UNKNOWN when what is there cannot be
    read as git would read it (a `.git` that is not a git directory, a
    gitdir file pointing nowhere valid)."""
    folder = start
    for _ in range(256):
        dotgit = folder / ".git"
        try:
            if dotgit.is_dir():
                gitdir = dotgit
            elif dotgit.is_file():
                text = _read(dotgit, 4096) or ""
                match = re.match(r"\s*gitdir:\s*(.+?)\s*$", text)
                if not match:
                    return UNKNOWN
                gitdir = (folder / match.group(1)).resolve()
            else:
                if folder.parent == folder:
                    return None
                folder = folder.parent
                continue
        except OSError:
            return UNKNOWN
        if not _is_git_dir(gitdir):
            return UNKNOWN
        common = gitdir
        pointer = _read(gitdir / "commondir", 4096)
        if pointer and pointer.strip():
            common = (gitdir / pointer.strip()).resolve()
        return gitdir, common
    return UNKNOWN


def _head_branch(gitdir: Path):
    """The branch HEAD names (a `ref:` line, or a symlink as old gits wrote
    it), None when detached (a hex object name), UNKNOWN otherwise."""
    head = gitdir / "HEAD"
    try:
        if head.is_symlink():
            target = os.readlink(head)
            match = re.fullmatch(r"(?:.*/)?refs/heads/(\S+)", target)
            return match.group(1) if match else UNKNOWN
    except OSError:
        return UNKNOWN
    text = _read(head, 4096)
    if text is None:
        return UNKNOWN
    match = re.fullmatch(r"ref:\s*refs/heads/(\S+)\s*", text)
    if match:
        return match.group(1)
    if _HEX_HEAD.fullmatch(text.strip()):
        return None
    return UNKNOWN


def _remote_heads(common: Path) -> set[str]:
    """Each remote's HEAD branch (`refs/remotes/<r>/HEAD`), the default branch."""
    out: set[str] = set()
    try:
        remotes = sorted((common / "refs" / "remotes").iterdir())[:REMOTES_CAP]
    except OSError:
        return out
    for remote in remotes:
        text = _read(remote / "HEAD", 4096)
        if text:
            match = re.match(r"\s*ref:\s*refs/remotes/[^/\s]+/(\S+)\s*$", text)
            if match:
                out.add(match.group(1))
    return out


def _config_files(dirs) -> list[Path]:
    """In git's order, lowest precedence first: system, XDG, global, then
    each repository's own config and its worktree config. Last wins."""
    home = Path(os.environ.get("HOME") or "~").expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    files = [Path("/etc/gitconfig"), Path(xdg) / "git" / "config", home / ".gitconfig"]
    if isinstance(dirs, list):
        for gitdir, common in dirs:
            files += [common / "config", gitdir / "config.worktree"]
    return files


def _strip_comment(text: str) -> str:
    """A value with its `#`/`;` comment cut, outside double quotes."""
    out, quoted, i = [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch in "#;" and not quoted:
            break
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _config_entries(files: list[Path], cache: dict | None = None) -> list[tuple[str, str, str]]:
    """(section, subsection, key=value) triples in precedence order (read the
    last match), best effort: git's own syntax without includes. Sections
    and keys are lowercased; comments are cut. Each file is parsed once per
    judgement (`cache`)."""
    out: list[tuple[str, str, str]] = []
    for path in files:
        if cache is not None and path in cache:
            out += cache[path]
            continue
        entries: list[tuple[str, str, str]] = []
        text = _read(path)
        section, sub = "", ""
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            header = re.match(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]', line)
            if header:
                name = header.group(1)
                if header.group(2) is not None:
                    section, sub = name.lower(), header.group(2)
                else:
                    section, _, sub = name.partition(".")
                    section = section.lower()
                line = _strip_comment(line[header.end():])
                if not line:
                    continue
            key, eq, value = line.partition("=")
            value = _strip_comment(value) if eq else "true"
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            entries.append((section, sub, f"{key.strip().lower()}={value}"))
        if cache is not None:
            cache[path] = entries
        out += entries
    return out


def _last(entries, section: str, key: str, sub: str | None = None) -> str | None:
    """The value git uses: the last one."""
    found = None
    for sec, subsection, pair in entries:
        name, _, value = pair.partition("=")
        if sec == section and name == key and (sub is None or subsection == sub):
            found = value
    return found


# --- the line ---------------------------------------------------------------


class _Line:
    def __init__(self, command: str, cwd):
        self.command = command
        self.hits: list[str] = []
        self.branches: list[str] = []         # protected branches a push names
        self.reasons: list[str] = []
        base = None
        if isinstance(cwd, (str, os.PathLike)) and str(cwd).strip():
            base = Path(cwd).expanduser()
            if not base.is_absolute():
                base = None
        self.dirs = [base] if base is not None else UNKNOWN
        if _GIT_ENV.search(command):
            self.dirs = UNKNOWN
        # The walk sees every `$(…)` emptied to `$(_)`; a refspec that is one
        # counts as the current branch only when every substitution on the
        # line reads the current branch (`$(git branch --show-current)`).
        try:
            subs = rules._substitution_texts(command)
        except Exception:       # noqa: BLE001 — unreadable: never the current branch
            subs = []
        self.subs_current = bool(subs) and all(
            _CURRENT_BRANCH.match("$(" + " ".join(str(inner).split()) + ")") for inner in subs)
        # An unquoted-looking `(` that is not `$(`, `<(` or `>(`: a subshell
        # or a group whose `cd` may not outlast it.
        self.subshell = bool(re.search(r"(?<![$<>])\(", command))
        # What an earlier program on the line did to the push that follows:
        # `heads` None = HEAD as the repository has it; else the branches it
        # may be (`_REPO_HEAD` among them when the move may not have
        # happened), or UNKNOWN. `config_changed`: a `git config` write.
        self.heads = None
        self.config_changed = False
        # A `;`, `||` or newline lets the line go on after a failed switch.
        self.lenient = bool(re.search(r";|\|\||\n", command))
        self._config_cache: dict = {}
        self.executor = bool(_GIT_EXEC_ENV.search(command))

    def config(self, repos) -> list:
        """The git config entries for these repositories, each file parsed
        once per judgement."""
        return _config_entries(_config_files(repos), self._config_cache)

    def switch(self, names) -> None:
        """An earlier program moves HEAD: to `names` (literal branches) or
        somewhere that cannot be told (UNKNOWN)."""
        if names is UNKNOWN or self.heads is UNKNOWN:
            self.heads = UNKNOWN
            return
        before = self.heads if self.heads is not None else [_REPO_HEAD]
        kept = before if (self.lenient or self.subshell) else []
        self.heads = list(dict.fromkeys(kept + list(names)))

    # what was found
    def hit(self, rule: str, why: str = "", branch: str | None = None) -> None:
        if rule not in self.hits:
            self.hits.append(rule)
        if branch and branch not in self.branches:
            self.branches.append(branch)
        if why and why not in self.reasons:
            self.reasons.append(why)

    # where it runs
    def move(self, target, *, home_if_empty: bool = False, union: bool = False) -> None:
        """A `cd TARGET` (or `env -C`, `--directory`) on the line. A `cd` into
        folders that exist, on a line with no subshell, is where what follows
        runs (`cd wt && git push` is judged in `wt`, not the shell's folder).
        Otherwise — a subshell whose `cd` ends with it, a folder that is not
        there (the `cd` fails and the line may go on), or a wrapper's folder
        option, which holds only for the program it starts — every folder
        the line could be in stays a candidate."""
        if self.dirs is UNKNOWN:
            return
        if target is None and home_if_empty:
            target = os.environ.get("HOME") or None
        if target is None or _wild(target) or str(target) in ("", "-"):
            self.dirs = UNKNOWN
            return
        text = str(target)
        if text.startswith("~"):
            text = str(Path(text).expanduser())
        moved = list(dict.fromkeys(
            Path(os.path.normpath(Path(text) if Path(text).is_absolute() else d / text))
            for d in self.dirs))
        if union or self.subshell or not all(folder.is_dir() for folder in moved):
            moved = list(dict.fromkeys(self.dirs + moved))
        self.dirs = moved if len(moved) <= DIRS_CAP else UNKNOWN

    def repos(self, dirs=None):
        """[(git dir, common dir)] for `dirs` (the line's), or UNKNOWN."""
        dirs = self.dirs if dirs is None else dirs
        if dirs is UNKNOWN:
            return UNKNOWN
        out = []
        for folder in dirs:
            found = _git_dirs(Path(os.path.normpath(folder)))
            if found is UNKNOWN:
                return UNKNOWN
            if found is not None and found not in out:
                out.append(found)
        return out


def _wild(word, replace: str | None = None) -> bool:
    """Whether bash (or xargs/find) would change this word before the
    program saw it: an active expansion or glob, a brace expansion (`{a,b}`,
    `{a..b}`: `git push origin {main,}` pushes main), or the replace string.
    A brace with neither (`{owner}` in a `gh api` path) is literal."""
    text = str(word)
    if replace and replace in text:
        return True
    mask = getattr(word, "active", None)
    if mask is None:
        mask = (True,) * len(text)
    if any(on and c in "$`*?[" for c, on in zip(text, mask)):
        return True
    return _brace_expands(text, mask)


def _brace_expands(text: str, mask) -> bool:
    """An active `{…}` holding a `,` or a `..` range at its own level. One
    pass, a stack of open braces: linear in the word."""
    stack: list[bool] = []
    for i, (ch, on) in enumerate(zip(text, mask)):
        if not on:
            continue
        if ch == "{":
            stack.append(False)
        elif ch == "}" and stack:
            if stack.pop():
                return True
        elif stack and (ch == "," or (ch == "." and i and text[i - 1] == "." and mask[i - 1])):
            stack[-1] = True
    return False


def _stem_could_be(word, names: tuple[str, ...]) -> tuple[str | None, bool]:
    """(the program's stem when it is literal — lowercased, `.exe` dropped —
    and whether a non-literal one could be any of `names`)."""
    text = str(word)
    mask = getattr(word, "active", None) or (True,) * len(text)
    start = text.rfind("/") + 1
    if not any(on and c in "$`*?[{" for c, on in zip(text[start:], mask[start:])):
        stem = text[start:].lower()
        return (stem[:-4] if stem.endswith(".exe") else stem), False
    could = False
    for posix in (True, False):
        atoms, kinds = rules._word_atoms(word, posix)
        cut = max((k for k, kind in enumerate(kinds) if kind == "sep"), default=-1)
        atoms, kinds = atoms[cut + 1:], kinds[cut + 1:]
        if not atoms:
            continue
        if "exp" in kinds:
            last = max(k for k, kind in enumerate(kinds) if kind == "exp")
            atoms = [rules._STAR] + atoms[last + 1:]
        could = could or any(rules._atoms_match(atoms, n) for n in names)
    return None, could


def _is_long(name: str, full: str) -> bool:
    """`--name` as git reads it: the option itself or an unambiguous-looking
    abbreviation of it (an ambiguous one fails in git; counted here)."""
    return bool(name) and full.startswith(name)


def _protected(name: str, defaults: set[str]) -> bool:
    lowered = name.lower()
    return lowered in {d.lower() for d in defaults} or any(
        fnmatch(lowered, pattern) for pattern in PROTECTED_BRANCHES)


def _branch_name(ref: str) -> str | None:
    """The branch a destination ref names, or None when it names no branch."""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    if ref.startswith("refs/"):
        return None             # a tag, a note, a remote-tracking ref: not a branch
    return ref


# --- git -------------------------------------------------------------------


def _git(line: _Line, args: list, prog, depth: int = 0, *, sure: bool = True) -> None:
    """`git ARGS` — find the subcommand past git's own options. `sure` is
    False when the program word is only possibly git (`$GIT push …`): then
    only a literal `push` is read, no alias and no unknown verb."""
    dirs = line.dirs
    overrides: list[str] = []
    i = 0
    sub = None
    while i < len(args):
        token = args[i]
        text = str(token)
        if text in _GIT_GLOBAL_VALUES:
            value = args[i + 1] if i + 1 < len(args) else None
            if text == "-C":
                dirs = _moved(dirs, value)
            elif text == "-c" and value is not None:
                overrides.append(str(value))
                if _wild(value):
                    overrides.append("*")
                if str(value).lower().startswith(_GIT_EXEC_KEYS):
                    line.executor = True
            elif text in _GIT_DIR_OPTS:
                dirs = UNKNOWN
            i += 2
            continue
        if text.startswith("--") and "=" in text:
            name = text.split("=", 1)[0]
            if name in _GIT_DIR_OPTS:
                dirs = UNKNOWN
            elif name == "--config-env":
                overrides.append("*")
                line.executor = True
            i += 1
            continue
        if text.startswith("-"):
            i += 1
            continue
        sub = token
        break
    if sub is None:
        return
    rest = list(args[i + 1:])
    if _wild(sub, prog.replace):
        if sure:
            # `git $CMD …`: it may be a push.
            _push(line, rest, prog, dirs, overrides, unsure_verb=True)
        return
    name = str(sub)
    if name in _GIT_EXEC_SUBS:
        line.executor = True            # it may run a program it is handed
    if name == "push":
        _push(line, rest, prog, dirs, overrides)
        return
    if not sure or depth >= ALIAS_DEPTH:
        return
    if name in _GIT_MOVES_HEAD:
        _git_moves_head(line, name, rest, dirs, prog.replace)
    if name in _GIT_RUNS_SHELL:
        # Subcommands that hand a shell a line: `submodule foreach CMD`,
        # `rebase -x CMD`, `bisect run CMD`.
        _git_shell_text(line, name, rest, depth)
        return
    if name in _GIT_NOT_PUSH:
        return
    expansion = _git_alias(line, name, dirs, overrides)
    if expansion is UNKNOWN:
        _push(line, rest, prog, dirs, overrides, unsure_verb=True)
    elif expansion:
        if expansion.startswith("!"):
            # A shell alias: its text, with the arguments after it, is a line.
            _line_into(line, expansion[1:] + " " + " ".join(str(a) for a in rest), depth + 1)
        else:
            words = rules._shell_words(expansion)
            _git(line, words + rest, prog, depth + 1)


_GIT_RUNS_SHELL = frozenset({"submodule", "rebase", "bisect"})
_GIT_MOVES_HEAD = frozenset({"checkout", "switch", "branch", "symbolic-ref", "config"})
_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l",
                           "--get-color", "--get-colorbool"})


def _git_moves_head(line: _Line, name: str, rest: list, dirs, replace) -> None:
    """A git program earlier on the line that moves HEAD or rewrites config
    (H-1 of #208's review): what a later push with no refspec, or of HEAD,
    pushes is no longer the repository's current branch."""
    words = [str(w) for w in rest]
    wild = any(_wild(w, replace) for w in rest)
    if name == "config":
        # A read leaves config as it was; anything else may write it (an
        # editor too). Then a plain push, or an alias, cannot be told.
        positionals = [w for w in words if not w.startswith("-")]
        verb = positionals[:1]
        reads = any(w in _CONFIG_READS for w in words) or verb in (["get"], ["list"])
        writes = (wild or any(w in ("-e", "--edit") for w in words)
                  or any(w.startswith(("--unset", "--add", "--replace", "--rename", "--remove"))
                         for w in words)
                  or verb in (["set"], ["unset"], ["edit"], ["rename-section"], ["remove-section"])
                  or (not reads and len(positionals) >= 2))
        if writes:
            line.config_changed = True
        return
    if name == "symbolic-ref":
        positionals = [w for w in rest if not str(w).startswith("-")]
        if len(positionals) >= 2 and str(positionals[0]) == "HEAD":
            target = positionals[1]
            if _wild(target, replace):
                line.switch(UNKNOWN)
            else:
                branch = _branch_name(str(target))
                line.switch([branch] if branch else UNKNOWN)
        return
    if name == "branch":
        if any(w in ("-u", "--unset-upstream") or w.startswith(("--set-upstream", "--track"))
               for w in words):
            line.config_changed = True          # an upstream, for push.default=upstream
        renames = [w for w in words if w in ("-m", "-M", "-c", "-C", "--move", "--copy")
                   or re.fullmatch(r"-[a-zA-Z]*[mMcC][a-zA-Z]*", w)]
        if renames:
            positionals = [w for w in rest if not str(w).startswith("-")]
            if wild or not positionals:
                line.switch(UNKNOWN)
                return
            new = str(positionals[-1])
            moving = any(re.search(r"[mM]", w) or w == "--move" for w in renames)
            if moving:
                line.switch([new])
            elif _protected(new, set()):
                line.switch([_REPO_HEAD, new])  # a copy onto a protected name
        return
    # checkout / switch
    if name == "checkout" and "--" in words:
        return                                  # paths: HEAD stays
    new = None
    positionals: list = []
    detach = False
    k = 0
    while k < len(rest):
        word = words[k]
        if word in ("-b", "-B", "-c", "-C", "--orphan"):
            new = rest[k + 1] if k + 1 < len(rest) else None
            k += 2
            continue
        if word.startswith(("--orphan=", "--create=", "--force-create=")):
            new = word.split("=", 1)[1]
        elif word in ("-", "@{-1}") or word.startswith("@{-"):
            line.switch(UNKNOWN)
            return
        elif word in ("--detach", "-d"):
            detach = True
        elif not word.startswith("-"):
            positionals.append(rest[k])
        k += 1
    if new is not None:
        line.switch(UNKNOWN if _wild(new, replace) else [str(new)])
        return
    if detach or not positionals:
        if detach:
            line.switch([])                     # detached: a HEAD push fails
        return
    target = positionals[0]
    if _wild(target, replace):
        line.switch(UNKNOWN)
        return
    text = str(target)
    if name == "checkout":
        # `git checkout FILE` restores a file and leaves HEAD: a name that is
        # a path where the line runs is read as one; anything else as a branch.
        if len(positionals) > 1 or _is_path(line, dirs, text):
            return
    if "--track" in words or "-t" in words:
        text = text.split("/", 1)[1] if "/" in text else text
    line.switch([_branch_name(text) or text])


def _is_path(line: _Line, dirs, text: str) -> bool:
    if text in (".", "..") or text.startswith(("./", "../", "/")):
        return True
    if dirs is UNKNOWN or not isinstance(dirs, list):
        return False
    return any((Path(d) / text).exists() for d in dirs)


def _git_shell_text(line: _Line, name: str, rest: list, depth: int) -> None:
    words = [str(w) for w in rest]
    texts: list[str] = []
    if name == "submodule" and "foreach" in words:
        after = words[words.index("foreach") + 1:]
        after = [w for w in after if w not in ("--recursive", "-q", "--quiet")]
        texts.append(" ".join(after))
    elif name == "bisect" and words[:1] == ["run"]:
        texts.append(" ".join(words[1:]))
    elif name == "rebase":
        for k, word in enumerate(words):
            if word in ("-x", "--exec") and k + 1 < len(words):
                texts.append(words[k + 1])
            elif word.startswith("--exec="):
                texts.append(word.split("=", 1)[1])
            elif word.startswith("-x") and len(word) > 2:
                texts.append(word[2:])
    for text in texts:
        if text.strip():
            _line_into(line, text, depth + 1)


def _moved(dirs, value):
    if dirs is UNKNOWN or value is None or _wild(value):
        return UNKNOWN
    text = str(value)
    if text.startswith("~"):
        text = str(Path(text).expanduser())
    return [Path(text) if Path(text).is_absolute() else d / text for d in dirs]


def _git_alias(line: _Line, name: str, dirs, overrides: list[str]):
    """The expansion of `git NAME` when it is an alias, '' when it is none,
    UNKNOWN when the config it would come from cannot be read."""
    wanted = name.lower()
    for value in overrides:
        if value == "*":
            return UNKNOWN
        key, _, expansion = value.partition("=")
        if key.lower() == f"alias.{wanted}":
            return expansion
    # With the folder unknown, only the owner's own config is read: a
    # repository-local alias there is a known gap, not an ask on every
    # `cd "$X" && git ls-tree`.
    repos = line.repos(dirs)
    value = _last(line.config(repos), "alias", wanted)
    if value is not None:
        return value
    # An earlier `git config` on this line may have just made it one.
    return UNKNOWN if line.config_changed else ""


def _push(line: _Line, args: list, prog, dirs, overrides: list[str], *,
          unsure_verb: bool = False) -> None:
    """`git push ARGS`, as the destination(s) it writes."""
    replace = prog.replace
    force = delete = every = tags = unsure = False
    positionals: list = []
    i = 0
    while i < len(args):
        token = args[i]
        text = str(token)
        i += 1
        if _wild(token, replace) and text.startswith("-"):
            unsure = True
            continue
        if text == "--":
            positionals += args[i:]
            break
        if text.startswith("--"):
            name, eq, _value = text[2:].partition("=")
            if name.startswith("no-"):
                continue
            if any(_is_long(name, full) for full in ("force", "force-with-lease", "force-if-includes")):
                force = True
            if _is_long(name, "delete") or _is_long(name, "prune"):
                delete = True
            if _is_long(name, "mirror"):
                force = delete = every = True
            if _is_long(name, "all") or _is_long(name, "branches"):
                every = True
            if _is_long(name, "tags"):
                tags = True
            if not eq and any(full == name for full in _PUSH_VALUES):
                i += 1
            continue
        if text.startswith("-") and len(text) > 1:
            letters = text[1:]
            for k, letter in enumerate(letters):
                if letter == "f":
                    force = True
                elif letter == "d":
                    delete = True
                elif letter == "o":
                    if k == len(letters) - 1:
                        i += 1
                    break
            continue
        positionals.append(token)

    if force:
        line.hit(PUSH_FORCE, "a force push destroys commits that are already on the remote")
    if delete:
        line.hit(PUSH_DELETE, "a deleted branch takes its pull request's head with it")
    if any(v == "*" or v.lower().startswith(("remote.", "push.", "branch."))
           for v in overrides):
        line.hit(PUSH_UNSURE, "a git config override on the line changes what the push writes")
        return

    repos = line.repos(dirs)
    defaults: set[str] = set()
    if isinstance(repos, list):
        for _gitdir, common in repos:
            defaults |= _remote_heads(common)
    if every:
        line.hit(PUSH_PROTECTED, "it pushes every branch, the default branch included", "every branch")

    remote = positionals[0] if positionals else None
    refspecs = positionals[1:]
    # Input appended as arguments (`xargs git push origin`) may be any refspec.
    if unsure or unsure_verb or (prog.via_input and prog.replace is None) or (
            remote is not None and _wild(remote, replace) and not refspecs):
        line.hit(PUSH_UNSURE, "what it pushes cannot be told from the command")
        return

    skip = False
    for spec in refspecs:
        if skip:
            skip = False
            continue
        text = str(spec)
        if text == "tag":
            skip = True
            continue
        if _wild(spec, replace):
            if _CURRENT_BRANCH.match(text) or (text in ("$(_)", "`_`") and line.subs_current):
                _current(line, repos, defaults)
            else:
                line.hit(PUSH_UNSURE, "the branch it pushes cannot be told from the command")
            continue
        if text.startswith("+"):
            line.hit(PUSH_FORCE, "a `+` refspec forces the push")
            text = text[1:]
        src, colon, dst = text.partition(":")
        if colon and not src:
            line.hit(PUSH_DELETE, "a `:branch` refspec deletes the branch")
        if not colon:
            dst = src
        if dst in ("HEAD", "@") or (not colon and re.match(r"^(HEAD|@)([~^].*)?$", dst)):
            _current(line, repos, defaults)
            continue
        branch = _branch_name(dst)
        if branch is None:
            continue
        if "*" in branch:
            names = set(defaults) | {"main", "master"}
            matched = sorted(n for n in names if fnmatch(n, branch))
            if matched or any(fnmatch(p, branch) for p in PROTECTED_BRANCHES):
                line.hit(PUSH_PROTECTED, "a glob refspec reaches the default branch",
                         matched[0] if matched else branch)
            continue
        if _protected(branch, defaults):
            line.hit(PUSH_PROTECTED, "a push to the default branch lands without review", branch)

    if not refspecs and not every and not tags:
        # A push with no refspec: the config decides what it writes
        # (`remote.<r>.push`, `.mirror`, `push.default`), else the current
        # branch to its own name.
        if line.config_changed:
            line.hit(PUSH_UNSURE, "an earlier `git config` on the line changes what a plain push writes")
            return
        entries = line.config(repos) if repos is not UNKNOWN else []
        if any(section == "remote" and pair.partition("=")[0] in ("push", "mirror")
               for section, _sub, pair in entries):
            line.hit(PUSH_UNSURE, "the repository's config sets what a plain push writes")
            return
        mode = (_last(entries, "push", "default") or "simple").lower()
        if mode == "matching":
            line.hit(PUSH_PROTECTED, "push.default=matching pushes every branch the remote has",
                     "every matching branch")
            return
        _current(line, repos, defaults,
                 entries if mode in ("upstream", "tracking") else None)


def _current(line: _Line, repos, defaults: set[str], upstream_entries=None) -> None:
    """A push of the current branch: asked when it is a protected one (or,
    under push.default=upstream, when its upstream is). An earlier program
    on the line that moves HEAD (`git checkout main && … && git push`) sets
    which branch that is (`_Line.switch`)."""
    heads = line.heads
    if heads is UNKNOWN:
        line.hit(PUSH_UNSURE, "an earlier command on the line moves HEAD somewhere that cannot be told")
        return
    branches: list = []
    for name in (heads if heads is not None else [_REPO_HEAD]):
        if name is not _REPO_HEAD:
            branches.append(name)
            continue
        if repos is UNKNOWN:
            line.hit(PUSH_UNSURE, "the branch it pushes depends on a folder that cannot be told")
            return
        for gitdir, _common in repos:
            branch = _head_branch(gitdir)
            if branch is UNKNOWN:
                line.hit(PUSH_UNSURE, "the current branch cannot be read")
            elif branch:
                branches.append(branch)
    for branch in branches:
        if _protected(branch, defaults):
            line.hit(PUSH_PROTECTED, "the branch it pushes is the default branch", branch)
        if upstream_entries:
            merge = _last(upstream_entries, "branch", "merge", sub=branch)
            target = _branch_name(merge.strip()) if merge else None
            if target and _protected(target, defaults):
                line.hit(PUSH_PROTECTED, "push.default=upstream pushes to the branch's "
                         "upstream, the default branch", target)


# --- gh --------------------------------------------------------------------


def _gh(line: _Line, args: list, prog, *, sure: bool = True) -> None:
    """`gh ARGS`. `sure` is False when the program is only possibly gh: then
    an unknown or non-literal top command is not asked for."""
    replace = prog.replace
    positionals: list[tuple[int, object]] = []
    i = 0
    while i < len(args):
        text = str(args[i])
        if text in _GH_VALUE_OPTS:
            i += 2
            continue
        if not text.startswith("-") or _wild(args[i], replace):
            positionals.append((i, args[i]))
        i += 1
    if not positionals:
        return
    first_at, top = positionals[0]
    if _wild(top, replace):
        if sure:
            line.hit(UNSURE, "which gh command runs cannot be told from the command")
        return
    name = str(top)
    if name == "pr":
        verb = None
        for _at, word in positionals[1:]:
            if _wild(word, replace):
                line.hit(UNSURE, "which `gh pr` command runs cannot be told from the command")
                return
            if str(word) in _GH_PR_VERBS:
                verb = str(word)
                break
        if verb is None and prog.via_input:
            line.hit(UNSURE, "its `gh pr` command is read from input")
        elif verb == "merge":
            line.hit(PR_MERGE, "a merge lands the pull request on its base branch")
        elif verb == "close":
            line.hit(PR_CLOSE, "a close ends the pull request")
        elif verb == "checkout":
            line.switch(UNKNOWN)        # HEAD is now the PR's branch, by its name
        return
    if name == "co":
        line.switch(UNKNOWN)            # the stock alias for `gh pr checkout`
        return
    if name == "api":
        _gh_api(line, args[first_at + 1:], replace)
        return
    if name in ("extension", "extensions", "ext"):
        if any(str(w) == "exec" for _a, w in positionals[1:2]):
            line.executor = True
            line.hit(UNSURE, "`gh extension exec` runs an extension, which may merge or close")
        return
    if name == "repo":
        words = [str(w) for w in args[first_at + 1:]]
        if words[:1] == ["sync"]:
            # `gh repo sync [DEST] [-s SRC] [-b BRANCH]`: a DEST is a remote
            # repository whose branch it rewrites; without one it is local.
            k, dest = 1, None
            while k < len(words):
                if words[k] in ("-s", "--source", "-b", "--branch"):
                    k += 2
                    continue
                if not words[k].startswith("-") and dest is None:
                    dest = words[k]
                k += 1
            if dest is not None:
                line.hit(PUSH_PROTECTED, "`gh repo sync` onto a remote repository rewrites its branch",
                         "the synced repository's default branch")
        return
    if sure and name not in _GH_BUILTINS:
        line.hit(UNSURE, f"`gh {name[:40]}` is an alias or an extension, which may merge or close")


# GraphQL mutations, matched as lowercased substrings (linear; a regex with
# `[\s\S]*` on both sides was quadratic, review M-6 of #208).
_GRAPHQL_MERGES = ("mergepullrequest", "enablepullrequestautomerge", "mergebranch",
                   "enqueuepullrequest")
_GRAPHQL_BRANCH = re.compile(r'branchname\W{0,6}([\w./-]+)')


def _gh_api(line: _Line, args: list, replace: str | None) -> None:
    method = None
    fields: list = []
    has_input = False
    endpoint = None
    i = 0
    while i < len(args):
        token = args[i]
        text = str(token)
        i += 1
        kind = value = None
        if text in _GH_API_VALUES:
            kind = _GH_API_VALUES[text]
            value = args[i] if i < len(args) else ""
            i += 1
        elif text.startswith("--") and "=" in text:
            name, _, rest = text.partition("=")
            kind, value = _GH_API_VALUES.get(name), rest
        elif len(text) > 2 and text[:2] in ("-X", "-f", "-F", "-H", "-q", "-t", "-p"):
            kind, value = _GH_API_VALUES[text[:2]], text[2:]
        elif text.startswith("-"):
            continue
        elif endpoint is None:
            endpoint = token
            continue
        if kind == "method":
            method = value
        elif kind == "field":
            fields.append(value)
        elif kind == "input":
            has_input = True
    if endpoint is None:
        return
    if method is not None and _wild(method, replace):
        write = True
    else:
        verb = str(method).upper() if method is not None else (
            "POST" if fields or has_input else "GET")
        write = verb not in ("GET", "HEAD")
        method = verb
    if _wild(endpoint, replace):
        if write:
            line.hit(UNSURE, "`gh api` writes to an endpoint that cannot be told")
        return
    path = _api_path(str(endpoint))
    text = " ".join(str(f) for f in fields) + " " + " ".join(str(a) for a in args)
    low = text.lower()
    unreadable_fields = has_input or any(
        _wild(f, replace) or "=@" in str(f) for f in fields)
    if path == "graphql":
        named = False
        if any(m in low for m in _GRAPHQL_MERGES):
            line.hit(PR_MERGE, "a GraphQL mutation that merges")
            named = True
        if "closepullrequest" in low or ("updatepullrequest" in low and "closed" in low):
            line.hit(PR_CLOSE, "a GraphQL mutation that closes a pull request")
            named = True
        if "deleteref" in low:
            line.hit(PUSH_DELETE, "a GraphQL mutation that deletes a ref")
            named = True
        if "updateref" in low:          # updateRef, updateRefs
            if "force" in low:
                line.hit(PUSH_FORCE, "a GraphQL ref update with force")
            line.hit(PUSH_UNSURE, "a GraphQL mutation that moves a ref, which may be the default branch")
            named = True
        if "createcommitonbranch" in low:
            branch = _GRAPHQL_BRANCH.search(low)
            if branch is None or _protected(branch.group(1), set()):
                line.hit(PUSH_PROTECTED, "a GraphQL commit onto a branch it does not name, or the "
                         "default branch", branch.group(1) if branch else "the default branch")
            named = True
        queries = [f for f in fields if str(f).startswith("query=")]
        unread = has_input or not queries or any(
            str(q).startswith("query=@") or _wild(q, replace) for q in queries)
        if unread and not named:
            line.hit(UNSURE, "a GraphQL request whose query cannot be read here")
        return
    if not write:
        return
    if re.search(r"(^|/)pulls/[^/]+/merge$", path) or re.search(r"(^|/)merges$", path):
        line.hit(PR_MERGE, "`gh api` merges through the REST API")
    elif re.search(r"(^|/)pulls/[^/]+$", path):
        line.hit(PR_CLOSE, "`gh api` edits a pull request, which can close it")
    elif re.search(r"(^|/)issues/[^/]+$", path) and "closed" in low:
        line.hit(PR_CLOSE, "`gh api` closes an issue or a pull request")
    elif re.search(r"(^|/)issues/[^/]+$", path) and unreadable_fields:
        line.hit(UNSURE, "`gh api` edits an issue or a pull request with fields it cannot read")
    elif re.search(r"(^|/)git/refs(/|$)", path):
        if method == "DELETE":
            line.hit(PUSH_DELETE, "`gh api` deletes a ref")
        elif re.search(r"force\s*=\s*true", low):
            line.hit(PUSH_FORCE, "`gh api` force-updates a ref")
        ref = re.sub(r"^.*?git/refs/", "", path)
        branch = _branch_name("refs/" + ref) if ref else None
        if method != "DELETE" and (branch is None or _protected(branch, set()) or not ref):
            line.hit(PUSH_PROTECTED, "`gh api` moves a branch", branch or "a branch")
    elif re.search(r"(^|/)contents/", path):
        branches = [str(f).split("=", 1)[1] for f in fields if str(f).startswith("branch=")]
        if not branches or any(_wild(b, replace) or _protected(b, set()) for b in branches):
            line.hit(PUSH_PROTECTED, "`gh api` commits a file to the default branch",
                     branches[0] if branches else "the default branch")


def _api_path(endpoint: str) -> str:
    """An endpoint as GitHub routes it: host dropped, query cut,
    percent-decoded, `./` and `..` resolved, lowercased."""
    path = re.sub(r"^https?://[^/]+/", "", endpoint.strip()).split("?", 1)[0].split("#", 1)[0]
    path = urllib.parse.unquote(path)
    path = posixpath.normpath("/" + path)
    return path.strip("/").lower()


# --- the walk ----------------------------------------------------------------


def _line_into(line: _Line, text: str, depth: int = 0) -> None:
    for variant in rules._program_variants(text):
        walk = rules._Walk()
        try:
            with rules.work_scope(fresh=True):
                walk.line(variant, True, 0)
        except rules.TooComplex:
            if re.search(r"(?<![\w-])(gh|git|git-push)(?![\w-])", variant):
                line.hit(UNSURE, "the line is too complex to judge")
        _programs(line, walk, depth)


_NAMES = ("gh", "git", "git-push", "gh.exe", "git.exe")


def _programs(line: _Line, walk, depth: int) -> None:
    interpreter = False
    for prog in walk.progs:
        if not prog.word:
            line.executor = True        # a program read from input
            continue                    # (the vex reading asks it too)
        stem, could = _stem_could_be(prog.word, _NAMES)
        args = list(prog.args)
        if stem is None or (stem not in _DATA_STEMS and stem not in _NAMES
                            and stem not in rules._WRAPPER_OPTS and stem not in rules._RUNNER_SPECS):
            # A program that may run text it is handed (a shell, eval, an
            # interpreter, ssh, curl, one this reader does not know): the
            # raw-line backstop applies (`judge`).
            line.executor = True
        if stem in ("cd", "pushd"):
            if any(str(a) == "-" or str(a).startswith("+") for a in args):
                line.move(None)         # `cd -`, `pushd +1`: a folder from the stack
                continue
            target = next((a for a in args if not str(a).startswith("-")), None)
            line.move(target, home_if_empty=True)
            continue
        if stem == "git":
            _git(line, args, prog, depth)
        elif stem == "git-push":
            _push(line, args, prog, line.dirs, [])
        elif stem == "gh":
            _gh(line, args, prog)
        elif stem is None:
            if could:
                _git(line, args, prog, depth, sure=False)
                _gh(line, args, prog, sure=False)
        else:
            _dir_options(line, stem, args)
            if _INTERPRETERS.fullmatch(stem):
                interpreter = True
                _text(line, " ".join(str(a) for a in args), inline=True)
            if stem == "xargs":
                line.executor = True
    if interpreter:
        for body in walk.bodies:
            _text(line, body, inline=True)


def _dir_options(line: _Line, stem: str, args: list) -> None:
    """A wrapper or runner that changes the folder what it starts runs in
    (`env -C`, `uv run --directory`, `pnpm -C`): a candidate folder."""
    if stem not in rules._WRAPPER_OPTS and stem not in rules._RUNNER_SPECS:
        return
    for k, arg in enumerate(args):
        text = str(arg)
        match = _DIR_OPTION.match(text)
        if match:
            value = text.split("=", 1)[1] if match.group(2) else (args[k + 1] if k + 1 < len(args) else None)
            line.move(value, union=True)
        elif text in _SHORT_DIR:
            line.move(args[k + 1] if k + 1 < len(args) else None, union=True)
        elif len(text) > 2 and text[:2] in _SHORT_DIR:
            line.move(text[2:], union=True)


def _text(line: _Line, text: str, *, inline: bool = False) -> bool:
    """Text read for the operations, a backstop and not a parse: inline
    interpreter code (`inline`), or the whole raw line (`judge`). A push
    named with no branch, force or deletion in inline code is a push of the
    current branch (review M-3 of #208). True when anything matched."""
    found = False
    pr_known = any(h in (PR_MERGE, PR_CLOSE, UNSURE) for h in line.hits)
    push_known = any(h in (PUSH_PROTECTED, PUSH_FORCE, PUSH_DELETE, PUSH_UNSURE) for h in line.hits)
    for pattern, rule in _TEXT_RULES:
        if not inline and pr_known:
            break                       # the structure already named it
        if pattern.search(text):
            line.hit(rule if inline else UNSURE, "inline code spells it" if inline
                     else "the line spells it where a program may run it")
            found = True
    if _TEXT_PUSH.search(text) and (inline or not push_known):
        what = [rule for pattern, rule in _TEXT_PUSH_WHAT if pattern.search(text)]
        for rule in what:
            line.hit(rule if inline else PUSH_UNSURE, "inline code spells it" if inline
                     else "the line spells a push where a program may run it",
                     "main" if rule == PUSH_PROTECTED and inline else None)
        if inline and not what and _TEXT_GIT_PUSH.search(text):
            repos = line.repos()
            _current(line, repos, _defaults(repos))
        found = found or bool(what)
    return found


def _defaults(repos) -> set[str]:
    out: set[str] = set()
    if isinstance(repos, list):
        for _gitdir, common in repos:
            out |= _remote_heads(common)
    return out


def judge(command: str, cwd=None) -> Hit | None:
    """The owner-only operation this command line performs, or None.

    `cwd` is where the line runs: the shell's own folder when the provider
    reports it, else the brief's. Without one, a push of the current branch
    cannot be told and is asked."""
    if not isinstance(command, str) or not command.strip():
        return None
    if "\x00" in command:
        command = command.replace("\x00", " ")
    line = _Line(command, cwd)
    _line_into(line, command)
    if line.executor:
        # The backstop (review M-2 of #208): where the line runs a program
        # that may run text it is handed — `echo '…' | sh`, `bash <(…)`,
        # `eval "$(…)"`, `x='…'; $x`, `ssh host …`, `cmd.exe /c`, curl to the
        # API, a git exec setting — any spelling of the operations anywhere
        # on it asks, saying it may. A line of only git, gh and data
        # programs (a commit message or PR body that mentions a merge) is
        # read by structure alone.
        _text(line, command)
    if not line.hits:
        return None
    words = []
    for rule in line.hits:
        if rule == PUSH_PROTECTED and line.branches:
            words.append("pushes to " + ", ".join(line.branches[:3]))
        else:
            words.append(HEADLINES[rule])
    headline = " · ".join(dict.fromkeys(words))
    reason = REASON + (": " + "; ".join(line.reasons[:4]) if line.reasons else "")
    return Hit(line.hits[0], headline, reason, tuple(line.hits))


__all__ = ["Hit", "HEADLINES", "PROTECTED_BRANCHES", "PR_CLOSE", "PR_MERGE", "PUSH_DELETE",
           "PUSH_FORCE", "PUSH_PROTECTED", "PUSH_UNSURE", "UNSURE", "judge"]
