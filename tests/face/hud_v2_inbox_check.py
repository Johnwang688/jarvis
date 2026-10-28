"""Inbox A2 (docs/plans/2026-10-24-owner-inbox-design.md, IN-1, §7 and §8): the
HUD's owner inbox — the sidebar's Inbox row and count, the app badge, and the
Inbox view — against inbox A1's routes (`GET /inbox`, `POST /inbox/<id>/answer`,
the `inbox_changed` bus record), played by `hud_v2_mock_inbox.py`.

Free and hermetic: its own `MockDaemon(0)` (an ephemeral port, never
8402/8403/8405; `guard` refuses the live ports and watches the CSP).

What it pins, one section each (each names the mutation it is meant to catch):
  sidebar   the Inbox row sits above the projects, counts ranks 1 to 5 only,
            reads amber while anything pending (rank 1 to 2) waits, and opens
            the view; the title stays `J.A.R.V.I.S.` [count all ranks; no row]
  groups    group headers and row order follow the rank, empty groups are
            hidden; amber marks ranks 1 to 2, red a failed task [order by time]
  live      `inbox_changed` (`ids: []` too) re-reads the inbox: the view and
            the count follow [no re-read on the event]
  answer    a question's answer posts `{text}` once to `/inbox/<id>/answer`
            with the id through `encodeURIComponent` (A1 decodes once); a
            proposal's Withdraw posts `{"withdraw": true}` [double post; raw id]
  conflict  a 409 shows "Already answered or gone." and re-reads [no re-read]
  withdraw  `withdraw_until` is the daemon's clock, so the window's clock is a
            hint only (review of PR #305, MED 1): Withdraw stays while the row
            is in the projection, turning "closing" near the end and never
            claiming the window closed; with the daemon 90 s behind it is
            still offered and the press posts; with the daemon 90 s ahead a
            press after the daemon dropped the row is its 409, said, re-read
            [gate on the browser's clock]
  reconnect a dropped event stream, rows changed with no `inbox_changed`, and
            the reconnect alone re-reads them [no re-read on `_connected`]
  clicks    a click anywhere on a row never posts anything; a row's link opens
            its thread or its task [row onClick answers]
  hostile   `<img onerror>` in `origin`, in question text and in a brief makes
            no element and runs nothing [innerHTML / Markdown]
  missed    inbox A3's "denied while you were away" rows sit under "For your
            information", uncounted, say why in the HUD's own words, offer
            only "Open its chat" (none without a chat; no "ask again"), show
            a hostile origin as text, and their link opens the chat
            [count them; an ask-again button; innerHTML]
  decision  inbox B1's decision rows (`POST /inbox/decisions/<id>`) sit under
            "When you have time", uncounted, plain Graphite; the options are
            radios with the agent's suggestion preselected and nothing posted;
            hostile question, options, why and origin stay text; Enter on a
            focused radio posts nothing (the arrows still move the pick);
            Answer posts `{choice}` once; words in the focused box post
            `{text}` on Enter; under a card the row is inert, Answer disabled,
            and a trusted click, typing and Enter post nothing [auto-submit;
            submit on Enter; double post; innerHTML; not inert]
  deferred  QH's deferred approvals sit under "Waiting on you" at rank 4,
            counted, never markable; the card's command is text; Approve
            and retry shows only where it may (never a Browser card or a
            day-old row) and posts `{action: "retry"}` once; Leave it posts
            `{action: "leave"}`; nothing is focused by default; under a card
            the row is inert and a trusted click posts nothing [double post;
            innerHTML; markable; autofocus; not inert]
  check     inbox B2's live checks (`POST /inbox/checks/<id>/steps/<n>`) sit
            under "When you have time", uncounted, plain Graphite; steps are
            text (hostile title, steps and origin make no element); Copy
            writes the step's words to the clipboard and posts nothing; a
            verdict posts `{verdict, note}` once; not confirmed deployed, the
            daemon's line shows and the marks are disabled; done, the row
            moves under "For your information" with its verdicts and the
            receipt copies as Markdown; the import reads drafts and saves
            only the one the owner saves; under a card the row is inert and a
            trusted click posts nothing [auto-mark; double post; innerHTML;
            mark while undeployed; not inert]
  marks     inbox B4's Snooze 1 h, Until tomorrow, 1 week and Dismiss show on
            rows of rank 5 to 8 only (never on a question, a proposal, a
            blocked task or "PR list unavailable"); the HUD reads
            `/inbox?all=1`; Dismiss posts `{dismiss: true}` once with the id
            through `encodeURIComponent`; the row leaves the list and a
            hidden rank-5 row leaves the count; "Show hidden (n)" shows it
            muted with "Dismissed" and Undo, which posts `{clear: true}` and
            brings it back; a snooze posts its time about an hour ahead; a
            click on a row marks nothing; under a card the marks are inert
            and a trusted click posts nothing [marks on rank 1 to 4; count
            hidden rows; double post; row onClick marks; not inert]
  card      the approval card with the Inbox open behaves exactly as it does
            with a chat pane (the same assertions, the same answers): one card,
            the veil's, never a second instance in the inbox; Enter does
            nothing; Tab stays on DENY; Escape denies; AUTHORIZE allows; ALWAYS
            allowlists; and the inbox is inert under it — a trusted click on
            its controls posts nothing and focus cannot enter it. A private
            (HUD-only) card fetches its private detail once, from the veil's
            card only, and its row shows none of it [second card; not inert]
  badge     `navigator.setAppBadge` (stubbed) gets the count and is cleared at
            0 and when a read fails; without the API nothing breaks [badge all
            ranks; no clear; a stale badge after a failed read]
  prs       inbox B3's PR rows sit under "Waiting on you" between a blocked
            task and a decision, counted; the PR's title is text; the one
            control is "Open on GitHub", a plain new-tab link to the PR's own
            URL (none for another repo's); a click on the row posts nothing;
            "PR list unavailable" is counted and never reads as "nothing
            waiting" [a link to any URL; innerHTML; empty as nothing waiting]
  older     an older daemon (404) shows no count and says the inbox is not
            available, never "nothing waiting" [404 read as empty]
  aurora    the owner's aurora (2026-10-25) is on the owner Inbox row only,
            never the "Inbox" project's (the mock's `inbox: true` project,
            named Inbox for the section); a still tint with nothing waiting
            (no animation at all), drifting with sparks while something
            waits, its keyframes only transform/opacity, dimmed and spark-free
            while the amber count is pending, which stays amber on an opaque
            chip; it moves no layout; it pauses under the approval card and
            resumes after; prefers-reduced-motion leaves a static tint with
            no animation [no reduced-motion rule; no veil pause; the aurora
            on the project row]. `JARVIS_AURORA_SHOTS=<dir>` also saves the
            sidebar, calm, waiting and pending, as PNGs there.

Run alone (after `cd hud && npm ci --ignore-scripts && npm run build`):
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env PYTHONPATH=$PWD \\
    PYTHONDONTWRITEBYTECODE=1 /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_inbox_check.py [section,...]
It also runs as a section of tests/face/hud_v2_check.py.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face import hud_v2_mock_inbox as mock_inbox  # noqa: E402
from tests.face.hud_v2_layout_check import _pane  # noqa: E402
from tests.face.hud_v2_multichat_check import _fresh  # noqa: E402

SINGLE = json.dumps({"preset": "single", "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})
TITLE = "J.A.R.V.I.S."
HOSTILE = '<img src=x onerror="window.__pwned=(window.__pwned||0)+1">'
AMBER, RED = "rgb(251, 191, 36)", "rgb(248, 113, 113)"
BADGE_STUB = """
window.__badges = [];
try {
  Object.defineProperty(Navigator.prototype, 'setAppBadge', { configurable: true, writable: true,
    value: function (n) { window.__badges.push(['set', n]); return Promise.resolve(); } });
  Object.defineProperty(Navigator.prototype, 'clearAppBadge', { configurable: true, writable: true,
    value: function () { window.__badges.push(['clear']); return Promise.resolve(); } });
} catch (e) {}
"""
NO_BADGE_API = """
try { delete Navigator.prototype.setAppBadge; delete Navigator.prototype.clearAppBadge; } catch (e) {}
"""


def _iso(seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def q_row(text="Which file?", rid="r9", thread="t1", origin=""):
    return {"id": f"q:{thread}:{rid}", "kind": "question", "rank": 2,
            "title": f"chat {thread} is stopped until you answer", "blocks": f"chat {thread} waits on this",
            "project_id": "p1", "thread_id": thread, "task_id": None, "asked_at": _iso(-60), "expires_at": None,
            "origin": origin, "detail": {"thread_id": thread, "req_id": rid, "text": text,
                                         "options": ["calc.py", "app.py"]}}


def prop_row(task="k9", until_s=600.0, brief="tidy the README"):
    return {"id": f"prop:{task}", "kind": "proposal", "rank": 3,
            "title": f"task {task} starts on its own unless you withdraw it",
            "blocks": "nothing waits yet; it starts when its window closes", "project_id": "p1",
            "thread_id": None, "task_id": task, "asked_at": _iso(-10), "expires_at": None, "origin": "",
            "detail": {"task_id": task, "brief": brief, "withdraw_until": _iso(until_s)}}


def task_row(task="k1", state="blocked", brief="add multiply to calc.py"):
    blocked = state == "blocked"
    return {"id": f"task:{task}", "kind": "task", "rank": 4 if blocked else 8,
            "title": f"task {task} is blocked" if blocked else f"task {task} failed",
            "blocks": f"task {task} waits for you to resume or cancel it" if blocked else "for your information",
            "project_id": "p1", "thread_id": None, "task_id": task, "asked_at": _iso(-300), "expires_at": None,
            "origin": "", "detail": {"task_id": task, "state": state, "brief": brief}}


def card(req: str, **over) -> dict:
    """An ordinary card as the broker publishes it (`ApprovalRequest.to_json()`)."""
    return {"req_id": req, "code": req[-4:].upper(), "tool": "run_command", "args": {"command": "ls -la"},
            "command": "ls -la", "reason": "reviewer declined", "layer": "human", "thread_id": "t1",
            "task_id": None, "provider": "codex", "origin": "Codex · chat ‹jarvis› (t1)", "allowlistable": True,
            "timeout_s": 120, "asked_at": _iso(), **over}


def appr_row(c: dict) -> dict:
    private = bool(c.get("hud_only"))
    return {"id": f"appr:{c['req_id']}", "kind": "approval", "rank": 1,
            "title": (f"{c['tool']} waits for your approval in the HUD" if private
                      else f"Codex asks to use {c['tool']}"),
            "blocks": "chat t1 waits on this", "project_id": None if private else "p1", "thread_id": "t1",
            "task_id": None, "asked_at": c["asked_at"], "expires_at": _iso(120),
            "origin": "" if private else c.get("origin", ""), "detail": c}


def missed_row(req="m1", thread="t1", task=None, resolution="timeout", origin="", tool="run_command"):
    """A "denied while you were away" row as inbox A3's projection serves it."""
    who = f"task {task}" if task else f"chat {thread}"
    why = "nobody answered in time" if resolution == "timeout" else "Jarvis stopped before you answered"
    return {"id": f"missed:{req}", "kind": "missed", "rank": 8,
            "title": f"Denied while you were away: {tool}", "blocks": f"{who} was denied it: {why}",
            "project_id": None, "thread_id": thread, "task_id": task, "asked_at": _iso(-600), "expires_at": None,
            "origin": origin, "detail": {"req_id": req, "tool": tool, "origin": origin, "thread_id": thread,
                                         "task_id": task, "at": _iso(-600), "resolution": resolution}}


def basic_rows():
    return [q_row(), prop_row(), task_row(), task_row("kf", "failed", "a failed brief")]


# -- helpers -------------------------------------------------------------------

def _st(mock):
    return mock_inbox.state(mock)


