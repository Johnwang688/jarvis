"""C5 of the chat-to-chat plan: the HUD for chats that message each other.

Plan `docs/plans/2026-10-19-chat-to-chat-plan.md` §7 and §8 C5; the decisions
file beside it wins. C3's merged daemon code wins over §7 where they differ:
the mock (`tests/face/hud_v2_mock.py`) holds relay *log records* in C3's
shape and folds them with the daemon's own `hud_api._relay_message`, and it
publishes them on the bus as `ChatRelay._record` does.

Free and hermetic: its own `MockDaemon(0)` (an ephemeral port, never
8402/8403/8405; `guard` refuses the live ports), every context under the CSP
watch, the Withdraw route owner-only in the mock as in the daemon.

What it pins (each a named check; on today's code every one fails because the
feature is missing):
  1. both panes render the relay rows with their test ids: the sender's
     `msg-relay-out` and `msg-relay-reply`, the target's `msg-relay-in`; the
     heads read "↪ SENT TO ‹title›", "↩ REPLY FROM <badge> · ‹title›",
     "FROM <badge> · ‹title›"; none is "YOU" or wears the owner's `user` class;
     the receiving body is plain text; "↩ reply sent to ‹title›" after the turn;
     the reply's footer says its delivery in words;
  2. Graphite: the queued chip's computed colour is `--amber`, answered
     `--green`, working `--accent`, not delivered `--text-muted`; nothing else
     in a relay row is amber or red;
  3. a `<script>` (and an `onerror` image) in a reply stays inert, while its
     Markdown still renders;
  4. a hostile peer title is one clean line of text (no element, no bidi, no
     newline) [mutation: raw title];
  5. live: progress, a new send, a reply and its delivery land on the bus;
  6. an arriving relay steals no focus or selection: `document.activeElement`,
     the box's words, the selected chat, every pane's thread and the pane
     layout are unchanged, no frame has focus, and a thread no pane shows is
     not opened [mutation: auto-select];
  7. "Open" on a received row opens the sending chat beside this one without
     focusing a frame — asserted by outcome (that chat is in a pane, this one
     still is), never by a layout shape (FS4 changes the split ladder);
  8. Withdraw is disabled under an approval card and a click there sends
     nothing; once the card is answered it calls the owner-only route, once,
     and the chip reads "not delivered · the owner withdrew it";
  9. a reload restores every row with its state;
 10. after a restart (the live service no longer knows a queued message), it
     reads "not delivered";
 11. the queued indicator in the target (`relay-waiting`, "1 message from
     ‹title› waits for this turn", then "2 messages from other chats wait for
     this turn") and the sidebar's amber `↪N` badge (`relays-waiting-<id>`,
     titled "N messages from other chats wait for this chat"), both gone when
     nothing waits.


THE INTERFACE THE IMPLEMENTER BUILDS (taken from the plan; "chosen" where it
is ambiguous; see also the headers of hud/src/lib/relays.test.ts and
hud/src/components/RelayRow.test.ts)
=============================================================================
  Rows: `[data-testid="msg-relay-in|msg-relay-out|msg-relay-reply"]` with
    `data-message-id` and `data-state`; head `.who`; provider badge
    `.badge.<fast|claude|codex|unknown>`; state chip (out rows)
    `[data-testid="relay-state"].relay-state.<pending|working|done|muted|error>`;
    `[data-testid="relay-progress"]`, `[data-testid="relay-withdraw"]`,
    `[data-testid="relay-open"]`, `[data-testid="relay-answered"]`,
    `[data-testid="relay-delivery"]`.
  ChatTab: `[data-testid="relay-waiting"]`, a muted line under the thinking
    line while `thread.relays_waiting` > 0: with one waiting it names the
    sender when the window heard its `relay_out` (chosen: the sender's title
    from the thread list by the record's `thread_id`, since C3's relay_out
    data names only the target), else "another chat".
  Sidebar: `[data-testid="relays-waiting-<thread id>"]`, class `badge relay`,
    text "↪N", colour `--amber`, `title` = waitingTitle(N).
  App: the SSE switch hands relay_out, relay_in, relay_state, relay_progress,
    relay_reply and relay_delivered to the store's `relay` action (by the
    record's thread, whichever panes show it), and re-reads the thread list
    (or patches the thread) on relay_out, relay_in and relay_state so
    `relays_waiting`/`relays_out` follow (chosen: the mock updates its thread
    records before it publishes). "Open" is `pickThread(id, true)` on a click
    only; Withdraw is `api.withdrawRelay(thread, message_id)`; both are
    disabled under a card. Nothing here selects, opens or focuses a pane.

Plan vs code (C3 wins): the transcript row has no `position`, so the queued
chip reads "queued" (no "· 2nd") unless a position is known; `relay_delivered`
is a sixth bus kind; rows carry `reason`; relay_out's data names the target
only.

Run alone (after `cd hud && npm run build`):
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env PYTHONPATH=$PWD \\
    PYTHONDONTWRITEBYTECODE=1 /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_relay_check.py
It also runs as a section of tests/face/hud_v2_check.py.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face.hud_v2_layout_check import _pane  # noqa: E402
from tests.face.hud_v2_multichat_check import _box, _chat, _chats, _expand, _fresh  # noqa: E402

TWO = json.dumps({"preset": "cols2", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})

SENDER, TARGET, OTHER, QUIET = "t1", "rc", "rx", "rq"
HOSTILE = '<img src=x onerror="window.__pwned=1">\u202e\u200bevil\nSECOND LINE ' + "z" * 100
REPLY = ("**Two failures** in `App.test.ts`.\n\n<script>window.__pwned = 1</script>"
         '<img src=x onerror="window.__pwned=1">')
ASK = "Please run the frontend tests and tell me what fails."


def at(s: int) -> str:
    return f"2026-10-19T09:{s // 60:02d}:{s % 60:02d}+00:00"


def _mark(mid, sender):
    return {"message_id": mid, "from": sender}


def seed(mock):
    """t1 (Jarvis, "desk chat") has sent to rc (Claude, "Frontend") and rx
    (Codex, "Backend"); rq (Jarvis, "Quiet chat") is a chat no pane shows."""
    from tests.face.hud_v2_mock import relay_record
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == SENDER)
    for tid, provider, title in ((TARGET, "claude", "Frontend"), (OTHER, "codex", "Backend"),
                                 (QUIET, "fast", "Quiet chat")):
        if not any(t["id"] == tid for t in w["threads"]):
            w["threads"].append({**base, "id": tid, "provider": provider, "title": title, "model": None,
                                 "updated": at(1)})
    out = lambda mid, to, title, provider, text, turn, s: relay_record(  # noqa: E731
        "relay_out", SENDER, "p1", {"message_id": mid, "to": to, "title": title, "provider": provider,
                                    "text": text, "chars": len(text)}, turn, None, at(s))
    w["transcripts"][SENDER] = [
        {"role": "user", "text": "ask the other chats", "at": at(0), "turn_id": "ts1"},
        out("m1", TARGET, "Frontend", "claude", ASK, "ts1", 1),
        out("m2", OTHER, HOSTILE, "codex", "Check the backend build.", "ts1", 2),
        {"role": "assistant", "text": "Sent both.", "at": at(3), "turn_id": "ts1"},
        relay_record("relay_reply", SENDER, "p1", {"message_id": "m1", "from": TARGET, "title": "Frontend",
                                                   "provider": "claude", "status": "answered", "text": REPLY},
                     None, None, at(20)),
        relay_record("relay_delivered", SENDER, "p1", {"message_id": "m1", "delivery": "turn"}, "ts2", _mark("m1", TARGET) | {"reply": True}, at(21)),
        {"role": "user", "text": "and two more", "at": at(30), "turn_id": "ts3"},
        out("m3", OTHER, "Backend", "codex", "A queued question for the backend.", "ts3", 31),
        out("m4", TARGET, "Frontend", "claude", "A message Jarvis restarted under.", "ts3", 32),
        out("m5", OTHER, "Backend", "codex", "Another queued question.", "ts3", 33),
    ]
    w["transcripts"][TARGET] = [
        relay_record("relay_in", TARGET, "p1", {"message_id": "m1", "from": SENDER, "title": "desk chat",
                                                "provider": "fast", "text": ASK, "chars": len(ASK)},
                     "tr1", _mark("m1", SENDER), at(5)),
        {"role": "assistant", "text": "Two failures in App.test.ts.", "at": at(19), "turn_id": "tr1"},
        relay_record("relay_state", TARGET, "p1", {"message_id": "m1", "state": "answered"}, "tr1",
                     _mark("m1", SENDER), at(20)),
    ]
    w["transcripts"][OTHER] = [{"role": "user", "text": "backend work", "at": at(0)}]
    w["transcripts"][QUIET] = [{"role": "user", "text": "quiet", "at": at(0)}]
    r = mock.relays()
    r["live"].clear()
    r["live"].update({
        "m1": {"state": "answered", "status": "answered", "delivery": "turn", "reason": None,
               "sender": SENDER, "target": TARGET},
        "m2": {"state": "running", "status": None, "delivery": None, "reason": None,
               "sender": SENDER, "target": OTHER},
        "m3": {"state": "queued", "status": None, "delivery": None, "reason": None,
               "sender": SENDER, "target": OTHER},
        # m4: none — it was queued when Jarvis restarted.
        "m5": {"state": "queued", "status": None, "delivery": None, "reason": None,
               "sender": SENDER, "target": OTHER},
    })
    mock.relay_counts()


# ---------------------------------------------------------------------------

def relay_checks(browser, mock, base, check, until, guard, init_script):
    print("\nchats that message each other (chat-to-chat C5)")
    seed(mock)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    try:
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        for section in (_render_checks, _colour_checks, _inert_checks, _hostile_title_checks, _live_checks,
                        _arrival_checks, _open_beside_checks, _withdraw_checks, _reload_checks,
                        _restart_checks, _waiting_checks):
            if only and section.__name__.strip("_") not in only:
                continue
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"relay {section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the relay section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# -- helpers -------------------------------------------------------------------

def _open_thread(page, until, n, tid):
    _box(page, n).click()
    _expand(page, until, "p1", f"thread-{tid}")
    page.locator(f'[data-testid="thread-{tid}"]').click()
    until(lambda: _chat(page, n)["threadId"] == tid, timeout=3)
    until(lambda: page.locator(_pane(n, '[data-testid="log"]')).inner_text() != "", timeout=3)


def _two(page, mock, until, left=SENDER, right=TARGET):
    _fresh(page, mock, until, TWO)
    _open_thread(page, until, 1, left)
    _open_thread(page, until, 2, right)


def _row(page, n, dir_, mid):
    return page.locator(_pane(n, f'[data-testid="msg-relay-{dir_}"][data-message-id="{mid}"]'))


def _chip(page, n, mid):
    return _row(page, n, "out", mid).locator('[data-testid="relay-state"]')


def _text(loc) -> str:
    return loc.first.inner_text().strip() if loc.count() else ""


def _content(loc) -> str:
    """The DOM's text (a badge's CSS upper-cases what inner_text reads)."""
    return (loc.first.text_content() or "").strip() if loc.count() else ""


def _colour(loc) -> str:
    return loc.first.evaluate("e => getComputedStyle(e).color") if loc.count() else ""


def _var(page, name) -> str:
    """A theme variable as a computed colour."""
    return page.evaluate("""(name) => { const s = document.createElement('span');
      s.style.color = `var(${name})`; document.body.append(s);
      const c = getComputedStyle(s).color; s.remove(); return c; }""", name)


def _rows(page, n) -> list:
    """Every relay row a pane draws, as (dir, message id, state, chip words)."""
    return page.evaluate("""(n) => [...document.querySelectorAll(`[data-testid="pane-${n}"] [data-testid^="msg-relay-"]`)]
      .map(e => [e.dataset.testid.slice(10), e.dataset.messageId, e.dataset.state,
                 (e.querySelector('[data-testid="relay-state"]') || {}).textContent || ''])""", n)


def _layout(page) -> list:
    return page.evaluate("""() => [...document.querySelectorAll('[data-testid^="pane-"][data-view]')]
      .map(e => `${e.dataset.testid}:${e.dataset.view}`).sort()""")


def _shown(page) -> set:
    return {c.get("threadId") for c in _chats(page).values() if c.get("threadId")}


# -- 1. both panes render ------------------------------------------------------

def _render_checks(page, mock, check, until):
    _two(page, mock, until)
    until(lambda: _row(page, 1, "out", "m1").count() == 1 and _row(page, 2, "in", "m1").count() == 1, timeout=3)
    check("the sender's pane draws its sends as msg-relay-out rows",
          all(_row(page, 1, "out", m).count() == 1 for m in ("m1", "m2", "m3", "m4", "m5")), str(_rows(page, 1)))
    check("the sender's pane draws the reply as a msg-relay-reply row", _row(page, 1, "reply", "m1").count() == 1)
    check("the target's pane draws the message as a msg-relay-in row", _row(page, 2, "in", "m1").count() == 1)
    check("the sending head reads ↪ SENT TO ‹title›",
          _text(_row(page, 1, "out", "m1").locator(".who")).startswith("↪ SENT TO Frontend"),
          _text(_row(page, 1, "out", "m1").locator(".who")))
    check("the reply's head reads ↩ REPLY FROM <badge> · ‹title›",
          _content(_row(page, 1, "reply", "m1").locator(".who")) == "↩ REPLY FROM Claude · Frontend"
          and _row(page, 1, "reply", "m1").locator(".who .badge.claude").count() == 1,
          _content(_row(page, 1, "reply", "m1").locator(".who")))
    check("the receiving head reads FROM <badge> · ‹title›",
          _content(_row(page, 2, "in", "m1").locator(".who")) == "FROM Jarvis · desk chat"
          and _row(page, 2, "in", "m1").locator(".who .badge.fast").count() == 1,
          _content(_row(page, 2, "in", "m1").locator(".who")))
    owners = page.evaluate("""() => [...document.querySelectorAll('[data-testid^="msg-relay-"]')]
      .filter(e => e.classList.contains('user') || /^YOU\\b/.test((e.querySelector('.who') || {}).textContent || '')).length""")
    relays = page.locator('[data-testid^="msg-relay-"]').count()
    check("no relay row is the owner's: never 'YOU', never the user class", relays >= 7 and owners == 0,
          f"{relays} rows, {owners} as the owner's")
    body = _row(page, 2, "in", "m1").locator(".body.user-text")
    check("the received message is plain text, not Markdown", _text(body) == ASK
          and _row(page, 2, "in", "m1").locator("strong, em, code, a").count() == 0)
    check("after its turn, the target shows '↩ reply sent to ‹title›'",
          _text(_row(page, 2, "in", "m1").locator('[data-testid="relay-answered"]')) == "↩ reply sent to desk chat")
    check("the reply's footer says its delivery in words",
          _text(_row(page, 1, "reply", "m1").locator('[data-testid="relay-delivery"]')) == "started a turn to report it")
    check("the chips read their states (C3 serves no position: plain 'queued')",
          [_text(_chip(page, 1, m)) for m in ("m1", "m2", "m3", "m4")]
          == ["answered", "working", "queued", "not delivered"],
          str([_text(_chip(page, 1, m)) for m in ("m1", "m2", "m3", "m4")]))
    check("Withdraw is offered on queued sends only",
          [_row(page, 1, "out", m).locator('[data-testid="relay-withdraw"]').count() for m in ("m1", "m2", "m3", "m4")]
          == [0, 0, 1, 0])
    check("Open is offered on the received row", _row(page, 2, "in", "m1").locator('[data-testid="relay-open"]').count() == 1)


# -- 2. Graphite ---------------------------------------------------------------

def _colour_checks(page, mock, check, until):
    until(lambda: _chip(page, 1, "m3").count() == 1, timeout=3)
    amber, green, accent, muted, red = (_var(page, v) for v in ("--amber", "--green", "--accent", "--text-muted", "--red"))
    check("the queued chip's computed colour is --amber", amber != "" and _colour(_chip(page, 1, "m3")) == amber,
          f"{_colour(_chip(page, 1, 'm3'))} vs {amber}")
    check("the answered chip's computed colour is --green", _colour(_chip(page, 1, "m1")) == green,
          f"{_colour(_chip(page, 1, 'm1'))} vs {green}")
    check("the working chip is --accent and not delivered is --text-muted",
          _colour(_chip(page, 1, "m2")) == accent and _colour(_chip(page, 1, "m4")) == muted,
          f"{_colour(_chip(page, 1, 'm2'))}, {_colour(_chip(page, 1, 'm4'))}")
    rgb = lambda c: c[c.find("(") + 1:].split(",")[:3]  # noqa: E731
    bad = page.evaluate("""([amber, red]) => {
      const hit = (c) => { const m = (c || '').match(/\\d+/g); if (!m) return false;
        const t = m.slice(0, 3).join(','); return t === amber || t === red; };
      const out = [];
      for (const row of document.querySelectorAll('[data-testid^="msg-relay-"]'))
        for (const e of [row, ...row.querySelectorAll('*')]) {
          if (e.matches('[data-testid="relay-state"].pending, [data-testid="relay-state"].error')) continue;
          const s = getComputedStyle(e);
          for (const c of [s.color, s.borderLeftColor, s.borderTopColor, s.backgroundColor, s.outlineColor])
            if (hit(c)) { out.push(`${row.dataset.testid}>${e.tagName}.${e.className}`); break; }
        }
      return out; }""", [",".join(x.strip() for x in rgb(amber)), ",".join(x.strip() for x in rgb(red))])
    rows = page.locator('[data-testid^="msg-relay-"]').count()
    check("nothing else in a relay row is amber or red", rows > 0 and not bad, f"{rows} rows; {bad[:4]}")


# -- 3. inert ------------------------------------------------------------------

def _inert_checks(page, mock, check, until):
    reply = _row(page, 1, "reply", "m1")
    until(lambda: reply.locator("strong").count() == 1, timeout=3)
    check("a reply's Markdown renders through the sanitiser", _text(reply.locator("strong")) == "Two failures")
    check("a <script> in a reply stays inert: no script, no onerror image, nothing ran",
          reply.count() == 1 and reply.locator("script").count() == 0 and reply.locator("img[onerror]").count() == 0
          and not page.evaluate("!!window.__pwned"))


# -- 4. hostile title ----------------------------------------------------------

def _hostile_title_checks(page, mock, check, until):
    who = _row(page, 1, "out", "m2").locator(".who")
    until(lambda: who.count() == 1, timeout=3)
    text = _text(who)
    check("a hostile peer title is cleaned text: no element, no bidi or zero-width, one line, capped",
          who.count() == 1 and who.locator("img").count() == 0 and "<img" in text
          and "\u202e" not in text and "\u200b" not in text and "\n" not in text
          and "evil SECOND LINE" in text and "z" * 60 not in text and not page.evaluate("!!window.__pwned"),
          repr(text[:120]))


# -- 5. live -------------------------------------------------------------------

def _live_checks(page, mock, check, until):
    mock.relay("relay_progress", SENDER, {"message_id": "m2", "text": "Building the <b>backend</b> now"}, at=at(40))
    progress = _row(page, 1, "out", "m2").locator('[data-testid="relay-progress"]')
    until(lambda: progress.count() == 1, timeout=3)
    check("relay_progress shows the excerpt, plain and muted, under a working chip",
          _text(progress) == "Building the <b>backend</b> now" and progress.locator("b").count() == 0
          and _text(_chip(page, 1, "m2")) == "working")
    r = mock.relays()
    r["live"]["m6"] = {"state": "queued", "status": None, "delivery": None, "reason": None,
                       "sender": SENDER, "target": TARGET}
    mock.relay_counts()
    mock.relay("relay_out", SENDER, {"message_id": "m6", "to": TARGET, "title": "Frontend", "provider": "claude",
                                     "text": "A live send.", "chars": 12}, turn_id="ts6", at=at(41))
    until(lambda: _row(page, 1, "out", "m6").count() == 1, timeout=3)
    check("a relay_out on the bus adds a queued out row in the sender's pane",
          _row(page, 1, "out", "m6").count() == 1 and _text(_chip(page, 1, "m6")) == "queued")
    r["live"]["m6"].update(state="running")
    mock.relay_counts()
    mock.relay("relay_in", TARGET, {"message_id": "m6", "from": SENDER, "title": "desk chat", "provider": "fast",
                                    "text": "A live send.", "chars": 12}, turn_id="tr6",
               relayed=_mark("m6", SENDER), at=at(42))
    until(lambda: _row(page, 2, "in", "m6").count() == 1, timeout=3)
    check("its relay_in adds a received row in the target's pane", _row(page, 2, "in", "m6").count() == 1)
    r["live"]["m6"].update(state="answered", status="answered")
    mock.relay("relay_state", TARGET, {"message_id": "m6", "state": "answered"}, turn_id="tr6",
               relayed=_mark("m6", SENDER), at=at(50))
    mock.relay("relay_reply", SENDER, {"message_id": "m6", "from": TARGET, "title": "Frontend", "provider": "claude",
                                       "status": "answered", "text": "Live *answer*."}, at=at(50))
    until(lambda: _row(page, 1, "reply", "m6").count() == 1, timeout=3)
    check("relay_reply adds the reply and turns the send's chip answered",
          _row(page, 1, "reply", "m6").count() == 1 and _text(_chip(page, 1, "m6")) == "answered"
          and _text(_row(page, 2, "in", "m6").locator('[data-testid="relay-answered"]')) == "↩ reply sent to desk chat")
    r["live"]["m6"].update(delivery="step")
    mock.relay("relay_delivered", SENDER, {"message_id": "m6", "delivery": "step", "unconfirmed": False}, at=at(51))
    footer = _row(page, 1, "reply", "m6").locator('[data-testid="relay-delivery"]')
    until(lambda: _text(footer) == "passed to Jarvis at its next step", timeout=3)
    check("relay_delivered puts the delivery in words", _text(footer) == "passed to Jarvis at its next step",
          _text(footer))


# -- 6. an arrival steals nothing [mutation: auto-select] --------------------------

def _snapshot(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement;
      const pane = a && a.closest && a.closest('[data-testid^="pane-"]');
      const s = window.__hud.state();
      return { active: a ? (a.getAttribute('data-testid') || a.tagName) : null,
               pane: pane ? pane.dataset.testid : null, tag: a ? a.tagName : null,
               value: a && 'value' in a ? a.value : null, selected: s.selectedChat,
               threads: Object.fromEntries(Object.entries(s.chats).map(([n, c]) => [n, c.threadId])) }; }""")


