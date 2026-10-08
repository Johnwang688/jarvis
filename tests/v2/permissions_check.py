"""Free synthetic checks for WP5 — the five layers, always-ask, the broker and
the escape hatch. No network, no CLI process, no credential ever printed.

Every path this suite touches is redirected at a temp directory first:
`config.ALLOWLIST_PATH`, `config.MODELS_PATH`, `config.V2_DATA_DIR`,
`config.V2_ALWAYS_ASK` and `$HOME` (the credential directories are spelled
`~/.ssh` and friends, so a suite that did not move HOME would be asserting
against the owner's real ones). The v1 lesson, in both directions: a suite must
not write live machine state — `apt` once landed on the owner's real allowlist
— and must not *read* it either, or whatever the owner has configured decides
what the test asserts.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from jarvis import config, permissions as v1_permissions, rules

CHECKS = 0
FAILURES: list[str] = []


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


class Sandbox:
    """Temp home + temp config paths, restored on exit."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="wp5-")
        self.root = Path(self.tmp)
        self.home = self.root / "home"
        (self.home / ".config" / "jarvis").mkdir(parents=True)
        self._saved = {
            "ALLOWLIST_PATH": config.ALLOWLIST_PATH,
            "MODELS_PATH": config.MODELS_PATH,
            "V2_DATA_DIR": config.V2_DATA_DIR,
            "V2_ALWAYS_ASK": config.V2_ALWAYS_ASK,
        }
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        config.ALLOWLIST_PATH = self.home / ".config" / "jarvis" / "allowlist.json"
        config.MODELS_PATH = self.home / ".config" / "jarvis" / "models.json"
        config.V2_DATA_DIR = self.root / "v2data"
        config.V2_ALWAYS_ASK = self.home / ".config" / "jarvis" / "always-ask.json"
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(config, name, value)
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    @property
    def decisions(self):
        path = config.V2_DATA_DIR / "decisions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Asker:
    """A human-backed surface that answers on cue and records what it was shown."""

    human_backed = True

    def __init__(self, decision=None):
        from jarvis.v2.provider import Decision
        self.decision = decision if decision is not None else Decision.ALLOW
        self.seen = []

    def ask(self, request):
        self.seen.append(request)
        return self.decision


def context(brief, project=None, task=None):
    from jarvis.v2.permissions import PermitContext
    return PermitContext(thread_id="abcd1234", brief=brief, provider="claude",
                         project=project, task=task)


def brief_for(profile, always_ask=(), task_id=None):
    from jarvis.v2.model import Role
    from jarvis.v2.provider import Brief
    return Brief(role=Role.IMPLEMENTER, cwd="/tmp", profile=profile,
                 always_ask=list(always_ask), task_id=task_id)


def bash(command):
    return "Bash", {"command": command}


# --------------------------------------------------------------------------
# 1. the decision matrix


DENY_COMMANDS = [
    "sudo rm -rf /",
    "sudo apt install nginx",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda1",
    "rm -rf /",
    "chmod -R 777 /",
    "git commit -m ok && sudo reboot",       # worst segment wins
    "cat .env",                              # secrets layer, though rules ALLOW it
    "grep KEY .env.local",
    "cat ~/.config/jarvis/google_token.json",
]

ALWAYS_ASK_COMMANDS = [
    ("vercel --prod", "vercel-prod"),
    ("vercel deploy --prod", "vercel-prod"),
    ("vercel build --production", "vercel-prod"),
    ("gh release create v1.0 --notes x", "gh-release"),
    ("git push origin main", "git-push-protected"),
    ("git push origin HEAD:main", "git-push-protected"),
    ("git push origin +master", "git-push-protected"),
    ("git push origin refs/heads/release-2", "git-push-protected"),
    ("git push --force origin feature", "git-push-force"),
    ("git push -f origin topic", "git-push-force"),
    ("DATABASE_URL=postgres://u@prod-db/app alembic upgrade head", "db-migrate-prod"),
    ("flyway -url=jdbc:postgresql://prod/app migrate", "db-migrate-prod"),
    ("npx prisma migrate deploy", "db-migrate-deploy"),
    ("supabase db push", "db-migrate-deploy"),
    ("stripe charges create --amount 500", "payments-stripe"),
    ("vercel domains buy example.com", "payments-domains"),
    ("gh auth login", "credentials-login"),
    ("vercel login", "credentials-login"),
    ("aws configure set region us-east-1", "credentials-login"),
    ("ssh-keygen -t ed25519 -f id_new", "credentials-login"),
    ("cat ~/.ssh/config", "credentials-path"),
    ("cp key.pem ~/.aws/credentials", "credentials-path"),
]

# The other half of every always-ask rule: the ordinary spelling must not ask.
ORDINARY_COMMANDS = [
    "vercel",
    "vercel ls",
    "gh release list",
    "gh release view v1.0",
    "git push origin feature",
    "git push origin my-branch",
    "alembic upgrade head",
    "rails db:migrate",
    "dbmate up",
    "npm install",
    "npm test",
    "make build",
    "mkdir build",
    "ls -la",
    "rm draft.txt",
    "pytest -x 2>&1",
    "docker build .",
    "cat README.md",
]