def _reset(mock, rows=None):
    s = _st(mock)
    s.update({"rows": list(rows if rows is not None else basic_rows()), "fail": None, "answer_status": {}})
    mock.world["approvals"] = []


def _answers(mock):
    return list(_st(mock)["answers"])


def _view(page):
    return page.locator('[data-testid="inbox-view"]')


def _row(page, row_id):
    return page.locator(f'[data-testid="inbox-row"][data-id="{row_id}"]')


def _row_ids(page):
    return page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-row\"]')].map(r => r.dataset.id)")


def _count(page):
    loc = page.locator('[data-testid="owner-inbox-count"]')
    return loc.inner_text().strip() if loc.count() else None


def _open(page, mock, until, rows=None, ws=SINGLE):
    _reset(mock, rows)
    _fresh(page, mock, until, ws)
    if ws == SINGLE:
        page.locator('[data-testid="owner-inbox"]').click()
    until(lambda: _view(page).count() == 1 and _view(page).get_attribute("data-status") == "ready", timeout=4)


def _reads(mock):
    return _st(mock)["reads"]


def _approval_posts(mock, req):
    return mock.sent("POST", f"/approvals/{req}")


def _active(page):
    return page.evaluate("() => document.activeElement && (document.activeElement.getAttribute('data-testid') "
                         "|| document.activeElement.tagName)")


# -- sections ------------------------------------------------------------------

def _sidebar_checks(page, mock, check, until):
    _reset(mock)
    _fresh(page, mock, until, SINGLE)
    row = page.locator('[data-testid="owner-inbox"]')
    until(lambda: _count(page) is not None, timeout=4)
    check("the sidebar has an Inbox row, a real button labelled Inbox",
          row.count() == 1 and row.evaluate("e => e.tagName") == "BUTTON" and "Inbox" in row.inner_text())
    above = page.evaluate("""() => {
      const r = document.querySelector('[data-testid="owner-inbox"]');
      const p = document.querySelector('[data-testid="project-p1"]');
      const h = [...document.querySelectorAll('#sidebar h2.bar')].find(e => /projects/i.test(e.textContent));
      return !!(r && p && h && (r.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING)
        && (r.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_FOLLOWING));
    }""")
    check("it sits above the projects (and their header)", above)
    check("its count is ranks 1 to 5 only: 3 of the 4 rows (the failed task is not counted) "
          "[mutation: count all ranks]", _count(page) == "3", str(_count(page)))
    badge = page.locator('[data-testid="owner-inbox-count"]')
    check("with a rank-2 question waiting the count is amber (pending)",
          badge.get_attribute("data-pending") == "true" and badge.evaluate("e => getComputedStyle(e).color") == AMBER,
          badge.evaluate("e => getComputedStyle(e).color"))
    check("the title is J.A.R.V.I.S. with the inbox counted", page.title() == TITLE, page.title())
    posts = len(_answers(mock))
    row.click()
    until(lambda: _view(page).count() == 1, timeout=3)
    check("a click on it opens the Inbox view in the focused pane, and answers nothing",
          page.locator(_pane(1)).get_attribute("data-view") == "inbox" and len(_answers(mock)) == posts)
    check("the row is marked while a pane shows the inbox", "sel" in (row.get_attribute("class") or ""))
    check("the pane's strip offers the inbox only while it shows it",
          page.locator(_pane(1, '[data-testid="tab-inbox"]')).count() == 1
          and page.locator(_pane(2, '[data-testid="tab-inbox"]')).count() == 0)
    check("the title is unchanged with the view open", page.title() == TITLE, page.title())
    _reset(mock, [prop_row(), task_row()])
    mock_inbox.changed(mock)
    until(lambda: _count(page) == "2", timeout=3)
    check("with nothing pending the count is not amber",
          badge.get_attribute("data-pending") == "false" and badge.evaluate("e => getComputedStyle(e).color") != AMBER)


def _groups_checks(page, mock, check, until):
    rows = basic_rows() + [task_row("kb2", "blocked", "second blocked")]
    _open(page, mock, until, rows)
    heads = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-group-head\"]')].map(e => e.textContent)")
    check("group headers follow the rank: Waiting now, Waiting on you, For your information "
          "(When you have time is empty, so hidden)",
          heads == ["Waiting now", "Waiting on you", "For your information"], str(heads))
    ids = _row_ids(page)
    check("rows are in rank order, oldest first within a rank as served [mutation: order by time]",
          ids == ["q:t1:r9", "prop:k9", "task:k1", "task:kb2", "task:kf"], str(ids))
    border = lambda i: _row(page, i).evaluate("e => getComputedStyle(e).borderLeftColor")  # noqa: E731
    check("a rank 1-2 row is marked amber, a failed task red, the rest neither",
          border("q:t1:r9") == AMBER and border("task:kf") == RED
          and border("prop:k9") not in (AMBER, RED) and border("task:k1") not in (AMBER, RED),
          f"{border('q:t1:r9')} {border('task:kf')} {border('prop:k9')}")
    accent = "rgb(111, 195, 223)"
    check("glacier cyan marks no row (it is for selection and focus only)",
          all(border(i) != accent for i in ids))
    check("a blocked or failed task offers nothing to press but its link",
          _row(page, "task:k1").locator("button").count() == 1
          and _row(page, "task:k1").locator('[data-testid="inbox-link"]').count() == 1)


def _live_checks(page, mock, check, until):
    _open(page, mock, until)
    before = _reads(mock)
    _st(mock)["rows"] = [r for r in _st(mock)["rows"] if r["id"] != "prop:k9"]
    mock_inbox.changed(mock, [])
    until(lambda: "prop:k9" not in _row_ids(page), timeout=3)
    check("an inbox_changed with ids [] re-reads: the gone row leaves the view and the count",
          "prop:k9" not in _row_ids(page) and _count(page) == "2" and _reads(mock) > before, str(_row_ids(page)))
    _st(mock)["rows"].append(prop_row("k7"))
    mock_inbox.changed(mock, ["prop:k7"], "proposal_reply")
    until(lambda: "prop:k7" in _row_ids(page), timeout=3)
    check("one naming a row re-reads too: the new row appears in its place", _row_ids(page)[:2] == ["q:t1:r9", "prop:k7"]
          and _count(page) == "3", str(_row_ids(page)))


def _answer_checks(page, mock, check, until):
    _open(page, mock, until)
    start = len(_answers(mock))
    box = _row(page, "q:t1:r9").locator('[data-testid="inbox-answer"]')
    send = _row(page, "q:t1:r9").locator('[data-testid="inbox-answer-send"]')
    send.click()
    time.sleep(0.3)
    check("Answer with an empty box posts nothing", len(_answers(mock)) == start)
    box.fill("calc.py")
    send.dblclick()
    until(lambda: len(_answers(mock)) > start, timeout=3)
    time.sleep(0.4)
    got = _answers(mock)[start:]
    check("Answer posts the text once, even on a double click [mutation: double post]", len(got) == 1, str(got))
    if got:
        raw, decoded, body = got[0]
        check("to /inbox/<id>/answer with the id through encodeURIComponent, decoded once to the row id",
              raw == "/inbox/q%3At1%3Ar9/answer" and decoded == "q:t1:r9", raw)
        check("with exactly {text}", body == {"text": "calc.py"}, str(body))
    until(lambda: "q:t1:r9" not in _row_ids(page), timeout=3)
    check("the answered row leaves on the daemon's inbox_changed", "q:t1:r9" not in _row_ids(page))
    start = len(_answers(mock))
    _row(page, "prop:k9").locator('[data-testid="inbox-withdraw"]').click()
    until(lambda: len(_answers(mock)) > start, timeout=3)
    got = _answers(mock)[start:]
    check("a proposal's Withdraw posts {\"withdraw\": true} to its own row",
          len(got) == 1 and got[0][1] == "prop:k9" and got[0][2] == {"withdraw": True}, str(got))
    # Enter in the box answers too, as the task view's box does.
    _reset(mock)
    mock_inbox.changed(mock)
    until(lambda: "q:t1:r9" in _row_ids(page), timeout=3)
    start = len(_answers(mock))
    box.fill("app.py")
    box.press("Enter")
    until(lambda: len(_answers(mock)) > start, timeout=3)
    check("Enter in the answer box answers it", _answers(mock)[start:][:1] and _answers(mock)[start][2] == {"text": "app.py"},
          str(_answers(mock)[start:]))


def _conflict_checks(page, mock, check, until):
    _open(page, mock, until)
    _st(mock)["answer_status"]["q:t1:r9"] = (409, {"error": mock_inbox.GONE})
    reads = _reads(mock)
    _row(page, "q:t1:r9").locator('[data-testid="inbox-answer"]').fill("calc.py")
    _row(page, "q:t1:r9").locator('[data-testid="inbox-answer-send"]').click()
    notice = page.locator('[data-testid="inbox-notice"]')
    until(lambda: notice.count() == 1, timeout=3)
    check("a 409 says 'Already answered or gone.'", notice.count() == 1 and notice.inner_text() == "Already answered or gone.",
          notice.inner_text() if notice.count() else "no notice")
    until(lambda: _reads(mock) > reads, timeout=3)
    check("and reads the inbox again [mutation: no re-read on 409]", _reads(mock) > reads)
    # A row gone between the read and the press is the daemon's own 409.
    _st(mock)["answer_status"] = {}
    _st(mock)["rows"] = [r for r in _st(mock)["rows"] if r["id"] != "prop:k9"]
    reads = _reads(mock)
    _row(page, "prop:k9").locator('[data-testid="inbox-withdraw"]').click()
    until(lambda: _reads(mock) > reads, timeout=3)
    until(lambda: "prop:k9" not in _row_ids(page), timeout=3)
    check("a row gone at the daemon answers 409, is said, and leaves on the re-read",
          notice.count() == 1 and "prop:k9" not in _row_ids(page))


CLOSING_S = 15  # lib/inbox CLOSING_MS


def _withdraw_checks(page, mock, check, until):
    # Same clocks: the hint turns "closing" CLOSING_S before the end, and the
    # button stays past withdraw_until for as long as the daemon holds the row.
    rows = [prop_row("k5", until_s=CLOSING_S + 2.5)]
    _open(page, mock, until, rows)
    w = _row(page, "prop:k5").locator('[data-testid="inbox-withdraw"]')
    hint = _row(page, "prop:k5").locator('[data-testid="inbox-withdraw-hint"]')
    check("inside its window the proposal offers Withdraw, hinted open", w.count() == 1
          and w.get_attribute("data-hint") == "open" and "Starts on its own" in hint.inner_text())
    posts = len(_answers(mock))
    flipped = until(lambda: w.count() == 1 and w.get_attribute("data-hint") == "closing", timeout=6)
    check("near its end by this window's clock the hint turns 'closing', with no re-read needed",
          bool(flipped) and hint.get_attribute("data-hint") == "closing")
    time.sleep(3)
    check("past withdraw_until by this window's clock, Withdraw stays while the daemon holds the row "
          "[mutation: gate on the browser's clock]", w.count() == 1 and w.is_enabled())
    check("and nothing claims the window has closed",
          "closed" not in _row(page, "prop:k5").inner_text().lower(), _row(page, "prop:k5").inner_text())
    check("and nothing was posted by the hint", len(_answers(mock)) == posts)

    # The daemon 90 s behind this window: its window is open for 30 s more by
    # its own clock, which this window reads as 60 s ago.
    _open(page, mock, until, [prop_row("kb", until_s=-60)])
    w = _row(page, "prop:kb").locator('[data-testid="inbox-withdraw"]')
    check("skew, daemon 90 s behind: Withdraw is still offered (hinted closing), never hidden",
          w.count() == 1 and w.is_enabled() and w.get_attribute("data-hint") == "closing"
          and "closed" not in _row(page, "prop:kb").inner_text().lower())
    start = len(_answers(mock))
    w.click()
    until(lambda: len(_answers(mock)) > start, timeout=3)
    got = _answers(mock)[start:]
    check("and the press posts {\"withdraw\": true}, which the daemon accepts", len(got) == 1
          and got[0][1] == "prop:kb" and got[0][2] == {"withdraw": True}, str(got))
    until(lambda: "prop:kb" not in _row_ids(page), timeout=3)
    check("the withdrawn row leaves on the daemon's inbox_changed, with no 409 said",
          "prop:kb" not in _row_ids(page) and page.locator('[data-testid="inbox-notice"]').count() == 0)

    # The daemon 90 s ahead: this window reads its window as open for two
    # minutes, but the daemon has closed it and dropped the row (no event yet).
    _open(page, mock, until, [prop_row("ka", until_s=120)])
    w = _row(page, "prop:ka").locator('[data-testid="inbox-withdraw"]')
    check("skew, daemon 90 s ahead: Withdraw is offered, hinted open", w.count() == 1
          and w.get_attribute("data-hint") == "open")
    _st(mock)["rows"] = []
    reads = _reads(mock)
    w.click()
    notice = page.locator('[data-testid="inbox-notice"]')
    until(lambda: notice.count() == 1, timeout=3)
    until(lambda: "prop:ka" not in _row_ids(page), timeout=3)
    check("a press after the daemon closed the window is its 409: 'Already answered or gone.', then a re-read",
          notice.count() == 1 and notice.inner_text() == "Already answered or gone." and _reads(mock) > reads
          and "prop:ka" not in _row_ids(page))