def _arrival_checks(page, mock, check, until):
    _two(page, mock, until)
    _box(page, 1).click()
    page.keyboard.type("half a thought")
    time.sleep(0.2)
    before, layout = _snapshot(page), _layout(page)
    r = mock.relays()
    # A relayed turn starting and running in pane 2's thread (not selected)…
    r["live"]["m8"] = {"state": "running", "status": None, "delivery": None, "reason": None,
                       "sender": OTHER, "target": TARGET}
    mock.relay_counts()
    mock.relay("relay_in", TARGET, {"message_id": "m8", "from": OTHER, "title": "Backend", "provider": "codex",
                                    "text": "Look at this, please.", "chars": 21}, turn_id="tr8",
               relayed=_mark("m8", OTHER), at=at(60))
    mock.emit("turn_started", {}, thread_id=TARGET, project_id="p1", turn_id="tr8", at=at(60),
              relayed=_mark("m8", OTHER))
    mock.emit("text", {"text": "Looking now."}, thread_id=TARGET, project_id="p1", turn_id="tr8", at=at(61),
              relayed=_mark("m8", OTHER))
    # …a relay between two chats no pane shows…
    r["live"]["m11"] = {"state": "running", "status": None, "delivery": None, "reason": None,
                        "sender": QUIET, "target": OTHER}
    mock.relay("relay_out", QUIET, {"message_id": "m11", "to": OTHER, "title": "Backend", "provider": "codex",
                                    "text": "x", "chars": 1}, turn_id="tq1", at=at(62))
    mock.relay("relay_in", OTHER, {"message_id": "m11", "from": QUIET, "title": "Quiet chat", "provider": "fast",
                                   "text": "x", "chars": 1}, turn_id="tx1", relayed=_mark("m11", QUIET), at=at(62))
    # …and a reply arriving in the selected pane's own thread.
    r["live"]["m12"] = {"state": "answered", "status": "answered", "delivery": None, "reason": None,
                        "sender": SENDER, "target": QUIET}
    mock.relay("relay_out", SENDER, {"message_id": "m12", "to": QUIET, "title": "Quiet chat", "provider": "fast",
                                     "text": "y", "chars": 1}, turn_id="ts12", at=at(63))
    mock.relay("relay_reply", SENDER, {"message_id": "m12", "from": QUIET, "title": "Quiet chat", "provider": "fast",
                                       "status": "answered", "text": "z"}, at=at(64))
    mock.emit("turn_finished", {"stop": "end"}, thread_id=TARGET, project_id="p1", turn_id="tr8", at=at(65),
              relayed=_mark("m8", OTHER))
    arrived = until(lambda: _row(page, 2, "in", "m8").count() == 1 and _row(page, 1, "reply", "m12").count() == 1,
                    timeout=4)
    time.sleep(0.4)
    after, layout_after = _snapshot(page), _layout(page)
    check("the arriving relays were drawn in the panes showing their threads", bool(arrived))
    check("an arriving relay steals no focus or selection: the focused box, its words, the selected chat, "
          "every pane's thread and the layout are unchanged",
          bool(arrived) and after == before and layout_after == layout,
          f"before {before} {layout}; after {after} {layout_after}")
    check("no frame has focus, and a chat no pane showed was not opened",
          bool(arrived) and after.get("tag") != "IFRAME" and QUIET not in _shown(page) and OTHER not in _shown(page))
    page.locator(_pane(1, '[data-testid="input"]')).fill("")


