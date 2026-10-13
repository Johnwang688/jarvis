"""Pure Browser host navigation identity checks; no Chrome or network."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import host as H


class NavigationIdentity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = H.BrowserHost()
        self.frame = SimpleNamespace(parent_frame=None, url="https://example.test/safe")
        self.page = SimpleNamespace(url=self.frame.url, main_frame=self.frame)
        self.tab = H._Tab("0123abcd", "p-0123abcd", None, self.page, page_identity=1)
        self.host._tabs[self.tab.id] = self.tab

    def test_same_url_and_same_document_navigation_invalidate_without_hud_report(self):
        self.host._landed(self.tab, self.frame)
        first = self.host.pane_tab(self.tab.id)
        self.assertEqual(first["document_generation"], 1)
        self.host._landed(self.tab, self.frame)
        second = self.host.pane_tab(self.tab.id)
        self.assertEqual(second["url"], first["url"])
        self.assertEqual(second["page_identity"], first["page_identity"])
        self.assertGreater(second["document_generation"], first["document_generation"])
        self.frame.url += "#section"
        self.host._landed(self.tab, self.frame)
        third = self.host.pane_tab(self.tab.id)
        self.assertEqual(third["url"], self.frame.url)
        self.assertGreater(third["document_generation"], second["document_generation"])

    async def test_cache_is_safe_from_the_browser_loop_and_returns_only_copies(self):
        self.host._landed(self.tab, self.frame)
        with patch.object(self.host, "_call", side_effect=AssertionError("must not submit to self")):
            before = self.host.pane_tab(self.tab.id)
            before["url"] = "mutated"
            after = self.host.pane_tab(self.tab.id)
            self.assertEqual(after["url"], self.frame.url)
            await asyncio.sleep(0)
            self.assertEqual(self.host.pane_tab(self.tab.id), after)
        self.assertNotIn("url", str(self.host.status()))

    async def test_invalid_or_closed_document_is_not_available_during_guard_replacement(self):
        self.host._landed(self.tab, self.frame)
        self.frame.url = "file:///untrusted"
        with patch.object(self.host, "_replace", new=AsyncMock()) as replace:
            self.host._landed(self.tab, self.frame)
            self.assertIsNone(self.host.pane_tab(self.tab.id))
            await asyncio.sleep(0)
            replace.assert_awaited_once()
        self.frame.url = "https://example.test/new"
        self.host._landed(self.tab, self.frame)
        self.assertIsNotNone(self.host.pane_tab(self.tab.id))
        self.host._tabs.clear()
        self.host._publish_snapshot()
        self.assertIsNone(self.host.pane_tab(self.tab.id))

    async def test_queued_landing_replacement_cannot_blank_a_newer_document(self):
        self.page.goto = AsyncMock()
        self.page.set_content = AsyncMock()
        self.frame.url = "file:///refused"
        self.host._landed(self.tab, self.frame)
        # A newer accepted landing wins before the queued guard gets CPU.
        self.frame.url = "https://example.test/owner"
        self.host._landed(self.tab, self.frame)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.page.goto.assert_not_awaited()
        self.page.set_content.assert_not_awaited()
        self.assertEqual(self.host.pane_tab(self.tab.id)["url"], self.frame.url)

    async def test_replacement_waits_for_navigation_lock_then_rechecks_generation(self):
        self.page.goto = AsyncMock()
        self.page.set_content = AsyncMock()
        lock = self.host._launching.setdefault(self.tab.profile, asyncio.Lock())
        await lock.acquire()
        self.frame.url = "file:///refused"
        self.host._landed(self.tab, self.frame)
        await asyncio.sleep(0)
        self.page.goto.assert_not_awaited()
        self.frame.url = "https://example.test/newer"
        self.host._landed(self.tab, self.frame)
        lock.release()
        await asyncio.sleep(0)
        self.page.goto.assert_not_awaited()

    async def test_current_replacement_explains_but_close_during_goto_prevents_content(self):
        async def goto(url, **_kwargs):
            self.frame.url = self.page.url = url
            self.host._landed(self.tab, self.frame)
        self.page.goto = AsyncMock(side_effect=goto)
        self.page.set_content = AsyncMock()
        generation = self.tab.document_generation
        await self.host._replace(self.tab, generation)
        self.page.goto.assert_awaited_once()
        self.page.set_content.assert_awaited_once()
        self.page.set_content.reset_mock()
        async def close_during_goto(url, **_kwargs):
            await goto(url)
            self.host._tabs.clear()
        self.page.goto.side_effect = close_during_goto
        await self.host._replace(self.tab, self.tab.document_generation)
        self.page.set_content.assert_not_awaited()

    def test_stopping_or_bad_identity_never_returns_cached_url(self):
        self.host._landed(self.tab, self.frame)
        for bad in (None, True, "", "../path", "0123ABCD"):
            self.assertIsNone(self.host.pane_tab(bad))
        self.host._stopped = True
        self.assertIsNone(self.host.pane_tab(self.tab.id))

    def test_subframe_navigation_does_not_replace_main_frame_identity(self):
        self.host._landed(self.tab, self.frame)
        before = self.host.pane_tab(self.tab.id)
        self.host._landed(self.tab, SimpleNamespace(parent_frame=self.frame, url="https://other.test"))
        self.assertEqual(self.host.pane_tab(self.tab.id), before)


if __name__ == "__main__":
    unittest.main()