def _reconnect_checks(page, mock, check, until):
    from tests.face.hud_v2_subagents_check import drop_events
    _open(page, mock, until)
    check("setup: the inbox shows its four rows", len(_row_ids(page)) == 4 and _count(page) == "3")
    # The daemon's projection changes while the stream is down: no
    # inbox_changed reaches this window.
    time.sleep(0.5)
    _st(mock)["rows"] = [q_row(), prop_row("k8")]
    reads = _reads(mock)
    time.sleep(0.5)
    check("with no event the window has not re-read", _reads(mock) == reads and "task:k1" in _row_ids(page))
    before = mock.sse_connections()
    drop_events(mock)
    mock.await_reconnect(before, timeout=8)
    until(lambda: _row_ids(page) == ["q:t1:r9", "prop:k8"], timeout=8)
    check("a reconnect alone re-reads the inbox: the view and the count follow [mutation: no re-read on "
          "_connected]", _row_ids(page) == ["q:t1:r9", "prop:k8"] and _count(page) == "2" and _reads(mock) > reads,
          str(_row_ids(page)))


def _clicks_checks(page, mock, check, until):
    _open(page, mock, until)
    posts, appr = len(_answers(mock)), len([c for c in mock.calls if c[0] == "POST" and c[1].startswith("/approvals")])
    for rid in _row_ids(page):
        r = _row(page, rid)
        r.click(position={"x": 4, "y": 4})
        r.locator('[data-testid="inbox-title"]').click()
        if r.locator('[data-testid="inbox-blocks"]').count():
            r.locator('[data-testid="inbox-blocks"]').click()
        for sel in ('[data-testid="inbox-question"]', '[data-testid="inbox-brief"]', '[data-testid="inbox-options"]'):
            if r.locator(sel).count():
                r.locator(sel).click()
    time.sleep(0.4)
    check("a click anywhere on a row (its edge, title, line, text) posts nothing [mutation: row onClick answers]",
          len(_answers(mock)) == posts
          and len([c for c in mock.calls if c[0] == "POST" and c[1].startswith("/approvals")]) == appr)
    check("and opens nothing: the pane still shows the inbox", page.locator(_pane(1)).get_attribute("data-view") == "inbox")
    _row(page, "task:k1").locator('[data-testid="inbox-link"]').click()
    until(lambda: page.evaluate("window.__hud.state().taskId") == "k1", timeout=3)
    check("a task row's link opens its task", page.evaluate("window.__hud.state().taskId") == "k1"
          and page.locator(_pane(1)).get_attribute("data-view") == "task")
    page.locator('[data-testid="owner-inbox"]').click()
    until(lambda: _view(page).count() == 1, timeout=3)
    _row(page, "q:t1:r9").locator('[data-testid="inbox-link"]').click()
    until(lambda: page.locator(_pane(1)).get_attribute("data-view") == "chat", timeout=3)
    until(lambda: page.evaluate("window.__hud.state().chats['1'].threadId") == "t1", timeout=3)
    check("a question row's link opens its chat", page.evaluate("window.__hud.state().chats['1'].threadId") == "t1")
    check("and no link answered anything", len(_answers(mock)) == posts)


def _hostile_checks(page, mock, check, until):
    rows = [q_row(text="Pick one:\n" + HOSTILE, origin="Codex · task " + HOSTILE),
            prop_row(brief="ship it " + HOSTILE), task_row("kf", "failed", HOSTILE)]
    # A question row carries no origin from A1; an approval does. Put the
    # payload in both so either path is covered.
    c = card("hostilecard1", origin="Codex · task ‹" + HOSTILE + "›")
    _open(page, mock, until, rows + [appr_row(c)])
    imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length")
    check("no element is made from untrusted text [mutation: innerHTML]", imgs == 0, str(imgs))
    check("and nothing ran", page.evaluate("window.__pwned === undefined"))
    origin = _row(page, "appr:hostilecard1").locator('[data-testid="inbox-origin"]').inner_text()
    question = _row(page, "q:t1:r9").locator('[data-testid="inbox-question"]').inner_text()
    check("an approval's origin is shown as its literal text", HOSTILE in origin, origin)
    check("a question's text is shown as its literal text, its lines kept",
          question.startswith("Pick one:\n") and HOSTILE in question, question)
    check("so is a brief", HOSTILE in _row(page, "prop:k9").locator('[data-testid="inbox-brief"]').inner_text())
    check("the title is unchanged by any of it", page.title() == TITLE, page.title())


def _missed_checks(page, mock, check, until):
    """Inbox A3 (IN-3): "denied while you were away" rows."""
    rows = basic_rows() + [missed_row(task="k3", origin="Codex · task ‹" + HOSTILE + "›"),
                           missed_row("m2", resolution="shutdown"), missed_row("m3", thread=None, task="k4")]
    _open(page, mock, until, rows)
    fyi = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-group-fyi\"] "
                        "[data-testid=\"inbox-row\"]')].map(r => r.dataset.id)")
    check("missed rows sit under 'For your information' beside the failed task, in the daemon's order",
          fyi == ["task:kf", "missed:m1", "missed:m2", "missed:m3"], str(fyi))
    check("they are not counted: the count stays at ranks 1 to 5 [mutation: count missed rows]",
          _count(page) == "3", str(_count(page)))
    m1, m2, m3 = _row(page, "missed:m1"), _row(page, "missed:m2"), _row(page, "missed:m3")
    border = m1.evaluate("e => getComputedStyle(e).borderLeftColor")
    check("a missed row is neither amber, red nor the selection cyan",
          border not in (AMBER, RED, "rgb(111, 195, 223)"), border)
    check("its title and line are the daemon's code-built ones",
          m1.locator('[data-testid="inbox-title"]').inner_text() == "Denied while you were away: run_command"
          and m1.locator('[data-testid="inbox-blocks"]').inner_text() == "task k3 was denied it: nobody answered in time")
    check("it says why in the HUD's own words, by resolution",
          m1.locator('[data-testid="inbox-missed-why"]').inner_text() == "Nobody answered in time, so it was denied."
          and m2.locator('[data-testid="inbox-missed-why"]').inner_text()
          == "Jarvis stopped before you answered, so it was denied.")
    # Inbox B4: a rank-8 row may also be snoozed or dismissed (its own
    # `inbox-marks` strip, the marks section's); nothing else.
    outside = ("e => [...e.querySelectorAll('button, input, textarea, a')]"
               ".filter(b => !b.closest('[data-testid=\"inbox-marks\"]')).map(b => b.dataset.testid)")
    controls = m1.evaluate(outside)
    marks = m1.evaluate("e => [...e.querySelectorAll('[data-testid=\"inbox-marks\"] button')].map(b => b.dataset.testid)")
    check("its one control is 'Open its chat': no answer box, no Withdraw, no 'ask again' [mutation: an ask-again "
          "button]", controls == ["inbox-link"] and m1.locator('[data-testid="inbox-link"]').inner_text() == "Open its chat"
          and "again" not in m1.locator('[data-testid="inbox-link"]').inner_text().lower()
          and set(marks) <= {"inbox-snooze-hour", "inbox-snooze-tomorrow", "inbox-snooze-week", "inbox-dismiss"},
          str(controls + marks))
    check("one with no chat has no link at all, not even to its task",
          m3.evaluate(outside) == [], str(m3.evaluate(outside)))
    imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length")
    check("its origin is literal text: no element made, nothing ran [mutation: innerHTML]",
          imgs == 0 and page.evaluate("window.__pwned === undefined")
          and HOSTILE in m1.locator('[data-testid="inbox-origin"]').inner_text(), str(imgs))
    posts = len(_answers(mock))
    for r in (m1, m2, m3):
        r.click(position={"x": 4, "y": 4})
        r.locator('[data-testid="inbox-title"]').click()
        r.locator('[data-testid="inbox-missed-why"]').click()
    time.sleep(0.3)
    check("a click anywhere on it posts nothing and opens nothing",
          len(_answers(mock)) == posts and page.locator(_pane(1)).get_attribute("data-view") == "inbox")
    m2.locator('[data-testid="inbox-link"]').click()
    until(lambda: page.locator(_pane(1)).get_attribute("data-view") == "chat", timeout=3)
    until(lambda: page.evaluate("window.__hud.state().chats['1'].threadId") == "t1", timeout=3)
    check("its link opens its chat, and answers nothing",
          page.evaluate("window.__hud.state().chats['1'].threadId") == "t1" and len(_answers(mock)) == posts)
    check("the title is unchanged", page.title() == TITLE, page.title())


PR_REPO = "Johnwang688/Calc"


def pr_row(n=7, url=None, title="Add multiply"):
    """A PR row as inbox B3's poll serves it."""
    return {"id": f"pr:{PR_REPO}#{n}", "kind": "pr", "rank": 5, "title": f"PR #{n} is ready to merge",
            "blocks": f"{PR_REPO} · branch claude/x · checks passing", "project_id": None, "thread_id": None,
            "task_id": None, "asked_at": _iso(-3600), "expires_at": None, "origin": "",
            "detail": {"repo": PR_REPO, "number": n, "title": title, "branch": "claude/x", "head": "a" * 40,
                       "checks": "passing", "merge_state": "CLEAN",
                       "url": url if url is not None else f"https://github.com/{PR_REPO}/pull/{n}"}}


