"""Filesystem reads and writes.

Writes carry a read-before-write invariant: the agent must read a file before
overwriting it, so it can never clobber content it has not seen.

Reads are **paged and line-numbered**. Both halves matter. Paging is what makes
a long file reachable at all — the old reader cut every file at 40k characters
with no way to ask for the rest, so `CLAUDE.md` (116KB) was permanently visible
only down to a third of the way, and `skills/self-improve.md`'s first
instruction ("read CLAUDE.md first") was quietly reading a truncated file.
Numbering is what makes `edit_file` reliable: the model can see where it is,
and a stated line number is checkable.

`edit_file` is the important one. Rewriting a whole file to change three lines
costs the model the entire file every time, and models drop content when they
re-emit at length — the read-before-write stamp catches *staleness*, never
*truncation*. Anchored replacement makes the cost proportional to the edit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated

from .. import config
from . import tool
from .secrets import is_credential, is_env_credential, refusal, write_refusal

_seen: dict[str, float] = {}

# Per-call ceilings. A read that hits either is not an error and not a dead end:
# the footer says exactly which lines came back and how to ask for the next
# page, so nothing is unreachable the way it used to be.
MAX_READ_CHARS = 40_000
DEFAULT_LINES = 1500

# Jarvis may improve his own code (see skills/self-improve.md), but not the
# layers that gate him: rewriting the approval broker, the permission gate,
# the secrets scrubber, dispatch, or this guard would let a bad edit (or an
# injected instruction) disarm the harness from inside. These change only by
# the owner's hand — including an *approved* run_command, which is the owner
# consenting per edit.
#
# Every write path in this module checks this, and `edit_file` is why the list
# has to be checked in more than one place now: a surgical replacement in
# permissions.py would disarm the gate exactly as thoroughly as overwriting it,
# so a new write tool that forgot this check would be a hole straight through
# the boundary rather than a smaller version of one.
SELF_PROTECTED = frozenset(
    {
        "jarvis/tools/secrets.py",
        "jarvis/tools/files.py",
        "jarvis/tools/__init__.py",
        "jarvis/face/approvals.py",
        "jarvis/permissions.py",
        # Decides which commands run with no human in the loop, and which are
        # refused outright. An agent that could edit these could widen its own
        # reach without anyone being asked — the same reasoning as the gate
        # itself, which is why they join the list rather than sitting beside it.
        "jarvis/rules.py",
        "jarvis/command_review.py",
        "jarvis/v2/browser/act_rules.py",
        "jarvis/v2/browser/act_review.py",
        # BR9b's dormant OFF gate, caller/ref ownership, credential scan,
        # per-event execution and exact-file release are authorization code.
        "jarvis/v2/browser/drive.py",
        "jarvis/v2/browser/drive_state.py",
        "jarvis/v2/browser/drive_scan.py",
        "jarvis/v2/browser/drive_execute.py",
        "jarvis/v2/browser/drive_files.py",
        "jarvis/v2/browser/drive_downloads.py",
        "jarvis/v2/tools/hud_browser_act.py",
        # Decides which shell commands may write the gate's own state files
        # (2026-10-08). Editing it is editing what never runs.
        "jarvis/protected_state.py",
        # Decides what git an unattended workflow may run, and where
        # (2026-10-09), and the tools that expose it. Same reasoning as the
        # rules: an agent that could edit the judge could widen it.
        "jarvis/gitops.py",
        "jarvis/tools/gitctl.py",
        # Decide what of the owner's terminal output reaches the model
        # (WP-F, 2026-10-10): the read guard, the credential formats it and
        # the renderer it reads through, and the tool. Editing them is editing
        # what a read refuses.
        "jarvis/credential_patterns.py",
        "jarvis/v2/terminal_guard.py",
        "jarvis/v2/terminal_text.py",
        "jarvis/v2/tools/terminal_read.py",
        # Decide which chat may call Jarvis through jarvis-mcp, with which
        # token, which tools it is offered, and how its calls are gated
        # (2026-10-10, plan docs/plans/2026-10-10-terminal-read-mcp-plan.md).
        # An agent that could edit them could hand itself a token or a tool.
        "jarvis/v2/callers.py",
        "jarvis/v2/bridge.py",
        "jarvis/v2/session_tools.py",
        "jarvis/v2/mcp_catalog.py",
        "jarvis/v2/desk.py",
        "jarvis/v2/mcp_session.py",
        # What one HUD chat may read of another, and (later) send it: the
        # chat tools and the daemon service behind them (decision CC-8,
        # 2026-10-19). Editing them is editing what a read withholds.
        "jarvis/v2/tools/chats.py",
        "jarvis/v2/chat_relay.py",
        # Decides what the HUD's editor may show and write, and what an
        # `@path`, the preview origin and a task diff withhold (ED-2,
        # 2026-10-11). Editing it is editing which credentials the window
        # serves.
        "jarvis/v2/editor_policy.py",
        # The editor's git routes and its navigation/explorer routes (ED-9,
        # ED-7): what HEAD's text, a status, a listing, a search or an fs op
        # may reveal or touch. The owner's answer, 2026-10-12.
        "jarvis/v2/editor_git.py",
        "jarvis/v2/editor_nav.py",
        # The v2 permit's five layers and the owner-only repository gate
        # (merging or closing a pull request, a push to main, a branch
        # deletion, a force push). The owner's decision, 2026-10-15: an agent
        # that could edit them could answer for the owner. Read-only in the
        # HUD editor too, so the owner changes them in a worktree, not there.
        "jarvis/v2/permissions.py",
        "jarvis/v2/merge_gate.py",
        # The owner inbox (design 2026-10-24, §9): it decides what the owner
        # sees as waiting and routes each answer to its source's own
        # function. An agent that could edit it could hide a card or answer
        # one.
        "jarvis/v2/inbox.py",
        # Its PR source (inbox B3, IN-4): which repositories are read with
        # the owner's gh login, and what a PR row may say.
        "jarvis/v2/inbox_prs.py",
        # The owner's decisions (inbox B1, IN-5): the store, its caps and
        # scrub, who may post, and the one tool that posts. An agent that
        # could edit them could lift the caps, skip the scrub or post from a
        # turn the owner never started.
        "jarvis/v2/decisions.py",
        "jarvis/v2/tools/owner_post.py",
        # The owner's live checks (inbox B2, IN-6): their store, caps and
        # scrub, the deployed test that gates a mark and the runbook import.
        # An agent that could edit it could mark a step for the owner or let
        # a check be marked before its PR runs.
        "jarvis/v2/checks.py",
        # The owner's snoozes and dismissals (inbox B4, IN-7): which rows of
        # rank 5 to 8 stay out of sight, and the rule that a mark never
        # hides a live approval, question, proposal or blocked task. An
        # agent that could edit it could hide one.
        "jarvis/v2/inbox_marks.py",
        # The phone's remote listener (phone design PH-1, P-1): its route
        # table, strict Host, Origin rule and headers decide what the
        # tailnet can reach on this machine. An agent that could edit it
        # could open a HUD route to the phone's port.
        "jarvis/v2/remote_listener.py",
        # The phone's passkeys, pairing window and sessions (phone design
        # PH-3..PH-6, P-2): it decides who counts as the owner off this
        # machine. An agent that could edit it could pair its own passkey.
        "jarvis/v2/remote_auth.py",
        # The phone's read API and event stream (phone design §5, P-3): it
        # decides what the phone can read off this machine and what each
        # read withholds. An agent that could edit it could send the phone
        # a private card's detail, a path or a credential.
        "jarvis/v2/remote_api.py",
        # Answering from the phone (phone design P-4): the per-answer
        # passkey assertion, who gets the HUD's authority and
        # what a card must show first; and §4.1's checks that grant it. An
        # agent that could edit either could answer an approval from the
        # phone with the HUD's authority.
        "jarvis/v2/remote_answer.py",
        "jarvis/v2/remote_secure.py",
        # Quiet hours (phone design §7, the owner's answer of 2026-10-28):
        # when nothing is pushed to the owner's phone, and (next) when an
        # approval waits for the morning. An agent that could edit it could
        # keep the owner from hearing about a card.
        "jarvis/v2/quiet.py",
        # The phone's Web Push (P-6): what leaves this machine for the
        # phone and where to. An agent that could edit it could send a
        # card's text in a push, or push to an address of its choosing.
        "jarvis/v2/push.py",
        # Writing from the phone (phone design P-5): messages, new chats and
        # task verbs, and which verb takes the passkey. An agent that could
        # edit it could give a phone message the desk's authority or cancel
        # a task without the passkey.
        "jarvis/v2/remote_write.py",
        # Push to talk from the phone (phone design P-7): what the phone's
        # microphone reaches (the desk's dictation path, text back, never
        # sent) and how much audio the listener reads for it. An agent that
        # could edit it could route a clip past the owner's local-only
        # switch or make a transcription send or answer.
        "jarvis/v2/remote_stt.py",
        # Approvals asked during quiet hours (QH, 2026-10-28): which asks
        # are deferred unasked, and the owner's single-use inbox grants that
        # the broker honours with no card. An agent that could edit it could
        # mint itself a grant.
        "jarvis/v2/quiet_defer.py",
        # The morning review (MR, 2026-10-28): which deferred calls one
        # owner confirmation grants as a set, and what a group's passkey
        # assertion is bound to. An agent that could edit it could widen a
        # set the owner approved.
        "jarvis/v2/review.py",
        # The approval broker every surface answers through: who counts as
        # owner-verified, which cards are HUD-only, and what a card shows
        # before Allow (the owner's decision, 2026-10-25). An agent that
        # could edit it could answer its own approval.
        "jarvis/v2/approvals.py",
        # Approval summaries (AS, 2026-10-28): the model-written line and
        # the code-built Where line on every approval card, and what of a
        # card leaves for the model. An agent that could edit it could put
        # its own words on the card the owner reads before Allow.
        "jarvis/v2/approval_summary.py",
        # The engine approval summaries run on (PM-A, 2026-10-28): the fence,
        # the caps and the cleaning hook between the model and the card. The
        # same reason as approval_summary.py, which it was factored out of.
        "jarvis/v2/summary_engine.py",
        # PR summaries (PM-C, 2026-10-28): the model-written lines on an inbox
        # PR row, what of a PR leaves for the model, the day's caps, and the
        # actions code puts beside them. An agent that could edit it could
        # put its own words, or its own buttons, on a PR the owner merges.
        "jarvis/v2/pr_summary.py",
    }
)

# `%6d\t` — the prefix read_file puts on every line. Matched here so a model
# that pastes a numbered excerpt straight back into edit_file still lands its
# edit instead of failing to match (see `_strip_numbering`).
_NUMBERED = re.compile(r"^( *\d+)\t")
# read_file right-aligns in a 6-wide field, so its prefix is always >= 6
# characters before the tab. Requiring that width is what separates "this is
# read_file output" from "this is a TSV whose first column happens to count
# 1, 2, 3" — which is the difference between a helpful strip and a corrupted
# data file.
_NUMBER_FIELD = 6


# SELF_PROTECTED freezes the gate's *code*. Nothing froze the gate's **data**,
# and that was a hole straight through it (found 2026-08-17).
#
# `config.ALLOWLIST_PATH` lives at ~/.config/jarvis/allowlist.json — deliberately
# outside the repo, because it is machine-local safety config. But the check
# above is `relative_to(config.REPO_ROOT)`, so *outside the repo* meant
# *unprotected by construction*; `allowlist.json` is not a credential name, so
# secrets.py did not cover it either; and neither write tool is `dangerous`. So
# `read_file` (to satisfy read-before-write) plus one ungated `edit_file` could
# put `{"tool": "run_command"}` in that file, and `permissions.load_allowlist()`
# re-reads it on every check — blanket auto-approval of every dangerous tool, on
# every surface, no restart, nobody asked, and indistinguishable afterwards from
# an entry the owner created by answering "always". Every agent has a write tool:
# both are in workflows.SAFE_TOOLS and SUBAGENT_TOOLS, so a background workflow
# that cannot itself run a dangerous tool could still unlock the owner's surfaces.
#
# The fix keys on the **identity of the file**, not its position relative to the
# repo, because that is what the protection was always about. A path set, rather
# than adding the name to secrets.PROTECTED_NAMES: this file is not a credential,
# it must stay *readable* (the HUD and the owner both inspect it), and borrowing
# the credential refusal would tell the model something untrue about why. Read
# from `config` per call, not captured at import, so a test that repoints
# ALLOWLIST_PATH at a temp file protects the temp file — the real one must never
# be what a suite exercises.
#
# No longer reachable by run_command either (2026-10-08): a shell command that
# writes these files is ASK whatever the allowlist says, and one that provably
# writes the allowlist is refused outright — jarvis/protected_state.py. That
# reverses the older exception here; SELF_PROTECTED's *code* files keep it.
#
# Widened 2026-10-08 (PR #15 review) to the set v2 already protects
# (`jarvis.v2.permissions.protected_paths`): the model roster, the Claude and
# Codex chat defaults, role routing and the Discord guild file decide which
# model answers and where Jarvis may act, and a v1 `write_file` — a
# background workflow's included — could rewrite every one of them. Built
# here rather than imported from v2 so the v1 tools never import the daemon's
# package; a suite asserts the two sets are equal, so they cannot drift. The
# no-tool guards (models_check, fastpath_check) let this one function name
# those files — it only ever refuses them — and nothing else in the tools.
#
# Since 2026-10-08 it is the only copy: `jarvis.protected_state` (the shell
# check) and `jarvis.v2.permissions.protected_paths` both return this set, and
# `config.ROUTING_PATH` joined it — `JARVIS_ROUTING` can put the live routing
# file outside the allowlist's directory. The saved Codex catalog joined it in
# PR #20: it is loaded as the Codex model table at daemon start.
def _protected_state_paths() -> set[Path]:
    """The gate's state files as configured, unresolved: the editor's read
    path (`v2/editor_policy.py`) needs to know which of them is a symlink."""
    # `vex.json` (VX-1, 2026-10-10) names the executables the HUD's VEX
    # Build, Download and brain terminal run when the owner clicks: an agent
    # that could repoint it could choose what runs on that click.
    # `browser.json` (BR-1a, BR-8a, 2026-10-12) holds the owner's Browser
    # switches: the LAN and every loopback server (an agent that could flip
    # them could open the owner's home network, or every server on this
    # machine, to the pages his Browser loads) and "open dev servers
    # automatically".
    # `stt.json` (V13, 2026-10-13) holds the owner's speech-to-text switches,
    # among them "never upload audio" and which backend hears the microphone
    # (an agent that could flip them could send the owner's voice to the cloud).
    # A pure function of these settings, `HOME` (for `~`) and the record
    # lists: kept per key, so a suite that repoints one gets its own set,
    # and a gate call does not rebuild ~30 paths (review of PR #315). What
    # they resolve to on disk is never kept (`_resolve_all`).
    key = (str(config.ALLOWLIST_PATH), str(config.MODELS_PATH), str(config.PROVIDER_DEFAULTS_PATH),
           str(config.DISCORD_GUILD_PATH), str(config.ROUTING_PATH), str(config.CODEX_CATALOG_PATH),
           str(config.VEX_PATH), str(config.BROWSER_SETTINGS_PATH), str(config.STT_SETTINGS_PATH),
           str(config.SPEAKER_DIR), str(config.V2_DATA_DIR), os.environ.get("HOME", ""),
           RECORD_TREES, RECORD_FILES)
    found = _STATE_PATHS.get(key)
    if found is not None:
        return set(found)
    names = ("allowlist.json", "models.json", "routing.json", "discord_guild.json",
             "provider_defaults.json", "vex.json", "browser.json", "stt.json")
    allow = config.ALLOWLIST_PATH.expanduser()
    paths = {allow, config.MODELS_PATH.expanduser(), config.PROVIDER_DEFAULTS_PATH.expanduser(),
             config.DISCORD_GUILD_PATH.expanduser(), config.ROUTING_PATH.expanduser(),
             config.CODEX_CATALOG_PATH.expanduser(), config.VEX_PATH.expanduser(),
             config.BROWSER_SETTINGS_PATH.expanduser(), config.STT_SETTINGS_PATH.expanduser()}
    paths |= {allow.parent / name for name in names}
    # The speaker store's two files that switch the voice check off (V12, V-12b,
    # owner 2026-10-14): `voiceprint.json` (whose voice is the owner's) and
    # `guest.json` (Guest voice). An agent that could rewrite or delete them could turn
    # the check that keeps other people's voices from the cloud off without anyone
    # being asked. Their folder is an ancestor, so deleting it is covered too. The
    # readings and the built-in-voice cache beside them are not protected.
    speaker = config.SPEAKER_DIR.expanduser()
    paths |= {speaker / "voiceprint.json", speaker / "guest.json"}
    # The phone's paired passkeys and sessions (phone design PH-6, P-2,
    # `v2/remote_auth.py`): an agent that could write `passkeys.json` could
    # pair a key of its own, and `sessions.json` could hold a session of its
    # own. Their folder is an ancestor, so deleting it is covered too.
    remote = Path(config.V2_DATA_DIR).expanduser() / "remote"
    paths |= {remote / "passkeys.json", remote / "sessions.json"}
    # The phone's Web Push (P-6, `v2/push.py`): the VAPID key, the
    # subscriptions (both also credential bundles by name, unreadable) and
    # the notifications switch. An agent that could write them could send
    # pushes as Jarvis, add a subscription of its own or turn them off.
    paths |= {remote / "jarvis_push_key.json", remote / "jarvis_push_subscriptions.json", remote / "push_settings.json"}
    # The owner's quiet hours (`v2/quiet.py`, the owner's answer of
    # 2026-10-28): while they hold nothing is pushed to the phone. An agent
    # that could rewrite the file could silence the owner's notifications.
    paths.add(Path(config.V2_DATA_DIR).expanduser() / "quiet.json")
    # The owner inbox's "denied while you were away" records (inbox IN-3,
    # 2026-10-25), under the v2 data root: what the owner is told was refused
    # while nobody watched. An agent that could forge or clear them could
    # hide that, so they are the owner's state, written by the daemon only.
    # The folder itself is named too, so moving, replacing or re-moding it
    # is judged as a write to it (deleting it was already covered as an
    # ancestor). Since inbox B1 (IN-5) the whole tree is protected too
    # (`_inbox_tree`): the decisions `owner_post` writes through the daemon
    # live in `decisions/`, one file per id, which no fixed list can name.
    # The `decisions` folder is named here so moving or re-moding it is
    # judged as a write to it, as the inbox folder's is.
    inbox = Path(config.V2_DATA_DIR).expanduser() / "inbox"
    # The `checks` folder (inbox B2: the live checks and the owner's
    # results) is named for the same reason, and so is `marks.json` (inbox
    # B4: what the owner snoozed or dismissed; a forged mark would hide a row).
    # `deferred.json` (QH): approvals deferred during quiet hours and the
    # owner's single-use grants; a forged grant would answer for the owner.
    paths |= {inbox, inbox / "missed.jsonl", inbox / "decisions", inbox / "checks", inbox / "marks.json",
              inbox / "deferred.json"}
    # Jarvis's own records (2026-10-25, review of PR #309: `cp /tmp/x
    # <data>/threads/<id>/log.jsonl` was a static ALLOW, and a forged
    # `{"kind":"user","data":{"origin":"owner",…}}` line then showed as the
    # owner's words in the HUD, other chats' `chat_read`, the phone and the
    # Discord catch-up). Each tree's folder is named, as the inbox's is, so
    # moving, replacing or re-moding it is judged as a write to it and a
    # grant above it is refused; every file below is protected by the tree
    # rule (`_record_trees`). `decisions.jsonl` is the gate's own audit log
    # of what was allowed, asked and denied (`permissions.log_decision`).
    data = Path(config.V2_DATA_DIR).expanduser()
    paths |= {data / name for name in RECORD_TREES} | {data / name for name in RECORD_FILES}
    if len(_STATE_PATHS) > 64:
        _STATE_PATHS.clear()
    _STATE_PATHS[key] = frozenset(paths)
    return paths


_STATE_PATHS: dict[tuple, frozenset] = {}


# Jarvis's own records under the v2 data root (`config.V2_DATA_DIR`), every
# one written by the daemon in-process and by nothing else (the inventory is
# in docs/notes/safety-permissions.md, "Protected state: Jarvis's own
# records"). A write into one by an agent is refused — the write tools,
# v2's layer 1 and grants; v1's shell gate asks — exactly as the inbox's
# tree is (inbox B1). Not here, on purpose: `claude/` and `codex/` (the
# session carriers and Codex's private homes, which the provider CLIs and a
# Codex chat's sandboxed commands write themselves; `editor_policy` withholds
# them), `inbox/` (its own tree, `_inbox_tree`) and `remote/` (its two named
# files above).
RECORD_TREES = (
    "threads",       # thread.json, brief.json (the brief a resume reopens), log.jsonl, activity.json, discord.json, subagents.jsonl
    "tasks",         # task.json, journal.jsonl, activity.json, discord.json
    "projects",      # <id>.json: name, root (the folder its chats work in), archive state
    "schedules",     # <id>.json: what runs, where, and when, with nobody watching
    "ledger",        # <day>.jsonl: usage accounting that admission reads
    "diff_reviews",  # <task id>.json: the quick reviews shown with a task's diff
    "discord",       # linker.json (sanctioned channel names), approval-posts.json
    "chat-git",      # pins.json (the origin a chat may push to), the empty git HOME
    "deleting",      # a permanent delete's staging folder and manifest
    "vex",           # VEX build locks and unfinished-build markers
)
RECORD_FILES = ("decisions.jsonl",)


def _resolve_all(paths) -> set[Path]:
    """`{p.resolve() for p in paths}`, for absolute paths, with each folder
    resolved once: most of them share two or three folders, and resolving
    each in full cost a gate call ~1 ms (review of PR #315). The same answer
    as `resolve` — a name that is itself a symlink is resolved in full, and
    a path with `..` is not shortened. One that cannot be resolved (a link
    loop) is kept as written, made absolute, so it stays protected rather
    than dropped (fail closed)."""
    folders: dict[str, str] = {}
    out: set[str] = set()
    for path in paths:
        text = os.fspath(path)
        try:
            if not os.path.isabs(text) or "/.." in text or text.endswith("/."):
                out.add(str(Path(text).resolve()))
                continue
            head, name = os.path.split(text.rstrip("/") or "/")
            parent = folders.get(head)
            if parent is None:
                parent = folders[head] = str(Path(head).resolve())
            here = os.path.join(parent, name) if name else parent
            out.add(str(Path(here).resolve()) if os.path.islink(here) else os.path.normpath(here))
        except (OSError, RuntimeError):
            out.add(os.path.abspath(text))
    return {Path(p) for p in out}


def _record_paths() -> tuple[set[Path], set[Path]]:
    """(every record tree's folder, every record file), resolved
    (`_resolve_all`: never raises, never drops one). Read from config per
    call, so a test that repoints V2_DATA_DIR protects its own."""
    data = Path(config.V2_DATA_DIR).expanduser()
    trees = _resolve_all(data / name for name in RECORD_TREES)
    return trees, _resolve_all(data / name for name in RECORD_FILES)


def _record_trees() -> set[Path]:
    """Every record tree's folder (`RECORD_TREES`), resolved."""
    return _record_paths()[0]


def _owner_trees() -> set[Path]:
    """The folders whose every file is protected (`protected_state.owner_trees`):
    the inbox's and Jarvis's records (`_record_trees`). Never raises and never
    drops one: a folder that cannot be resolved is kept as written (review of
    PR #315: this used to fall back to the inbox alone)."""
    data = Path(config.V2_DATA_DIR).expanduser()
    return _resolve_all([data / "inbox", *(data / name for name in RECORD_TREES)])


def _record_of(resolved: Path) -> Path | None:
    """The record tree `resolved` is or lies in, or the record file it is,
    else None."""
    trees, record_files = _record_paths()
    for tree in trees:
        if resolved == tree or tree in resolved.parents:
            return tree
    return resolved if resolved in record_files else None


def _in_record_tree(resolved: Path) -> bool:
    try:
        return _record_of(resolved) is not None
    except (OSError, RuntimeError):
        return True  # cannot tell: refuse


def record_refusal(name: str) -> str:
    """Why a write into Jarvis's own records is refused: one sentence for the
    write tools, v2's layer 1 and a refused shell line."""
    return (f"{name} is one of Jarvis's own records (its chats, tasks, projects and "
            "schedules), which only the daemon writes: the owner and other chats rely on "
            "them as the true record of what happened. Use Jarvis's tools for what you "
            "wanted to change, or say what and why instead.")


def _inbox_tree() -> Path:
    """The owner inbox's folder (`<v2 data root>/inbox`), resolved: every
    file at or below it is the inbox's own state (inbox B1). The write tools
    refuse anything there, v2's layer 1 and grants refuse it, and
    `protected_state.owner_trees` judges every shell write into it. Read from
    config per call, so a test that repoints V2_DATA_DIR protects its own.
    Raises OSError/RuntimeError when it cannot be resolved; callers refuse."""
    return (Path(config.V2_DATA_DIR).expanduser() / "inbox").resolve()


def _in_inbox_tree(resolved: Path) -> bool:
    try:
        tree = _inbox_tree()
    except (OSError, RuntimeError):
        return True  # cannot tell: refuse
    return resolved == tree or tree in resolved.parents


def _protected_state() -> set[Path]:
    return _resolve_all(_protected_state_paths())


# A repository's own `.git` (hooks, config, the refs and the worktree
# registrations) and the owner's global git configuration are files that git
# *executes or obeys*: a hook is run by the owner's next commit, an ssh command
# setting by their next push, a filter or textconv driver by their next diff.
# The write tools could write any of them (found 2026-10-09 while designing the
# workflow git tool: none is a credential name and none is in SELF_PROTECTED),
# which made "write a file" a way to have the owner's own git run a program of
# the agent's choosing. Git's internals are git's to write; the agent reaches
# them through the `git` tool, which judges every invocation (jarvis/gitops.py).
#
# Keyed on a `.git` path *component* of the resolved path, so a symlink into a
# repository does not launder it, and `.github` / `.gitignore` / `.gitattributes`
# stay ordinary files. The global files are keyed on identity, the way the
# gate's state is, and read per call so a test that repoints HOME protects its
# own.
def _git_internal(resolved: Path) -> bool:
    if ".git" in resolved.parts:
        return True
    try:
        home = Path.home()
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config_home = Path(xdg).expanduser() if xdg else home / ".config"
        exact = {(home / name).resolve()
                 for name in (".gitconfig", ".git-credentials", ".gitattributes", ".gitignore")}
        git_dir = (config_home / "git").resolve()
        return resolved in exact or resolved == git_dir or git_dir in resolved.parents
    except (OSError, RuntimeError):
        return True  # cannot tell: refuse


# The built HUD the v2 daemon serves (`config.HUD_DIST`, BR-0). Its script runs
# with the HUD's own Origin on the HUD listener — the one request
# `POST /approvals` accepts — so writing a file into it is planting a program
# the owner's next reload runs with the power to approve a card. Keyed on the
# resolved directory (a symlink into it does not launder a write), read per
# call so a test can repoint it. Not a widening of SELF_PROTECTED: the HUD's
# *source* stays writable. Since C-1 (ED-4) `config.HUD_DIST` is the install
# root `config.HUD_DIR`, so this one check covers every installed build and a
# build planted beside them; `jarvis hud install` (the owner's or the lead's
# CLI, never a tool) is what writes it.
def _hud_roots() -> set[Path]:
    """Every folder the HUD is or could be served from, resolved: the install
    root `config.HUD_DIR` always — even in a process started with
    JARVIS_HUD_DEV=1, whose tools would otherwise leave it writable (review of
    PR #44, L4) — and `config.HUD_DIST`, which is `hud/dist` under that flag.
    The one definition the write tools, v2's grants and `protected_state`'s
    shell check share. Raises OSError/RuntimeError when a root cannot be
    resolved; callers refuse then."""
    return {Path(config.HUD_DIR).expanduser().resolve(),
            Path(config.HUD_DIST).expanduser().resolve()}


def _hud_build(resolved: Path) -> bool:
    try:
        roots = _hud_roots()
    except (OSError, RuntimeError):
        return True  # cannot tell: refuse
    return any(resolved == root or root in resolved.parents for root in roots)


def _self_protected(target: Path) -> bool:
    resolved = target.resolve()
    if resolved in _protected_state():
        return True
    if _git_internal(resolved):
        return True
    if _hud_build(resolved):
        return True
    if _in_inbox_tree(resolved):
        return True
    if _in_record_tree(resolved):
        return True
    try:
        rel = resolved.relative_to(config.REPO_ROOT)
    except ValueError:
        return False
    return str(rel) in SELF_PROTECTED


def _protect_refusal(target: Path) -> str:
    if _git_internal(target.resolve()):
        return (
            f"Error: {target.name} is git's own configuration or internals, which git executes "
            "or obeys, so it can only be changed by the owner by hand. Use the git tool for "
            "git operations."
        )
    if _hud_build(target.resolve()):
        return (
            f"Error: {target.name} is in the built HUD the daemon serves, whose script "
            "can approve what the owner is asked. Change the HUD's source under "
            "hud/src instead; the owner installs it with `jarvis hud install`."
        )
    if _in_record_tree(target.resolve()):
        return "Error: " + record_refusal(target.name)
    return (
        f"Error: {target.name} is part of Jarvis's safety layer and can "
        "only be changed by the owner by hand. Explain what you wanted "
        "changed and why instead."
    )


def in_working_dir(path: str) -> str:
    """`path`, placed in the run's working folder when it is relative.

    The v2 fast path binds its thread's project folder for each turn
    (`runtime.working_dir()`, decision F-6 of 2026-10-14), so `src/app.py`
    means the project's file, not one under whatever directory the daemon was
    started in. Done first, before any check, so every check below — the
    credential name, the credential folder, the env-file rule, SELF_PROTECTED
    and the gate's state — judges the same absolute path the tool then
    touches, never a relative spelling resolved somewhere else. An absolute or
    `~` path is returned as written, and with nothing bound every path is
    returned as written: the process directory, as before.
    """
    from .. import runtime

    base = runtime.working_dir()
    text = str(path)
    if base is None or os.path.isabs(os.path.expanduser(text)):
        return text
    return os.path.join(base, text)


def _resolve(path: str) -> Path:
    return Path(in_working_dir(path)).expanduser().resolve()


# Credential folders (`config.credential_dirs()`: ~/.ssh, ~/.aws, ~/.config/gh,
# ~/.config/jarvis, ~/.gnupg and the HUD Browser's profile folder) are closed
# to v1's file tools, reads included (review of PR #69: the Browser profile's
# History, Web Data and Sessions were readable by the fast path's read_file
# and grep_files). v2 already refuses them (`permissions._credential_dirs`,
# `editor_policy.credential_dirs`). The one exception keeps an older rule: the
# gate's own state files — the allowlist, models.json … — stay readable
# config, but only by their own path, never through a symlink.
_credential_dir_cache: dict[tuple, list[Path]] = {}


def _credential_roots() -> list[Path]:
    key = (config.credential_dirs(), os.environ.get("HOME", ""))
    found = _credential_dir_cache.get(key)
    if found is None:
        found = []
        for raw in key[0]:
            base = Path(os.path.abspath(os.path.expanduser(raw)))
            found.append(base)
            try:
                real = base.resolve()
            except (OSError, RuntimeError):
                continue
            if real != base:
                found.append(real)
        _credential_dir_cache.clear()
        _credential_dir_cache[key] = found
    return found


def credential_folder_judge():
    """`_credential_folder` for many paths at once — a search: the folders
    and the gate's own paths are read once, each directory's real path once,
    and a path's own real path only when its last component is a symlink
    (otherwise it is its directory's real path plus its name). String
    prefixes, not `Path.parents`: a search judges every file it reads
    (review of PR #69: 20k paths took 3.4 s against 0.31 s before)."""
    roots = tuple(str(r) if str(r).endswith(os.sep) else str(r) + os.sep for r in _credential_roots())
    # Not the phone's folder (`config.remote_dir()`, review of PR #325 MED-1):
    # its state files hold a VAPID private key, push secrets and session
    # hashes, so none of them is readable config.
    remote = os.path.abspath(os.path.expanduser(str(config.remote_dir()))) + os.sep
    own = frozenset(a for a in (os.path.abspath(str(p)) for p in _protected_state_paths())
                    if not a.startswith(remote))
    real_dirs: dict[str, str] = {}

    def inside(p: str) -> bool:
        p = p + os.sep
        return any(p.startswith(r) for r in roots)

    def judge(path: str | Path) -> bool:
        try:
            lexical = os.path.abspath(os.path.expanduser(os.fspath(path)))
            head, tail = os.path.split(lexical)
            if not tail or os.path.islink(lexical):
                resolved = os.path.realpath(lexical)
            else:
                real = real_dirs.get(head)
                if real is None:
                    real = real_dirs[head] = os.path.realpath(head)
                resolved = os.path.join(real, tail)
        except (OSError, RuntimeError, ValueError, TypeError):
            return True
        if resolved == lexical and lexical in own:
            return False
        return inside(lexical) or inside(resolved)

    return judge


def _credential_folder(path: str | Path) -> bool:
    """True if `path`, as written or where it resolves, is a credential
    folder or lies inside one — unless it is one of the gate's state files
    named by its own, un-symlinked path."""
    return credential_folder_judge()(path)


def _credential_folder_refusal(path: str | Path) -> str:
    return (f"Error: {Path(str(path)).name} is in a credential folder (SSH, cloud and Git "
            "logins, Jarvis's own credentials or the owner's Browser profile), which Jarvis "
            "does not read or write. Ask the owner for what you need instead.")


def _strip_numbering(text: str, strict: bool = True) -> str | None:
    """`text` with read_file's line-number prefixes removed, or None.

    Returns None unless *every* non-empty line carries a prefix and the numbers
    run consecutively — anything looser would corrupt ordinary content that
    happens to start with a digit and a tab.

    `strict` additionally demands read_file's own 6-wide right-aligned field.
    write_file uses it (a wrong strip there silently mangles a saved file);
    edit_file does not, because there the strip is only accepted when it
    produces a match that was otherwise going to fail outright.
    """
    lines = text.split("\n")
    matches = [(line, _NUMBERED.match(line)) for line in lines if line.strip()]
    if not matches or not all(m for _, m in matches):
        return None
    if strict and any(len(m.group(1)) < _NUMBER_FIELD for _, m in matches):
        return None
    values = [int(m.group(1)) for _, m in matches]
    if any(b - a != 1 for a, b in zip(values, values[1:])):
        return None
    return "\n".join(_NUMBERED.sub("", line) for line in lines)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


@tool
def read_file(
    path: Annotated[str, "Path to the file, e.g. ~/notes/todo.md"],
    offset: Annotated[int, "First line to read, 1-based. Use with limit to page through a long file."] = 1,
    limit: Annotated[int, "How many lines to read (default 1500)"] = DEFAULT_LINES,
) -> str:
    """Read a UTF-8 text file. Output is line-numbered, `<number>\\ttext`.

    Long files come back one page at a time. If the footer says lines were left
    over, call this again with `offset` set past the last line you got — the
    rest of the file is always reachable that way.

    The line numbers are display only. When passing text to edit_file, use the
    file's own content without the number-and-tab prefix.
    """
    path = in_working_dir(path)
    target = _resolve(path)
    # Checked before .exists() so the refusal is the same whether or not the
    # file is there, and against both spellings so a symlink cannot launder it
    # (`is_credential` also follows every symlink hop of the name: ENV-1).
    if is_credential(path) or is_credential(target):
        return refusal(target.name)
    if _credential_folder(path):
        return _credential_folder_refusal(path)
    if not target.exists():
        return f"Error: {target} does not exist."
    if target.is_dir():
        return f"Error: {target} is a directory. Use list_dir instead."

    text = target.read_text(encoding="utf-8", errors="replace")
    _seen[str(target)] = target.stat().st_mtime
    if not text:
        return "[file is empty]"

    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset or 1))
    if start > total:
        return f"Error: {target} has {total} lines; offset {start} is past the end."
    count = max(1, int(limit or DEFAULT_LINES))
    window = lines[start - 1 : start - 1 + count]

    rendered: list[str] = []
    used = 0
    for i, line in enumerate(window):
        entry = f"{start + i:6d}\t{line}"
        # The character ceiling is a backstop against a file of very long lines;
        # it stops the page early rather than cutting mid-line, so the footer's
        # "resume at" number stays truthful.
        if used + len(entry) > MAX_READ_CHARS and rendered:
            break
        rendered.append(entry)
        used += len(entry) + 1

    last = start + len(rendered) - 1
    body = "\n".join(rendered)
    if start > 1 or last < total:
        body += (
            f"\n\n[lines {start}-{last} of {total}."
            + (
                f" Continue with read_file(path, offset={last + 1})]"
                if last < total
                else " End of file.]"
            )
        )
    return body


