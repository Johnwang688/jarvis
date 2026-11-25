"""Offline regressions for content and attribution in indexed Gmail."""

import json
import unittest
from unittest.mock import patch

from c2_acceptance_check import Base, FakeGmail, ME, PLANTED, b64, message
from jarvis.v3.ingest import gmail
from jarvis.v3.ingest.gmail import GmailIngester


class ContentReview(Base):
    def store(self, record):
        api = FakeGmail([record])
        result = self.runner(GmailIngester(api, clock=lambda: self.now)).tick()["gmail"]
        self.assertIsNone(result["error"])
        row = self.index.recent(1)[0]
        page = 1
        chunks = []
        while page is not None:
            doc = self.index.read(row["id"], page=page)
            chunks.extend(chunk["text"] for chunk in doc["messages"])
            page = doc["next_page"]
        return doc, "".join(chunks)

    def test_prose_ending_in_wrote_is_kept(self):
        body = "On the application under availability I wrote:\nI can start Thursday.\n\nThanks,\nAda"
        _, text = self.store(message("prose", "Application", body))
        self.assertIn(body, text)

    def test_greater_than_at_start_of_authored_code_line_is_kept(self):
        body = "Please run this report:\n\n```sh\nprintf '%s\\n' ready \\\n> report.txt\n```\n\nThanks,\nAda"
        _, text = self.store(message("code", "Report", body))
        self.assertIn(body, text)

    def test_localized_reply_attribution_is_cut_with_its_quote(self):
        body = ("Jeudi me convient.\n\nMerci,\nAda\n\n"
                "Le lun. 12 oct. 2026 à 10:00, Bob <bob@example.com> a écrit :\n"
                "> Est-ce que jeudi te convient ?")
        _, text = self.store(message("french", "Rendez-vous", body))
        self.assertIn("Merci,\nAda", text)
        self.assertNotIn("Est-ce que", text)
        self.assertNotIn("a écrit", text)

    def test_html_quote_does_not_drop_following_answer(self):
        record = message("inline", "Plan", "")
        record["payload"]["parts"] = [{
            "mimeType": "text/html",
            "body": {"data": b64('<p>Answering below.</p><blockquote type="cite">'
                                 'oldquotedplan</blockquote><p>I can start Thursday.</p>')},
        }]
        _, text = self.store(record)
        self.assertNotIn("oldquotedplan", text)
        self.assertIn("I can start Thursday.", text)

    def test_gmail_plus_address_matches_profile(self):
        local, domain = ME.split("@")
        record = message("plus", "Reminder", "remember this",
                         sender=f"Owner <{local.upper()}+reminders@{domain.upper()}>")
        doc, _ = self.store(record)
        self.assertEqual(doc["messages"][0]["role"], "owner")

    def test_encoded_display_name_does_not_hide_owner_mailbox(self):
        record = message("encoded", "Reminder", "remember this",
                         sender=f"=?UTF-8?Q?Owner=40work?= <{ME}>")
        doc, _ = self.store(record)
        self.assertEqual(json.loads(doc["meta_json"])["from_owner"], 1)

    def test_token_after_dash_is_redacted_in_mail_fields(self):
        prefix = f"backup-{PLANTED}"
        record = message("file", prefix, "Report included.",
                         sender=f"{prefix} <sender@example.org>", files=(f"{prefix}.txt",))
        doc, text = self.store(record)
        for field, value in {"subject": doc["title"], "metadata": doc["meta_json"],
                             "attachment": text.split("Attachments: ", 1)[1]}.items():
            with self.subTest(field=field):
                self.assertNotIn(PLANTED, value)

    def test_mime_scan_stops_after_all_output_budgets_are_full(self):
        files = tuple(f"image{number}.png" for number in range(600))
        record = message("wide", "Image report", "p" * 16_000, html="h" * 16_000, files=files)
        with patch.object(gmail, "_headers", wraps=gmail._headers) as headers:
            _, text = self.store(record)
        self.assertIn("image99.png", text)
        self.assertNotIn("image100.png", text)
        self.assertLessEqual(headers.call_count, 150)


if __name__ == "__main__":
    unittest.main()
