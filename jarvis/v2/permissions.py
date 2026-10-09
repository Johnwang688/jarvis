"""The five permission layers of design §6, behind one callback.

`build_permit(ctx, asker)` returns the `PermissionCallback` every provider is
handed. The layers are evaluated in order and the first to answer wins:

1. **DENY** — v1 `rules.decide()`'s never-approvable verdicts, v1
   `secrets.protected_in_command`, and, for file tools, any target that is
   SELF_PROTECTED, the persistent allowlist, a credential bundle, or the v2
   `allowlist.json` / `models.json` / `routing.json` trio. Never asked of
   anyone, and unreachable through the escape hatch (§6.1).
2. **ALWAYS-ASK** — `config.V2_ALWAYS_ASK_DEFAULTS` plus the additions in
   `config.V2_ALWAYS_ASK` and `Brief.always_ask` / `Project.always_ask`. A
   match goes to the owner regardless of what any reviewer thought.
3. **CLI reviewer** — not here. See the ALLOW note below.
4. **Jarvis ALLOW** — the owner's persistent allowlist, or an ALLOW verdict
   from `rules`, honoured **only for a human-backed asker**. This is the v1
   invariant the 2026-08-09 round nearly shipped broken: a deny-all asker
   (strict profile, a thread nobody is watching) never auto-approves.
5. **Human** — profile ASK asks; profile AUTO returns ALLOW (layer 3 —
   the provider's own classifier still decides); profile STRICT denies.

One exception sits between layers 1 and 2: a call the provider marks as a
**sandbox widening** (`permit(..., widening="network on · …")`, Codex only)
is always asked of a human, with no Always, whatever the profile — strict
denies it — because the provider's reviewer has already passed it by the time
it reaches us, so an AUTO "the reviewer decides" would mean no one decided.

**`Decision.ALLOW` out of this callback means "not refused by Jarvis", not
"approved by the owner"** — except when layer 2 or layer 5 produced it, which
is the only case where a human actually said yes. Which layer answered is
carried on every `ApprovalRecord` written to
`config.V2_DATA_DIR/decisions.jsonl`, so the log can tell the two apart; a
surface that shows "the owner approved this" must read the layer, not the
decision. WP3's Claude hook maps an ALLOW under AUTO to `{}` (no opinion,
the classifier still runs) and under ASK to an explicit allow, which is the
same distinction seen from the provider side.

**R8 is not solved here and this file does not pretend otherwise.** Codex has
no universal pre-tool callback under `auto_review`, so layer 2 cannot be
enforced from the approval requests its reviewer chooses to escalate;
`CodexProvider` refuses a brief with a non-empty `always_ask` rather than
claim an enforcement it lacks. The seam is `always_ask_for()` below: whatever
mechanism answers R8 (a Codex pre-execution hook, or a `jarvis-mcp` tool with
the native equivalent removed from the toolset) consumes the same list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Iterable

from jarvis import config, permissions as v1_permissions, rules
from jarvis.tools import files as v1_files, secrets as v1_secrets
from .approvals import ApprovalRequest, DenyAll, label, v1_request
from .model import PermissionProfile, Project, Task, utcnow
from .provider import Brief, Decision, PermissionCallback

LOG = logging.getLogger(__name__)

# Layer names, as they appear in the decision log.
L_DENY, L_ALWAYS_ASK, L_REVIEWER, L_JARVIS_ALLOW, L_HUMAN = (
    "deny", "always-ask", "reviewer", "jarvis-allow", "human")
# A provider-reported sandbox widening (see `build_permit`'s `widening`).
L_WIDENING = "sandbox-widening"

# Tools that carry a shell command line, per provider.
COMMAND_TOOLS = frozenset({
    "Bash", "BashOutput",       # Claude
    "shell", "local_shell",     # Codex
    "run_command", "run_readonly",   # fast path / jarvis-mcp
})

# Tools that write a file. The argument holding the target differs per
# provider, so every plausible spelling is read rather than one guessed key:
# a file tool whose target we cannot find must not slip past layer 1.
FILE_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit",    # Claude
    "apply_patch", "patch",                           # Codex
    "write_file", "edit_file",                        # fast path / jarvis-mcp
})
_PATH_KEYS = ("file_path", "notebook_path", "path", "filename", "file")
_PATHS_KEYS = ("paths", "files", "file_paths")

_PROTECTED_BRANCHES = ("main", "master", "prod*", "production*", "release*")
_PROD_MARKER = re.compile(r"(?<![A-Za-z])(prod|production|live)(?![A-Za-z])", re.I)
_MIGRATE_PROD_PHRASES = (
    ("alembic", "upgrade"), ("rails", "db:migrate"), ("rake", "db:migrate"),
    ("flyway", "migrate"), ("dbmate", "up"),
)
_MIGRATE_ALWAYS_PHRASES = (
    ("prisma", "migrate", "deploy"), ("supabase", "db", "push"),
)
_CREDENTIAL_PHRASES = (("gh", "auth"), ("vercel", "login"), ("aws", "configure"))
_CREDENTIAL_STEMS = frozenset({"ssh-keygen", "ssh-copy-id", "ssh-add"})

_log_lock = threading.Lock()


# --------------------------------------------------------------------------
# context


@dataclass
class PermitContext:
    """Who is asking. `task` is None for a chat thread."""
    thread_id: str
    brief: Brief
    provider: str = ""
    project: Project | None = None
    task: Task | None = None

    @property
    def task_id(self) -> str | None:
        if self.task is not None:
            return self.task.id
        return self.brief.task_id if self.brief is not None else None

    def origin(self) -> str:
        """The sanitized attribution an approval surface shows (v1 `_label`)."""
        task_id = self.task_id
        if task_id is None:
            return ""
        name = self.task.brief if self.task is not None else ""
        return label(task_id, name)


@dataclass
class ApprovalRecord:
    tool: str
    args_digest: str
    layer: str
    decision: str
    reason: str = ""
    command: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    at: str = field(default_factory=utcnow)

    def to_json(self) -> dict:
        return dict(self.__dict__)


def _digest(args: dict) -> str:
    """A stable fingerprint of the arguments. A digest, not the arguments:
    the log is a durable file and a tool call can carry anything."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def log_decision(record: ApprovalRecord, path: Path | None = None) -> None:
    """Append to the v2 decision log. A log that cannot be written must never
    be a reason a decision fails, so every error here is swallowed after a
    warning — the decision itself already happened."""
    target = Path(path) if path is not None else Path(config.V2_DATA_DIR) / "decisions.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json(), allow_nan=False) + "\n"
        with _log_lock:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        LOG.warning("Cannot write the v2 decision log at %s", target, exc_info=True)


