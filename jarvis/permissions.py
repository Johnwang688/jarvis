"""Permission modes and the persistent allowlist for dangerous tools.

gate() wraps a surface's approver (the CLI y/N prompt, the HUD card) and
checks, in order:

  1. mode "all"   — approve everything. Reachable ONLY via
                    `jarvis face --dangerously-skip-permissions`; the mode is
                    a process variable, never persisted, so a restart is
                    always back to asking.
  2. the allowlist — persistent, owner-curated: each entry exists because the
                    owner explicitly chose "always allow" on a specific
                    request ('a' at the CLI prompt, ALWAYS in the HUD card).
                    Matching entries skip the ask.
  3. mode "ask"    — the default: hand the decision to the surface approver.

Entry shapes in ~/.config/jarvis/allowlist.json:
  {"tool": "gmail_send"}                     the whole tool
  {"tool": "run_command", "prefix": "git"}   commands whose first word is git

The command prefix is the first word only, and it matches whole tokens —
"git" allows "git push" but not "gitfoo". Broader patterns are the owner's
call, by editing the JSON by hand.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from . import command_review, config, rules

_mode = "ask"
_lock = threading.Lock()


def mode() -> str:
    return _mode


def set_mode(new_mode: str) -> None:
    global _mode
    if new_mode not in ("ask", "all"):
        raise ValueError(f"unknown permissions mode {new_mode!r}")
    _mode = new_mode


def load_allowlist() -> list[dict[str, Any]]:
    try:
        entries = json.loads(config.ALLOWLIST_PATH.read_text(encoding="utf-8"))
        return entries if isinstance(entries, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_allowlist(entries: list[dict[str, Any]]) -> None:
    config.ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.ALLOWLIST_PATH.write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )


def entry_for(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The allowlist entry an 'always allow' on this request should create.

    Commands allowlist by first word (approving `git status` should not
    silence `rm`); everything else allowlists the whole tool.
    """
    command = str(args.get("command", "")).strip()
    if tool_name in ("run_command", "run_readonly") and command:
        return {"tool": tool_name, "prefix": command.split()[0]}
    return {"tool": tool_name}


def add_allow(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Persist an entry for this request. Returns the entry (new or existing)."""
    entry = entry_for(tool_name, args)
    with _lock:
        entries = load_allowlist()
        if entry not in entries:
            entries.append(entry)
            _save_allowlist(entries)
    return entry


def allows(tool_name: str, args: dict[str, Any]) -> bool:
    """True if a persistent allowlist entry covers this exact request."""
    command = str(args.get("command", ""))
    first_word = command.split()[0] if command.split() else ""
    for entry in load_allowlist():
        if entry.get("tool") != tool_name:
            continue
        prefix = entry.get("prefix")
        if prefix is None:
            return True
        if first_word == prefix:
            return True
    return False


def command_verdict(tool_name: str, args: dict[str, Any]) -> rules.Verdict:
    """deny / allow / ask for one dangerous-tool request.

    Only `run_command` has a command line to reason about; every other
    dangerous tool keeps the old behaviour and asks. Evaluated once, by
    `dispatch()`, because the fetch-execute review costs a network round trip
    and a model call — doing it again inside the approver would double both.
    """
    if tool_name != "run_command":
        return rules.Verdict(rules.ASK)

    command = str(args.get("command", ""))
    verdict = rules.decide(command)
    if verdict.decision == rules.DENY:
        return verdict

    # A pipe-to-shell is the one case where the static rules genuinely cannot
    # answer: everything that matters is in a file that has not been fetched
    # yet. So fetch it and look. See command_review for why a clean verdict
    # alone is not enough to auto-approve.
    if command_review.is_fetch_execute(command):
        decision, reason = command_review.verdict_for(command)
        return rules.Verdict(decision, reason)
    return verdict


def gate(inner: Callable[[Any, dict], bool]) -> Callable[[Any, dict], bool]:
    """Wrap a surface approver with the mode and allowlist checks.

    The result is what every Agent gets as `approve=` — never None (see the
    dispatch()-runs-unguarded invariant).
    """

    def approve(tool: Any, args: dict) -> bool:
        if _mode == "all":
            return True
        if allows(getattr(tool, "name", str(tool)), args):
            return True
        return inner(tool, args)

    # Marks this approver as one with a person behind it — a CLI prompt, a HUD
    # card, a DM. `dispatch()` will only honour an ALLOW verdict from rules.py
    # for an approver carrying this flag.
    #
    # It exists because of what a bare approver means. workflows.py hands its
    # agents a deny-all lambda precisely *because* nobody is watching a
    # background thread, and an auto-approve that skipped the approver entirely
    # would have quietly turned "workflows cannot run dangerous tools" into
    # "workflows can run any allowlisted dangerous tool". Auto-approval is a
    # convenience for a surface where the owner is present and would have said
    # yes; it is not a property of the command on its own.
    approve.jarvis_human_backed = True
    return approve
