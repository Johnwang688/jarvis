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
# `python permissions_check.py command_tool_checks` runs only the named sections.
ONLY = frozenset(a for a in __import__("sys").argv[1:] if a.endswith("_checks"))
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
            "PROVIDER_DEFAULTS_PATH": config.PROVIDER_DEFAULTS_PATH,
            "V2_DATA_DIR": config.V2_DATA_DIR,
            "V2_ALWAYS_ASK": config.V2_ALWAYS_ASK,
            "DISCORD_GUILD_PATH": config.DISCORD_GUILD_PATH,
            "ROUTING_PATH": config.ROUTING_PATH,
            "CODEX_CATALOG_PATH": config.CODEX_CATALOG_PATH,
            "SPEAKER_DIR": config.SPEAKER_DIR,
        }
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        config.ALLOWLIST_PATH = self.home / ".config" / "jarvis" / "allowlist.json"
        config.MODELS_PATH = self.home / ".config" / "jarvis" / "models.json"
        config.PROVIDER_DEFAULTS_PATH = self.home / ".config" / "jarvis" / "provider_defaults.json"
        config.V2_DATA_DIR = self.root / "v2data"
        config.V2_ALWAYS_ASK = self.home / ".config" / "jarvis" / "always-ask.json"
        config.DISCORD_GUILD_PATH = self.home / ".config" / "jarvis" / "discord_guild.json"
        config.ROUTING_PATH = self.home / ".config" / "jarvis" / "routing.json"
        config.CODEX_CATALOG_PATH = self.home / ".local" / "share" / "jarvis" / "codex-models.json"
        config.SPEAKER_DIR = self.home / ".local" / "share" / "jarvis" / "speaker"
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