def matrix_checks():
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import (L_ALWAYS_ASK, L_DENY, L_HUMAN,
                                       L_JARVIS_ALLOW, L_REVIEWER, build_permit)
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief_for(P.AUTO)), asker)

        for command in DENY_COMMANDS:
            before = len(asker.seen)
            decision = permit(*bash(command), brief_for(P.AUTO))
            eq(decision, Decision.DENY, f"layer 1 must deny: {command}")
            eq(len(asker.seen), before, f"a DENY is never asked of anyone: {command}")

        for command, rule_id in ALWAYS_ASK_COMMANDS:
            before = len(asker.seen)
            decision = permit(*bash(command), brief_for(P.AUTO))
            eq(decision, Decision.ALLOW, f"always-ask + yes = allow: {command}")
            ok(len(asker.seen) == before + 1, f"always-ask must ask: {command}")
            if asker.seen[-1:]:
                ok(rule_id in asker.seen[-1].reason,
                   f"{command} should match rule {rule_id}, got {asker.seen[-1].reason!r}")
                eq(asker.seen[-1].command, command,
                   "the whole command travels with the request")

        for command in ORDINARY_COMMANDS:
            before = len(asker.seen)
            decision = permit(*bash(command), brief_for(P.AUTO))
            eq(len(asker.seen), before, f"ordinary work must not ask: {command}")
            eq(decision, Decision.ALLOW, f"ordinary work is allowed under AUTO: {command}")

        # Layer 4 vs layer 3: which one answered, on the same profile.
        layers = {}
        for command in ("npm test", "make build", "ls -la", "rm draft.txt", "docker build ."):
            permit(*bash(command), brief_for(P.AUTO))
            layers[command] = box.decisions[-1]["layer"]
        eq(layers["npm test"], L_JARVIS_ALLOW, "a rules ALLOW is layer 4")
        eq(layers["rm draft.txt"], L_REVIEWER, "a rules ASK under AUTO falls to the reviewer")
        eq(layers["docker build ."], L_REVIEWER, "an unrecognised command defers to the reviewer")

        # Layer 2 beats layer 4 even when the rules would auto-approve.
        eq(rules.decide("npx prisma migrate deploy").decision, rules.ALLOW,
           "precondition: the rules would auto-approve this migration")
        permit(*bash("npx prisma migrate deploy"), brief_for(P.AUTO))
        eq(box.decisions[-1]["layer"], L_ALWAYS_ASK,
           "always-ask outranks a rules ALLOW")

        # Layer 1 beats layer 4 too.
        eq(rules.decide("cat .env").decision, rules.ALLOW,
           "precondition: the rules alone would auto-approve `cat .env`")
        permit(*bash("cat .env"), brief_for(P.AUTO))
        eq(box.decisions[-1]["layer"], L_DENY, "the secrets layer outranks a rules ALLOW")

        # Profiles: the same commands, the other two ways.
        ask_permit = build_permit(context(brief_for(P.ASK)), asker)
        strict_permit = build_permit(context(brief_for(P.STRICT)), asker)
        before = len(asker.seen)
        eq(ask_permit(*bash("rm draft.txt"), brief_for(P.ASK)), Decision.ALLOW,
           "ASK profile: the owner said yes")
        eq(len(asker.seen), before + 1, "ASK profile asks where AUTO deferred")
        eq(box.decisions[-1]["layer"], L_HUMAN, "the ASK profile answers at layer 5")

        before = len(asker.seen)
        eq(strict_permit(*bash("rm draft.txt"), brief_for(P.STRICT)), Decision.DENY,
           "STRICT profile denies rather than asking")
        eq(len(asker.seen), before, "STRICT profile does not ask")
        eq(strict_permit(*bash("sudo rm -rf /"), brief_for(P.STRICT)), Decision.DENY,
           "STRICT still denies at layer 1")

        # A deny answer is carried through, not softened.
        no = Asker(Decision.DENY)
        no_permit = build_permit(context(brief_for(P.AUTO)), no)
        eq(no_permit(*bash("git push origin main"), brief_for(P.AUTO)), Decision.DENY,
           "the owner's no is the answer")

        total = len(DENY_COMMANDS) + len(ALWAYS_ASK_COMMANDS) + len(ORDINARY_COMMANDS)
        ok(total >= 40, f"the matrix must cover at least 40 commands (has {total})")


# --------------------------------------------------------------------------
# 2. always-ask: additions, tools, and the seam R8 still needs


