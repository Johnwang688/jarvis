import unittest

from jarvis.v3.jobs import default_jobs


class JobTableReview(unittest.TestCase):
    def test_ingesters_preserve_the_existing_daily_brief_jobs(self):
        table = {job.name: job for job in default_jobs()}
        self.assertLessEqual({"calendar", "schoolwork", "github"}, table.keys())
        self.assertLessEqual({"morning-brief", "evening-wrap"}, table.keys())


if __name__ == "__main__":
    unittest.main()
