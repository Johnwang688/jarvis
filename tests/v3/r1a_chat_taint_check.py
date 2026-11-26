"""Exercise session taint through the chat tool loop and private storage."""

import json
from threading import Event
import unittest
from unittest.mock import patch

from jarvis import tools

from a6b1_acceptance_check import Model, reply
from a6c1_acceptance_check import ADD, CREATE
from w1b_acceptance_check import Base, SEND, START


DATES = {"start": "2030-01-08", "end": "2030-01-09"}
PATCH = {"name": "favourite-tea", "old": "green", "new": "oolong"}
CLEAN_CALLS = {
    "get_datetime": {}, "calendar_free": DATES, "work_projects": {},
    "brief_post": {"kind": "news", "ask": "A fixture brief."},
    "vault_add": ADD, "vault_patch": PATCH, "vault_archive": {"name": "favourite-tea"},
    "calendar_create": CREATE, "calendar_update": {"event_id": "e1", "title": "Updated"},
    "calendar_delete": {"event_id": "e1"}, "work_start": START, "work_send": SEND,
}
TAINTING_CALLS = {
    "index_search": {"query": "fixture"}, "calendar_list": DATES,
    "work_show": {"thread_id": "t-known"}, "vault_list": {},
    "vault_read": {"name": "favourite-tea"}, "vault_search": {"query": "fixture"},
    "skill_view": {"name": "fixture"}, "work_threads": {}, "skill_names": {},
}


