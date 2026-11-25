"""Offline regressions for C2 history recovery, mail text, and MIME work."""

import unittest
from unittest.mock import patch

from c2_acceptance_check import Base, FakeGmail, PLANTED, b64, message
from jarvis.v3.index import Index
from jarvis.v3.ingest import Runner, gmail


GMAIL = "gmail" + ".com"      # the alias rules are Gmail-only; the mailbox name is an invented fixture

class DeltaReview(Base):
    def test_expired_history_checks_deleted_mail_that_aged_out(self):
        class DateFilteredGmail(FakeGmail):
            def list(self, query, page_token=None):
                self.calls.append(("list", query, page_token))
                cutoff = int(query.removeprefix("after:"))
                return {"messages": [{"id": mid} for mid, record in self.box.items()
                                     if int(record["internalDate"]) > cutoff * 1000]}

        api = DateFilteredGmail([
            message("gone", "Old", "expireddeletedneedle", age_days=179),
            message("kept", "Old", "expiredretainedneedle", age_days=179),
        ])
        runner = self.runner(gmail.GmailIngester(api, clock=lambda: self.now))
        self.assertIsNone(runner.tick()["gmail"]["error"])
        self.assertEqual(len(self.hits("expireddeletedneedle")), 1)
        self.assertEqual(len(self.hits("expiredretainedneedle")), 1)

        self.now += 3 * 86400
        api.delete("gone")
        api.expired_before = api.history_id
        for _ in range(6):
            with Index(self.index.path) as index:
                ingester = gmail.GmailIngester(api, clock=lambda: self.now, page_docs=1)
                result = Runner(index, [ingester], clock=lambda: self.now).tick()["gmail"]
                self.assertIsNone(result["error"])
            self.now += 301

        self.assertEqual(len(self.hits("expiredretainedneedle")), 1)
        self.assertEqual(self.hits("expireddeletedneedle"), [])

    def store(self, record):
        runner = self.runner(gmail.GmailIngester(FakeGmail([record]), clock=lambda: self.now))
        self.assertIsNone(runner.tick()["gmail"]["error"])
        row = next(row for row in self.index.recent(20) if row["ref"] == record["id"])
        doc = self.index.read(row["id"])
        return doc, "".join(chunk["text"] for chunk in doc["messages"])

    def test_underscore_delimited_credentials_are_scrubbed(self):
        wrapped = f"backup_{PLANTED}_copy"
        doc, body = self.store(message("archive", wrapped, "Report attached.",
                                       sender=f"{wrapped} <sender@example.org>",
                                       files=(f"{wrapped}.txt",), labels=("INBOX", wrapped)))
        for field, value in (("title", doc["title"]), ("metadata", doc["meta_json"]), ("body", body)):
            with self.subTest(field=field):
                self.assertIn("backup_", value)
                self.assertNotIn(PLANTED, value)

    def test_blank_line_between_quote_paragraphs(self):
        body = ("Thursday works.\n\nThanks,\nAda\n\n"
                "On Mon, 12 Oct 2026 at 10:00, Bob <bob@example.org> wrote:\n"
                "> earlierthreadneedle\n\n> laterthreadneedle")
        _, text = self.store(message("reply", "Schedule", body))
        self.assertIn("Thanks,\nAda", text)
        self.assertNotIn("earlierthreadneedle", text)
        self.assertNotIn("laterthreadneedle", text)

    def test_html_reply_with_plain_blockquote(self):
        record = message("htmlreply", "Schedule", "")
        record["payload"]["parts"] = [{
            "mimeType": "text/html",
            "body": {"data": b64("<p>Thursday works.</p>"
                                 "<p>On Mon, 12 Oct 2026 at 10:00, "
                                 "Bob &lt;bob@example.org&gt; wrote:</p><blockquote>"
                                 "Earlier meeting proposal.</blockquote>")},
        }]
        _, text = self.store(record)
        self.assertIn("Thursday works.", text)
        self.assertNotIn("Earlier meeting proposal.", text)

    def test_gmail_dotted_alias_matches_profile(self):
        api = FakeGmail([message("alias", "Reminder", "Prepare the agenda.",
                                 sender="Owner <Example.Fixture.Invalid+notes@" + GMAIL + ">")])
        with patch.object(api, "profile", return_value={
            "emailAddress": "examplefixtureinvalid@" + GMAIL, "historyId": "100",
        }):
            result = self.runner(gmail.GmailIngester(api, clock=lambda: self.now)).tick()["gmail"]
        self.assertIsNone(result["error"])
        doc = self.index.read(self.index.recent(1)[0]["id"])
        self.assertEqual(doc["messages"][0]["role"], "owner")

if __name__ == "__main__":
    unittest.main()
