"""Real Chromium AO-3 checks. No shell, live daemon, or Remote browser."""
from __future__ import annotations
import json
import os
import re
import threading
from tests.face.hud_v2_layout_check import _boot, _pane, _set_storage, _set_ws, _ws, _approval
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_autoopen_mock import DevFixture

PROMPT = '[data-testid="devserver-prompt"]'
FRAME = '[data-testid="browser-frame"]'


def panes(page):
    return _ws(page)["ws"]["panes"]


def tabs(page):
    return [t for pane in panes(page) for t in pane.get("browser", {}).get("tabs", [])]


def configure(page, mock, until, preset="single", views=("chat",), width=1600):
    page.set_viewport_size({"width": width, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": preset, "focused": 1,
        "panes": [{"view": view} for view in views]}))
    _boot(page, mock, None, until, reload=True)
    page.wait_for_timeout(150)


def _discovery_only(call):
    method, path, body = call
    if method == "GET" and path in ("/browser/profiles", "/browser/settings") and body == {}:
        return True
    # BR3b publishes Direct panes too. This must not hide Remote allocation,
    # navigation, tickets or a front report containing any Remote handle. Named
    # drive is a saved preference; Direct remains structurally non-drivable.
    if method != "POST" or path != "/browser/front" or not isinstance(body, dict):
        return False
    return (isinstance(body.get("panes"), list) and all(
        isinstance(pane, dict) and "ctx" in pane and pane["ctx"] is None
        and isinstance(pane.get("profile"), str) and re.fullmatch(r"p-[0-9a-f]{8}", pane["profile"])
        and type(pane.get("drive")) is bool and isinstance(pane.get("tabs"), list)
        and all(isinstance(tab, dict) and tab.get("mode") == "direct" and "tab_id" not in tab
                for tab in pane["tabs"]) for pane in body["panes"]))


def browser_calls(mock):
    return [call for call in mock.calls if call[1].startswith("/browser/") and not _discovery_only(call)]


def window_id(mock):
    return [body["window"] for method, path, body in mock.calls
            if method == "GET" and path == "/events"][-1]


def wait_placed(page, entry):
    page.wait_for_function("id => [...document.querySelectorAll('[data-browser-page]')].length > 0", entry.id)
    page.wait_for_timeout(100)


def autoopen_checks(browser, check, until, guard, init_script):
    print("\nautomatic Direct tabs: real claim registry, focus, cards and windows")
    direct = {"panes": [{"profile": "p-11111111", "ctx": None, "drive": False,
                         "tabs": [{"id": "abcdef", "mode": "direct", "url": "http://localhost:12345/"}]}]}
    enabled_direct=json.loads(json.dumps(direct));enabled_direct["panes"][0]["drive"]=True
    check("AO3 call audit admits exact read-only settings and Direct-only preference discovery",
          _discovery_only(("GET", "/browser/profiles", {}))
          and _discovery_only(("GET", "/browser/settings", {}))
          and _discovery_only(("POST", "/browser/front", direct))
          and _discovery_only(("POST", "/browser/front", enabled_direct)))
    unsafe_fronts = []
    for field, value in (("mode", "remote"), ("tab_id", "1234abcd")):
        changed = json.loads(json.dumps(direct)); changed["panes"][0]["tabs"][0][field] = value
        unsafe_fronts.append(("POST", "/browser/front", changed))
    for field, value in (("ctx", "1234abcd"), ("drive", "true"), ("profile", "private")):
        changed = json.loads(json.dumps(direct)); changed["panes"][0][field] = value
        unsafe_fronts.append(("POST", "/browser/front", changed))
    check("AO3 call audit never hides Remote front, navigation, tickets, allocation or profile writes",
          not any(_discovery_only(call) for call in unsafe_fronts + [
              ("POST", "/browser/tabs", {}),
              ("POST", "/browser/contexts", {}), ("POST", "/browser/tabs/1234abcd/ticket", {}),
              ("POST", "/browser/panes/1/tabs/abcdef/address", {"mode": "remote"}),
              ("POST", "/browser/profiles", {}), ("GET", "/browser/profiles", {"extra": True}),
              ("GET", "/browser/settings", {"extra": True}),
              ("PATCH", "/browser/settings", {"agents_may_drive": True}),
              ("POST", "/browser/act", {})]))
    sections = (_split, _behind, _quiet, _prompt, _card, _settings, _off_race, _windows, _terminal, _pointer, _kept, _lease, _source_window, _window_lapse, _hostile, _prompt_down, _dirty, _cross_off)
    for section in sections:
        if os.environ.get("AUTO_ONLY") and os.environ["AUTO_ONLY"] not in section.__name__: continue
        mock = MockDaemon(0).start()
        fixture = DevFixture(mock)
        context = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
        guard(context)
        context.add_init_script(init_script)
        page = context.new_page()
        page.set_default_timeout(4000)
        if section is _lease: page.clock.install()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            _boot(page, mock, "http://127.0.0.1:" + str(mock.port), until)
            before_browser = browser_calls(mock)
            section(browser, context, page, mock, fixture, check, until, guard, init_script)
            added = browser_calls(mock)[len(before_browser):]
            check(section.__name__ + " never invokes Remote browser routes",
                  added == [] if section is not _settings else all(method == "PATCH" and path == "/browser/settings" and set(body) == {"auto_open"} and type(body["auto_open"]) is bool for method, path, body in added),
                  str(added))
            check(section.__name__ + " has no page errors", not errors, "; ".join(errors[:2]))
        except Exception as error:
            check(section.__name__ + " ran to the end", False, str(error).splitlines()[0][:240])
        finally:
            context.close()
            fixture.close()
            mock.stop()