def _pr_checks(page, mock, check, until):
    """Inbox B3 (IN-4): pull requests and "PR list unavailable"."""
    rows = [task_row(), pr_row(title="Fix " + HOSTILE), pr_row(8, url="https://github.com/evil/repo/pull/8"),
            dec_row()]
    _open(page, mock, until, rows)
    you = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-group-you\"] "
                        "[data-testid=\"inbox-row\"]')].map(r => r.dataset.id)")
    check("PR rows sit under 'Waiting on you' after the blocked task",
          you == ["task:k1", f"pr:{PR_REPO}#7", f"pr:{PR_REPO}#8"], str(you))
    check("they are counted (ranks 1 to 5)", _count(page) == "3", str(_count(page)))
    r7, r8 = _row(page, f"pr:{PR_REPO}#7"), _row(page, f"pr:{PR_REPO}#8")
    # Inbox B4: a PR row (rank 5) may also be snoozed or dismissed, in its own `inbox-marks` strip.
    controls = r7.evaluate("e => [...e.querySelectorAll('button, input, textarea, a')]"
                           ".filter(b => !b.closest('[data-testid=\"inbox-marks\"]')).map(b => b.dataset.testid)")
    marks = r7.evaluate("e => [...e.querySelectorAll('[data-testid=\"inbox-marks\"] button')].map(b => b.dataset.testid)")
    link = r7.locator('[data-testid="inbox-pr-link"]')
    check("its one control is 'Open on GitHub', a new-tab link to its own PR [mutation: a link to any URL]",
          controls == ["inbox-pr-link"] and link.get_attribute("href") == f"https://github.com/{PR_REPO}/pull/7"
          and marks == ["inbox-snooze-hour", "inbox-snooze-tomorrow", "inbox-snooze-week", "inbox-dismiss"]
          and link.get_attribute("target") == "_blank" and link.get_attribute("rel") == "noopener noreferrer",
          str(controls))
    check("a URL for another repository is not linked",
          r8.locator("a").count() == 0, str(r8.locator("a").count()))
    imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length")
    check("the PR's title is literal text: no element made, nothing ran [mutation: innerHTML]",
          imgs == 0 and page.evaluate("window.__pwned === undefined")
          and HOSTILE in r7.locator('[data-testid="inbox-pr-title"]').inner_text(), str(imgs))
    posts, decided = len(_answers(mock)), len(_decided(mock))
    for r in (r7, r8):
        r.click(position={"x": 4, "y": 4})
        r.locator('[data-testid="inbox-title"]').click()
    time.sleep(0.3)
    check("a click on a PR row posts nothing and opens nothing",
          len(_answers(mock)) == posts and len(_decided(mock)) == decided
          and page.locator(_pane(1)).get_attribute("data-view") == "inbox")
    # Under a card the PR row is inert: a trusted click, a middle click or
    # Enter on its link opens no page (review of PR #320, NIT-2).
    c = card("prcard0001")
    mock.world["approvals"].append(c)
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    opened = []
    page.context.on("page", lambda p: opened.append(p))
    check("under a card the PR row is inert [mutation: not inert]",
          r7.evaluate("e => !!e.closest('[inert]')"))
    box = link.bounding_box()
    if box:
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.click(x, y)
        page.mouse.click(x, y, button="middle")
    link.evaluate("e => e.focus()")
    page.keyboard.press("Enter")
    time.sleep(0.5)
    for p in opened:
        p.close()
    check("a click, a middle click or Enter on its link under the card opens nothing [mutation: no auxclick guard]",
          not opened and cardloc.count() == 1, f"{len(opened)} pages opened")
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    unavailable = {"id": "prx:list", "kind": "pr_unavailable", "rank": 5,
                   "title": "PR list unavailable for 1 repository", "blocks": "open pull requests may be waiting",
                   "project_id": None, "thread_id": None, "task_id": None, "asked_at": None, "expires_at": None,
                   "origin": "", "detail": {"repos": [{"repo": PR_REPO, "reason": "gh is not logged in",
                                                       "last_read": None}]}}
    _open(page, mock, until, [unavailable])
    check("'PR list unavailable' is counted and never reads as nothing waiting [mutation: empty as nothing]",
          _count(page) == "1" and page.locator('[data-testid="inbox-empty"]').count() == 0
          and page.locator('[data-testid="inbox-pr-unavailable"]').inner_text() == f"{PR_REPO}: gh is not logged in",
          str(_count(page)))
    check("'PR list unavailable' offers no Snooze or Dismiss (inbox B4) [mutation: prx markable]",
          _row(page, "prx:list").locator('[data-testid="inbox-marks"]').count() == 0)
    check("the title is unchanged", page.title() == TITLE, page.title())


def _card_observe(page, mock, until, prefix, with_rows: bool):
    """Run the card's own assertions (tests/face/hud_v2_check.py `approval_checks`)
    and return what was seen, so the inbox's run can be compared with the chat
    pane's. `with_rows` also puts each card's `appr:` row in the inbox, as the
    daemon's projection would."""
    seen = {}
    cardloc = page.locator('[data-testid="approval-card"]')

    def raise_card(req, **over):
        c = card(req, **over)
        mock.world["approvals"].append(c)
        if with_rows:
            _st(mock)["rows"].insert(0, appr_row(c))
            mock_inbox.changed(mock, [f"appr:{req}"], "approval_requested")
        mock.emit("approval_requested", c)
        until(lambda: cardloc.count() == 1, timeout=4)
        time.sleep(0.25)
        return c

    def settle(req):
        until(lambda: cardloc.count() == 0, timeout=4)
        if with_rows:
            _st(mock)["rows"] = [r for r in _st(mock)["rows"] if r["id"] != f"appr:{req}"]
            mock_inbox.changed(mock, [f"appr:{req}"], "approval_resolved")

    r1 = f"{prefix}0001"
    raise_card(r1)
    seen["one card, the veil's"] = cardloc.count() == 1 and page.evaluate(
        "() => document.querySelectorAll('#authveil [data-testid=\"approval-card\"]').length === 1")
    seen["focus on the card"] = _active(page) == "approval-card"
    page.keyboard.press("Enter")
    time.sleep(0.3)
    seen["Enter does nothing"] = not _approval_posts(mock, r1) and cardloc.count() == 1
    page.keyboard.press("Tab")
    a = _active(page)
    page.keyboard.press("Tab")
    b = _active(page)
    page.keyboard.press("Shift+Tab")
    c_ = _active(page)
    seen["Tab and Shift+Tab stay on DENY"] = (a, b, c_) == ("approval-deny",) * 3
    seen["AUTHORIZE and ALWAYS are out of the Tab order"] = page.evaluate(
        "() => ['approval-allow','approval-always'].every(t => document.querySelector(`#authveil [data-testid=\"${t}\"]`).tabIndex === -1)")
    page.keyboard.press("Escape")
    body = until(lambda: _approval_posts(mock, r1) or None, timeout=3)
    seen["Escape denies"] = bool(body) and body[-1] == {"decision": "deny", "always": False}
    settle(r1)
    r2 = f"{prefix}0002"
    raise_card(r2)
    page.locator("#authveil [data-testid=\"approval-allow\"]").click()
    body = until(lambda: _approval_posts(mock, r2) or None, timeout=3)
    seen["AUTHORIZE allows, once, not always"] = bool(body) and body == [{"decision": "allow", "always": False}]
    settle(r2)
    r3 = f"{prefix}0003"
    raise_card(r3)
    page.locator("#authveil [data-testid=\"approval-always\"]").click()
    body = until(lambda: _approval_posts(mock, r3) or None, timeout=3)
    seen["ALWAYS allows and allowlists"] = bool(body) and body == [{"decision": "allow", "always": True}]
    settle(r3)
    r4 = f"{prefix}0004"
    raise_card(r4)
    page.locator("#authveil [data-testid=\"approval-deny\"]").click()
    body = until(lambda: _approval_posts(mock, r4) or None, timeout=3)
    seen["DENY denies"] = bool(body) and body == [{"decision": "deny", "always": False}]
    settle(r4)
    seen["the title is J.A.R.V.I.S."] = page.title() == TITLE
    return seen


def _card_checks(page, mock, check, until):
    # The card as it is with a chat pane: the reference run.
    _reset(mock, [])
    _fresh(page, mock, until, SINGLE)
    reference = _card_observe(page, mock, until, "chatpane", with_rows=False)
    check("reference: every card assertion holds with a chat pane", all(reference.values()),
          str({k: v for k, v in reference.items() if not v}))
    # The same assertions with the Inbox open and each card's row in it.
    _open(page, mock, until, basic_rows())
    inboxed = _card_observe(page, mock, until, "inboxpane", with_rows=True)
    for name, ok in inboxed.items():
        check(f"with the Inbox open: {name} (as with a chat pane)", ok == reference[name] and ok)
    check("the card with the Inbox open behaves exactly as with a chat pane [mutation: a second card]",
          inboxed == reference, str({k: (reference[k], v) for k, v in inboxed.items() if reference[k] != v}))

    # Under a card the inbox is inert, its approval row included.
    c = card("inertcard01")
    mock.world["approvals"].append(c)
    _st(mock)["rows"].insert(0, appr_row(c))
    mock_inbox.changed(mock, ["appr:inertcard01"], "approval_requested")
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    until(lambda: "appr:inertcard01" in _row_ids(page), timeout=3)
    check("the card's row is in the inbox, under Waiting now, saying its card is on screen",
          _row_ids(page)[0] == "appr:inertcard01"
          and _row(page, "appr:inertcard01").locator('[data-testid="inbox-card-note"]').inner_text()
          == "On the card now: answer it there.")
    check("the row is not a second card: no approval-card inside the inbox, nothing of the command shown",
          page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] [data-testid=\"approval-card\"]').length") == 0
          and "ls -la" not in _row(page, "appr:inertcard01").inner_text())
    check("the whole inbox is inert under the card", page.evaluate(
        "() => !!document.querySelector('[data-testid=\"inbox-view\"]').closest('[inert]')"))
    check("and its buttons are disabled too", page.evaluate(
        "() => [...document.querySelectorAll('[data-testid=\"inbox-view\"] button')].every(b => b.disabled)"))
    posts = len(_answers(mock))
    for sel in ('[data-testid="inbox-answer-send"]', '[data-testid="inbox-withdraw"]', '[data-testid="inbox-link"]',
                '[data-testid="inbox-answer"]'):
        loc = page.locator(f'[data-testid="inbox-view"] {sel}').first
        box = loc.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.evaluate("() => document.querySelector('[data-testid=\"inbox-answer\"]').focus()")
    page.keyboard.type("calc.py")
    page.keyboard.press("Enter")
    time.sleep(0.4)
    check("a trusted click on the inbox's controls and typing under the card post nothing",
          len(_answers(mock)) == posts and _active(page) not in ("inbox-answer",) and cardloc.count() == 1,
          f"{len(_answers(mock)) - posts} posts, focus {_active(page)}")
    check("and the answer box got none of it",
          page.locator('[data-testid="inbox-answer"]').first.input_value() == "")
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    _st(mock)["rows"] = [r for r in _st(mock)["rows"] if r["id"] != "appr:inertcard01"]
    mock_inbox.changed(mock, ["appr:inertcard01"], "approval_resolved")
    until(lambda: "appr:inertcard01" not in _row_ids(page), timeout=3)
    check("once answered the inbox is live again and the row is gone", page.evaluate(
        "() => !document.querySelector('[data-testid=\"inbox-view\"]').closest('[inert]')")
        and "appr:inertcard01" not in _row_ids(page))

    # A queued card: its row says so.
    a, b = card("queuedcard1"), card("queuedcard2")
    for c in (a, b):
        mock.world["approvals"].append(c)
        _st(mock)["rows"].insert(0, appr_row(c))
        mock.emit("approval_requested", c)
    mock_inbox.changed(mock, ["appr:queuedcard1", "appr:queuedcard2"], "approval_requested")
    until(lambda: cardloc.count() == 1 and "appr:queuedcard2" in _row_ids(page), timeout=4)
    check("a queued card's row says it is queued behind the one on screen",
          "Queued" in _row(page, "appr:queuedcard2").locator('[data-testid="inbox-card-note"]').inner_text()
          and cardloc.get_attribute("data-req") == "queuedcard1")
    for req in ("queuedcard1", "queuedcard2"):
        until(lambda: cardloc.count() == 1 and cardloc.get_attribute("data-req") == req, timeout=4)
        page.locator("#authveil [data-testid=\"approval-deny\"]").click()
        until(lambda: _approval_posts(mock, req), timeout=3)
    until(lambda: cardloc.count() == 0, timeout=4)
    _st(mock)["rows"] = [r for r in _st(mock)["rows"] if not r["id"].startswith("appr:")]
    mock_inbox.changed(mock, [], "approval_resolved")

    # A private (HUD-only) card: its private detail is fetched once, by the
    # veil's card, and its row carries none of it.
    req = "Pv0123456789"
    p = {"req_id": req, "code": "PV01", "tool": "hud_browser_act", "layer": "browser-gate", "hud_only": True,
         "thread_id": "t1", "asked_at": _iso(), "timeout_s": 120, "allowlistable": False, "args": {}, "command": None}
    detail_path = f"/browser/approvals/{req}"
    fetched = lambda: len([c for c in mock.calls if c[0] == "GET" and c[1] == detail_path])  # noqa: E731
    before = fetched()
    mock.world["approvals"].append(p)
    _st(mock)["rows"].insert(0, appr_row(p))
    mock_inbox.changed(mock, [f"appr:{req}"], "approval_requested")
    mock.emit("approval_requested", p)
    until(lambda: cardloc.count() == 1 and f"appr:{req}" in _row_ids(page), timeout=4)
    time.sleep(0.6)
    check("a private card is the veil's own Browser card, the only one",
          cardloc.count() == 1 and "browserauth" in (cardloc.get_attribute("class") or "")
          and page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] [data-testid=\"approval-card\"]').length") == 0)
    check("its private detail is fetched once, by the veil's card, never by the inbox [mutation: a second card]",
          fetched() - before == 1, str(fetched() - before))
    row_text = _row(page, f"appr:{req}").inner_text()
    check("its row shows the row's own fields only (no layer, code or command)",
          "hud_browser_act waits for your approval in the HUD" in row_text and "PV01" not in row_text
          and "browser-gate" not in row_text and _row(page, f"appr:{req}").locator('[data-testid="inbox-origin"]').count() == 0,
          row_text)
    page.locator("#authveil [data-testid=\"approval-deny\"]").click()
    body = until(lambda: _approval_posts(mock, req) or None, timeout=3)
    check("and DENY on it answers as it does in its own pane", bool(body) and body[-1].get("decision") == "deny",
          str(body))
    until(lambda: cardloc.count() == 0, timeout=4)