# --------------------------------------------------------------------------
# layer 1 — DENY


def protected_paths() -> set[Path]:
    """The v2 config trio, v1's allowlist and the Discord guild file, resolved.

    Derived from `config.ALLOWLIST_PATH`'s directory rather than hard-coded at
    `~/.config/jarvis`, so a test that repoints the allowlist repoints the trio
    with it and never writes near the owner's real files.
    """
    allow = Path(config.ALLOWLIST_PATH).expanduser()
    directory = allow.parent
    # discord_guild.json (B1) is not permission state but decides where Jarvis
    # may create, rename and move channels: only `jarvis auth discord-guild`
    # writes it.
    names = ("allowlist.json", "models.json", "routing.json", "discord_guild.json")
    paths = {allow, Path(config.MODELS_PATH).expanduser(),
             Path(config.DISCORD_GUILD_PATH).expanduser()}
    paths |= {directory / name for name in names}
    return {_resolve(p) for p in paths}


def _resolve(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def file_targets(tool: str, args: dict) -> list[str]:
    """Every path a file tool would write. Several spellings are read because
    the key differs per provider and a target we fail to find is a target
    layer 1 fails to protect."""
    found: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
    for key in _PATHS_KEYS:
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            found.extend(v for v in value if isinstance(v, str) and v.strip())
    for edit in args.get("edits", []) if isinstance(args.get("edits"), list) else []:
        if isinstance(edit, dict):
            for key in _PATH_KEYS:
                value = edit.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value)
    return found


