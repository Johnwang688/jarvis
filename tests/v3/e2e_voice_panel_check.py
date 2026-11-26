"""Run the Voice panel in Chromium against a loopback daemon and fake voice replies."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

import e2e_window_check


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
VOICE = "/v3/owner/voice"
MARKUP = '<img src=x onerror="window.fixtureMarkup=true"> Fixture text'
WINDOW_SIZES = ((1280, 720), (1100, 640), (900, 700))


class VoiceWindow(unittest.TestCase):
    setUpClass = classmethod(e2e_window_check.Window.setUpClass.__func__)

    def setUp(self):
        e2e_window_check.Window.setUp(self)
        e2e_window_check.Window.start_daemon(self)
        self.snapshot = {
            "problem": None,
            "state": "resting", "transcriber": {"name": "fixture-asr", "reason": "fixture transcriber ready"},
            "voice": {"name": "fixture-voice", "reason": "only voice"}, "detector": "fixture",
            "gpu_warm": False, "counters": {}, "brain": {"name": "fixture", "state": "idle", "turns": 1},
            "helper": {"connected": False, "listening": True},
            "settings": {"speed": {"value": 1.35, "min": 0.8, "max": 2.0},
                         "follow_up_s": {"value": 8, "min": 0, "max": 30},
                         "end_silence_ms": {"value": 500, "min": 300, "max": 1000}},
            "turns": [{
                "heard_at": NOW.timestamp() - 10, "text": MARKUP,
                "tools": [{"name": "fake_lookup", "ok": True}, {"name": "fake_update", "ok": False}],
                "approval_asked": True, "sentence_count": 2, "silence_ms": 500, "decode_ms": 87,
                "first_audio_ms": 2100, "first_say_s": None, "end_s": 3.5,
            }],
        }
        self.reads = 0
        self.helper_actions = []
        self.setting_actions = []
        self.refuse_helper = False
        self.refuse_setting = False
        self.full_rail = False
        self.confirmed_settings = {}
        self.errors = []
        self.addCleanup(self.assertEqual, self.errors, [])

    def open(self):
        context = self.browser.new_context(viewport={"width": 1280, "height": 720}, service_workers="block")
        self.addCleanup(context.close)
        context.set_default_timeout(e2e_window_check.WAIT_MS)
        context.route("**/*", self.route)
        context.on("weberror", lambda error: self.errors.append(str(error.error)))
        self.page = context.new_page()
        self.page.clock.install(time=NOW)
        self.page.clock.pause_at(NOW + timedelta(seconds=1))
        self.assertEqual(self.page.goto(self.origin + "/").status, 200)
        self.panel = self.page.get_by_role("region", name="Voice", exact=True)
        self.expect(self.panel.locator(".voice-state")).to_have_text("Resting")
        self.details = self.panel.locator(".voice-details")
        self.assertFalse(self.details.evaluate("element => element.open"))

    def open_details(self):
        self.details.get_by_text("Details and settings", exact=True).click()
        self.expect(self.panel.get_by_text("fixture-asr", exact=False)).to_be_visible()
        self.expect(self.panel.get_by_text("fixture transcriber ready", exact=False)).to_be_visible()

    def route(self, route):
        request = route.request
        if not request.url.startswith(self.origin + "/"):
            self.errors.append("Unexpected non-loopback browser request.")
            route.abort()
            return
        path = request.url.removeprefix(self.origin)
        if path == VOICE and request.method == "GET":
            self.reads += 1
            route.fulfill(json=deepcopy(self.snapshot))
        elif path == VOICE + "/helper" and request.method == "POST":
            action = request.post_data_json["action"]
            self.helper_actions.append(action)
            if self.refuse_helper:
                route.fulfill(status=409, json={"message": "The fixture helper could not be started."})
                return
            self.snapshot["helper"].update(connected=action == "start", canceller=True)
            route.fulfill(json={"message": f"Fixture helper action accepted ({action})."})
        elif path == VOICE + "/settings" and request.method == "POST":
            body = request.post_data_json
            self.setting_actions.append(body)
            if self.refuse_setting:
                route.fulfill(status=400, json={"message": "Fixture rejected that value."})
                return
            for name, value in body.items():
                self.snapshot["settings"][name]["value"] = self.confirmed_settings.get(name, value)
            route.fulfill(json={"settings": deepcopy(self.snapshot["settings"])})
        elif self.full_rail and path == "/v3/status":
            answer = route.fetch()
            jobs = {f"fixture-job-{n}": {"last_run": NOW.timestamp() - 90 * n,
                     "last_outcome": "ok", "failures": 0} for n in range(20)}
            route.fulfill(response=answer, json=answer.json() | {"jobs": jobs})
        elif self.full_rail and path == "/v3/owner/work":
            threads = [
                {"thread_id": f"fixture-{n}", "project": "fixture-project", "title": f"Fixture work {n}",
                 "status": "running", "message": n < 3, "waiting_on_owner": False,
                 "started_at": NOW.timestamp() - n, "updated_at": NOW.timestamp() - n}
                for n in range(50)
            ]
            route.fulfill(json={"projects": [{"name": "fixture-project"}], "threads": threads})
        else:
            route.continue_()

    def visible(self, value):
        self.page.evaluate("""(visible) => {
            Object.defineProperty(document, "hidden", { configurable: true, value: !visible });
            document.dispatchEvent(new Event("visibilitychange"));
        }""", value)

    def color(self, name):
        return self.panel.evaluate("""(panel, name) => {
            const probe = document.createElement("span");
            probe.style.color = `var(${name})`;
            panel.append(probe);
            const color = getComputedStyle(probe).color;
            probe.remove();
            return color;
        }""", name)

    def test_states_colors_and_helper_actions(self):
        self.open()
        chip = self.panel.locator(".voice-state")
        amber, red = self.color("--amber"), self.color("--red")
        for state in ("resting", "listening", "working", "speaking", "asking", "unavailable"):
            self.snapshot["state"] = state
            self.visible(True)
            self.expect(chip).to_have_text(state.title())
            colors = self.panel.evaluate("""panel => [...panel.querySelectorAll("*")]
                .map(element => getComputedStyle(element).color)""")
            self.assertEqual(amber in colors, state == "asking")
            self.assertEqual(red in colors, state == "unavailable")
        self.snapshot["state"] = "resting"
        self.visible(True)
        self.expect(chip).to_have_text("Resting")
        self.panel.get_by_role("button", name="Start", exact=True).click()
        self.expect(self.panel.get_by_text("Fixture helper action accepted (start).", exact=True)).to_be_visible()
        self.expect(self.panel.get_by_text("Helper connected", exact=True)).to_be_visible()
        self.expect(self.panel.get_by_text("echo cancelling on", exact=True)).to_be_visible()
        self.snapshot["helper"]["canceller"] = False
        self.visible(True)
        self.expect(self.panel.get_by_text("no echo cancelling", exact=True)).to_be_visible()
        self.panel.get_by_role("button", name="Stop", exact=True).click()
        self.expect(self.panel.get_by_text("Fixture helper action accepted (stop).", exact=True)).to_be_visible()
        self.expect(self.panel.get_by_text("Helper not running", exact=True)).to_be_visible()
        self.refuse_helper = True
        self.panel.get_by_role("button", name="Start", exact=True).click()
        error = self.panel.get_by_text("The fixture helper could not be started.", exact=True)
        self.expect(error).to_have_css("color", red)
        self.assertEqual(self.helper_actions, ["start", "stop", "start"])

    def test_problem_is_the_first_detail_and_disappears_when_cleared(self):
        self.open()
        self.open_details()
        problem = self.details.locator(".voice-details-body > .voice-error")
        self.expect(problem).to_have_count(0)
        for age in (5, 86400 * 3):
            self.snapshot["problem"] = {
                "text": "Claude isn’t signed in.", "hint": "Run “claude” in a terminal and sign in.",
                "at": NOW.timestamp() - age,
            }
            self.visible(True)
            self.expect(problem).to_have_text("Claude isn’t signed in.")
            self.expect(problem).to_have_attribute("title", "Run “claude” in a terminal and sign in.")
            self.expect(problem).to_have_css("color", self.color("--red"))
            self.assertTrue(problem.evaluate("el => el === el.parentElement.firstElementChild"))
            self.snapshot["problem"] = None
            self.visible(True)
            self.expect(problem).to_have_count(0)

    def test_settings_release_confirms_server_value_and_refusal_restores_it(self):
        self.open()
        self.open_details()
        speed = self.panel.get_by_role("slider", name="Speed")
        self.confirmed_settings["speed"] = 1.6
        speed.scroll_into_view_if_needed()
        box = speed.bounding_box()
        x, y = box["x"], box["y"] + box["height"] / 2
        self.page.mouse.move(x + 0.5 * box["width"], y)
        self.page.mouse.down()
        for fraction in (0.55, 0.65, 0.75):
            self.page.mouse.move(x + fraction * box["width"], y)
            self.assertEqual(self.setting_actions, [])
        self.page.mouse.move(x + 0.75 * box["width"], y + box["height"] + 10)
        sent = float(speed.input_value())
        self.page.mouse.up()
        self.expect(self.panel.get_by_text("1.60×", exact=True)).to_be_visible()
        self.expect(speed).to_have_value("1.6")
        self.assertEqual(self.setting_actions, [{"speed": sent}])
        self.refuse_setting = True
        speed.press("ArrowRight")
        error = self.panel.get_by_text("Fixture rejected that value.", exact=True)
        self.expect(error).to_have_css("color", self.color("--red"))
        self.expect(speed).to_have_value("1.6")
        self.expect(self.panel.get_by_text("1.60×", exact=True)).to_be_visible()
        self.assertEqual(self.setting_actions, [{"speed": sent}, {"speed": 1.65}])
        self.refuse_setting = False
        follow = self.panel.get_by_role("spinbutton", name="Follow-up (seconds)")
        follow.fill("12.5")
        follow.press("Enter")
        self.expect(error).to_have_count(0)
        self.expect(follow).to_have_value("12.5")
        self.assertEqual(self.setting_actions[-1], {"follow_up_s": 12.5})

    def test_end_silence_commits_once_and_displays_confirmed_value(self):
        self.snapshot["settings"]["end_silence_ms"]["value"] = 525
        self.open()
        wait = self.panel.get_by_role("spinbutton", name="Wait after you stop (ms)")
        self.expect(wait).to_have_count(0)
        self.open_details()
        self.expect(wait).to_have_value("525")
        for attribute, value in (("min", "300"), ("max", "1000"), ("step", "50")):
            self.expect(wait).to_have_attribute(attribute, value)
        hint = "Shorter replies sooner, but may cut you off if you pause."
        self.expect(self.details.get_by_text(hint, exact=True)).to_be_visible()
        self.expect(wait).to_have_accessible_description(hint)

        self.confirmed_settings["end_silence_ms"] = 650
        wait.fill("400")
        wait.fill("450")
        self.assertEqual(self.setting_actions, [])
        wait.press("Tab")
        self.expect(wait).to_have_value("650")
        self.assertEqual(self.setting_actions, [{"end_silence_ms": 450}])

        self.refuse_setting = True
        wait.fill("800")
        wait.press("Enter")
        self.expect(self.panel.get_by_text("Fixture rejected that value.", exact=True)).to_be_visible()
        self.expect(wait).to_have_value("650")
        wait.press("Tab")
        self.assertEqual(self.setting_actions, [{"end_silence_ms": 450}, {"end_silence_ms": 800}])

    def test_recent_turns_show_literal_text_and_details(self):
        self.open()
        recent = self.panel.locator(".voice-recent")
        self.assertFalse(recent.evaluate("element => element.open"))
        self.expect(self.panel.get_by_text("heard → first sound 2.1 s", exact=True)).to_be_visible()
        self.expect(self.panel.get_by_text(MARKUP, exact=True).first).to_be_visible()
        self.open_details()
        recent.locator(":scope > summary").click()
        turn = recent.locator(".voice-turn")
        turn.locator("summary").click()
        self.expect(turn.get_by_text("500 ms", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("87 ms", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("2.1 s", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("3.5 s", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("—", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("Asked for approval", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("✓ fake_lookup", exact=True)).to_be_visible()
        self.expect(turn.get_by_text("✗ fake_update", exact=True)).to_be_visible()
        self.expect(turn.locator("summary bdi")).to_have_text(MARKUP)
        self.expect(turn.locator("summary > span")).to_have_css("white-space", "pre-wrap")
        self.expect(self.panel.locator("img")).to_have_count(0)
        self.assertIsNone(self.page.evaluate("window.fixtureMarkup"))
        self.visible(True)
        self.expect(turn.locator("dd").first).to_be_visible()

    def advance_to_poll(self, milliseconds):
        with self.page.expect_response(self.origin + VOICE):
            self.page.clock.run_for(milliseconds)

    def test_mic_polling_duration_and_visibility(self):
        self.open()
        self.open_details()
        mic = self.panel.get_by_role("button", name="Mic test")
        mic.click()
        self.expect(self.panel.get_by_text("Start the helper first", exact=True)).to_be_visible()
        self.page.clock.run_for(1000)
        self.assertEqual(self.reads, 1)
        self.snapshot["helper"].update(connected=True, level=67)
        self.visible(True)
        self.expect(self.panel.get_by_text("Helper connected", exact=True)).to_be_visible()
        self.snapshot["helper"]["level"] = 68
        with self.page.expect_response(self.origin + VOICE):
            mic.click()
        meter = self.panel.get_by_role("meter", name="Microphone level")
        self.expect(meter).to_have_attribute("value", "68")
        self.expect(mic).to_have_attribute("aria-disabled", "true")
        for count in range(8):
            self.snapshot["helper"]["level"] = 100 + count
            self.advance_to_poll(125)
            self.expect(meter).to_have_attribute("value", str(100 + count))
        self.assertEqual(self.reads, 11)
        self.visible(False)
        self.page.clock.run_for(8_999)
        self.assertEqual(self.reads, 11)
        self.expect(meter).to_be_visible()
        self.page.clock.run_for(1)
        self.expect(meter).to_have_count(0)
        self.expect(mic).to_have_attribute("aria-disabled", "false")
        self.assertEqual(self.reads, 11)
        self.snapshot["transcriber"]["reason"] = "resumed polling fixture"
        self.visible(True)
        self.expect(self.panel.get_by_text("resumed polling fixture", exact=False)).to_be_visible()
        self.page.clock.run_for(9_999)
        self.assertEqual(self.reads, 12)
        self.advance_to_poll(1)
        self.assertEqual(self.reads, 13)

    def test_keyboard_controls_and_no_page_overflow(self):
        self.open()
        for width, height in ((1280, 720), (1920, 1080)):
            self.page.set_viewport_size({"width": width, "height": height})
            dimensions = self.panel.evaluate("""panel => [panel, panel.parentElement,
                document.querySelector(".agent-window"), document.documentElement].map(
                    element => [element.scrollWidth, element.clientWidth, element.scrollHeight, element.clientHeight]
                )""")
            self.assertTrue(all(sw <= cw for sw, cw, _, _ in dimensions), dimensions)
            self.assertTrue(all(sh <= ch for _, _, sh, ch in dimensions[-2:]), dimensions)
        expected = [
            (self.panel.get_by_role("button", name="Start", exact=True), "Start"),
            (self.details.locator(":scope > summary"), "Details and settings"),
            (self.panel.locator(".voice-recent > summary"), "Recent"),
            (self.panel.locator(".voice-turn > summary"), "Fixture text"),
            (self.panel.get_by_role("slider", name="Speed"), "Speed"),
            (self.panel.get_by_role("spinbutton", name="Follow-up (seconds)"), "Follow-up (seconds)"),
            (self.panel.get_by_role("spinbutton", name="Wait after you stop (ms)"), "Wait after you stop (ms)"),
            (self.panel.get_by_role("button", name="Mic test"), "Mic test"),
        ]
        for _ in range(30):
            self.page.keyboard.press("Tab")
            if expected[0][0].evaluate("element => element === document.activeElement"):
                break
        for index, (control, name) in enumerate(expected):
            if index:
                self.page.keyboard.press("Tab")
            self.expect(control).to_be_focused()
            self.expect(control).to_have_css("outline-style", "solid")
            self.expect(control).to_have_css("outline-width", "2px")
            self.assertIn(name, control.aria_snapshot())
            if index in (1, 2):
                self.page.keyboard.press("Enter")
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)

    def test_compact_and_expanded_panel_fit_a_full_rail(self):
        self.full_rail = True
        transcript = "W" * 300
        self.snapshot["helper"].update(connected=True, canceller=True)
        turn = self.snapshot["turns"][0]
        self.snapshot["turns"] = [dict(turn, heard_at=turn["heard_at"] - n, text=transcript) for n in range(20)]
        self.open()
        self.expect(self.page.locator(".agent-jobs li")).to_have_count(20)
        self.expect(self.page.locator(".work-list li")).to_have_count(47)
        self.expect(self.panel.locator(".voice-transcript")).to_have_css("text-overflow", "ellipsis")
        self.page.evaluate("() => document.fonts.ready")

        for expanded in (False, True):
            if expanded:
                self.open_details()
                self.panel.locator(".voice-recent > summary").click()
                self.expect(self.panel.locator(".voice-turn > summary")).to_have_count(20)
            for width, height in WINDOW_SIZES:
                with self.subTest(width=width, height=height, expanded=expanded):
                    self.page.set_viewport_size({"width": width, "height": height})
                    seen = self.panel.evaluate("""panel => {
                        const box = el => {
                            const { left, top, right, bottom } = el.getBoundingClientRect();
                            return [left, top, right, bottom];
                        };
                        const size = el => [el.scrollWidth, el.clientWidth, el.scrollHeight, el.clientHeight];
                        const rail = panel.parentElement;
                        const inside = (selector, parent) => [...parent.querySelectorAll(selector)].map(box);
                        return {
                            page: size(document.documentElement), window: size(document.querySelector('.agent-window')),
                            rail: size(rail), panels: inside(':scope > [data-panel]', rail), voice: box(panel),
                            compact: inside('.voice-top, .voice-helper, .voice-last', panel),
                            helper: inside('.voice-helper > *', panel),
                            body: size(panel.querySelector('.voice-details-body')),
                            lists: ['.agent-jobs', '.work-list'].map(selector => size(rail.querySelector(selector))),
                        };
                    }""")
                    for name in ("page", "window", "rail"):
                        sw, cw, sh, ch = seen[name]
                        self.assertLessEqual(sw, cw, name)
                        self.assertLessEqual(sh, ch, name)
                    for left, top, right, bottom in seen["panels"]:
                        self.assertTrue(0 <= left < right <= width and 0 <= top < bottom <= height, seen)
                    for _, _, scroll, client in seen["lists"]:
                        self.assertGreater(scroll, client, seen)
                        self.assertGreater(client, 0, seen)
                    left, top, right, bottom = seen["voice"]
                    for x0, y0, x1, y1 in seen["compact"]:
                        self.assertTrue(left <= x0 < x1 <= right and top <= y0 < y1 <= bottom, seen)
                    helper_text, helper_button, canceller_note = seen["helper"]
                    self.assertLessEqual(helper_text[2], helper_button[0], seen)
                    self.assertLessEqual(helper_button[2], canceller_note[0], seen)
                    self.assertLess(max(row[1] for row in seen["helper"]), min(row[3] for row in seen["helper"]), seen)
                    if not expanded:
                        print(f"Compact Voice at {width}×{height}: {bottom - top:.1f} px", flush=True)
                    else:
                        sw, cw, scroll, client = seen["body"]
                        self.assertLessEqual(sw, cw, seen)
                        self.assertGreater(client, 0, seen)
                        self.assertGreater(scroll, client, seen)
                    if expanded and (width, height) == (1100, 640):
                        settings = self.panel.get_by_role("spinbutton", name="Wait after you stop (ms)")
                        settings.scroll_into_view_if_needed()
                        self.assertGreater(self.panel.locator(".voice-details-body").evaluate("el => el.scrollTop"), 0)
                        self.expect(settings).to_be_in_viewport(ratio=1)
                        self.expect(self.panel.locator(".voice-state")).to_be_in_viewport(ratio=1)
                        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
                        self.panel.locator(".voice-details-body").evaluate("el => el.scrollTop = 0")


if __name__ == "__main__":
    unittest.main()
