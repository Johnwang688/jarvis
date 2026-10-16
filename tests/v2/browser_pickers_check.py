"""Owner picker fallbacks (B-PICKERS, BR-7/M9) with real headless Chrome 155.

Pure: the wire shapes, the kinds' value grammars, label cleaning and the
message budget. Real Chrome, through the production owner ticket and socket
(`browser_routes_check`'s isolated daemon: temp HOME and BROWSER_DIR, a gate
that resolves nothing, fixture pages on an ephemeral loopback port): a
single select with optgroups, a disabled option and a disabled group; the
six input kinds; a select in a same-process iframe; a page that swaps the
element, or its options, while the picker is open; navigation, a card and a
reattach while it is open; a page that opens a picker on an element the
owner did not click; direct CDP input (the driving path's kind of input);
and an agent's real `hud_browser_act` click on a select, which never offers
a picker. Nothing on the bus or in the log carries a label.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/browser_pickers_check.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2.browser import pickers as PK, stream as S  # noqa: E402
from tests.v2 import browser_routes_check as B  # noqa: E402
from tests.v2.owner_browser_check import rss_bytes  # noqa: E402

CANARY = "PICKCANARYq"       # in option labels: never on the bus or in the log

LOG = """<script>
window.__ev = []; window.__mutations = 0;
const T = e => (e.target && (e.target.id || e.target.nodeName)) || '';
for (const k of ['focus', 'blur', 'keydown', 'keyup', 'input', 'change', 'click', 'mousedown'])
  document.addEventListener(k, e => __ev.push([e.type, T(e), e.isTrusted]), true);
new MutationObserver(x => __mutations += x.length).observe(document.documentElement,
  {attributes: true, childList: true, subtree: true, characterData: true});
</script>"""

FRAME_DOC = ("<body style='margin:0'><select id=fs style='position:absolute;left:10px;top:20px;"
             "width:200px;height:30px'><option value=one>One<option value=two>Two</select>"
             "<script>window.__ev=[];for(const k of ['input','change'])document.addEventListener(k,"
             "e=>__ev.push([e.type,e.target.id,e.isTrusted]),true);</script>")

PICKERS = f"""<!doctype html><title>pickers</title>
<style>body{{margin:0;font:14px sans-serif}}.f{{position:absolute;left:10px;width:220px;height:30px}}</style>
<select id=s class=f style="top:10px">
 <option value=a>Alpha {CANARY}
 <optgroup label="Group one"><option value=b>Bravo<option value=c disabled>Charlie</optgroup>
 <optgroup label="Off" disabled><option value=d>Delta</optgroup>
 <option value=e hidden>Echo
 <option value=f>Fox&#x202e;trot&#x200b; {"x" * 200}
</select>
<input id=date type=date class=f style="top:60px" min="2026-01-01" max="2026-12-31" value="2026-03-04">
<input id=time type=time class=f style="top:110px">
<input id=dtl type=datetime-local class=f style="top:160px">
<input id=month type=month class=f style="top:210px">
<input id=week type=week class=f style="top:260px">
<input id=color type=color class=f style="top:310px" value="#336699">
<input id=text class=f style="top:360px">
<select id=other class=f style="top:410px"><option value=1>One<option value=2>Two</select>
<button id=opener class=f style="top:460px">open another</button>
<select id=multi class=f multiple style="top:510px;height:60px"><option>m1<option>m2</select>
<select id=chsel class=f style="top:575px;left:300px"><option value=x>X<option value=y>Y</select>
<input id=file type=file style="position:absolute;left:600px;top:575px;width:100px">
<iframe id=fr style="position:absolute;left:300px;top:10px;width:300px;height:120px;border:0"
 srcdoc="{FRAME_DOC}"></iframe>
