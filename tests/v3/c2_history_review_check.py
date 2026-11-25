"""Offline regression checks for Gmail history recovery and poll limits."""

from contextlib import contextmanager
import unittest

from c2_acceptance_check import Base, FakeGmail, message
from jarvis.v3.index import Index
from jarvis.v3.ingest import Runner
from jarvis.v3.ingest.gmail import GmailIngester


class HistoryReview(Base):
    @contextmanager
    def restart(self, api, *, page_docs):
        with Index(self.index.path) as index:
            gmail = GmailIngester(api, clock=lambda: self.now, page_docs=page_docs)
            yield Runner(index, [gmail], clock=lambda: self.now)

    def test_expired_history_reconciles_deleted_mail(self):
        api = FakeGmail([message("removed", "Old", "vanishedparcel"),
                        message("kept", "Keep", "retainedparcel", age_days=2)])
        with self.restart(api, page_docs=1) as runner:
            self.drain(runner, 4)
        self.assertEqual(len(self.hits("vanishedparcel")), 1)
        api.delete("removed")
        api.add(message("new", "New", "freshparcel"))
        api.expired_before = api.history_id
        with self.restart(api, page_docs=1) as runner:
            self.drain(runner, 6)
        self.assertEqual(len(self.hits("freshparcel")), 1)
        self.assertEqual(len(self.hits("retainedparcel")), 1)
        self.assertEqual(self.hits("vanishedparcel"), [])

    def test_paged_history_obeys_doc_budget_and_resumes_after_restart(self):
        class Paged(FakeGmail):
            def history(self, start_history_id, page_token=None):
                response = super().history(start_history_id, page_token)
                rows = response["history"]
                start = int(page_token or 0)
                end = start + 5
                response["history"] = rows[start:end]
                if end < len(rows):
                    response["nextPageToken"] = str(end)
                return response

        api = Paged()
        with self.restart(api, page_docs=5) as runner:
            runner.tick()
        for number in range(60):
            api.add(message(f"m{number}", "Shipment", f"parcelbatch{number}marker"))

        first = len(api.calls)
        counts = []
        for _ in range(18):
            with self.restart(api, page_docs=5) as runner:
                result = runner.tick()["gmail"]
            self.assertIsNone(result["error"])
            counts.append(sum(call[0] == "get" for call in api.calls[first:]))
            first = len(api.calls)
            self.now += 301
        for number in range(60):
            hits = self.hits(f"parcelbatch{number}marker")
            self.assertEqual([hit["ref"] for hit in hits], [f"m{number}"])
            self.assertEqual(len(self.index.read(hits[0]["id"])["messages"]), 1)
        self.assertLessEqual(max(counts), 5, f"Message GETs per poll: {counts}")


if __name__ == "__main__":
    unittest.main()