# -- 7. Open beside ------------------------------------------------------------

def _open_beside_checks(page, mock, check, until):
    _two(page, mock, until, left=TARGET, right=OTHER)
    open_ = _row(page, 1, "in", "m1").locator('[data-testid="relay-open"]')
    until(lambda: open_.count() == 1, timeout=3)
    sends = len([c for c in mock.calls if c[0] == "POST" and c[1].endswith("/send")])
    check("before a click, the sending chat is in no pane", SENDER not in _shown(page))
    if open_.count():
        open_.click()
    until(lambda: SENDER in _shown(page), timeout=3)
    tag = page.evaluate("document.activeElement && document.activeElement.tagName")
    check("Open shows the sending chat in a pane beside this one, and this chat stays",
          SENDER in _shown(page) and TARGET in _shown(page), str(_shown(page)))
    check("Open focuses no frame and sends nothing",
          SENDER in _shown(page) and tag != "IFRAME"
          and len([c for c in mock.calls if c[0] == "POST" and c[1].endswith("/send")]) == sends, str(tag))


# -- 8. Withdraw ---------------------------------------------------------------

def _withdraws(mock, mid="m3") -> list:
    return [c for c in mock.calls if c[0] == "POST" and c[1] == f"/threads/{SENDER}/relays/{mid}/withdraw"]


