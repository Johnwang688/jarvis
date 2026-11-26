"""The built v3 window is one screen: the page never scrolls, panels scroll inside.

Runs the real window against the isolated daemon of `e2e_window_check` with a
full feed and a long chat, at several window sizes. Set E2E_SHOTS to a folder
to keep a screenshot of each size.
"""

import os
from pathlib import Path
import time
import unittest

import e2e_window_check as window

SIZES = ((1440, 900), (1280, 720), (1100, 640), (900, 700))
MEASURE = """() => {
  const rect = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const one = (selector) => document.querySelector(selector);
  const win = one(".agent-window");
  const log = one(".chat-log");
  const feed = one('[data-panel="feed"]');
  const last = [...log.querySelectorAll(".chat-message")].at(-1);
  return {
    view: [innerWidth, innerHeight],
    page: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
    window: [win.scrollWidth, win.clientWidth, win.scrollHeight, win.clientHeight],
    panels: Object.fromEntries([...document.querySelectorAll("[data-panel]")].map((p) => [p.dataset.panel, rect(p)])),
    controls: Object.fromEntries([".chat-send", ".chat-composer textarea", ".approval-actions"]
      .filter(one).map((selector) => [selector, rect(one(selector))])),
    log: [log.scrollHeight, log.clientHeight, ...rect(log)],
    feed: [feed.scrollHeight, feed.clientHeight],
    last: last ? rect(last) : null,
    orb: rect(one("[data-orb]")),
  };
}"""