def _badge_checks(browser, mock, check, until, guard, init_script, base):
    for label, script in (("stubbed", BADGE_STUB), ("absent", NO_BADGE_API)):
        ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
        guard(ctx)
        ctx.add_init_script(init_script)
        ctx.add_init_script(script)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            _reset(mock)
            before = mock.sse_connections()
            page.goto(base + "/")
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            until(lambda: _count(page) == "3", timeout=4)
            if label == "stubbed":
                badges = lambda: page.evaluate("window.__badges")  # noqa: E731
                until(lambda: ["set", 3] in badges(), timeout=3)
                check("the app badge is set to the count (ranks 1 to 5) [mutation: badge all ranks]",
                      ["set", 3] in badges() and ["set", 4] not in badges(), str(badges()))
                _reset(mock, [task_row("kf", "failed")])
                mock_inbox.changed(mock)
                until(lambda: badges() and badges()[-1] == ["clear"], timeout=3)
                check("and cleared at 0 [mutation: no clear]", badges()[-1] == ["clear"], str(badges()))
                n = len(badges())
                mock_inbox.changed(mock)
                time.sleep(0.5)
                check("a re-read with the same count does not touch the badge again", len(badges()) == n,
                      str(badges()))
                # A read that fails clears a badge it had set (review of PR #305):
                # an older daemon's 404, and a daemon error.
                for fail in ((404, "route not found"), (500, "the daemon hit an error")):
                    _reset(mock)
                    mock_inbox.changed(mock)
                    until(lambda: badges()[-1] == ["set", 3], timeout=3)
                    _st(mock)["fail"] = fail
                    mock_inbox.changed(mock)
                    until(lambda: badges()[-1] == ["clear"], timeout=3)
                    check(f"a failed read ({fail[0]}) clears the badge rather than leave a stale count "
                          "[mutation: a stale badge]", badges()[-1] == ["clear"] and _count(page) is None,
                          str(badges()[-3:]))
                    _st(mock)["fail"] = None
                check("the title stays J.A.R.V.I.S. while the badge moves", page.title() == TITLE, page.title())
            else:
                check("without the Badging API the inbox still counts and nothing throws",
                      _count(page) == "3" and not errors, "; ".join(errors[:2]))
        finally:
            ctx.close()


OWNER_ROW = '[data-testid="owner-inbox"]'
TEXT = "rgb(226, 223, 215)"    # --text
BG_1 = "rgb(26, 26, 25)"       # --bg-1


def _pseudo(page, selector, pseudo, prop):
    return page.evaluate("([s, p, q]) => { const e = document.querySelector(s); "
                         "return e ? getComputedStyle(e, p)[q] : null; }", [selector, pseudo, prop])


def _aurora_state(page):
    return page.locator(OWNER_ROW).get_attribute("data-aurora")


def _aurora_shot(page, name):
    import os
    where = os.environ.get("JARVIS_AURORA_SHOTS")
    if not where:
        return
    time.sleep(1.2)     # some drift, and a spark, into the frame
    box = page.locator("#sidebar").bounding_box()
    path = Path(where) / f"hud-aurora-{name}.png"
    page.screenshot(path=str(path), clip={"x": box["x"], "y": box["y"], "width": box["width"],
                                          "height": min(box["height"], 260)})
    print(f"  (screenshot: {path})")


def _aurora_checks(page, mock, check, until):
    # The mock's `inbox: true` project (p1), named as the real one is.
    inbox_project = next(p for p in mock.world["projects"] if p.get("inbox"))
    was = inbox_project["name"]
    inbox_project["name"] = "Inbox"
    try:
        _aurora_body(page, mock, check, until, inbox_project["id"])
    finally:
        inbox_project["name"] = was


def _aurora_body(page, mock, check, until, inbox_id):
    _reset(mock, [])
    reads = _reads(mock)
    _fresh(page, mock, until, SINGLE)
    row = page.locator(OWNER_ROW)
    until(lambda: row.count() == 1 and _reads(mock) > reads, timeout=4)
    until(lambda: _aurora_state(page) == "calm", timeout=3)
    project = f'[data-testid="project-{inbox_id}"]'
    check("the owner Inbox row carries the aurora, and its label is still Inbox",
          "aurora" in (row.get_attribute("class") or "") and row.locator(".nm").inner_text() == "Inbox"
          and "gradient" in (_pseudo(page, OWNER_ROW, "::before", "backgroundImage") or ""))
    check("the inbox: true project's row, also labelled Inbox, has none "
          "[mutation: the aurora on the project row]",
          page.locator(f'[data-testid="project-name-{inbox_id}"]').inner_text() == "Inbox"
          and page.evaluate(f"() => document.querySelectorAll('{project}.aurora, {project} .aurora').length") == 0
          and _pseudo(page, project, "::before", "animationName") in ("none", None)
          and _pseudo(page, project, "::after", "animationName") in ("none", None)
          and "gradient" not in (_pseudo(page, project, "::before", "backgroundImage") or ""))
    check("with nothing waiting it is a still tint: no animation on either layer, no sparks "
          "[mutation: calm animates]",
          _pseudo(page, OWNER_ROW, "::before", "animationName") == "none"
          and _pseudo(page, OWNER_ROW, "::after", "animationName") == "none"
          and _pseudo(page, OWNER_ROW, "::after", "display") == "none")
    frames = page.evaluate("""() => {
      const out = {};
      for (const sheet of document.styleSheets) {
        let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
        for (const r of rules) {
          if (r.type !== CSSRule.KEYFRAMES_RULE || !r.name.startsWith('aurora-')) continue;
          const props = new Set();
          for (const k of r.cssRules) for (let i = 0; i < k.style.length; i++) props.add(k.style[i]);
          out[r.name] = [...props].sort();
        }
      }
      return out;
    }""")
    check("the shipped keyframes animate only transform (the band) and opacity (the sparks) "
          "[mutation: a background-position drift]",
          frames == {"aurora-drift": ["transform"], "aurora-twinkle": ["opacity"]}, str(frames))
    check("the row's text is --text, unchanged",
          row.locator(".nm").evaluate("e => getComputedStyle(e).color") == TEXT)
    check("the title is unchanged", page.title() == TITLE, page.title())
    _aurora_shot(page, "calm")

    # Something waits, nothing pending: the drift and the sparks.
    _reset(mock, [prop_row(), task_row()])
    mock_inbox.changed(mock)
    until(lambda: _count(page) == "2" and _aurora_state(page) == "live", timeout=4)
    check("while something waits (not pending) the band drifts and the sparks twinkle",
          _pseudo(page, OWNER_ROW, "::before", "animationName") == "aurora-drift"
          and _pseudo(page, OWNER_ROW, "::before", "animationDuration") == "11s"
          and _pseudo(page, OWNER_ROW, "::after", "animationName") == "aurora-twinkle"
          and _pseudo(page, OWNER_ROW, "::after", "display") == "block")
    _aurora_shot(page, "waiting")

    # Pending (a rank-2 question): the amber count reads exactly as before.
    _reset(mock)
    mock_inbox.changed(mock)
    until(lambda: _count(page) == "3" and _aurora_state(page) == "pending", timeout=4)
    badge = page.locator('[data-testid="owner-inbox-count"]')
    check("while pending the count is amber on an opaque --bg-1 chip",
          badge.evaluate("e => getComputedStyle(e).color") == AMBER
          and badge.evaluate("e => getComputedStyle(e).backgroundColor") == BG_1,
          badge.evaluate("e => [getComputedStyle(e).color, getComputedStyle(e).backgroundColor].join(' / ')"))
    check("and the aurora steps back: dimmed band, still drifting, no sparks",
          float(_pseudo(page, OWNER_ROW, "::before", "opacity")) < 1
          and _pseudo(page, OWNER_ROW, "::before", "animationName") == "aurora-drift"
          and _pseudo(page, OWNER_ROW, "::after", "display") == "none")
    _aurora_shot(page, "pending")

    # Selected (a pane shows the inbox): the 2px accent bar is left clear of
    # the layers the isolated row paints over its own inset shadow.
    row.click()
    until(lambda: "sel" in (row.get_attribute("class") or ""), timeout=3)
    check("selected, the accent bar stays visible: the layers start 2px in, the bar is the accent",
          _pseudo(page, OWNER_ROW, "::before", "left") == "2px"
          and _pseudo(page, OWNER_ROW, "::after", "left") == "2px"
          and "rgb(111, 195, 223)" in row.evaluate("e => getComputedStyle(e).boxShadow"),
          row.evaluate("e => getComputedStyle(e).boxShadow"))

    # No layout shift: the same box with and without the aurora, nothing overflows.
    boxes = page.evaluate("""(sel) => {
      const r = document.querySelector(sel);
      const box = () => { const b = r.getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; };
      const a = box(); r.classList.remove('aurora'); const b = box(); r.classList.add('aurora');
      return {same: JSON.stringify(a) === JSON.stringify(b), fits: getComputedStyle(r).overflowX === "hidden",
              sidebar: document.getElementById('sidebar').scrollWidth <= document.getElementById('sidebar').clientWidth};
    }""", OWNER_ROW)
    check("the aurora moves no layout: the same box without it, clipped by the row, nothing overflows the sidebar",
          boxes["same"] and boxes["fits"] and boxes["sidebar"], str(boxes))

    # The approval card is the loudest thing on screen: the aurora pauses under it.
    check("it runs while no card is up",
          _pseudo(page, OWNER_ROW, "::before", "animationPlayState") == "running")
    c = card("auroracard01")
    mock.world["approvals"].append(c)
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    until(lambda: page.evaluate(f"() => !!document.querySelector('{OWNER_ROW}').closest('[inert]')"), timeout=3)
    check("under the approval card the aurora pauses [mutation: no veil pause]",
          _pseudo(page, OWNER_ROW, "::before", "animationPlayState") == "paused"
          and _pseudo(page, OWNER_ROW, "::after", "animationPlayState") == "paused",
          str(_pseudo(page, OWNER_ROW, "::before", "animationPlayState")))
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    until(lambda: _pseudo(page, OWNER_ROW, "::before", "animationPlayState") == "running", timeout=3)
    check("and runs again once the card is answered", True)

    # prefers-reduced-motion: a static tint, no animation at all.
    _reset(mock, [prop_row(), task_row()])
    mock_inbox.changed(mock)
    until(lambda: _aurora_state(page) == "live", timeout=4)
    page.emulate_media(reduced_motion="reduce")
    try:
        time.sleep(0.1)
        check("under prefers-reduced-motion the band does not animate [mutation: no reduced-motion rule]",
              _pseudo(page, OWNER_ROW, "::before", "animationName") == "none",
              str(_pseudo(page, OWNER_ROW, "::before", "animationName")))
        check("nor do the sparks, which are gone",
              _pseudo(page, OWNER_ROW, "::after", "animationName") == "none"
              and _pseudo(page, OWNER_ROW, "::after", "display") == "none")
        check("the tint stays, still: the gradient is there, unmoved",
              "gradient" in (_pseudo(page, OWNER_ROW, "::before", "backgroundImage") or "")
              and _pseudo(page, OWNER_ROW, "::before", "transform") in ("none", "matrix(1, 0, 0, 1, 0, 0)"),
              str(_pseudo(page, OWNER_ROW, "::before", "transform")))
    finally:
        page.emulate_media(reduced_motion="no-preference")