def _withdraw_checks(page, mock, check, until):
    _two(page, mock, until)
    button = _row(page, 1, "out", "m3").locator('[data-testid="relay-withdraw"]')
    until(lambda: button.count() == 1, timeout=3)
    check("Withdraw is enabled while the send is queued and no card is up",
          button.count() == 1 and button.is_enabled())
    mock.emit("approval_requested", {
        "req_id": "relay-card", "code": "RL01", "tool": "run_command", "args": {"command": "ls"}, "command": "ls",
        "reason": "reviewer declined", "layer": "human", "origin": "task: relay", "allowlistable": True,
        "timeout_s": 120})
    card = page.locator('[data-testid="approval-card"]')
    until(lambda: card.count() == 1)
    until(lambda: button.count() == 1 and not button.is_enabled(), timeout=2)
    disabled = button.count() == 1 and not button.is_enabled()
    refused = mock.relays()["refused"]
    if button.count():
        button.evaluate("e => e.click()")       # a script's click behind the veil
    time.sleep(0.4)
    check("under a card Withdraw is disabled, and a click sends nothing",
          button.count() == 1 and disabled and not _withdraws(mock) and mock.relays()["refused"] == refused
          and not mock.relays()["withdrawn"])
    page.locator('[data-testid="approval-deny"]').click()
    until(lambda: card.count() == 0)
    until(lambda: button.count() == 1 and button.is_enabled(), timeout=2)
    if button.count():
        button.click()
    until(lambda: mock.relays()["withdrawn"] == [(SENDER, "m3")], timeout=3)
    check("once the card is answered, Withdraw calls the owner-only route once",
          mock.relays()["withdrawn"] == [(SENDER, "m3")] and len(_withdraws(mock)) == 1
          and mock.relays()["refused"] == refused, str(mock.relays()))
    until(lambda: _text(_chip(page, 1, "m3")).startswith("not delivered"), timeout=3)
    check("the withdrawn send reads 'not delivered · the owner withdrew it' and offers no Withdraw",
          _text(_chip(page, 1, "m3")) == "not delivered · the owner withdrew it"
          and _row(page, 1, "out", "m3").locator('[data-testid="relay-withdraw"]').count() == 0,
          _text(_chip(page, 1, "m3")))
    # The mock's gate is the daemon's rule, not a pass-through.
    codes = []
    for headers in ({}, {"Origin": "http://evil.example"}):
        req = urllib.request.Request(f"http://127.0.0.1:{mock.port}/threads/{SENDER}/relays/m5/withdraw",
                                     data=b"{}", method="POST", headers={"Content-Type": "application/json", **headers})
        try:
            urllib.request.urlopen(req, timeout=3)
            codes.append(200)
        except urllib.error.HTTPError as e:
            codes.append(e.code)
    check("the Withdraw route refuses anyone but the owner's window", codes == [403, 403]
          and mock.relays()["live"]["m5"]["state"] == "queued", str(codes))