def always_ask_checks():
    from jarvis.v2.model import PermissionProfile as P, Project
    from jarvis.v2.permissions import always_ask_for, always_ask_match, build_permit
    from jarvis.v2.provider import Decision

    with Sandbox():
        asker = Asker(Decision.ALLOW)
        brief = brief_for(P.AUTO)

        # The outward tools ask by name, whatever their arguments.
        for tool in ("gmail_send", "discord_send"):
            permit = build_permit(context(brief), asker)
            before = len(asker.seen)
            eq(permit(tool, {"to": "someone@example.com"}, brief), Decision.ALLOW,
               f"{tool} is always-ask")
            eq(len(asker.seen), before + 1, f"{tool} must reach the owner")
        ok(always_ask_match("gmail_read", {}) is None, "reading mail is not always-ask")

        # Project additions.
        project = Project(id="p0000000", name="x", root="/tmp",
                          always_ask=["terraform apply", "helm"])
        permit = build_permit(context(brief, project=project), asker)
        before = len(asker.seen)
        permit(*bash("terraform apply -auto-approve"), brief)
        eq(len(asker.seen), before + 1, "a project addition is enforced")
        before = len(asker.seen)
        permit(*bash("terraform plan"), brief)
        eq(len(asker.seen), before, "a project addition matches the phrase, not the stem alone")
        before = len(asker.seen)
        permit(*bash("helm upgrade release ."), brief)
        eq(len(asker.seen), before + 1, "a one-word addition matches the command")

        # Brief additions (the provider's own copy of the list).
        wide = brief_for(P.AUTO, always_ask=["pnpm publish"])
        permit = build_permit(context(wide), asker)
        before = len(asker.seen)
        permit(*bash("pnpm publish --access public"), wide)
        eq(len(asker.seen), before + 1, "a brief addition is enforced")

        # The file is additions-only, and a broken file costs only the additions.
        config.V2_ALWAYS_ASK.write_text(json.dumps(["kubectl delete"]))
        ok(any(e == "kubectl delete" for e in always_ask_for(brief)),
           "the additions file is read")
        ok(any(isinstance(e, dict) and e.get("id") == "vercel-prod" for e in always_ask_for(brief)),
           "the shipped defaults survive an additions file")
        config.V2_ALWAYS_ASK.write_text("[]")
        ok(any(isinstance(e, dict) and e.get("id") == "outward-send" for e in always_ask_for(brief)),
           "an EMPTY additions file cannot disarm the shipped list")
        config.V2_ALWAYS_ASK.write_text("{ not json")
        ok(len(always_ask_for(brief)) >= len(config.V2_ALWAYS_ASK_DEFAULTS),
           "a corrupt additions file cannot disarm the shipped list")
        config.V2_ALWAYS_ASK.unlink()

        # Every shipped default carries a reason a reader can act on.
        for rule in config.V2_ALWAYS_ASK_DEFAULTS:
            ok(bool(rule.get("id")) and bool(rule.get("reason")),
               f"every shipped always-ask rule needs an id and a reason: {rule}")

        # R8: the seam exists and is one list, not two.
        ok(callable(always_ask_for), "always_ask_for is the single source for the Codex half (R8)")


# --------------------------------------------------------------------------
# 3. file tools


def file_deny_checks():
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import denied_file, file_targets, build_permit
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        repo = config.REPO_ROOT
        brief = brief_for(P.AUTO)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)

        denied_targets = [
            str(repo / "jarvis" / "permissions.py"),         # SELF_PROTECTED
            str(repo / "jarvis" / "rules.py"),
            str(repo / "jarvis" / "tools" / "secrets.py"),
            str(repo / "jarvis" / "face" / "approvals.py"),
            str(config.ALLOWLIST_PATH),                       # the gate's data
            str(config.MODELS_PATH),
            str(config.ALLOWLIST_PATH.with_name("routing.json")),
            str(config.ALLOWLIST_PATH.with_name("models.json")),
            "~/.config/jarvis/allowlist.json",                # spelled with a ~
            str(repo / "jarvis" / "tools" / ".." / "rules.py"),   # spelled with a ..
            "/tmp/somewhere/.env",
            "/tmp/somewhere/google_token.json",
            os.path.relpath(repo / "jarvis" / "permissions.py"),  # spelled relative to cwd
            os.path.relpath(repo / "jarvis" / "tools" / ".." / "permissions.py"),
        ]
        for target in denied_targets:
            ok(denied_file(target) is not None, f"a write to {target} must be refused")

        for target in (str(repo / "jarvis" / "agent.py"), "/tmp/scratch/app.py",
                       str(repo / "README.md"), "/tmp/.env.example"):
            ok(denied_file(target) is None, f"an ordinary write must not be refused: {target}")

        # Every provider's spelling of "the file I am writing".
        spellings = [
            ("Write", {"file_path": str(config.ALLOWLIST_PATH), "content": "[]"}),
            ("Edit", {"file_path": str(config.ALLOWLIST_PATH), "old_string": "a", "new_string": "b"}),
            ("NotebookEdit", {"notebook_path": str(config.ALLOWLIST_PATH)}),
            ("MultiEdit", {"edits": [{"file_path": str(config.ALLOWLIST_PATH)}]}),
            ("apply_patch", {"paths": ["/tmp/ok.py", str(config.ALLOWLIST_PATH)]}),
            ("write_file", {"path": str(config.ALLOWLIST_PATH), "content": "[]"}),
            ("edit_file", {"path": str(config.ALLOWLIST_PATH)}),
        ]
        for tool, args in spellings:
            before = len(asker.seen)
            eq(permit(tool, args, brief), Decision.DENY,
               f"{tool} must not reach the gate's own state file")
            eq(len(asker.seen), before, f"{tool}'s refusal is never put to the owner")
            ok(bool(file_targets(tool, args)), f"{tool}'s target must be found at all")

        # A command that writes the same file is refused too.
        eq(permit(*bash(f"echo '[]' > {config.ALLOWLIST_PATH}"), brief), Decision.DENY,
           "a redirect onto the allowlist is refused")
        eq(permit(*bash(f"tee {config.ALLOWLIST_PATH}"), brief), Decision.DENY,
           "a writing stem given the allowlist is refused")
        eq(box.decisions[-1]["layer"], "deny", "and it is refused at layer 1")

        # A write under a credential directory is always-ask, not a refusal.
        before = len(asker.seen)
        eq(permit("write_file", {"path": str(Path(os.environ["HOME"]) / ".ssh" / "config")}, brief),
           Decision.ALLOW, "a credential-directory write is approvable")
        eq(len(asker.seen), before + 1, "but it is always asked")