@tool
def write_file(
    path: Annotated[str, "Path to write"],
    content: Annotated[str, "Full new contents of the file"],
) -> str:
    """Write a text file whole, creating parent directories as needed.

    An existing file must be read with read_file first. To change part of a
    file that already exists, prefer edit_file — it costs a fraction as much
    and cannot drop the parts you were not changing.
    """
    path = in_working_dir(path)
    target = _resolve(path)
    if _self_protected(target):
        return _protect_refusal(target)
    if is_env_credential(path) or is_env_credential(target):
        # The owner's `.env*` rule (ENV-1): Jarvis writes only `*.example`.
        return write_refusal(Path(path).name)
    if is_credential(path) or is_credential(target):
        return refusal(target.name)
    if _credential_folder(path):
        return _credential_folder_refusal(path)
    if target.exists():
        stamp = _seen.get(str(target))
        current = target.stat().st_mtime
        if stamp is None:
            return f"Error: {target} already exists. Read it first, then write."
        if stamp != current:
            return f"Error: {target} changed on disk since you read it. Read it again."

    # read_file numbers its output, so a whole-file rewrite of something just
    # read is one copy-paste away from baking the line numbers into the file.
    # `_strip_numbering` only fires on a consecutively-numbered block, so this
    # cannot misfire on real content — and the note makes the save visible
    # rather than silent.
    note = ""
    stripped = _strip_numbering(content)
    if stripped is not None and content.strip():
        content = stripped
        note = " (stripped read_file's line numbers from the content)"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _seen[str(target)] = target.stat().st_mtime
    return f"Wrote {len(content)} characters to {target}.{note}"