# -- 9. reload, 10. restart ----------------------------------------------------

def _reload(page, mock, until):
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    for n, tid in ((1, SENDER), (2, TARGET)):
        if until(lambda: _chat(page, n)["threadId"] == tid, timeout=2) is not True:
            _open_thread(page, until, n, tid)


def _reload_checks(page, mock, check, until):
    _two(page, mock, until)
    until(lambda: _row(page, 1, "out", "m6").count() == 1, timeout=3)
    before = (_rows(page, 1), _rows(page, 2))
    _reload(page, mock, until)
    until(lambda: (_rows(page, 1), _rows(page, 2)) == before, timeout=4)
    after = (_rows(page, 1), _rows(page, 2))
    check("a reload restores every relay row with its state", bool(before[0]) and bool(before[1]) and after == before,
          f"{before} -> {after}")


def _restart_checks(page, mock, check, until):
    check("before a restart a queued send reads queued", _text(_chip(page, 1, "m5")) == "queued",
          _text(_chip(page, 1, "m5")))
    # Jarvis restarts: the service forgets m5, which never got a final state.
    mock.relays()["live"].pop("m5", None)
    mock.relay_counts()
    _reload(page, mock, until)
    until(lambda: _chip(page, 1, "m5").count() == 1, timeout=3)
    check("after a restart an unfated send reads 'not delivered', muted",
          _text(_chip(page, 1, "m5")) == "not delivered" and _colour(_chip(page, 1, "m5")) == _var(page, "--text-muted")
          and _text(_chip(page, 1, "m4")) == "not delivered"
          and _row(page, 1, "out", "m5").locator('[data-testid="relay-withdraw"]').count() == 0,
          f"{_text(_chip(page, 1, 'm5'))!r} {_text(_chip(page, 1, 'm4'))!r}")