# --------------------------------------------------------------------------
# 4. layer 4 is human-backed only


def human_backed_checks():
    from jarvis.v2.approvals import DenyAll, v1_request
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        brief = brief_for(P.AUTO)
        allowed = "npm test"
        eq(rules.decide(allowed).decision, rules.ALLOW, "precondition: an ALLOW command")

        human = build_permit(context(brief), Asker(Decision.ALLOW))
        eq(human(*bash(allowed), brief), Decision.ALLOW, "a human-backed asker honours ALLOW")
        eq(box.decisions[-1]["layer"], "jarvis-allow", "and records layer 4")

        # The regression the v1 round nearly shipped: a deny-all approver must
        # not auto-approve an ALLOW command just because nobody is watching.
        blind = DenyAll()
        ok(blind.human_backed is False, "DenyAll is not human-backed")
        deny_permit = build_permit(context(brief), blind)
        eq(deny_permit(*bash(allowed), brief), Decision.ALLOW,
           "under AUTO a non-human-backed asker still defers to the reviewer")
        eq(box.decisions[-1]["layer"], "reviewer",
           "but NOT at layer 4 — a deny-all asker never auto-approves")

        strict = brief_for(P.STRICT)
        strict_permit = build_permit(context(strict), blind)
        eq(strict_permit(*bash(allowed), strict), Decision.DENY,
           "strict + deny-all: an ALLOW command still does not run")

        # The owner's persistent allowlist, honoured only for a human.
        config.ALLOWLIST_PATH.write_text(json.dumps([{"tool": "run_command", "prefix": "pnpm"}]))
        human_cmd = build_permit(context(brief), Asker(Decision.DENY))
        eq(human_cmd("run_command", {"command": "pnpm build"}, brief), Decision.ALLOW,
           "an allowlisted command runs without asking")
        eq(box.decisions[-1]["layer"], "jarvis-allow", "through layer 4")
        # The same entry covers the same command spelled as the providers spell it.
        eq(human_cmd("Bash", {"command": "pnpm build"}, brief), Decision.ALLOW,
           "a Claude Bash call maps onto the same v1 entry")
        eq(box.decisions[-1]["layer"], "jarvis-allow", "also through layer 4")
        eq(human_cmd("shell", {"command": ["pnpm", "build"]}, brief), Decision.ALLOW,
           "and so does a Codex argv array")
        blind_cmd = build_permit(context(brief), DenyAll())
        blind_cmd("run_command", {"command": "pnpm build"}, brief)
        eq(box.decisions[-1]["layer"], "reviewer",
           "the same entry buys a deny-all asker nothing")
        # And the allowlist never reaches past layer 1.
        eq(human_cmd("run_command", {"command": "pnpm build && sudo reboot"}, brief),
           Decision.DENY, "an allowlist entry cannot authorise a DENY riding beside it")

        # An ALWAYS on a provider's command tool must not mint a blanket grant.
        eq(v1_request("Bash", {"command": "pnpm build"}),
           ("run_command", {"command": "pnpm build"}),
           "a Bash request is allowlisted as a command, by stem")
        eq(v1_permissions.entry_for(*v1_request("Bash", {"command": "pnpm build"})),
           {"tool": "run_command", "prefix": "pnpm"},
           "so an ALWAYS mints one stem, never `{tool: Bash}` over every command")
        raised = None
        try:
            v1_permissions.entry_for(*v1_request("Bash", {"command": "a && b"}))
        except v1_permissions.NotAllowlistable as exc:
            raised = exc
        ok(raised is not None, "and a compound Bash command still mints nothing")
        # A hand-written blanket entry under the provider's own name buys nothing.
        config.ALLOWLIST_PATH.write_text(json.dumps([{"tool": "Bash"}]))
        blanket = build_permit(context(brief), Asker(Decision.DENY))
        blanket("Bash", {"command": "rm -rf ~/work"}, brief)
        eq(box.decisions[-1]["layer"], "reviewer",
           "a hand-written {tool: Bash} entry authorises nothing")
        config.ALLOWLIST_PATH.unlink()