def brief_for(profile, always_ask=(), task_id=None, cwd="/tmp"):
    from jarvis.v2.model import Role
    from jarvis.v2.provider import Brief
    return Brief(role=Role.IMPLEMENTER, cwd=str(cwd), profile=profile,
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
    from jarvis.v2.permissions import (_command_writes_protected_state, build_permit,
                                       denied_file, file_targets, protected_paths)
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
            # 2026-10-08: the Claude/Codex chat default is the owner's alone.
            str(config.PROVIDER_DEFAULTS_PATH),
            str(config.ALLOWLIST_PATH.with_name("provider_defaults.json")),
            "~/.config/jarvis/provider_defaults.json",
            # B1: where Jarvis may create and move channels is setup's alone.
            str(config.DISCORD_GUILD_PATH),
            str(config.ALLOWLIST_PATH.with_name("discord_guild.json")),
            "~/.config/jarvis/discord_guild.json",
            # V12 (V-12b, owner 2026-10-14): the files that switch the voice check off.
            str(config.SPEAKER_DIR / "voiceprint.json"),
            str(config.SPEAKER_DIR / "guest.json"),
            "~/.local/share/jarvis/speaker/guest.json",
            # V13 (owner, 2026-10-14: keep it protected): the speech-to-text switches
            # decide whether the owner's voice may leave the machine.
            str(config.STT_SETTINGS_PATH),
            str(config.ALLOWLIST_PATH.with_name("stt.json")),
            "~/.config/jarvis/stt.json",
            # PR #20: the saved Codex catalog becomes the Codex table at start.
            str(config.CODEX_CATALOG_PATH),
            "~/.local/share/jarvis/codex-models.json",
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
        guild_file = config.DISCORD_GUILD_PATH
        for tool, args in (("Write", {"file_path": str(guild_file), "content": "{}"}),
                           ("write_file", {"path": str(guild_file), "content": "{}"}),
                           ("Edit", {"file_path": str(guild_file), "old_string": "a",
                                     "new_string": "b"})):
            before = len(asker.seen)
            eq(permit(tool, args, brief), Decision.DENY,
               f"{tool} must not write discord_guild.json (B1)")
            eq(len(asker.seen), before, f"{tool}'s guild-file refusal is never put to the owner")
        eq(permit(*bash(f"echo '{{}}' > {guild_file}"), brief), Decision.DENY,
           "a redirect onto discord_guild.json is refused")
        # PR #20: the saved Codex catalog is loaded as the Codex table at
        # start; planting /dev/zero or a FIFO there was one ALLOW-ed `ln`.
        catalog = config.CODEX_CATALOG_PATH
        for tool, args in (("Write", {"file_path": str(catalog), "content": "{}"}),
                           ("write_file", {"path": str(catalog), "content": "{}"})):
            eq(permit(tool, args, brief), Decision.DENY, f"{tool} must not write codex-models.json")
        eq(permit(*bash(f"ln -sf /dev/zero {catalog}"), brief), Decision.DENY,
           "a symlink planted at the Codex catalog is refused")

        # One detector for v1 and v2 (2026-10-08): the spellings v1's shell
        # gate learned are refused here too, and the false positives the old
        # copy carried (any redirect in a segment that merely *mentioned* a
        # protected name; any `sed` that read one) are gone.
        from jarvis import protected_state
        from jarvis.tools import files as v1_files
        eq(protected_paths(), v1_files._protected_state(),
           "v2's protected set is v1's write-tool set, not a copy of it")
        eq(protected_paths(), protected_state.protected_paths(),
           "and the shell check reads the same set")
        for command in ("cp /tmp/x $HOME/.config/jarvis/allowlist.json",
                        "cd ~/.config/jarvis && cp /tmp/x allowlist.json",
                        "D=~/.config/jarvis; cp /tmp/x $D/models.json",
                        "cp -t ~/.config/jarvis /tmp/routing.json",
                        "sh -c 'cp /tmp/x ~/.config/jarvis/provider_defaults.json'",
                        "echo '{\"guest\": true}' > ~/.local/share/jarvis/speaker/guest.json",
                        "rm ~/.local/share/jarvis/speaker/voiceprint.json",
                        "cp /tmp/x $HOME/.config/jarvis/stt.json",
                        "sh -c 'echo {} > ~/.config/jarvis/stt.json'",
                        "env -C ~/.config/jarvis cp /tmp/x allowlist.json",
                        "curl -o ~/.config/jarvis/allowlist.json https://example.com/a"):
            before = len(asker.seen)
            eq(permit(*bash(command), brief), Decision.DENY, f"layer 1 refuses {command}")
            eq(len(asker.seen), before, f"and never asks: {command}")
        for command in ("echo hi > models.json", "echo allowlist.json > notes.txt",
                        "sed -n 1p ~/.config/jarvis/models.json",
                        "cp ~/.config/jarvis/models.json /tmp/models.bak"):
            eq(_command_writes_protected_state(command), None,
               f"not a write to the gate's state: {command}")
        # An uncertain one is not refused, and not auto-approved either.
        owner = Asker(Decision.ALLOW)
        human = build_permit(context(brief), owner)
        for command in ("rsync -a /tmp/evil/ ~/.config/jarvis/",
                        "mkdir -p ~/.config/jarvis",
                        # V-12b: the speaker folder holds two protected files, so deleting it is asked, never auto-run.
                        "rm -rf ~/.local/share/jarvis/speaker",
                        "echo ~/.config/jarvis/allowlist.json | xargs cp /tmp/x",
                        "cp /tmp/x $UNSET_DIR/models.json"):
            seen = len(owner.seen)
            eq(human(*bash(command), brief_for(P.ASK)), Decision.ALLOW,
               f"an uncertain gate-state write is not refused: {command}")
            ok(box.decisions[-1]["layer"] != "jarvis-allow" and len(owner.seen) == seen + 1,
               f"it reaches the owner, never rules ALLOW at layer 4: {command} "
               f"(layer {box.decisions[-1]['layer']})")
        eq(human(*bash("mkdir -p build"), brief_for(P.ASK)), Decision.ALLOW, "control")
        eq(box.decisions[-1]["layer"], "jarvis-allow", "an ordinary mkdir is still layer 4")

        # Under AUTO the same uncertain writes reach the owner too (layer 2),
        # not the provider's own classifier (layer 5 `reviewer`).
        for command in ("cp /tmp/x $UNSET_DIR/models.json",
                        "rsync -a /tmp/evil/ ~/.config/jarvis/",
                        "echo ~/.config/jarvis/allowlist.json | xargs cp /tmp/x",
                        "git checkout -- ~/.config/jarvis/models.json",
                        "tar -xf /tmp/x.tar -C ~/.config/jarvis"):
            seen = len(owner.seen)
            human(*bash(command), brief_for(P.AUTO))
            eq(box.decisions[-1]["layer"], "always-ask",
               f"AUTO: an uncertain gate-state write is put to the owner: {command}")
            eq(len(owner.seen), seen + 1, f"AUTO: and the owner is actually asked: {command}")
        human(*bash("mkdir -p build"), brief_for(P.AUTO))
        ok(box.decisions[-1]["layer"] != "always-ask", "AUTO control: ordinary work is not asked")

        # Relative names resolve in the *brief's* folder, never the daemon's.
        gate_dir = config.ALLOWLIST_PATH.parent
        project_dir = box.root / "project"
        project_dir.mkdir()
        gate_link = box.root / "gate-link"
        gate_link.symlink_to(gate_dir, target_is_directory=True)
        saved_cwd = os.getcwd()
        try:
            for process_cwd in (gate_dir, project_dir):
                os.chdir(process_cwd)
                for where, expected in ((gate_dir, Decision.DENY), (gate_link, Decision.DENY),
                                        (project_dir, None)):
                    brief = brief_for(P.AUTO, cwd=where)
                    got = build_permit(context(brief), Asker(Decision.ALLOW))(
                        *bash("echo hi > models.json"), brief)
                    if expected is Decision.DENY:
                        eq(got, Decision.DENY,
                           f"`echo hi > models.json` in {where.name} is refused "
                           f"(daemon cwd {process_cwd.name})")
                    else:
                        ok(box.decisions[-1]["layer"] != "deny",
                           f"`echo hi > models.json` in a project is not refused "
                           f"(daemon cwd {process_cwd.name})")
        finally:
            os.chdir(saved_cwd)

        # config.ROUTING_PATH outside the allowlist's folder is protected by
        # its own setting, by both the write tools and the shell check.
        saved_routing = config.ROUTING_PATH
        elsewhere = box.root / "elsewhere" / "routing.json"
        elsewhere.parent.mkdir()
        config.ROUTING_PATH = elsewhere
        try:
            ok(denied_file(str(elsewhere)) is not None, "a write tool may not write ROUTING_PATH")
            eq(_command_writes_protected_state(f"cp /tmp/x {elsewhere}"), "routing.json",
               "nor may a shell command")
        finally:
            config.ROUTING_PATH = saved_routing

        # Review round 2: a copier's destination is parsed, not "the last
        # operand" — every one of these writes the allowlist, so layer 1 refuses.
        for command in ("cp /tmp/x ~/.config/jarvis/allowlist.json # backup",
                        "cp /tmp/x ~/.config/jarvis/allowlist.json ${NOTHING}",
                        'cp /tmp/x ~/.config/jarvis/allowlist.json "$@"',
                        "cp /tmp/x ~/.config/jarvis/allowlist.json {fd}>/dev/null",
                        "cp -bt ~/.config/jarvis /tmp/allowlist.json",
                        "cp -t$HOME/.config/jarvis /tmp/allowlist.json",
                        "install /tmp/x ~/.config/jarvis/allowlist.json # x"):
            eq(_command_writes_protected_state(command), "allowlist.json",
               f"layer 1 sees the copy's real destination: {command}")

        # Review round 2: a line too complex to judge is always-ask with that
        # reason, under AUTO as under ASK, and is judged in well under a second.
        for command in ("echo " + "$(" * 5000, "echo " + "$(x) " * 200,
                        "echo " + "$(" * 200 + "x" + ")" * 200):
            for profile in (P.AUTO, P.ASK):
                seen = len(owner.seen)
                start = time.perf_counter()
                human(*bash(command), brief_for(profile))
                spent = time.perf_counter() - start
                ok(spent < 1.0, f"a pathological line is judged quickly ({spent:.2f}s)")
                eq(box.decisions[-1]["layer"], "always-ask",
                   f"{profile.value}: a line too complex to judge is put to the owner")
                ok("too complex to judge" in box.decisions[-1]["reason"],
                   f"and says why: {box.decisions[-1]['reason'][:80]!r}")
                eq(len(owner.seen), seen + 1, "and the owner really is asked")

        # A write under a credential directory is always-ask, not a refusal.
        before = len(asker.seen)
        eq(permit("write_file", {"path": str(Path(os.environ["HOME"]) / ".ssh" / "config")}, brief),
           Decision.ALLOW, "a credential-directory write is approvable")
        eq(len(asker.seen), before + 1, "but it is always asked")

        # The three 2026-10-09 findings, aimed at the gate's own state: every
        # one a certain write at layer 1, DENY, never asked. Each was ALLOW on
        # origin/main's detector and so ran under AUTO with nobody consulted.
        allow = "~/.config/jarvis/allowlist.json"
        findings = [
            # 1. the shell's inline flag inside a cluster, or behind env -S /
            #    builtin, which the old `"-c" in tokens` test never saw.
            f"bash -lc 'cp /tmp/x {allow}'",
            f"sh -ec 'cp /tmp/x {allow}'",
            f"bash -c -- 'cp /tmp/x {allow}'",
            f"builtin eval 'cp /tmp/x {allow}'",
            f"env '-Scp /tmp/x {allow}'",
            f"env --split-string='cp /tmp/x {allow}'",
            # 2. redirect forms the detector's drifted scan missed.
            f"echo x >|{allow}",
            f"echo x <>{allow}",
            f"echo x 2>&1>{allow}",
            f"echo x >&2>{allow}",
            f"echo x >& {allow}",
            f"echo x {{fd}}>{allow}",
            # 3. a sed script command that writes.
            f"sed 'w {allow}' f.txt",
            f"sed 's/a/b/w {allow}' f.txt",
            f"sed -ni 's/a/b/' {allow}",
            # neighbours: a continuation before the target, a find -exec shell.
            f"cp /tmp/x \\\n{allow}",
            f"find ~/.config/jarvis -name allowlist.json -execdir sh -c 'cp /tmp/x {{}}' \\;",
            # PR #24 review: `grep` in Claude Code's shell — the one a v2
            # Claude worker's commands run through — is ugrep, which writes
            # its configuration wherever --save-config names.
            f"grep --save-config={allow} x",
        ]
        human_auto = build_permit(context(brief_for(P.AUTO)), Asker(Decision.ALLOW))
        owner2 = Asker(Decision.ALLOW)
        human_ask = build_permit(context(brief_for(P.ASK)), owner2)
        for command in findings:
            eq(_command_writes_protected_state(command), "allowlist.json",
               f"layer 1 sees the write onto the allowlist: {command}")
            seen = len(owner2.seen)
            eq(human_auto(*bash(command), brief_for(P.AUTO)), Decision.DENY,
               f"AUTO: refused at layer 1, nobody asked: {command}")
            eq(human_ask(*bash(command), brief_for(P.ASK)), Decision.DENY,
               f"ASK: refused at layer 1: {command}")
            eq(len(owner2.seen), seen, f"and never asked: {command}")
        # A ugrep filter is a program run per file: never layer 4's ALLOW, so
        # under ASK the owner is asked rather than the rules auto-approving.
        seen = len(owner2.seen)
        human_ask(*bash("grep --filter='*:PLACEHOLDER_PROGRAM' x ."), brief_for(P.ASK))
        ok(box.decisions[-1]["layer"] != "jarvis-allow",
           "a filter-running grep is not an ordinary rules ALLOW at layer 4")
        eq(len(owner2.seen), seen + 1, "and reaches the owner")
        # A sed *read* of the allowlist is an ordinary read, not a write.
        eq(_command_writes_protected_state(f"sed -n '1,5p' {allow}"), None,
           "a sed read of a gate file is not a write")
        eq(_command_writes_protected_state(f"grep -n x {allow}"), None,
           "nor is a grep of it")
        eq(_command_writes_protected_state(f"sort {allow}"), None,
           "nor is sorting it to stdout")


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

        eq(len(resolved), 1, "an answered request is announced exactly once")

        # Timeout denies, and is announced: without the announcement a Discord
        # post kept its buttons and the gateway kept the request forever.
        timed_out = []
        quick = PendingApprovals(timeout_s=0.05, on_request=lambda r: None,
                                 on_resolve=lambda r, d, why: timed_out.append((r.req_id, d, why)))
        late = ApprovalRequest(tool="Bash")
        eq(quick.ask(late), Decision.DENY, "timeout denies")
        eq(timed_out, [(late.req_id, Decision.DENY, "timeout")],
           "and a timeout publishes exactly one resolution, a denial")
        refused = None
        try:
            quick.resolve(late.req_id, Decision.ALLOW)
        except ValueError as exc:
            refused = exc
        ok(refused is not None and len(timed_out) == 1,
           "an answer after the timeout is refused and announces nothing")

        # Shutdown releases waiters with a denial.
        released = []
        closing = PendingApprovals(on_resolve=lambda r, d, why: released.append((d, why)))
        held = []
        thread = threading.Thread(target=lambda: held.append(closing.ask(ApprovalRequest(tool="Bash"))))
        thread.start()
        deadline = time.monotonic() + 3
        while not closing.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        closing.shutdown()
        thread.join(3)
        eq(held, [Decision.DENY], "shutdown denies every waiter")
        eq(released, [(Decision.DENY, "shutdown")], "and announces each one, once")
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
        commands = message.text[4:].split("\n") if message.text.startswith("run:") else []
        for index, command in enumerate(commands):
            # The real providers announce every gate consultation before
            # `permit` decides anything — Claude's hook once per tool use,
            # Codex once per escalation. That is the shape that flashed a card
            # and sent a DM per tool call in auto mode, so the fake keeps it.
            req_id = f"gate-{len(self.calls)}-{index}"
            yield Event(K.APPROVAL_REQUESTED, handle.thread_id,
                        {"req_id": req_id, "tool": "Bash",
                         "args": {"command": command}, "command": command})
            decision = handle.native["permit"]("Bash", {"command": command},
                                               handle.native["brief"])
            self.calls.append((command, decision))
            yield Event(K.APPROVAL_RESOLVED, handle.thread_id,
                        {"req_id": req_id, "decision": decision.value})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def answer(self, handle, req_id, decision):
        raise ValueError("unknown request")

    def close(self, handle):
        pass


def http_json(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = None if body is None else json.dumps(body).encode()
        headers = {**({"Content-Type": "application/json"} if payload else {}), **(headers or {})}
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        conn.close()


def hud_json(daemon, method, path, body=None):
    """A request as the HUD window sends it: its own listener, its Origin."""
    return http_json(daemon.face_port, method, path, body,
                     {"Origin": f"http://127.0.0.1:{daemon.face_port}"})


def approve_route_owner_only_checks(daemon, req_id):
    """BR-0: only the HUD window can answer a card over HTTP.

    The API listener serves the same HUD page, so a page loaded there sends an
    Origin equal to its Host and passed `check_origin`. v1's agent browser
    could load that page (it refused only the face's port) and click the
    card's button. Fails on a49423c: the first request below answered 200.
    """
    def still_pending():
        status, pending = http_json(daemon.port, "GET", "/approvals")
        return status == 200 and [r["req_id"] for r in pending] == [req_id]

    body = {"decision": "allow"}
    api_origin = {"Origin": f"http://127.0.0.1:{daemon.port}"}
    status, _ = http_json(daemon.port, "POST", "/approvals/" + req_id, body, api_origin)
    eq(status, 403, "the API listener refuses an approval sent with its own Origin "
                    "(a page it served)")
    ok(still_pending(), "and the request is still waiting for the owner")
    status, _ = http_json(daemon.port, "POST", "/approvals/" + req_id, body,
                          {"Origin": f"http://localhost:{daemon.port}"})
    eq(status, 403, "…in its localhost spelling too")
    status, _ = http_json(daemon.port, "POST", "/approvals/" + req_id, body)
    eq(status, 403, "the API listener refuses an approval with no Origin")
    status, _ = http_json(daemon.port, "POST", "/approvals/" + req_id, body,
                          {"Origin": f"http://127.0.0.1:{daemon.face_port}"})
    eq(status, 403, "the API listener refuses the HUD's Origin as well (it is not the HUD's Host)")
    status, _ = http_json(daemon.face_port, "POST", "/approvals/" + req_id, body)
    eq(status, 403, "the HUD's listener refuses an approval with no Origin")
    status, _ = http_json(daemon.face_port, "POST", "/approvals/" + req_id, body,
                          {"Origin": f"http://127.0.0.1:{daemon.workshop_port}"})
    eq(status, 403, "the HUD's listener refuses another origin (the workshop's)")
    ok(still_pending(), "none of those refusals resolved or dropped the request")
    status, pending = http_json(daemon.port, "GET", "/approvals")
    eq(status, 200, "reading pending approvals stays open on the API listener")


def hud_build_deny_checks():
    """BR-0: no provider's write tool, and no Codex grant, reaches the built
    HUD the daemon serves — a script planted there approves with the HUD's own
    Origin on the owner's next reload. On dfb1d7e every write below was
    allowed and every grant approvable."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit, denied_file, denied_grant
    from jarvis.v2.provider import Decision

    real = config.HUD_DIST
    ok(denied_file(str(real / "index.html")) is not None,
       "the real config.HUD_DIST (the installed HUD since C-1) is refused")
    with Sandbox() as box:
        dist = box.root / "hud" / "dist"
        (dist / "assets").mkdir(parents=True)
        (box.root / "hud" / "src").mkdir()
        link = box.root / "looks-ordinary"
        link.symlink_to(dist, target_is_directory=True)
        config.HUD_DIST = dist
        try:
            for target in (dist / "index.html", dist / "assets" / "planted.js", link / "index.html",
                           dist / "new" / "nested.js", dist):
                why = denied_file(str(target))
                ok(why is not None and "built HUD" in why, f"a write to {target} is refused ({why})")
            eq(denied_file(str(box.root / "hud" / "src" / "App.tsx")), None,
               "the HUD's source stays writable")
            brief = brief_for(P.AUTO)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            page = str(dist / "index.html")
            for tool, args in (("Write", {"file_path": page, "content": "<script></script>"}),
                               ("Edit", {"file_path": page, "old_string": "a", "new_string": "b"}),
                               ("apply_patch", {"paths": [page]}),
                               ("write_file", {"path": page, "content": "x"})):
                before = len(asker.seen)
                eq(permit(tool, args, brief), Decision.DENY, f"{tool} must not write the built HUD")
                eq(len(asker.seen), before, f"{tool}'s refusal is never put to the owner")
            for grant in (dist, dist / "assets", box.root / "hud", link):
                ok(denied_grant(str(grant), "write") is not None,
                   f"a write grant on {grant} would reach the built HUD")
            eq(denied_grant(str(dist), "read"), None, "reading it is not a lever")
            eq(denied_grant(str(box.root / "hud" / "src"), "write"), None,
               "a write grant on the HUD's source is not refused for this reason")
        finally:
            config.HUD_DIST = real


def env_file_checks():
    """ENV-1, the owner's `.env*` rule at layer 1: no provider's file tool
    reads or writes a credential env file (`*.example` only), no shell line
    writes one, and no Codex grant that is or holds one is approvable. On
    main: Claude's read tools were not judged at all, `.env.production` was
    writable, and a grant on a folder holding one was approvable."""
    from jarvis.v2 import permissions as perms
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit, denied_file, denied_grant, denied_read
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        project = box.root / "proj"
        (project / "src").mkdir(parents=True)
        (project / "src" / "app.ts").write_text("x\n")
        prod = project / ".env.production"
        prod.write_text("API_KEY=FAKE-ENV1-V2\n")
        (project / ".env.example").write_text("API_KEY=\n")
        (project / "settings.txt").symlink_to(".env.production")
        (project / ".env.local.example").symlink_to(".env.production")
        brief = brief_for(P.AUTO, cwd=project)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)

        refused = [
            ("Read", {"file_path": str(prod)}),
            ("Read", {"file_path": ".env.staging"}),                 # placed in the brief's folder
            ("Read", {"file_path": str(project / "settings.txt")}),
            ("Read", {"file_path": str(project / ".env.local.example")}),
            ("NotebookRead", {"notebook_path": str(project / ".env.qa")}),
            ("Grep", {"pattern": "KEY", "path": str(prod)}),
            ("Grep", {"pattern": "KEY", "path": str(project), "glob": "{*.ts,.env.*}"}),
            ("Grep", {"pattern": "KEY", "glob": "**/.env*"}),
            ("Grep", {"pattern": "KEY", "glob": "config/.ENV.Prod"}),
            # Delta review LOW-B: Grep reads what its glob selects, hidden
            # files included — `*.env*` is `rg --hidden -g '*.env*'`.
            ("Grep", {"pattern": "API_KEY", "glob": "*.env*"}),
            ("Grep", {"pattern": "API_KEY", "glob": "**/*.local"}),
            ("Grep", {"pattern": "API_KEY", "path": "app/.env.prod/"}),
            # …and the raw-line check the shell gives the same strings
            # (`grep -r KEY app/.env.prod/conf`, `rg -g '!.env.production'`).
            ("Grep", {"pattern": "API_KEY", "path": "app/.env.prod/conf"}),
            ("Grep", {"pattern": "API_KEY", "glob": "!.env.production"}),
            ("read_file", {"path": str(prod)}),
            ("grep_files", {"pattern": "KEY", "path": str(prod)}),
            ("Write", {"file_path": str(prod), "content": "x"}),
            ("Write", {"file_path": str(project / "settings.txt"), "content": "x"}),
            ("Write", {"file_path": str(project / ".env.local.example"), "content": "x"}),
            ("Edit", {"file_path": ".env.sample", "old_string": "a", "new_string": "b"}),
            ("MultiEdit", {"file_path": str(project / ".env.dev"), "edits": []}),
            ("NotebookEdit", {"notebook_path": str(project / ".env.test")}),
            ("apply_patch", {"paths": [str(project / "src" / "x.ts"), str(prod)]}),
            ("write_file", {"path": str(project / ".env.preview"), "content": "x"}),
            ("edit_file", {"path": str(prod), "old_string": "a", "new_string": "b"}),
            bash("cat .env.production"),
            bash("echo X=1 > .env.staging"),
            bash("cp src/app.ts .env.production"),
            bash("echo X=1 > settings.txt"),                          # a symlink, in the brief's folder
            ("shell", {"command": ["bash", "-lc", "sed -i s/a/b/ .env.local"]}),
        ]
        for tool, args in refused:
            before = len(asker.seen)
            eq(permit(tool, args, brief), Decision.DENY, f"{tool} {args} is refused")
            eq(len(asker.seen), before, f"{tool}'s refusal is never put to the owner")
        eq(prod.read_text(), "API_KEY=FAKE-ENV1-V2\n", "the credential env file is untouched")
        layers = {d["layer"] for d in box.decisions if d["decision"] == "deny"}
        eq(layers, {"deny"}, "every refusal is layer 1's")

        allowed = [
            ("Read", {"file_path": str(project / ".env.example")}),
            ("Write", {"file_path": str(project / "config" / ".env.staging.example"), "content": "x"}),
            ("Edit", {"file_path": ".env.example", "old_string": "=", "new_string": "=x"}),
            ("Glob", {"pattern": "**/*.ts"}),
            # Delta review NIT: Glob returns names only, never contents.
            ("Glob", {"pattern": "**/.env*"}), ("Glob", {"pattern": "config/.ENV.Prod"}),
            ("Glob", {"pattern": "**/.*"}), ("Glob", {"pattern": "**/*.env*"}),
            ("Grep", {"pattern": "KEY", "path": str(project)}),   # a folder search: the stated limit
            ("Grep", {"pattern": ".env.production", "path": str(project / "src")}),  # a regex, not a file
            bash("cp .env.example .env.local.example.new.example"),
        ]
        for tool, args in allowed:
            eq(permit(tool, args, brief), Decision.ALLOW, f"{tool} {args} is not refused")

        # A write that only may land on one is asked, whatever the profile.
        line = "cp src/app.ts $JARVIS_ENV1_UNSET.env"
        before = len(asker.seen)
        eq(permit(*bash(line), brief), Decision.ALLOW, "the owner said yes")
        eq(len(asker.seen), before + 1, "a maybe-write onto an env file is asked under AUTO")
        ok("env-file" in asker.seen[-1].reason, f"as the env-file rule ({asker.seen[-1].reason})")

        # denied_read / denied_file directly.
        ok(denied_read("Read", {"file_path": ".env.production"}, str(project)) is not None,
           "a relative read is placed in the folder it runs in")
        eq(denied_read("Read", {"file_path": str(project / ".env.example")}), None,
           ".env.example is readable")
        ok(denied_file(str(project / ".env.local.example")) is not None,
           "an .example name that leads to a credential env file is refused")
        eq(denied_file(str(project / ".env.example")), None, ".env.example is writable")

        # Codex grants: a grant that is or holds one, read or write.
        clean = box.root / "clean"
        (clean / "venv" / ".env" / "bin").mkdir(parents=True)    # a virtualenv named .env: a folder
        (clean / "venv" / ".env" / "bin" / "activate").write_text("x\n")
        (clean / ".env.example").write_text("x\n")
        linked = box.root / "linked"
        linked.mkdir()
        (linked / "conf").symlink_to(prod)
        for grant, access in ((project, "write"), (project, "read"), (prod, "write"),
                              (project / "src" / "..", "write"), (linked, "write"),
                              (project / ".env.local.example", "read")):
            why = denied_grant(str(grant), access)
            ok(why is not None and "credential env file" in why, f"{access} grant on {grant} ({why})")
        for access in ("write", "read"):
            eq(denied_grant(str(clean), access), None, f"a {access} grant on a folder with none")
            eq(denied_grant(str(project / "src"), access), None, f"a {access} grant on src/")
        # ENV-1 review, NIT 1: a virtualenv named `.env` is a folder, judged
        # by what it holds — a write grant on it is not refused for its name.
        for access in ("write", "read"):
            eq(denied_grant(str(clean / "venv" / ".env"), access), None,
               f"a {access} grant on a virtualenv folder named .env")
        saved = perms.GRANT_SCAN_ENTRIES
        perms.GRANT_SCAN_ENTRIES = 2
        try:
            why = denied_grant(str(clean), "write")
            ok(why is not None and "could not check" in why,
               f"past the bound, a grant nobody will be asked about is refused ({why})")
            # ENV-1 review, NIT 2: through the permit a widening reaches the
            # owner, so a folder too large to walk is asked about, saying so.
            before = len(asker.seen)
            eq(permit("apply_patch", {"paths": [str(clean / "x.ts")]}, brief,
                      widening="write grant", grants=(("write", str(clean)),)),
               Decision.ALLOW, "past the bound a widening goes to the owner (who says yes)")
            eq(len(asker.seen), before + 1, "it was asked")
            headline = asker.seen[-1].headline
            ok("could not check" in headline and "credential env files" in headline,
               f"and the card says the folder was not checked ({headline})")
            strict = brief_for(P.STRICT, cwd=project)
            eq(build_permit(context(strict), asker)(
                "apply_patch", {"paths": [str(clean / "x.ts")]}, strict,
                widening="write grant", grants=(("write", str(clean)),)),
               Decision.DENY, "strict still refuses the widening")
        finally:
            perms.GRANT_SCAN_ENTRIES = saved
        eq(permit("apply_patch", {"paths": [str(project / "src" / "x.ts")]}, brief,
                  widening="write grant", grants=(("write", str(project)),)),
           Decision.DENY, "a widening whose grant holds a credential env file is refused unasked")

        # ENV-1 review, MED-1: Claude's Glob and Grep on ordinary file-name
        # globs are allowed (refused at layer 1 at 153920d); a dot-led one that
        # can match a credential env name is refused.
        for pattern in ("**/*test*", "src/**/*.test*", "**/*template*", "**/*.bak", "**/*dev*",
                        "**/*.{ts,tsx}", "**/.github/*.yml", "**/*.local"):
            eq(permit("Glob", {"pattern": pattern}, brief), Decision.ALLOW, f"Glob {pattern}")
        for value in ("*test*", "*.production", "*.bak"):
            eq(permit("Grep", {"pattern": "x", "glob": value}, brief), Decision.ALLOW,
               f"Grep glob {value}")
        for pattern in ("**/.e?v.*", "**/.[e]nv.custom", "**/.*.custom", "src/{a,.env}.*",
                        "**/.ENV*", ".env.*", "*.env*", "*.local", "*env*", "**/*.env.ts"):
            eq(permit("Grep", {"pattern": "x", "glob": pattern}, brief), Decision.DENY,
               f"Grep glob {pattern}")
            # Glob lists names, never contents (delta review NIT).
            eq(permit("Glob", {"pattern": pattern}, brief), Decision.ALLOW, f"Glob {pattern}")
        for value in ("*.custom", "**/*.py", "**/*.ts", "*.json", "**/*.json"):
            eq(permit("Grep", {"pattern": "x", "glob": value}, brief), Decision.ALLOW,
               f"Grep glob {value} (as main; the stated limit of a folder search)")
        # NIT 5: a Codex `also_commands` line is judged in the brief's folder.
        eq(permit("shell", {"command": "true"}, brief, widening="terminal input",
                  also_commands=("echo x > settings.txt",)),
           Decision.DENY, "an also-command writing through a symlink in the brief's folder")


def installed_hud_deny_checks():
    """C-1 (ED-4): the installed HUD is what the daemon serves, so it is what
    BR-0's one check refuses — `config.HUD_DIST` is the install root. Every
    provider write tool, into a build or as a newer build planted beside them,
    is denied unasked, and a Codex grant on the root, a build or anything
    above the root is refused. On main the install root was ordinary ground."""
    from jarvis.v2 import hud_install
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit, denied_file, denied_grant
    from jarvis.v2.provider import Decision

    saved = (config.HUD_DIR, config.HUD_DIST, config.HUD_DEV)
    with Sandbox() as box:
        built = box.root / "built"
        (built / "assets").mkdir(parents=True)
        (built / "index.html").write_text("<!doctype html><title>J.A.R.V.I.S.</title>")
        (built / "assets" / "app-1a2b.js").write_text("console.log(1)")
        root = box.root / "share" / "jarvis" / "hud"
        config.HUD_DIR = config.HUD_DIST = root
        config.HUD_DEV = False
        try:
            installed = hud_install.install(built, root)
            newer = root / "29991231T235959999999Z-0123456789ab"
            brief = brief_for(P.AUTO)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for target in (installed.path / "index.html", installed.path / "assets" / "app-1a2b.js",
                           installed.path / "manifest.json", newer / "index.html", root / "x.js"):
                why = denied_file(str(target))
                ok(why is not None and "built HUD" in why and "jarvis hud install" in why,
                   f"a write to {target} is refused ({why})")
                for tool, args in (("Write", {"file_path": str(target), "content": "<script></script>"}),
                                   ("apply_patch", {"paths": [str(target)]}),
                                   ("write_file", {"path": str(target), "content": "x"})):
                    before = len(asker.seen)
                    eq(permit(tool, args, brief), Decision.DENY, f"{tool} must not write {target}")
                    eq(len(asker.seen), before, f"{tool}'s refusal is never put to the owner")
            for grant in (root, installed.path, newer, root.parent, box.root / "share"):
                ok(denied_grant(str(grant), "write") is not None,
                   f"a write grant on {grant} would reach the installed HUD")
            eq(denied_grant(str(root), "read"), None, "reading it is not a lever")
            eq(denied_grant(str(built), "write"), None, "the build output itself is ordinary ground")

            # Review of PR #44, MED-2: the installer is never Jarvis's to run
            # (layer 1 DENY, never asked, under AUTO), and a shell write into
            # the install root always asks (layer 2) — on main, every one of
            # these was ALLOW under AUTO.
            for line in ("jarvis hud install", "python -m jarvis hud install",
                         "/repo/.venv/bin/jarvis hud install --allow-dirty",
                         "uv run python -m jarvis hud install", "python3 -m jarvis.v2.hud_install",
                         "bash -lc 'jarvis hud install'", "env JARVIS_HUD_DIR=/tmp/x nohup jarvis hud install",
                         'python -c "from jarvis.v2 import hud_install; hud_install.main()"'):
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.DENY, f"Bash {line!r} is refused")
                eq(len(asker.seen), before, f"{line!r} is never put to the owner")
            for line in (f"cp /tmp/x.js {installed.path}/assets/x.js",
                         f"rsync -a /tmp/dist/ {root}/29991231T235959999999Z-0123456789ab/",
                         f"echo pwned > {installed.path}/index.html"):
                before = len(asker.seen)
                permit(*bash(line), brief)
                eq(len(asker.seen), before + 1, f"{line!r} is asked (always-ask: the installed HUD)")
                ok("installed HUD" in asker.seen[-1].reason,
                   f"{line!r} says why it asks ({asker.seen[-1].reason!r})")
            asker_no = Asker(Decision.DENY)
            permit_no = build_permit(context(brief), asker_no)
            for line in ("jarvis hud", "cat jarvis/v2/hud_install.py",
                         'git commit -m "docs: run jarvis hud install after a merge"'):
                eq(rules.decide(line).decision != rules.DENY, True,
                   f"{line!r} is not refused by the installer rule")
        finally:
            config.HUD_DIR, config.HUD_DIST, config.HUD_DEV = saved


def daemon_and_rebuild_ops_checks():
    """`jarvis restart` (and `jarvis daemon2 restart`), `jarvis hud rebuild`
    and a `systemctl` that changes the daemon's unit (2026-10-11) are the
    owner's, from their own terminal: layer 1 DENY, never asked, under AUTO
    and STRICT — through `protected_state.owner_op`, the one detector.
    Before, `jarvis restart` and `jarvis hud rebuild` were asked under AUTO
    only if the provider's own classifier asked, and `systemctl --user
    restart jarvis-daemon2` was ALLOW in `rules.decide`. A Claude or Codex
    worker runs under the daemon it would restart."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit
    from jarvis.v2.provider import Decision

    with Sandbox():
        for profile in (P.AUTO, P.STRICT):
            brief = brief_for(profile)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for line in ("jarvis restart", "jarvis restart --force", "python -m jarvis restart",
                         "jarvis daemon2 restart", "/repo/.venv/bin/jarvis hud rebuild --pull --restart",
                         "uv run jarvis hud rebuild", "bash -lc 'jarvis restart'",
                         "env A=1 nohup jarvis hud --pull rebuild", "python3 -m jarvis.v2.owner_ops",
                         'python -c "from jarvis.v2 import owner_ops; owner_ops.restart()"',
                         "systemctl --user restart jarvis-daemon2",
                         "systemctl --user stop jarvis-daemon2.service",
                         "nohup systemctl --user --no-block restart jarvis-daemon2"):
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.DENY, f"[{profile.value}] Bash {line!r} is refused")
                eq(len(asker.seen), before, f"[{profile.value}] {line!r} is never put to the owner")
        brief = brief_for(P.AUTO)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)
        for line in ("echo Run jarvis restart", 'grep -rn "hud rebuild" docs',
                     "git commit -m 'run `jarvis restart` after merges'",
                     "cat > notes.md <<'EOF'\njarvis hud rebuild\nEOF",
                     "systemctl --user status jarvis-daemon2", "journalctl --user -u jarvis-daemon2 -n 50",
                     "curl -s localhost:8405/status"):
            ok(permit(*bash(line), brief) != Decision.DENY, f"{line!r} is not refused")


REVIEW_48_V2_DENY = (
    # M1
    "systemctl --user -qp status restart jarvis-daemon2", "systemctl --user -P status stop jarvis-daemon2",
    "systemctl --user --marked restart jarvis-daemon2", "systemctl --user restart jarvis-daemon{2,}",
    "U=jarvis-daemon2; systemctl --user restart $U", "systemctl --user stop app.slice",
    "systemctl --user kill basic.target", "echo restart jarvis-daemon2 | xargs systemctl --user",
    "busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 "
    "org.freedesktop.systemd1.Manager RestartUnit ss jarvis-daemon2.service replace",
    # module runners and inline source
    "python3 -m runpy jarvis hud install", "python3 -m cProfile -m jarvis restart",
    "python3 -m pdb -c continue -m jarvis restart", "python3 -m coverage run -m jarvis restart",
    'python3 -c "from jarvis.v2.owner_ops import *; restart()"',
    "python3 -c \"import importlib; m = importlib.import_module('jarvis.v2.owner_ops'); m.restart()\"",
    "python3 -c \"import subprocess; subprocess.run(['systemctl', '--user', 'restart', 'jarvis-daemon2'])\"",
    # the owner's daemon2 kill rule
    "pkill -f daemon2", "pkill -9 -f 'jarvis daemon2'", "pkill -KILL -f 'daemo[n]2'",
    "pkill --signal KILL -f 'n2$'", "pkill -fdaemon2", "pkill -f -- 'jarvis d'",
    "P=daemon2; pkill -f $P", "kill $(pgrep -f daemon2)", "kill -9 `pgrep -f daemon2`",
    "kill -s KILL $(pidof daemon2)", "pgrep -f daemon2 | xargs kill -9", "killall -r daemon2",
    "nohup pkill -f daemon2",
)
REVIEW_48_V2_ORDINARY = (
    "pkill -f jarvis", "pkill -f vite", "pkill -f 'npm run dev'", "pgrep -f daemon2",
    "echo pkill -f daemon2", "git commit -m 'stop using pkill -f daemon2'",
    "systemctl --user reset-failed jarvis-daemon2", "systemctl --user restart pipewire",
    "python3 -m cProfile -m pytest",
)


def review_48_checks():
    """The review of PR #48 at layer 1: every new spelling is DENY under AUTO
    and STRICT and never put to the owner; the must-stay lines are not
    refused. On c218da8 (merged with main) v2's AUTO permit let the kill and
    module-runner spellings run unasked."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit
    from jarvis.v2.provider import Decision

    with Sandbox():
        for profile in (P.AUTO, P.STRICT):
            brief = brief_for(profile)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for line in REVIEW_48_V2_DENY:
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.DENY, f"[{profile.value}] Bash {line!r} is refused")
                eq(len(asker.seen), before, f"[{profile.value}] {line!r} is never put to the owner")
        brief = brief_for(P.AUTO)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)
        for line in REVIEW_48_V2_ORDINARY:
            ok(permit(*bash(line), brief) != Decision.DENY, f"{line!r} is not refused")


# Program runners judged by what they start (2026-10-12): each layer-1 line was
# allowed under AUTO on main, and each always-ask line went to the reviewer.
RUNNER_V2_DENY = (
    "npx jarvis hud install", "yarn exec jarvis restart", "npm exec -c 'jarvis hud install'",
    "bun x jarvis restart", "pnpm exec -c 'jarvis restart'",
    "env -u ls cp /tmp/x ~/.config/jarvis/allowlist.json",
    "exec -a ls cp /tmp/x ~/.config/jarvis/allowlist.json",
    "npm exec -c 'cp /tmp/x ~/.config/jarvis/allowlist.json'",
    "bun exec 'cp /tmp/x ~/.config/jarvis/allowlist.json'",
    "npm exec -c 'cp /tmp/x .env.production'",
    # The delta review of PR #75 (1723b53): H-A, a memo keyed without its
    # segment; H-B, a wrapper's own output file; M-A, a named output behind
    # find -exec; M-B, shells outside the lists and an unknown program's -c;
    # LOW, owner ops behind find, fd and parallel.
    "uv run sh <<< 'true' ; uv run sh <<< 'cp /tmp/x ~/.config/jarvis/allowlist.json'",
    "npm exec -- bash <<< 'true' && npm exec -- bash <<< 'cp /tmp/x ~/.config/jarvis/allowlist.json'",
    "time -o ~/.config/jarvis/allowlist.json -f x true", "time --output=~/.config/jarvis/allowlist.json true",
    "env time -o~/.config/jarvis/allowlist.json true", "script ~/.config/jarvis/allowlist.json",
    "systemd-run --user -p StandardOutput=file:/tmp/../tmp/x -p StandardError=file:"
    "~/.config/jarvis/allowlist.json true",
    "find /tmp/x -maxdepth 0 -exec sort -o ~/.config/jarvis/allowlist.json {} \\;",
    "find /tmp/x -maxdepth 0 -exec sed -n 'w ~/.config/jarvis/allowlist.json' {} \\;",
    "uv run tcsh -c 'cp /tmp/x ~/.config/jarvis/allowlist.json'", "npx nu -c 'jarvis restart'",
    "uv run myshell -c 'cp /tmp/x ~/.config/jarvis/allowlist.json'",
    "npx zx -e 'jarvis hud install'", "find . -exec jarvis restart \\;", "fd -x jarvis restart",
    "parallel jarvis ::: restart", "csh -c 'jarvis restart'",
)
RUNNER_V2_ALWAYS_ASK = (
    # H-B: a wrapper's option value is a path again for the credential-folder
    # rule (`credentials-path` read only the unwrapped command's words).
    ("time -o ~/.ssh/notes true", "credentials-path"),
    ("time --output=~/.aws/notes true", "credentials-path"),
    ("time -o~/.gnupg/notes true", "credentials-path"), ("env -C ~/.ssh ls", "credentials-path"),
    ("script -c true ~/.ssh/session.log", "credentials-path"),
    ("npx vercel --prod", "vercel-prod"), ("npx vercel deploy --prod", "vercel-prod"),
    ("pnpm dlx vercel --prod", "vercel-prod"), ("npm exec -c 'vercel --prod'", "vercel-prod"),
    ("uv run git push --force origin topic", "git-push-force"),
    ("env -u X git push origin main", "git-push-protected"),
    ("npx stripe charges create --amount 5", "payments-stripe"),
    ("bun x gh release create v1", "gh-release"),
)
RUNNER_V2_ORDINARY = ("npx vercel", "npx vercel ls", "uv run pytest", "npx vite", "npm exec vitest",
                      "uv run git push origin topic")


def runner_checks():
    """What a wrapper or runner starts, at each layer: refused at layer 1 under
    AUTO and STRICT and never asked; an always-ask rule matched through it;
    and no longer auto-allowed at layer 4 when the program asks on its own."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import always_ask_match, build_permit
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        for profile in (P.AUTO, P.STRICT):
            brief = brief_for(profile)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for line in RUNNER_V2_DENY:
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.DENY, f"[{profile.value}] Bash {line!r} is refused")
                eq(len(asker.seen), before, f"[{profile.value}] {line!r} is never put to the owner")
        brief = brief_for(P.AUTO)
        for line, rule in RUNNER_V2_ALWAYS_ASK:
            match = always_ask_match("Bash", {"command": line}, brief)
            eq(match[0] if match else None, rule, f"{line!r} matches always-ask rule {rule}")
            asker = Asker(Decision.DENY)
            permit = build_permit(context(brief), asker)
            eq(permit(*bash(line), brief), Decision.DENY, f"{line!r}: the owner's no holds")
            eq(len(asker.seen), 1, f"{line!r} is put to the owner under AUTO")
            eq(box.decisions[-1]["layer"], "always-ask", f"{line!r} at layer 2")
        for line in RUNNER_V2_ORDINARY:
            eq(always_ask_match("Bash", {"command": line}, brief), None, f"{line!r} asks nothing new")
        # Layer 4: a human-backed asker no longer waves through what a runner
        # starts when that program asks on its own; ordinary runs still pass.
        human = build_permit(context(brief), Asker(Decision.ALLOW))
        human(*bash("uv run rm -rf build"), brief)
        eq(box.decisions[-1]["layer"], "reviewer", "`uv run rm -rf build` is not layer 4's ALLOW")
        human(*bash("uv run pytest"), brief)
        eq(box.decisions[-1]["layer"], "jarvis-allow", "`uv run pytest` still is")
        # The review of PR #75, HIGH 1: past the line's work budget what it
        # starts is too complex to judge — asked at layer 2 in every profile,
        # never the reviewer's call and never the allowlist's.
        config.ALLOWLIST_PATH.write_text(json.dumps(
            [{"tool": "run_command", "prefix": s} for s in ("npx", "pnpm", "uv", "nohup", "rm")]))
        for line in ("npx --x " * 60 + "rm -rf ~/x", "pnpm --x exec " * 40 + "rm -rf ~/x",
                     "nohup " * 13 + "rm -rf build",
                     # past `decide`'s budget alone: only `runners_unsure` asks it
                     "uv run --x y --x y " * 2 + "w " * 2000 + "jarvis restart",
                     # a NUL byte (delta review of PR #75, LOW): never a ValueError
                     "rm a\x00b", "nohup rm\x00 -rf build"):
            asker = Asker(Decision.DENY)
            permit = build_permit(context(brief), asker)
            eq(permit(*bash(line), brief), Decision.DENY, f"too complex: the owner's no holds ({line[:24]}…)")
            eq(len(asker.seen), 1, f"too complex: asked under AUTO ({line[:24]}…)")
            eq(box.decisions[-1]["layer"], "always-ask", f"too complex: at layer 2 ({line[:24]}…)")
            ok("too-complex" in box.decisions[-1]["reason"] or "gate-state" in box.decisions[-1]["reason"],
               f"too complex: said so ({box.decisions[-1]['reason'][:60]})")
        config.ALLOWLIST_PATH.unlink()


_UNIT_CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/jarvis-daemon2.service"
# Review 2 of PR #48 at layer 1 (each allowed under AUTO on 819e58a, or — the
# first — a permit that never returned).
REVIEW_48_2_V2_DENY = (
    "pkill -f 'daemo.{0,1}2'",
    "systemctl --user show -p MainPID --value jarvis-daemon2 | xargs kill",
    "kill -9 $(systemctl --user show -P MainPID jarvis-daemon2)",
    f"pkill --cgroup {_UNIT_CGROUP}", f"pgrep --cgroup {_UNIT_CGROUP} | xargs kill",
    f"cat /sys/fs/cgroup{_UNIT_CGROUP}/cgroup.procs | xargs kill",
    "lsof -ti :8405 | xargs kill", "fuser -k 8405/tcp", "kill $(fuser 8405/tcp 2>/dev/null)",
    "ps aux | grep daemon2 | awk '{print $2}' | xargs kill",
    "python3 -c \"from jarvis.v2 import owner_ops; owner_ops.hand_off(['restart'], print)\"",
    "if true; then systemctl --user restart jarvis-daemon2; fi", "while true; do pkill -f daemon2; done",
    "if true; then jarvis hud install; fi", "case x in x) jarvis restart;; esac",
)
REVIEW_48_2_V2_ORDINARY = (
    "pgrep -f daemon2 && kill -9 4321", "lsof -ti :5173 | xargs kill", "fuser -k 5173/tcp",
    "systemctl --user show -p MainPID --value jarvis-daemon2", "pgrep -f '" + ".?" * 30 + "Z'",
    "busctl --user get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/"
    "jarvis_2ddaemon2_2eservice org.freedesktop.systemd1.Service ActiveState",
    "if true; then systemctl --user restart pipewire; fi",
)


def review_48_round2_checks():
    """Review 2 of PR #48 at layer 1: the new spellings are DENY under AUTO
    and STRICT and never put to the owner, each judged quickly (a kill
    pattern that cannot be bounded fails closed instead of freezing the
    permit); the must-stay lines are not refused."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit
    from jarvis.v2.provider import Decision

    with Sandbox():
        for profile in (P.AUTO, P.STRICT):
            brief = brief_for(profile)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for line in REVIEW_48_2_V2_DENY:
                before = len(asker.seen)
                start = time.perf_counter()
                eq(permit(*bash(line), brief), Decision.DENY, f"[{profile.value}] Bash {line!r} is refused")
                ok(time.perf_counter() - start < 0.5, f"[{profile.value}] {line!r} is judged quickly")
                eq(len(asker.seen), before, f"[{profile.value}] {line!r} is never put to the owner")
        brief = brief_for(P.AUTO)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)
        for line in REVIEW_48_2_V2_ORDINARY:
            start = time.perf_counter()
            ok(permit(*bash(line), brief) != Decision.DENY, f"{line!r} is not refused")
            ok(time.perf_counter() - start < 0.5, f"{line!r} is judged quickly")


REVIEW_48_DELTA_V2_ASK = (
    "pkill -f '.*.*.*.*daemon2'", "pkill -f 'python.*.*.*.*jarvis'",
    "kill $(pgrep -f 'jarvis.*.*.*d')", "killall -r 'python.*.*.*.*2'",
    "ps aux | grep -E 'jarvis.*.*.*.*n' | awk '{print $2}' | xargs kill",
)
REVIEW_48_DELTA_V2_DENY = (
    "grep -l daemon2 /proc/*/cmdline | cut -d/ -f3 | xargs kill",
    "ss -ltnp | grep 8405 | grep -o 'pid=[0-9]*' | cut -d= -f2 | xargs kill",
    "netstat -tlnp | grep :8405 | awk '{print $7}' | cut -d/ -f1 | xargs kill",
)
REVIEW_48_DELTA_V2_ORDINARY = (
    "pkill -f 'node.*vite.*--port.*5173'", "pkill -f 'gunicorn.*worker.*[0-9]+'",
    "pkill -f 'python.*celery.*worker.*--concurrency.*4'",
    "kill $(pgrep -f 'uvicorn.*app.*--reload.*8000')",
)


def review_48_delta_checks():
    """The delta review of PR #48 at layers 1 and 2: a kill pattern too
    costly to judge is always-ask (`daemon-kill`) under AUTO and STRICT,
    never a layer-1 DENY, even with an allowlist entry for every stem on it;
    the new /proc and socket sources are layer-1 DENY; the review's
    ordinary costly patterns are not refused. Each fails on 92c225b."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import L_ALWAYS_ASK, build_permit
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        config.ALLOWLIST_PATH.write_text(json.dumps(
            [{"tool": "run_command", "prefix": stem}
             for stem in ("pkill", "kill", "killall", "pgrep", "ps", "grep", "awk", "xargs")]))
        for profile in (P.AUTO, P.STRICT):
            brief = brief_for(profile)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for line in REVIEW_48_DELTA_V2_ASK:
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.ALLOW,
                   f"[{profile.value}] {line!r}: asked, and the owner's yes stands")
                eq(len(asker.seen), before + 1, f"[{profile.value}] {line!r} is put to the owner")
                eq(box.decisions[-1]["layer"], L_ALWAYS_ASK, f"[{profile.value}] {line!r} is always-ask")
                ok(asker.seen[-1:] and "daemon-kill" in asker.seen[-1].reason,
                   f"[{profile.value}] {line!r} asks under the daemon-kill rule")
            for line in REVIEW_48_DELTA_V2_DENY:
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.DENY, f"[{profile.value}] {line!r} is refused")
                eq(len(asker.seen), before, f"[{profile.value}] {line!r} is never put to the owner")
        brief = brief_for(P.AUTO)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)
        for line in REVIEW_48_DELTA_V2_ORDINARY:
            ok(permit(*bash(line), brief) != Decision.DENY, f"{line!r} is not refused")


# The third delta review of PR #75 at layer 1: an assignment or a reserved
# word in front of a wrapper's output file or a runner's here-string shell, a
# `c`/`e` cluster handed to an unknown program, and a systemd `Exec…=`
# property. Each was allowed under AUTO at 89074f1 (or asked, by layer 2).
_A = "~/.config/jarvis/allowlist.json"
THIRD_DELTA_75_V2_DENY = (
    f"X=1 time -o {_A} -f x ls", f"FOO= time -o {_A} ls", f"X=$(true) time -o {_A} ls",
    f"X=1 env time -o {_A} ls", f"X=1 /usr/bin/time -o {_A} ls", f"X=1 nohup time -o {_A} ls",
    f"X=1 script -c true {_A}", f"X=1 systemd-run --user -p StandardOutput=file:{_A} true",
    f"X=1 uv run sh <<< 'cp /tmp/x {_A}'", f"X=1 npm exec -- sh <<< 'cp /tmp/x {_A}'",
    f"A=1 B=2 time -o {_A} ls", f"A=\"x y\" time -o {_A} ls", f"b+=y time -o {_A} ls",
    f"if true; then time -o {_A} ls; fi", f"case x in x) uv run sh <<< 'cp /tmp/x {_A}';; esac",
    "X=1 uv run sh <<< 'jarvis restart'",
    "D=$HOME; D+=/.config/jarvis; cp /tmp/x $D/allowlist.json",
    "npx mysh -lc 'jarvis restart'", f"uv run mysh -ec 'cp /tmp/x {_A}'",
    "pnpm exec zx -xc 'jarvis hud install'",
    "systemd-run --user -p ExecStartPost='jarvis restart' true",
    f"systemd-run --user --property=ExecStart='/bin/sh -c \"cp /tmp/x {_A}\"' true",
)


def third_delta_75_checks():
    """The third delta review of PR #75 at layer 1: refused under AUTO and
    STRICT, never put to the owner."""
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit, denied_command
    from jarvis.v2.provider import Decision

    with Sandbox():
        for profile in (P.AUTO, P.STRICT):
            brief = brief_for(profile)
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            for line in THIRD_DELTA_75_V2_DENY:
                ok(denied_command(line, "/tmp"), f"layer 1 refuses {line[:60]!r}")
                before = len(asker.seen)
                eq(permit(*bash(line), brief), Decision.DENY, f"[{profile.value}] Bash {line[:60]!r} is refused")
                eq(len(asker.seen), before, f"[{profile.value}] {line[:60]!r} is never put to the owner")
        # Layer 2 reads the program past a reserved word, a group or a function
        # header too: on main `if true; then git push --force origin main; fi`
        # matched no rule and went to the provider's reviewer under AUTO.
        from jarvis.v2.permissions import always_ask_match
        brief = brief_for(P.AUTO)
        fronts = ("if true; then {s}; fi", "while true; do {s}; done", "case x in x) {s};; esac",
                  "f() {{ {s}; }}", "! {s}", "{{ {s}; }}", "( {s} )", "coproc {s}", "b+=y {s}",
                  "a[0]=x {s}")
        push = ("git-push-protected", "git-push-force")
        for base, rule in (("git push --force origin main", push), ("git push origin main", push),
                           ("npx vercel --prod", ("vercel-prod",)),
                           ("vercel --prod", ("vercel-prod",)),
                           ("uv run git push --force origin main", push)):
            for front in fronts:
                line = front.format(s=base)
                match = always_ask_match("Bash", {"command": line}, brief)
                ok(match is not None and match[0] in rule,
                   f"layer 2 matches {line!r} ({match and match[0]})")
                asker = Asker(Decision.DENY)
                eq(build_permit(context(brief), asker)(*bash(line), brief), Decision.DENY,
                   f"{line!r}: the owner's no holds")
                eq(len(asker.seen), 1, f"{line!r} is put to the owner under AUTO")
        for line in ("if true; then git status; fi", "while true; do npm test; done",
                     "f() { ls; }", "( make )"):
            eq(always_ask_match("Bash", {"command": line}, brief), None, f"{line!r} asks nothing new")


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
            approve_route_owner_only_checks(daemon, queued[0]["req_id"])
            ok(len(provider.calls) == 1, "no refused approval reached the blocked hook")
            status, body = hud_json(daemon, "POST", "/approvals/" + queued[0]["req_id"],
                                    {"decision": "allow"})
            eq(status, 200, "POST /approvals from the HUD window answers it")
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

            status, _ = hud_json(daemon, "POST", "/approvals/" + queued[0]["req_id"],
                                 {"decision": "allow"})
            eq(status, 404, "an answered request cannot be answered again")
            status, _ = hud_json(daemon, "POST", "/approvals/nope", {"decision": "sideways"})
            ok(status in (400, 404), "a nonsense decision is refused")
        finally:
            daemon.stop()


def _drain(subscription, wait=0.3):
    seen = []
    while True:
        try:
            seen.append(subscription.get(timeout=wait))
        except Exception:
            return seen


def auto_quiet_checks():
    """Auto mode must not flash: a decision made without the owner is never a
    card, a DM or a resolution, while a real question still reaches them.

    Fails against the pre-2026-10-08 daemon, which forwarded every provider
    gate event onto the bus as `approval_requested` / `approval_resolved` —
    ten tool calls meant ten cards and ten DMs, each withdrawn a moment later.
    """
    from jarvis.v2 import daemon as daemon_mod
    from jarvis.v2.model import PermissionProfile as P, ProviderName, Role
    from jarvis.v2.provider import Brief, Decision, UserMessage
    from jarvis.v2.stores import Stores

    with Sandbox() as box:
        provider = PermitProvider()
        stores = Stores(config.V2_DATA_DIR)
        daemon = daemon_mod.Daemon(stores, {ProviderName.FAST: provider}, None, 0)
        daemon.start()
        try:
            project_root = box.root / "project"
            project_root.mkdir()
            status, project = http_json(daemon.port, "POST", "/projects",
                                        {"name": "p", "root": str(project_root)})
            eq(status, 201, "project created")
            brief = Brief(role=Role.IMPLEMENTER, cwd=str(project_root), profile=P.AUTO)
            thread = daemon.open_thread(project["id"], Role.IMPLEMENTER,
                                        ProviderName.FAST, brief)
            subscription = daemon.bus.subscribe(
                lambda r: r.get("thread_id") == thread.id or "approval" in r.get("kind", ""))

            # Ten calls the policy settles on its own: rules ALLOW (layer 4)
            # and "the provider's reviewer decides" (layer 5 under auto).
            allowed = ["pnpm build", "git status", "ls -la", "npm test", "make",
                       "git commit -m wip", "curl https://example.com",
                       "docker ps", "pytest -x", "cargo build"]
            daemon.send(thread.id, UserMessage("run:" + "\n".join(allowed)))
            deadline = time.monotonic() + 5
            while len(provider.calls) < len(allowed) and time.monotonic() < deadline:
                time.sleep(0.005)
            eq([decision for _, decision in provider.calls], [Decision.ALLOW] * len(allowed),
               "every auto-settled call was allowed")
            seen = _drain(subscription)
            kinds = [r.get("kind") for r in seen]
            eq(kinds.count("approval_requested"), 0,
               "ten auto-settled tool calls publish no approval_requested")
            eq(kinds.count("approval_resolved"), 0,
               "and no approval_resolved flicker either")
            ok("turn_finished" in kinds, "the turn itself was published")
            eq(daemon.approvals.pending(), [], "nothing is left pending for the owner")
            logged = [row.get("kind") for row in stores.threads.read_log(thread.id)]
            eq(logged.count("gate_requested"), len(allowed),
               "each gate consultation is still in the thread log, for the audit")
            ok("approval_requested" not in logged,
               "and none of it is logged as a question for the owner")

            # A genuinely ask-worthy call still reaches the owner, exactly once.
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
            eq(len(queued), 1, "the always-ask push is queued for the owner")
            status, _ = hud_json(daemon, "POST", "/approvals/" + queued[0]["req_id"],
                                 {"decision": "deny"})
            eq(status, 200, "and answered")
            deadline = time.monotonic() + 5
            while len(provider.calls) < len(allowed) + 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            eq(provider.calls[-1], ("git push origin main", Decision.DENY),
               "the owner's no reaches the blocked call")
            seen = _drain(subscription)
            asked = [r for r in seen if r.get("kind") == "approval_requested"]
            eq(len(asked), 1, "a real question publishes exactly one approval_requested")
            ok(all((r.get("data") or {}).get("code") for r in asked),
               "and it is the broker's, carrying the code a surface answers by")
            eq([r.get("kind") for r in seen].count("approval_resolved"), 1,
               "and exactly one approval_resolved")
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

        # Layer 1 at the moment of running judges a relative path in the folder
        # the command will run in, not the daemon's (review round 2: dropping
        # the cwd argument survived every suite). `_run` is a recorder here, so
        # nothing is executed whichever way the check goes.
        gate_dir = config.ALLOWLIST_PATH.parent
        gate_dir.mkdir(parents=True, exist_ok=True)
        ok(Path.cwd().resolve() != gate_dir.resolve(), "the daemon is not in the gate's folder")
        gate_task = stores.tasks.create(project.id, "work in the gate's folder")
        gate_task.worktree = str(gate_dir)
        stores.tasks.save(gate_task)
        gate_thread = stores.threads.create(project.id, Role.IMPLEMENTER,
                                            ProviderName.CLAUDE, task_id=gate_task.id)
        relative_write = "echo '[]' > allowlist.json"
        for where, refused in ((gate_thread, True), (thread, False)):
            daemon, hatch, broker = hatch_with(Decision.ALLOW)
            executed = []
            hatch._run = lambda item, task, executed=executed: (
                executed.append(item.command) or "[exit 0]")
            hatch.handle({"kind": "reviewer_declined", "thread_id": where.id,
                          "data": {"tool": "Bash", "command": relative_write,
                                   "reason": "denied by the classifier"}})
            if refused:
                eq(executed, [], "a relative allowlist write in the gate's folder is refused")
                ok(daemon.sent[0][1].text.startswith("[refused by Jarvis rules:"),
                   "and the worker is told why")
            else:
                eq(executed, [relative_write],
                   "the same relative write in a project folder is the owner's to run")

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


# --------------------------------------------------------------------------
# command-bearing tools (2026-10-15): Claude's Monitor, and any tool whose
# arguments carry a command line, are judged as commands — fail closed.

COMMAND_TOOL_DENY = (
    "cat .env", "grep KEY .env.local", "sudo rm -rf /", "pkill -f daemon2",
    "systemctl --user restart jarvis-daemon2", "cat ~/.config/jarvis/google_token.json",
    "tail -f .env.production",
)


def command_tool_checks():
    """Claude's Monitor runs a shell command but was not a command tool, so
    layer 1 never read it: `cat .env` and a daemon kill reached the reviewer
    under AUTO. Now Monitor, PowerShell and any tool whose arguments carry a
    `command`/`cmd`/`script` line are judged at every layer."""
    from jarvis.v2 import approvals
    from jarvis.v2.approvals import PendingApprovals, v1_request
    # Read lazily, so the rest of this section runs (and fails) against main.
    is_command_tool = getattr(approvals, "is_command_tool", lambda tool, args: None)
    from jarvis.v2.model import PermissionProfile as P
    from jarvis.v2.permissions import build_permit, deny_all
    from jarvis.v2.provider import Decision

    with Sandbox() as box:
        shapes = (("Monitor", "command"), ("PowerShell", "command"), ("FutureShell", "command"),
                  ("mcp__some__exec", "cmd"), ("SomeRunner", "script"))
        # Layer 1, unasked, in every profile and for every asker.
        for profile in (P.AUTO, P.ASK, P.STRICT):
            for tool, key in shapes:
                for line in COMMAND_TOOL_DENY:
                    for asker in (Asker(Decision.ALLOW), deny_all()):
                        brief = brief_for(profile)
                        permit = build_permit(context(brief), asker)
                        tag = f"[{profile.value}] {tool} {key}={line!r}"
                        eq(permit(tool, {key: line, "description": "watch"}, brief), Decision.DENY,
                           f"{tag} is refused")
                        eq(box.decisions[-1]["layer"], "deny", f"{tag} at layer 1")
                        if isinstance(asker, Asker):
                            eq(asker.seen, [], f"{tag} is never asked")
        # Argv arrays and a second command key are read too.
        brief = brief_for(P.AUTO)
        permit = build_permit(context(brief), Asker(Decision.ALLOW))
        for args in ({"command": ["cat", ".env"]}, {"command": "", "cmd": "cat .env"},
                     {"command": "ls", "script": "cat .env"}, {"cmd": ["bash", "-lc", "cat .env"]}):
            eq(permit("FutureShell", args, brief), Decision.DENY, f"FutureShell {args} is refused")
        # Layer 2 reads it: an always-ask rule asks through Monitor.
        for line, rule in (("git push --force origin topic", "git-push-force"),
                           ("npx prisma migrate deploy", "db-migrate-deploy"),
                           ("vercel --prod", "vercel-prod")):
            asker = Asker(Decision.DENY)
            permit = build_permit(context(brief), asker)
            eq(permit("Monitor", {"command": line}, brief), Decision.DENY, f"Monitor {line!r}: the no holds")
            eq(len(asker.seen), 1, f"Monitor {line!r} is put to the owner")
            ok(rule in box.decisions[-1]["reason"], f"Monitor {line!r} names {rule}")
            eq(asker.seen[0].command if asker.seen else None, line, "the whole command travels")
        # After #208, a merge through Monitor asks like one through Bash.
        try:
            from jarvis.v2 import merge_gate  # noqa: F401
        except ImportError:
            merge_gate = None
        if merge_gate is not None:
            asker = Asker(Decision.DENY)
            permit = build_permit(context(brief), asker)
            eq(permit("Monitor", {"command": "gh pr merge 1"}, brief), Decision.DENY,
               "Monitor gh pr merge: asked and refused")
            eq(len(asker.seen), 1, "Monitor gh pr merge reaches the owner")
        # Ordinary Monitor use is unchanged in kind: judged, then the reviewer.
        asker = Asker(Decision.ALLOW)
        permit = build_permit(context(brief), asker)
        eq(permit("Monitor", {"command": "tail -f build.log | grep --line-buffered ERROR"}, brief),
           Decision.ALLOW, "an ordinary Monitor runs")
        eq(asker.seen, [], "unasked under AUTO")
        # Not every argument is a command: no command key, an empty one or a
        # non-text one is not a line.
        for tool, args in (("WebFetch", {"url": "https://example.com", "prompt": "cat .env"}),
                           ("Agent", {"prompt": "run cat .env"}), ("FutureTool", {"command": ""}),
                           ("FutureTool", {"command": {"op": "cat .env"}}), ("FutureTool", {"command": 5})):
            eq(is_command_tool(tool, args), False, f"{tool} {args} is not a command tool")
        for tool in ("Bash", "BashOutput", "Monitor", "PowerShell", "shell", "local_shell",
                     "exec_command", "run_command", "run_readonly"):
            ok(is_command_tool(tool, {}), f"{tool} is a command tool by name")
        # The allowlist: a Monitor yes is a stem entry, never `{"tool": "Monitor"}`,
        # and an old whole-tool entry does not wave a Monitor line through.
        eq(v1_request("Monitor", {"command": "npm test", "description": "x"}),
           ("run_command", {"command": "npm test"}), "Monitor maps to run_command")
        eq(v1_request("FutureShell", {"cmd": ["npm", "test"]}),
           ("FutureShell", {"cmd": ["npm", "test"]}),
           "an unknown command-bearing tool is not a shell to the allowlist (#211 review, F1)")
        config.ALLOWLIST_PATH.write_text(json.dumps([{"tool": "Monitor"}, {"tool": "FutureShell"}]))
        human = build_permit(context(brief), Asker(Decision.ALLOW))
        for tool in ("Monitor", "FutureShell"):
            human(tool, {"command": "rm -rf build"}, brief)
            ok(box.decisions[-1]["layer"] != "jarvis-allow",
               f"a whole-tool {tool} entry is not a yes to `rm -rf build`")
        config.ALLOWLIST_PATH.unlink()
        ask = brief_for(P.ASK)
        broker = PendingApprovals(on_request=lambda r: None)
        answers = []
        worker = threading.Thread(target=lambda: answers.append(build_permit(context(ask), broker)(
            "Monitor", {"command": "docker compose logs -f"}, ask)))
        worker.start()
        deadline = time.monotonic() + 5
        while not broker.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        if broker.pending():
            broker.resolve(broker.pending()[0].req_id, Decision.ALLOW, always=True)
        worker.join(5)
        eq(answers, [Decision.ALLOW], "the Monitor card was answered")
        entries = json.loads(config.ALLOWLIST_PATH.read_text()) if config.ALLOWLIST_PATH.exists() else []
        ok({"tool": "Monitor"} not in entries and any(e.get("tool") == "run_command" for e in entries),
           f"an Always on Monitor mints a stem entry ({entries})")

        # --- #211 review, F1: judged is not trusted. An unknown command-bearing
        # tool gets layers 1 and 2, never the allowlist, rules' ALLOW or an
        # Always: main's decision or a stricter one, in every profile.
        config.ALLOWLIST_PATH.write_text(json.dumps(
            [{"tool": "run_command", "prefix": p} for p in ("npm", "git", "cat")]
            + [{"tool": t} for t in ("mcp__remote__exec", "mcp__ops__deploy")]))
        for profile in (P.STRICT, P.ASK, P.AUTO):
            brief = brief_for(profile)
            tag = f"[{profile.value}]"
            asker = Asker(Decision.ALLOW)
            permit = build_permit(context(brief), asker)
            eq(permit("mcp__remote__exec", {"command": "cat", "args": [".env"]}, brief), Decision.DENY,
               f"{tag} an argv beside the command is judged with it: `cat .env` is refused")
            eq(box.decisions[-1]["layer"], "deny", f"{tag} at layer 1")
            decision = permit("mcp__remote__exec", {"command": "npm", "args": ["publish"]}, brief)
            ok(box.decisions[-1]["layer"] != "jarvis-allow",
               f"{tag} no allowlist entry or rules ALLOW answers an unknown command tool")
            eq(decision, {P.STRICT: Decision.DENY, P.ASK: Decision.ALLOW, P.AUTO: Decision.ALLOW}[profile],
               f"{tag} npm publish: strict refuses, ask asks, auto is the reviewer's (as on main)")
            eq(len(asker.seen), 1 if profile is P.ASK else 0, f"{tag} asked only under ASK")
            before = len(asker.seen)
            permit("mcp__remote__exec", {"command": "git", "args": ["push", "--force", "origin", "x"]}, brief)
            eq(len(asker.seen), before + 1, f"{tag} a force push through argv is always-ask")
            eq(box.decisions[-1]["layer"], "always-ask", f"{tag} at layer 2")
            for request in asker.seen:
                eq(request.allowlistable, False, f"{tag} an unknown command tool's card offers no Always")
        # An Always on an unknown command tool writes nothing.
        config.ALLOWLIST_PATH.unlink()
        broker = PendingApprovals(on_request=lambda r: None)
        answers = []
        ask = brief_for(P.ASK)
        worker = threading.Thread(target=lambda: answers.append(build_permit(context(ask), broker)(
            "mcp__ops__deploy", {"command": "npm run deploy"}, ask)))
        worker.start()
        deadline = time.monotonic() + 5
        while not broker.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        if broker.pending():
            broker.resolve(broker.pending()[0].req_id, Decision.ALLOW, always=True)
        worker.join(5)
        eq(answers, [Decision.ALLOW], "the deploy card was answered once")
        ok(not config.ALLOWLIST_PATH.exists() or json.loads(config.ALLOWLIST_PATH.read_text()) == [],
           "an Always on an unknown command tool writes no entry (no npm stem for Bash)")
        # And the broker itself refuses, even for a request marked allowlistable.
        from jarvis.v2.approvals import ApprovalRequest
        broker = PendingApprovals(on_request=lambda r: None)
        request = ApprovalRequest(tool="mcp__ops__deploy", args={"command": "npm run deploy"},
                                  command="npm run deploy", allowlistable=True)
        worker = threading.Thread(target=lambda: broker.ask(request))
        worker.start()
        deadline = time.monotonic() + 5
        while not broker.pending() and time.monotonic() < deadline:
            time.sleep(0.005)
        if broker.pending():
            broker.resolve(broker.pending()[0].req_id, Decision.ALLOW, always=True)
        worker.join(5)
        ok(not config.ALLOWLIST_PATH.exists() or json.loads(config.ALLOWLIST_PATH.read_text()) == [],
           "the broker mints nothing for an unknown command tool, whatever the request says")

        # --- F2: Workflow's script is JavaScript: main's decisions exactly.
        CODE_TOOLS = getattr(approvals, "CODE_TOOLS", frozenset())   # lazily: runs against main
        for tool in ("Workflow", "AppifactRepl", "REPL"):
            ok(tool in CODE_TOOLS, f"{tool} is a code tool, not a shell")
        script = {"script": "const x = 'see .env docs'; await agent('rm -rf / is not run here')"}
        eq(is_command_tool("Workflow", script), False, "Workflow is never read as a shell line")
        eq(v1_request("Workflow", script), ("Workflow", script), "Workflow allowlists as a whole tool, as on main")
        for profile, want, layer in ((P.AUTO, Decision.ALLOW, "reviewer"), (P.STRICT, Decision.DENY, "human")):
            brief = brief_for(profile)
            eq(build_permit(context(brief), Asker(Decision.ALLOW))("Workflow", script, brief), want,
               f"[{profile.value}] a Workflow script mentioning .env is not refused at layer 1")
            eq(box.decisions[-1]["layer"], layer, f"[{profile.value}] Workflow answered where main answers it")
        config.ALLOWLIST_PATH.write_text(json.dumps([{"tool": "Workflow"}]))
        strict = brief_for(P.STRICT)
        eq(build_permit(context(strict), Asker(Decision.ALLOW))("Workflow", script, strict), Decision.ALLOW,
           "a whole-tool Workflow entry still covers its scripts, as on main")
        config.ALLOWLIST_PATH.unlink()

        # --- F3: a shell with no command text is refused; an unknown tool's
        # non-text command is asked.
        brief = brief_for(P.AUTO)
        for tool, args in (("Bash", {"command": ""}), ("Bash", {"command": {"x": 1}}),
                           ("Monitor", {"command": 5}), ("shell", {"command": []}),
                           ("run_command", {"command": None}), ("PowerShell", {"command": "  "})):
            asker = Asker(Decision.ALLOW)
            eq(build_permit(context(brief), asker)(tool, args, brief), Decision.DENY,
               f"{tool} {args}: nothing to judge, refused")
            eq((box.decisions[-1]["layer"], asker.seen), ("deny", []), f"{tool} {args}: at layer 1, unasked")
        for args in ({"command": {"op": "deploy"}}, {"command": 5}, {"cmd": ["npm", 3]}):
            asker = Asker(Decision.DENY)
            eq(build_permit(context(brief), asker)("mcp__x__run", args, brief), Decision.DENY,
               f"mcp__x__run {args}: asked, and the no holds")
            eq(len(asker.seen), 1, f"mcp__x__run {args}: put to the owner under AUTO")
            ok("command-unreadable" in box.decisions[-1]["reason"], f"mcp__x__run {args}: says why")
        eq(build_permit(context(brief), deny_all())("mcp__x__run", {"command": {"op": 1}}, brief),
           Decision.DENY, "with nobody to ask, an unreadable command is refused")
        asker = Asker(Decision.ALLOW)
        build_permit(context(brief), asker)("BashOutput", {"bash_id": "b1"}, brief)
        eq(asker.seen, [], "BashOutput with no command key is unchanged")


def main():
    for section in (matrix_checks, always_ask_checks, file_deny_checks,
                    human_backed_checks, log_checks, approvals_checks, hud_build_deny_checks,
                    env_file_checks,
                    installed_hud_deny_checks, daemon_and_rebuild_ops_checks, review_48_checks,
                    review_48_round2_checks, review_48_delta_checks, runner_checks,
                    third_delta_75_checks, daemon_checks,
                    auto_quiet_checks, hatch_checks, discord_checks, command_tool_checks):
        if ONLY and section.__name__ not in ONLY:
            continue
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