<script>
document.getElementById('chsel').addEventListener('mousedown', () => document.getElementById('file').click());
document.getElementById('opener').addEventListener('click', () => document.getElementById('other').showPicker());
</script>
""" + LOG

LONG = "<!doctype html><select id=s style='position:absolute;left:10px;top:10px;width:220px;height:30px'>" + "".join(
    f"<option value=v{i}>{'label ' * 30}{i}" for i in range(2000)) + "</select>"

B.PAGES["/pickers"] = PICKERS
B.PAGES["/pickers-long"] = LONG
GROUPS = "<!doctype html><select id=s style='position:absolute;left:10px;top:10px;width:220px;height:30px'>" + "".join(
    f"<optgroup label='{'\u6f22' * 80}{i}'><option value=v{i}>{'\u5b57' * 80}{i}</optgroup>" for i in range(500)) + "</select>"
B.PAGES["/pickers-groups"] = GROUPS
B.PAGES["/pickers-next"] = "<!doctype html><title>next</title><p>next page</p>"

POINTS = {"s": (100, 25), "date": (222, 75), "time": (222, 125), "dtl": (222, 175), "month": (222, 225),
          "week": (222, 275), "color": (100, 325), "text": (100, 375), "other": (100, 425),
          "opener": (100, 475), "multi": (100, 530), "fs": (410, 45), "chsel": (400, 590)}


class Pure(unittest.TestCase):
    def test_wire_shapes(self):
        ok = [{"t": "pick", "id": "0123abcd", "index": 0}, {"t": "pick", "id": "0123abcd", "index": 65535},
              {"t": "pick", "id": "0123abcd", "value": "2026-10-16"}, {"t": "pick", "id": "0123abcd", "value": ""},
              {"t": "pick", "id": "0123abcd", "value": "#a1b2c3"}, {"t": "pick_cancel", "id": "0123abcd"}]
        for m in ok:
            parsed = S.parse(json.dumps(m).encode())
            self.assertEqual(parsed.t, m["t"])
            if m["t"] == "pick":
                self.assertEqual(parsed.method, PK.PICK_METHOD)
                self.assertTrue(parsed.input and parsed.takeover)
            else:
                self.assertFalse(parsed.input)
        bad = [{"t": "pick", "id": "0123ABCD", "index": 1}, {"t": "pick", "id": "0123abc", "index": 1},
               {"t": "pick", "id": "0123abcd"}, {"t": "pick", "id": "0123abcd", "index": -1},
               {"t": "pick", "id": "0123abcd", "index": 65536}, {"t": "pick", "id": "0123abcd", "index": True},
               {"t": "pick", "id": "0123abcd", "index": 1.0}, {"t": "pick", "id": "0123abcd", "index": 1, "value": "x"},
               {"t": "pick", "id": "0123abcd", "value": "x" * 65}, {"t": "pick", "id": "0123abcd", "value": "a b"},
               {"t": "pick", "id": "0123abcd", "value": "<script>"}, {"t": "pick", "id": "0123abcd", "value": 5},
               {"t": "pick_cancel", "id": "0123abcd", "index": 1}, {"t": "pick_cancel"}]
        for m in bad:
            with self.assertRaises(S.Bad, msg=m):
                S.parse(json.dumps(m).encode())

    def test_value_grammar(self):
        good = {"date": ["2026-10-16", "275760-09-13", ""], "time": ["09:30", "09:30:15", "09:30:15.250", ""],
                "datetime-local": ["2026-10-16T09:30", "2026-10-16T09:30:01.5", ""], "month": ["2026-10", ""],
                "week": ["2026-W42", ""], "color": ["#00ff7f"]}
        badv = {"date": ["2026-1-16", "16/10/2026", "2026-10-16T00:00"], "time": ["9:30", "09:30:1"],
                "datetime-local": ["2026-10-16 09:30"], "month": ["2026-10-01"], "week": ["2026-42"],
                "color": ["", "#00FF7F", "#0f0", "red"]}
        for kind, values in good.items():
            for v in values:
                self.assertTrue(PK.valid_value(kind, v), (kind, v))
        for kind, values in badv.items():
            for v in values:
                self.assertFalse(PK.valid_value(kind, v), (kind, v))
        self.assertFalse(PK.valid_value("select", "a"))
        self.assertFalse(PK.valid_value("date", None))

    def test_labels_are_cleaned_and_capped(self):
        self.assertEqual(PK.label("a\u202eb\u200bc\nd"), "a b c d")
        out = PK.label("x" * 500)
        self.assertLessEqual(len(out), PK.LABEL_MAX)
        self.assertNotIn("‮", PK.label("Fox‮trot"))
        self.assertNotIn("\n", PK.label("one\ntwo"))
        self.assertEqual(PK.label(None), "")


class PickersChrome(B.BrowserStream):
    """Ordered sections on one daemon and one Chrome."""

    def setUp(self):
        cls = type(self)
        if cls.tab is None:
            tid = self.owner("POST", "/browser/tabs", {"url": self.url("/pickers")})["tab_id"]
            cls.tid = tid
            cls.tab = self.host._tabs[tid]
        self.w = self.attached(self.tid)
        self.w.send({"t": "resize", "width": 800, "height": 600})
        self.w.settled()
        self.load()

    def load(self, path="/pickers"):
        self.navigate(self.tid, path)
        self.wait(lambda: self.tab.pane_ready, what="landed")
        self.w.settled()

    def ev(self, frame=False):
        expression = ("document.getElementById('fr').contentWindow.__ev.splice(0)" if frame
                      else "__ev.splice(0)")
        return self.js(self.tid, expression)

    def is_open(self, el):
        if el == "fs":
            return self.js(self.tid, "document.getElementById('fr').contentDocument.getElementById('fs').matches(':open')")
        return self.js(self.tid, f"document.getElementById('{el}').matches(':open')")

    def click(self, el, *, hover=True):
        x, y = POINTS[el]
        if hover:
            self.w.send(B.mouse("mouseMoved", x, y, buttons=0))
        B.click(self.w, x, y)
        self.w.settled()

    def pickers(self):
        return [m for m in self.w.messages if m.get("t") == "picker"]

    def open_picker(self, el, **kw):
        before = len(self.pickers())
        self.click(el, **kw)
        self.w.until(lambda w: len(self.pickers()) > before, what=f"a picker for {el}")
        return self.pickers()[-1]

    def closed(self, picker_id, timeout=8.0):
        return self.w.wait_for("picker_closed", id=picker_id, timeout=timeout)

    def no_picker(self, el, **kw):
        before = len(self.pickers())
        self.click(el, **kw)
        self.w.drain(0.4)
        self.assertEqual(len(self.pickers()), before, f"no picker for {el}")

    # -- 1: select ----------------------------------------------------------------------------------

    def test_01_select_offers_its_options_and_applies_to_that_element(self):
        self.ev()
        p = self.open_picker("s")
        self.assertEqual(p["kind"], "select")
        self.assertFalse(self.is_open("s"), "the native popup was closed")
        events = self.ev()
        self.assertNotIn("keydown", [e[0] for e in events], "the closing Escape reached no page listener")
        self.assertNotIn("keyup", [e[0] for e in events])
        self.assertEqual(self.js(self.tid, "document.activeElement.id"), "s", "focus stays on the control")
        labels = [(i["i"], i["label"], i["disabled"], i["g"]) for i in p["items"]]
        self.assertEqual([row[0] for row in labels], [0, 1, 2, 3, 5], "the hidden option is not listed")
        self.assertTrue(labels[0][1].startswith("Alpha "))
        self.assertEqual([row[2] for row in labels], [False, False, True, True, False])
        self.assertEqual(p["groups"], ["Group one", "Off"])
        self.assertEqual([row[3] for row in labels], [-1, 0, 0, 1, -1])
        self.assertNotIn("‮", labels[4][1])
        self.assertNotIn("​", labels[4][1])
        self.assertLessEqual(len(labels[4][1]), PK.LABEL_MAX)
        self.assertEqual((p["selected"], p["total"], p["shown"]), (0, 6, 5))
        r = p["rect"]
        self.assertAlmostEqual(r["x"], 10, delta=1)
        self.assertAlmostEqual(r["y"], 10, delta=1)
        self.assertAlmostEqual(r["width"], 220, delta=1)
        self.assertNotIn("value", json.dumps(p["items"]), "option values never leave the daemon")
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "b")
        events = [e for e in self.ev() if e[0] in ("input", "change")]
        self.assertEqual(events, [["input", "s", False], ["change", "s", False]])
        self.assertEqual(self.js(self.tid, "__mutations"), 0, "nothing was written to the DOM")

    def test_02_disabled_same_and_stale_choices(self):
        p = self.open_picker("s")
        self.w.send({"t": "pick", "id": p["id"], "index": 2})        # Charlie, disabled
        self.assertEqual(self.closed(p["id"])["reason"], "refused")
        p = self.open_picker("s")
        self.w.send({"t": "pick", "id": p["id"], "index": 3})        # Delta, in a disabled group
        self.assertEqual(self.closed(p["id"])["reason"], "refused")
        p = self.open_picker("s")
        self.w.send({"t": "pick", "id": p["id"], "index": 4})        # Echo, hidden: never offered
        self.assertEqual(self.closed(p["id"])["reason"], "refused")
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a")
        self.ev()
        p = self.open_picker("s")
        self.w.send({"t": "pick", "id": p["id"], "index": 0})        # the current one
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.assertEqual([e for e in self.ev() if e[0] in ("input", "change")], [], "the same value fires nothing")
        p = self.open_picker("s")
        self.w.send({"t": "pick", "id": "deadbeef", "index": 1})     # another picker's id
        self.w.settled()
        self.w.send({"t": "pick_cancel", "id": p["id"]})
        self.w.settled()
        self.w.send({"t": "pick", "id": p["id"], "index": 1})        # after its cancel
        self.w.settled()
        self.w.drain(0.3)
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a")
        self.assertIsNone(self.host._pickers.current(self.tab))

    # -- 2: date, time, datetime-local, month, week, colour ------------------------------------------

    def test_03_date_kinds_and_colour(self):
        cases = {"date": ("2026-10-16", {"min": "2026-01-01", "max": "2026-12-31", "value": "2026-03-04"}),
                 "time": ("09:30", {}), "dtl": ("2026-10-16T09:30", {}), "month": ("2026-10", {}),
                 "week": ("2026-W42", {}), "color": ("#ff0000", {"value": "#336699"})}
        kinds = {"dtl": "datetime-local"}
        for el, (value, seen) in cases.items():
            with self.subTest(el):
                self.ev()
                p = self.open_picker(el)
                self.assertEqual(p["kind"], kinds.get(el, el))
                self.assertFalse(self.is_open(el))
                for name, expected in seen.items():
                    self.assertEqual(p[name], expected)
                self.w.send({"t": "pick", "id": p["id"], "value": value})
                self.assertEqual(self.closed(p["id"])["reason"], "applied")
                self.assertEqual(self.js(self.tid, f"document.getElementById('{el}').value"), value)
                events = [e for e in self.ev() if e[0] in ("input", "change", "keydown")]
                self.assertEqual(events, [["input", el, False], ["change", el, False]])

    def test_04_values_a_control_would_not_take(self):
        p = self.open_picker("date")
        self.w.send({"t": "pick", "id": p["id"], "value": "2027-01-01"})    # past max
        self.assertEqual(self.closed(p["id"])["reason"], "refused")
        p = self.open_picker("date")
        self.w.send({"t": "pick", "id": p["id"], "value": "#ff0000"})       # another kind's grammar
        self.assertEqual(self.closed(p["id"])["reason"], "refused")
        self.assertEqual(self.js(self.tid, "document.getElementById('date').value"), "2026-03-04")
        p = self.open_picker("date")
        self.w.send({"t": "pick", "id": p["id"], "value": ""})              # clear: not required
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.assertEqual(self.js(self.tid, "document.getElementById('date').value"), "")
        self.js(self.tid, "document.getElementById('date').required = true")
        p = self.open_picker("date")
        self.assertTrue(p["required"])
        self.w.send({"t": "pick", "id": p["id"], "value": ""})
        self.assertEqual(self.closed(p["id"])["reason"], "refused")

    # -- 3: a select in a same-process iframe ------------------------------------------------------

    def test_05_select_in_an_iframe(self):
        p = self.open_picker("fs")
        self.assertFalse(self.is_open("fs"))
        self.assertAlmostEqual(p["rect"]["x"], 310, delta=1)
        self.assertAlmostEqual(p["rect"]["y"], 30, delta=1)
        self.assertEqual([i["label"] for i in p["items"]], ["One", "Two"])
        self.ev(frame=True)
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.assertEqual(self.js(self.tid, "document.getElementById('fr').contentDocument.getElementById('fs').value"), "two")
        self.assertEqual(self.ev(frame=True), [["input", "fs", False], ["change", "fs", False]])
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a", "the top select is untouched")

    # -- 4: the element or its options change while the picker is open ---------------------------------

    def test_06_a_swapped_element_is_never_applied_to(self):
        p = self.open_picker("s")
        # The page keeps the old element (so its node still resolves) and puts
        # an identical one, same id and options, where it was.
        self.js(self.tid, """(() => { const old = document.getElementById('s');
            window.__old = old; const twin = old.cloneNode(true); old.replaceWith(twin); })()""")
        self.ev()
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.assertEqual(self.js(self.tid, "[document.getElementById('s').value, __old.value]"), ["a", "a"])
        self.assertEqual([e for e in self.ev() if e[0] in ("input", "change")], [])

    def test_07_changed_options_are_never_applied_to(self):
        p = self.open_picker("s")
        self.js(self.tid, "document.getElementById('s').options[1].value = 'bb'")
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        p = self.open_picker("s")
        self.js(self.tid, "document.getElementById('s').append(new Option('Golf', 'g'))")
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        p = self.open_picker("s")
        self.js(self.tid, "document.getElementById('s').disabled = true")
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a")
        p = self.open_picker("date")
        self.js(self.tid, "document.getElementById('date').type = 'text'")
        self.w.send({"t": "pick", "id": p["id"], "value": "2026-05-05"})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.assertEqual(self.js(self.tid, "document.getElementById('date').value"), "2026-03-04")

    def test_08_a_handler_that_replaces_the_control_stops_the_change(self):
        self.js(self.tid, """document.getElementById('s').addEventListener('input', e => {
            const t = e.target.cloneNode(true); window.__old = e.target; e.target.replaceWith(t); }, {once: true})""")
        p = self.open_picker("s")
        self.ev()
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.assertEqual([e[0] for e in self.ev() if e[0] in ("input", "change")], ["input"])

    def test_08b_a_handler_that_changes_the_value_stops_the_change(self):
        self.js(self.tid, """document.getElementById('s').addEventListener('input', e => {
            e.target.selectedIndex = 0; }, {once: true})""")
        p = self.open_picker("s")
        self.ev()
        self.w.send({"t": "pick", "id": p["id"], "index": 5})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.assertEqual([e[0] for e in self.ev() if e[0] in ("input", "change")], ["input"])

    def test_08c_focus_moved_elsewhere_still_applies_to_the_picked_element(self):
        p = self.open_picker("s")
        self.js(self.tid, "document.getElementById('other').focus()")
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.assertEqual(self.js(self.tid, "[document.getElementById('s').value, document.getElementById('other').value]"),
                         ["b", "1"])

    # -- 5: navigation, a card, a reattach, a new press ----------------------------------------------

    def test_09_navigation_closes_it(self):
        p = self.open_picker("s")
        self.owner("POST", f"/browser/tabs/{self.tid}/navigate", {"url": self.url("/pickers-next")})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.assertIsNone(self.host._pickers.current(self.tab))
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.w.settled()
        self.load()
        p = self.open_picker("s")
        self.js(self.tid, "history.pushState({}, '', '#moved')")       # same-document navigation
        self.assertEqual(self.closed(p["id"])["reason"], "changed")

    def test_10_a_card_closes_it_and_nothing_is_applied_after(self):
        p = self.open_picker("s")
        with self.card():
            self.assertEqual(self.closed(p["id"])["reason"], "held")
            self.w.send({"t": "pick", "id": p["id"], "index": 1})
            self.w.settled()
            before = len(self.pickers())
            self.click("s")                                               # held: nothing reaches the page
            self.w.drain(0.3)
            self.assertEqual(len(self.pickers()), before)
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.w.settled()
        self.w.drain(0.3)
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a")

    def test_11_reattach_resize_hide_and_a_new_press_close_it(self):
        p = self.open_picker("s")
        self.w.send({"t": "resize", "width": 801, "height": 600})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        p = self.open_picker("s")
        self.w.send({"t": "visible", "visible": False})
        self.assertEqual(self.closed(p["id"])["reason"], "changed")
        self.w.send({"t": "visible", "visible": True})
        self.w.settled()
        p = self.open_picker("s")
        self.click("text")
        self.assertEqual(self.closed(p["id"])["reason"], "cancelled")
        p = self.open_picker("s")
        self.w.close()
        self.w.until(lambda w: w.closed is not None or True)
        self.wait(lambda: self.host._pickers.current(self.tab) is None, what="detach forgot the picker")
        self.w = self.attached(self.tid)
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.w.settled()
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a")

    # -- 6: what never offers one -------------------------------------------------------------------

    def test_12_only_the_element_the_owner_clicked(self):
        # The page opens a picker on another element in its own click handler.
        self.ev()
        self.no_picker("opener")
        opened = self.is_open("other")
        if opened:
            self.w.send({"t": "key", "e": dict(PK.ESCAPE, location=0)})
            self.w.settled()
        self.assertIsNone(self.host._pickers.current(self.tab))
        self.no_picker("text")
        self.no_picker("multi")                        # a list box opens no popup
        self.no_picker("date", hover=False)            # the field's own text: segments, no popup
        # Direct CDP input, the driving path's kind: the popup opens, no picker.
        x, y = POINTS["s"]
        for kind in ("mousePressed", "mouseReleased"):
            self.run_on(self.tab.cdp.send("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left", "buttons": 1 if kind == "mousePressed" else 0,
                "clickCount": 1}))
        self.w.drain(0.4)
        self.assertTrue(self.is_open("s"), "the driving path's click opened the native popup")
        self.assertIsNone(self.host._pickers.current(self.tab))
        self.run_on(self.tab.cdp.send("Input.dispatchKeyEvent", dict(PK.ESCAPE)))
        self.wait(lambda: not self.is_open("s"), what="the driving-path popup closed")

    def test_13_typing_after_a_picker_is_ordinary(self):
        p = self.open_picker("s")
        self.w.send({"t": "pick_cancel", "id": p["id"]})
        self.click("text")
        B.type_keys(self.w, "abc", pace=True)
        self.wait(lambda: self.js(self.tid, "document.getElementById('text').value") == "abc", what="typed")
        self.w.send(B.key("Escape", "Escape", 27))
        self.w.send(B.key("Escape", "Escape", 27, up=True))
        self.w.settled()
        self.assertIn(["keydown", "text", True], [e for e in self.ev() if e[0] == "keydown"][-1:])

    def test_14_a_long_list_is_bounded(self):
        self.load("/pickers-long")
        p = self.open_picker("s")
        self.assertEqual(p["total"], 2000)
        self.assertLessEqual(p["shown"], PK.OPTIONS_MAX)
        self.assertLess(p["shown"], 2000)
        self.assertLessEqual(len(json.dumps(p)), PK.MESSAGE_MAX)
        self.w.send({"t": "pick", "id": p["id"], "index": p["items"][-1]["i"]})
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.w.send({"t": "pick_cancel", "id": p["id"]})

    # -- 7: review of PR #232 ---------------------------------------------------------------------------

    def test_16_no_picker_behind_a_file_chooser(self):
        """MED 1: a page that opens a file chooser from the select's own
        mousedown. The select's popup opens too; nothing is offered, and
        nothing can be applied once the chooser is gone."""
        before = len(self.pickers())
        self.click("chsel")
        chooser = self.w.wait_for("filechooser")
        self.w.drain(0.5)
        self.assertEqual(len(self.pickers()), before, "no picker behind a file chooser")
        self.assertIsNone(self.host._pickers.current(self.tab))
        self.owner("POST", f"/browser/tabs/{self.tid}/files", {"chooser": chooser["id"], "files": []})
        self.w.drain(0.3)
        self.assertEqual(len(self.pickers()), before)
        self.assertIsNone(self.host._pickers.current(self.tab))
        if self.is_open("chsel"):
            self.run_on(self.tab.cdp.send("Input.dispatchKeyEvent", dict(PK.ESCAPE)))
        self.assertEqual(self.js(self.tid, "document.getElementById('chsel').value"), "x")

    def _inject_after_read(self, attention):
        """Patch the read so `attention(tab)` runs after it (a dialog or an
        auth prompt arriving while the probe is under way)."""
        original = PK.Pickers._call

        async def call(this, cdp, object_id, fn, *args):
            value = await original(this, cdp, object_id, fn, *args)
            if fn is PK.READ_FN:
                attention(self.tab)
            return value
        return original, call

    def test_17_no_picker_when_a_dialog_arrives_during_the_probe(self):
        original, call = self._inject_after_read(lambda tab: tab.dialogs.__setitem__("fake0001", object()))
        PK.Pickers._call = call
        try:
            before = len(self.pickers())
            self.click("s")
            self.w.drain(0.4)
            self.assertEqual(len(self.pickers()), before, "no picker while a dialog is open")
            self.assertIsNone(self.host._pickers.current(self.tab))
            self.assertTrue(self.is_open("s"), "the native popup was left alone")
        finally:
            PK.Pickers._call = original
            self.run_on(self._clear_dialog())
        self.run_on(self.tab.cdp.send("Input.dispatchKeyEvent", dict(PK.ESCAPE)))
        self.wait(lambda: not self.is_open("s"), what="cleanup Escape")

    async def _clear_dialog(self):
        self.tab.dialogs.pop("fake0001", None)

    def test_18_a_failed_build_leaves_the_native_popup_alone(self):
        original = PK.Pickers._build
        PK.Pickers._build = lambda this, *args: (None, None)
        try:
            before = len(self.pickers())
            self.click("s")
            self.w.drain(0.4)
            self.assertEqual(len(self.pickers()), before)
            self.assertTrue(self.is_open("s"), "nothing was sent to the native popup")
        finally:
            PK.Pickers._build = original
        self.run_on(self.tab.cdp.send("Input.dispatchKeyEvent", dict(PK.ESCAPE)))
        self.wait(lambda: not self.is_open("s"), what="cleanup Escape")

    def test_19_many_groups_stay_within_the_cap(self):
        """LOW 2: 500 single-option optgroups with 80-character CJK labels."""
        self.load("/pickers-groups")
        p = self.open_picker("s")
        size = len(json.dumps(p))
        self.assertLessEqual(size, PK.MESSAGE_MAX)
        used = sorted({i["g"] for i in p["items"] if i["g"] >= 0})
        self.assertEqual(used, list(range(len(p["groups"]))), "only the groups the items use are sent")
        self.assertLess(p["shown"], 500)
        self.w.send({"t": "pick_cancel", "id": p["id"]})
        self.load()

    def test_20_a_card_raised_during_the_pin_stops_the_press(self):
        """LOW 3: the hold is asked again after the pin's await."""
        original = PK.Pickers.pin
        raised = {}

        async def pin(this, tab, cdp, params):
            found = await original(this, tab, cdp, params)
            request = B.ApprovalRequest(tool="Bash", args={"command": "true"}, command="true")
            raised["request"] = request
            import threading
            threading.Thread(target=self.daemon.approvals.ask, args=(request,), daemon=True).start()
            for _ in range(200):
                if self.daemon.approvals.pending():
                    break
                await asyncio.sleep(0.01)
            return found
        self.ev()
        PK.Pickers.pin = pin
        try:
            x, y = POINTS["s"]
            self.w.send(B.mouse("mousePressed", x, y))
            self.wait(lambda: "request" in raised and self.daemon.approvals.pending(), what="the card")
            self.w.drain(0.3)
        finally:
            PK.Pickers.pin = original
            try:
                self.daemon.approvals.resolve(raised["request"].req_id, "deny")
            except (KeyError, ValueError):
                pass
        self.w.send(B.mouse("mouseReleased", x, y))
        self.w.settled()
        self.assertNotIn("mousedown", [e[0] for e in self.ev()], "the press never reached the page")
        self.assertFalse(self.is_open("s"))
        self.assertIsNone(self.host._pickers.current(self.tab))

    def test_21_a_second_pick_with_the_same_id_does_nothing(self):
        """LOW 4: one apply per picker."""
        p = self.open_picker("s")
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.assertEqual(self.closed(p["id"])["reason"], "applied")
        self.js(self.tid, "document.getElementById('s').value = 'a'")
        self.ev()
        closes = len([m for m in self.w.messages if m.get("t") == "picker_closed" and m.get("id") == p["id"]])
        self.w.send({"t": "pick", "id": p["id"], "index": 1})
        self.w.settled()
        self.w.drain(0.3)
        self.assertEqual(self.js(self.tid, "document.getElementById('s').value"), "a")
        self.assertEqual([e for e in self.ev() if e[0] in ("input", "change")], [])
        self.assertEqual(len([m for m in self.w.messages if m.get("t") == "picker_closed" and m.get("id") == p["id"]]), closes)

    def test_15_nothing_on_the_bus_or_in_the_log(self):
        # What the pin costs an owner's press (two CDP calls before it is sent).
        # and what the probe after its release costs where nothing opened.
        pickers, spent = self.host._pickers, {"pin": [], "opened": []}
        originals = {name: getattr(PK.Pickers, name) for name in spent}

        def timer(name):
            async def timed(this, *args):
                start = time.perf_counter()
                try:
                    return await originals[name](this, *args)
                finally:
                    spent[name].append(time.perf_counter() - start)
            return timed
        for name in spent:
            setattr(PK.Pickers, name, timer(name))
        try:
            for _ in range(20):
                self.click("text", hover=False)
        finally:
            for name, original in originals.items():
                setattr(PK.Pickers, name, original)
        for name, values in spent.items():
            values.sort()
            self.assertEqual(len(values), 20)
            self.measured[f"{name}_ms_p50"] = round(values[10] * 1000, 2)
            self.measured[f"{name}_ms_p95"] = round(values[18] * 1000, 2)
        self.assertIsNone(pickers.current(self.tab))
        self.open_picker("s")
        rss = rss_bytes(B.chrome_tree(config.BROWSER_DIR))
        self.measured["picker_rss_mb"] = round(rss / 2 ** 20, 1)
        self.assertLess(rss, 1.5 * 2 ** 30)
        self.assertNotIn(CANARY, json.dumps(self.records))
        self.assertNotIn(CANARY, "\n".join(self.logs))
        self.assertNotIn("Bravo", json.dumps(self.records))


