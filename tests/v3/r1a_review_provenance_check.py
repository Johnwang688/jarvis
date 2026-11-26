"""Exercise real chat handlers with inert text from outside the session."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from a6b1_acceptance_check import Model, reply
from a6c1_acceptance_check import NOW
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.t3_client import Run
from jarvis.v3.tools.calendar import CalendarTools
from jarvis.v3.tools.skills import SkillTools
from jarvis.v3.work import Ledger, Registry, WorkService
from w1b_acceptance_check import Base, SEND, START


class Provenance(Base):
    def open_chat(self, *steps, work=None, request=None):
        self.model = Model(*steps)
        calendar = None
        if request is not None:
            self.token.write_text("fake-token")
            calendar = lambda approve=None: CalendarTools(request=request, now=lambda: NOW, approve=approve)
        return self.service(self.model, approvals=self.broker, work=work, calendar=calendar)

    def answer(self, decision, tool):
        item = self.next_card()
        self.assertEqual(item["tool"], tool)
        self.assertEqual(self.broker.answer(item["id"], item["digest"], decision),
                         {"approve": "approved", "deny": "denied"}[decision])
        return item

    def check_outside_result(self, svc, source_tool, marker, gated_tool):
        item = self.answer("deny", gated_tool)
        self.assertEqual(self.wait_idle(svc), "idle")
        delivered = [row["content"] for row in self.model.seen[1]["messages"]
                     if row["role"] == "tool" and row["name"] == source_tool]
        self.assertEqual(len(delivered), 1)
        self.assertIn(marker, delivered[0])
        self.assertEqual((svc.snapshot()["outside_text"], item["level"]), (True, "L2"))

    def work_service(self, thread_id):
        project = self.tmp / "sample-app"
        project.mkdir()
        registry = Registry(self.v3 / "work" / "projects.json")
        registry.add("sample-app", project, provider="codex", model="example-model")
        t3 = SimpleNamespace(start=lambda *args, **kwargs: Run(thread_id, "r-fixture", "queued"))
        return WorkService(registry=registry, ledger=Ledger(self.v3 / "work" / "threads.jsonl"), t3=t3)

    def test_thread_title_from_an_earlier_tainted_chat(self):
        marker = "Fixture title from a calendar event"
        work = self.work_service(SEND["thread_id"])

        def request(method, url, **kwargs):
            return 200, {"items": [{"id": "e-fixture", "summary": marker,
                                   "start": {"dateTime": "2030-01-08T10:00:00-06:00"},
                                   "end": {"dateTime": "2030-01-08T11:00:00-06:00"}}]}

        def start_from_calendar(messages):
            if any(row["role"] == "tool" and marker in row["content"] for row in messages):
                return reply(calls=[("work_start", dict(START, title=marker))])
            return reply("The calendar title was missing.")

        svc = self.open_chat(
            reply(calls=[("calendar_list", {"start": "2030-01-08", "end": "2030-01-09"})]),
            start_from_calendar,
            reply("Started."),
            reply(calls=[("work_threads", {})]), reply(calls=[("work_send", SEND)]), reply("Done."),
            work=work, request=request,
        )
        svc.send("Read the calendar fixture and use the event title for the new thread.")
        self.assertEqual(self.answer("approve", "work_start")["level"], "L2")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(work.threads()[0].title, marker)
        svc.new_session()
        self.model.seen.clear()
        svc.send("List my threads and check the follow-up.")
        self.check_outside_result(svc, "work_threads", marker, "work_send")

    def test_live_skill_name_from_a_tainted_reviewer(self):
        marker = "fixture-review-note"
        root = self.v3 / "skills"
        with patch.dict("os.environ", {"JARVIS_LEARNED_SKILLS": str(root)}):
            store = SkillStore(root, ledger=self.v3 / "skill-ledger", links=(), owner_roots=())
            reviewer = SkillTools(store, session="fixture-review", tainted=True)
            result = reviewer.skill_manage("create", marker, description="A fixture skill.", body="A fixture lesson.")
            self.assertIn("Flagged for review", result)
            svc = self.talk(reply(calls=[("skill_names", {})]), reply(calls=[("work_send", SEND)]), reply("Done."))
            svc.send("List the learned skills and check the follow-up.")
            self.check_outside_result(svc, "skill_names", marker, "work_send")


if __name__ == "__main__":
    unittest.main()