def denied_file(target: str) -> str | None:
    """The reason this path may not be written, or None."""
    path = Path(target).expanduser()
    if v1_secrets.is_protected(path):
        return (f"{path.name} holds live credentials; Jarvis never writes it. "
                "Ask the owner to change it by hand.")
    resolved = _resolve(path)
    if resolved in protected_paths():
        return (f"{path.name} is Jarvis's own permission state. Changing it "
                "would widen what runs without anyone being asked, so it is "
                "the owner's to edit by hand.")
    try:
        if v1_files._self_protected(path):
            return (f"{path.name} is part of Jarvis's safety layer and can only "
                    "be changed by the owner by hand. Say what you wanted "
                    "changed and why instead.")
    except Exception:
        LOG.warning("SELF_PROTECTED check failed for %s", target, exc_info=True)
    return None


def _command_of(args: dict) -> str:
    """The command line in a tool's arguments, whatever the provider calls it."""
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            # Codex hands an argv array; join it so the rules see one line.
            return " ".join(value)
    return ""


def denied_command(command: str) -> str | None:
    """The reason this command may not run at all, or None.

    Two sources, both v1's and both unchanged: the never-approvable verdicts
    (sudo, dd/mkfs, `rm -rf` at a root, fork bombs, raw block writes) and the
    secrets layer, which refuses a command that *names* a credential file
    because approving a command is not consent to what it prints.
    """
    if not command.strip():
        return None
    verdict = rules.decide(command)
    if verdict.decision == rules.DENY:
        return verdict.reason or "refused by Jarvis's never-approvable rules"
    name = v1_secrets.protected_in_command(command)
    if name:
        return v1_secrets.refusal(name)
    touched = _command_writes_protected_state(command)
    if touched:
        return (f"{touched} is Jarvis's own permission state; a command that "
                "writes it would widen what runs without anyone being asked.")
    return None


_STATE_WRITERS = frozenset({
    "tee", "sed", "cp", "mv", "rm", "install", "truncate", "dd", "shred",
    "chmod", "chown", "ln", "touch", "python", "python3", "perl", "node",
})


def _command_writes_protected_state(command: str) -> str | None:
    """A narrow, honest check: a redirect onto the allowlist, or a writing stem
    given it as an argument.

    It is not a boundary and the code says so — a shell has more spellings than
    any matcher has patterns, which is the same caveat `rules.py`'s DENY
    section already carries. It closes the obvious spelling of the hole the v1
    round found in `edit_file`; the real protection is that the file's *tool*
    path is refused above and that nothing auto-approves an unrecognised
    command.
    """
    protected = protected_paths()
    for segment in rules.segments(command):
        tokens = rules._tokens(segment)
        named = None
        for token in tokens:
            candidate = token.strip("\"'><")
            if not candidate or candidate.startswith("-"):
                continue
            if _resolve(candidate) in protected:
                named = Path(candidate).name
                break
        if named is None:
            # A redirect glues the path to the operator (`>~/.config/...`).
            for protected_path in protected:
                if protected_path.name in segment and rules.redirects_to_file(segment):
                    named = protected_path.name
                    break
            if named is None:
                continue
        if rules.redirects_to_file(segment):
            return named
        stems = [stem for stem, _ in rules.command_targets(segment)]
        if any(stem in _STATE_WRITERS for stem in stems):
            return named
    return None


# --------------------------------------------------------------------------
# layer 2 — always-ask


def _load_additions() -> list:
    """The additions file. Additions only: §6 lets a project add to this layer
    and not remove from it, and the same has to hold for a file, or a layer
    the owner cannot see could be disarmed by emptying it. An unreadable file
    costs the additions and never the shipped defaults."""
    path = Path(config.V2_ALWAYS_ASK).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Ignoring unreadable always-ask additions at %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        LOG.warning("Ignoring always-ask additions at %s: not a JSON list", path)
        return []
    return [entry for entry in raw if isinstance(entry, (str, dict))]


def always_ask_for(brief: Brief | None = None, project: Project | None = None) -> list:
    """Every rule in force: shipped defaults + file + project + brief.

    This is the R8 seam. Codex cannot enforce layer 2 from its reviewer's
    escalations alone, so whatever answers R8 — a Codex pre-execution hook, or
    a `jarvis-mcp` tool that asks with the native equivalent removed from the
    toolset — reads its list from here rather than growing a second copy.
    """
    entries: list = list(config.V2_ALWAYS_ASK_DEFAULTS)
    entries += _load_additions()
    if project is not None:
        entries += [e for e in project.always_ask if isinstance(e, (str, dict))]
    if brief is not None:
        entries += [e for e in brief.always_ask if isinstance(e, (str, dict))]
    return entries


