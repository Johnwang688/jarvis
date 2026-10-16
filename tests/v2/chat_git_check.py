"""Free checks for the Codex chat git tools (owner decision CG-0, 2026-10-15;
`docs/plans/2026-10-15-codex-git-credentials.md` §4.5).

Everything runs on a temporary HOME under /var/tmp, temporary Jarvis config,
local bare repositories standing in for GitHub (`chat_git._ALLOW_TEST_REMOTE`,
set here in code) and a fake `gh` that records its argv, folder, environment
and input. No network, no real login, no live daemon.

    python tests/v2/chat_git_check.py                    # every section
    python tests/v2/chat_git_check.py malicious_checks   # named sections
"""
from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config, runtime, tools  # noqa: E402
from jarvis.v2.tools import chat_git_tools  # noqa: E402,F401 — registers the tools

CHECKS = 0
FAILURES: list[str] = []
ONLY = frozenset(a for a in sys.argv[1:] if a.endswith("_checks"))


def ok(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")


def eq(actual, expected, message):
    ok(actual == expected, f"{message} (got {actual!r}, wanted {expected!r})")


# --------------------------------------------------------------------------
# harness

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
data = sys.stdin.read()
with open(os.path.join(here, "gh.log"), "a") as log:
    log.write(json.dumps({"argv": args, "cwd": os.getcwd(), "env": dict(os.environ),
                          "stdin": data}) + "\n")
mode_file = os.path.join(here, "mode")
mode = open(mode_file).read().strip() if os.path.exists(mode_file) else ""
if args[:2] == ["auth", "git-credential"]:
    if args[2:3] == ["get"]:
        print("protocol=https\nhost=github.com\nusername=x-access-token\npassword=FAKE-NOT-A-TOKEN")
    sys.exit(0)
if mode == "leak":
    sys.stderr.write("error: token ghp_" + "A" * 36 + " was rejected\n")
    sys.exit(1)
if args[:2] == ["pr", "create"]:
    print("https://github.com/owner/repo/pull/7")
sys.exit(0)
'''

IDENTITY = {"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}


class Box:
    """Temp HOME, config paths, fake gh and a marker folder; restored on exit."""

    def __enter__(self):
        from jarvis.v2 import chat_git
        self.root = Path(tempfile.mkdtemp(prefix="chat-git-", dir="/var/tmp")).resolve()
        self.home = self.root / "home"
        (self.home / ".config" / "jarvis").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.gh = self.bin / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.marks = self.root / "marks"
        self.marks.mkdir()
        # The owner's global config: a credential helper that must never run
        # for the daemon's pushes (the reset), and an identity.
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Owner Name\n\temail = owner@example.invalid\n"
            f"[credential]\n\thelper = !touch {self.marks}/global-helper\n")
        self._saved = {name: getattr(config, name) for name in (
            "ALLOWLIST_PATH", "MODELS_PATH", "PROVIDER_DEFAULTS_PATH", "V2_DATA_DIR",
            "V2_ALWAYS_ASK", "DISCORD_GUILD_PATH", "ROUTING_PATH", "CODEX_CATALOG_PATH",
            "SPEAKER_DIR", "CHAT_WORKTREES_DIR")}
        self._env = {k: os.environ.get(k) for k in ("HOME", "JARVIS_GH", "CODEX_HOME")}
        os.environ["HOME"] = str(self.home)
        os.environ["JARVIS_GH"] = str(self.gh)
        os.environ["CODEX_HOME"] = str(self.home / ".codex")
        cfg = self.home / ".config" / "jarvis"
        config.ALLOWLIST_PATH = cfg / "allowlist.json"
        config.MODELS_PATH = cfg / "models.json"
        config.PROVIDER_DEFAULTS_PATH = cfg / "provider_defaults.json"
        config.V2_DATA_DIR = self.root / "v2data"
        config.V2_ALWAYS_ASK = cfg / "always-ask.json"
        config.DISCORD_GUILD_PATH = cfg / "discord_guild.json"
        config.ROUTING_PATH = cfg / "routing.json"
        config.CODEX_CATALOG_PATH = self.home / ".local" / "share" / "jarvis" / "codex-models.json"
        config.SPEAKER_DIR = self.home / ".local" / "share" / "jarvis" / "speaker"
        config.CHAT_WORKTREES_DIR = self.root / "chat-worktrees"
        config.CHAT_WORKTREES_DIR.mkdir(mode=0o700)
        self._remotes = dict(chat_git._ALLOW_TEST_REMOTE)
        return self

    def __exit__(self, *exc):
        from jarvis.v2 import chat_git
        chat_git._ALLOW_TEST_REMOTE.clear()
        chat_git._ALLOW_TEST_REMOTE.update(self._remotes)
        for name, value in self._saved.items():
            setattr(config, name, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    # -- helpers ---------------------------------------------------------------

    def git(self, *args, cwd):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", **IDENTITY}
        return subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def project(self, name, *, branch="topic", url=None):
        """A Codex chat's checkout (`main`), its GitHub stand-in, and a linked
        worktree on `branch` under the chat worktrees folder, one commit ahead."""
        from jarvis.v2 import chat_git
        repo = f"owner/{name}"
        proj = self.root / name
        proj.mkdir()
        self.git("init", "-q", "-b", "main", cwd=proj)
        (proj / "README.md").write_text("hello\n")
        self.git("add", "README.md", cwd=proj)
        self.git("commit", "-qm", "init", cwd=proj)
        self.git("remote", "add", "origin", url or f"https://github.com/{repo}.git", cwd=proj)
        remote = self.root / f"{name}-remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.root)
        self.git("push", "-q", str(remote), "main:main", cwd=proj)
        chat_git._ALLOW_TEST_REMOTE[repo] = str(remote)
        wt = config.CHAT_WORKTREES_DIR / name
        self.git("worktree", "add", "-q", "-b", branch, str(wt), cwd=proj)
        (wt / "change.txt").write_text("change\n")
        self.git("add", "change.txt", cwd=wt)
        self.git("commit", "-qm", "change", cwd=wt)
        return SimpleNamespace(proj=proj, remote=remote, wt=wt, repo=repo, branch=branch,
                               common=proj / ".git")

    def remote_ref(self, remote, branch):
        try:
            return self.git("--git-dir", str(remote), "rev-parse", "--verify", "-q",
                            f"refs/heads/{branch}", cwd=self.root)
        except subprocess.CalledProcessError:
            return None

    def gh_log(self):
        path = self.bin / "gh.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def clear_gh(self):
        for name in ("gh.log", "mode"):
            path = self.bin / name
            if path.exists():
                path.unlink()

    def markers(self):
        return sorted(p.name for p in self.marks.iterdir())

    @property
    def decisions(self):
        path = config.V2_DATA_DIR / "decisions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Asker:
    human_backed = True

    def __init__(self, decision=None):
        from jarvis.v2.provider import Decision
        self.decision = decision if decision is not None else Decision.ALLOW
        self.seen = []

    def ask(self, request):
        self.seen.append(request)
        return self.decision


def permit_for(cwd, asker, profile=None):
    from jarvis.v2.model import PermissionProfile, Role
    from jarvis.v2.permissions import PermitContext, build_permit
    from jarvis.v2.provider import Brief
    brief = Brief(role=Role.CHAT, cwd=str(cwd), profile=profile or PermissionProfile.AUTO)
    return build_permit(PermitContext("cg000000", brief, provider="codex"), asker)


def call(tool, args, cwd, *, asker=None, profile=None, permit=None):
    """One call the way the bridge makes it (`bridge.call`): a fresh Context,
    the session's offered names bound, the session's permit as approver
    (`dangerous_tool=True`), a nondangerous tool's permit asked first."""
    from jarvis.v2.provider import Decision
    asker = asker if asker is not None else Asker()
    permit = permit or permit_for(cwd, asker, profile)

    def approve(entry, arguments):
        try:
            return Decision(permit(entry.name, dict(arguments), None,
                                   dangerous_tool=True)) is Decision.ALLOW
        except Exception:
            return False

    def run():
        from jarvis.v2 import chat_git
        runtime.bind(depth=0, tool_names=frozenset(chat_git.TOOLS), approve=approve,
                     caller={"thread_id": "cg000000", "project_id": "p", "provider": "codex",
                             "turn_id": "t1"})
        if not tools.REGISTRY[tool].dangerous:
            if Decision(permit(tool, dict(args), None)) is not Decision.ALLOW:
                return "Refused: this chat did not permit the Jarvis tool call."
        return tools.dispatch(tool, json.dumps(args), approve=approve).text

    return contextvars.Context().run(run), asker


# --------------------------------------------------------------------------
# sections


def unit_checks():
    """The URL, branch and argument readers on their own."""
    from jarvis.v2 import chat_git
    for url, want in (("https://github.com/owner/repo.git", "owner/repo"),
                      ("https://github.com/owner/repo", "owner/repo"),
                      ("https://github.com/Owner-1/re.po_x.git", "Owner-1/re.po_x"),
                      ("git@github.com:owner/repo.git", "owner/repo"),
                      ("git@github.com:owner/repo", "owner/repo")):
        eq(chat_git.github_repo(url), want, f"github_repo accepts {url}")
    for url in ("https://gitlab.com/owner/repo.git", "https://evil.example/owner/repo.git",
                "https://github.com.evil.example/owner/repo.git",
                "https://user@github.com/owner/repo.git", "https://user:pw@github.com/owner/repo",
                "https://github.com:443/owner/repo.git", "https://github.com/owner/repo.git?x=1",
                "https://github.com/owner/repo.git#frag", "http://github.com/owner/repo.git",
                "ssh://git@github.com/owner/repo.git", "git@github.com:owner/repo/extra",
                "ext::sh -c touch% /tmp/pwned", "file:///tmp/repo.git", "/tmp/repo.git",
                "https://github.com/owner/../repo", "https://github.com/../owner/repo",
                "https://github.com/owner/.hidden", "", None, "https://github.com/owner",
                "github.com/owner/repo", "https://GITHUB.COM/owner/repo"):
        eq(chat_git.github_repo(url), None, f"github_repo refuses {url!r}")
    for name in ("topic", "codex/fix-1", "a.b_c-d", "feature/x/y"):
        ok(chat_git.valid_branch(name), f"valid_branch accepts {name}")
    for name in ("-x", "--force", "a..b", "a:b", "a b", "a@{1}", "a/", "a.", ".a", "a/.b",
                 "a.lock", "a//b", "HEAD", "", "a\tb", "x" * 201, "a~1", "a^", "a?", "a*", "a[b"):
        ok(not chat_git.valid_branch(name), f"valid_branch refuses {name!r}")
    for tool, args in (("chat_git_push", {"worktree": "/x", "force": True}),
                       ("chat_git_push", {"worktree": "/x", "delete": True}),
                       ("chat_pr_merge", {"worktree": "/x", "number": 3, "method": "admin"}),
                       ("chat_pr_merge", {"worktree": "/x", "number": 3, "admin": True}),
                       ("chat_pr_merge", {"worktree": "/x", "number": 3, "auto": True}),
                       ("chat_pr_merge", {"worktree": "/x", "number": True}),
                       ("chat_pr_merge", {"worktree": "/x", "number": 0}),
                       ("chat_pr_close", {"worktree": "/x", "number": "3"}),
                       ("chat_pr_create", {"worktree": "/x", "title": ""}),
                       ("chat_pr_create", {"worktree": "/x", "title": "a\nb"}),
                       ("chat_pr_create", {"worktree": "/x", "title": "t", "base": "-x"}),
                       ("chat_pr_create", {"worktree": "/x", "title": "t", "draft": "yes"}),
                       ("chat_pr_create", {"worktree": "/x", "title": "t" * 300})):
        try:
            chat_git.normalize(tool, args)
            ok(False, f"normalize refuses {tool} {args}")
        except chat_git.Refused:
            ok(True, "")
    # The seam is code only: nothing under jarvis/ but chat_git names it, and
    # chat_git never fills it from the environment or a file.
    root = Path(__file__).resolve().parents[2] / "jarvis"
    users = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                   if "_ALLOW_TEST_REMOTE" in p.read_text(encoding="utf-8"))
    eq(users, ["v2/chat_git.py"], "only chat_git names the test seam")
    source = (root / "v2" / "chat_git.py").read_text(encoding="utf-8")
    seam_lines = [l for l in source.splitlines() if "_ALLOW_TEST_REMOTE" in l]
    ok(not any("environ" in l or "getenv" in l or "json" in l for l in seam_lines),
       "the seam is never read from the environment or a file")
    # CG-1's reader.
    for command in ("git push", "git push origin topic", "git -C /x push -u origin b",
                    "gh pr create --fill", "gh pr merge 3 --squash", "gh api user",
                    "/usr/bin/gh repo view", "bash -lc 'gh pr view'", "env A=1 git fetch",
                    "git pull --rebase", "git ls-remote origin", "git-push origin x",
                    "cd /x && git fetch origin", "git fetch https://github.com/o/r.git"):
        ok(chat_git.raw_remote(command), f"raw_remote reads {command!r} as network git or gh")
    for command in ("git status", "git commit -m 'gh push'", "git fetch /local/mirror x",
                    "git fetch ./mirror", "git fetch file:///tmp/m", "echo gh", "npm test",
                    "$G push", "git log", "", None):
        ok(not chat_git.raw_remote(command), f"raw_remote leaves {command!r} alone")


def session_tool_checks():
    """Offered to Codex owner chats only, foreground only; never to Claude,
    tasks, the standalone MCP server or a whole-registry surface."""
    from jarvis.v2 import chat_git, mcp_catalog, session_tools
    from jarvis.v2.model import Role
    from jarvis.v2.provider import Brief
    codex = {t.name: t for t in session_tools.for_provider("codex")}
    claude = {t.name for t in session_tools.for_provider("claude")}
    for name in sorted(chat_git.TOOLS):
        entry = codex.get(name)
        ok(entry is not None, f"{name} is offered to Codex chats")
        if entry is not None:
            eq(entry.providers, frozenset({"codex"}), f"{name}: Codex only")
            eq(entry.foreground_only, True, f"{name}: foreground only (a sub-agent cannot push)")
            eq((entry.read_only, entry.desk_only, entry.owner_gated), (False, False, False),
               f"{name}: not read-only, not desk-only, not owner-gated")
        ok(name not in claude, f"{name} is not offered to Claude chats")
        ok(name not in mcp_catalog.MCP_TOOLS, f"{name} is not in the external MCP catalog")
        ok(name not in tools.default_names(), f"{name} is explicit-only")
        eq(tools.REGISTRY[name].dangerous, name in chat_git.DANGEROUS,
           f"{name}: dangerous exactly when it publishes or changes a pull request")
    eq(chat_git.DANGEROUS, chat_git.TOOLS - {"chat_git_fetch"}, "fetch is the only safe one")
    chat = Brief(role=Role.CHAT, cwd="/tmp", mcp_servers={"jarvis": {}})
    task = Brief(role=Role.CHAT, cwd="/tmp", mcp_servers={"jarvis": {}}, task_id="t1")
    ok(session_tools.codex_session_tool(chat, "chat_git_push") is not None,
       "the owner's Codex chat session holds chat_git_push")
    eq(session_tools.codex_session_tool(task, "chat_git_push"), None, "a task's thread does not")
    eq(session_tools.claude_session_tool(chat, "mcp__jarvis__chat_git_push"), None,
       "a Claude chat does not")


def push_checks():
    """The happy path, the pin, the mirror, the bound approval."""
    from jarvis.v2 import chat_git
    from jarvis.v2.provider import Decision
    with Box() as box:
        p = box.project("alpha")
        args = {"worktree": str(p.wt)}
        head = box.git("rev-parse", "HEAD", cwd=p.wt)
        text, asker = call("chat_git_push", args, p.proj)
        ok(text.startswith("Pushed topic to owner/alpha"), f"push reports success ({text[:200]!r})")
        eq(box.remote_ref(p.remote, "topic"), head, "the remote holds exactly the worktree's commit")
        eq(len(asker.seen), 1, "the owner is asked once")
        if asker.seen:
            card = asker.seen[0]
            line = f"git push https://github.com/owner/alpha.git refs/heads/topic:refs/heads/topic"
            eq(card.command, line, "the card's command is the exact line the daemon runs")
            eq(card.allowlistable, False, "no Always")
            ok("Codex wants to push topic to owner/alpha" in card.headline, card.headline)
            ok("first push: pins origin to owner/alpha" in card.headline,
               f"the first push names the pin ({card.headline!r})")
        eq(box.decisions[-1]["command"], line, "the log keeps the line too")
        eq(box.decisions[-1]["layer"], "human", "answered by the owner")
        eq(chat_git.pinned(p.common), "owner/alpha", "the first push pins origin")
        root = config.V2_DATA_DIR / "chat-git"
        eq(root.stat().st_mode & 0o777, 0o700, "the chat git folder is 0700")
        eq((root / "pins.json").stat().st_mode & 0o777, 0o600, "pins.json is 0600")
        mirror = chat_git.mirror_dir(p.common)
        eq(mirror.stat().st_mode & 0o777, 0o700, "the mirror is 0700")
        ok(mirror.parent == root, "the mirror lives under V2_DATA_DIR/chat-git")
        ok("credential" not in (mirror / "config").read_text(), "the mirror stores no helper")
        ok(not any("FAKE-NOT-A-TOKEN" in f.read_text(errors="replace")
                   for f in mirror.rglob("*") if f.is_file()), "nor any token")
        eq(box.markers(), [], "the owner's other credential helper never ran")
        log = box.gh_log()
        ok(any(e["argv"][:3] == ["auth", "git-credential", "get"] for e in log) or not log,
           "only gh's helper is consulted")
        # A second push: no pin text.
        (p.wt / "more.txt").write_text("more\n")
        box.git("add", "more.txt", cwd=p.wt)
        box.git("commit", "-qm", "more", cwd=p.wt)
        text, asker = call("chat_git_push", args, p.proj)
        ok(text.startswith("Pushed"), f"a second push works ({text[:120]!r})")
        ok(asker.seen and "first push" not in asker.seen[0].headline, "and names no pin")
        eq(box.remote_ref(p.remote, "topic"), box.git("rev-parse", "HEAD", cwd=p.wt), "moved")

        # The owner's no, deny-all, strict: nothing runs.
        from jarvis.v2.model import PermissionProfile
        from jarvis.v2.permissions import deny_all
        (p.wt / "third.txt").write_text("3\n")
        box.git("add", "third.txt", cwd=p.wt)
        box.git("commit", "-qm", "third", cwd=p.wt)
        before = box.remote_ref(p.remote, "topic")
        for label, asker, profile in (("no", Asker(Decision.DENY), None),
                                      ("deny-all", deny_all("nobody"), None),
                                      ("strict", Asker(), PermissionProfile.STRICT)):
            text, _ = call("chat_git_push", args, p.proj, asker=asker, profile=profile)
            ok(text.startswith("The user declined"), f"[{label}] declined ({text[:80]!r})")
            eq(box.remote_ref(p.remote, "topic"), before, f"[{label}] nothing pushed")
        eq(asker.seen if isinstance(asker, Asker) else [], [], "strict asks nobody")

        # Without the permit's grant the tool refuses (a bare call, or one the
        # permit did not approve as the owner's yes).
        def bare():
            runtime.bind(depth=0)
            return tools.REGISTRY["chat_git_push"].func(**args)
        text = contextvars.Context().run(bare)
        ok(text.startswith("Refused: this Jarvis git tool runs only"), f"no grant, no push ({text!r})")
        eq(box.remote_ref(p.remote, "topic"), before, "nothing pushed without a grant")

        # A sub-agent depth is refused even with a grant.
        permit = permit_for(p.proj, Asker())

        def deep():
            runtime.bind(depth=1)
            permit("chat_git_push", dict(args), None, dangerous_tool=True)
            return tools.REGISTRY["chat_git_push"].func(**args)
        text = contextvars.Context().run(deep)
        ok(text.startswith("Refused: a sub-agent"), f"depth 1 refused ({text!r})")

        # The branch switched while the card was open: never pushed.
        other = box.git("rev-parse", "HEAD", cwd=p.wt)
        box.git("branch", "other", cwd=p.wt)
        gitdir = Path(box.git("rev-parse", "--absolute-git-dir", cwd=p.wt))

        def switched():
            runtime.bind(depth=0)
            decision = permit("chat_git_push", dict(args), None, dangerous_tool=True)
            (gitdir / "HEAD").write_text("ref: refs/heads/other\n")
            try:
                return decision, tools.REGISTRY["chat_git_push"].func(**args)
            finally:
                (gitdir / "HEAD").write_text("ref: refs/heads/topic\n")
        decision, text = contextvars.Context().run(switched)
        eq(decision, Decision.ALLOW, "the owner said yes to topic")
        ok(text.startswith("Refused: the repository changed"), f"a switched HEAD is refused ({text!r})")
        eq(box.remote_ref(p.remote, "other"), None, "the other branch was never pushed")
        ok(other, "")

        # The grant is one call: a second func call needs a second yes.
        def twice():
            runtime.bind(depth=0)
            permit("chat_git_push", dict(args), None, dangerous_tool=True)
            first = tools.REGISTRY["chat_git_push"].func(**args)
            return first, tools.REGISTRY["chat_git_push"].func(**args)
        first, second = contextvars.Context().run(twice)
        ok(first.startswith("Pushed"), f"the granted call runs ({first[:80]!r})")
        ok(second.startswith("Refused: this Jarvis git tool runs only"), "the grant is used up")

        # A grant for other arguments does not cover this call.
        def other_args():
            runtime.bind(depth=0)
            permit("chat_git_push", {"worktree": str(p.proj)}, None, dangerous_tool=True)
            return tools.REGISTRY["chat_git_push"].func(**args)
        text = contextvars.Context().run(other_args)
        ok(text.startswith("Refused"), "a grant for another worktree does not cover this one")

        # Nor one for another body (the body is not on the line, so only the
        # arguments bind it).
        box.clear_gh()

        def other_body():
            runtime.bind(depth=0)
            permit("chat_pr_create", {**args, "title": "T", "body": "approved"}, None, dangerous_tool=True)
            return tools.REGISTRY["chat_pr_create"].func(**args, title="T", body="swapped")
        text = contextvars.Context().run(other_body)
        ok(text.startswith("Refused: this Jarvis git tool runs only"),
           f"a grant for another body does not cover this one ({text[:80]!r})")
        ok(not any(e["argv"][:2] == ["pr", "create"] for e in box.gh_log()), "no PR was opened")


def refusal_checks():
    """Refused unasked: detached HEAD, protected and default branches, bad
    names, a worktree elsewhere, another repository, origin changes, URLs."""
    from jarvis.v2 import chat_git
    with Box() as box:
        p = box.project("beta")
        args = {"worktree": str(p.wt)}
        gitdir = Path(box.git("rev-parse", "--absolute-git-dir", cwd=p.wt))
        sha = box.git("rev-parse", "HEAD", cwd=p.wt)

        def refused(tool, a, words, label, cwd=None):
            box.clear_gh()
            text, asker = call(tool, a, cwd or p.proj)
            ok(text.startswith("The user declined"), f"{label}: refused ({text[:100]!r})")
            eq(asker.seen, [], f"{label}: unasked")
            record = box.decisions[-1] if box.decisions else {}
            eq(record.get("layer"), "deny", f"{label}: at layer 1")
            ok(words in record.get("reason", ""), f"{label}: says {words!r} ({record.get('reason')!r})")
            eq(box.gh_log(), [], f"{label}: gh never ran")

        (gitdir / "HEAD").write_text(sha + "\n")
        refused("chat_git_push", args, "detached", "detached HEAD")
        for name in ("main", "master", "release-1", "production", "prod-x", "Main"):
            (gitdir / "HEAD").write_text(f"ref: refs/heads/{name}\n")
            refused("chat_git_push", args, "protected", f"branch {name}")
            refused("chat_pr_create", {**args, "title": "t"}, "protected", f"PR from {name}")
        (p.common / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
        (p.common / "refs" / "remotes" / "origin" / "HEAD").write_text(
            "ref: refs/remotes/origin/develop\n")
        (gitdir / "HEAD").write_text("ref: refs/heads/develop\n")
        refused("chat_git_push", args, "protected", "the remote's HEAD branch")
        (p.common / "refs" / "remotes" / "origin" / "HEAD").unlink()
        for name in ("-x", "a..b", "a:b", "x.lock"):
            (gitdir / "HEAD").write_text(f"ref: refs/heads/{name}\n")
            refused("chat_git_push", args, "not a plain branch", f"branch {name!r}")
        (gitdir / "HEAD").write_text("ref: refs/heads/topic\n")

        # The remote's real default branch, read at push time.
        box.git("push", "-q", str(p.remote), "main:refs/heads/topic", cwd=p.proj)
        box.git("--git-dir", str(p.remote), "symbolic-ref", "HEAD", "refs/heads/topic", cwd=box.root)
        before = box.remote_ref(p.remote, "topic")
        text, asker = call("chat_git_push", args, p.proj)
        ok(text.startswith("Refused: topic is a protected or default branch"),
           f"the remote's default branch is refused at run time ({text[:100]!r})")
        eq(box.remote_ref(p.remote, "topic"), before, "and not pushed")
        box.git("--git-dir", str(p.remote), "symbolic-ref", "HEAD", "refs/heads/main", cwd=box.root)
        box.git("--git-dir", str(p.remote), "update-ref", "-d", "refs/heads/topic", cwd=box.root)

        # A worktree outside the chat root, a subfolder, another repository.
        elsewhere = box.root / "elsewhere"
        box.git("worktree", "add", "-q", "-b", "away", str(elsewhere), cwd=p.proj)
        refused("chat_git_push", {"worktree": str(elsewhere)}, "worktree must be", "outside the root")
        (p.wt / "sub").mkdir()
        refused("chat_git_push", {"worktree": str(p.wt / "sub")}, "top of a git checkout", "a subfolder")
        refused("chat_git_push", {"worktree": "relative/path"}, "absolute", "a relative path")
        other = box.project("gamma")
        refused("chat_git_push", {"worktree": str(other.wt)}, "different repository",
                "a worktree of another repository")

        # read_target on a symlinked or oversized `.git` file.
        dot = p.wt / ".git"
        saved = dot.read_text()
        real = box.root / "dotgit-file"
        real.write_text(saved)
        dot.unlink()
        dot.symlink_to(real)
        refused("chat_git_push", args, "validated", "a symlinked .git file")
        dot.unlink()
        dot.write_text(saved.strip() + "\n" + " " * 5000 + "\n")
        refused("chat_git_push", args, "validated", "an oversized .git file")
        dot.write_text(saved)
        cfg = p.common / "config"
        saved_cfg = cfg.read_bytes()
        cfg.write_bytes(saved_cfg + b"#" * 70_000 + b"\n")
        refused("chat_git_push", args, "config", "an oversized repository config")
        cfg.write_bytes(saved_cfg)

        # Every URL that is not github.com is refused before anything runs.
        for url in ("https://gitlab.com/owner/beta.git", "https://user@github.com/owner/beta.git",
                    "https://github.com:443/owner/beta.git", "ext::sh -c touch% /tmp/x",
                    "file:///tmp/beta.git", str(p.remote), "http://github.com/owner/beta.git"):
            box.git("remote", "set-url", "origin", url, cwd=p.proj)
            refused("chat_git_push", args, "not a github.com", f"origin {url}")
        box.git("remote", "set-url", "origin", "https://github.com/owner/beta.git", cwd=p.proj)
        box.git("config", "--add", "remote.origin.url", "https://github.com/evil/beta.git", cwd=p.proj)
        refused("chat_git_push", args, "exactly one", "two origin URLs")
        box.git("config", "--unset-all", "remote.origin.url", cwd=p.proj)
        box.git("config", "remote.origin.url", "https://github.com/owner/beta.git", cwd=p.proj)
        inc = box.root / "inc.cfg"
        inc.write_text('[remote "origin"]\n\turl = https://github.com/evil/beta.git\n')
        box.git("config", "include.path", str(inc), cwd=p.proj)
        text, _ = call("chat_git_push", args, p.proj)
        ok(text.startswith("Pushed topic to owner/beta"),
           f"an include is not followed when origin is read ({text[:100]!r})")
        box.git("config", "--unset", "include.path", cwd=p.proj)

        # merge, close and fetch need a pin; now there is one. An origin
        # change after the pin is refused for every tool.
        eq(chat_git.pinned(p.common), "owner/beta", "pinned")
        box.git("remote", "set-url", "origin", "https://github.com/evil/beta.git", cwd=p.proj)
        for tool, a in (("chat_git_push", args), ("chat_pr_create", {**args, "title": "t"}),
                        ("chat_pr_merge", {**args, "number": 3}),
                        ("chat_pr_close", {**args, "number": 3})):
            refused(tool, a, "origin changed", f"{tool} after an origin change")
        box.clear_gh()
        text, asker = call("chat_git_fetch", args, p.proj)
        ok(text.startswith("Refused"), f"fetch after an origin change is refused ({text[:80]!r})")
        eq(box.gh_log(), [], "fetch: gh never ran")
        box.git("remote", "set-url", "origin", "https://github.com/owner/beta.git", cwd=p.proj)
        # And a pin cannot be overwritten in code either.
        try:
            chat_git.pin(p.common, "evil/beta")
            ok(False, "pin refuses a different repository")
        except chat_git.Refused:
            ok(True, "")
        # Unpinned repositories: merge, close and fetch are refused.
        for tool, a in (("chat_pr_merge", {"worktree": str(other.wt), "number": 1}),
                        ("chat_pr_close", {"worktree": str(other.wt), "number": 1})):
            refused(tool, a, "no pinned", f"{tool} with no pin", cwd=other.proj)
        text, _ = call("chat_git_fetch", {"worktree": str(other.wt)}, other.proj)
        ok(text.startswith("Refused"), f"fetch with no pin is refused ({text[:80]!r})")


def gh_checks():
    """gh runs with --repo, in the mirror, with the owner's HOME and nothing
    of Codex's; its output is scrubbed; merges and closes carry the headline."""
    from jarvis.v2 import chat_git
    with Box() as box:
        p = box.project("delta")
        args = {"worktree": str(p.wt)}
        mirror = chat_git.mirror_dir(p.common)
        box.clear_gh()
        text, asker = call("chat_pr_create", {**args, "title": "Add change", "body": "Body\ntext",
                                              "base": "main", "draft": True}, p.proj)
        ok("Opened the pull request." in text, f"create reports success ({text[:200]!r})")
        eq(box.remote_ref(p.remote, "topic"), box.git("rev-parse", "HEAD", cwd=p.wt),
           "create pushed the branch first")
        line = ("git push https://github.com/owner/delta.git refs/heads/topic:refs/heads/topic && "
                "gh pr create --repo owner/delta --head topic --base main --draft "
                "--title 'Add change' --body-file -")
        eq(asker.seen[0].command if asker.seen else None, line, "one card shows both commands")
        prs = [e for e in box.gh_log() if e["argv"][:2] == ["pr", "create"]]
        eq(len(prs), 1, "gh pr create ran once")
        if prs:
            run = prs[0]
            eq(run["argv"], ["pr", "create", "--repo", "owner/delta", "--head", "topic", "--base",
                             "main", "--draft", "--title", "Add change", "--body-file", "-"],
               "exactly the shown argv")
            eq(run["cwd"], str(mirror), "gh runs in the mirror, never the Codex worktree")
            eq(run["stdin"], "Body\ntext", "the body on stdin")
            env = run["env"]
            eq(env.get("HOME"), str(box.home), "gh sees the owner's HOME")
            for key in ("CODEX_HOME", "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "GH_TOKEN",
                        "GITHUB_TOKEN", "JARVIS_GH"):
                ok(key not in env, f"gh's environment has no {key}")
            eq(env.get("GH_PROMPT_DISABLED"), "1", "prompts off")
            eq(env.get("GIT_CONFIG_KEY_0"), "core.hooksPath", "gh's own git is pinned too")
        for tool, n, words, argv in (
                ("chat_pr_merge", {"number": 7}, "merges a pull request",
                 ["pr", "merge", "7", "--repo", "owner/delta", "--squash"]),
                ("chat_pr_merge", {"number": 8, "method": "rebase"}, "merges a pull request",
                 ["pr", "merge", "8", "--repo", "owner/delta", "--rebase"]),
                ("chat_pr_close", {"number": 9}, "closes a pull request",
                 ["pr", "close", "9", "--repo", "owner/delta"])):
            box.clear_gh()
            text, asker = call(tool, {**args, **n}, p.proj)
            ok("succeeded" in text, f"{tool} reports success ({text[:120]!r})")
            eq(len(asker.seen), 1, f"{tool}: asked once")
            if asker.seen:
                card = asker.seen[0]
                ok(card.headline.startswith(words), f"{tool}: the card leads with {words!r} ({card.headline!r})")
                eq(card.command, "gh " + " ".join(argv), f"{tool}: the card's command is the line")
                eq(card.allowlistable, False, f"{tool}: no Always")
            log = box.gh_log()
            eq([e["argv"] for e in log], [argv], f"{tool}: exactly that argv, once")
            eq([e["cwd"] for e in log], [str(mirror)], f"{tool}: in the mirror")
        # Redaction of a token-shaped line on gh's stderr.
        box.clear_gh()
        (box.bin / "mode").write_text("leak")
        text, _ = call("chat_pr_close", {**args, "number": 10}, p.proj)
        ok("ghp_" + "A" * 36 not in text, "a token-shaped value never reaches the model")
        ok("[redacted]" in text, f"it is marked redacted ({text[:160]!r})")
        box.clear_gh()
        # chat_git_fetch: no card, the mirror updated, a local fetch command.
        box.git("--git-dir", str(p.remote), "branch", "upstream-work", "main", cwd=box.root)
        text, asker = call("chat_git_fetch", args, p.proj)
        ok(text.startswith("Fetched owner/delta"), f"fetch reports success ({text[:160]!r})")
        eq(asker.seen, [], "fetch asks nobody")
        ok(f"git fetch {mirror}" in text, "it names the mirror to fetch from")
        box.git("fetch", "-q", str(mirror), "+refs/remotes/origin/*:refs/remotes/origin/*", cwd=p.wt)
        ok(box.git("rev-parse", "--verify", "-q", "refs/remotes/origin/upstream-work", cwd=p.wt),
           "Codex's own local fetch from the mirror works")
        from jarvis.v2.model import PermissionProfile
        text, _ = call("chat_git_fetch", args, p.proj, profile=PermissionProfile.STRICT)
        ok(text.startswith("Refused"), "strict refuses fetch")


def env_checks():
    """The local fetch from the Codex repository has no credential in its
    environment; the network commands have the owner's; the helper list is
    reset to gh's alone."""
    from jarvis.v2 import chat_git
    with Box() as box:
        p = box.project("eps")
        calls = []
        real = chat_git._run

        def spy(argv, env, cwd, timeout, stdin=None):
            calls.append((list(argv), dict(env), cwd))
            return real(argv, env, cwd, timeout, stdin)
        chat_git._run = spy
        try:
            text, _ = call("chat_git_push", {"worktree": str(p.wt)}, p.proj)
        finally:
            chat_git._run = real
        ok(text.startswith("Pushed"), f"pushed ({text[:120]!r})")
        local = [c for c in calls if str(p.common) in c[0] and "fetch" in c[0]]
        eq(len(local), 1, "one local fetch from the Codex repository")
        empty = config.V2_DATA_DIR / "chat-git" / "empty-home"
        if local:
            argv, env, cwd = local[0]
            eq(env.get("HOME"), str(empty), "the local fetch's HOME is the empty private home")
            eq(env.get("GIT_CONFIG_GLOBAL"), "/dev/null", "and no global config")
            eq(env.get("GIT_CONFIG_NOSYSTEM"), "1", "and no system config")
            ok(not any(k.startswith(("GH_", "XDG_", "DBUS", "SSH_")) for k in env),
               f"and no gh, XDG, D-Bus or ssh variable ({sorted(env)})")
            eq(set(env), {"PATH", "HOME", "LANG", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                          "GIT_TERMINAL_PROMPT", "GIT_CEILING_DIRECTORIES"}, "exactly these keys")
            ok(not any("credential" in a for a in argv), "no credential helper on its line")
            for pin in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "protocol.ext.allow=never"):
                ok(pin in argv, f"hardened: {pin}")
            eq(list(empty.iterdir()), [], "the empty home stays empty")
        pushes = [c for c in calls if "push" in c[0]]
        eq(len(pushes), 1, "one push")
        if pushes:
            argv, env, cwd = pushes[0]
            eq(env.get("HOME"), str(box.home), "the push has the owner's HOME")
            ok(not any(k.startswith("GIT_") and k not in ("GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM")
                       for k in env), "and no other GIT_* variable")
            k = argv.index("credential.helper=") if "credential.helper=" in argv else -1
            ok(k > 0 and argv[k - 1] == "-c", "the helper list is reset")
            ok(any(a.startswith("credential.https://github.com.helper=!") and a.endswith(" auth git-credential")
                   and str(box.gh) in a for a in argv), "then only gh's helper")
            ok("http.proxy=" in argv, "no proxy")
            eq(cwd, str(chat_git.mirror_dir(p.common)), "run in the mirror")
        # The reset, end to end: git asks for a github.com credential in the
        # mirror. The owner's global helper (touches a marker) must not run;
        # gh's must.
        box.clear_gh()
        mirror = chat_git.mirror_dir(p.common)
        code, out, _err = chat_git.run_git(
            ["--git-dir", str(mirror), "credential", "fill"],
            stdin="protocol=https\nhost=github.com\npath=owner/eps.git\n\n")
        eq(code, 0, "credential fill answers")
        ok("username=x-access-token" in out, "from gh's helper")
        eq(box.markers(), [], "the owner's other helper never ran")
        ok(any(e["argv"][:3] == ["auth", "git-credential", "get"] for e in box.gh_log()),
           "gh auth git-credential was asked")
        # What the mirror's git would use for github.com: no proxy, no rewrite.
        code, out, _ = chat_git.run_git(["--git-dir", str(mirror), "ls-remote", "--get-url",
                                         "https://github.com/owner/eps.git"])
        eq(out.strip(), "https://github.com/owner/eps.git", "no insteadOf rewrites the destination")


# The malicious repository config suite (§4.5). Each case plants keys or
# hooks in the Codex repository; the push must still land in the pinned
# remote (or fail cleanly), no marker may be written, and the decoy remote
# must stay empty.
def _script(box, name):
    path = box.root / "scripts" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch '{box.marks}/{name}'\nexit 0\n")
    path.chmod(0o755)
    return path


HOOKS = ("pre-push", "post-checkout", "reference-transaction", "pre-receive", "update",
         "post-update", "post-receive", "push-to-checkout", "pre-commit", "post-merge",
         "fsmonitor-watchman", "pre-auto-gc", "post-rewrite")


def malicious_checks():
    from jarvis.v2 import chat_git
    with Box() as box:
        decoy = box.root / "decoy.git"
        box.git("init", "-q", "--bare", str(decoy), cwd=box.root)

        def raw(p, *args, stdin=None):
            """Plain git in the Codex repository, as Codex's own would run:
            the positive control that each plant is live there."""
            env = {"PATH": "/usr/bin:/bin", "HOME": str(box.root), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", **IDENTITY}
            return subprocess.run(["git", *args], cwd=str(p.proj), env=env, input=stdin or "",
                                  capture_output=True, text=True, timeout=60)

        def fired(name):
            return lambda p: (box.marks / name).exists()

        controls = {
            "hooks": lambda p: (raw(p, "commit", "-q", "--allow-empty", "-m", "control"),
                                fired("hook-pre-commit")(p))[1],
            "status": lambda p, name: (raw(p, "status"), fired(name)(p))[1],
            "ssh": lambda p, name: (raw(p, "ls-remote", "ssh://example.invalid/x.git"), fired(name)(p))[1],
            "helper": lambda p, name: (raw(p, "credential", "fill",
                                           stdin="protocol=https\nhost=github.com\n\n"), fired(name)(p))[1],
            "url": lambda p: decoy.name in raw(p, "ls-remote", "--get-url",
                                               f"https://github.com/{p.repo}.git").stdout,
            "pushurl": lambda p: decoy.name in raw(p, "remote", "get-url", "--push", "origin").stdout,
            "proxy": lambda p: "127.0.0.1:9" in raw(p, "config", "--get-urlmatch", "http.proxy",
                                                    "https://github.com/").stdout,
        }

        def case(label, plant, tool="chat_git_push", control=None):
            p = box.project(label.replace(" ", "-").replace(".", "-"))
            cfg = []
            plant(p, cfg)
            for key, value in cfg:
                box.git("config", "--add", key, value, cwd=p.proj)
            args = {"worktree": str(p.wt)}
            if tool == "chat_pr_create":
                args["title"] = "t"
            text, asker = call(tool, args, p.proj)
            ok(text.startswith("Pushed"), f"[{label}] the push still works ({text[:160]!r})")
            eq(box.remote_ref(p.remote, p.branch), box.git("rev-parse", "HEAD", cwd=p.wt),
               f"[{label}] the pinned remote holds the branch")
            eq(box.markers(), [], f"[{label}] nothing planted ran")
            eq(box.git("--git-dir", str(decoy), "for-each-ref", cwd=box.root), "",
               f"[{label}] the decoy remote received nothing")
            ok(asker.seen and asker.seen[0].command.startswith(
                f"git push https://github.com/{p.repo}.git "), f"[{label}] the card names the pin")
            # The mirror's own view of github.com: untouched by the plant.
            mirror = chat_git.mirror_dir(p.common)
            code, out, _ = chat_git.run_git(["--git-dir", str(mirror), "ls-remote", "--get-url",
                                             f"https://github.com/{p.repo}.git"])
            eq(out.strip(), f"https://github.com/{p.repo}.git", f"[{label}] no rewrite in the mirror")
            code, out, _ = chat_git.run_git(["--git-dir", str(mirror), "config", "--get-urlmatch",
                                             "http.proxy", f"https://github.com/{p.repo}.git"])
            eq(out.strip(), "", f"[{label}] no proxy in the mirror")
            if control is not None:
                ok(control(p), f"[{label}] positive control: the plant is live in the Codex repo")
            for marker in box.marks.iterdir():
                marker.unlink()

        def hooks(p, cfg):
            folder = box.root / "hooks"
            folder.mkdir(exist_ok=True)
            for name in HOOKS:
                (folder / name).write_text(f"#!/bin/sh\ntouch '{box.marks}/hook-{name}'\n")
                (folder / name).chmod(0o755)
                shutil.copy(folder / name, p.common / "hooks" / name)
                (p.common / "hooks" / name).chmod(0o755)
            cfg.append(("core.hooksPath", str(folder)))
        case("hooksPath and hooks", hooks, control=controls["hooks"])
        case("hooks on pr create", hooks, tool="chat_pr_create", control=controls["hooks"])
        case("fsmonitor", lambda p, cfg: cfg.append(("core.fsmonitor", str(_script(box, "fsmonitor")))),
             control=lambda p: controls["status"](p, "fsmonitor"))
        case("sshCommand", lambda p, cfg: cfg.append(("core.sshCommand", str(_script(box, "ssh")))),
             control=lambda p: controls["ssh"](p, "ssh"))

        def alternates(p, cfg):
            other = box.root / "alt-source.git"
            if not other.exists():
                box.git("init", "-q", "--bare", str(other), cwd=box.root)
            (p.common / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (p.common / "objects" / "info" / "alternates").write_text(str(other / "objects") + "\n")
            cfg.append(("core.alternateRefsCommand", str(_script(box, "alternate-refs"))))
        case("alternateRefsCommand", alternates,
             control=lambda p: (raw(p, "fetch", "-q", str(p.remote), "main"),
                                fired("alternate-refs")(p))[1])
        case("packObjectsHook",
             lambda p, cfg: cfg.append(("uploadpack.packObjectsHook", str(_script(box, "pack-objects")))))

        def include(p, cfg):
            inc = box.root / f"include-{p.repo.split('/')[1]}.cfg"
            inc.write_text(
                f"[core]\n\thooksPath = {box.root / 'hooks'}\n\tfsmonitor = {_script(box, 'inc-fsmonitor')}\n"
                f"\tsshCommand = {_script(box, 'inc-ssh')}\n"
                f"\talternateRefsCommand = {_script(box, 'inc-alt')}\n"
                f"[uploadpack]\n\tpackObjectsHook = {_script(box, 'inc-pack')}\n"
                f"[credential]\n\thelper = !{_script(box, 'inc-helper')}\n"
                f'[url "{decoy}"]\n\tinsteadOf = https://github.com/\n'
                f"[http]\n\tproxy = http://127.0.0.1:9\n")
            cfg.append(("include.path", str(inc)))
            cfg.append((f"includeIf.gitdir:{p.common}/.path", str(inc)))
        case("include.path", include, control=lambda p: controls["status"](p, "inc-fsmonitor")
             and controls["helper"](p, "inc-helper") and controls["url"](p))
        case("credential.helper",
             lambda p, cfg: cfg.append(("credential.helper", "!" + str(_script(box, "helper")))),
             control=lambda p: controls["helper"](p, "helper"))
        case("insteadOf", lambda p, cfg: cfg.extend([(f"url.{decoy}.insteadOf", "https://github.com/"),
                                                    (f"url.{decoy}.pushInsteadOf", "https://github.com/")]),
             control=controls["url"])
        case("pushurl", lambda p, cfg: cfg.append(("remote.origin.pushurl", str(decoy))),
             control=controls["pushurl"])
        case("http.proxy", lambda p, cfg: cfg.extend([("http.proxy", "http://127.0.0.1:9"),
                                                     ("https.proxy", "http://127.0.0.1:9")]),
             control=controls["proxy"])

        def everything(p, cfg):
            for key in ("core.pager", "core.editor", "core.askPass", "gpg.program", "diff.external",
                        "core.gitProxy", "sequence.editor", "uploadpack.packObjectsHook",
                        "core.fsmonitor", "core.sshCommand", "core.alternateRefsCommand"):
                cfg.append((key, str(_script(box, "x-" + key))))
            cfg.append(("filter.x.process", str(_script(box, "x-filter"))))
            cfg.append(("filter.x.smudge", str(_script(box, "x-smudge"))))
            (p.common / "info").mkdir(exist_ok=True)
            (p.common / "info" / "attributes").write_text("* filter=x\n")
            cfg.append(("protocol.allow", "always"))
            cfg.append(("protocol.ext.allow", "always"))
        case("every other exec key", everything,
             control=lambda p: controls["status"](p, "x-core.fsmonitor"))


def identity_checks():
    """The owner's identity reaches Codex's environment, and nothing else does."""
    from jarvis.v2.providers import codex_config
    with Box() as box:
        codex_config._IDENTITY = None
        try:
            env = codex_config.clean_env(box.root / "codex-home")
            eq({k: env.get(k) for k in IDENTITY}, {
                "GIT_AUTHOR_NAME": "Owner Name", "GIT_AUTHOR_EMAIL": "owner@example.invalid",
                "GIT_COMMITTER_NAME": "Owner Name", "GIT_COMMITTER_EMAIL": "owner@example.invalid"},
                "the owner's global identity")
            eq(box.markers(), [], "reading it ran no helper")
        finally:
            codex_config._IDENTITY = None


SECTIONS = (unit_checks, session_tool_checks, push_checks, refusal_checks, gh_checks,
            env_checks, malicious_checks, identity_checks)


def main():
    for section in SECTIONS:
        if ONLY and section.__name__ not in ONLY:
            continue
        print(f"-- {section.__name__}")
        section()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} chat git checks FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"all chat git checks passed ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