class Layout(window.Window):
    def fill(self, *, turns=8):
        for n in range(14):
            self.feed.post({
                "kind": "news", "rank": 5, "ask": f"Fixture brief {n}: a line of fixture text for the feed.",
                "why_now": "The fixture feed is deliberately longer than the window. " * 3,
            })
        self.start_daemon(*(self.reply(f"Fixture reply {n}. " + "More fixture words. " * 40) for n in range(turns)))
        self.open_window()
        for n in range(turns):
            self.send(f"Fixture message {n}")
            self.expect(self.transcript.get_by_text(f"Fixture reply {n}.", exact=False)).to_be_visible()
            self.expect_idle()

    def measure(self, width, height, name):
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.wait_for_function(f"innerWidth === {width} && innerHeight === {height}")
        # Two frames: the resize has been laid out and the transcript has followed.
        self.page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        shots = os.environ.get("E2E_SHOTS")
        if shots:
            self.page.screenshot(path=str(Path(shots) / f"{name}-{width}x{height}.png"))
        return self.page.evaluate(MEASURE)

    def assert_one_screen(self, seen, *, inside):
        width, height = seen["view"]
        self.assertLessEqual(seen["page"][0], width, "the page scrolls sideways")
        self.assertLessEqual(seen["page"][1], height, "the page scrolls")
        scroll_width, client_width, scroll_height, client_height = seen["window"]
        self.assertLessEqual(scroll_width, client_width + 1, "the window overflows sideways")
        self.assertLessEqual(scroll_height, client_height + 1, "the window overflows")
        boxes = {name: seen["panels"][name] for name in inside} | seen["controls"]
        for name, (left, top, right, bottom) in boxes.items():
            with self.subTest(view=seen["view"], box=name):
                self.assertGreater(right - left, 40)
                self.assertGreater(bottom - top, 20)
                self.assertGreaterEqual(left, -1)
                self.assertGreaterEqual(top, -1)
                self.assertLessEqual(right, width + 1)
                self.assertLessEqual(bottom, height + 1)

    def test_a_full_window_never_scrolls_the_page(self):
        self.fill()
        for width, height in SIZES:
            seen = self.measure(width, height, "full")
            with self.subTest(view=(width, height)):
                self.assert_one_screen(seen, inside=("chat", "feed", "status", "cron"))
                self.assertIn(".chat-send", seen["controls"])
                scroll, client, left, top, right, bottom = seen["log"]
                self.assertGreater(scroll, client, "the transcript should scroll inside its panel")
                self.assertGreaterEqual(client, 120, "the transcript has no room")
                self.assertGreater(seen["feed"][0], seen["feed"][1], "the feed should scroll inside its panel")
                # The newest message is in view: the transcript followed its end.
                self.assertLessEqual(seen["last"][3], bottom + 1)
                self.assertGreater(seen["last"][3], top)
                # The chat is the widest column.
                chat, feed = seen["panels"]["chat"], seen["panels"]["feed"]
                self.assertGreater(chat[2] - chat[0], feed[2] - feed[0])
                self.assertLessEqual(seen["orb"][3] - seen["orb"][1], 64)

    def test_briefs_are_short_rows_until_opened(self):
        for n in range(14):
            self.feed.post({
                "kind": "news", "rank": 5, "ask": f"Fixture brief {n}: a line of fixture text for the feed.",
                "why_now": "The fixture feed is deliberately longer than the window. " * 3,
            })
        self.feed.post({
            "kind": "decision", "rank": 4, "ask": "Schedule the fixture review?", "why_now": "The fixture is ready.",
            "default": {"action": "none", "at": None},
        })
        self.start_daemon()
        self.open_window()
        rows = self.feed_panel.get_by_role("article")
        self.expect(rows).to_have_count(15, timeout=window.WAIT_MS)
        self.measure(1280, 720, "rows")
        boxes = self.page.evaluate("""() => {
          const feed = document.querySelector('[data-panel="feed"]').getBoundingClientRect();
          return [...document.querySelectorAll('[data-panel="feed"] article')].map((row) => {
            const r = row.getBoundingClientRect();
            return [r.height, r.top >= feed.top && r.bottom <= feed.bottom, row.scrollWidth <= row.clientWidth + 1];
          });
        }""")
        self.assertTrue(all(height <= 64 for height, _, _ in boxes), boxes)
        self.assertTrue(all(fits for _, _, fits in boxes), "a row's text spills sideways")
        self.assertGreaterEqual(sum(inside for _, inside, _ in boxes), 8, "a 720 px window should show eight rows")

        decision = rows.filter(has_text="Schedule the fixture review?")
        self.expect(decision.get_by_role("button", name="Approve")).to_have_count(0)
        decision.get_by_role("button", name="Schedule the fixture review?").click()
        self.expect(decision.get_by_role("button", name="Approve")).to_be_visible()
        self.expect(decision.get_by_text("If you ignore it")).to_be_visible()
        seen = self.measure(1280, 720, "row-open")
        self.assert_one_screen(seen, inside=("chat", "feed", "status"))

        first = rows.filter(has_text="Fixture brief 0:")
        first.get_by_role("button", name="Got it").click()
        self.expect(self.feed_panel.locator(":scope > .feed-list").get_by_role("article")).to_have_count(14)

    def test_an_empty_window_fills_the_screen(self):
        self.start_daemon()
        self.open_window()
        seen = self.measure(1440, 900, "empty")
        self.assert_one_screen(seen, inside=("chat", "feed", "status", "cron", "t3", "vault", "skills"))
        for name in ("chat", "feed"):
            left, top, right, bottom = seen["panels"][name]
            self.assertGreater(bottom - top, 600, f"{name} should take the height of the window")

    def test_work_proposal_details_fit_in_the_window(self):
        from jarvis.v3.proposals import ProposalStore
        from proposal_route_check import AGENT, ASK, BRIEF, FOLDER, PROJECT, TAINT, TITLE, work_draft

        store = ProposalStore(self.data / "proposals")
        proposal = store.add(work_draft())
        store.brief(proposal["id"], self.feed)
        self.start_daemon()
        self.open_window()
        row = self.feed_panel.get_by_role("article", name=ASK)
        row.get_by_role("button", name=ASK).click()
        self.expect(row.get_by_role("button", name="Read the details first")).to_be_disabled()
        details = row.locator(".feed-proposal")
        details.get_by_text("Details", exact=True).click()
        self.expect(details.locator("summary + p")).to_have_text(TAINT)
        for value in (PROJECT, FOLDER, AGENT, TITLE):
            self.expect(details.get_by_text(value, exact=True)).to_be_visible()
        full_brief = details.locator("dd").last
        self.expect(full_brief).to_be_visible()
        self.assertEqual(full_brief.inner_text(), BRIEF)
        self.expect(row.get_by_role("button", name="Approve")).to_be_enabled()
        seen = self.measure(1280, 720, "proposal")
        self.assert_one_screen(seen, inside=("chat", "feed", "status"))
        summary = details.get_by_text("Details", exact=True)
        summary.press("Enter")
        self.expect(full_brief).to_be_hidden()
        self.expect(row.get_by_role("button", name="Read the details first")).to_be_disabled()
        summary.press("Space")
        self.expect(full_brief).to_be_visible()
        row.get_by_role("button", name="Approve").click()
        self.expect(row.get_by_text("Approved", exact=True)).to_be_visible()

    def test_a_full_work_list_scrolls_inside_its_panel(self):
        self.start_daemon()
        self.open_window()
        names = [f"project-{n}" for n in range(4)]
        now = time.time()
        threads = [
            {"thread_id": f"thread-{n}", "project": names[n % len(names)],
             "title": f"Fixture title {n} " + "W" * 60, "status": "running",
             "message": n < 3,
             "waiting_on_owner": n % 3 == 0, "started_at": now - n, "updated_at": now - n}
            for n in range(50)
        ]
        self.page.route(self.origin + "/v3/owner/work", lambda route: route.fulfill(json={
            "projects": [{"name": name} for name in names], "threads": threads,
        }))
        self.page.reload(wait_until="load")
        panel = self.page.get_by_role("region", name="T3", exact=True)
        self.expect(panel.locator("li")).to_have_count(50, timeout=window.WAIT_MS)
        self.expect(panel.get_by_role("heading").nth(1)).to_have_text("Messages from Jarvis")
        scroll_list = panel.locator(".work-list")
        controls = "button,a,input,select,textarea,iframe,details,summary,[tabindex],[contenteditable]"
        for width, height in (*SIZES, (390, 844)):
            with self.subTest(view=(width, height)):
                seen = self.measure(width, height, "work")
                if width > 680:
                    self.assert_one_screen(seen, inside=("chat", "feed", "status", "cron", "t3", "vault", "skills"))
                else:
                    self.assertLessEqual(seen["window"][0], seen["window"][1] + 1)
                    self.assertGreater(seen["window"][2], seen["window"][3])
                    self.assertLess(seen["panels"]["chat"][1], seen["panels"]["feed"][1])
                scroll, client = scroll_list.evaluate("el => [el.scrollHeight, el.clientHeight]")
                self.assertGreater(client, 0, "the list has no room")
                self.assertGreater(scroll, client, "the list should scroll inside the T3 panel")
                self.assertEqual(panel.locator(controls).count(), 0)

    def test_an_approval_card_stays_on_screen(self):
        self.request_vault_add("The note was saved.")
        for width, height in ((1280, 720), (1100, 640)):
            seen = self.measure(width, height, "approval")
            with self.subTest(view=(width, height)):
                self.assertIn(".approval-actions", seen["controls"])
                self.assert_one_screen(seen, inside=("chat", "feed", "status"))
        self.chat_panel.get_by_role("button", name="Deny").click()
        self.expect_idle()

    def test_the_job_list_shows_the_daemons_jobs(self):
        self.start_daemon()
        self.open_window()
        cron = self.page.get_by_role("region", name="Cron", exact=True)
        self.expect(cron.get_by_text("No scheduled jobs reported.", exact=True)).to_be_visible()
        # This daemon runs no jobs, so the table is added to its real answer.
        jobs = {f"fixture-job-{n}": {"last_run": time.time() - 90 * n, "last_outcome": "ok", "failures": 0}
                for n in range(18)}
        jobs["fixture-broken"] = {"last_run": time.time() - 30, "last_outcome": "error", "failures": 3}
        jobs["fixture-waiting"] = {"last_run": None, "last_outcome": None, "failures": 0}
        jobs["<img src=x onerror=alert(1)>"] = {"last_run": 1, "last_outcome": "ok", "failures": 0}

        def with_jobs(route):
            answer = route.fetch()
            route.fulfill(response=answer, json=answer.json() | {"jobs": jobs})

        self.page.route(self.origin + "/v3/status", with_jobs)
        rows = cron.get_by_role("listitem")
        self.expect(rows).to_have_count(20, timeout=window.WAIT_MS)
        self.expect(rows.filter(has_text="fixture-broken")).to_contain_text("error · 3 failed")
        self.expect(rows.filter(has_text="fixture-waiting")).to_contain_text("Not run yet")
        self.expect(rows.filter(has_text="fixture-job-2")).to_contain_text("OK")
        self.expect(rows.filter(has_text="fixture-job-2")).to_contain_text("3 min ago")
        self.assertEqual(cron.locator("button,a,input,select,textarea,img,[tabindex]").count(), 0)
        seen = self.measure(1280, 720, "jobs")
        self.assert_one_screen(seen, inside=("chat", "feed", "status", "cron", "t3", "vault", "skills"))
        scroll, client = self.page.evaluate(
            "() => { const el = document.querySelector('.agent-jobs'); return [el.scrollHeight, el.clientHeight]; }")
        self.assertGreater(scroll, client, "a long job list should scroll inside its panel")
        self.page.unroute(self.origin + "/v3/status", with_jobs)


# The inherited window tests run in their own file.
for _name in dir(window.Window):
    if _name.startswith("test_"):
        setattr(Layout, _name, None)


if __name__ == "__main__":
    unittest.main()