def _tokens_of(segment: str) -> list[str]:
    return rules.unwrap(rules._tokens(segment))


def _any_token(patterns: Iterable[str], tokens: list[str]) -> bool:
    return any(fnmatch(token, pattern) for pattern in patterns for token in tokens)


def _phrase_in(phrase: tuple[str, ...], tokens: list[str]) -> bool:
    """A contiguous run of tokens, fnmatch per token.

    `("vercel", "domains", "buy")` matches `vercel domains buy example.com`
    and `npx prisma migrate deploy` matches `("prisma","migrate","deploy")`,
    which is why this is a subsequence test and not a stem test.
    """
    width = len(phrase)
    if not width or width > len(tokens):
        return False
    return any(all(fnmatch(tokens[i + j], phrase[j]) for j in range(width))
               for i in range(len(tokens) - width + 1))


def _phrase_ordered(phrase: tuple[str, ...], tokens: list[str]) -> bool:
    """The phrase's tokens in order, with anything allowed between them.

    Contiguity is the wrong test for a CLI subcommand, and `flyway
    -url=jdbc:postgresql://prod/app migrate` is why: the flags sit *between*
    the program and its verb, so a contiguous match saw no migration at all
    and the whole prod-migration rule silently did not fire. Order still
    matters — `deploy` before `migrate` is a different command — and every
    part must be an exact token, so `--deploy-preview` is not a `deploy`.
    """
    index = 0
    for part in phrase:
        while index < len(tokens) and not fnmatch(tokens[index], part):
            index += 1
        if index >= len(tokens):
            return False
        index += 1
    return True


def _refspec_targets(tokens: list[str]) -> list[str]:
    """The branch names a `git push` names, in any spelling."""
    if "push" not in tokens:
        return []
    after = tokens[tokens.index("push") + 1:]
    targets = []
    for token in after:
        if token.startswith("-"):
            continue
        ref = token.split(":")[-1].lstrip("+")
        for prefix in ("refs/heads/", "heads/"):
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
        if ref:
            targets.append(ref)
    return targets


def _git_push(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] == "git" and "push" in tokens


def _kind_matches(kind: str, segment: str, tokens: list[str], stems: list[str]) -> bool:
    if kind == "git_push_protected":
        # A bare `git push` with no refspec is deliberately *not* matched: the
        # branch it would push is not in the command, and in a task worktree it
        # is `jarvis/<slug>`, never main. Asking about every push would teach
        # approve-without-reading, which is the failure this layer exists to
        # prevent. Recorded as a known gap in WP5-notes.md.
        return _git_push(tokens) and any(
            fnmatch(ref.lower(), pattern) for ref in _refspec_targets(tokens)
            for pattern in _PROTECTED_BRANCHES)
    if kind == "git_push_force":
        return _git_push(tokens) and any(
            token == "-f" or token.startswith("--force") for token in tokens)
    if kind == "migration_prod":
        return (any(_phrase_ordered(p, tokens) for p in _MIGRATE_PROD_PHRASES)
                and bool(_PROD_MARKER.search(segment)))
    if kind == "migration_always":
        return any(_phrase_ordered(p, tokens) for p in _MIGRATE_ALWAYS_PHRASES)
    if kind == "credential_command":
        return (any(_phrase_ordered(p, tokens) for p in _CREDENTIAL_PHRASES)
                or any(stem in _CREDENTIAL_STEMS for stem in stems))
    if kind == "credential_path":
        return any(_under_credential_dir(token) for token in tokens)
    LOG.warning("Ignoring always-ask rule with unknown kind %r", kind)
    return False


def _credential_dirs() -> list[Path]:
    return [_resolve(d) for d in config.V2_CREDENTIAL_DIRS]


def _under_credential_dir(target: str) -> bool:
    candidate = target.strip("\"'><")
    if not candidate or candidate.startswith("-"):
        return False
    if "/" not in candidate and not candidate.startswith("~"):
        return False
    resolved = _resolve(candidate)
    for directory in _credential_dirs():
        if resolved == directory or directory in resolved.parents:
            return True
    return False