def _older_checks(page, mock, check, until):
    _reset(mock)
    _st(mock)["fail"] = (404, "route not found")
    _fresh(page, mock, until, SINGLE)
    page.locator('[data-testid="owner-inbox"]').click()
    until(lambda: _view(page).count() == 1 and _view(page).get_attribute("data-status") == "unavailable", timeout=4)
    check("an older daemon (404): the sidebar shows no count, never a zero", _count(page) is None)
    check("and the view says the inbox is not available, never 'nothing waiting' [mutation: 404 read as empty]",
          page.locator('[data-testid="inbox-unavailable"]').count() == 1
          and page.locator('[data-testid="inbox-empty"]').count() == 0)
    _st(mock)["fail"] = None


def dec_row(did="d0123456789ab", thread="t1", default=1, question="Which store?", why="It decides the schema.",
            options=("sqlite", "postgres", "files"), origin="Claude · chat ‹jarvis› (t1)"):
    """An open decision as inbox B1's projection serves it (rank 6)."""
    return {"id": f"dec:{did}", "kind": "decision", "rank": 6, "title": f"chat {thread} asks for your decision",
            "blocks": f"nothing waits; chat {thread} hears your answer as a message", "project_id": "p1",
            "thread_id": thread, "task_id": None, "asked_at": _iso(-120), "expires_at": None, "origin": origin,
            "detail": {"decision_id": did, "question": question, "options": list(options), "default": default,
                       "why": why, "answer": None, "delivery": None}}


def _decided(mock):
    return list(_st(mock).get("decided", []))


def _decision_checks(page, mock, check, until):
    """Inbox B1 (IN-5): decision rows, rank 6, answered at /inbox/decisions/<id>."""
    hostile = dec_row(question="Pick:\n" + HOSTILE, why="because " + HOSTILE,
                      options=("sqlite", "postgres " + HOSTILE, "files"), origin="Claude · chat ‹" + HOSTILE + "› (t1)")
    _st(mock)["decided"] = []
    _open(page, mock, until, basic_rows() + [hostile])
    answers_before = len(_answers(mock))
    row = _row(page, "dec:d0123456789ab")
    time_ids = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-group-time\"] "
                             "[data-testid=\"inbox-row\"]')].map(r => r.dataset.id)")
    check("a decision sits under 'When you have time'", time_ids == ["dec:d0123456789ab"], str(time_ids))
    check("it is not counted: the count stays at ranks 1 to 5", _count(page) == "3", str(_count(page)))
    border = row.evaluate("e => getComputedStyle(e).borderLeftColor")
    check("Graphite: a decision row is neither amber, red nor the selection cyan",
          border not in (AMBER, RED, "rgb(111, 195, 223)"), border)
    radios = row.locator('[data-testid="inbox-decision-option"]')
    checked = [radios.nth(i).is_checked() for i in range(radios.count())]
    time.sleep(0.4)
    check("its options are radios with the suggestion preselected, and nothing was posted [mutation: auto-submit]",
          checked == [False, True, False] and _decided(mock) == [] and len(_answers(mock)) == answers_before,
          str(checked))
    imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length")
    check("its question, options, why and origin are literal text: no element made, nothing ran [mutation: innerHTML]",
          imgs == 0 and page.evaluate("window.__pwned === undefined")
          and HOSTILE in row.locator('[data-testid="inbox-decision-question"]').inner_text()
          and HOSTILE in row.locator('[data-testid="inbox-decision-why"]').inner_text()
          and HOSTILE in row.locator('[data-testid="inbox-origin"]').inner_text(), str(imgs))
    # Enter on a focused radio answers nothing; the arrows still move the pick.
    radios.nth(0).click()
    radios.nth(0).press("Enter")
    time.sleep(0.4)
    check("Enter on a focused radio posts nothing [mutation: submit on Enter]", _decided(mock) == [],
          str(_decided(mock)))
    radios.nth(0).press("ArrowDown")
    check("the arrow keys still move the pick", radios.nth(1).is_checked(), str(radios.nth(1).is_checked()))
    radios.nth(2).click()
    send = row.locator('[data-testid="inbox-decision-send"]')
    send.dblclick()
    until(lambda: _decided(mock), timeout=3)
    time.sleep(0.4)
    got = _decided(mock)
    check("Answer posts the picked option's index once, to /inbox/decisions/<id> [mutation: double post]",
          len(got) == 1 and got[0][0] == "/inbox/decisions/d0123456789ab" and got[0][2] == {"choice": 2}, str(got))
    until(lambda: "dec:d0123456789ab" not in _row_ids(page), timeout=3)
    note = page.locator('[data-testid="inbox-notice"]')
    check("the row leaves on inbox_changed, and the daemon's note is said",
          note.count() == 1 and "Sent to the asking chat" in note.inner_text())

    # The owner's own words, from the focused box with Enter.
    _st(mock)["rows"].append(dec_row("d00000000000b", default=None))
    mock_inbox.changed(mock, ["dec:d00000000000b"], "decision_posted")
    until(lambda: "dec:d00000000000b" in _row_ids(page), timeout=3)
    row = _row(page, "dec:d00000000000b")
    check("with no suggestion nothing is preselected and Answer is off",
          not any(row.locator('[data-testid="inbox-decision-option"]').nth(i).is_checked() for i in range(3))
          and row.locator('[data-testid="inbox-decision-send"]').is_disabled())
    box = row.locator('[data-testid="inbox-decision-text"]')
    box.fill("use the files for now")
    box.press("Enter")
    until(lambda: len(_decided(mock)) > 1, timeout=3)
    check("Enter in the focused box posts the owner's words as {text}",
          _decided(mock)[-1][1:] == ("d00000000000b", {"text": "use the files for now"}), str(_decided(mock)[-1:]))

    # Under a card the row is inert, as the rest of the inbox.
    _st(mock)["rows"].append(dec_row("d00000000000c"))
    mock_inbox.changed(mock, ["dec:d00000000000c"], "decision_posted")
    until(lambda: "dec:d00000000000c" in _row_ids(page), timeout=3)
    c = card("deccard0001")
    mock.world["approvals"].append(c)
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    row = _row(page, "dec:d00000000000c")
    check("under a card the decision row is inert and its Answer disabled",
          row.evaluate("e => !!e.closest('[inert]')") and row.locator('[data-testid="inbox-decision-send"]').is_disabled())
    posts = len(_decided(mock))
    for sel in ('[data-testid="inbox-decision-option"]', '[data-testid="inbox-decision-send"]',
                '[data-testid="inbox-decision-text"]'):
        found = row.locator(sel).first.bounding_box()
        if found:
            page.mouse.click(found["x"] + found["width"] / 2, found["y"] + found["height"] / 2)
    page.keyboard.type("x")
    page.keyboard.press("Enter")
    time.sleep(0.4)
    check("a trusted click, typing and Enter under the card post nothing [mutation: not inert]",
          len(_decided(mock)) == posts and cardloc.count() == 1, f"{len(_decided(mock)) - posts} posts")
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    check("the title is unchanged", page.title() == TITLE, page.title())


def defer_row(did="D1", thread="t1", *, rank=4, grantable=True, hud_only=False, command="make deploy"):
    """An approval deferred during quiet hours (QH) as `quiet_defer.rows` serves it."""
    c = {"req_id": did, "tool": "Bash", "args": {"command": command}, "command": command,
         "reason": "ask profile: every gated call reaches the owner", "layer": "human", "thread_id": thread,
         "task_id": None, "provider": "claude", "origin": "", "asked_at": _iso(-3600), "allowlistable": True,
         "headline": ""}
    return {"id": f"defer:{did}", "kind": "deferred", "rank": rank,
            "title": "Bash is waiting for your morning approval" if rank == 4
            else "Deferred overnight and not approved: Bash",
            "blocks": f"chat {thread} was told to carry on without it", "project_id": None, "thread_id": thread,
            "task_id": None, "asked_at": _iso(-3600), "expires_at": None, "origin": "",
            "detail": {"deferral_id": did, "tool": "Bash", "card": c, "thread_id": thread, "task_id": None,
                       "at": _iso(-3600), "until": _iso(3600), "grantable": grantable, "expired": rank != 4,
                       "hud_only": hud_only}}


def _summary_checks(page, mock, check, until):
    """AS: a deferred row's buttons first, its AI summary as text, code's WHY
    ASKED and WHERE, its command collapsed only with a summary and one click
    away; an approval row shows its card's summary and code lines."""
    long = "cd /srv/calc\nmake deploy --prod && git push origin main"
    with_summary = defer_row("S1", command=long)
    with_summary["detail"]["summary"] = HOSTILE + " deploys calc"
    with_summary["detail"]["card"]["where"] = "Calc · main · ~/projects/calc"
    without = defer_row("S2", command=long)
    c = card("sumcard0001", summary="Lists the folder.", summary_state="ready", where="Calc · main · ~/c")
    _open(page, mock, until, basic_rows() + [with_summary, without])
    row = _row(page, "defer:S1")
    order = row.evaluate("""r => { const b = r.querySelector('[data-testid="inbox-deferred-buttons"]');
        const s = r.querySelector('[data-testid="inbox-deferred-summary"]');
        return !!(b && s && (b.compareDocumentPosition(s) & Node.DOCUMENT_POSITION_FOLLOWING)); }""")
    check("AS: a deferred row's buttons come before its summary", order)
    check("AS: the summary is marked and drawn as text",
          row.locator('[data-testid="inbox-deferred-summary"]').inner_text().startswith("AI SUMMARY")
          and page.evaluate("window.__pwned === undefined")
          and page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length") == 0)
    check("AS: WHY ASKED and WHERE are code's lines",
          row.locator('[data-testid="inbox-deferred-reason"]').inner_text().startswith("WHY ASKED [human] ")
          and row.locator('[data-testid="inbox-deferred-where"]').inner_text() == "WHERE Calc · main · ~/projects/calc")
    check("AS: with a summary the command is collapsed to its first line",
          row.locator('[data-testid="inbox-deferred-command"]').count() == 0
          and row.locator('[data-testid="inbox-deferred-preview"]').inner_text().strip() == "cd /srv/calc")
    row.locator('[data-testid="inbox-deferred-toggle"]').click()
    check("AS: one click shows the whole command",
          row.locator('[data-testid="inbox-deferred-command"]').inner_text() == long)
    other = _row(page, "defer:S2")
    check("AS: with no summary the command is whole and there is no toggle",
          other.locator('[data-testid="inbox-deferred-command"]').inner_text() == long
          and other.locator('[data-testid="inbox-deferred-toggle"]').count() == 0)
    mock.world["approvals"].append(c)
    _st(mock)["rows"].insert(0, appr_row(c))
    mock_inbox.changed(mock, ["appr:sumcard0001"], "approval_requested")
    mock.emit("approval_requested", c)
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
    arow = _row(page, "appr:sumcard0001")
    until(lambda: arow.locator('[data-testid="inbox-summary"]').count() == 1, timeout=4)
    check("AS: an approval row shows its card's summary and Where",
          "Lists the folder." in arow.locator('[data-testid="inbox-summary"]').inner_text()
          and arow.locator('[data-testid="inbox-where"]').inner_text() == "WHERE Calc · main · ~/c",
          str(page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-summary\"]')].map(e => e.closest('[data-id]')?.dataset.id)")))
    check("AS: and still nothing of the command",
          "ls -la" not in _row(page, "appr:sumcard0001").inner_text())
    page.locator('[data-testid="approval-card"] button.deny').click()
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    _st(mock)["rows"] = [r for r in _st(mock)["rows"] if r["id"] != "appr:sumcard0001"]
    mock_inbox.changed(mock, ["appr:sumcard0001"], "approval_resolved")
    mock.world["approvals"].clear()


