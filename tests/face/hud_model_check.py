"""Synthetic checks for the HUD's model picker. Free — no API calls.

Puppet-string pattern (hud_session_check): the real `jarvis.html` is served
and `/config`, `/models`, `/models/catalog`, `/model` are stubs this test
scripts. Route tests do not cover the window's own JS, and almost all of this
feature *is* window JS — the search box, the two filters, the add/remove
toggle, and which of the two stacked pickers Escape backs out of.

The one that is not cosmetic: a model **name comes off the network**, and this
window is the surface that gates approvals. It must land as text.

Run:  .venv/bin/python tests/face/hud_model_check.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8479
BASE = f"http://127.0.0.1:{PORT}"

SSE: queue.Queue = queue.Queue()
POSTS: list[tuple[str, dict]] = []  # what the window POSTed, in order

DEFAULT = "openai/gpt-5.6-luna"


def model(model_id, name, iq=None, out=4.0, vision=False, ctx=128000, efforts=()):
    return {
        "id": model_id, "name": name, "context": ctx,
        "prompt_usd": round(out / 4, 3), "completion_usd": out,
        "vision": vision, "reasoning": bool(efforts), "efforts": list(efforts),
        "intelligence": iq, "agentic": None, "free": out == 0,
        "description": "",
    }


CATALOG = [
    model("anthropic/claude-opus-5", "Anthropic: Opus 5", iq=63.1, out=25.0, vision=True),
    model(DEFAULT, "OpenAI: Luna", iq=58, out=4.0, vision=True,
          efforts=["max", "high", "medium", "low"]),
    model("google/gemini-3.7-flash", "Google: Gemini 3.7 Flash", iq=51, out=0.6,
          efforts=["max", "high", "low"]),
    model("tiny/free-model", "Tiny: Free", iq=31, out=0.0),
    model("mid/unrated", "Mid: Unrated", iq=None, out=0.5),
    # A name written by whoever publishes the model. It reaches the window
    # that draws authorization cards, so it must never be parsed as markup.
    model("evil/inject", "<img src=x onerror=window.__pwned=1>Evil"),
]

ROSTER = [DEFAULT]


def roster_payload():
    by_id = {m["id"]: m for m in CATALOG}
    rows = []
    for model_id in ROSTER:
        row = dict(by_id[model_id])
        pinned = STATE["efforts"].get(model_id, "")
        row["effort"] = pinned
        # What the server would compute: the pin, else the global default
        # clamped to this model's ladder.
        ladder = row.get("efforts") or []
        row["effective_effort"] = pinned or ("max" if "max" in ladder else "")
        rows.append(row)
    return {
        "models": rows,
        "selected": STATE["selected"],
        "default": DEFAULT,
        "current": STATE["selected"] or DEFAULT,
        "default_effort": "max",
    }


STATE = {"selected": "", "efforts": {}}


class ScriptedHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while True:
                event = SSE.get()
                if event is None:
                    return
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
        if self.path == "/config":
            self._json({"llm": STATE["selected"] or DEFAULT, "stt": "test/stt",
                        "tts": "test/tts", "voice": "test", "speed": 1.0})
            return
        if self.path == "/models":
            self._json(roster_payload())
            return
        if self.path == "/models/catalog":
            self._json({"models": CATALOG, "roster": list(ROSTER), "stale": ""})
            return
        super().do_GET()

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        POSTS.append((self.path, data))
        if self.path == "/model":
            STATE["selected"] = data.get("model", "")
        elif self.path == "/models":
            if data.get("model"):
                level = data.get("effort") or ""
                if level:
                    STATE["efforts"][data["model"]] = level
                else:
                    STATE["efforts"].pop(data["model"], None)
            if data.get("add") and data["add"] not in ROSTER:
                ROSTER.append(data["add"])
            if data.get("remove"):
                if data["remove"] in ROSTER:
                    ROSTER.remove(data["remove"])
                if STATE["selected"] == data["remove"]:
                    STATE["selected"] = ""
        else:
            self.send_error(404)
            return
        self._json(roster_payload())


def texts(page, selector: str) -> list[str]:
    return page.eval_on_selector_all(selector, "els => els.map(e => e.textContent)")


def run_checks(page) -> None:
    # 1. The CORE readout is the way in, and it names the running model.
    assert page.inner_text("#cfg-llm") == "gpt-5.6-luna"
    page.click("#model-row")
    page.wait_for_selector("#models.on")
    rows = texts(page, "#modellist .sess")
    assert len(rows) == 2, rows
    assert "CONFIG DEFAULT" in rows[0] and DEFAULT in rows[0], rows
    here = texts(page, "#modellist .sess.here")
    assert len(here) == 1 and "CONFIG DEFAULT" in here[0], here
    assert "IQ 58" in rows[1] and "VISION" in rows[1] and "$1.0/$4.0" in rows[1], rows
    print("ok  list: CORE opens the roster, marks the default, badges price and IQ")

    # 2. Push-to-talk is inert while a picker is open — the same rule every
    #    other picker follows, and it comes free from the .picker class.
    if page.evaluate("() => !!stream"):
        page.evaluate("() => pressToTalk()")
        assert not page.evaluate("() => window.__hud.mic.ptt()"), \
            "PTT started with the model picker open"
        print("ok  list: push-to-talk is inert while it is open")

    # 3. ADD MODEL browses everything eligible.
    page.click("#model-add")
    page.wait_for_selector("#modelcatalog.on")
    assert not page.is_visible("#models"), "both pickers open at once"
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 6")
    note = page.inner_text("#catalog-note")
    assert "6 MATCHES" in note and "6 ELIGIBLE" in note, note
    listed = texts(page, "#cataloglist .sess.here")
    assert len(listed) == 1 and "OpenAI: Luna" in listed[0], listed
    print("ok  catalog: ADD MODEL browses the eligible catalog and marks what is on the list")

    # 4. A hostile model name is text, not markup — this window gates approvals.
    assert page.evaluate("() => window.__pwned === undefined"), "a model name ran script"
    assert page.eval_on_selector_all("#cataloglist img", "els => els.length") == 0
    assert any("<img src=x" in r for r in texts(page, "#cataloglist .sess")), "name lost"
    print("ok  catalog: a model name carrying markup renders as text")

    # 5. Search matches name and id.
    page.fill("#model-search", "gemini")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 1")
    assert "Gemini" in texts(page, "#cataloglist .sess")[0]
    page.fill("#model-search", "anthropic/")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 1")
    assert "Opus" in texts(page, "#cataloglist .sess")[0]
    # Typing a space must not grab the microphone: Space is push-to-talk at
    # the document level, so the box has to stop the event like the input bar.
    page.fill("#model-search", "")
    page.click("#model-search")
    page.keyboard.type("opus 5")
    assert page.input_value("#model-search") == "opus 5"
    if page.evaluate("() => !!stream"):
        assert not page.evaluate("() => window.__hud.mic.ptt()"), "a typed space started PTT"
    page.fill("#model-search", "")
    print("ok  search: filters on name and id, and a typed space stays in the box")

    # 6. Price filter, judged on output price. FREE is its own bucket.
    page.select_option("#model-price", "0")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 1")
    assert "Tiny: Free" in texts(page, "#cataloglist .sess")[0]
    page.select_option("#model-price", "1")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 3")
    rows = " ".join(texts(page, "#cataloglist .sess"))
    assert "Opus" not in rows and "Luna" not in rows, rows
    page.select_option("#model-price", "")

    # 7. Intelligence filter, and it says that unrated models fall out of it.
    page.select_option("#model-iq", "50")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 3")
    rows = " ".join(texts(page, "#cataloglist .sess"))
    assert "Unrated" not in rows and "Tiny" not in rows, rows
    assert "UNRATED MODELS HIDDEN" in page.inner_text("#catalog-note")
    page.select_option("#model-iq", "60")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess').length === 1")
    assert "Opus" in texts(page, "#cataloglist .sess")[0]
    page.select_option("#model-iq", "")
    print("ok  filters: price and intelligence narrow the list, unrated exclusion is stated")

    # 8. Clicking adds; clicking the same row again takes it back off.
    page.click("#cataloglist .sess:not(.here)")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess.here').length === 2")
    assert POSTS[-1] == ("/models", {"add": "anthropic/claude-opus-5"}), POSTS[-1]
    page.click("#cataloglist .sess.here")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess.here').length === 1")
    assert POSTS[-1][1].get("remove") == "anthropic/claude-opus-5", POSTS[-1]
    page.click("#cataloglist .sess:not(.here)")
    page.wait_for_function("() => document.querySelectorAll('#cataloglist .sess.here').length === 2")
    print("ok  add/remove: a catalog row toggles roster membership")

    # 9. Escape backs out one level, then closes.
    page.keyboard.press("Escape")
    page.wait_for_selector("#models.on")
    assert not page.is_visible("#modelcatalog")
    rows = texts(page, "#modellist .sess")
    assert len(rows) == 3 and any("Opus 5" in r for r in rows), rows
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.getElementById('models').classList.contains('on')")
    print("ok  escape: backs out of the catalog to the roster, then closes")

    # 10. Selecting switches the model and relabels CORE.
    before = len(POSTS)
    page.click("#model-row")
    page.wait_for_selector("#models.on")
    page.click("#modellist .sess:not(.here) >> nth=1")
    page.wait_for_function("() => document.getElementById('cfg-llm').textContent === 'claude-opus-5'",
                           timeout=5_000)
    assert POSTS[-1] == ("/model", {"model": "anthropic/claude-opus-5"}), POSTS[-1]
    assert len(POSTS) == before + 1
    assert not page.is_visible("#models"), "picker stayed open after a switch"
    print("ok  select: POSTs /model, relabels CORE, closes the picker")

    # 11. Reasoning effort is set per model, from the row, on the models that
    #     publish a ladder — and setting it must not also switch him onto that
    #     model, which is what the row itself does.
    page.click("#model-row")
    page.wait_for_selector("#models.on")
    page.click("#model-add")
    page.wait_for_selector("#modelcatalog.on")
    page.click('#cataloglist .sess[data-model="google/gemini-3.7-flash"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#cataloglist .sess.here').length >= 3")
    page.click("#catalog-back")
    page.wait_for_selector("#models.on")

    # One control per model with a ladder, and none for the one without.
    ladders = page.eval_on_selector_all(
        "#modellist .sess",
        "els => els.map(e => [e.dataset.model || '', !!e.querySelector('.effort')])")
    have = {m: yes for m, yes in ladders if m}
    assert have.get(DEFAULT) is True, have
    assert have.get("google/gemini-3.7-flash") is True, have
    assert have.get("anthropic/claude-opus-5") is False, "a model with no ladder got a control"
    # The options are that model's own levels, not one shared list.
    opts = page.eval_on_selector_all(
        f'#modellist .sess[data-model="google/gemini-3.7-flash"] .effort option',
        "els => els.map(e => e.value)")
    assert opts == ["", "max", "high", "low"], opts
    auto = page.text_content(f'#modellist .sess[data-model="{DEFAULT}"] .effort option')
    assert "AUTO" in auto and "MAX" in auto, auto  # names what the default resolves to

    before = len(POSTS)
    selected_before = page.inner_text("#cfg-llm")
    page.select_option(f'#modellist .sess[data-model="{DEFAULT}"] .effort', "low")
    page.wait_for_function(
        "() => document.querySelectorAll('#modellist .effort.pinned').length === 1")
    assert POSTS[-1] == ("/models", {"model": DEFAULT, "effort": "low"}), POSTS[-1]
    assert len(POSTS) == before + 1, "setting effort did something else as well"
    assert page.inner_text("#cfg-llm") == selected_before, "setting effort switched models"
    assert page.is_visible("#models"), "setting effort closed the picker"

    # Back to AUTO clears the pin — the way out of any choice made here.
    page.select_option(f'#modellist .sess[data-model="{DEFAULT}"] .effort', "")
    page.wait_for_function(
        "() => document.querySelectorAll('#modellist .effort.pinned').length === 0")
    assert POSTS[-1] == ("/models", {"model": DEFAULT, "effort": ""}), POSTS[-1]
    print("ok  effort: a per-model control, its own ladder, and it does not select the model")
    page.evaluate("() => editRoster({ remove: 'google/gemini-3.7-flash' })")
    page.wait_for_function("() => document.querySelectorAll('#modellist .sess').length === 3")

    # 12. The × on a roster row removes without selecting.
    page.click("#model-row")
    page.wait_for_selector("#models.on")
    page.click("#modellist .sess.here .drop")
    page.wait_for_function("() => document.querySelectorAll('#modellist .sess').length === 2")
    assert POSTS[-1] == ("/models", {"remove": "anthropic/claude-opus-5"}), POSTS[-1]
    # Removing what was selected falls back to the default, live.
    assert page.inner_text("#cfg-llm") == "gpt-5.6-luna"
    assert page.is_visible("#models"), "removing a row closed the picker"
    page.keyboard.press("Escape")
    print("ok  remove: × drops the row without selecting it, selection falls back")

    # 13. Another window's switch relabels this one.
    SSE.put({"kind": "model", "data": {**roster_payload(), "current": "google/gemini-3.7-flash"}})
    page.wait_for_function(
        "() => document.getElementById('cfg-llm').textContent === 'gemini-3.7-flash'",
        timeout=5_000,
    )
    print("ok  sse: a model broadcast relabels the window")


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(ScriptedHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
            run_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        SSE.put(None)
        server.shutdown()
        server.server_close()
    print("\nall HUD model checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