# -- 11. the queued indicator and the sidebar badge --------------------------------

def _waiting_checks(page, mock, check, until):
    _two(page, mock, until)
    r = mock.relays()
    mock.thread_running(TARGET, at(70))
    mock.emit("turn_started", {}, thread_id=TARGET, project_id="p1", turn_id="tr9", at=at(70))
    r["live"]["m9"] = {"state": "queued", "status": None, "delivery": None, "reason": None,
                       "sender": SENDER, "target": TARGET}
    mock.relay_counts()
    mock.relay("relay_out", SENDER, {"message_id": "m9", "to": TARGET, "title": "Frontend", "provider": "claude",
                                     "text": "When you are free.", "chars": 18}, turn_id="ts9", at=at(71))
    line = page.locator(_pane(2, '[data-testid="relay-waiting"]'))
    until(lambda: _text(line) == "1 message from desk chat waits for this turn", timeout=3)
    check("the target's pane says one message from ‹title› waits for this turn",
          _text(line) == "1 message from desk chat waits for this turn", _text(line))
    check("the line is muted, and not in the transcript",
          line.count() == 1 and _colour(line) == _var(page, "--text-muted")
          and page.locator(_pane(2, '[data-testid="log"] [data-testid="relay-waiting"]')).count() == 0)
    _expand(page, until, "p1", f"thread-{TARGET}")
    badge = page.locator(f'[data-testid="relays-waiting-{TARGET}"]')
    until(lambda: _text(badge) == "↪1", timeout=3)
    check("the sidebar marks the chat ↪1 in amber, titled in words",
          _text(badge) == "↪1" and _colour(badge) == _var(page, "--amber")
          and badge.count() == 1 and badge.get_attribute("title") == "1 message from another chat waits for this chat",
          f"{_text(badge)!r} {badge.get_attribute('title') if badge.count() else None!r}")
    check("no other chat is marked", page.locator('[data-testid^="relays-waiting-"]').count() == 1)
    r["live"]["m10"] = {"state": "queued", "status": None, "delivery": None, "reason": None,
                        "sender": OTHER, "target": TARGET}
    mock.relay_counts()
    mock.relay("relay_out", OTHER, {"message_id": "m10", "to": TARGET, "title": "Frontend", "provider": "claude",
                                    "text": "Me too.", "chars": 7}, turn_id="tx10", at=at(72))
    until(lambda: _text(badge) == "↪2" and _text(line) == "2 messages from other chats wait for this turn", timeout=3)
    check("two waiting: '2 messages from other chats wait for this turn' and ↪2",
          _text(line) == "2 messages from other chats wait for this turn" and _text(badge) == "↪2"
          and badge.get_attribute("title") == "2 messages from other chats wait for this chat",
          f"{_text(line)!r} {_text(badge)!r}")
    for mid, sender in (("m9", SENDER), ("m10", OTHER)):
        r["live"][mid].update(state="dropped", reason='the owner stopped "Frontend"')
        mock.relay_counts()
        fate = {"message_id": mid, "state": "dropped", "reason": 'the owner stopped "Frontend"'}
        mock.relay("relay_state", sender, fate, at=at(73))
        mock.relay("relay_state", TARGET, fate, at=at(73))
    mock.thread_running(TARGET, None)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id=TARGET, project_id="p1", turn_id="tr9", at=at(74))
    until(lambda: badge.count() == 0 and line.count() == 0, timeout=3)
    check("with nothing waiting, the line and the badge go",
          badge.count() == 0 and line.count() == 0 and _text(_chip(page, 1, "m9")).startswith("not delivered"))


# ---------------------------------------------------------------------------

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
            relay_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard,
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
    print(f"all {H.CHECKS} relay checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