def _rule_matches(rule, tool: str, segments: list[tuple[str, list[str], list[str]]],
                  targets: list[str]) -> tuple[bool, str, str]:
    """(matched, rule id, reason) for one rule against one tool call."""
    if isinstance(rule, str):
        phrase = tuple(part for part in rule.split() if part)
        if not phrase:
            return False, "", ""
        if len(phrase) == 1 and phrase[0] == tool:
            return True, rule, "named by an always-ask addition"
        for _segment, tokens, _stems in segments:
            if _phrase_in(phrase, tokens):
                return True, rule, "named by an always-ask addition"
        return False, "", ""

    if not isinstance(rule, dict):
        return False, "", ""
    rule_id = str(rule.get("id") or rule.get("kind") or "addition")
    reason = str(rule.get("reason", ""))
    tools = rule.get("tools") or []
    if isinstance(tools, (list, tuple)) and tool in tools:
        return True, rule_id, reason
    has_command_criteria = any(k in rule for k in ("kind", "stem", "all", "any", "none"))
    if not has_command_criteria:
        return False, "", ""

    kind = rule.get("kind")
    stem = rule.get("stem")
    stems_wanted = [stem] if isinstance(stem, str) else list(stem or [])
    require_all = list(rule.get("all") or [])
    require_any = list(rule.get("any") or [])
    forbid = list(rule.get("none") or [])

    for segment, tokens, stems in segments:
        if kind is not None:
            if _kind_matches(str(kind), segment, tokens, stems):
                return True, rule_id, reason
            continue
        if stems_wanted and not any(s in stems_wanted for s in stems):
            continue
        if require_all and not all(_any_token([p], tokens) for p in require_all):
            continue
        if require_any and not _any_token(require_any, tokens):
            continue
        if forbid and _any_token(forbid, tokens):
            continue
        return True, rule_id, reason

    # A path rule also applies to a file tool's targets, which have no segment.
    if kind == "credential_path" and any(_under_credential_dir(t) for t in targets):
        return True, rule_id, reason
    return False, "", ""


def always_ask_match(tool: str, args: dict, brief: Brief | None = None,
                     project: Project | None = None) -> tuple[str, str] | None:
    """(rule id, reason) if this call is on the always-ask list, else None."""
    command = _command_of(args) if tool in COMMAND_TOOLS else ""
    segments: list[tuple[str, list[str], list[str]]] = []
    for segment in rules.segments(command) if command.strip() else []:
        tokens = _tokens_of(segment)
        stems = [s for s, _ in rules.command_targets(segment)] or (
            [tokens[0].rsplit("/", 1)[-1]] if tokens else [])
        segments.append((segment, tokens, stems))
    targets = file_targets(tool, args) if tool in FILE_TOOLS else []
    for rule in always_ask_for(brief, project):
        matched, rule_id, reason = _rule_matches(rule, tool, segments, targets)
        if matched:
            return rule_id, reason
    return None


# --------------------------------------------------------------------------
# the callback


