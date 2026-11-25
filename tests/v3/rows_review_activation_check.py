"""Check feed row activation, keyboard navigation, and chip clipping in the built HUD."""

import unittest

import e2e_window_check


class RowsReview(e2e_window_check.Window):
    def open_decision(self):
        ask = "Publish the fixture report?"
        self.feed.post({
            "kind": "decision", "rank": 4, "ask": ask,
            "why_now": "The fixture review is complete.",
            "default": {"action": "none", "at": None},
        })
        self.start_daemon()
        self.open_window()
        self.page.evaluate("""() => {
            const fetch = window.fetch;
            window.feedAnswers = [];
            window.fetch = (url, options) => {
                if (url === "/v3/owner/feed/answer" && options?.method === "POST") {
                    window.feedAnswers.push(JSON.parse(options.body).action);
                }
                return fetch(url, options);
            };
        }""")
        row = self.feed_panel.get_by_role("article", name=ask)
        title = row.get_by_role("button", name=ask)
        self.expect(title).to_have_attribute("aria-expanded", "false")
        self.expect(row.get_by_role("button", name="Approve")).to_have_count(0)
        return row, title

    def test_row_double_click_only_discloses_the_brief(self):
        _, title = self.open_decision()
        bounds = title.bounding_box()
        x, y = bounds["x"] + 24, bounds["y"] + bounds["height"] / 2
        self.page.mouse.click(x, y)
        self.expect(title).to_have_attribute("aria-expanded", "true")
        self.assertEqual(self.page.evaluate("window.feedAnswers"), [])
        title.press("Space")
        self.expect(title).to_have_attribute("aria-expanded", "false")
        self.page.mouse.dblclick(x, y)
        self.assertEqual(self.page.evaluate("window.feedAnswers"), [])

    def test_row_typing_a_on_the_closed_title_does_not_approve(self):
        _, title = self.open_decision()
        title.focus()
        self.page.keyboard.press("a")
        self.assertEqual(self.page.evaluate("window.feedAnswers"), [])

    def test_row_typing_n_on_the_closed_title_does_not_dismiss(self):
        _, title = self.open_decision()
        title.focus()
        self.page.keyboard.press("n")
        self.assertEqual(self.page.evaluate("window.feedAnswers"), [])

    def test_row_can_tab_forward_to_approve_after_opening(self):
        row, title = self.open_decision()
        title.focus()
        self.page.keyboard.press("Enter")
        self.expect(title).to_be_focused()
        self.expect(title).to_have_attribute("aria-expanded", "true")
        approve = row.get_by_role("button", name="Approve")
        self.expect(approve).to_be_visible()
        visited = []
        for _ in range(20):
            self.page.keyboard.press("Tab")
            if not row.evaluate("(row) => row.contains(document.activeElement)"):
                break
            focused = row.locator(":focus")
            if focused.and_(approve).count():
                return
            if focused.and_(title).count():
                break
            visited.append(focused.text_content())
        self.fail(f"Forward Tab skipped Approve. Focused within the row: {visited!r}")

    def test_row_title_identifies_the_answers_it_discloses(self):
        row, title = self.open_decision()
        title.click()
        approve = row.get_by_role("button", name="Approve")
        self.expect(approve).to_be_visible()
        controlled_ids = (title.get_attribute("aria-controls") or "").split()
        self.assertTrue(approve.evaluate("""(button, ids) =>
            ids.some(id => document.getElementById(id)?.contains(button))
        """, controlled_ids), "The title's aria-controls omits the disclosed answer controls")

    def check_chip(self, *, recent):
        ask = "Review the fixture release?"
        brief = self.feed.post({
            "kind": "decision" if recent else "news", "rank": 5, "ask": ask,
            "why_now": "The fixture deadline is set.",
            "due": "2030-11-30T23:59:00Z",
            **({"default": {"action": "none", "at": None}} if recent else {}),
        })
        if recent:
            self.feed.answer(brief["id"], "approve")
        self.start_daemon()
        self.open_window()
        self.page.set_viewport_size({"width": 1280, "height": 720})
        row = self.feed_panel.get_by_role("article", name=ask)
        self.expect(row).to_have_attribute("data-collapsed", "true")
        self.page.evaluate("document.fonts.ready")
        chips = row.locator(".feed-chips")
        chip = chips.get_by_text("Approved", exact=True) if recent else chips.get_by_text("Due", exact=False)
        container = chips.bounding_box()
        bounds = chip.bounding_box()
        self.assertLessEqual(bounds["x"] + bounds["width"], container["x"] + container["width"] + 1)

    def test_row_news_due_chip_fits(self):
        self.check_chip(recent=False)

    def test_row_recent_outcome_chip_fits(self):
        self.check_chip(recent=True)


# The inherited window tests run in their own file.
for _name in dir(e2e_window_check.Window):
    if _name.startswith("test_"):
        setattr(RowsReview, _name, None)


if __name__ == "__main__":
    unittest.main()