class ChatTaint(Base):
    def meta_path(self, svc):
        return svc.root / svc.snapshot()["session_id"] / "meta.json"

    def test_result_classification_including_errors(self):
        self.token.write_text("fake-token")
        svc = self.talk()
        for outside_text, calls in ((False, CLEAN_CALLS), (True, TAINTING_CALLS)):
            for name, args in calls.items():
                for result in ("Fixture result.", "Error: fixture failure."):
                    with self.subTest(name=name, result=result):
                        svc.new_session()
                        self.assertIs(svc.snapshot()["outside_text"], False)
                        self.model.steps.extend((reply(calls=[(name, args)]), reply("Done.")))
                        with patch.object(tools, "dispatch", return_value=tools.ToolResult(result)) as dispatch:
                            svc.send("Try the fixture tool.")
                            self.assertEqual(self.wait_idle(svc), "idle")
                        dispatch.assert_called_once()
                        self.assertEqual(self.results(), [result])
                        self.assertIs(svc.snapshot()["outside_text"], outside_text)
                        self.assertIs(json.loads(self.meta_path(svc).read_text())["outside_text"], outside_text)

    def test_unknown_tool_taints_before_the_model_sees_the_refusal(self):
        observed = []

        def observe(messages):
            observed.append((svc.snapshot()["outside_text"], json.loads(meta_path.read_text())["outside_text"],
                             [row["name"] for row in messages if row["role"] == "tool"]))
            return reply("Done.")

        svc = self.talk(reply(calls=[("future_fixture_tool", {})]), observe)
        svc.new_session()
        meta_path = self.meta_path(svc)
        svc.send("Try the unknown tool.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(observed, [(True, True, ["future_fixture_tool"])])
        self.assertTrue(self.results()[0].startswith("Error:"), self.results())

    def test_reload_uses_metadata_or_tool_history(self):
        svc = self.talk(reply(calls=[("index_search", {})]), reply("Done."))
        svc.send("Search the fixture index.")
        self.assertEqual(self.wait_idle(svc), "idle")
        meta_path = self.meta_path(svc)
        original = json.loads(meta_path.read_text())
        self.assertIs(original["outside_text"], True)
        self.assertTrue(svc.close())
        for flag in (True, None, False):
            with self.subTest(flag=flag):
                meta = dict(original, outside_text=flag)
                if flag is None:
                    del meta["outside_text"]
                meta_path.write_text(json.dumps(meta))
                reloaded = self.service(Model())
                self.assertEqual(reloaded.snapshot()["session_id"], meta_path.parent.name)
                self.assertIs(reloaded.snapshot()["outside_text"], True)
                self.assertTrue(reloaded.close())

    def test_failed_turn_preserves_taint_when_tool_history_is_dropped(self):
        svc = self.talk(reply(calls=[("index_search", {})]), RuntimeError("Fixture completion failed."),
                        reply("Recovered."))
        svc.send("Search the fixture index.")
        self.assertEqual(self.wait_idle(svc), "error")
        meta_path = self.meta_path(svc)
        transcript = json.loads(meta_path.with_name("messages.json").read_text())
        self.assertFalse(any(row["role"] == "tool" for row in transcript))
        self.assertIs(svc.snapshot()["outside_text"], True)
        svc.send("Continue without a tool.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(svc.close())
        reloaded = self.service(Model())
        self.assertEqual(reloaded.snapshot()["session_id"], meta_path.parent.name)
        self.assertIs(reloaded.snapshot()["outside_text"], True)
        self.assertTrue(reloaded.close())

        # With no saved tool result and no flag, a legacy transcript cannot prove it stayed clean.
        meta = json.loads(meta_path.read_text())
        del meta["outside_text"]
        meta_path.write_text(json.dumps(meta))
        self.assertIs(self.service(Model()).snapshot()["outside_text"], True)

    def test_a_saved_result_with_conflicting_tool_names_taints(self):
        svc = self.talk(reply(calls=[("get_datetime", {})]), reply("Done."))
        svc.send("Read the clock.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIs(svc.snapshot()["outside_text"], False)
        path = self.meta_path(svc).with_name("messages.json")
        self.assertTrue(svc.close())
        transcript = json.loads(path.read_text())
        result, = (row for row in transcript if row["role"] == "tool")
        result["name"] = "vault_read"
        path.write_text(json.dumps(transcript))
        reloaded = self.service(Model())
        self.assertEqual(reloaded.snapshot()["session_id"], path.parent.name)
        self.assertIs(reloaded.snapshot()["outside_text"], True)

    def test_close_prevents_writes_from_a_late_tool_result(self):
        entered, release = Event(), Event()

        def blocked(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return tools.ToolResult("Fixture result.")

        svc = self.talk(reply(calls=[("index_search", {})]), reply("Too late."))
        with patch.object(tools, "dispatch", side_effect=blocked):
            svc.send("Search the fixture index.")
            try:
                self.assertTrue(entered.wait(5))
                meta_path = self.meta_path(svc)
                before = meta_path.read_bytes()
                self.assertFalse(svc.close(grace_s=0))
            finally:
                release.set()
            self.assertTrue(svc.close())
        self.assertEqual(meta_path.read_bytes(), before)
        self.assertEqual(len(self.model.seen), 1)

    def test_steering_and_stop_preserve_taint(self):
        after_read, finish_read = Event(), Event()
        after_steer, finish_steer = Event(), Event()

        def continue_after_read(_messages):
            after_read.set()
            if not finish_read.wait(5):
                raise TimeoutError
            return reply(calls=[("get_datetime", {})])

        def stop_after_steer(_messages):
            after_steer.set()
            if not finish_steer.wait(5):
                raise TimeoutError
            return reply("Too late.")

        svc = self.talk(reply(calls=[("index_search", {})]), continue_after_read, stop_after_steer)
        svc.send("Search the fixture index.")
        try:
            self.assertTrue(after_read.wait(5))
            self.assertIs(svc.snapshot()["outside_text"], True)
            self.assertTrue(svc.send("Use the short format.")["steered"])
            finish_read.set()
            self.assertTrue(after_steer.wait(5))
            self.assertTrue(any("Use the short format." in row["content"]
                                for row in self.model.seen[-1]["messages"] if row["role"] == "user"))
            self.assertIs(svc.snapshot()["outside_text"], True)
            self.assertEqual(svc.stop()["state"], "stopping")
        finally:
            finish_read.set()
            finish_steer.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIs(svc.snapshot()["outside_text"], True)
        self.assertIs(json.loads(self.meta_path(svc).read_text())["outside_text"], True)
        svc.new_session()
        self.assertIs(svc.snapshot()["outside_text"], False)


if __name__ == "__main__":
    unittest.main()