def build_permit(ctx: PermitContext, asker) -> PermissionCallback:
    """The §6 callback for one thread. `asker` is a `PendingApprovals` or a
    `DenyAll`; its `human_backed` flag is what layer 4 turns on."""

    def permit(tool: str, args: dict, brief: Brief | None = None, *,
               widening: str | None = None) -> Decision:
        """`widening` is set only by a provider whose approval would widen
        its own sandbox if accepted (Codex: extra permissions, network, a
        grant root, terminal input). Such a call is asked of a human every
        time — never ALLOWed by layer 4 or 5, never Always — and a deny-all
        asker, no surface or a timeout declines it. It is a keyword the model
        cannot reach: it never rides in `args`."""
        brief = brief if brief is not None else ctx.brief
        args = dict(args or {})
        profile = brief.profile if brief is not None else PermissionProfile.AUTO
        command = _command_of(args) if tool in COMMAND_TOOLS else ""

        def finish(decision: Decision, layer: str, reason: str) -> Decision:
            log_decision(ApprovalRecord(
                tool=tool, args_digest=_digest(args), layer=layer,
                decision=decision.value, reason=reason,
                command=_safe_command(command), thread_id=ctx.thread_id,
                task_id=ctx.task_id, provider=ctx.provider or None))
            return decision

        # --- layer 1: never approvable, never asked of anyone --------------
        if tool in COMMAND_TOOLS and command.strip():
            refusal = denied_command(command)
            if refusal:
                return finish(Decision.DENY, L_DENY, refusal)
        if tool in FILE_TOOLS:
            for target in file_targets(tool, args):
                refusal = denied_file(target)
                if refusal:
                    return finish(Decision.DENY, L_DENY, refusal)

        # --- sandbox widening: a human, every time ------------------------
        # A Codex approval reaches us only after its own reviewer passed it,
        # so the AUTO answer below ("the reviewer decides") would mean nobody
        # decided. Accepting one also accepts the grant riding on it, and the
        # reply cannot strip that. So this asks whatever the profile, and an
        # ALWAYS is never offered: a standing yes to a widening is a wider box.
        if widening:
            if profile == PermissionProfile.STRICT:
                return finish(Decision.DENY, L_WIDENING,
                              f"strict profile refuses a sandbox widening: {widening}")
            match = always_ask_match(tool, args, brief, ctx.project)
            reason = widening + (f" (also always-ask rule {match[0]})" if match else "")
            decision = _ask(asker, ctx, tool, args, command, reason, L_WIDENING,
                            allowlistable=False, headline=widening)
            return finish(decision, L_WIDENING, reason)

        # --- layer 2: the owner is asked whatever the reviewer thought -----
        match = always_ask_match(tool, args, brief, ctx.project)
        if match is not None:
            rule_id, why = match
            reason = f"always-ask rule {rule_id}" + (f": {why}" if why else "")
            decision = _ask(asker, ctx, tool, args, command, reason, L_ALWAYS_ASK)
            return finish(decision, L_ALWAYS_ASK, reason)

        # --- layer 4: the owner's standing yes, human-backed only ----------
        # Layer 3 (the CLI reviewer) is not evaluated here; see the module
        # docstring for what an ALLOW out of this callback does and does not
        # mean.
        if getattr(asker, "human_backed", False):
            if v1_permissions.allows(*v1_request(tool, args)):
                return finish(Decision.ALLOW, L_JARVIS_ALLOW,
                              "covered by the owner's persistent allowlist")
            if command.strip() and rules.decide(command).decision == rules.ALLOW:
                return finish(Decision.ALLOW, L_JARVIS_ALLOW,
                              "ordinary development command (rules ALLOW)")

        # --- layer 5: profile decides -------------------------------------
        if profile == PermissionProfile.STRICT:
            return finish(Decision.DENY, L_HUMAN,
                          "strict profile: nothing runs that the owner has not "
                          "already allowlisted")
        if profile == PermissionProfile.ASK:
            reason = "ask profile: every gated call reaches the owner"
            decision = _ask(asker, ctx, tool, args, command, reason, L_HUMAN)
            return finish(decision, L_HUMAN, reason)
        return finish(Decision.ALLOW, L_REVIEWER,
                      "no opinion; the provider's own reviewer decides")

    return permit


def _ask(asker, ctx: PermitContext, tool: str, args: dict, command: str,
         reason: str, layer: str, *, allowlistable: bool = True,
         headline: str = "") -> Decision:
    request = ApprovalRequest(
        tool=tool, args=args, command=command or None, reason=reason, layer=layer,
        thread_id=ctx.thread_id, task_id=ctx.task_id,
        provider=ctx.provider or None, origin=ctx.origin(),
        allowlistable=allowlistable, headline=headline)
    try:
        return Decision(asker.ask(request))
    except Exception:       # an asker that raises has not approved anything
        LOG.exception("Approval channel failed for %s; denying", tool)
        return Decision.DENY


def _safe_command(command: str) -> str | None:
    """The command as the log keeps it: scrubbed of credential values and
    capped. Layer 1 already refuses a command that names a credential file;
    this is the second half of the same rule for one that merely carries a
    value."""
    if not command:
        return None
    try:
        scrubbed = v1_secrets.scrub(command)
    except Exception:
        scrubbed = "[unscrubbable command withheld]"
    return scrubbed[:2000]


def deny_all(reason: str = "nobody is watching this thread") -> DenyAll:
    return DenyAll(reason)


__all__ = [
    "ApprovalRecord", "PermitContext", "always_ask_for", "always_ask_match",
    "build_permit", "denied_command", "denied_file", "deny_all", "file_targets",
    "log_decision", "protected_paths", "COMMAND_TOOLS", "FILE_TOOLS",
]