# Only this file's sections: the harness's own tests belong to its suite.
for _name in [n for n in dir(B.BrowserStream) if n.startswith("test_")]:
    if _name not in PickersChrome.__dict__:
        setattr(PickersChrome, _name, None)


def agent_suite():
    """An agent's real `hud_browser_act` click on a select never offers a
    picker (the driving path sends its own CDP input, never `_deliver`)."""
    from tests.v2 import browser_drive_execute_real_check as E

    class AgentClick(E.Real):
        def test_agent_click_on_a_select_offers_no_picker(self):
            self.html("""<select aria-label='Choice' style='position:absolute;left:10px;top:10px;width:200px;height:30px'>
              <option>A</option><option>B</option></select>""")
            # An owner window shows the tab (a stand-in socket), so nothing but
            # the path itself keeps the agent's click from offering a picker.
            class Window:
                def __init__(self):
                    self.sent, self.closed = [], __import__("threading").Event()

                def send_text(self, text):
                    self.sent.append(text)

                def send_binary(self, data):
                    pass
            window, stream = Window(), self.tab.stream
            with stream._lock:
                stream._sock, stream.visible = window, True
            try:
                before = self.host._pickers.offered
                result = self.act("click", "Choice")
                self.assertEqual(result.outcome, "acted", result)
                time.sleep(0.3)
                self.assertEqual(self.host._pickers.offered, before)
                self.assertIsNone(self.host._pickers.current(self.tab))
                self.assertFalse(any('"picker"' in text for text in window.sent))
            finally:
                with stream._lock:
                    if stream._sock is window:
                        stream._sock = None
    return unittest.TestSuite([AgentClick("test_agent_click_on_a_select_offers_no_picker")])


if __name__ == "__main__":
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(Pure))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PickersChrome))
    if "--no-agent" not in sys.argv:
        suite.addTests(agent_suite())
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
