"""Check mail privacy, copied-subject protection, and source attribution in T3."""

import unittest

from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.index import Index
from jarvis.v3.triggers import _line

import planner_check as fixture
from planner_check import basis, iso, proposal, reply


class NoticeReview(unittest.TestCase):
    setUp = fixture.PlannerCheck.setUp
    register = fixture.PlannerCheck.register
    signal = fixture.PlannerCheck.signal
    signal_at = fixture.PlannerCheck.signal_at
    run_planner = fixture.PlannerCheck.run_planner

    def mail(self, mid, title, text, *, automated=True):
        received = self.now - 3601
        with Index() as index:
            index.add("gmail", mid, title=title, ts=received,
                      messages=[("sender", text, received)])
        return self.signal("mail.needs_reply", "mail", title=title, fields={
            "message": mid, "received": iso(received), "automated": automated,
            "untrusted_from": "user@example.com",
        })

    def deliver(self, item, summary=""):
        def run(name, _prompt, *, model, **_kwargs):
            text = summary if model == "haiku" else reply(item)
            return JobResult(name, model, ok=True, outcome="ok", text=text)

        self.assertEqual(self.run_planner(run).posted, 1)

    def wire(self):
        self.assertEqual(len(self.store.proposals("messaged")), 1)
        self.assertEqual(len(self.t3.starts), 1)
        return self.t3.starts[0][1]

    def check_private_code(self, code):
        source = self.mail("fixture-mail", "Fixture authentication notice",
                           f"Your sign-in code is {code}. It expires shortly.")
        self.deliver(proposal(basis(source)), f"The sign-in code is {code}. It expires soon.")
        wire = self.wire()
        self.assertNotIn(code, wire)
        self.assertNotIn("It expires soon.", wire)

    def test_short_numeric_code_stays_out_of_summary(self):
        self.check_private_code("2468")

    def test_alphanumeric_code_stays_out_of_summary(self):
        self.check_private_code("QR7M")

    def test_spaced_code_stays_out_of_summary(self):
        self.check_private_code("27-46 8")

    def test_completed_start_subject_still_has_copy_protection(self):
        title = "Review the remaining test fixtures"
        source = self.mail("fixture-mail", "Proposal: " + title, "Fixture status report.")
        item = proposal(basis(source), "thread.start")
        item["action"]["title"] = title
        self.deliver(item)
        self.assertEqual(len(self.store.proposals("briefed")), 1, self.t3.starts)
        self.assertFalse(self.t3.starts)

    def test_summary_is_attached_to_the_mail_it_describes(self):
        first = self.mail("first-mail", "Fixture registration", "A registration was received.")
        second = self.mail("second-mail", "Fixture shipment", "The shipment has left.",
                           automated=False)
        item = proposal(basis(first))
        item["basis"].append(basis(second))
        summary = "It confirms receipt of a registration."
        self.deliver(item, summary)
        wire = self.wire()
        self.assertIn(summary, wire)
        self.assertLess(wire.index(summary), wire.index("Fixture shipment"), wire)

    def test_mail_prompt_withholds_the_same_labelled_password_as_a_title(self):
        password = "2468"
        private_text = f"{'Pass' + 'word'}: {password}"
        title = _line(private_text)
        self.assertNotIn(password, title)
        self.mail("fixture-mail", title, f"Fixture sign-in notice. {private_text}")
        prompts = []

        def run(name, prompt, *, model, **_kwargs):
            prompts.append(prompt)
            return JobResult(name, model, ok=True, outcome="ok", text=reply())

        self.assertEqual(self.run_planner(run).outcome, "ok")
        prompt, = prompts
        self.assertFalse(private_text in prompt, "The planner received the fixture password verbatim.")


if __name__ == "__main__":
    unittest.main()