def _deferred_checks(page, mock, check, until):
    """QH: a deferred approval, rank 4, Approve and retry and Leave it."""
    rows = basic_rows() + [defer_row(command="make deploy " + HOSTILE), defer_row("D2", hud_only=True, grantable=False),
                           defer_row("D3", rank=8, grantable=False)]
    _open(page, mock, until, rows)
    you = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-group-you\"] "
                        "[data-testid=\"inbox-row\"]')].map(r => r.dataset.id)")
    check("a deferred row sits under 'Waiting on you' at rank 4 (the Browser one too)",
          "defer:D1" in you and "defer:D2" in you and "defer:D3" not in you, str(you))
    check("rank 4 deferred rows are counted [mutation: leave them uncounted]", _count(page) == "5", str(_count(page)))
    row = _row(page, "defer:D1")
    imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length")
    check("its card's command is literal text: no element made, nothing ran [mutation: innerHTML]",
          imgs == 0 and page.evaluate("window.__pwned === undefined")
          and HOSTILE in row.locator('[data-testid="inbox-deferred-command"]').inner_text(), str(imgs))
    check("no Snooze or Dismiss on any deferred row [mutation: markable]",
          all(_row(page, i).locator('[data-testid="inbox-marks"]').count() == 0
              for i in ("defer:D1", "defer:D2", "defer:D3")))
    check("Approve and retry only where it may be: never a Browser card or a day-old row",
          row.locator('[data-testid="inbox-deferred-retry"]').count() == 1
          and _row(page, "defer:D2").locator('[data-testid="inbox-deferred-retry"]').count() == 0
          and _row(page, "defer:D3").locator('[data-testid="inbox-deferred-retry"]').count() == 0
          and _row(page, "defer:D2").locator('[data-testid="inbox-deferred-leave"]').count() == 1)
    check("nothing is focused by default [mutation: autofocus]",
          _active(page) not in ("inbox-deferred-retry", "inbox-deferred-leave"), str(_active(page)))
    before = len(_answers(mock))
    row.click(position={"x": 4, "y": 4})
    time.sleep(0.3)
    check("a click on the row posts nothing", len(_answers(mock)) == before)
    row.locator('[data-testid="inbox-deferred-retry"]').dblclick()
    until(lambda: len(_answers(mock)) > before, timeout=3)
    time.sleep(0.4)
    got = _answers(mock)[before:]
    check("Approve and retry posts {action: retry} once, the id through encodeURIComponent [mutation: double post]",
          len(got) == 1 and got[0][0] == "/inbox/defer%3AD1/answer" and got[0][2] == {"action": "retry"}, str(got))
    until(lambda: "defer:D1" not in _row_ids(page), timeout=3)
    note = page.locator('[data-testid="inbox-notice"]')
    check("the row leaves and the daemon's note is said",
          note.count() == 1 and "Approved once for 30 minutes" in note.inner_text())
    before = len(_answers(mock))
    _row(page, "defer:D3").locator('[data-testid="inbox-deferred-leave"]').click()
    until(lambda: len(_answers(mock)) > before, timeout=3)
    check("Leave it posts {action: leave}", _answers(mock)[-1][2] == {"action": "leave"}, str(_answers(mock)[-1]))
    # Under a card the row is inert, as the rest of the inbox.
    c = card("defcard0001")
    mock.world["approvals"].append(c)
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    row = _row(page, "defer:D2")
    check("under a card the deferred row is inert and Leave it disabled",
          row.evaluate("e => !!e.closest('[inert]')") and row.locator('[data-testid="inbox-deferred-leave"]').is_disabled())
    posts = len(_answers(mock))
    found = row.locator('[data-testid="inbox-deferred-leave"]').bounding_box()
    if found:
        page.mouse.click(found["x"] + found["width"] / 2, found["y"] + found["height"] / 2)
    page.keyboard.press("Enter")
    time.sleep(0.4)
    check("a trusted click and Enter under the card post nothing [mutation: not inert]",
          len(_answers(mock)) == posts and cardloc.count() == 1, f"{len(_answers(mock)) - posts} posts")
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    check("the title is unchanged", page.title() == TITLE, page.title())


def chk_row(cid="c0123456789ab", thread="t1", steps=None, prs=(), markable=True, deploy_text="",
            title="Quick review", origin="Claude · chat ‹jarvis› (t1)"):
    """An open live check as inbox B2's projection serves it (rank 7)."""
    steps = steps or [{"do": "Press Quick review.", "expect": "A spinner shows."},
                      {"do": "Wait for the result.", "expect": "A panel lists findings."}]
    return {"id": f"chk:{cid}", "kind": "check", "rank": 7,
            "title": f"Live check {cid} from chat {thread}: 0 of {len(steps)} steps marked",
            "blocks": deploy_text or "nothing waits; your marks are the receipt", "project_id": "p1",
            "thread_id": thread, "task_id": None, "asked_at": _iso(-60), "expires_at": None, "origin": origin,
            "detail": {"check_id": cid, "title": title, "prs": list(prs), "steps": steps, "results": [],
                       "done": False, "deploy": {"state": "deployed" if markable else "not confirmed deployed",
                                                 "text": deploy_text, "markable": markable},
                       "merged": False, "deployed_sha": None, "created_at": _iso(-60), "imported_from": None}}


def _marked(mock):
    return list(_st(mock).get("marked", []))


def mark_pr_row(n=301, head="a" * 40):
    """Inbox B3's rank-5 PR row (#320), as its projection serves it."""
    return {"id": f"pr:Johnwang688/jarvis#{n}", "kind": "pr", "rank": 5, "title": f"PR #{n} is ready to merge",
            "blocks": "waits on your merge", "project_id": "p1", "thread_id": None, "task_id": None,
            "asked_at": _iso(-120), "expires_at": None, "origin": "",
            "detail": {"repo": "Johnwang688/jarvis", "number": n, "title": "x", "head": head, "checks": "success"}}


def mark_prx_row():
    """Inbox B3's "PR list unavailable" (#320): never snoozed or dismissed."""
    return {"id": "prx:list", "kind": "pr_unavailable", "rank": 5, "title": "PR list unavailable",
            "blocks": "Jarvis could not read the open PRs", "project_id": None, "thread_id": None,
            "task_id": None, "asked_at": _iso(-30), "expires_at": None, "origin": "", "detail": {"repos": []}}


def _mark_posts(mock):
    return list(_st(mock).get("mark_posts", []))


def _marks_checks(page, mock, check, until):
    """Inbox B4 (IN-7, IN-8): Snooze and Dismiss on ranks 5 to 8, Show hidden, Undo."""
    _st(mock)["mark_posts"], _st(mock)["marks"] = [], {}
    reads_all = _st(mock).get("all_reads", 0)
    _open(page, mock, until, basic_rows() + [mark_pr_row(), mark_prx_row(), dec_row(), missed_row()])
    check("the HUD reads /inbox?all=1", _st(mock).get("all_reads", 0) > reads_all)
    with_marks = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-row\"]')]"
                               ".filter(r => r.querySelector('[data-testid=\"inbox-marks\"]')).map(r => r.dataset.id)")
    check("Snooze and Dismiss show on ranks 5 to 8 only, never on 1 to 4 or 'PR list unavailable' "
          "[mutation: marks on rank 1 to 4]",
          sorted(with_marks) == sorted(["pr:Johnwang688/jarvis#301", "dec:d0123456789ab", "task:kf", "missed:m1"]),
          str(with_marks))
    labels = _row(page, "task:kf").locator('[data-testid="inbox-marks"] button').all_inner_texts()
    check("its buttons are Snooze 1 h, Until tomorrow, 1 week and Dismiss",
          labels == ["Snooze 1 h", "Until tomorrow", "1 week", "Dismiss"], str(labels))
    check("the count is ranks 1 to 5: the question, proposal, blocked task, PR and 'PR list unavailable'",
          _count(page) == "5", str(_count(page)))
    # A click on a row, its title and its words marks nothing.
    _row(page, "task:kf").locator('[data-testid="inbox-title"]').click()
    _row(page, "task:kf").click(position={"x": 4, "y": 4})
    time.sleep(0.3)
    check("a click on a row marks nothing [mutation: row onClick marks]", _mark_posts(mock) == [],
          str(_mark_posts(mock)))
    focused = _active(page)
    check("nothing is focused or defaulted to a mark button", not str(focused).startswith("inbox-snooze")
          and focused != "inbox-dismiss", str(focused))
    # Dismiss a rank-5 PR: it leaves the list and the count.
    _row(page, "pr:Johnwang688/jarvis#301").locator('[data-testid="inbox-dismiss"]').dblclick()
    until(lambda: "pr:Johnwang688/jarvis#301" not in _row_ids(page), timeout=3)
    time.sleep(0.3)
    posts = _mark_posts(mock)
    check("Dismiss posts {dismiss: true} once, the id through encodeURIComponent [mutation: double post; raw id]",
          len(posts) == 1 and posts[0] == ("/inbox/pr%3AJohnwang688%2Fjarvis%23301/mark",
                                           "pr:Johnwang688/jarvis#301", {"dismiss": True}), str(posts))
    until(lambda: _count(page) == "4", timeout=3)
    check("the hidden PR leaves the count [mutation: count hidden rows]", _count(page) == "4", str(_count(page)))
    toggle = page.locator('[data-testid="inbox-show-hidden"]')
    check("'Show hidden (1)' appears", toggle.count() == 1 and toggle.inner_text() == "Show hidden (1)",
          toggle.inner_text() if toggle.count() else "none")
    toggle.click()
    until(lambda: "pr:Johnwang688/jarvis#301" in _row_ids(page), timeout=3)
    hidden = _row(page, "pr:Johnwang688/jarvis#301")
    check("shown hidden, it is muted and says 'Dismissed', with Undo and no Dismiss",
          hidden.get_attribute("data-hidden") == "1"
          and hidden.locator('[data-testid="inbox-hidden-note"]').inner_text() == "Dismissed"
          and hidden.locator('[data-testid="inbox-mark-undo"]').count() == 1
          and hidden.locator('[data-testid="inbox-dismiss"]').count() == 0)
    check("shown, it is still not counted", _count(page) == "4", str(_count(page)))
    border = hidden.evaluate("e => getComputedStyle(e).borderLeftColor")
    check("Graphite: a hidden row is neither amber, red nor the selection cyan",
          border not in (AMBER, RED, "rgb(111, 195, 223)"), border)
    hidden.locator('[data-testid="inbox-mark-undo"]').click()
    until(lambda: len(_mark_posts(mock)) == 2, timeout=3)
    check("Undo posts {clear: true}", _mark_posts(mock)[-1][1:] == ("pr:Johnwang688/jarvis#301", {"clear": True}),
          str(_mark_posts(mock)[-1:]))
    until(lambda: page.locator('[data-testid="inbox-show-hidden"]').count() == 0, timeout=3)
    check("it is back, counted, and the toggle is gone", _count(page) == "5"
          and _row(page, "pr:Johnwang688/jarvis#301").get_attribute("data-hidden") is None, str(_count(page)))
    # A snooze posts its time about an hour ahead (the browser's clock).
    _row(page, "dec:d0123456789ab").locator('[data-testid="inbox-snooze-hour"]').click()
    until(lambda: len(_mark_posts(mock)) == 3, timeout=3)
    body = _mark_posts(mock)[-1][2]
    ahead = (datetime.fromisoformat(body.get("snooze_until", "").replace("Z", "+00:00"))
             - datetime.now(timezone.utc)).total_seconds() if body.get("snooze_until") else -1
    check("Snooze 1 h posts {snooze_until} about an hour ahead, with its zone",
          set(body) == {"snooze_until"} and 3500 < ahead <= 3600 and body["snooze_until"].endswith("Z"), str(body))
    until(lambda: "dec:d0123456789ab" not in _row_ids(page) or
          _row(page, "dec:d0123456789ab").get_attribute("data-hidden") == "1", timeout=3)
    # Under a card the marks are inert.
    c = card("markcard001")
    mock.world["approvals"].append(c)
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    row = _row(page, "task:kf")
    check("under a card the marks are inert and disabled",
          row.evaluate("e => !!e.closest('[inert]')")
          and row.locator('[data-testid="inbox-dismiss"]').is_disabled())
    before = len(_mark_posts(mock))
    for sel in ('[data-testid="inbox-dismiss"]', '[data-testid="inbox-snooze-week"]'):
        found = row.locator(sel).first.bounding_box()
        if found:
            page.mouse.click(found["x"] + found["width"] / 2, found["y"] + found["height"] / 2)
    time.sleep(0.4)
    check("a trusted click under the card marks nothing [mutation: not inert]",
          len(_mark_posts(mock)) == before and cardloc.count() == 1, f"{len(_mark_posts(mock)) - before} posts")
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    check("the title is unchanged", page.title() == TITLE, page.title())
    _st(mock)["marks"] = {}


