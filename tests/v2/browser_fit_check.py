"""A Remote page wider than its pane is laid out at its own width and drawn
scaled down (host.py `_fit`): production Chrome behind the daemon, a real
owner ticket and socket, fixture pages on an ephemeral port, no network.

Run only the cases below; the imported BrowserStream's broader suite is not
selected. No real profile, account, paid model or live Jarvis port is used.
"""
from pathlib import Path
import sys
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.v2 import browser_routes_check as B

# Every page repaints (a blinking corner), as a live page does: frames are
# what drive the repaint measurement.
BEAT = ("<style>@keyframes beat{from{background:#123}to{background:#9ab}}"
        "#beat{position:fixed;left:0;bottom:0;width:20px;height:20px;"
        "animation:beat .15s infinite alternate}</style><div id=beat></div>")
BASE = "<!doctype html><title>{t}</title><style>html,body{{margin:0}}{css}</style>"
EDGE = ("<div id=edge style='position:absolute;left:760px;top:0;width:40px;height:40px;cursor:pointer'"
        " onclick='window.__edge=(window.__edge||0)+1'></div>")
B.PAGES.update({
    # In-flow content: an empty (zero-height) body overflows nothing.
    "/fit800": BASE.format(t="fit800", css="body{min-width:800px}") + "<p>wide</p>" + EDGE + BEAT,
    "/fit2000": BASE.format(t="fit2000", css="body{min-width:2000px}") + "<p>wider</p>" + BEAT,
    "/plain": BASE.format(t="plain", css="") + "<p>fits</p>" + BEAT,
    # Spotify's shape: an 800 px minimum, and 12 px past any width.
    "/spot": BASE.format(t="spot", css="body{min-width:800px;position:relative}"
                         "#past{position:absolute;top:0;right:-12px;width:12px;height:10px}")
             + "<p>spot</p><div id=past></div>" + BEAT,
    "/grow": BASE.format(t="grow", css="#wide{width:calc(100vw + 50px);height:10px}")
             + "<div id=wide></div>" + BEAT,
})
NARROW = (500, 600)
PAGE_VIEW = "[innerWidth, innerHeight, devicePixelRatio]"