# --------------------------------------------------------------------------
# 5. the decision log


def log_checks():
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        brief = brief_for(P.AUTO, task_id="ff00ff00")
        permit = build_permit(context(brief), Asker(Decision.ALLOW))
        permit(*bash("ls -la"), brief)
        permit(*bash("git push origin main"), brief)
        permit(*bash("sudo rm -rf /"), brief)
        rows = box.decisions
        eq(len(rows), 3, "one record per call")
        for row in rows:
            for field in ("tool", "args_digest", "layer", "decision", "thread_id",
                          "task_id", "provider", "at"):
                ok(field in row, f"the decision record needs a {field}")
            eq(row["task_id"], "ff00ff00", "the record is attributed to the task")
            eq(row["provider"], "claude", "and to the provider")
        eq(rows[-1]["decision"], "deny", "the denial is recorded as one")
        ok(rows[0]["args_digest"] != rows[1]["args_digest"],
           "different arguments digest differently")
        ok("rm -rf" in (rows[-1]["command"] or ""), "the command is kept for the audit")

        # A log that cannot be written is not a reason a decision fails.
        config.V2_DATA_DIR.write_text  # noqa: B018 - documentation of intent
        broken = Path(box.root) / "not-a-dir"
        broken.write_text("file, not a directory")
        saved, config.V2_DATA_DIR = config.V2_DATA_DIR, broken / "nested"
        try:
            eq(permit(*bash("ls -la"), brief), Decision.ALLOW,
               "an unwritable decision log must not break the gate")
        finally:
            config.V2_DATA_DIR = saved


# --------------------------------------------------------------------------
# 6. PendingApprovals


def approvals_checks():
    from jarvis.v2.approvals import (ApprovalRequest, LOCAL_TIMEOUT_S,
                                     PendingApprovals, REMOTE_TIMEOUT_S, label)
    from jarvis.v2.provider import Decision

    with Sandbox():
        requested, resolved = [], []
        broker = PendingApprovals(on_request=requested.append,
                                  on_resolve=lambda r, d, why: resolved.append((r, d, why)))
        ok(broker.human_backed is True, "the broker is human-backed")
        eq(broker.timeout_s, LOCAL_TIMEOUT_S, "a local broker waits two minutes")
        eq(PendingApprovals(remote=True).timeout_s, REMOTE_TIMEOUT_S,
           "a remote broker waits ten (you have to get your phone out)")

        request = ApprovalRequest(tool="Bash", args={"command": "git push origin main"},
                                  command="git push origin main", reason="always-ask",
                                  origin=label("ab12cd34", "ship the thing"))
        answer = []
        worker = threading.Thread(target=lambda: answer.append(broker.ask(request)))
        worker.start()
        deadline = time.monotonic() + 3
        while not requested and time.monotonic() < deadline:
            time.sleep(0.005)
        eq(len(requested), 1, "the request is announced")
        ok(len(request.req_id) >= 12, "ids are 72-bit tokens, not counters")
        eq(len(request.code), 4, "each request carries a 4-character code")
        eq(requested[0].command, "git push origin main",
           "the event carries the entire command")
        eq(requested[0].origin, 'task ab12cd34 "ship the thing"',
           "the origin is the sanitized task label")
        eq(len(broker.pending()), 1, "it is listed while open")

        broker.resolve(request.req_id, Decision.ALLOW)
        worker.join(3)
        eq(answer, [Decision.ALLOW], "the blocked asker wakes with the answer")
        eq(len(resolved), 1, "the resolution is announced")
        eq(broker.pending(), [], "and it leaves the pending list")

        raised = None
        try:
            broker.resolve(request.req_id, Decision.DENY)
        except ValueError as exc:
            raised = exc
        ok(raised is not None, "a second resolve is refused (one-shot ids)")

        # Timeout denies.
        quick = PendingApprovals(timeout_s=0.05)
        eq(quick.ask(ApprovalRequest(tool="Bash")), Decision.DENY, "timeout denies")

        # Shutdown releases waiters with a denial.
        closing = PendingApprovals()
        held = []
        thread = threading.Thread(target=lambda: held.append(closing.ask(ApprovalRequest(tool="Bash"))))
        thread.start()
        deadline = time.monotonic() + 3
        while not closing.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        closing.shutdown()
        thread.join(3)
        eq(held, [Decision.DENY], "shutdown denies every waiter")
        eq(closing.ask(ApprovalRequest(tool="Bash")), Decision.DENY,
           "and a closed broker denies immediately")

        # An announcement that raises is nowhere-to-ask, which denies.
        def explode(_request):
            raise RuntimeError("no surface")
        eq(PendingApprovals(on_request=explode).ask(ApprovalRequest(tool="Bash")),
           Decision.DENY, "a surface that cannot be told grants nothing")

        # The code answers exactly one open request.
        coded = PendingApprovals(timeout_s=2)
        first = ApprovalRequest(tool="Bash", args={"command": "ls"})
        second = ApprovalRequest(tool="Bash", args={"command": "pwd"})
        results = {}
        for name, item in (("first", first), ("second", second)):
            threading.Thread(target=lambda n=name, i=item: results.__setitem__(n, coded.ask(i))).start()
        deadline = time.monotonic() + 3
        while len(coded.pending()) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        eq(len({r.code for r in coded.pending()}), 2, "open codes are distinct")
        coded.resolve_code(first.code, Decision.ALLOW)
        spent = None
        try:
            coded.resolve_code(first.code, Decision.ALLOW)
        except ValueError as exc:
            spent = exc
        ok(spent is not None, "a spent code is dead")
        coded.resolve_code(second.code, Decision.DENY)
        deadline = time.monotonic() + 3
        while len(results) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        eq(results.get("first"), Decision.ALLOW, "the code resolved its own request")
        eq(results.get("second"), Decision.DENY, "and the other one kept its answer")

        # `always` mints an entry through v1 entry_for -- and only when it can.
        config.ALLOWLIST_PATH.write_text("[]")
        minting = PendingApprovals(timeout_s=2)
        simple = ApprovalRequest(tool="run_command", args={"command": "pnpm build"})
        got = []
        threading.Thread(target=lambda: got.append(minting.ask(simple))).start()
        deadline = time.monotonic() + 3
        while not minting.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        entry = minting.resolve(simple.req_id, Decision.ALLOW, always=True)
        eq(entry, {"tool": "run_command", "prefix": "pnpm"}, "an always writes the entry")
        eq(json.loads(config.ALLOWLIST_PATH.read_text()),
           [{"tool": "run_command", "prefix": "pnpm"}], "and persists it")

        compound = ApprovalRequest(tool="run_command", args={"command": "pnpm build && npm test"})
        got2 = []
        threading.Thread(target=lambda: got2.append(minting.ask(compound))).start()
        deadline = time.monotonic() + 3
        while not minting.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        entry = minting.resolve(compound.req_id, Decision.ALLOW, always=True)
        ok(entry is None, "a compound command mints no standing rule")
        deadline = time.monotonic() + 3
        while len(got2) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        eq(got2, [Decision.ALLOW], "but the approval still stands (v1's rule)")
        eq(len(json.loads(config.ALLOWLIST_PATH.read_text())), 1,
           "and nothing was added to the allowlist")

        # The label sanitizer: model-chosen text cannot fake a second line.
        dirty = label("ab12cd34", "line one\nline `two`" + "x" * 200)
        ok("\n" not in dirty and "`" not in dirty, "the origin label is sanitized")
        ok(len(dirty) < 120, "and capped")


