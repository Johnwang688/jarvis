from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import voice_laya_flow_check as fixture
from jarvis.v2.voice_intent import route as legacy_route
from windows.voiceactions import Actions
from windows.voiceflow import Flow


class Delta(unittest.TestCase):
    def setUp(self):
        self.c = fixture.Choices()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)

    def legacy(self, *, carried=True):
        c = self.c
        context = None
        if carried:
            with patch(fixture.ROUTE) as router:
                c.offer(router)
            context = Flow.retire_choices(c.wire)
        legacy = c.client()
        legacy.request_laya = False
        if carried:
            Flow.carry_choices(legacy, context)
        legacy.ping()
        self.assertTrue(legacy.web_navigation)
        self.assertFalse(legacy.laya_routing)
        return c.make_flow(legacy)

    def actions(self, name, app_id, windows=()):
        desktop = SimpleNamespace(windows=lambda: windows, focus=Mock(return_value=True))
        actions = Actions(desktop=desktop, runner=Mock(), launcher=Mock(), opener=Mock())
        actions.refresh_apps = Mock()
        actions.apps = [{"name": name, "id": app_id}]
        return actions

    def test_carried_offer_guards_resolved_legacy_launch(self):
        flow = self.legacy()
        app_id = "Chrome._crx_abcdefghijklmnopabcdefghijklmnop"
        actions = self.actions("noaa.gov", app_id)
        flow.actions = actions
        for phrase in ("open NOAA", "open NOAA gov"):
            with self.subTest(phrase=phrase):
                actions.launcher.reset_mock()
                self.assertEqual(legacy_route(phrase, "wake")["kind"], "open_app")
                self.c.clip("wake", phrase, flow=flow)
                actions.launcher.assert_not_called()

    def test_classic_paths_use_full_image_names(self):
        for stem in ("acme-report", "acme report", "acme.report", "acme_report"):
            with self.subTest(stem=stem):
                image = rf"C:\Program Files\Acme.Tools_2026\{stem}.exe"
                actions = self.actions("Quarterly Reports", image, [(52, "Report", image)])
                self.assertEqual(actions.route_names()[1], ("Quarterly Reports",))
                self.assertTrue(actions.switch_app("Quarterly Reports", exact=True))
                actions.desktop.focus.assert_called_once_with(52)

    def test_classic_path_does_not_name_a_folder_or_suffix_as_image(self):
        for app_id, decoy in (
            (r"C:\Program Files\Acme\acme.report.exe", r"C:\Other\report.exe"),
            (r"C:\Program Files\Acme\acme_report.exe", r"C:\Other\report.exe"),
            (r"C:\Program Files\Updater.exe.Backups\acme-report.exe", r"C:\Other\Updater.exe"),
        ):
            with self.subTest(app_id=app_id, decoy=decoy):
                actions = self.actions(
                    "Quarterly Reports", app_id, [(41, "Other utility", decoy), (52, "Report", app_id)]
                )
                self.assertTrue(actions.switch_app("Quarterly Reports", exact=True))
                actions.desktop.focus.assert_called_once_with(52)

    def test_carried_offer_preserves_unrelated_navigation_and_app(self):
        flow = self.legacy()
        self.c.clip("wake", "go to example.org", flow=flow)
        self.c.actions.navigate.assert_called_once_with("https://example.org")
        actions = self.actions("Calculator", "Calculator_123!App")
        flow.actions = actions
        self.c.clip("wake", "open Calculator", flow=flow)
        self.assertEqual(actions.launcher.call_args.args[0],
                         ["explorer.exe", r"shell:AppsFolder\Calculator_123!App"])

    def test_legacy_navigation_without_offer_is_unchanged(self):
        flow = self.legacy(carried=False)
        self.c.clip("wake", "go to noaa.gov", flow=flow)
        self.c.actions.navigate.assert_called_once_with("https://noaa.gov")

    def test_carried_offer_preserves_dictation(self):
        flow = self.legacy()
        for mode in ("hotkey", "dictation"):
            with self.subTest(mode=mode):
                self.c.typer.reset_mock()
                self.c.clip(mode, "type the words go to noaa.gov", flow=flow)
                self.c.typer.run.assert_called_once_with([("text", "go to noaa.gov")])
        self.c.actions.navigate.assert_not_called()
        self.c.actions.open_app.assert_not_called()

    def test_search_with_offered_domain_as_query_stays_a_search(self):
        flow = self.legacy()
        actions = self.actions("Calculator", "Calculator_123!App")
        flow.actions = actions
        self.c.clip("wake", "search for noaa.gov", flow=flow)
        actions.opener.assert_called_once_with("https://www.google.com/search?q=noaa.gov")
        actions.launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
