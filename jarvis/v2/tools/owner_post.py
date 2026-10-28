"""`owner_post`: put a decision or a live check in front of the owner (owner
inbox design `docs/plans/2026-10-24-owner-inbox-design.md`, IN-5, IN-6;
packages B1 and B2).

One bounded tool, two kinds: "decision" (B1) and "check" (B2). Every argument
is optional in the schema because each kind has its own: a decision takes
`question`, `options`, `why` and `default`, a check `title`, `steps` and
`prs`, and the service refuses a kind given the other's (or missing its
own). It reaches the daemon's post service (`jarvis/v2/decisions.py`,
`installed()`) and nothing else: it cannot read the owner's list, answer,
dismiss, approve or mark anything (a check's results are the owner's alone,
IN-6), and its module names none of the owner's routes
(`tests/v2/archive_check.py`, `tests/v2/owner_post_check.py`,
`tests/v2/live_checks_check.py`).

Bound as `chat_send` is (chat-to-chat CC-3, CC-6, CC-7 and P-6), checked
twice: here first (`runtime.at_desk()` and `runtime.depth() == 0`), then by
the service under the daemon lock against the live session and the exact
turn, which also requires a Claude or Codex chat.

* Claude and Codex owner chats are offered it through the session catalog
  (`session_tools.py`): foreground-only (a Claude sub-agent is denied by the
  hook, a Codex child by `admit_foreground_mcp`) and desk-only. Not
  read-only, so `permit` decides (`jarvis-session-delegate`: allowed unasked
  on AUTO and ASK, refused for STRICT, asked under an always-ask rule).
* The fast path never holds it (not in `fastpath.FAST_TOOLS`, `DESK_TOOLS`,
  `EDIT_TOOLS` or `CHAT_TOOLS`), and the service refuses a fast thread even
  if it did. `EXPLICIT_ONLY`, so no v1 surface holds it; never in
  `mcp_catalog.MCP_TOOLS`, so the external `jarvis mcp` does not list it.
* Not `PARALLEL_SAFE` (the caps are counted per call) and not dangerous: it
  asks nobody, because what it does is bounded, scrubbed, untrusted text the
  owner reads when they choose.

SELF_PROTECTED: it is the one way an agent writes to the owner's list.
"""
from __future__ import annotations

from typing import Annotated

from jarvis import runtime
from jarvis.tools import EXPLICIT_ONLY, tool

from ..decisions import NOT_AT_DESK, NOT_FROM_A_SUB_AGENT, NOT_IN_CHAT, installed


@tool
def owner_post(
    kind: Annotated[str, 'What to post: "decision" (a question the owner answers) or "check" (steps '
                         'only the owner can run and mark)'],
    question: Annotated[str | None, 'decision: the question, complete in itself: the owner reads it '
                                    'away from this chat (at most 500 characters)'] = None,
    options: Annotated[list[str] | None, "decision: 2 to 6 short answers the owner can pick, each at "
                                         "most 120 characters; text only, never commands"] = None,
    why: Annotated[str | None, "decision: why it matters: what each answer changes, and what you do "
                               "meanwhile (at most 1000 characters)"] = None,
    default: Annotated[int | None, "decision: the index (from 0) of the option you would take if "
                                   "asked to choose; leave it out for none"] = None,
    title: Annotated[str | None, "check: what it checks, in one line (at most 200 characters)"] = None,
    steps: Annotated[list[dict] | None, 'check: 1 to 30 steps, in order, each {"do": what the owner '
                                        'does, "expect": what they should see}, each at most 1000 '
                                        'characters; text only, nothing here is run'] = None,
    prs: Annotated[list[int] | None, "check: the numbers of the pull requests it checks (at most "
                                     "10); the owner can mark it only once they are deployed"] = None,
) -> str:
    """Put something in front of the owner, to handle when they have time.

    kind "decision": a question; it goes on their inbox with your options,
    and their pick (or their own words) comes back to this chat later as a
    message from the owner, as its own turn. Use it for a choice only the
    owner can make that need not stop this turn, never for permission to run
    something (that is the approval card's) and never to wait on. Give
    question, options, why and optionally default.

    kind "check": a live check, steps only the owner can run (on their PC,
    in the installed HUD), each with what to do and what to expect. Give
    title, steps and optionally prs. The owner marks each step pass, fail or
    skip; the result is theirs alone and is not sent back to you.

    Only during a turn the owner started at the HUD in this chat. At most 5
    open of each kind per chat and 20 a day; nothing over a length cap is
    cut, it is refused."""
    if not runtime.at_desk():
        return NOT_AT_DESK
    if runtime.depth() != 0:
        return NOT_FROM_A_SUB_AGENT
    who = runtime.caller()
    service = installed()
    if who is None or service is None:
        return NOT_IN_CHAT
    return service.post(who, kind, question, options, default, why, title=title, steps=steps, prs=prs)


EXPLICIT_ONLY.add("owner_post")