# --------------------------------------------------------------------------
# 7. the daemon routes


class PermitProvider:
    """A fake provider whose fake hook calls `permit`, as the real ones do."""

    def __init__(self):
        from jarvis.v2.model import ProviderName
        self.name = ProviderName.FAST
        self.calls = []
        self.messages = []

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        from jarvis.v2.provider import SessionHandle
        return SessionHandle(thread.id, self.name, "fake:" + thread.id,
                             {"permit": permit, "brief": brief})

    resume = start

    def usage(self, handle):
        from jarvis.v2.provider import Usage
        return Usage()

    def send(self, handle, message):
        from jarvis.v2.provider import Event, EventKind as K
        self.messages.append(message)
        yield Event(K.TURN_STARTED, handle.thread_id)
        if message.text.startswith("run:"):
            command = message.text[4:]
            decision = handle.native["permit"]("Bash", {"command": command},
                                               handle.native["brief"])
            self.calls.append((command, decision))
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def answer(self, handle, req_id, decision):
        raise ValueError("unknown request")

    def close(self, handle):
        pass


def http_json(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        conn.close()


def daemon_checks():
    from jarvis.v2 import daemon as daemon_mod
    from jarvis.v2.model import PermissionProfile as P, ProviderName, Role
    from jarvis.v2.provider import Brief, Decision, UserMessage
    from jarvis.v2.stores import Stores

    with Sandbox() as box:
        provider = PermitProvider()
        daemon = daemon_mod.Daemon(Stores(config.V2_DATA_DIR), {ProviderName.FAST: provider},
                                   None, 0)
        daemon.start()
        try:
            ok(daemon.permit_factory == daemon._permit,
               "the daemon builds the WP5 policy when no factory is injected")
            project_root = box.root / "project"
            project_root.mkdir()
            status, project = http_json(daemon.port, "POST", "/projects",
                                        {"name": "p", "root": str(project_root)})
            eq(status, 201, "project created")
            brief = Brief(role=Role.IMPLEMENTER, cwd=str(project_root), profile=P.AUTO)
            thread = daemon.open_thread(project["id"], Role.IMPLEMENTER,
                                        ProviderName.FAST, brief)

            # A DENY never reaches /approvals.
            daemon.send(thread.id, UserMessage("run:sudo rm -rf /"))
            deadline = time.monotonic() + 3
            while not provider.calls and time.monotonic() < deadline:
                time.sleep(0.005)
            eq(provider.calls[-1][1], Decision.DENY, "the hook's DENY came back")
            status, pending = http_json(daemon.port, "GET", "/approvals")
            eq((status, pending), (200, []), "a DENY is never queued for the owner")

            # An always-ask command queues, and POST /approvals resolves it.
            seen = []
            subscription = daemon.bus.subscribe(
                lambda r: r.get("kind", "").startswith("approval_"))
            worker = threading.Thread(
                target=lambda: daemon.send(thread.id, UserMessage("run:git push origin main")))
            worker.start()
            deadline = time.monotonic() + 5
            queued = []
            while time.monotonic() < deadline:
                status, queued = http_json(daemon.port, "GET", "/approvals")
                if queued:
                    break
                time.sleep(0.01)
            eq(len(queued), 1, "the always-ask command is queued for the owner")
            eq(queued[0]["command"], "git push origin main",
               "the queue shows the entire command")
            status, body = http_json(daemon.port, "POST", "/approvals/" + queued[0]["req_id"],
                                     {"decision": "allow"})
            eq(status, 200, "POST /approvals answers it")
            deadline = time.monotonic() + 5
            while len(provider.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            eq(provider.calls[-1], ("git push origin main", Decision.ALLOW),
               "and the answer reaches the blocked hook")
            while True:
                try:
                    seen.append(subscription.get(timeout=0.2)["kind"])
                except Exception:
                    break
            ok("approval_requested" in seen and "approval_resolved" in seen,
               "both approval events are published on the bus")

            status, _ = http_json(daemon.port, "POST", "/approvals/" + queued[0]["req_id"],
                                  {"decision": "allow"})
            eq(status, 404, "an answered request cannot be answered again")
            status, _ = http_json(daemon.port, "POST", "/approvals/nope", {"decision": "sideways"})
            ok(status in (400, 404), "a nonsense decision is refused")
        finally:
            daemon.stop()


# --------------------------------------------------------------------------
# 8. the escape hatch


class HatchDaemon:
    """Just enough daemon for the hatch: a bus, stores and a send sink."""

    def __init__(self, stores):
        from jarvis.v2.bus import EventBus
        self.stores = stores
        self.bus = EventBus()
        self.sent = []

    def send(self, thread_id, message):
        self.sent.append((thread_id, message))


def hatch_checks():
    from jarvis.v2.approvals import PendingApprovals
    from jarvis.v2.hatch import EscapeHatch, worker_env
    from jarvis.v2.model import ProviderName, Role
    from jarvis.v2.provider import Decision
    from jarvis.v2.stores import Stores

    with Sandbox() as box:
        stores = Stores(config.V2_DATA_DIR)
        project_root = box.root / "proj"
        project_root.mkdir()
        worktree = box.root / "worktree"
        worktree.mkdir()
        project = stores.projects.create("p", str(project_root))
        task = stores.tasks.create(project.id, "ship the widget")
        task.worktree = str(worktree)
        stores.tasks.save(task)
        thread = stores.threads.create(project.id, Role.IMPLEMENTER, ProviderName.CLAUDE,
                                       task_id=task.id)

        def hatch_with(answer, timeout=2.0):
            broker = PendingApprovals(timeout_s=timeout)
            if answer is not None:
                def respond(request):
                    threading.Thread(
                        target=lambda: broker.resolve(request.req_id, answer),
                        daemon=True).start()
                broker._on_request = respond
            daemon = HatchDaemon(stores)
            return daemon, EscapeHatch(daemon, broker), broker

        def declined(command):
            return {"kind": "reviewer_declined", "thread_id": thread.id,
                    "data": {"tool": "Bash", "command": command,
                             "reason": "denied by the classifier"}}

        # ALLOW -> runs in the worktree, output reaches the thread as owner-ran.
        daemon, hatch, broker = hatch_with(Decision.ALLOW)
        hatch.handle(declined("pwd && touch ran.txt"))
        eq(len(daemon.sent), 1, "the result reaches the worker session")
        thread_id, message = daemon.sent[0]
        eq(thread_id, thread.id, "on the thread that was declined")
        eq(message.origin, "owner-ran", "tagged as something the owner ran")
        ok(message.text.startswith("[owner ran: pwd && touch ran.txt]"),
           f"the message names the exact command: {message.text[:80]!r}")
        ok(str(worktree) in message.text, "the command ran in the task worktree")
        ok((worktree / "ran.txt").exists(), "and really ran")
        ok("[exit 0]" in message.text, "the exit status is reported")
        rows = box.decisions
        ok(any(r["decision"] == "reviewer-declined-owner-ran" for r in rows),
           "the decision log records reviewer-declined-owner-ran")
        eq(json.loads(config.ALLOWLIST_PATH.read_text()) if config.ALLOWLIST_PATH.exists() else [],
           [], "the hatch never mints an allowlist entry")

        # The ask itself is marked un-allowlistable, and ALWAYS mints nothing.
        pinned = PendingApprovals(timeout_s=2)
        seen = []
        pinned._on_request = seen.append
        daemon2 = HatchDaemon(stores)
        hatch2 = EscapeHatch(daemon2, pinned)
        thread_runner = threading.Thread(target=lambda: hatch2.handle(declined("echo hi")),
                                         daemon=True)
        thread_runner.start()
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.005)
        eq(len(seen), 1, "the hatch asks through the broker")
        ok(seen[0].allowlistable is False,
           "a reviewer-declined command is never a standing rule")
        ok(seen[0].reason.startswith("reviewer declined: "),
           "the reason names the decline and the command")
        ok(str(task.id) in seen[0].origin, "the ask is attributed to the task")
        pinned.resolve(seen[0].req_id, Decision.ALLOW, always=True)
        thread_runner.join(3)
        eq(json.loads(config.ALLOWLIST_PATH.read_text()) if config.ALLOWLIST_PATH.exists() else [],
           [], "an ALWAYS through the hatch still mints nothing")

        # DENY -> not run, the worker is told.
        daemon, hatch, broker = hatch_with(Decision.DENY)
        hatch.handle(declined("touch denied.txt"))
        ok(not (worktree / "denied.txt").exists(), "a denied command does not run")
        eq(daemon.sent[0][1].origin, "system", "the note is a system message")
        eq(daemon.sent[0][1].text, "[owner declined to run: touch denied.txt]",
           "and says exactly what was declined")

        # A DENY-rule command is unreachable through the hatch even with a yes.
        daemon, hatch, broker = hatch_with(Decision.ALLOW)
        hatch.handle(declined("sudo touch /tmp/wp5-should-not-exist"))
        ok(not Path("/tmp/wp5-should-not-exist").exists(),
           "layer 1 is re-checked at the moment of running")
        ok(daemon.sent[0][1].text.startswith("[refused by Jarvis rules:"),
           "and the worker is told why")
        eq(daemon.sent[0][1].origin, "system", "a refusal is a system message")
        ok(any(r["decision"] == "reviewer-declined-refused" for r in box.decisions),
           "the refusal is logged")

        # Timeout -> not run.
        daemon, hatch, broker = hatch_with(None, timeout=0.05)
        hatch.handle(declined("touch timed-out.txt"))
        ok(not (worktree / "timed-out.txt").exists(), "a timed-out ask runs nothing")
        ok(daemon.sent[0][1].text.startswith("[owner declined to run:"),
           "a timeout reads as a decline")

        # Mid-turn declines wait for the turn to finish.
        daemon, hatch, broker = hatch_with(Decision.ALLOW)
        hatch.handle({"kind": "turn_started", "thread_id": thread.id, "data": {}})
        hatch.handle(declined("touch after-turn.txt"))
        eq(daemon.sent, [], "nothing is asked while the worker is mid-turn")
        hatch.handle({"kind": "turn_finished", "thread_id": thread.id, "data": {}})
        eq(len(daemon.sent), 1, "the ask lands once the turn is over")
        ok((worktree / "after-turn.txt").exists(), "and the command then runs")

        # The environment the command inherits carries no Jarvis credential.
        env = worker_env({"PATH": "/usr/bin", "OPENROUTER_API_KEY": "x",
                          "JARVIS_V2_DATA": "y", "ANTHROPIC_API_KEY": "z", "HOME": "/h"})
        eq(sorted(env), ["HOME", "PATH"], "the worker env is stripped of Jarvis secrets")

        # A decline with no command is not an offer to run something.
        daemon, hatch, broker = hatch_with(Decision.ALLOW)
        hatch.handle({"kind": "reviewer_declined", "thread_id": thread.id,
                      "data": {"tool": "Write", "command": None, "reason": "no"}})
        eq(daemon.sent, [], "a non-command decline has nothing to offer")


def discord_checks():
    """S1: no fast-path tool reaches Discord, and `/always` (typed, or the
    keyword) never mints a rule the request cannot honestly become."""
    from jarvis.v2.approvals import ApprovalRequest
    from jarvis.v2.discord.gateway import DiscordRouter
    from jarvis.v2.providers import fastpath

    ok("discord_" in fastpath.FORBIDDEN_PREFIXES, "discord_ is a forbidden fast-path prefix")
    ok(not [n for n in fastpath.FAST_TOOLS if n.startswith("discord_")],
       "no discord_ tool is on the fast path")
    with Sandbox():
        for command, expected in (("pnpm build", True), ("git status && rm -rf build", False),
                                  ("   ", False), ("cat $(ls)", False)):
            request = ApprovalRequest(tool="Bash", args={"command": command}, command=command)
            eq(DiscordRouter.allowlistable(request)[0], expected,
               f"/always allowed for {command!r}")
        hatch = ApprovalRequest(tool="Bash", args={"command": "pnpm build"},
                                command="pnpm build", allowlistable=False)
        eq(DiscordRouter.allowlistable(hatch)[0], False,
           "/always is refused for an escape-hatch request")
        ok(not config.ALLOWLIST_PATH.exists(), "checking allowlistability writes nothing")


def main():
    for section in (matrix_checks, always_ask_checks, file_deny_checks,
                    human_backed_checks, log_checks, approvals_checks,
                    daemon_checks, hatch_checks, discord_checks):
        print(f"-- {section.__name__}")
        section()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} v2 permissions checks FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"all v2 permissions checks passed ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