def _split(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    inp = page.locator(_pane(1, '[data-testid="input"]'))
    inp.fill("keep typing")
    inp.focus()
    # The microphone opens asynchronously during normal boot even without a
    # dev-server event. Measure ownership only after that bootstrap completes:
    # the stream is granted, a capture path is chosen, and the speech detector has
    # settled (Silero ready, or the energy fallback for a stated reason -- V5).
    # (`mic.state().open` is the detector's "an utterance is open" flag, not the
    # microphone: Chrome's fake device beeps opened the energy VAD, and Silero rightly
    # does not call a beep speech, so it can no longer stand for "bootstrapped".)
    check("microphone bootstrap completes before the split baseline",
          bool(until(lambda: page.evaluate(
              "window.__hud.voiceDiagnostics().mic === 'available' && window.__hud.mic.path() !== 'unknown'"
              " && ['ready', 'failed'].includes(window.__hud.mic.detector().state)"))))
    before = browser_calls(mock)
    selection = page.evaluate("({focused: window.__hud.workspace().ws.focused, selected: window.__hud.state().selectedChat, micOpen: window.__hud.mic.state().open, micClaimed: window.__hud.mic.state().claimed, micGranted: window.__hud.voiceDiagnostics().mic, micPath: window.__hud.mic.path(), micDetector: window.__hud.mic.detector().detector})")
    entry = f.add(mock.dev_port, path="/?secret=gone#gone")
    until(lambda: len(tabs(page)) == 1)
    rows = tabs(page)
    check("live agent up splits beside the source without replacing chat", len(rows) == 1 and panes(page)[0]["view"] == "chat")
    check("automatic URL drops query/fragment and carries immutable source", bool(rows) and rows[0]["url"] == f"http://localhost:{mock.dev_port}/"
          and rows[0].get("source") == {"kind": "dev_server", "entryId": entry.id}, str(rows))
    check("automatic frame keeps own-origin permission off", page.locator(FRAME).get_attribute("sandbox") == "allow-scripts allow-forms")
    after_selection = page.evaluate("({focused: window.__hud.workspace().ws.focused, selected: window.__hud.state().selectedChat, micOpen: window.__hud.mic.state().open, micClaimed: window.__hud.mic.state().claimed, micGranted: window.__hud.voiceDiagnostics().mic, micPath: window.__hud.mic.path(), micDetector: window.__hud.mic.detector().detector})")
    check("split retains workspace focus, selected chat and mic",
          selection == after_selection, str({"before": selection, "after": after_selection}))
    page.keyboard.type(" still here")
    check("split never steals the owner's typing focus", inp.input_value() == "keep typing still here")
    check("claim and placement use one 32-hex page identity",
          re.fullmatch("[0-9a-f]{32}", window_id(mock)) is not None and entry.window == window_id(mock)
          and bool(mock.posted("/dev-servers/" + entry.id + "/placed")))
    check("automatic split makes zero Remote browser-manager requests beyond Direct discovery", browser_calls(mock) == before)
    f.publish({"kind": "dev_server", "data": {"id": entry.id, "state": "up", "source": entry.source}})
    page.wait_for_timeout(250)
    check("duplicate SSE cannot create another tab", len(tabs(page)) == 1)


def _behind(browser, context, page, mock, f, check, until, guard, init):
    # Eight retained pane ids mean no spare split; a focused Browser receives
    # a background tab while its original page and URL box remain selected.
    configure(page, mock, until, "grid4", ("browser", "chat", "chat", "chat", "chat", "chat", "chat", "chat"), 1600)
    state = _ws(page)["ws"]
    state["order"] = [1, 2, 3, 4]
    state["kept"] = [5, 6, 7, 8]
    for n in (4, 5, 6, 7): state["panes"][n]["projectId"] = "p1"
    state["panes"][0]["browser"] = {"tabs": [{"id": "old001", "url": f"http://localhost:{mock.dev_port}/old"}], "active": "old001"}
    _set_ws(page, json.dumps(state))
    _boot(page, mock, None, until, reload=True)
    url = page.locator(_pane(1, '[data-testid="browser-url"]'))
    url.focus()
    entry = f.add(mock.dev_port + 1)
    until(lambda: len(tabs(page)) == 2)
    current = panes(page)[0]["browser"]
    check("in-use Browser appends a tab behind the active page", len(current["tabs"]) == 2 and current["active"] == "old001", str(current))
    check("background automatic tab says New tab and has an unseen dot",
          page.locator(".browser-unseen").count() == 1 and page.locator(".browser-newtab-note").count() == 1)
    accent_dot = page.locator(".browser-unseen").evaluate("""e => {
      const probe = document.createElement('i'); probe.style.background = 'var(--accent)';
      e.append(probe); const expected = getComputedStyle(probe).backgroundColor;
      const actual = getComputedStyle(e).backgroundColor; probe.remove(); return actual === expected;
    }""")
    check("unseen automatic tab uses the theme accent", accent_dot)
    check("appending behind preserves the URL box focus", url.evaluate("(e) => document.activeElement === e"))
    check("behind placement sends its receipt", bool(mock.posted("/dev-servers/" + entry.id + "/placed")))


def _quiet(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    # Bootstrap sees an older unclaimed server. It offers a quiet notice.
    entry = f.add(mock.dev_port)
    _boot(page, mock, None, until, reload=True)
    until(lambda: page.locator(PROMPT).count() == 1)
    page.wait_for_timeout(700)
    check("initial unclaimed listing remains quiet until Open", not tabs(page) and page.locator(PROMPT + " button").filter(has_text=re.compile("^Open$")).count() == 1)
    page.locator(PROMPT + " button").filter(has_text=re.compile("^Open$")).click()
    until(lambda: len(tabs(page)) == 1)
    check("explicit Open claims then places the quiet server", len(tabs(page)) == 1 and entry.state == "placed")


def _prompt(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until, "single", ("chat",), 700)
    inp = page.locator(_pane(1, '[data-testid="input"]'))
    inp.fill("draft")
    inp.focus()
    entry = f.add(mock.dev_port)
    until(lambda: page.locator(PROMPT).count() == 1)
    check("no spare room offers a prompt without changing any pane", not tabs(page))
    # Real keyboard input outside the prompt is never a pane command.
    page.keyboard.type("1")
    check("prompt numbers outside the prompt remain typed text", inp.input_value() == "draft1" and not tabs(page))
    page.wait_for_timeout(600)
    rect = page.locator(PROMPT).bounding_box()
    input_rect = inp.bounding_box()
    title = page.locator("#titlebar").bounding_box()
    check("prompt sits below titlebar and above chat input", rect["y"] >= title["y"] + title["height"] and rect["y"] + rect["height"] < input_rect["y"])
    target = page.locator(PROMPT + ' [data-dev-slot="1"]')
    page.set_viewport_size({"width": 720, "height": 900})
    page.wait_for_timeout(60)
    target.dispatch_event("click")
    page.wait_for_timeout(120)
    check("moving prompt disarms its target for 500ms", not tabs(page))
    page.wait_for_timeout(550)
    target.click()
    until(lambda: len(tabs(page)) == 1)
    check("explicit prompt choice makes a one-tab Browser", panes(page)[0]["view"] == "browser" and len(tabs(page)) == 1 and entry.state == "placed")
    page.locator(_pane(1, '[data-testid="tab-chat"]')).click()
    check("choosing the chat pane retained its unsent words", inp.input_value() == "draft1")


def _card(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    _approval(mock, "ao-card")
    page.wait_for_selector('[data-testid="approval-card"]')
    before = _ws(page)["ws"]
    entry = f.add(mock.dev_port)
    until(lambda: entry.state == "claimed")
    page.wait_for_timeout(250)
    check("an approval card defers both layout mutation and prompt", _ws(page)["ws"] == before and page.locator(PROMPT).count() == 0)
    page.keyboard.press("Escape")
    until(lambda: len(tabs(page)) == 1)
    check("queued claim places once the approval card leaves", len(tabs(page)) == 1 and entry.state == "placed")


def _settings(browser, context, page, mock, f, check, until, guard, init):
    from tests.face.hud_v2_check import open_settings
    configure(page, mock, until)
    open_settings(page, "Browser")
    checkbox = page.locator('[data-testid="browser-auto-open"]')
    check("Browser Settings bootstraps enabled from the registry", checkbox.is_checked())
    entry = f.add(mock.dev_port)
    until(lambda: entry.state == "claimed")
    page.wait_for_timeout(200)
    check("Settings coverage defers all placement and prompt UI", not tabs(page) and page.locator(PROMPT).count() == 0)
    checkbox.click()
    until(lambda: not f.enabled)
    page.locator('[data-testid="settings-close"]').click()
    page.wait_for_timeout(300)
    check("OFF revokes a queued Settings placement", not tabs(page) and entry.window is None)
    open_settings(page, "Browser")
    checkbox.click()
    until(lambda: f.enabled)
    page.locator('[data-testid="settings-close"]').click()
    page.wait_for_timeout(250)
    check("ON does not replay an old automatic placement", not tabs(page))


def _off_race(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    f.claim_gate = threading.Event()
    entry = f.add(mock.dev_port)
    until(lambda: entry.state == "claimed")
    f.off()
    page.wait_for_timeout(100)
    f.off(True)
    f.claim_gate.set()
    page.wait_for_timeout(600)
    check("OFF then ON fences a winning claim response still in flight", not tabs(page))
    check("stale claim result cannot send placed", not mock.posted("/dev-servers/" + entry.id + "/placed"))


def _windows(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    second = context.new_page()
    _boot(second, mock, "http://127.0.0.1:" + str(mock.port), until)
    try:
        first_id, second_id = list(f.streams)[-2:]
        second.bring_to_front()
        second.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_timeout(50)
        entry = f.add(mock.dev_port)
        until(lambda: entry.state == "placed")
        total = len(tabs(page)) + len(tabs(second))
        check("two real windows arbitrate to one foreground placement", total == 1 and entry.window == second_id, str((total, entry.window, second_id)))
        check("window identity is memory-only and differs across pages", first_id != second_id and len(first_id) == len(second_id) == 32)
        check("both windows used the actual claim route", len(mock.posted("/dev-servers/" + entry.id + "/claim")) == 2)
        check("one winner sends exactly one placed receipt", len(mock.posted("/dev-servers/" + entry.id + "/placed")) == 1)
    finally:
        second.close()


def _terminal(browser, context, page, mock, f, check, until, guard, init):
    from tests.face.hud_v2_terminal_check import FakePty, _new_terminal
    fake = FakePty(context, mock)
    mock.fake_pty = fake
    terminal = _new_terminal(page, mock, until, fake)
    holder = fake.holders[terminal]
    entry = f.add(mock.dev_port, {"kind": "terminal", "terminal_id": terminal}, window=window_id(mock))
    holder["ws"].send(json.dumps({"type": "dev_url", "id": entry.id, "url": entry.url}))
    until(lambda: len(tabs(page)) == 1)
    check("attached terminal preclaim places without a second claim", len(tabs(page)) == 1 and not mock.posted("/dev-servers/" + entry.id + "/claim"))
    check("terminal attach binds the same 32-hex page identity", "window=" + window_id(mock) in holder["ws"].url)
    holder["ws"].send(json.dumps({"type": "dev_url", "id": entry.id, "url": entry.url}))
    page.wait_for_timeout(150)
    check("duplicate terminal control never places twice", len(tabs(page)) == 1)


def _pointer(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    inp = page.locator(_pane(1, '[data-testid="input"]'))
    box = inp.bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 10)
    page.mouse.down()
    entry = f.add(mock.dev_port)
    until(lambda: entry.state == "claimed")
    page.wait_for_timeout(150)
    check("held owner pointer defers an otherwise valid split", not tabs(page))
    page.mouse.up()
    page.wait_for_timeout(60)
    check("release keeps the 150ms quiet interval", not tabs(page))
    until(lambda: len(tabs(page)) == 1)
    check("split runs after pointer release quiet interval", len(tabs(page)) == 1)


def _kept(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    state = _ws(page)["ws"]
    state["order"], state["kept"] = [1], [2, 3, 4, 5, 6, 7, 8]
    # Nonblank kept specs must not be overwritten. Pane 4 is the oldest
    # genuinely blank default spec and may be reused.
    state["panes"][1].update(view="file", projectId="p1", filePath="calc.py")
    state["panes"][2].update(view="chat", projectId="p2")
    for i in (4, 5, 6, 7): state["panes"][i]["projectId"] = "p1"
    _set_ws(page, json.dumps(state))
    _boot(page, mock, None, until, reload=True)
    before = panes(page)
    f.add(mock.dev_port)
    until(lambda: len(tabs(page)) == 1)
    after = panes(page)
    check("8-to-1 retained layout reuses only its oldest blank pane", after[3]["view"] == "browser" and _ws(page)["ws"]["order"] == [1, 4], str(_ws(page)["ws"]["order"]))
    check("all nonblank kept pane specs survive automatic splitting", all(before[i] == after[i] for i in (1, 2, 4, 5, 6, 7)))


def _lease(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    _approval(mock, "ao-lease")
    page.wait_for_selector('[data-testid="approval-card"]')
    entry = f.add(mock.dev_port)
    until(lambda: entry.state == "claimed")
    for _ in range(6):
        prior_lease = entry.lease_until
        f.now += 30
        page.clock.fast_forward(31_000)
        until(lambda: page.evaluate("true") and entry.lease_until > prior_lease)
    renews = mock.posted("/dev-servers/" + entry.id + "/renew")
    check("a three-minute card renews the claim without opening anything", len(renews) >= 6 and entry.lease_until > f.now and not tabs(page), str(len(renews)))
    f.down(entry)
    page.wait_for_timeout(50)
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    check("down while blocked cancels the queued placement", not tabs(page) and not mock.posted("/dev-servers/" + entry.id + "/placed"))


def _source_window(browser, context, page, mock, f, check, until, guard, init):
    from tests.face.hud_v2_multichat_check import _open
    configure(page, mock, until)
    _open(page, until, 1, "t1")
    source_id = window_id(mock)
    second = context.new_page()
    _boot(second, mock, "http://127.0.0.1:" + str(mock.port), until)
    try:
        second.bring_to_front()
        second.evaluate("window.dispatchEvent(new Event('focus'))")
        entry = f.add(mock.dev_port)
        until(lambda: entry.state == "placed")
        check("drawn source wins over a newer foreground window", entry.window == source_id and len(tabs(page)) == 1 and not tabs(second))
        claims = mock.posted("/dev-servers/" + entry.id + "/claim")
        check("claim shows is derived from the actual drawn conversation", any(c["window"] == source_id and c["shows"] for c in claims))
    finally:
        second.close()


def _window_lapse(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    second = context.new_page()
    _boot(second, mock, "http://127.0.0.1:" + str(mock.port), until)
    second.bring_to_front()
    second.evaluate("window.dispatchEvent(new Event('focus'))")
    second_id = window_id(mock)
    _approval(mock, "ao-lapse")
    page.wait_for_selector('[data-testid="approval-card"]')
    second.wait_for_selector('[data-testid="approval-card"]')
    entry = f.add(mock.dev_port)
    until(lambda: entry.state == "claimed")
    check("blocked foreground window holds the lease before closing", entry.window == second_id)
    second.close()
    until(lambda: entry.window is not None and entry.window != second_id, timeout=5)
    page.keyboard.press("Escape")
    until(lambda: entry.state == "placed")
    check("closing a blocked winner releases its claim to the remaining window", len(tabs(page)) == 1 and entry.window != second_id)


def _hostile(browser, context, page, mock, f, check, until, guard, init):
    from tests.face.hud_v2_browser_check import _away
    configure(page, mock, until)
    _away(page)
    inp = page.locator(_pane(1, '[data-testid="input"]'))
    inp.fill("owner")
    inp.focus()
    f.add(mock.dev_port, path="/focus-mount")
    until(lambda: page.locator('[data-testid="browser-stolen"]').count() > 0)
    page.keyboard.type(" safe")
    check("automatic hostile frame is removed by the existing focus guard", page.locator('[data-testid="browser-stolen"]').count() == 1 and page.locator(FRAME).count() == 0)
    check("automatic hostile frame receives no owner keys", inp.input_value() == "owner safe" and not mock.dev_keys)


def _prompt_down(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until, width=700)
    entry = f.add(mock.dev_port)
    until(lambda: page.locator(PROMPT).count() == 1)
    button = page.locator(PROMPT + ' [data-dev-slot="1"]')
    page.wait_for_timeout(50)
    button.dispatch_event("click")
    page.wait_for_timeout(150)
    check("new prompt ignores immediate clicks", not tabs(page))
    f.down(entry)
    until(lambda: page.locator(PROMPT).count() == 0)
    check("down removes an offered prompt", page.locator(PROMPT).count() == 0)
    f.now += 61
    entry = f.add(mock.dev_port + 1, {"kind": "thread", "thread_id": "t2", "task_id": None})
    until(lambda: page.locator(PROMPT).count() == 1)
    page.wait_for_timeout(600)
    page.locator(PROMPT).focus()
    page.keyboard.press("Escape")
    until(lambda: entry.state == "dismissed")
    check("Escape focused inside prompt dismisses through owner route", entry.state == "dismissed" and len(mock.posted("/dev-servers/" + entry.id + "/dismiss")) == 1)


def _dirty(browser, context, page, mock, f, check, until, guard, init):
    from tests.face.hud_v2_multichat_check import _open, _edit
    configure(page, mock, until, "cols2", ("chat", "file"), 1100)
    _open(page, until, 1, "t1")
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    _edit(page, until, 2, "\n# keep my unsaved edit")
    save = page.locator(_pane(2, '[data-testid="file-save"]'))
    until(lambda: not save.is_disabled())
    f.add(mock.dev_port)
    until(lambda: page.locator(PROMPT).count() == 1)
    page.wait_for_timeout(600)
    target = page.locator(PROMPT + ' [data-dev-slot="2"]')
    check("prompt disables the last view of an unsaved editor", target.get_attribute("aria-disabled") == "true" and "unsaved edit" in target.inner_text())
    target.dispatch_event("click")
    check("disabled editor choice cannot replace the dirty view", panes(page)[1]["view"] == "file" and not save.is_disabled())


def _cross_off(browser, context, page, mock, f, check, until, guard, init):
    configure(page, mock, until)
    second = context.new_page()
    _boot(second, mock, "http://127.0.0.1:" + str(mock.port), until)
    try:
        second.bring_to_front()
        second.evaluate("window.dispatchEvent(new Event('focus'))")
        _approval(mock, "ao-off-all")
        page.wait_for_selector('[data-testid="approval-card"]')
        second.wait_for_selector('[data-testid="approval-card"]')
        entry = f.add(mock.dev_port)
        until(lambda: entry.state == "claimed")
        f.off()
        page.wait_for_timeout(100)
        f.off(True)
        page.wait_for_timeout(100)
        page.keyboard.press("Escape")
        second.keyboard.press("Escape")
        page.wait_for_timeout(350)
        check("cross-window OFF invalidates pending card placement in every page", not tabs(page) and not tabs(second))
        check("OFF/ON cannot revive a queued placed receipt", not mock.posted("/dev-servers/" + entry.id + "/placed"))
    finally:
        second.close()