@tool
def edit_file(
    path: Annotated[str, "Path to the file to change"],
    old_string: Annotated[
        str,
        "The exact text to replace, copied from the file. Include enough "
        "surrounding lines to make it unique. Do not include read_file's "
        "line-number prefix.",
    ],
    new_string: Annotated[str, "What to put in its place. Empty string deletes it."],
    replace_all: Annotated[
        bool, "Replace every occurrence instead of requiring exactly one"
    ] = False,
) -> str:
    """Replace an exact stretch of text in a file, leaving the rest untouched.

    This is the right way to change a file that already exists. `old_string`
    must match the file byte for byte and must appear exactly once, unless
    replace_all is set — if the match is ambiguous, add more surrounding lines
    rather than guessing.

    The file must have been read with read_file first, the same rule write_file
    follows.
    """
    path = in_working_dir(path)
    target = _resolve(path)
    if _self_protected(target):
        return _protect_refusal(target)
    if is_env_credential(path) or is_env_credential(target):
        # The owner's `.env*` rule (ENV-1): Jarvis writes only `*.example`.
        return write_refusal(Path(path).name)
    if is_credential(path) or is_credential(target):
        return refusal(target.name)
    if _credential_folder(path):
        return _credential_folder_refusal(path)
    if not target.exists():
        return f"Error: {target} does not exist. Use write_file to create it."
    if target.is_dir():
        return f"Error: {target} is a directory."
    if not old_string:
        return "Error: old_string is empty. Use write_file to create or replace a whole file."
    if old_string == new_string:
        return "Error: old_string and new_string are identical — nothing to change."

    stamp = _seen.get(str(target))
    current = target.stat().st_mtime
    if stamp is None:
        return f"Error: {target} has not been read this session. Read it first, then edit."
    if stamp != current:
        return f"Error: {target} changed on disk since you read it. Read it again."

    text = target.read_text(encoding="utf-8", errors="replace")

    hits = text.count(old_string)
    salvaged = ""
    if hits == 0:
        # A model quoting an excerpt back from read_file keeps the numbering
        # more often than not. Retry once without it rather than making the
        # display format a trap.
        stripped = _strip_numbering(old_string, strict=False)
        if stripped is not None and text.count(stripped) > 0:
            old_string = stripped
            hits = text.count(old_string)
            salvaged = " (ignored the line numbers you included)"
        else:
            return (
                f"Error: that text does not appear in {target.name}. It must match "
                "byte for byte, including indentation. Read the file again and "
                "copy the exact lines."
            )
    if hits > 1 and not replace_all:
        first = _line_of(text, text.index(old_string))
        return (
            f"Error: that text appears {hits} times in {target.name} (first at line "
            f"{first}). Include more surrounding lines to identify the one you "
            "mean, or pass replace_all=true to change all of them."
        )

    line = _line_of(text, text.index(old_string))
    target.write_text(text.replace(old_string, new_string), encoding="utf-8")
    _seen[str(target)] = target.stat().st_mtime

    where = f"line {line}" if hits == 1 else f"{hits} places, first at line {line}"
    delta = len(new_string) - len(old_string)
    return f"Edited {target} at {where} ({delta:+d} characters).{salvaged}"


@tool
def list_dir(
    path: Annotated[str, "Directory to list"] = ".",
) -> str:
    """List the entries in a directory."""
    target = _resolve(path)
    if not target.is_dir():
        return f"Error: {target} is not a directory."

    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name.startswith("."):
            continue
        entries.append(f"{item.name}/" if item.is_dir() else f"{item.name}  ({item.stat().st_size}B)")
    return "\n".join(entries) or "[empty directory]"


@tool
def find_files(
    pattern: Annotated[str, "Glob pattern, e.g. **/*.py"],
    path: Annotated[str, "Directory to search from"] = ".",
) -> str:
    """Find files matching a glob pattern."""
    root = _resolve(path)
    if not root.is_dir():
        return f"Error: {root} is not a directory."

    hits = []
    for match in root.glob(pattern):
        if any(part.startswith(".") for part in match.parts):
            continue
        hits.append(os.path.relpath(match, root))
        if len(hits) >= 200:
            hits.append("[...more matches truncated]")
            break
    return "\n".join(hits) or f"No files matching {pattern!r} under {root}."