class Fit(B.BrowserStream):
    def open(self, path, width=500, height=600, scale=1):
        tid = self.owner("POST", "/browser/tabs", {"url": self.url(path)})["tab_id"]
        self.addCleanup(lambda: self.ask("DELETE", f"/browser/tabs/{tid}"))
        window = self.attached(tid)
        tab = self.host._tabs[tid]
        self.wait(lambda: tab.pane_ready and tab.cdp is not None
                  and self.js(tid, "document.readyState") == "complete", what=f"{path} loaded")
        self.counted(tab)
        window.send({"t": "visible", "visible": True})
        window.send({"t": "resize", "width": width, "height": height, "deviceScaleFactor": scale})
        return tid, window, tab

    def counted(self, tab):
        """Count the fit's CDP calls (measurements and overrides)."""
        tab.calls = {"Page.getLayoutMetrics": 0, "Emulation.setDeviceMetricsOverride": 0}
        original = tab.cdp.send

        async def send(method, params=None):
            if method in tab.calls:
                tab.calls[method] += 1
            return await original(method, params)
        tab.cdp.send = send

    def laid_out(self, tid, window, width, height, dpr, what):
        """Wait until the page and the newest frame are both at (width,
        height), the page at `dpr`, reading frames as a drawing window does."""
        def step(w):
            w.ack_last()
            if not w.frames or w.frames[-1][1:3] != (width, height):
                return False
            got = self.js(tid, PAGE_VIEW)
            return bool(got) and got[:2] == [width, height] and abs(got[2] - dpr) < 1e-3
        try:
            window.until(step, 10, what)
        except AssertionError:
            tab = self.host._tabs[tid]
            raise AssertionError(
                f"{what}: page {self.js(tid, PAGE_VIEW)}, content "
                f"{self.js(tid, 'document.documentElement.scrollWidth')}, frame "
                f"{window.frames[-1][1:3] if window.frames else None}, fit {tab.stream.fitted} "
                f"from {tab.fit_from} stuck {tab.fit_stuck}, calls {tab.calls}") from None

    def assert_view(self, tid, view):
        """The page's [innerWidth, innerHeight, devicePixelRatio]: the ratio
        to float32 (Chrome divides the emulated scale by its own RASTER_SCALE,
        so 1 reads 1.0000000298)."""
        got = self.js(tid, PAGE_VIEW)
        self.assertEqual(got[:2], view[:2])
        self.assertAlmostEqual(got[2], view[2], delta=1e-6)

    def measured_more(self, tab, window, n):
        """Read frames until the fit has measured `n` more times."""
        start = tab.calls["Page.getLayoutMetrics"]

        def step(w):
            w.ack_last()
            return tab.calls["Page.getLayoutMetrics"] >= start + n
        window.until(step, 10, f"{n} more measurements")

    def frames(self, window, n):
        start = len(window.frames)

        def step(w):
            w.ack_last()
            return len(w.frames) >= start + n
        window.until(step, 10, f"{n} more frames")

    # -- cases -------------------------------------------------------------------------------

    def test_01_min_width_fits_and_input_maps(self):
        tid, window, tab = self.open("/fit800")
        # 500 × 600 at 1 → 800 × 960 at 0.625: the pane's aspect, its device pixels.
        self.laid_out(tid, window, 800, 960, 0.625, "min-width page fitted")
        self.assertEqual(tab.casting, NARROW, "the screencast keeps the pane's device pixels")
        self.assertEqual(tab.stream.page_viewport(), (800, 960, 0.625))
        self.assertEqual(tab.stream.viewport, (500, 600, 1.0), "the window's own viewport is kept")
        # Stable over repaints: measured again, never widened or narrowed.
        overrides = tab.calls["Emulation.setDeviceMetricsOverride"]
        self.measured_more(tab, window, 3)
        self.assertEqual(tab.calls["Emulation.setDeviceMetricsOverride"], overrides)
        self.assert_view(tid, [800, 960, 0.625])
        # Page points past the pane's width are the page's (the window's
        # `clientToPage` gives them): a click and the owner's cursor query.
        B.click(window, 780, 20)
        window.settled()
        self.wait(lambda: self.js(tid, "window.__edge") == 1, what="click past the pane's width")
        window.send({"t": "cursor", "seq": 1, "x": 780, "y": 20})
        self.assertEqual(window.wait_for("cursor", seq=1)["cursor"], "pointer")
        # A wider pane resets to the pane; a narrow one fits again; a scale
        # carries through (2 → 1.25).
        window.send({"t": "resize", "width": 1000, "height": 700, "deviceScaleFactor": 1})
        self.laid_out(tid, window, 1000, 700, 1, "a wide pane resets")
        self.assertIsNone(tab.stream.fitted)
        self.assertEqual(tab.casting, (1000, 700))
        window.send({"t": "resize", "width": 500, "height": 600, "deviceScaleFactor": 2})
        self.laid_out(tid, window, 800, 960, 1.25, "fitted again at scale 2")
        self.assertEqual(tab.casting, (1000, 1200))
        # A burst of resizes ends on the last one.
        for w in range(400, 720, 20):
            window.send({"t": "resize", "width": w, "height": 600, "deviceScaleFactor": 1})
        window.send({"t": "resize", "width": 640, "height": 480, "deviceScaleFactor": 1})
        self.laid_out(tid, window, 800, 600, 0.8, "the last of a burst wins")

    def test_02_a_page_that_fits_is_not_widened(self):
        tid, window, tab = self.open("/plain")
        self.laid_out(tid, window, 500, 600, 1, "plain page at the pane's size")
        start, began = tab.calls["Page.getLayoutMetrics"], time.monotonic()
        self.frames(window, 20)
        self.measured_more(tab, window, 2)
        elapsed = time.monotonic() - began
        self.assertIsNone(tab.stream.fitted)
        self.assert_view(tid, [500, 600, 1])
        # Repaints are measured at most twice a second.
        self.assertLessEqual(tab.calls["Page.getLayoutMetrics"] - start, 2 * elapsed + 1.5)

    def test_03_bounds(self):
        # A page that grows with its viewport is widened, seen to grow as
        # much, and put back: widening cannot help it, and it never loops.
        tid, window, tab = self.open("/grow")
        self.wait(lambda: tab.fit_stuck, what="growing page recognised")
        self.laid_out(tid, window, 500, 600, 1, "100vw + 50px left at the pane's size")
        self.assertIsNone(tab.stream.fitted)
        overrides = tab.calls["Emulation.setDeviceMetricsOverride"]
        self.frames(window, 15)
        self.assertEqual(tab.calls["Emulation.setDeviceMetricsOverride"], overrides)
        self.assert_view(tid, [500, 600, 1])
        # An 800 px minimum that also runs 12 px past any width settles at
        # 800 + 12 instead of creeping 12 px a repaint.
        self.owner("POST", f"/browser/tabs/{tid}/navigate", {"url": self.url("/spot")})
        self.laid_out(tid, window, 812, 974, 500 / 812, "Spotify's shape fitted to its minimum")
        self.wait(lambda: tab.fit_stuck, what="12 px past any width recognised")
        overrides = tab.calls["Emulation.setDeviceMetricsOverride"]
        self.frames(window, 15)
        self.assertEqual(tab.calls["Emulation.setDeviceMetricsOverride"], overrides)
        self.assertEqual(self.js(tid, PAGE_VIEW)[:2], [812, 974])
        # A min-width beyond 3 × the pane stops at 3 × (drawn at 1/3).
        self.owner("POST", f"/browser/tabs/{tid}/navigate", {"url": self.url("/fit2000")})
        self.laid_out(tid, window, 1500, 1800, 1 / 3, "capped at 3 × the pane")
        self.assertEqual(tab.casting, NARROW)
        self.measured_more(tab, window, 2)
        self.assertEqual(self.js(tid, PAGE_VIEW)[:2], [1500, 1800])

    def test_04_min_width_after_load_is_fitted_on_repaint(self):
        tid, window, tab = self.open("/plain")
        self.laid_out(tid, window, 500, 600, 1, "plain page at the pane's size")
        self.measured_more(tab, window, 1)
        self.js(tid, "document.body.style.minWidth = '800px'")    # hydration
        self.laid_out(tid, window, 800, 960, 0.625, "min-width set after load fitted")
        # Between resets a repaint only widens: the min-width gone, the
        # layout stays until the next reset.
        self.js(tid, "document.body.style.minWidth = ''")
        self.measured_more(tab, window, 3)
        self.assertEqual(self.js(tid, PAGE_VIEW)[:2], [800, 960])

    def test_05_new_documents_reset_route_changes_do_not(self):
        tid, window, tab = self.open("/fit800")
        self.laid_out(tid, window, 800, 960, 0.625, "min-width page fitted")
        # The page now fits the pane; a repaint never narrows it.
        self.js(tid, "document.body.style.minWidth = '0px'; document.getElementById('edge').remove()")
        self.measured_more(tab, window, 2)
        self.assertEqual(self.js(tid, PAGE_VIEW)[0], 800, "a repaint never narrows")
        # An SPA's route changes (same-document) are not resets: no override,
        # no relayout at the pane's width, the layout kept.
        overrides = tab.calls["Emulation.setDeviceMetricsOverride"]
        generation = tab.document_generation
        self.js(tid, "history.pushState(null, '', '/fit800?next'); location.hash = 'a';"
                     " history.replaceState(null, '', '/fit800?again')")
        self.wait(lambda: tab.document_generation >= generation + 3, what="three same-document navigations")
        self.measured_more(tab, window, 3)
        self.assertEqual(tab.calls["Emulation.setDeviceMetricsOverride"], overrides,
                         "a same-document navigation sets no viewport")
        self.assert_view(tid, [800, 960, 0.625])
        self.assertEqual(tab.stream.page_viewport(), (800, 960, 0.625))
        self.assertTrue(all(f[1:3] == (800, 960) for f in window.frames[-5:]))
        # A new document is: page-initiated, then the owner's.
        self.js(tid, "location.assign('/plain')")
        self.laid_out(tid, window, 500, 600, 1, "a page-initiated navigation resets")
        self.assertIsNone(tab.stream.fitted)
        self.owner("POST", f"/browser/tabs/{tid}/navigate", {"url": self.url("/fit800")})
        self.laid_out(tid, window, 800, 960, 0.625, "fitted after navigating")
        self.owner("POST", f"/browser/tabs/{tid}/navigate", {"url": self.url("/plain")})
        self.laid_out(tid, window, 500, 600, 1, "navigation to a fitting page resets")
        self.assertIsNone(tab.stream.fitted)
        self.assertEqual(tab.casting, NARROW)

def suite():
    # Fit's own cases only, never the inherited BrowserStream sections.
    return unittest.TestSuite(Fit(name) for name in sorted(vars(Fit)) if name.startswith("test_"))


def load_tests(loader, tests, pattern):
    return suite()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(suite())
    raise SystemExit(not result.wasSuccessful())