def _check_checks(page, mock, check, until):
    """Inbox B2 (IN-6): live check rows, rank 7, marked at /inbox/checks/<id>/steps/<n>."""
    hostile = chk_row(title="Review " + HOSTILE, origin="Claude · chat ‹" + HOSTILE + "› (t1)",
                      steps=[{"do": "Run " + HOSTILE, "expect": "It shows " + HOSTILE},
                             {"do": "rm -rf ~/x", "expect": "Nothing happens."}])
    gated = chk_row("c00000000000d", prs=(301,), markable=False,
                    deploy_text="Not confirmed deployed: no merge commit for #301 in this checkout.")
    _st(mock)["marked"] = []
    _open(page, mock, until, basic_rows() + [hostile, gated])
    page.evaluate("""() => { window.__copied = [];
      Object.defineProperty(navigator, 'clipboard', { configurable: true,
        value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } } }); }""")
    row = _row(page, "chk:c0123456789ab")
    time_ids = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"inbox-group-time\"] "
                             "[data-testid=\"inbox-row\"]')].map(r => r.dataset.id)")
    check("a check sits under 'When you have time'", "chk:c0123456789ab" in time_ids, str(time_ids))
    check("it is not counted: the count stays at ranks 1 to 5", _count(page) == "3", str(_count(page)))
    border = row.evaluate("e => getComputedStyle(e).borderLeftColor")
    check("Graphite: a check row is neither amber, red nor the selection cyan",
          border not in (AMBER, RED, "rgb(111, 195, 223)"), border)
    time.sleep(0.4)
    check("nothing is marked by showing it [mutation: auto-mark]", _marked(mock) == [], str(_marked(mock)))
    imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"inbox-view\"] img').length")
    check("its title, steps and origin are literal text: no element made, nothing ran [mutation: innerHTML]",
          imgs == 0 and page.evaluate("window.__pwned === undefined")
          and HOSTILE in row.locator('[data-testid="inbox-check-title"]').inner_text()
          and HOSTILE in row.locator('[data-testid="inbox-check-do"]').first.inner_text()
          and HOSTILE in row.locator('[data-testid="inbox-origin"]').inner_text(), str(imgs))
    steps = row.locator('[data-testid="inbox-check-step"]')
    steps.nth(1).locator('[data-testid="inbox-check-copy"]').click()
    until(lambda: page.evaluate("window.__copied.length") > 0, timeout=3)
    check("Copy puts the step's words on the clipboard and posts nothing [mutation: copy runs]",
          page.evaluate("window.__copied") == ["rm -rf ~/x"] and _marked(mock) == [],
          str(page.evaluate("window.__copied")))
    steps.nth(0).locator('[data-testid="inbox-check-note"]').fill("looked right")
    steps.nth(0).locator('[data-testid="inbox-check-note"]').press("Enter")
    time.sleep(0.3)
    check("Enter in a note marks nothing", _marked(mock) == [], str(_marked(mock)))
    steps.nth(0).locator('[data-testid="inbox-check-pass"]').dblclick()
    until(lambda: _marked(mock), timeout=3)
    time.sleep(0.4)
    got = _marked(mock)
    check("Pass posts {verdict, note} once, to /inbox/checks/<id>/steps/0 [mutation: double post]",
          len(got) == 1 and got[0][0] == "/inbox/checks/c0123456789ab/steps/0"
          and got[0][3] == {"verdict": "pass", "note": "looked right"}, str(got))
    # The gated check: its line shows, its marks are off.
    grow = _row(page, "chk:c00000000000d")
    check("not confirmed deployed: the daemon's line shows and every mark is off [mutation: mark while undeployed]",
          "Not confirmed deployed" in grow.locator('[data-testid="inbox-check-deploy"]').inner_text()
          and all(grow.locator(f'[data-testid="inbox-check-{v}"]').first.is_disabled() for v in ("pass", "fail", "skip")))
    # The second step done: the row moves to the receipt band.
    row = _row(page, "chk:c0123456789ab")
    row.locator('[data-testid="inbox-check-step"]').nth(1).locator('[data-testid="inbox-check-fail"]').click()
    until(lambda: page.evaluate("() => !!document.querySelector('[data-testid=\"inbox-group-fyi\"] "
                                "[data-id=\"chk:c0123456789ab\"]')"), timeout=3)
    row = _row(page, "chk:c0123456789ab")
    verdicts = row.evaluate("e => [...e.querySelectorAll('[data-testid=\"inbox-check-step\"]')].map(s => s.dataset.verdict)")
    check("done: under 'For your information' with its verdicts and no marks left",
          verdicts == ["pass", "fail"] and row.locator('[data-testid="inbox-check-pass"]').count() == 0, str(verdicts))
    row.locator('[data-testid="inbox-check-receipt"]').click()
    until(lambda: page.evaluate("window.__copied.length") > 1, timeout=3)
    receipt = page.evaluate("window.__copied[1]")
    check("the receipt copies as Markdown, with one heading and the verdicts",
          receipt.startswith("## Live check c0123456789ab:") and "1. **PASS**" in receipt and "2. **FAIL**" in receipt
          and sum(1 for line in receipt.splitlines() if line.startswith("#")) == 1, receipt[:200])
    # The import: drafts are read, only Save stores one.
    _st(mock)["imports"] = []
    _st(mock)["import_reply"] = {"path": "/p/docs/plans/x.md", "digest": "d1", "drafts": [
        {"index": 0, "title": "First (#5)", "prs": [5], "steps": [{"do": "a", "expect": "b"}], "savable": True,
         "reason": None},
        {"index": 1, "title": "VEX view", "prs": [], "steps": [], "savable": False,
         "reason": "it has no numbered steps"}]}
    page.locator('[data-testid="inbox-import-toggle"]').click()
    page.locator('[data-testid="inbox-import-path"]').fill("/p/docs/plans/x.md")
    page.locator('[data-testid="inbox-import-read"]').click()
    until(lambda: page.locator('[data-testid="inbox-import-draft"]').count() == 2, timeout=3)
    check("the import reads drafts and saves none by itself",
          _st(mock)["imports"] == [{"path": "/p/docs/plans/x.md"}], str(_st(mock)["imports"]))
    page.locator('[data-testid="inbox-import-save"]').click()
    until(lambda: len(_st(mock)["imports"]) > 1, timeout=3)
    check("Save confirms one draft with the file's digest",
          _st(mock)["imports"][-1] == {"path": "/p/docs/plans/x.md", "digest": "d1", "save": 0},
          str(_st(mock)["imports"][-1]))
    # Under a card the row is inert.
    _st(mock)["rows"].append(chk_row("c00000000000e"))
    mock_inbox.changed(mock, ["chk:c00000000000e"], "check_posted")
    until(lambda: "chk:c00000000000e" in _row_ids(page), timeout=3)
    c = card("chkcard0001")
    mock.world["approvals"].append(c)
    mock.emit("approval_requested", c)
    cardloc = page.locator('[data-testid="approval-card"]')
    until(lambda: cardloc.count() == 1, timeout=4)
    row = _row(page, "chk:c00000000000e")
    check("under a card the check row is inert and its marks disabled",
          row.evaluate("e => !!e.closest('[inert]')") and row.locator('[data-testid="inbox-check-pass"]').first.is_disabled())
    posts = len(_marked(mock))
    for sel in ('[data-testid="inbox-check-pass"]', '[data-testid="inbox-check-copy"]',
                '[data-testid="inbox-check-note"]'):
        found = row.locator(sel).first.bounding_box()
        if found:
            page.mouse.click(found["x"] + found["width"] / 2, found["y"] + found["height"] / 2)
    page.keyboard.type("x")
    page.keyboard.press("Enter")
    time.sleep(0.4)
    check("a trusted click, typing and Enter under the card mark and copy nothing [mutation: not inert]",
          len(_marked(mock)) == posts and page.evaluate("window.__copied.length") == 2 and cardloc.count() == 1,
          f"{len(_marked(mock)) - posts} posts")
    page.keyboard.press("Escape")
    until(lambda: cardloc.count() == 0, timeout=4)
    page.locator('[data-testid="inbox-import-toggle"]').click()
    check("the title is unchanged", page.title() == TITLE, page.title())


# ---------------------------------------------------------------------------

SECTIONS = (_sidebar_checks, _groups_checks, _live_checks, _answer_checks, _conflict_checks, _withdraw_checks,
            _reconnect_checks, _clicks_checks, _hostile_checks, _missed_checks, _decision_checks, _check_checks,
            _marks_checks, _deferred_checks, _summary_checks, _card_checks,
            _older_checks, _aurora_checks, _pr_checks)


def inbox_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe owner inbox (inbox A2)")
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        before = mock.sse_connections()
        _reset(mock)
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        for section in SECTIONS:
            if only and section.__name__.strip("_").removesuffix("_checks") not in only:
                continue
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"inbox {section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the inbox section", not errors, "; ".join(errors[:3]))
        check("the owner test refused nothing the HUD's own page asked", _st(mock)["refused"] == 0,
              str(_st(mock)["refused"]))
    finally:
        ctx.close()
    if not only or "badge" in only:
        try:
            _badge_checks(browser, mock, check, until, guard, init_script, base)
        except Exception as e:
            check("inbox badge ran to the end", False, str(e).splitlines()[0][:200])


def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            inbox_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard,
                         H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} inbox checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
