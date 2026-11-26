import unittest

import ctx1_check as context
import planner_check as fixture
from jarvis.v3 import triggers


class MailScrubCheck(unittest.TestCase):
    setUp = fixture.PlannerCheck.setUp
    register = fixture.PlannerCheck.register
    signal = fixture.PlannerCheck.signal
    signal_at = fixture.PlannerCheck.signal_at
    run_planner = fixture.PlannerCheck.run_planner
    add_mail = context.PlannerNoticeCheck.add_mail
    job = context.PlannerNoticeCheck.job

    def test_split_password_is_withheld_from_both_model_inputs(self):
        # A mail the title scrub would withhold gives no text to either model:
        # the planner sees the sender and subject only and no summary is requested.
        fake_password = "fake-password-with-4829"
        mail = f"{'Pass' + 'word'}:\n{fake_password}\n\nPlease review your account activity."
        self.assertNotIn(fake_password, triggers._line(mail))
        identifier = self.add_mail(text=mail)
        runner = self.job(fixture.proposal(fixture.basis(identifier)))
        self.assertEqual(self.run_planner(runner).posted, 1)
        self.assertEqual(runner.call_count, 1)
        prompt = runner.call_args_list[0].args[1]
        self.assertNotIn(fake_password, prompt)
        self.assertNotIn(f"mail text {identifier}", prompt)


if __name__ == "__main__": unittest.main()
